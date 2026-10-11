#!/usr/bin/env python3
"""Purge phantom weekend-dated rows from weekday-instrument
daily CSV caches.

Weekday-only instruments (GOLD/SILVER/OIL/COPPER/NATGAS/PLATINUM/DXY and
similar) can pick up a phantom Sunday-dated row in their daily CSV that
upstream does not have.

ORDER MATTERS: purge only after the write path that produces the rows is
closed (CSVCache.save now drops them on the way in) — otherwise the row
reappears next Sunday and this script has to be re-run forever. Running it
earlier stops the immediate damage to published daily-change numbers, but
is a symptom fix.

Safety
------
* Dry run by default. Nothing is written unless ``--execute`` is passed.
* TWO independent exemptions, and a symbol must clear BOTH to be purged.
  One of them is not enough — the symbol
  classifier alone would delete thousands of legitimate rows across dozens
  of symbols (see ``trades_weekends``):

  1. By symbol: ``OHLCVDatabase._is_crypto`` / ``_is_crypto_metric``, the
     same classification every crypto ``cache.save()`` call site in
     database.py relies on. Necessary, but it misses dotted symbols
     (``BTC.D`` arrives here as ``BTC_D``) and delisted-but-real crypto.
  2. By shape: the series' own weekend SHARE. Asking "are there any
     weekend rows" would indeed be fooled by the defect being purged;
     asking "what share" is not — phantoms measured 0.09–2.4%, genuine
     24/7 and FX series 12–29%, with an empty band between.
* Writes only through ``csv_cache.CSVCache.save(..., allow_shrink=True)``
  — the app's own atomic temp-file + os.replace writer, so a reader never
  observes a half-written file. ``allow_shrink=True`` is required and
  correct here: removing rows is the entire point.
* Never touches anything outside ``--data-dir``. Never SSHes anywhere on
  its own.

Usage
-----
Run INSIDE the app container (or anywhere ``database.OHLCVDatabase`` and
its data-source clients import cleanly), against the live data dir:

    docker exec carrac python scripts/purge_weekend_bars.py               # dry run, all symbols
    docker exec carrac python scripts/purge_weekend_bars.py --execute     # actually rewrite
    docker exec carrac python scripts/purge_weekend_bars.py --symbols GOLD,SILVER,OIL --execute

Read the dry-run output before ever passing --execute against a live data
directory.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from csv_cache import CSVCache, trades_weekends  # noqa: E402


def _symbol_from_filename(path: Path) -> str:
    """Recover the cache symbol from a ``<SYM>_daily.csv`` filename.

    ``CSVCache.get_path`` upper-cases and strips ``.``/``^`` when building
    the filename, so this is lossy for symbols that originally contained
    those characters (e.g. ``BTC.D`` -> ``BTC_D_daily.csv``). None of the
    symbols this script targets (GOLD/SILVER/OIL/COPPER/NATGAS/PLATINUM/
    DXY/GRAM/GT/U) contain either character, so the round trip is exact
    for every real target; anything else is best-effort and shows up
    clearly in the dry-run report for a human to sanity-check before
    ``--execute``.
    """
    name = path.name
    suffix = '_daily.csv'
    return name[: -len(suffix)] if name.endswith(suffix) else path.stem


def find_weekend_rows(df: pd.DataFrame) -> pd.DatetimeIndex:
    return df.index[df.index.weekday >= 5]


# Second, INDEPENDENT exemption. The rule itself now lives in csv_cache
# alongside its other consumer — CSVCache.save, which drops phantom weekend
# rows on the way IN (the writer was database.py merging
# stooq.fetch output). Two copies of a rule that
# deletes data on one side and refuses data on the other would drift silently,
# so both import this one. The measured populations and the reason a SHARE
# test beats an ANY test are documented at the definition.
#
# What one check alone would cost: a dry run with only the symbol
# classifier proposed deleting thousands of legitimate rows
# across dozens of symbols — every FX major and all three dominance series — because
# ``_is_crypto`` is a live-universe lookup and CRO, JST, DEXE, OKB, KCS, WBTC
# and ~20 others have dropped out of it. A symbol must clear BOTH exemptions.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', default=os.environ.get('CARRAC_DATA_DIR', 'data'),
                        help='directory holding <SYM>_daily.csv files (default: data/, or $CARRAC_DATA_DIR)')
    parser.add_argument('--symbols', default=None,
                        help='comma-separated symbol allowlist (default: every non-crypto daily cache found)')
    parser.add_argument('--execute', action='store_true',
                        help='actually rewrite the CSVs (default is dry run — report only)')
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"ERROR: data dir not found: {data_dir}", file=sys.stderr)
        return 2

    try:
        from database import OHLCVDatabase
        db = OHLCVDatabase()
    except Exception as exc:
        print(f"ERROR: could not construct OHLCVDatabase for crypto classification: {exc}\n"
              f"Run this inside the app container (docker exec carrac ...), where "
              f"database.py's data-source clients import cleanly.", file=sys.stderr)
        return 2

    wanted: Optional[set] = None
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(',') if s.strip()}

    cache = CSVCache(cache_dir=str(data_dir))
    paths = sorted(data_dir.glob('*_daily.csv'))

    total_symbols_with_weekend_rows = 0
    total_rows_purged = 0
    skipped_crypto = 0
    skipped_weekend_traders = 0
    skipped_detail: list = []

    for path in paths:
        symbol = _symbol_from_filename(path)
        if wanted is not None and symbol.upper() not in wanted:
            continue

        if db._is_crypto(symbol) or db._is_crypto_metric(symbol):
            skipped_crypto += 1
            continue

        df = cache.load(symbol)
        if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
            continue

        weekend_dates = find_weekend_rows(df)
        if len(weekend_dates) == 0:
            continue

        # Second exemption, independent of the symbol classifier above.
        # See trades_weekends() for what this caught and why one check is
        # not enough for an operation that deletes history.
        if trades_weekends(df):
            skipped_weekend_traders += 1
            skipped_detail.append(
                f"{symbol} ({len(weekend_dates)}/{len(df)} = "
                f"{len(weekend_dates) / len(df):.1%} weekend)"
            )
            continue

        total_symbols_with_weekend_rows += 1
        total_rows_purged += len(weekend_dates)
        shown = ', '.join(str(d.date()) for d in weekend_dates[:6])
        more = f" +{len(weekend_dates) - 6} more" if len(weekend_dates) > 6 else ''
        verb = 'PURGING' if args.execute else 'WOULD PURGE'
        print(f"{verb} {symbol}: {len(weekend_dates)} weekend row(s) [{shown}{more}] "
              f"of {len(df)} total")

        if args.execute:
            clean = df.loc[~df.index.isin(weekend_dates)]
            cache.save(symbol, clean, allow_shrink=True, is_crypto=False)

    if skipped_detail:
        print(
            f"\nEXEMPT — these trade weekends, so their weekend bars are REAL "
            f"({skipped_weekend_traders} symbol(s) the symbol classifier missed):"
        )
        for line in skipped_detail:
            print(f"  {line}")

    print(
        f"\n{'purged' if args.execute else 'would purge'} "
        f"{total_rows_purged} weekend row(s) across {total_symbols_with_weekend_rows} "
        f"symbol(s); skipped {skipped_crypto} crypto/crypto-metric cache(s) "
        f"by symbol and {skipped_weekend_traders} more by weekend-share."
    )
    if not args.execute and total_rows_purged:
        print("Dry run only — re-run with --execute to actually rewrite the CSVs.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
