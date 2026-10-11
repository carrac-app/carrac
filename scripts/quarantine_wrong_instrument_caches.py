#!/usr/bin/env python3
"""Quarantine daily caches that hold the WRONG INSTRUMENT.

CoinPaprika's top-coins list is a crypto market-cap ranking, but
``CryptoMetricsClient.get_top_coins`` registered its symbols into
``crypto_map`` on its cache-MISS branches only. The list has a 1h TTL, so on
almost every call the function returned at the cache-hit early return and the
registration never ran. An unregistered symbol is not crypto to
``database._is_crypto``, and ``<TICKER>.US`` was the DEFAULT for anything
``get_ohlcv_data`` did not recognise — so a crypto ticker was resolved against
a US equity universe and came back with real, current, well-formed data for a
completely different company:

    M    22.82   Macy's, Inc.                (not MemeCore)
    SKY  90.25   Champion Homes, Inc.        (not Sky)
    PI  165.54   Impinj, Inc.                (not Pi Network)
    LEO   6.17   BNY Mellon Strategic Muni   (not UNUS SED LEO)
    BGB  11.27   Blackstone Strategic Credit (not Bitget Token)

Both defects are fixed in code (``data_sources/coinpaprika.py`` registers on
every return path; ``database.OHLCVDatabase._equity_path_eligible`` requires a
named reason before the equity path is taken). This script deals with what is
already on disk, which the code fix cannot: ``_get_crypto_ohlcv`` MERGES a
rebuild onto the existing frame (``pd.concat([cached_df, rebuilt])``), so
contaminated rows survive a rebuild forever unless the file is moved aside.

Safety
------
* Dry run by default. Nothing moves unless ``--execute`` is passed.
* QUARANTINE, NOT DELETE. Files move to ``<data-dir>/.carrac454-quarantine/``
  and can be moved back. The mixed population is the whole difficulty here:
  measured on a real data directory, of 20 fall-through caches TEN held real crypto
  that was merely stale (CRO, KCS, METH, MNT, OKB, WBTC, BFUSD, JST, USDD,
  USDT — 99.9–100% agreement with the crypto venue). A blunt purge of the
  fall-through set would have destroyed all of them.
* TWO independent identifications, and either one is enough to condemn — but
  each has to be a POSITIVE identification of the wrong instrument, never a
  suspicion:

    A. LIVE-QUOTE MATCH. The cached tail close matches the live Yahoo quote
       for the bare ticker to within --equity-tol (default 1%) AND disagrees
       with the crypto price for that symbol (CoinPaprika, from the top-coins
       list) by more than --crypto-tol (default 20%). Both halves are needed:
       the first alone would condemn a stablecoin sitting near some fund's
       NAV, the second alone would condemn any merely-stale crypto series.

    B. VENUE DISAGREEMENT. The cached series is compared row-by-row against
       the authoritative series from the crypto venue itself; if they agree on
       fewer than --min-agreement of at least --min-overlap shared dates, what
       we hold is not that coin's history.

  A alone is not enough, because it can only see contamination that is still
  UPDATING. Measured on that directory, GT (agreement 5.6%) and BDX (0.0%)
  are both unambiguously the equity — Goodyear and Becton Dickinson — but
  their caches froze days earlier, so their tails no longer match today's
  quote and A misses both. B is what actually decided them.

* Symbols that neither test can identify are KEPT, however suspicious. No
  Yahoo quote and no crypto venue means no evidence, and this script does not
  act on absence of evidence.
* Re-verifies every symbol at run time. There is no hardcoded hit list, so the
  script is safe to re-run and cannot act on a stale conclusion.
* Never touches anything outside ``--data-dir``. Never SSHes anywhere.

Usage
-----
    docker exec carrac python scripts/quarantine_wrong_instrument_caches.py
    docker exec carrac python scripts/quarantine_wrong_instrument_caches.py --execute

Read the dry-run report before ever passing --execute.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

QUARANTINE_DIRNAME = '.carrac454-quarantine'
_YAHOO_QUOTE = 'https://query1.finance.yahoo.com/v8/finance/chart/{}'
_HEADERS = {'User-Agent': 'Mozilla/5.0'}


def _yahoo_equity_price(symbol: str, timeout: float = 8.0):
    """Live regularMarketPrice for the bare ticker, or None.

    Deliberately the BARE ticker with no mapping: the question this answers is
    "is what we stored the US-listed instrument that shares this ticker?", and
    that is exactly the symbol ``get_stooq_symbol``'s ``<TICKER>.US``
    fall-through resolved to.
    """
    try:
        resp = requests.get(_YAHOO_QUOTE.format(symbol), headers=_HEADERS,
                            timeout=timeout)
        if not resp.ok:
            return None
        meta = resp.json()['chart']['result'][0]['meta']
        price = meta.get('regularMarketPrice')
        return float(price) if price else None
    except Exception:
        return None


def _venue_agreement(db, symbol: str, cached, row_tol: float):
    """(agreement_share, overlapping_rows) against the coin's own venue.

    Returns (None, 0) when no crypto venue serves the symbol — no
    third-party fallback is used here: it would be a metered last resort and
    a rate-limited empty response would read identically to "no such coin",
    which is exactly the wrong thing to be wrong about when the outcome is
    moving someone's data aside.
    """
    routed = False
    try:
        routed = (db.hyperliquid.is_supported(symbol)
                  or db.ccxt.is_supported(symbol)
                  or not db.binance.fetch_ohlcv(symbol, 5).empty)
    except Exception:
        return None, 0
    if not routed:
        return None, 0
    try:
        real = db._fetch_crypto_data(symbol, days=1500)
    except Exception:
        return None, 0
    if real is None or real.empty:
        return None, 0

    left = cached[['close']].copy()
    left.index = left.index.normalize()
    right = real[['close']].copy()
    right.index = right.index.normalize()
    joined = left.join(right, how='inner', lsuffix='_cached', rsuffix='_venue')
    if joined.empty:
        return None, 0
    denom = joined['close_venue'].abs().clip(lower=1e-12)
    agree = (joined['close_cached'] - joined['close_venue']).abs() / denom <= row_tol
    return float(agree.mean()), int(len(joined))


def _rel_diff(a: float, b: float) -> float:
    if b in (None, 0):
        return float('inf')
    return abs(a - b) / abs(b)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', default=None,
                        help="Cache dir (default: the app's own CSVCache dir)")
    parser.add_argument('--symbols', default=None,
                        help='Comma-separated subset; default = every '
                             'fall-through symbol with a cache file')
    parser.add_argument('--equity-tol', type=float, default=0.01,
                        help='Max relative gap to the Yahoo equity quote for a '
                             'positive equity identification (default 0.01)')
    parser.add_argument('--crypto-tol', type=float, default=0.20,
                        help='Min relative gap to the crypto price required '
                             'before condemning (default 0.20)')
    parser.add_argument('--min-overlap', type=int, default=30,
                        help='Shared dates needed before the venue comparison '
                             'is allowed to decide anything (default 30)')
    parser.add_argument('--min-agreement', type=float, default=0.50,
                        help='Share of shared dates that must agree with the '
                             'crypto venue to clear signal B (default 0.50)')
    parser.add_argument('--row-tol', type=float, default=0.02,
                        help='Per-row relative tolerance when comparing against '
                             'the venue (default 0.02)')
    parser.add_argument('--execute', action='store_true',
                        help='Actually move the condemned files')
    args = parser.parse_args()

    import database  # noqa: E402  (heavy import; after arg parsing)

    db = database.OHLCVDatabase()
    data_dir = Path(args.data_dir) if args.data_dir else Path(db.cache.cache_dir)
    quarantine = data_dir / QUARANTINE_DIRNAME

    # Crypto prices, straight from the provenance the symbols came from.
    try:
        crypto_px = {c['symbol'].upper(): c.get('price')
                     for c in (db._get_top_coins(100) or [])}
    except Exception as exc:
        print(f"! could not read the top-coins list ({exc}); every symbol will "
              f"fail signal 2 conservatively and be KEPT")
        crypto_px = {}

    # A FRESH instance, so crypto_map holds only the static base map — this is
    # the state a process is in when it has not seen a top-coins cache miss,
    # i.e. exactly the state that produced the contamination.
    base = database.OHLCVDatabase()
    equity_maps = set(base.stock_map) | set(base.etf_map) | set(base.commodity_map) \
        | set(base.index_map) | set(base.forex_map)

    if args.symbols:
        candidates = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    else:
        candidates = sorted(
            s for s in crypto_px
            if s not in base.crypto_map and s not in base.crypto_metric_map
            and s not in equity_maps)

    print(f"data dir      : {data_dir}")
    print(f"quarantine    : {quarantine}")
    print(f"candidates    : {len(candidates)}")
    print(f"mode          : {'EXECUTE' if args.execute else 'DRY RUN'}\n")
    header = ('%-10s %-12s %-11s %-12s %-9s %-12s %-9s %-14s %s' %
              ('SYMBOL', 'CACHED_TAIL', 'LAST_BAR', 'YAHOO_EQ', 'eq_gap',
               'CRYPTO_PX', 'cx_gap', 'VENUE_AGREE', 'VERDICT'))
    print(header)
    print('-' * len(header))

    condemned: list[tuple[str, Path]] = []
    kept = 0
    for symbol in candidates:
        df = db.cache.load(symbol)
        if df is None or df.empty:
            continue
        tail = float(df['close'].iloc[-1])
        last_bar = str(df.index[-1].date())

        yahoo = _yahoo_equity_price(symbol)
        crypto = crypto_px.get(symbol)
        eq_gap = _rel_diff(tail, yahoo) if yahoo else float('inf')
        cx_gap = _rel_diff(tail, crypto) if crypto else float('inf')

        signal_a = (yahoo is not None and eq_gap <= args.equity_tol
                    and cx_gap > args.crypto_tol)

        agreement, overlap = _venue_agreement(db, symbol, df, args.row_tol)
        signal_b = (agreement is not None and overlap >= args.min_overlap
                    and agreement < args.min_agreement)

        if signal_a or signal_b:
            why = 'A' if signal_a else ''
            why += 'B' if signal_b else ''
            verdict = f'QUARANTINE ({why})'
            path = Path(db.cache.get_path(symbol))
            if path.exists():
                condemned.append((symbol, path))
            else:
                verdict = 'QUARANTINE (no file?)'
        else:
            verdict = 'keep'
            kept += 1

        print('%-10s %-12.6g %-11s %-12s %-9s %-12s %-9s %-14s %s' % (
            symbol, tail, last_bar,
            ('%.6g' % yahoo) if yahoo else 'n/a',
            ('%.4f' % eq_gap) if eq_gap != float('inf') else '-',
            ('%.6g' % crypto) if crypto else 'n/a',
            ('%.4f' % cx_gap) if cx_gap != float('inf') else '-',
            ('%.1f%% of %d' % (100 * agreement, overlap)) if agreement is not None else 'no venue',
            verdict))

    print(f"\n{len(condemned)} to quarantine, {kept} kept")
    if not condemned:
        return 0
    if not args.execute:
        print("\nDRY RUN — nothing moved. Re-run with --execute to act.")
        return 0

    quarantine.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
    for symbol, path in condemned:
        target = quarantine / f"{path.stem}.{stamp}{path.suffix}"
        shutil.move(str(path), str(target))
        print(f"  moved {path.name} -> {target.relative_to(data_dir)}")
    print(f"\n{len(condemned)} file(s) quarantined under {quarantine}. "
          f"Move one back to restore it.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
