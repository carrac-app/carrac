"""Tests for DSL aliases and errors: REF alias, MTF alias,
BBWIDTH/BBWIDTH_PCT, and the 'did you mean' error messages."""

import json
from pathlib import Path

import numpy as np
import pytest

from carrac.formula_eval import (
    FUNCTION_REGISTRY,
    Evaluator,
    Parser,
    tokenize,
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


# ── REF alias for OFFSET ────────────────────────────────────────────

def test_ref_registered_in_function_registry():
    assert 'ref' in FUNCTION_REGISTRY, "REF must be registered in FUNCTION_REGISTRY"


def test_ref_same_as_offset():
    """REF(close, 1) must equal OFFSET(close, 1) and close[1]."""
    ohlcv = _load_fixture('trend_up')
    ref_result = _eval('ref(close, 1)', ohlcv)
    offset_result = _eval('offset(close, 1)', ohlcv)
    index_result = _eval('close[1]', ohlcv)

    assert ref_result[0] == 'a'
    np.testing.assert_array_equal(ref_result[1], offset_result[1])
    np.testing.assert_array_equal(ref_result[1], index_result[1])


def test_ref_lookback_semantics():
    """REF(close, 1) at bar i should equal close at bar i-1 (positive n = look back)."""
    ohlcv = _load_fixture('trend_up')
    close = np.asarray(ohlcv['close'], dtype=np.float64)
    result = _eval('ref(close, 1)', ohlcv)
    arr = result[1]
    # First bar should be NaN (no previous bar)
    assert np.isnan(arr[0])
    # Bar 1 should equal close[0]
    assert arr[1] == close[0]
    # Bar 5 should equal close[4]
    assert arr[5] == close[4]


def test_ref_is_nan_for_out_of_range():
    """REF(close, n) where n >= len should produce NaN."""
    ohlcv = _load_fixture('trend_up')
    n = len(ohlcv['close'])
    result = _eval(f'ref(close, {n + 1})', ohlcv)
    assert np.all(np.isnan(result[1]))


# ── MTF alias for HTF ───────────────────────────────────────────────

def test_mtf_returns_nan_when_data_absent():
    """MTF behaves like HTF: returns NaN array when CSV cache has no data."""
    ohlcv = _load_fixture('trend_up')
    result = _eval("mtf('1W', close)", ohlcv, symbol='_UNKNOWN_', timeframe='1D')
    assert result[0] == 'a'
    assert np.all(np.isnan(result[1]))


def test_mtf_rejects_non_string_first_arg():
    """MTF inherits HTF validation — first arg must be a timeframe string."""
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError, match='timeframe string literal'):
        _eval("mtf(7, close)", ohlcv, timeframe='1D')


def test_mtf_rejects_lower_or_equal_timeframe():
    """MTF rejects same or lower timeframe (inherits HTF validation)."""
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError, match='must be higher'):
        _eval("mtf('1D', close)", ohlcv, timeframe='1D')


# ── BBWIDTH and BBWIDTH_PCT ─────────────────────────────────────────

def test_bbwidth_registered():
    assert 'bbwidth' in FUNCTION_REGISTRY
    assert 'bbwidth_pct' in FUNCTION_REGISTRY


def test_bbwidth_equals_upper_minus_lower():
    """BBWIDTH(20, 2) must equal BB_UPPER(20,2) - BB_LOWER(20,2) elementwise."""
    ohlcv = _load_fixture('trend_up')
    width = _eval('bbwidth(close, 20, 2)', ohlcv)
    upper = _eval('bb_upper(close, 20, 2)', ohlcv)
    lower = _eval('bb_lower(close, 20, 2)', ohlcv)
    diff = upper[1] - lower[1]

    valid = ~(np.isnan(width[1]) | np.isnan(diff))
    assert np.sum(valid) > 50, "Should have >50 valid bars"
    np.testing.assert_allclose(width[1][valid], diff[valid], rtol=1e-10)


def test_bbwidth_is_positive():
    """BBWIDTH must be strictly positive wherever BB bands exist."""
    ohlcv = _load_fixture('trend_up')
    result = _eval('bbwidth(close, 20, 2)', ohlcv)
    arr = result[1]
    valid = arr[~np.isnan(arr)]
    assert len(valid) > 0
    assert np.all(valid > 0), "BBWIDTH must be positive everywhere"


def test_bbwidth_default_args():
    """BBWIDTH() with no args should use defaults (close, period=20, mult=2)."""
    ohlcv = _load_fixture('trend_up')
    with_args = _eval('bbwidth(close, 20, 2)', ohlcv)
    no_args = _eval('bbwidth()', ohlcv)
    valid = ~(np.isnan(with_args[1]) | np.isnan(no_args[1]))
    np.testing.assert_allclose(with_args[1][valid], no_args[1][valid])


def test_bbwidth_pct_returns_reasonable_values():
    """BBWIDTH_PCT should be a positive percentage, typically 1–30% for crypto."""
    ohlcv = _load_fixture('trend_up')
    result = _eval('bbwidth_pct(close, 20, 2)', ohlcv)
    arr = result[1]
    valid = arr[~np.isnan(arr)]
    assert len(valid) > 0
    assert np.all(valid > 0), "BBWIDTH_PCT must be positive"
    # Typical crypto BB width % is 1–50%; sanity check
    assert np.all(valid < 200), "BBWIDTH_PCT sanity: should be under 200%"


def test_bbwidth_pct_formula():
    """BBWIDTH_PCT(20,2) == (BB_UPPER - BB_LOWER) / BB_MID * 100."""
    ohlcv = _load_fixture('trend_up')
    bwp = _eval('bbwidth_pct(close, 20, 2)', ohlcv)
    upper = _eval('bb_upper(close, 20, 2)', ohlcv)
    lower = _eval('bb_lower(close, 20, 2)', ohlcv)
    mid = _eval('bb_mid(close, 20)', ohlcv)

    manual = (upper[1] - lower[1]) / mid[1] * 100
    valid = ~(np.isnan(bwp[1]) | np.isnan(manual))
    assert np.sum(valid) > 50
    np.testing.assert_allclose(bwp[1][valid], manual[valid], rtol=1e-10)


# ── Improved error messages ─────────────────────────────────────────

def test_unknown_identifier_says_did_you_mean():
    """Unknown identifier error must contain 'did you mean' (case-insensitive)."""
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError) as exc:
        _eval('closee > 0', ohlcv)
    msg = str(exc.value).lower()
    assert 'did you mean' in msg


def test_unknown_function_says_did_you_mean():
    """Unknown function error must contain 'did you mean' (case-insensitive)."""
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError) as exc:
        _eval('rsis(14) < 30', ohlcv)
    msg = str(exc.value).lower()
    assert 'did you mean' in msg


def test_unknown_function_mentions_get_dsl_reference():
    """Unknown function error must mention indicator_get_dsl_reference."""
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError) as exc:
        _eval('totally_unknown_fn(14) < 30', ohlcv)
    msg = str(exc.value)
    assert 'indicator_get_dsl_reference' in msg


def test_identifier_that_is_a_function_suggests_parens():
    """If user writes 'rsi' (no parens), error should say 'add parentheses'."""
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError) as exc:
        _eval('rsi > 30', ohlcv)
    msg = str(exc.value).lower()
    # Should mention function / parentheses
    assert 'function' in msg or 'parenthes' in msg


def test_unknown_function_suggests_close_match():
    """Unknown function 'emaa' should suggest 'ema' as a close match."""
    ohlcv = _load_fixture('trend_up')
    with pytest.raises(ValueError) as exc:
        _eval('emaa(14) < 30', ohlcv)
    msg = str(exc.value).lower()
    assert 'ema' in msg
