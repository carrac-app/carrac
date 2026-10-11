#!/usr/bin/env python3
"""Remove persisted synthetic bars from daily caches.

A synthetic bar is OHLC-identical with zero volume: the shape
``inject_synthetic_candle`` produces from one live price, and the shape a dead
upstream pair hands back when it repeats its last known price forever. Once
persisted, each one made the staleness guard read the series as one day old,
which authorised the next — 761 consecutive fabricated rows on AGIX before
anyone looked.

``CSVCache.save`` now refuses NEW ones (see ``_reject_persisted_synthetic_bars``).
This removes what already accumulated. Sibling of ``purge_weekend_bars.py`` and
deliberately built to the same rules:

* **Dry run by default.** ``--execute`` is required to write anything.
* **Three independent exemptions, ALL of which must clear before a row goes.**
  One check was not enough on the weekend purge — with only the symbol
  classifier it proposed deleting 6,784 legitimate rows — and the same caution
  applies here, because this script also deletes history.

  1. **Trailing run only.** Only a contiguous synthetic run at the END of the
     series is removed. An interior synthetic row is left alone: deleting one
     would punch a hole, which hides an interior gap forever
     behind a fresh tail. Fixing one bug by causing the other is not a fix.
  2. **Dated before today.** TODAY's flat zero-volume row is REAL — at 00:00
     UTC an exchange kline for the new session genuinely is open=prev close,
     H=L=C=open, volume 0, and it fills in as the day trades. BTC carries one
     every night.
  3. **A DERIVED series with no real bars is using that as its encoding.** The
     derived ratio feeds (ALTBTC, TOTAL3BTC) publish one computed value a day as
     OHLC with volume 0 and are perfectly alive; purging one would delete the
     whole feed. This was narrowed from "any series with no real
     bars", which read the encoding off the bars and so exempted pure
     fabrication too — a file that is synthetic end to end was untouchable by
     the very script written to clean it.

* Writes only through ``CSVCache.save(..., allow_shrink=True)`` — the app's own
  atomic temp-file + ``os.replace`` writer, so a reader never sees a partial
  file. ``allow_shrink=True`` is required and correct: removing rows is the
  entire point, and AGIX alone loses 761 of ~983.
* Never touches anything outside ``--data-dir``. Never SSHes anywhere.

Usage
-----
Run INSIDE the app container, against the live data dir:

    docker exec carrac python scripts/purge_synthetic_bars.py            # dry run
    docker exec carrac python scripts/purge_synthetic_bars.py --execute
    docker exec carrac python scripts/purge_synthetic_bars.py --symbols AGIX,MKR --execute

READ the dry-run output before ever passing --execute.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from carrac.staleness import synthetic_bar_mask  # noqa: E402
from csv_cache import CSVCache  # noqa: E402
from instrument_master import is_derived_series  # noqa: E402


def _symbol_from_filename(path: Path) -> str:
    return path.name[: -len('_daily.csv')]


def trailing_synthetic_rows(df: pd.DataFrame, today: pd.Timestamp,
                            symbol: str) -> pd.DatetimeIndex:
    """The rows this script would remove, applying all three exemptions.

    Returns an empty index when any exemption blocks the purge, so the caller
    never has to re-derive the rules.
    """
    if df is None or df.empty:
        return pd.DatetimeIndex([])
    mask = np.asarray(synthetic_bar_mask(df))
    if not mask.any() or (mask.all() and is_derived_series(symbol)):
        # exemption 3: no real bars anywhere AND the routing map says this is a
        # computed series -> this shape IS the encoding. The
        # symbol test matters. Without it the exemption covered any all-synthetic
        # file, including one that is pure fabrication, which made the residue
        # this script exists to remove permanently unreachable by it.
        return pd.DatetimeIndex([])

    # exemption 1: contiguous run at the END only
    tail = np.zeros(len(mask), dtype=bool)
    for i in range(len(mask) - 1, -1, -1):
        if not mask[i]:
            break
        tail[i] = True
    if not tail.any():
        return pd.DatetimeIndex([])

    # exemption 2: today's bar is a real session in progress
    dates = df.index.normalize()
    keep_today = np.asarray(dates >= today)
    drop = tail & ~keep_today
    return df.index[drop]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', default='/app/data')
    parser.add_argument('--symbols', default='',
                        help='Comma-separated subset. Default: every *_daily.csv.')
    parser.add_argument('--execute', action='store_true',
                        help='Actually rewrite the files. Default is a dry run.')
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"no such data dir: {data_dir}", file=sys.stderr)
        return 2

    wanted = {s.strip().upper() for s in args.symbols.split(',') if s.strip()}
    cache = CSVCache(cache_dir=str(data_dir))
    today = pd.Timestamp(pd.Timestamp.now().date())

    total_rows = 0
    affected = []
    exempt_all_synthetic = []

    for path in sorted(data_dir.glob('*_daily.csv')):
        symbol = _symbol_from_filename(path)
        if wanted and symbol.upper() not in wanted:
            continue
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception as e:
            print(f"  !! {symbol}: unreadable ({e})")
            continue
        if df.empty:
            continue

        mask = np.asarray(synthetic_bar_mask(df))
        if mask.any() and mask.all() and is_derived_series(symbol):
            exempt_all_synthetic.append(symbol)
            continue

        victims = trailing_synthetic_rows(df, today, symbol)
        if len(victims) == 0:
            continue

        affected.append((symbol, len(victims), str(victims.min().date()),
                         str(victims.max().date()), len(df)))
        total_rows += len(victims)

        if args.execute:
            cleaned = df.drop(index=victims)
            cache.save(symbol, cleaned, allow_shrink=True,
                       is_crypto=True)  # skip the weekend check; not its business

    verb = 'REMOVED' if args.execute else 'would remove'
    print(f"\n{verb} {total_rows} synthetic row(s) across {len(affected)} symbol(s)\n")
    for symbol, n, first, last, rows_before in affected:
        after = rows_before - n
        print(f"  {symbol:12s} {n:4d} rows  {first} .. {last}   "
              f"({rows_before} -> {after})")

    if exempt_all_synthetic:
        print(f"\nEXEMPT — derived series, flat/volumeless by construction, left "
              f"untouched ({len(exempt_all_synthetic)}):")
        print(f"  {', '.join(sorted(exempt_all_synthetic))}")

    if not args.execute and total_rows:
        print("\nDry run. Re-run with --execute to apply. Read the list above first.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
