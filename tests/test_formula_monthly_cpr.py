"""Tests for monthly CPR DSL functions and 1M OHLCV resampling.

All tests use self-contained synthetic OHLCV data with explicit DatetimeIndex
so results are fully deterministic without external data dependencies.
"""

import numpy as np
import pandas as pd

from carrac.formula_eval import (
    FUNCTION_REGISTRY,
    Evaluator,
    Parser,
    _monthly_cpr_levels,
    _monthly_cpr_p,
    _monthly_cpr_bc,
    _monthly_cpr_tc,
    _monthly_cpr_r1,
    _monthly_cpr_r2,
    _monthly_cpr_r3,
    _monthly_cpr_s1,
    _monthly_cpr_s2,
    _monthly_cpr_s3,
    tokenize,
)


# ── Synthetic daily data spanning 3 complete months + 1 partial ─────────
# Month A (Jan 2024): 22 trading days, H=105 L=95 C_last=102
# Month B (Feb 2024): 20 trading days, H=115 L=108 C_last=112
# Month C (Mar 2024): 21 trading days, H=120 L=110 C_last=118
# Month D (Apr 2024): 3 partial days (current incomplete month)

def _make_daily_index(start, end):
    """Business-day DatetimeIndex (tz-naive UTC, matching CPRCalculator expectation)."""
    return pd.bdate_range(start=start, end=end, freq='B')


JAN_IDX = _make_daily_index('2024-01-02', '2024-01-31')
FEB_IDX = _make_daily_index('2024-02-01', '2024-02-29')
MAR_IDX = _make_daily_index('2024-03-01', '2024-03-29')
APR_IDX = _make_daily_index('2024-04-01', '2024-04-03')

ALL_IDX = JAN_IDX.append(FEB_IDX).append(MAR_IDX).append(APR_IDX)
N = len(ALL_IDX)

# Build H/L/C arrays — constant within each month for easy expected-value math
H_VAL = {0: 105.0, 1: 115.0, 2: 120.0, 3: 125.0}
L_VAL = {0: 95.0,  1: 108.0, 2: 110.0, 3: 121.0}
C_LAST = {0: 102.0, 1: 112.0, 2: 118.0, 3: 123.0}


def _make_arrays():
    h, l, c = [], [], []
    mon_ranges = [
        (JAN_IDX, 0),
        (FEB_IDX, 1),
        (MAR_IDX, 2),
        (APR_IDX, 3),
    ]
    for idx, m in mon_ranges:
        nb = len(idx)
        h.extend([H_VAL[m]] * nb)
        l.extend([L_VAL[m]] * nb)
        # Linearly ramp close to end at C_LAST[m]
        closes = np.linspace(H_VAL[m] - 5, C_LAST[m], nb)
        c.extend(closes.tolist())

    return (
        np.array(h, dtype=np.float64),
        np.array(l, dtype=np.float64),
        np.array(c, dtype=np.float64),
        ALL_IDX.values.astype('datetime64[ns]'),
    )


H, L, C, DATES = _make_arrays()

# Pre-compute expected monthly CPR values (vs prev month):
# For bars in Feb (prev month = Jan):  H_prev=105, L_prev=95, C_prev_last=C_LAST[0]=102
# pivot = (105+95+102)/3 = 100.6667,  bc = (105+95)/2 = 100, tc = 2*100.6667-100 = 101.3333
_JAN_H, _JAN_L, _JAN_C = H_VAL[0], L_VAL[0], C_LAST[0]
_PIVOT_FOR_FEB = (_JAN_H + _JAN_L + _JAN_C) / 3
_BC_FOR_FEB    = (_JAN_H + _JAN_L) / 2
_TC_FOR_FEB    = 2 * _PIVOT_FOR_FEB - _BC_FOR_FEB

# Index of first Feb bar in ALL_IDX
JAN_LEN = len(JAN_IDX)
FEB_LEN = len(FEB_IDX)
MAR_LEN = len(MAR_IDX)
_FIRST_FEB = JAN_LEN             # first February bar index
_LAST_FEB  = JAN_LEN + FEB_LEN - 1
_FIRST_MAR = JAN_LEN + FEB_LEN
_LAST_MAR  = JAN_LEN + FEB_LEN + MAR_LEN - 1
_FIRST_APR = JAN_LEN + FEB_LEN + MAR_LEN


# ── Helper: build evaluator context ─────────────────────────────────────

def _make_context(h=H, l=L, c=C, dates=DATES):
    return {
        'open': h.copy(),  # use high as open proxy — not material for CPR
        'high': h,
        'low': l,
        'close': c,
        'volume': np.ones(len(c), dtype=np.float64),
        '_dates': dates,
    }


def _eval_expr(expr, ctx=None):
    if ctx is None:
        ctx = _make_context()
    n = len(ctx['close'])
    ast = Parser(tokenize(expr)).parse()
    return Evaluator(ctx, n, symbol='TEST', timeframe='1D').eval(ast)


# ── 1M resampling ────────────────────────────────────────────────────

def test_1m_resampling_registered():
    """'1M' must resample daily bars to MONTH-START buckets.

    Asserts the contract (month-start anchoring) rather than grepping
    `inspect.getsource(_load_ohlcv_cached)` for the literals '1M' and 'MS',
    which would pin the shape of the implementation, not its behaviour.
    The monthly CPR functions below depend on month-start anchoring.
    """
    from carrac.formula_eval import _TF_SOURCE

    csv_tf, rule = _TF_SOURCE['1M']
    assert csv_tf == 'daily'
    assert rule == 'MS', (
        f"1M must resample to month-START ('MS'), got {rule!r} — "
        "'M'/'ME' would anchor to month-end and shift every monthly CPR level"
    )


# ── Monthly CPR function array contracts ──────────────────────────────

def test_monthly_cpr_p_non_nan_for_feb_bars():
    """Pivot must be non-NaN for all February bars (prev month = Jan is complete)."""
    out = _monthly_cpr_p(H, L, C, DATES)
    assert not np.any(np.isnan(out[_FIRST_FEB:_LAST_FEB + 1])), \
        "monthly_cpr_p returned NaN in February bars"


def test_monthly_cpr_p_nan_for_jan_bars():
    """January bars have no previous month — must all be NaN."""
    out = _monthly_cpr_p(H, L, C, DATES)
    assert np.all(np.isnan(out[:JAN_LEN])), \
        "monthly_cpr_p should be NaN for the first calendar month"


def test_monthly_cpr_p_stepwise_constant_within_feb():
    """All Feb bars must share the same pivot value (step function)."""
    out = _monthly_cpr_p(H, L, C, DATES)
    feb_slice = out[_FIRST_FEB:_LAST_FEB + 1]
    assert np.all(feb_slice == feb_slice[0]), \
        "monthly_cpr_p is not constant within a single calendar month"


def test_monthly_cpr_p_changes_at_month_boundary():
    """Pivot must differ between Feb and Mar (they use Jan vs Feb prev month data)."""
    out = _monthly_cpr_p(H, L, C, DATES)
    pivot_feb = out[_FIRST_FEB]
    pivot_mar = out[_FIRST_MAR]
    assert not np.isnan(pivot_feb) and not np.isnan(pivot_mar), \
        "Expected non-NaN pivots in Feb and Mar"
    assert pivot_feb != pivot_mar, \
        "Pivot should change at the month boundary"


def test_monthly_cpr_p_matches_expected_formula():
    """Pivot = (prev_H + prev_L + prev_C) / 3 — verify against known Jan values."""
    out = _monthly_cpr_p(H, L, C, DATES)
    expected = _PIVOT_FOR_FEB
    actual = out[_FIRST_FEB]
    assert abs(actual - expected) < 1e-9, \
        f"Pivot mismatch: expected {expected:.6f}, got {actual:.6f}"


def test_monthly_cpr_bc_matches_expected_formula():
    """BC = (prev_H + prev_L) / 2."""
    out = _monthly_cpr_bc(H, L, C, DATES)
    expected = _BC_FOR_FEB
    actual = out[_FIRST_FEB]
    assert abs(actual - expected) < 1e-9, \
        f"BC mismatch: expected {expected:.6f}, got {actual:.6f}"


def test_monthly_cpr_tc_matches_expected_formula():
    """TC = 2 * pivot - BC."""
    out = _monthly_cpr_tc(H, L, C, DATES)
    expected = _TC_FOR_FEB
    actual = out[_FIRST_FEB]
    assert abs(actual - expected) < 1e-9, \
        f"TC mismatch: expected {expected:.6f}, got {actual:.6f}"


def test_monthly_cpr_r1_formula():
    """R1 = 2 * pivot - prev_L."""
    out = _monthly_cpr_r1(H, L, C, DATES)
    expected = 2 * _PIVOT_FOR_FEB - _JAN_L
    actual = out[_FIRST_FEB]
    assert abs(actual - expected) < 1e-9, f"R1 mismatch: {actual} vs {expected}"


def test_monthly_cpr_r2_formula():
    """R2 = pivot + (prev_H - prev_L)."""
    out = _monthly_cpr_r2(H, L, C, DATES)
    expected = _PIVOT_FOR_FEB + (_JAN_H - _JAN_L)
    actual = out[_FIRST_FEB]
    assert abs(actual - expected) < 1e-9, f"R2 mismatch: {actual} vs {expected}"


def test_monthly_cpr_r3_formula():
    """R3 = pivot + 2 * (prev_H - prev_L)."""
    out = _monthly_cpr_r3(H, L, C, DATES)
    expected = _PIVOT_FOR_FEB + 2 * (_JAN_H - _JAN_L)
    actual = out[_FIRST_FEB]
    assert abs(actual - expected) < 1e-9, f"R3 mismatch: {actual} vs {expected}"


def test_monthly_cpr_s1_formula():
    """S1 = 2 * pivot - prev_H."""
    out = _monthly_cpr_s1(H, L, C, DATES)
    expected = 2 * _PIVOT_FOR_FEB - _JAN_H
    actual = out[_FIRST_FEB]
    assert abs(actual - expected) < 1e-9, f"S1 mismatch: {actual} vs {expected}"


def test_monthly_cpr_s2_formula():
    """S2 = pivot - (prev_H - prev_L)."""
    out = _monthly_cpr_s2(H, L, C, DATES)
    expected = _PIVOT_FOR_FEB - (_JAN_H - _JAN_L)
    actual = out[_FIRST_FEB]
    assert abs(actual - expected) < 1e-9, f"S2 mismatch: {actual} vs {expected}"


def test_monthly_cpr_s3_formula():
    """S3 = pivot - 2 * (prev_H - prev_L)."""
    out = _monthly_cpr_s3(H, L, C, DATES)
    expected = _PIVOT_FOR_FEB - 2 * (_JAN_H - _JAN_L)
    actual = out[_FIRST_FEB]
    assert abs(actual - expected) < 1e-9, f"S3 mismatch: {actual} vs {expected}"


def test_all_9_levels_non_nan_in_feb():
    """All 9 monthly CPR levels must be non-NaN for February bars."""
    fns = [
        _monthly_cpr_p, _monthly_cpr_tc, _monthly_cpr_bc,
        _monthly_cpr_r1, _monthly_cpr_r2, _monthly_cpr_r3,
        _monthly_cpr_s1, _monthly_cpr_s2, _monthly_cpr_s3,
    ]
    for fn in fns:
        out = fn(H, L, C, DATES)
        assert not np.any(np.isnan(out[_FIRST_FEB:_LAST_FEB + 1])), \
            f"{fn.__name__} returned NaN in February bars"


# ── Registry + alias ─────────────────────────────────────────────────────

def test_all_9_monthly_cpr_registered():
    """All 9 monthly CPR functions plus alias must be in FUNCTION_REGISTRY."""
    expected = {
        'monthly_cpr_p', 'monthly_pivot',
        'monthly_cpr_tc', 'monthly_cpr_bc',
        'monthly_cpr_r1', 'monthly_cpr_r2', 'monthly_cpr_r3',
        'monthly_cpr_s1', 'monthly_cpr_s2', 'monthly_cpr_s3',
    }
    missing = expected - set(FUNCTION_REGISTRY.keys())
    assert not missing, f"Missing from FUNCTION_REGISTRY: {missing}"


def test_monthly_pivot_alias_same_as_monthly_cpr_p():
    """monthly_pivot alias must resolve to the same function as monthly_cpr_p."""
    fn_p, _, _, _ = FUNCTION_REGISTRY['monthly_cpr_p']
    fn_alias, _, _, _ = FUNCTION_REGISTRY['monthly_pivot']
    assert fn_p is fn_alias, "monthly_pivot alias does not point to _monthly_cpr_p"


# ── DSL evaluator integration ─────────────────────────────────────────────

def test_eval_monthly_cpr_p_via_dsl():
    """monthly_cpr_p() should resolve to a non-NaN value for Mar+ bars via DSL."""
    typ, arr = _eval_expr('monthly_cpr_p()')
    assert typ == 'a'
    # First bar (Jan) = NaN, later bars should be non-NaN
    assert not np.all(np.isnan(arr)), "All values are NaN — function not wired"
    assert not np.isnan(arr[_FIRST_MAR]), \
        f"March bar {_FIRST_MAR} should be non-NaN"


def test_eval_monthly_pivot_alias_via_dsl():
    """monthly_pivot() alias must return the same array as monthly_cpr_p()."""
    _, arr_p = _eval_expr('monthly_cpr_p()')
    _, arr_alias = _eval_expr('monthly_pivot()')
    np.testing.assert_array_equal(arr_p, arr_alias)


def test_eval_monthly_cpr_p_stepwise_via_dsl():
    """DSL: same value for two consecutive February bars."""
    _, arr = _eval_expr('monthly_cpr_p()')
    i = _FIRST_FEB + 1  # second Feb bar
    assert arr[i - 1] == arr[i], \
        f"monthly_cpr_p not constant within month: {arr[i-1]} vs {arr[i]}"


def test_eval_monthly_cpr_tc_via_dsl():
    """monthly_cpr_tc() returns non-NaN values for Feb bars."""
    _, arr = _eval_expr('monthly_cpr_tc()')
    assert not np.any(np.isnan(arr[_FIRST_FEB:_LAST_FEB + 1]))


def test_eval_monthly_cpr_bc_via_dsl():
    """monthly_cpr_bc() returns non-NaN values for Feb bars."""
    _, arr = _eval_expr('monthly_cpr_bc()')
    assert not np.any(np.isnan(arr[_FIRST_FEB:_LAST_FEB + 1]))


def test_eval_all_resistance_support_via_dsl():
    """All six R/S monthly CPR lines return non-NaN for Feb bars."""
    for name in ('monthly_cpr_r1', 'monthly_cpr_r2', 'monthly_cpr_r3',
                 'monthly_cpr_s1', 'monthly_cpr_s2', 'monthly_cpr_s3'):
        _, arr = _eval_expr(f'{name}()')
        feb_vals = arr[_FIRST_FEB:_LAST_FEB + 1]
        assert not np.any(np.isnan(feb_vals)), \
            f"{name}() returned NaN in February bars"


# ── HTF 1M resampling ────────────────────────────────────────────────────

class _FakeDailyCache:
    """Stands in for CSVCache: serves one synthetic daily frame for any symbol."""

    def __init__(self, df):
        self._df = df

    def load(self, symbol, timeframe):
        return self._df if timeframe == 'daily' else None


def test_htf_1m_resampling_produces_month_start_bars(monkeypatch):
    """End-to-end: loading '1M' returns fewer bars than '1D', all on day 1.

    Runs the real load_ohlcv round trip against a synthetic daily frame, so it
    fails if resampling is dropped, mis-anchored to month-end, or if the
    timeframe silently resolves to daily.
    """
    from carrac import formula_eval
    from carrac.formula_eval import load_ohlcv

    idx = pd.date_range('2023-01-01', '2024-06-30', freq='D')
    close = np.linspace(100.0, 200.0, len(idx))
    daily_df = pd.DataFrame(
        {'open': close - 1, 'high': close + 2, 'low': close - 2,
         'close': close, 'volume': np.full(len(idx), 10.0)},
        index=idx,
    )
    monkeypatch.setattr(formula_eval, '_get_csv_cache', lambda: _FakeDailyCache(daily_df))
    formula_eval._load_ohlcv_cached.cache_clear()
    try:
        monthly = load_ohlcv('BTC', '1M', requested_bars=2000)
        daily = load_ohlcv('BTC', '1D', requested_bars=2000)
    finally:
        formula_eval._load_ohlcv_cached.cache_clear()

    assert monthly is not None and daily is not None
    assert len(monthly['close']) == 18  # Jan 2023 .. Jun 2024
    assert len(monthly['close']) < len(daily['close']), \
        "1M returned as many bars as 1D — resampling did not happen"

    days = pd.to_datetime(monthly['_dates']).day
    assert (days == 1).all(), \
        "monthly bars are not anchored to month-start; monthly CPR levels would shift"


# ── Performance smoke test ────────────────────────────────────────────────

def test_monthly_cpr_levels_under_10ms_for_1000_bars():
    """Vectorized monthly CPR for 1000 bars must complete < 10ms."""
    import time

    rng = np.random.default_rng(42)
    n = 1000
    # Create 1000 daily bars spanning ~4 years
    idx = pd.bdate_range('2020-01-02', periods=n, freq='B')
    h_big = rng.uniform(100, 200, n).astype(np.float64)
    l_big = h_big - rng.uniform(1, 10, n).astype(np.float64)
    c_big = l_big + rng.uniform(0, (h_big - l_big))
    dates_big = idx.values.astype('datetime64[ns]')

    # Thread CPU time, best of 5: wall-clock inflates when the machine is busy
    # (parallel test workers), which says nothing about the vectorised code.
    runs = []
    for _ in range(5):
        t0 = time.thread_time()
        _monthly_cpr_levels(h_big, l_big, c_big, dates_big)
        runs.append((time.thread_time() - t0) * 1000)
    elapsed_ms = min(runs)

    assert elapsed_ms < 10, \
        f"monthly_cpr_levels took {elapsed_ms:.1f}ms for 1000 bars (budget: 10ms)"
