"""Tests for formula engine additions: HTF, DIVERGENCE, derivatives vars."""

import json
from pathlib import Path

import numpy as np
import pytest

from carrac.formula_eval import (
    DERIV_VARS, ENGINE_VERSION, Evaluator, Parser,
    get_deriv_deps, tokenize,
)


def _load_fixture(name):
    fixtures = json.loads(
        (Path(__file__).parent / 'golden' / 'formula' / 'fixtures.json').read_text()
    )
    for f in fixtures['ohlcv_fixtures']:
        if f['name'] == name:
            return f
    raise KeyError(name)


def _eval(expr, ohlcv, symbol='BTC', timeframe='1D', extra_context=None):
    ctx = {k: np.asarray(v, dtype=np.float64) for k, v in ohlcv.items()
           if k in ('open', 'high', 'low', 'close', 'volume')}
    if extra_context:
        ctx.update(extra_context)
    n = len(ctx['close'])
    ast = Parser(tokenize(expr)).parse()
    return Evaluator(ctx, n, symbol=symbol, timeframe=timeframe).eval(ast)


# ── Engine version ─────────────────────────────────────────────────────

def test_engine_version_is_int():
    assert isinstance(ENGINE_VERSION, int)
    assert ENGINE_VERSION >= 3


# ── Derivatives variable detection ─────────────────────────────────────

def test_deriv_deps_detects_all_vars(admin_caller):
    # These three are Deribit-derived and admin-only. The public
    # shape is asserted separately below.
    expr = 'skew_25d < -5 AND basis_apr > 10 AND iv_rv_premium > 0'
    deps = get_deriv_deps(expr)
    names = {d[0] for d in deps}
    assert 'skew_25d' in names
    assert 'basis_apr' in names
    assert 'iv_rv_premium' in names


def test_deriv_deps_hides_deribit_vars_from_a_normal_caller():
    """The public formula surface must not resolve these at all.

    Not merely "returns no data" — unknown, so a user gets an error instead of
    a formula that silently evaluates against an empty series.
    """
    deps = get_deriv_deps('skew_25d < -5 AND basis_apr > 10 AND iv_rv_premium > 0')
    assert deps == []


def test_deriv_deps_still_sees_permitted_vars_publicly():
    """funding is Binance-derived (licence-permitted) — must stay public."""
    deps = get_deriv_deps('funding > 0')
    assert {d[0] for d in deps} == {'funding'}


def test_funding_oi_ratio_is_gone_not_merely_dataless():
    """The var mapped to a column nothing ever wrote, so every
    formula using it errored claiming a per-asset coverage gap. Removed the
    way the earlier cleanup removed its four. Unknown identifier — not an empty
    series, not a misleading coverage error."""
    assert 'funding_oi_ratio' not in DERIV_VARS
    assert get_deriv_deps('funding_oi_ratio > 0') == []


def test_deriv_deps_skips_function_like_occurrences():
    # Even a made-up call should not trigger dep detection
    deps = get_deriv_deps('skew_25d(14) + 5')  # 'skew_25d(' used as call
    assert deps == []


def test_quant_and_deriv_deps_dont_overlap():
    # No var appears in both dicts — they must be disjoint
    from carrac.formula_eval import DERIV_VARS, QUANT_VARS
    intersect = set(DERIV_VARS.keys()) & set(QUANT_VARS.keys())
    assert not intersect, f"DERIV and QUANT share: {intersect}"


def test_all_deriv_vars_exported():
    # Spot-check the requested set
    expected = {'basis_apr', 'skew_25d', 'iv_7d', 'iv_30d', 'iv_90d',
                'term_slope', 'gex_net', 'gex_flip_price', 'iv_rv_premium'}
    assert expected.issubset(set(DERIV_VARS.keys()))


def test_deriv_var_resolves_when_context_injected():
    """When caller pre-injects the value, formula resolves cleanly."""
    ohlcv = _load_fixture('trend_up')
    n = len(ohlcv['close'])
    fake_skew = np.full(n, -3.5)
    result = _eval('skew_25d < -5', ohlcv, extra_context={'skew_25d': fake_skew})
    # -3.5 < -5 is False → 0
    assert result[0] == 'a'
    assert result[1][-1] == 0.0


# ── HTF ────────────────────────────────────────────────────────────────

def test_htf_rejects_non_string_first_arg():
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError, match='timeframe string literal'):
        _eval("htf(7, close)", ohlcv, timeframe='1D')


def test_htf_rejects_unknown_timeframe():
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError, match='unknown timeframe'):
        _eval("htf('bogus', close)", ohlcv, timeframe='1D')


def test_htf_rejects_lower_or_equal_timeframe():
    ohlcv = _load_fixture('trend_up')
    # 1D caller, 1D target — equal, should fail
    with pytest.raises(ValueError, match='must be higher'):
        _eval("htf('1D', close)", ohlcv, timeframe='1D')
    # 1D caller, 1h target — lower, should fail
    with pytest.raises(ValueError, match='must be higher'):
        _eval("htf('1h', close)", ohlcv, timeframe='1D')


def test_htf_rejects_nested_htf():
    ohlcv = _load_fixture('trend_up')
    # htf_depth guard triggers even before OHLCV load
    with pytest.raises(ValueError, match='cannot be nested'):
        _eval("htf('1W', htf('1M', close))", ohlcv, timeframe='1D')


def test_htf_returns_nan_when_data_absent():
    """Without a real CSV cache, HTF should return a NaN array, not raise."""
    ohlcv = _load_fixture('trend_up')
    # _FIXTURE_ symbol won't be in CSV cache; load_ohlcv returns None → NaN array
    result = _eval("htf('1W', close)", ohlcv, symbol='_UNKNOWN_', timeframe='1D')
    assert result[0] == 'a'
    assert np.all(np.isnan(result[1]))


# ── DIVERGENCE ─────────────────────────────────────────────────────────

def test_divergence_argcount_validation():
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError, match='DIVERGENCE expects'):
        _eval('divergence(close)', ohlcv)
    with pytest.raises(ValueError, match='DIVERGENCE expects'):
        _eval('divergence(close, rsi(14), 60, 70)', ohlcv)


def test_divergence_rejects_small_lookback():
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError, match='lookback must be >= 20'):
        _eval('divergence(close, rsi(14), 10)', ohlcv)


def test_divergence_returns_zero_on_trendless_data():
    """A monotonic trend has no price HH-with-osc-LH pairs → no bearish divergence."""
    ohlcv = _load_fixture('trend_up')
    result = _eval('divergence(close, rsi(14), 40)', ohlcv)
    assert result[0] == 'a'
    # After warmup, values should all be 0 (no divergence in a clean uptrend)
    arr = result[1]
    valid = arr[~np.isnan(arr)]
    assert len(valid) > 0
    assert np.all(np.abs(valid) < 0.5)  # all zeros


def test_divergence_warmup_is_nan():
    """First left+right+20 bars should be NaN."""
    ohlcv = _load_fixture('trend_up')
    result = _eval('divergence(close, rsi(14), 40)', ohlcv)
    assert np.all(np.isnan(result[1][:10]))


# ── Public API: evaluate_formula doesn't crash on new operators ────────

def test_evaluate_formula_missing_data():
    """Missing OHLCV should surface a clean error, not a crash."""
    from carrac.formula_eval import evaluate_formula
    result = evaluate_formula('rsi(14) < 30', '_NO_SUCH_SYMBOL_', '1D')
    # Either no data or empty — both are accepted
    assert result.get('error') is None or 'No OHLCV' in result.get('evidence', '')


def test_evaluate_formula_empty_expression():
    from carrac.formula_eval import evaluate_formula
    result = evaluate_formula('', 'BTC', '1D')
    assert 'Empty expression' in result['error']


def test_evaluate_formula_too_long():
    from carrac.formula_eval import evaluate_formula
    long_expr = 'rsi(14)' + ' + rsi(14)' * 200  # way over 500
    result = evaluate_formula(long_expr, 'BTC', '1D')
    assert 'too long' in result['error'].lower()


# ── EMA/Wilders seed on NaN prefix ─────────────────────────

def test_ema_seeds_past_nan_prefix():
    """EMA on a series with NaN prefix (e.g. derived from a slow EMA) must
    seed at the first valid p-window, not return all-NaN.

    Pins: `_ema` must not bail to NaN whenever `src[:period]` contains any
    NaN, or the MACD histogram never emits values.
    """
    from carrac.formula_eval import _ema
    # 20 NaN + 20 values = valid 9-window starts at index 20
    src = np.concatenate([np.full(20, np.nan), np.arange(20, dtype=np.float64)])
    out = _ema(src, 9)
    # First valid window is indices [20..28]; out[28] must be the SMA seed
    assert not np.isnan(out[28])
    # After seed, EMA continues forward
    assert not np.isnan(out[30])


def test_macd_hist_produces_values_after_warmup():
    """End-to-end: MACD hist must emit numbers starting at bar (slow + signal - 2),
    not be all-NaN. Same invariant as above."""
    import numpy as _np
    fixture = _load_fixture('trend_up')
    result = _eval('macd_hist(close, 12, 26, 9)', fixture)
    arr = result[1]
    # Slow EMA seeds at 25, signal EMA(9 of line) seeds 8 bars later → bar 33
    assert _np.isnan(arr[32])
    assert not _np.isnan(arr[33])


def test_wilders_seeds_past_nan_prefix():
    """Same fix applies to Wilder's smoothing (used by ATR and others)."""
    from carrac.formula_eval import _wilders
    src = np.concatenate([np.full(15, np.nan), np.ones(15, dtype=np.float64)])
    out = _wilders(src, 5)
    # Valid window first available at indices [15..19]
    assert not np.isnan(out[19])
    # Smoothing continues forward
    assert not np.isnan(out[25])
