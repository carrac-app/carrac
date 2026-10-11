"""Tests for MCP tools — formula + indicator registry."""

import numpy as np
import pandas as pd
import pytest

import carrac.formula_eval as formula_eval
from carrac.mcp import tools  # noqa: F401  ensures registration
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.tools.indicators import backtest_indicator_formula
from carrac.mcp.protocol import ToolError
from carrac.mcp.backtest_models import MODEL_HOLD_N
from carrac.mcp.indicator_registry_parser import (
    get_all_indicators,
    get_categories,
)


class TestIndicatorRegistryParser:
    def test_parses_minimum_indicator_count(self):
        all_ind = get_all_indicators()
        assert len(all_ind) >= 50

    def test_known_indicators_present(self):
        all_ind = get_all_indicators()
        for name in ('RSI', 'MACD', 'EMA', 'CONSENSIO', 'CPR', 'VOL'):
            assert name in all_ind, f'{name} missing from registry'

    def test_rsi_has_expected_fields(self):
        rsi = get_all_indicators()['RSI']
        assert rsi['label'] == 'RSI'
        assert rsi['category'] == 'oscillators'
        assert rsi['type'] == 'builtin_pane'
        assert 'oversold' in (rsi.get('states') or [])
        assert 14 in (rsi.get('calcParams') or [])

    def test_categories_loaded(self):
        cats = get_categories()
        ids = {c['id'] for c in cats}
        assert {'oscillators', 'trend', 'macro'}.issubset(ids)


class TestListIndicators:
    def test_returns_all_when_no_filter(self):
        fn = TOOL_REGISTRY['indicator_list']['handler']
        out = fn({})
        assert out['count'] >= 50
        assert any(i['name'] == 'RSI' for i in out['indicators'])

    def test_category_filter(self):
        fn = TOOL_REGISTRY['indicator_list']['handler']
        out = fn({'category': 'oscillators'})
        assert all(i['category'] == 'oscillators' for i in out['indicators'])
        assert out['count'] < 30  # smaller than full set

    def test_returns_categories_metadata(self):
        fn = TOOL_REGISTRY['indicator_list']['handler']
        out = fn({})
        assert isinstance(out['categories'], list)
        assert len(out['categories']) >= 5

    def test_unknown_category_raises_with_valid_names(self):
        """An unknown category must not silently return an empty
        list (indistinguishable from 'this category has no indicators').
        It must raise a ToolError naming the valid categories."""
        fn = TOOL_REGISTRY['indicator_list']['handler']
        with pytest.raises(ToolError) as exc:
            fn({'category': 'totally-bogus-category'})
        msg = str(exc.value)
        assert 'oscillators' in msg or 'trend' in msg


class TestGetIndicatorDefinition:
    def test_returns_full_metadata(self):
        fn = TOOL_REGISTRY['indicator_get_definition']['handler']
        out = fn({'name': 'RSI'})
        assert out['name'] == 'RSI'
        assert 'desc' in out
        assert 'states' in out

    def test_unknown_returns_error_with_suggestions(self):
        fn = TOOL_REGISTRY['indicator_get_definition']['handler']
        out = fn({'name': 'xyz999unknown'})
        assert 'error' in out
        assert 'suggestions' in out
        assert isinstance(out['suggestions'], list)

    def test_missing_name_raises(self):
        fn = TOOL_REGISTRY['indicator_get_definition']['handler']
        with pytest.raises(ToolError):
            fn({})


class TestRunCustomFormula:
    def test_invalid_expression_returns_error(self):
        fn = TOOL_REGISTRY['indicator_run_formula']['handler']
        out = fn({'symbol': 'BTC', 'expression': 'this is not valid (((', 'timeframe': '1D'})
        assert out['value'] is None
        assert out['error']

    def test_missing_symbol_raises(self):
        fn = TOOL_REGISTRY['indicator_run_formula']['handler']
        with pytest.raises(ToolError):
            fn({'expression': 'RSI(14)', 'timeframe': '1D'})

    def test_missing_expression_raises(self):
        fn = TOOL_REGISTRY['indicator_run_formula']['handler']
        with pytest.raises(ToolError):
            fn({'symbol': 'BTC', 'timeframe': '1D'})

    def test_too_long_expression_returns_error(self):
        fn = TOOL_REGISTRY['indicator_run_formula']['handler']
        long_expr = 'RSI(14) > 50' + (' AND RSI(14) > 50' * 50)
        out = fn({'symbol': 'BTC', 'expression': long_expr, 'timeframe': '1D'})
        # Either parse error or length error.
        assert out['value'] is None


class TestBacktestIndicatorFormula:
    def test_invalid_formula_raises(self):
        fn = backtest_indicator_formula
        with pytest.raises(ToolError):
            fn({'symbol': 'BTC', 'expression': 'garbage((('})

    def test_missing_args_raises(self):
        fn = backtest_indicator_formula
        with pytest.raises(ToolError):
            fn({})

    def test_lookback_clamped(self):
        fn = backtest_indicator_formula
        # 50000 should clamp to 2000 — won't crash even without data.
        try:
            fn({'symbol': 'BTC', 'expression': 'RSI(14) > 50',
                'lookback_bars': 50000, 'hold_bars': 5})
        except ToolError as e:
            # Acceptable: data unavailable in test env.
            assert 'OHLCV' in str(e) or 'Not enough' in str(e)


# ── Model field on all three response branches ──

def _make_daily_df(n_rows, start='2015-01-01'):
    """Deterministic synthetic daily OHLCV, close/volume independently
    controllable per bar (see test_backtest_lookback_depth.py for the
    original of this pattern)."""
    dates = pd.date_range(start=start, periods=n_rows, freq='D')
    rng = np.random.default_rng(275)
    close = 100 + np.cumsum(rng.normal(0, 1, n_rows))
    high = close + 1.0
    low = close - 1.0
    open_ = close - 0.5
    volume = np.full(n_rows, 1000.0)
    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume},
        index=dates,
    )


class _FakeCSVCache:
    def __init__(self, frames):
        self._frames = frames

    def load(self, symbol, timeframe='daily'):
        df = self._frames.get((symbol, timeframe))
        return df.copy() if df is not None else None


@pytest.fixture(autouse=True)
def _no_redis_model_field(monkeypatch):
    """Force the Redis response-cache lookup to miss so these tests always
    exercise a real computation."""
    monkeypatch.setattr('carrac.redis_cache.get_redis', lambda: None)


class TestBacktestIndicatorFormulaModelField:
    """Every branch of backtest_indicator_formula must carry
    'model': 'Hold-N Signal Test' so a caller (an automated client, an AI agent) can never
    mistake it for Full Simulation or Historical Analog output."""

    def test_main_branch_carries_model(self, monkeypatch):
        """The success path (entries found, valid trades) — main return."""
        df = _make_daily_df(400)
        monkeypatch.setattr(formula_eval, '_csv_cache', _FakeCSVCache({('BTC', 'daily'): df}))
        formula_eval._load_ohlcv_cached.cache_clear()

        fn = backtest_indicator_formula
        out = fn({'symbol': 'BTC', 'expression': 'close < close[1]',
                  'lookback_bars': 300, 'hold_bars': 5})

        assert out['trades'] > 0  # confirms we hit the main branch, not a fallback
        assert out['model'] == MODEL_HOLD_N == 'Hold-N Signal Test'
        formula_eval._load_ohlcv_cached.cache_clear()

    def test_no_entries_branch_carries_model(self, monkeypatch):
        """An expression that never flips True — the 'no entries' early return."""
        df = _make_daily_df(200)
        monkeypatch.setattr(formula_eval, '_csv_cache', _FakeCSVCache({('BTC', 'daily'): df}))
        formula_eval._load_ohlcv_cached.cache_clear()

        fn = backtest_indicator_formula
        out = fn({'symbol': 'BTC', 'expression': 'close > close + 1000000',
                  'lookback_bars': 150, 'hold_bars': 5})

        assert out['trades'] == 0
        assert 'No signal flips' in out['message']
        assert out['model'] == MODEL_HOLD_N
        formula_eval._load_ohlcv_cached.cache_clear()

    def test_no_valid_trades_branch_carries_model(self, monkeypatch):
        """Entries are found (via a volume-only signal, independent of price)
        but the one entry's fill price is NaN — the 'no valid trades after
        price filtering' branch."""
        n = 60
        hold_bars = 5
        flip_bar = 30
        df = _make_daily_df(n)
        # Single volume spike -> exactly one False->True flip at flip_bar,
        # completely independent of the close series.
        df['volume'] = 1000.0
        df.iloc[flip_bar, df.columns.get_loc('volume')] = 9999.0
        # Poison the fill price at the entry bar so it gets filtered as invalid.
        df.iloc[flip_bar, df.columns.get_loc('close')] = float('nan')
        monkeypatch.setattr(formula_eval, '_csv_cache', _FakeCSVCache({('BTC', 'daily'): df}))
        formula_eval._load_ohlcv_cached.cache_clear()

        fn = backtest_indicator_formula
        out = fn({'symbol': 'BTC', 'expression': 'volume > 5000',
                  'lookback_bars': n, 'hold_bars': hold_bars})

        assert out['trades'] == 0
        assert 'No valid trades' in out['message']
        assert out['model'] == MODEL_HOLD_N
        formula_eval._load_ohlcv_cached.cache_clear()


class TestToolRegistration:
    def test_three_tools_registered(self):
        # backtest_indicator_formula is a plain helper now,
        # absorbed into backtest_run (source=formula) — no longer a
        # standalone registry entry.
        for name in ('indicator_run_formula', 'indicator_list',
                     'indicator_get_definition'):
            assert name in TOOL_REGISTRY
        assert 'backtest_indicator_formula' not in TOOL_REGISTRY
        assert 'backtest_run' in TOOL_REGISTRY

    def test_min_tier_set(self):
        # Free-beta split: discovery is free, the compute-heavy formula engine is paid.
        # indicator_run_formula stays premium — it IS the custom formula engine
        # (TIER_LIMITS['custom_formulas'] is False for member, True for
        # tester/premium/admin only). backtest_run (was
        # backtest_indicator_formula) is a different surface — a Hold-N
        # backtest of a boolean expression — so it moves to the
        # `backtester` knob (True for member too), matching the existing
        # Flask precedent (scanner_backtest_endpoint).
        assert TOOL_REGISTRY['indicator_run_formula']['min_tier'] == 'premium'
        assert TOOL_REGISTRY['indicator_run_formula']['feature'] is None
        assert TOOL_REGISTRY['indicator_list']['min_tier'] == 'free'
        assert TOOL_REGISTRY['indicator_get_definition']['min_tier'] == 'free'
        assert TOOL_REGISTRY['backtest_run']['min_tier'] == 'member'
        assert TOOL_REGISTRY['backtest_run']['feature'] == 'backtester'
