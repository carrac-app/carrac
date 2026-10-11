#!/usr/bin/env python3
"""
Backfill daily history for the six session-traded markets.

A fresh install's SPX/QQQ/GOLD/SILVER/OIL/DXY daily caches only go back to
whenever collecting started (about two years), not because the free sources are
shallow — ``data_sources/yahoo.py``'s ``fetch_daily`` hardcodes
``range=2y``, and Stooq (``data_sources/stooq.py``) is frequently
unreachable from many hosts, so in practice every one of these six
series was seeded once via Yahoo's 2-year window and never widened.

This script fetches the DEEPER history each source will actually give
(reusing the same clients, ticker maps, and HTTP/breaker plumbing the app
already uses elsewhere — no new HTTP calls), splices only the genuinely
older rows onto the front of the existing series, and validates the WHOLE
merged file before writing it out atomically.

SAFETY
------
* Never reads from or writes to a live data path. ``--existing-dir``
  is a local copy of the CSVs (copy them out of the data directory first), and
  a scratch dir has no `data/` in it that any live process reads.
* Writes only ever land under ``--out-dir``, and only via
  ``csv_cache.CSVCache`` — the same atomic temp-file + ``os.replace`` the
  app itself uses, so a reader (there is none in scratch, but the
  discipline matters) never sees a half-written file.
* No forward-filling, no interpolation. Weekend/holiday gaps in session
  markets are real and are preserved as gaps — see ``validate_series``.

Usage
-----
    .venv/bin/python3 scripts/backfill_daily_history.py \\
        --existing-dir /path/to/scratch/existing \\
        --out-dir /path/to/scratch/backfill \\
        [--symbols SPX,QQQ,GOLD,SILVER,OIL,DXY] \\
        [--cross-check-stooq]

Prints one report line per symbol and a summary table. Never installs
anything — the operator reviews ``--out-dir`` and copies it in by hand.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd

# --- project-root bootstrap ------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from csv_cache import CSVCache  # noqa: E402
from data_sources.circuit_breaker import CircuitBreaker  # noqa: E402
from data_sources.stooq import WRONG_SCALE_ON_STOOQ, StooqClient  # noqa: E402
from data_sources.yahoo import YahooClient  # noqa: E402

logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
logger = logging.getLogger('backfill_daily_history')

# The six session-traded markets this script targets. Crypto (BTC/ETH/XRP/SOL)
# already goes back to Binance's own 2017-08-17 start and needs nothing.
SESSION_SYMBOLS = ['SPX', 'QQQ', 'GOLD', 'SILVER', 'OIL', 'DXY']

# Columns + order exactly as data/<SYM>_daily.csv already has them today
# (matches the files the collectors write).
OHLCV_COLUMNS = ['open', 'high', 'low', 'close', 'volume']

# Deep-history anchor: earlier than any of the six tickers' real inception
# (SPX/^GSPC on Yahoo starts 1969-12-31; QQQ 1999; GOLD/SILVER/OIL futures
# 2000; DXY 1971). Yahoo/Stooq just hand back "as far back as we have."
DEEP_HISTORY_START = datetime(1962, 1, 1)

# A gap wider than this between two consecutive rows is reported as a
# finding (worth a human look) but is NOT treated as invalid — real
# multi-day market closures happen (e.g. long weekends, Christmas/New
# Year clusters). We never fill it; we just flag it.
DEFAULT_MAX_NORMAL_GAP_DAYS = 10

# Be polite to free sources between symbols. Yahoo's own burst limiter
# (api_rate_limiter.py: 'yahoo_minute', 10 calls/60s) is 1 call/symbol here
# (fetch_stooq_raw never falls through to Yahoo — see its docstring), so
# a few seconds of spacing is generous.
INTER_SYMBOL_SLEEP_SECS = 3.0


class ValidationError(Exception):
    """Raised when a merged series is structurally broken (must not be written)."""


# --------------------------------------------------------------------------- fetch

def make_clients() -> tuple[YahooClient, StooqClient]:
    """Fresh, in-memory-only clients — no shared state with the running app."""
    yahoo = YahooClient(CircuitBreaker('yahoo-daily-backfill'),
                         CircuitBreaker('yahoo-hourly-backfill'))
    stooq = StooqClient(CircuitBreaker('stooq-backfill'), yahoo)
    return yahoo, stooq


def fetch_yahoo_deep(yahoo: YahooClient, symbol: str,
                      start: datetime = DEEP_HISTORY_START) -> pd.DataFrame:
    """Fetch the full daily history Yahoo will give us for `symbol`.

    Reuses ``YahooClient``'s ticker map (``resolve_symbol``), breaker, and
    retry/backoff plumbing (``_request``) exactly as ``fetch_daily`` does.
    The only difference is ``period1``/``period2`` spanning decades instead
    of the hardcoded ``range=2y`` — the same period1/period2 idiom
    ``fetch_hourly`` already uses on this client, just with ``interval=1d``.

    NOTE: `range=max` (Yahoo's own "everything" shorthand) was tried first
    and silently returns thinned/coarse data for wide spans (168 rows for
    ^GSPC across 42 years, i.e. not daily). Explicit period1/period2 does
    not have that problem (14267 dense daily rows, median 1-day spacing).
    """
    yf_symbol = yahoo.resolve_symbol(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
    resp = yahoo._request(
        'GET', url,
        params={
            'interval': '1d',
            'period1': int(start.timestamp()),
            'period2': int(datetime.now().timestamp()),
        },
        headers=yahoo.HEADERS,
        breaker=yahoo.daily_breaker,
    )
    if resp is None or not resp.ok:
        return pd.DataFrame()
    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("Yahoo deep-fetch parse error for %s: %s", symbol, exc)
        return pd.DataFrame()

    result = data.get('chart', {}).get('result') or []
    if not result:
        return pd.DataFrame()
    chart = result[0]
    timestamps = chart.get('timestamp', [])
    quote = chart.get('indicators', {}).get('quote', [{}])[0]
    if not timestamps:
        return pd.DataFrame()

    df = pd.DataFrame({
        'open': quote.get('open', []),
        'high': quote.get('high', []),
        'low': quote.get('low', []),
        'close': quote.get('close', []),
        'volume': quote.get('volume', []),
    }, index=pd.to_datetime(timestamps, unit='s').normalize())
    df = df.dropna(subset=['close'])
    df = df[~df.index.duplicated(keep='last')].sort_index()
    return df


def fetch_stooq_raw(stooq: StooqClient, symbol: str,
                     start: datetime = DEEP_HISTORY_START) -> pd.DataFrame:
    """Deep history via Stooq's CSV endpoint ONLY — deliberately does not
    fall through to ``StooqClient.fetch()``'s built-in Yahoo fallback.

    Reuses the same ticker maps (``get_stooq_symbol``) and ETF->spot
    multipliers (GOLD/SILVER) the app already uses; only the URL fetch +
    parse is duplicated from ``StooqClient.fetch``, minus its fallback
    branch — using that fallback here would make a "cross-check against
    Stooq" meaningless (silently comparing Yahoo against itself) and burns
    a second Yahoo call we don't need.

    FINDING: as of this run, Stooq's ``/q/d/l/`` endpoint returns a
    JavaScript proof-of-work bot-challenge page (not CSV) for every ticker
    tried (AAPL.US, ^SPX, QQQ.US, GLD.US) from this network. Confirmed via
    ``StooqClient.fetch()`` tracing: every call silently fell through to
    ``_yahoo_fallback`` internally. This function returns an empty frame in
    that case instead of masking it.
    """
    # Refuse the symbols where Stooq only has a wrong-scale ETF
    # proxy. This path parses Stooq CSV directly rather than going through
    # StooqClient.fetch(), so it does NOT inherit that method's reroute to
    # Yahoo — without this check a backfill would write exactly the corrupt
    # rows the reroute exists to prevent.
    if symbol.upper() in WRONG_SCALE_ON_STOOQ:
        logger.info(f"{symbol}: skipping Stooq — no spot series, use the Yahoo path")
        return pd.DataFrame()

    stooq_symbol = stooq.get_stooq_symbol(symbol)
    d1 = (start + timedelta(days=1)).strftime('%Y%m%d')
    d2 = datetime.now().strftime('%Y%m%d')
    url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d&d1={d1}&d2={d2}"

    resp = stooq._request('GET', url)
    if resp is None or not resp.ok:
        return pd.DataFrame()

    body = resp.text
    if 'Exceeded' in body or 'limit' in body.lower() or body.lstrip()[:15].lower().startswith('<!doctype html'):
        return pd.DataFrame()

    try:
        df = pd.read_csv(StringIO(body))
    except Exception:
        return pd.DataFrame()
    if df.empty or 'Date' not in df.columns:
        return pd.DataFrame()

    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)
    df = df.sort_index()
    df.columns = df.columns.str.lower()
    df = df[[c for c in OHLCV_COLUMNS if c in df.columns]]
    if 'volume' not in df.columns:
        df['volume'] = 0

    multiplier = stooq.etf_to_spot_multipliers.get(symbol.upper(), 1)
    if multiplier != 1:
        for col in ('open', 'high', 'low', 'close'):
            df[col] = df[col] * multiplier

    return df


# --------------------------------------------------------------------------- compare

def compare_overlap(old_df: Optional[pd.DataFrame], new_df: pd.DataFrame,
                     columns=('open', 'high', 'low', 'close')) -> tuple[int, float, float, Optional[str]]:
    """Compare two series on the dates they both cover.

    Returns (overlap_rows, max_abs_diff, max_pct_diff, column_with_max_diff).
    Never mutates either input; this is report-only.
    """
    if old_df is None or old_df.empty or new_df.empty:
        return 0, 0.0, 0.0, None

    common = old_df.index.intersection(new_df.index)
    if len(common) == 0:
        return 0, 0.0, 0.0, None

    max_abs = 0.0
    max_pct = 0.0
    max_col = None
    for col in columns:
        if col not in old_df.columns or col not in new_df.columns:
            continue
        a = old_df.loc[common, col].astype(float)
        b = new_df.loc[common, col].astype(float)
        abs_diff = (a - b).abs()
        pct_diff = (abs_diff / a.abs()).replace([float('inf'), float('-inf')], pd.NA)
        col_max_abs = float(abs_diff.max()) if len(abs_diff) else 0.0
        col_max_pct = float(pct_diff.max(skipna=True)) if pct_diff.notna().any() else 0.0
        if col_max_abs > max_abs:
            max_abs, max_col = col_max_abs, col
        max_pct = max(max_pct, col_max_pct)

    return len(common), max_abs, max_pct, max_col


# --------------------------------------------------------------------------- merge

def merge_history(old_df: Optional[pd.DataFrame], new_df: pd.DataFrame) -> pd.DataFrame:
    """Splice `new_df` rows strictly BEFORE `old_df`'s earliest date onto the
    front of `old_df`. Existing rows are never altered or dropped — only
    genuinely older trading days are added. The overlap is used solely for
    ``compare_overlap``'s agreement check, never to overwrite `old_df`
    (Carrac's already-cached rows stay authoritative for dates it already
    covers).
    """
    new_df = new_df.sort_index()
    if old_df is None or old_df.empty:
        return new_df

    old_start = old_df.index.min()
    older = new_df[new_df.index < old_start]
    merged = pd.concat([older, old_df])
    merged = merged[~merged.index.duplicated(keep='last')].sort_index()
    return merged


def validate_series(df: pd.DataFrame, symbol: str,
                     max_normal_gap_days: int = DEFAULT_MAX_NORMAL_GAP_DAYS) -> list[str]:
    """Whole-file validation (the central risk: a torn/interior
    hole that only shows up if you check every row, not just the last one).

    Raises ``ValidationError`` on structural defects that must never be
    written: duplicate dates, non-monotonic order, missing values.

    Returns a list of non-fatal *findings* — gaps wider than
    `max_normal_gap_days`. These are reported, not fixed: weekend/holiday
    gaps in session markets are real data, not defects, and we never
    fabricate or forward-fill a bar to paper over one.
    """
    if df.empty:
        raise ValidationError(f"{symbol}: merged series is empty")

    if df.index.has_duplicates:
        dupes = sorted({str(d.date()) for d in df.index[df.index.duplicated()]})
        raise ValidationError(f"{symbol}: duplicate dates in merged file: {dupes[:5]}")

    if not df.index.is_monotonic_increasing:
        raise ValidationError(f"{symbol}: dates are not strictly increasing")

    missing_cols = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing_cols:
        raise ValidationError(f"{symbol}: missing columns {missing_cols}")

    na_cols = [c for c in ('open', 'high', 'low', 'close') if df[c].isna().any()]
    if na_cols:
        raise ValidationError(f"{symbol}: NaN values present in columns {na_cols}")

    findings = []
    if len(df) > 1:
        gaps = df.index.to_series().diff().dt.days.dropna()
        wide = gaps[gaps > max_normal_gap_days]
        for date, gap in wide.items():
            prev_date = date - pd.Timedelta(days=int(gap))
            findings.append(f"gap {int(gap)}d: {prev_date.date()} -> {date.date()}")

    return findings


def flat_candle_count(df: pd.DataFrame) -> int:
    """Rows where open==high==low==close — a common source artifact for a
    placeholder/no-trade bar. Not fatal (some illiquid days genuinely don't
    move), but worth surfacing as a data-quality doubt.
    """
    if df.empty:
        return 0
    return int(((df['open'] == df['high']) & (df['high'] == df['low']) & (df['low'] == df['close'])).sum())


# --------------------------------------------------------------------------- io

def load_existing_csv(path: Path) -> Optional[pd.DataFrame]:
    """Read a *local scratch copy* of an existing data/<SYM>_daily.csv.

    Mirrors ``csv_cache.CSVCache.load``'s parsing exactly (index_col=0,
    parse_dates=True) so schema comparisons are apples-to-apples.
    """
    if not path.exists():
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    return df


def write_output(df: pd.DataFrame, symbol: str, out_dir: Path) -> Path:
    """Write the merged series to `out_dir` (NEVER a live data/ dir), using
    the app's own atomic CSV writer (``csv_cache.CSVCache``) so the on-disk
    schema (blank index header, column order, float formatting) matches
    ``data/<SYM>_daily.csv`` exactly and the write is temp-file+rename.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_df = df[OHLCV_COLUMNS].copy().astype(float)
    out_df.index.name = None  # matches existing session-market CSVs (blank header)
    cache = CSVCache(cache_dir=str(out_dir))
    cache.save(symbol, out_df, timeframe='daily')
    return cache.get_path(symbol, 'daily')


# --------------------------------------------------------------------------- report

@dataclass
class BackfillResult:
    symbol: str
    rows_before: int
    rows_after: int
    old_start: Optional[str]
    new_start: Optional[str]
    overlap_rows: int
    max_abs_discrepancy: float
    max_pct_discrepancy: float
    discrepancy_column: Optional[str]
    source_used: str
    out_path: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    error: Optional[str] = None


def backfill_symbol(symbol: str, existing_dir: Path, out_dir: Path,
                     yahoo: YahooClient, stooq: StooqClient,
                     cross_check_stooq: bool = True) -> BackfillResult:
    old_df = load_existing_csv(existing_dir / f"{symbol}_daily.csv")
    rows_before = 0 if old_df is None else len(old_df)
    old_start = None if old_df is None or old_df.empty else str(old_df.index.min().date())

    notes: list[str] = []

    new_df = fetch_yahoo_deep(yahoo, symbol)
    source_used = 'yahoo'
    if new_df.empty:
        notes.append("Yahoo returned no data for this symbol — falling back to Stooq")
        new_df = fetch_stooq_raw(stooq, symbol)
        source_used = 'stooq'

    if new_df.empty:
        return BackfillResult(
            symbol=symbol, rows_before=rows_before, rows_after=rows_before,
            old_start=old_start, new_start=old_start, overlap_rows=0,
            max_abs_discrepancy=0.0, max_pct_discrepancy=0.0, discrepancy_column=None,
            source_used='none', notes=notes,
            error="neither Yahoo nor Stooq returned data — no file written",
        )

    overlap_rows, max_abs, max_pct, disc_col = compare_overlap(old_df, new_df)

    if cross_check_stooq and source_used == 'yahoo':
        alt_df = fetch_stooq_raw(stooq, symbol)
        if not alt_df.empty:
            x_overlap, x_abs, x_pct, x_col = compare_overlap(new_df, alt_df)
            if x_overlap > 0:
                notes.append(
                    f"cross-check yahoo vs stooq: {x_overlap} overlapping rows, "
                    f"max abs diff {x_abs:.4f} on '{x_col}' ({x_pct:.1%})"
                )
            else:
                notes.append("cross-check: stooq returned data but no overlapping dates with yahoo")
        else:
            notes.append("cross-check: stooq returned no data for this symbol")

    merged = merge_history(old_df, new_df)

    older_block = merged if old_df is None else merged[merged.index < (old_df.index.min() if not old_df.empty else merged.index.max())]
    flats = flat_candle_count(older_block)
    if flats:
        notes.append(f"{flats} flat (O=H=L=C) candle(s) in the newly-spliced older rows")

    try:
        findings = validate_series(merged, symbol)
    except ValidationError as exc:
        return BackfillResult(
            symbol=symbol, rows_before=rows_before, rows_after=rows_before,
            old_start=old_start, new_start=old_start, overlap_rows=overlap_rows,
            max_abs_discrepancy=max_abs, max_pct_discrepancy=max_pct,
            discrepancy_column=disc_col, source_used=source_used, notes=notes,
            error=f"VALIDATION FAILED: {exc}",
        )

    notes.extend(f"gap-finding: {f}" for f in findings)

    out_path = write_output(merged, symbol, out_dir)

    return BackfillResult(
        symbol=symbol, rows_before=rows_before, rows_after=len(merged),
        old_start=old_start, new_start=str(merged.index.min().date()),
        overlap_rows=overlap_rows, max_abs_discrepancy=max_abs,
        max_pct_discrepancy=max_pct, discrepancy_column=disc_col,
        source_used=source_used, out_path=str(out_path), notes=notes,
    )


def print_report(results: list[BackfillResult]) -> None:
    header = f"{'symbol':<8} {'before':>7} {'after':>7} {'new_start':>11} {'overlap':>8} {'max_abs_diff':>13} {'source':>7}"
    print(header)
    print('-' * len(header))
    for r in results:
        if r.error:
            print(f"{r.symbol:<8} ERROR: {r.error}")
            continue
        print(f"{r.symbol:<8} {r.rows_before:>7} {r.rows_after:>7} "
              f"{r.new_start or '-':>11} {r.overlap_rows:>8} "
              f"{r.max_abs_discrepancy:>13.4f} {r.source_used:>7}")
    print()
    for r in results:
        if r.notes:
            print(f"[{r.symbol}] notes:")
            for n in r.notes:
                print(f"  - {n}")


# --------------------------------------------------------------------------- cli

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--existing-dir', required=True, type=Path,
                         help="Local scratch dir holding pulled copies of data/<SYM>_daily.csv")
    parser.add_argument('--out-dir', required=True, type=Path,
                         help="Scratch output dir for merged CSVs — NEVER a live data/ dir")
    parser.add_argument('--symbols', default=','.join(SESSION_SYMBOLS),
                         help=f"Comma-separated symbols (default: {','.join(SESSION_SYMBOLS)})")
    parser.add_argument('--no-cross-check', action='store_true',
                         help="Skip the Yahoo-vs-Stooq cross-source sanity check")
    args = parser.parse_args(argv)

    out_dir = args.out_dir.resolve()
    # Refuse anything that looks like it could be a live app data dir.
    forbidden_names = {'data'}
    if out_dir.name in forbidden_names or str(out_dir).rstrip('/') == str(Path(_PROJECT_ROOT) / 'data'):
        print(f"REFUSING to write to {out_dir} — looks like a live data/ dir.", file=sys.stderr)
        return 2

    symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    yahoo, stooq = make_clients()

    results = []
    for i, symbol in enumerate(symbols):
        if i:
            time.sleep(INTER_SYMBOL_SLEEP_SECS)
        try:
            result = backfill_symbol(symbol, args.existing_dir, out_dir, yahoo, stooq,
                                      cross_check_stooq=not args.no_cross_check)
        except Exception as exc:  # never let one symbol's crash kill the run
            logger.exception("Unhandled error backfilling %s", symbol)
            result = BackfillResult(
                symbol=symbol, rows_before=0, rows_after=0, old_start=None,
                new_start=None, overlap_rows=0, max_abs_discrepancy=0.0,
                max_pct_discrepancy=0.0, discrepancy_column=None,
                source_used='none', error=str(exc),
            )
        results.append(result)

    print_report(results)
    return 1 if any(r.error for r in results) else 0


if __name__ == '__main__':
    sys.exit(main())
