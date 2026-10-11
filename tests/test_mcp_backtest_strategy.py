"""Tests for backtest_simulate, the first MCP-reachable
Full Simulation backtest (carrac/backtest_sim.py wired to MCP).

Covers: registration/dispatch, schema validation, the stopType default trap
(stop_percent/target_percent must actually change the result), the model
field, window fields (bars_used/window_start/window_end/history_available_
bars/truncated), the at-least-one-exit-path guard, and cache-key versioning.
"""

import json

import numpy as np
import pandas as pd
import pytest

import carrac.formula_eval as formula_eval
from carrac.mcp import tools  # noqa: F401  ensures registration
from carrac.mcp.registry import TOOL_REGISTRY
from carrac.mcp.protocol import ToolError
from carrac.mcp.backtest_models import MODEL_FULL_SIMULATION
from carrac.mcp.tools.backtest import _hold_bars_exit_expression, _sim_cache_key


def _make_daily_df(n_rows: int, start: str = '2015-01-01', seed: int = 275) -> pd.DataFrame:
    """Synthetic daily OHLCV with real intrabar range (high/low independently
    jittered from close) so stop/target levels can actually be crossed —
    unlike a flat/zero-range fixture, this lets crossover/crossunder-based
    entry/exit expressions fire multiple times over a few hundred bars."""
    dates = pd.date_range(start=start, periods=n_rows, freq='D')
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1.5, n_rows))
    high = close + np.abs(rng.normal(0.5, 0.3, n_rows))
    low = close - np.abs(rng.normal(0.5, 0.3, n_rows))
    open_ = close - rng.normal(0, 0.3, n_rows)
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


@pytest.fixture
def btc_cache(monkeypatch):
    """600 daily bars for BTC, real intrabar range."""
    df = _make_daily_df(600)
    monkeypatch.setattr(formula_eval, '_csv_cache', _FakeCSVCache({('BTC', 'daily'): df}))
    formula_eval._load_ohlcv_cached.cache_clear()
    yield df
    formula_eval._load_ohlcv_cached.cache_clear()


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Force the Redis response-cache lookup to miss so these tests always
    exercise a real computation (matches the sibling backtest tool tests)."""
    monkeypatch.setattr('carrac.redis_cache.get_redis', lambda: None)


def _fn():
    return TOOL_REGISTRY['backtest_simulate']['handler']


# ── Registration + dispatch ──────────────────────────────────────

class TestRegistration:
    def test_registered(self):
        assert 'backtest_simulate' in TOOL_REGISTRY

    def test_min_tier_and_feature(self):
        t = TOOL_REGISTRY['backtest_simulate']
        assert t['min_tier'] == 'member'
        assert t['feature'] == 'backtester'

    def test_dispatches_and_returns_model(self, btc_cache):
        out = _fn()({
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'lookback_bars': 300,
        })
        assert out['model'] == MODEL_FULL_SIMULATION == 'Full Simulation'


# ── Schema validation ─────────────────────────────────────────────

class TestSchemaValidation:
    def test_missing_symbol_raises(self):
        with pytest.raises(ToolError):
            _fn()({'entry_expression': 'close > 0', 'exit_expression': 'close < 0'})

    def test_missing_entry_expression_raises(self):
        with pytest.raises(ToolError):
            _fn()({'symbol': 'BTC', 'exit_expression': 'close < 0'})

    def test_invalid_entry_expression_raises(self):
        with pytest.raises(ToolError):
            _fn()({'symbol': 'BTC', 'entry_expression': 'garbage(((',
                   'exit_expression': 'close < 0'})

    def test_invalid_exit_expression_raises(self):
        with pytest.raises(ToolError):
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0',
                   'exit_expression': 'garbage((('})

    def test_invalid_direction_raises(self):
        with pytest.raises(ToolError):
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0',
                   'exit_expression': 'close < 0', 'direction': 'sideways'})

    def test_zero_stop_percent_raises(self):
        with pytest.raises(ToolError):
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0',
                   'stop_percent': 0})

    def test_negative_target_percent_raises(self):
        with pytest.raises(ToolError):
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0',
                   'target_percent': -5})

    def test_zero_max_hold_bars_raises(self):
        with pytest.raises(ToolError):
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0',
                   'max_hold_bars': 0})


# ── At-least-one-exit-path guard ──────────────────────────────────

class TestExitPathRequired:
    def test_no_exit_path_raises(self):
        with pytest.raises(ToolError) as exc:
            _fn()({'symbol': 'BTC', 'entry_expression': 'crossover(ema(5), ema(20))'})
        assert 'exit' in str(exc.value).lower()

    def test_exit_expression_alone_is_sufficient(self, btc_cache):
        out = _fn()({'symbol': 'BTC', 'entry_expression': 'crossover(ema(5), ema(20))',
                      'exit_expression': 'crossunder(ema(5), ema(20))', 'lookback_bars': 300})
        assert out['model'] == MODEL_FULL_SIMULATION

    def test_stop_percent_alone_is_sufficient(self, btc_cache):
        out = _fn()({'symbol': 'BTC', 'entry_expression': 'crossover(ema(5), ema(20))',
                      'stop_percent': 5, 'lookback_bars': 300})
        assert out['model'] == MODEL_FULL_SIMULATION

    def test_target_percent_alone_is_sufficient(self, btc_cache):
        out = _fn()({'symbol': 'BTC', 'entry_expression': 'crossover(ema(5), ema(20))',
                      'target_percent': 5, 'lookback_bars': 300})
        assert out['model'] == MODEL_FULL_SIMULATION

    def test_max_hold_bars_alone_is_sufficient(self, btc_cache):
        out = _fn()({'symbol': 'BTC', 'entry_expression': 'crossover(ema(5), ema(20))',
                      'max_hold_bars': 10, 'lookback_bars': 300})
        assert out['model'] == MODEL_FULL_SIMULATION


# ── stopType default trap: stop/target must actually change the result ──

class TestStopTargetActuallyApply:
    """The raw engine defaults stopType='close' (stop/target checking OFF).
    backtest_simulate must switch to 'intrabar' automatically whenever
    stop_percent/target_percent is supplied — otherwise the field would be
    silently accepted and ignored."""

    _BASE = {
        'symbol': 'BTC',
        'entry_expression': 'crossover(ema(5), ema(20))',
        'exit_expression': 'crossunder(ema(5), ema(20))',
        'lookback_bars': 500,
        'direction': 'long',
    }

    def test_stop_type_defaults_to_close_without_stop_or_target(self, btc_cache):
        out = _fn()(dict(self._BASE))
        assert out['stop_type'] == 'close'

    def test_stop_percent_switches_stop_type_to_intrabar(self, btc_cache):
        out = _fn()(dict(self._BASE, stop_percent=2))
        assert out['stop_type'] == 'intrabar'

    def test_target_percent_switches_stop_type_to_intrabar(self, btc_cache):
        out = _fn()(dict(self._BASE, target_percent=5))
        assert out['stop_type'] == 'intrabar'

    def test_stop_percent_changes_stats_vs_no_stop(self, btc_cache):
        """Real evidence: identical entry/exit/direction, only stop_percent
        differs. A tight stop must change total return, drawdown, and must
        actually produce stop_intrabar exits."""
        no_stop = _fn()(dict(self._BASE))
        with_stop = _fn()(dict(self._BASE, stop_percent=2))

        assert no_stop['stop_type'] == 'close'
        assert with_stop['stop_type'] == 'intrabar'

        # Same number of entry signals either way (stop only changes HOW
        # trades exit, not whether the entry formula fires) but the stats
        # must differ — a stop that has no effect on the numbers would mean
        # it was silently ignored.
        assert no_stop['stats']['totalReturnPercent'] != pytest.approx(
            with_stop['stats']['totalReturnPercent'])

        stop_exits = [t for t in with_stop['trades'] if 'stop' in t['exitReason']]
        assert len(stop_exits) > 0, 'stop_percent=2 produced zero stop-triggered exits'
        for t in stop_exits:
            # gapAwareFills (on by default) fills a gapped-through stop at the
            # WORSE of (nominal, bar open) — so the loss can exceed the
            # nominal 2%, never come in better than it.
            assert t['pnlPercent'] <= -1.9

        no_stop_exit_reasons = {t['exitReason'] for t in no_stop['trades']}
        assert 'stop_intrabar' not in no_stop_exit_reasons

    def test_target_percent_changes_stats_vs_no_target(self, btc_cache):
        no_target = _fn()(dict(self._BASE))
        with_target = _fn()(dict(self._BASE, target_percent=3))

        assert no_target['stats']['totalReturnPercent'] != pytest.approx(
            with_target['stats']['totalReturnPercent'])

        tp_exits = [t for t in with_target['trades'] if 'tp' in t['exitReason']]
        assert len(tp_exits) > 0, 'target_percent=3 produced zero target-triggered exits'
        for t in tp_exits:
            # gapAwareFills (on by default) fills a gapped-through target at
            # the BETTER of (nominal, bar open) — so the gain can exceed the
            # nominal 3%, never come in worse than it.
            assert t['pnlPercent'] >= 2.9


# ── max_hold_bars synthesis ────────────────────────────────────────

class TestMaxHoldBars:
    def test_hold_bars_exit_expression_is_valid_formula(self):
        expr = _hold_bars_exit_expression('close == 999', 4)
        valid = formula_eval.validate_formula(expr)
        assert valid['valid'], valid.get('error')

    def test_caps_holding_duration_for_edge_triggered_entry(self):
        """Direct engine-level check (bypassing the MCP tool's OHLCV loading)
        that the synthesized clause caps a trade at exactly max_hold_bars —
        this is the tool's ONLY way to express a time-based exit, since
        carrac/backtest_sim.py has no native hold-bars-cap parameter."""
        from carrac.backtest_sim import run_backtest

        entry = 'close == 999'
        expr = _hold_bars_exit_expression(entry, 4)
        n = 15
        ohlcv = {
            'open': [100.0] * n, 'high': [100.1] * n,
            'low': [99.9] * n, 'close': [100.0] * n, 'volume': [1.0] * n,
        }
        ohlcv['close'][2] = 999.0
        ohlcv['open'][2] = 999.0
        ohlcv['high'][2] = 999.1
        ohlcv['low'][2] = 998.9

        result = run_backtest(
            {'entry': entry, 'exit': expr, 'entryMode': 'next_open',
             'commissionPercent': 0, 'slippageBps': 0},
            ohlcv,
        )
        assert result['error'] is None
        assert len(result['trades']) == 1
        trade = result['trades'][0]
        assert trade['bars'] == 4
        assert trade['exitReason'] == 'signal'

    def test_tool_end_to_end_produces_bounded_trades(self, btc_cache):
        out = _fn()({
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'max_hold_bars': 8,
            'lookback_bars': 400,
        })
        assert out['max_hold_bars'] == 8
        assert all(t['bars'] <= 8 for t in out['trades'])
        assert out['effective_exit_expression'] != '0'


# ── Window fields (honesty about window) ──────────────────────

class TestWindowFields:
    def test_window_fields_present_and_consistent(self, btc_cache):
        out = _fn()({
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'lookback_bars': 300,
        })
        assert out['lookback_bars'] == 300
        assert out['bars_used'] == 300
        assert out['history_available_bars'] == 600
        assert out['truncated'] is False
        assert out['window_start'] < out['window_end']

    def test_truncated_flagged_honestly_not_silently_substituted(self, btc_cache):
        out = _fn()({
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'lookback_bars': 2000,
        })
        assert out['lookback_bars'] == 2000  # requested value, unchanged
        assert out['truncated'] is True
        assert out['history_available_bars'] == 600
        assert out['bars_used'] == 600  # actually measured


# ── Cache key versioning ──────────────────────────────────────────

class TestCacheKeyVersioning:
    def test_cache_key_changes_with_stop_percent(self):
        k1 = _sim_cache_key(symbol='BTC', entry='close > 0', exit='close < 0',
                             timeframe='1D', lookback_bars=300, direction='long',
                             stop_percent=None, target_percent=None, max_hold_bars=None,
                             size_percent=100, commission_percent=0, slippage_bps=0,
                             initial_capital=10000)
        k2 = _sim_cache_key(symbol='BTC', entry='close > 0', exit='close < 0',
                             timeframe='1D', lookback_bars=300, direction='long',
                             stop_percent=5, target_percent=None, max_hold_bars=None,
                             size_percent=100, commission_percent=0, slippage_bps=0,
                             initial_capital=10000)
        assert k1 != k2

    def test_cache_key_is_deterministic(self):
        kwargs = dict(symbol='BTC', entry='close > 0', exit='close < 0',
                       timeframe='1D', lookback_bars=300, direction='long',
                       stop_percent=5, target_percent=None, max_hold_bars=None,
                       size_percent=100, commission_percent=0, slippage_bps=0,
                       initial_capital=10000)
        assert _sim_cache_key(**kwargs) == _sim_cache_key(**kwargs)

    def test_cache_key_folds_in_engine_version(self, monkeypatch):
        """A fix to engine semantics must bust stale
        Redis entries automatically, so ENGINE_VERSION has to be part of the
        key, not just this tool's own _SIM_CACHE_VERSION."""
        import carrac.backtest_sim as backtest_sim

        kwargs = dict(symbol='BTC', entry='close > 0', exit='close < 0',
                       timeframe='1D', lookback_bars=300, direction='long',
                       stop_percent=None, target_percent=None, max_hold_bars=None,
                       size_percent=100, commission_percent=0, slippage_bps=0,
                       initial_capital=10000)
        k_before = _sim_cache_key(**kwargs)
        monkeypatch.setattr(backtest_sim, 'ENGINE_VERSION', 999)
        k_after = _sim_cache_key(**kwargs)
        assert k_before != k_after

    def test_tool_writes_and_reads_through_redis_cache(self, btc_cache, monkeypatch):
        store = {}

        class FakeRedis:
            def get(self, key):
                return store.get(key)

            def setex(self, key, ttl, value):
                store[key] = value

        monkeypatch.setattr('carrac.redis_cache.get_redis', lambda: FakeRedis())

        args = {
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'lookback_bars': 300,
        }
        first = _fn()(dict(args))
        assert '_cached' not in first
        assert store  # something was written

        second = _fn()(dict(args))
        assert second.get('_cached') is True
        assert second['model'] == MODEL_FULL_SIMULATION


# ── Trade cap / response shape ─────────────────────────────────────

class TestResponseShape:
    def test_trades_are_json_serializable(self, btc_cache):
        """Mirrors protocol.py's _handle_tools_call, which serializes every
        tool's return value via json.dumps(result, default=_json_default) —
        the numpy types backtest_sim.py's arrays/arithmetic produce are only
        safe because of that `default` hook, same as every other MCP tool."""
        from carrac.mcp.protocol import _json_default

        out = _fn()({
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'lookback_bars': 300,
        })
        json.dumps(out, default=_json_default)

    def test_exit_expression_null_when_not_supplied(self, btc_cache):
        out = _fn()({
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'max_hold_bars': 10,
            'lookback_bars': 300,
        })
        assert out['exit_expression'] is None
        assert 'barssince' in out['effective_exit_expression']


# ── stop_atr_mult / target_r_mult ──────────────────────

class TestAtrStopSchemaValidation:
    def test_zero_stop_atr_mult_raises(self):
        with pytest.raises(ToolError):
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0', 'stop_atr_mult': 0})

    def test_negative_target_r_mult_raises(self):
        with pytest.raises(ToolError):
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0',
                   'stop_atr_mult': 1.5, 'target_r_mult': -2})

    def test_stop_atr_mult_alone_is_sufficient_exit_path(self, btc_cache):
        out = _fn()({'symbol': 'BTC', 'entry_expression': 'crossover(ema(5), ema(20))',
                      'stop_atr_mult': 1.5, 'lookback_bars': 400})
        assert out['model'] == MODEL_FULL_SIMULATION

    def test_stop_percent_and_stop_atr_mult_together_raises(self):
        with pytest.raises(ToolError) as exc:
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0',
                   'stop_percent': 5, 'stop_atr_mult': 1.5})
        assert 'mutually exclusive' in str(exc.value).lower()

    def test_target_percent_and_target_r_mult_together_raises(self):
        with pytest.raises(ToolError) as exc:
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0',
                   'stop_atr_mult': 1.5, 'target_percent': 5, 'target_r_mult': 2})
        assert 'mutually exclusive' in str(exc.value).lower()

    def test_target_r_mult_without_any_stop_raises(self):
        with pytest.raises(ToolError) as exc:
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0', 'target_r_mult': 2})
        assert 'stop' in str(exc.value).lower()

    def test_target_r_mult_requires_a_stop_even_with_target_percent_omitted(self):
        # Sanity: the requires-check fires independent of any target_percent
        # combination, not just as a side effect of the mutual-exclusion check.
        with pytest.raises(ToolError) as exc:
            _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0',
                   'exit_expression': 'close < 0', 'target_r_mult': 3})
        assert 'stop' in str(exc.value).lower()

    def test_target_r_mult_accepted_alongside_a_percent_stop(self, btc_cache):
        """R is defined by the stop, not by the unit the stop was spelled in — so
        stop_percent + target_r_mult is a legal pairing and must NOT raise. This is
        the case the first cut rejected; see the engine docstring for
        why the conditional rule was dropped."""
        result = _fn()({'symbol': 'BTC', 'entry_expression': 'close > 0',
                        'stop_percent': 3, 'target_r_mult': 2})
        assert result['stop_mode'] == 'percent'
        assert result['target_mode'] == 'r_multiple'
        assert result['stop_type'] == 'intrabar'


class TestAtrStopAutoSwitch:
    """Same near-miss as for stop_percent:
    a test that would FAIL if the auto-switch-to-intrabar code were removed."""

    def test_stop_atr_mult_switches_stop_type_to_intrabar(self, btc_cache):
        out = _fn()({'symbol': 'BTC', 'entry_expression': 'crossover(ema(5), ema(20))',
                      'exit_expression': 'crossunder(ema(5), ema(20))',
                      'stop_atr_mult': 1.5, 'lookback_bars': 500})
        assert out['stop_type'] == 'intrabar'

    def test_target_r_mult_switches_stop_type_to_intrabar(self, btc_cache):
        out = _fn()({'symbol': 'BTC', 'entry_expression': 'crossover(ema(5), ema(20))',
                      'exit_expression': 'crossunder(ema(5), ema(20))',
                      'stop_atr_mult': 1.5, 'target_r_mult': 2, 'lookback_bars': 500})
        assert out['stop_type'] == 'intrabar'

    def test_stop_atr_mult_changes_stats_vs_no_stop(self, btc_cache):
        base = {
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'lookback_bars': 500,
        }
        no_stop = _fn()(dict(base))
        with_stop = _fn()(dict(base, stop_atr_mult=1.5))

        assert no_stop['stop_type'] == 'close'
        assert with_stop['stop_type'] == 'intrabar'
        assert no_stop['stats']['totalReturnPercent'] != pytest.approx(
            with_stop['stats']['totalReturnPercent'])

        stop_exits = [t for t in with_stop['trades'] if 'stop' in t['exitReason']]
        assert len(stop_exits) > 0, 'stop_atr_mult=1.5 produced zero stop-triggered exits'

        no_stop_reasons = {t['exitReason'] for t in no_stop['trades']}
        assert 'stop_intrabar' not in no_stop_reasons


class TestAtrStopResponseFields:
    def test_stop_mode_and_target_mode_reflect_which_unit_is_active(self, btc_cache):
        base = {
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'lookback_bars': 400,
        }
        neither = _fn()(dict(base))
        assert neither['stop_mode'] is None
        assert neither['target_mode'] is None

        percent = _fn()(dict(base, stop_percent=5, target_percent=8))
        assert percent['stop_mode'] == 'percent'
        assert percent['target_mode'] == 'percent'

        atr = _fn()(dict(base, stop_atr_mult=1.5, target_r_mult=2))
        assert atr['stop_mode'] == 'atr'
        assert atr['target_mode'] == 'r_multiple'

    def test_realized_stop_distance_reports_both_units(self, btc_cache):
        out = _fn()({
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'stop_atr_mult': 1.5,
            'lookback_bars': 500,
        })
        rsd = out['realized_stop_distance']
        assert rsd is not None
        assert rsd['atr_mult_requested'] == 1.5
        stop_exits = [t for t in out['trades'] if 'stop' in t['exitReason']]
        if stop_exits:
            assert rsd['n_trades_with_stop'] > 0
            assert rsd['median_percent'] > 0
            assert rsd['min_percent'] <= rsd['median_percent'] <= rsd['max_percent']

    def test_realized_stop_distance_is_null_for_percent_stop(self, btc_cache):
        out = _fn()({
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'stop_percent': 5,
            'lookback_bars': 300,
        })
        assert out['realized_stop_distance'] is None

    def test_trades_carry_planned_stop_and_target_prices(self, btc_cache):
        out = _fn()({
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'stop_atr_mult': 1.5,
            'target_r_mult': 2,
            'lookback_bars': 500,
        })
        assert len(out['trades']) > 0
        for t in out['trades']:
            assert 'stopPrice' in t
            assert 'tpPrice' in t
            assert t['stopPrice'] is not None  # every trade had an ATR stop configured


class TestAtrMultResultsMove:
    """Acceptance criterion: run the same strategy at mult 1.0/1.5/3.0 and
    confirm the results genuinely differ — byte-identical output across
    multipliers is the silent-substitution signature."""

    def test_results_move_across_stop_atr_mult(self, btc_cache):
        base = {
            'symbol': 'BTC',
            'entry_expression': 'crossover(ema(5), ema(20))',
            'exit_expression': 'crossunder(ema(5), ema(20))',
            'lookback_bars': 500,
        }
        r10 = _fn()(dict(base, stop_atr_mult=1.0))
        r15 = _fn()(dict(base, stop_atr_mult=1.5))
        r30 = _fn()(dict(base, stop_atr_mult=3.0))

        returns = (r10['stats']['totalReturnPercent'], r15['stats']['totalReturnPercent'],
                   r30['stats']['totalReturnPercent'])
        assert returns[0] != pytest.approx(returns[1])
        assert returns[1] != pytest.approx(returns[2])
        assert returns[0] != pytest.approx(returns[2])

        # Wider ATR multiples must produce a wider (or equal) realised stop
        # distance in percent terms — a monotonic sanity check, not just
        # "different numbers".
        med10 = r10['realized_stop_distance']['median_percent']
        med15 = r15['realized_stop_distance']['median_percent']
        med30 = r30['realized_stop_distance']['median_percent']
        assert med10 is not None and med15 is not None and med30 is not None
        assert med10 < med15 < med30

    def test_cache_key_changes_with_stop_atr_mult(self):
        k1 = _sim_cache_key(symbol='BTC', entry='close > 0', exit='close < 0',
                             timeframe='1D', lookback_bars=300, direction='long',
                             stop_percent=None, target_percent=None, max_hold_bars=None,
                             stop_atr_mult=1.0, target_r_mult=None,
                             size_percent=100, commission_percent=0, slippage_bps=0,
                             initial_capital=10000)
        k2 = _sim_cache_key(symbol='BTC', entry='close > 0', exit='close < 0',
                             timeframe='1D', lookback_bars=300, direction='long',
                             stop_percent=None, target_percent=None, max_hold_bars=None,
                             stop_atr_mult=3.0, target_r_mult=None,
                             size_percent=100, commission_percent=0, slippage_bps=0,
                             initial_capital=10000)
        assert k1 != k2
