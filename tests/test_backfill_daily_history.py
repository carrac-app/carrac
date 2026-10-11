"""Unit tests for scripts/backfill_daily_history.py.

Pure synthetic data only — no network access. Covers the merge/validation
core that the script's risk notes call out explicitly: gap preservation,
duplicate rejection, strictly-increasing ordering, and atomic writes.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from csv_cache import CSVCache
from scripts.backfill_daily_history import (
    OHLCV_COLUMNS,
    ValidationError,
    compare_overlap,
    flat_candle_count,
    load_existing_csv,
    merge_history,
    validate_series,
    write_output,
)


def _frame(dates, closes=None, opens=None, highs=None, lows=None, volumes=None):
    """Build a minimal OHLCV frame with a DatetimeIndex, no name (matches
    the existing session-market CSVs' blank index header)."""
    idx = pd.to_datetime(list(dates))
    n = len(idx)
    closes = closes if closes is not None else [100.0 + i for i in range(n)]
    opens = opens if opens is not None else closes
    highs = highs if highs is not None else [c + 1 for c in closes]
    lows = lows if lows is not None else [c - 1 for c in closes]
    volumes = volumes if volumes is not None else [1000.0] * n
    df = pd.DataFrame(
        {'open': opens, 'high': highs, 'low': lows, 'close': closes, 'volume': volumes},
        index=idx,
    )
    df.index.name = None
    return df


# --------------------------------------------------------------------- merge

def test_merge_splices_only_strictly_older_rows():
    old = _frame(['2024-05-22', '2024-05-23', '2024-05-24'], closes=[10, 11, 12])
    new = _frame(
        ['2024-05-20', '2024-05-21', '2024-05-22', '2024-05-23', '2024-05-24'],
        closes=[7, 8, 999, 999, 999],  # overlap values deliberately wrong
    )

    merged = merge_history(old, new)

    # Old rows for dates old already had must win — new's bogus 999s are
    # not allowed to clobber the already-cached values.
    assert merged.loc['2024-05-22', 'close'] == 10
    assert merged.loc['2024-05-23', 'close'] == 11
    assert merged.loc['2024-05-24', 'close'] == 12
    # Genuinely older dates got spliced in.
    assert merged.loc['2024-05-20', 'close'] == 7
    assert merged.loc['2024-05-21', 'close'] == 8
    assert len(merged) == 5


def test_merge_preserves_weekend_gap_as_data():
    # Friday -> Monday: a real 3-day gap, must not be filled.
    old = _frame(['2024-05-24', '2024-05-27'], closes=[10, 11])  # Fri, Mon
    new = _frame(['2024-05-20', '2024-05-21'], closes=[8, 9])   # older Mon/Tue

    merged = merge_history(old, new)

    dates = [d.strftime('%Y-%m-%d') for d in merged.index]
    assert dates == ['2024-05-20', '2024-05-21', '2024-05-24', '2024-05-27']
    # No synthetic row for 2024-05-25/26 (the weekend) was inserted.
    assert '2024-05-25' not in dates
    assert '2024-05-26' not in dates


def test_merge_with_no_existing_cache_returns_sorted_new():
    new = _frame(['2024-05-22', '2024-05-20', '2024-05-21'], closes=[3, 1, 2])
    merged = merge_history(None, new)
    assert list(merged.index) == sorted(merged.index)
    assert merged.iloc[0]['close'] == 1


def test_merge_is_ordered_ascending():
    old = _frame(['2024-01-10', '2024-01-11'])
    new = _frame(['2024-01-01', '2024-01-05', '2024-01-08'])
    merged = merge_history(old, new)
    assert merged.index.is_monotonic_increasing
    assert not merged.index.has_duplicates


# ------------------------------------------------------------------ validate

def test_validate_rejects_duplicate_dates():
    df = pd.concat([_frame(['2024-01-01', '2024-01-02']), _frame(['2024-01-02'])])
    with pytest.raises(ValidationError, match="duplicate"):
        validate_series(df, 'TEST')


def test_validate_rejects_non_monotonic_order():
    df = _frame(['2024-01-02', '2024-01-01', '2024-01-03'])
    # Force out-of-order without triggering the dedup/sort merge_history would do.
    df = df.iloc[[0, 1, 2]]
    with pytest.raises(ValidationError, match="strictly increasing"):
        validate_series(df, 'TEST')


def test_validate_rejects_nan_values():
    df = _frame(['2024-01-01', '2024-01-02'])
    df.loc[df.index[0], 'close'] = float('nan')
    with pytest.raises(ValidationError, match="NaN"):
        validate_series(df, 'TEST')


def test_validate_rejects_empty_frame():
    df = _frame([])
    with pytest.raises(ValidationError, match="empty"):
        validate_series(df, 'TEST')


def test_validate_flags_wide_gap_without_raising():
    # A 60-day hole is reported, not fabricated over, and does not raise.
    df = _frame(['2024-01-01', '2024-03-01'])
    findings = validate_series(df, 'TEST', max_normal_gap_days=10)
    assert len(findings) == 1
    assert '2024-01-01' in findings[0] and '2024-03-01' in findings[0]


def test_validate_does_not_flag_normal_weekend_gap():
    df = _frame(['2024-05-24', '2024-05-27'])  # Fri -> Mon, 3 days
    findings = validate_series(df, 'TEST', max_normal_gap_days=10)
    assert findings == []


# ------------------------------------------------------------------ compare

def test_compare_overlap_reports_max_discrepancy():
    # Restrict to 'close' to isolate the formula from the O/H/L default
    # offsets _frame() applies (which would otherwise also contribute).
    old = _frame(['2024-01-01', '2024-01-02', '2024-01-03'], closes=[100.0, 100.0, 100.0])
    new = _frame(['2024-01-01', '2024-01-02', '2024-01-03'], closes=[100.0, 105.0, 100.0])
    overlap_rows, max_abs, max_pct, col = compare_overlap(old, new, columns=('close',))
    assert overlap_rows == 3
    assert max_abs == pytest.approx(5.0)
    assert max_pct == pytest.approx(0.05)
    assert col == 'close'


def test_compare_overlap_no_intersection_is_zero():
    old = _frame(['2024-01-01'])
    new = _frame(['2024-02-01'])
    overlap_rows, max_abs, max_pct, col = compare_overlap(old, new)
    assert (overlap_rows, max_abs, max_pct, col) == (0, 0.0, 0.0, None)


def test_compare_overlap_handles_none_old():
    new = _frame(['2024-01-01'])
    assert compare_overlap(None, new) == (0, 0.0, 0.0, None)


# -------------------------------------------------------------- flat candle

def test_flat_candle_count():
    df = _frame(['2024-01-01', '2024-01-02'], opens=[10, 10], highs=[10, 12],
                lows=[10, 9], closes=[10, 11])
    assert flat_candle_count(df) == 1


# ----------------------------------------------------------------- io/write

def test_write_output_matches_existing_schema(tmp_path):
    df = _frame(['2024-01-01', '2024-01-02'], opens=[1.5, 2.5], highs=[2.0, 3.0],
                lows=[1.0, 2.0], closes=[1.8, 2.8], volumes=[10, 20])
    out_path = write_output(df, 'TEST', tmp_path)

    text = out_path.read_text()
    lines = text.splitlines()
    assert lines[0] == ',open,high,low,close,volume'  # blank index header
    assert lines[1] == '2024-01-01,1.5,2.0,1.0,1.8,10.0'  # volume forced float, matches production files
    assert out_path.name == 'TEST_daily.csv'


def test_write_output_column_order(tmp_path):
    df = _frame(['2024-01-01'])
    df = df[['volume', 'close', 'open', 'high', 'low']]  # scrambled on purpose
    out_path = write_output(df, 'TEST', tmp_path)
    header = out_path.read_text().splitlines()[0]
    assert header == ',' + ','.join(OHLCV_COLUMNS)


def test_load_existing_csv_round_trips(tmp_path):
    df = _frame(['2024-01-01', '2024-01-02'])
    src = tmp_path / 'TEST_daily.csv'
    df.to_csv(src)
    loaded = load_existing_csv(src)
    assert isinstance(loaded.index, pd.DatetimeIndex)
    assert len(loaded) == 2


def test_load_existing_csv_missing_file_returns_none(tmp_path):
    assert load_existing_csv(tmp_path / 'NOPE_daily.csv') is None


# -------------------------------------------------------------- atomicity

def test_write_is_atomic_failed_second_write_preserves_original(tmp_path, monkeypatch):
    """A failure mid-write must never leave a half-written or missing file.

    Reuses csv_cache.CSVCache's temp-file+os.replace discipline (the exact
    mechanism the script's risk notes describe — a torn read of a
    half-written CSV: ``_atomic_write_csv`` unlinks its temp file and
    re-raises on failure, and ``CSVCache.save`` swallows that so one bad
    write never crashes the caller). We write a good file, then force the
    *next* write to blow up inside ``to_csv`` and assert the original
    survives untouched, with no leftover ``.tmp`` file.
    """
    good = _frame(['2024-01-01', '2024-01-02'], closes=[1.0, 2.0])
    out_path = write_output(good, 'TEST', tmp_path)
    original_bytes = out_path.read_bytes()

    bad = _frame(['2024-01-01', '2024-01-02', '2024-01-03'], closes=[9.0, 9.0, 9.0])

    def _boom(self, *a, **kw):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(pd.DataFrame, 'to_csv', _boom)
    write_output(bad, 'TEST', tmp_path)  # CSVCache.save() swallows the failure

    monkeypatch.undo()
    assert out_path.read_bytes() == original_bytes
    leftover_tmp = [f for f in os.listdir(tmp_path) if f.startswith('.') and f.endswith('.tmp')]
    assert leftover_tmp == []


def test_write_output_never_shrinks_silently(tmp_path):
    big = _frame([f'2024-01-{d:02d}' for d in range(1, 26)])  # 25 rows
    write_output(big, 'TEST', tmp_path)

    small = _frame(['2024-01-01'])  # 1 row — would be a >50% shrink
    out_path = write_output(small, 'TEST', tmp_path)

    # CSVCache.save() refuses the shrinking write and keeps the big file.
    on_disk = pd.read_csv(out_path, index_col=0, parse_dates=True)
    assert len(on_disk) == 25


def test_csv_cache_reused_directly_matches_write_output(tmp_path):
    """Sanity check that write_output really is CSVCache under the hood
    (same file layout), not a parallel hand-rolled writer."""
    df = _frame(['2024-01-01'])
    write_output(df, 'TEST', tmp_path)
    cache = CSVCache(cache_dir=str(tmp_path))
    loaded = cache.load('TEST')
    assert loaded is not None
    assert len(loaded) == 1
