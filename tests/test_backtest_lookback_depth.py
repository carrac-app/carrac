"""Tests that lookback_bars changes the measured window, and that the response
reports the window actually used rather than echoing the requested value.

Covers:
  - load_ohlcv / _load_ohlcv_cached accept a per-call `requested_bars` that
    actually changes the loaded window (rather than a hardcoded OHLCV_BARS).
  - The requested-bars value participates in the lru_cache key, so a deep
    request can never be served a shallow cached result or vice versa.
  - backtest_indicator_formula reports the window ACTUALLY measured
    (bars_used, window_start, window_end), not just the requested value.
  - Requesting more history than exists is flagged (truncated,
    history_available_bars) rather than silently substituted.
  - The Redis response-cache key changes when the engine/version bumps, so a
    fix like this one can never be masked by a stale cached entry.
  - Default callers (the /api/formula/* hot path, HTF alignment) still load
    exactly OHLCV_BARS (500) bars — unaffected by this change.
"""

import numpy as np
import pandas as pd
import pytest

import carrac.formula_eval as formula_eval
from carrac.mcp import tools  # noqa: F401  ensures tool registration
from carrac.mcp.tools.indicators import backtest_indicator_formula


def _make_daily_df(n_rows: int, start: str = '2015-01-01') -> pd.DataFrame:
    """Deterministic synthetic daily OHLCV — enough variety for STREAK/CROSS
    style formulas to actually produce flips, but reproducible across runs."""
    dates = pd.date_range(start=start, periods=n_rows, freq='D')
    rng = np.random.default_rng(276)
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
    """Stand-in for csv_cache.CSVCache — returns pre-built frames, no disk I/O."""

    def __init__(self, frames):
        self._frames = frames  # {(symbol, csv_tf): DataFrame}

    def load(self, symbol, timeframe='daily'):
        df = self._frames.get((symbol, timeframe))
        return df.copy() if df is not None else None


@pytest.fixture
def deep_history_cache(monkeypatch):
    """3000 daily bars for BTC — deep enough that lookback_bars actually matters
    (mirrors the real ~3272-row BTC history measured on real data)."""
    df = _make_daily_df(3000)
    fake = _FakeCSVCache({('BTC', 'daily'): df})
    monkeypatch.setattr(formula_eval, '_csv_cache', fake)
    formula_eval._load_ohlcv_cached.cache_clear()
    yield df
    formula_eval._load_ohlcv_cached.cache_clear()


@pytest.fixture
def shallow_history_cache(monkeypatch):
    """549 daily bars — mirrors the real shallow SPX depth seen in production."""
    df = _make_daily_df(549, start='2024-05-22')
    fake = _FakeCSVCache({('SPX', 'daily'): df})
    monkeypatch.setattr(formula_eval, '_csv_cache', fake)
    formula_eval._load_ohlcv_cached.cache_clear()
    yield df
    formula_eval._load_ohlcv_cached.cache_clear()


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Force the Redis response-cache lookup to miss so every test exercises
    a real computation, regardless of whether a local dev Redis is running."""
    monkeypatch.setattr('carrac.redis_cache.get_redis', lambda: None)


# ── load_ohlcv / _load_ohlcv_cached ─────────────────────────────────────

class TestLoadOhlcvRequestedBars:
    def test_default_still_loads_exactly_500(self, deep_history_cache):
        """Unaffected default-path callers (routes.py x2, HTF alignment,
        evaluate_formula) must keep loading exactly OHLCV_BARS."""
        ohlcv = formula_eval.load_ohlcv('BTC', '1D')
        assert len(ohlcv['close']) == formula_eval.OHLCV_BARS == 500
        assert ohlcv['_total_available'] == 3000

    def test_requested_bars_changes_window(self, deep_history_cache):
        ohlcv_500 = formula_eval.load_ohlcv('BTC', '1D', requested_bars=500)
        ohlcv_2000 = formula_eval.load_ohlcv('BTC', '1D', requested_bars=2000)
        assert len(ohlcv_500['close']) == 500
        assert len(ohlcv_2000['close']) == 2000
        # Different windows -> different oldest bar.
        assert ohlcv_500['_dates'][0] != ohlcv_2000['_dates'][0]

    def test_requested_bars_is_part_of_cache_key(self, deep_history_cache):
        """Pins the cache-key failure mode:
        functools.lru_cache keyed only on (symbol, timeframe, cache_key) would
        serve a 500-bar result to a 2000-bar request within the same 5-min
        bucket. Calling both within the same test proves they share a
        cache_key bucket yet still return different-sized windows."""
        a = formula_eval.load_ohlcv('BTC', '1D', requested_bars=500)
        b = formula_eval.load_ohlcv('BTC', '1D', requested_bars=2000)
        assert len(a['close']) != len(b['close'])

    def test_total_available_reflects_true_csv_depth(self, shallow_history_cache):
        """Even when asked for far more than exists, _total_available reports
        the true on-disk depth, not the (possibly smaller) trimmed window."""
        ohlcv = formula_eval.load_ohlcv('SPX', '1D', requested_bars=2000)
        assert ohlcv['_total_available'] == 549
        assert len(ohlcv['close']) == 549  # can't manufacture bars that don't exist


# ── backtest_indicator_formula ──────────────────────────────────────────

def _bt(**kwargs):
    fn = backtest_indicator_formula
    args = {'symbol': 'BTC', 'expression': 'close < close[1]', 'hold_bars': 5}
    args.update(kwargs)
    return fn(args)


class TestBacktestLookbackHonesty:
    def test_lookback_bars_changes_measured_window(self, deep_history_cache):
        out_500 = _bt(lookback_bars=500)
        out_2000 = _bt(lookback_bars=2000)
        assert out_500['bars_used'] == 500
        assert out_2000['bars_used'] == 2000
        assert out_500['window_start'] != out_2000['window_start']
        # Two different
        # lookbacks must not be byte-identical once real depth exists.
        assert (out_500['trades'], out_500['hit_rate']) != (
            out_2000['trades'], out_2000['hit_rate']
        )

    def test_response_reports_actual_window(self, deep_history_cache):
        out = _bt(lookback_bars=800)
        assert out['lookback_bars'] == 800  # requested value, unchanged meaning
        assert out['bars_used'] == 800
        assert 'window_start' in out and 'window_end' in out
        assert out['window_start'] < out['window_end']
        assert out['history_available_bars'] == 3000
        assert out['truncated'] is False

    def test_requesting_more_than_exists_is_flagged(self, shallow_history_cache):
        out = backtest_indicator_formula({
            'symbol': 'SPX', 'expression': 'close < close[1]',
            'lookback_bars': 2000, 'hold_bars': 5,
        })
        assert out['truncated'] is True
        assert out['history_available_bars'] == 549
        assert out['bars_used'] <= 549
        # Never silently substitute — the requested value must stay visible.
        assert out['lookback_bars'] == 2000

    def test_not_truncated_when_enough_history(self, deep_history_cache):
        out = _bt(lookback_bars=500)
        assert out['truncated'] is False
        assert out['history_available_bars'] == 3000

    def test_load_ohlcv_called_with_warmup_margin(self, deep_history_cache, monkeypatch):
        """backtest_indicator_formula must ask for lookback_bars PLUS a warmup
        margin, not exactly lookback_bars, so indicators have room to
        stabilize before start_idx."""
        import carrac.mcp.tools.indicators as ind_mod

        real_load_ohlcv = formula_eval.load_ohlcv
        captured = {}

        def spy(symbol, timeframe='1D', requested_bars=None):
            captured['requested_bars'] = requested_bars
            return real_load_ohlcv(symbol, timeframe, requested_bars=requested_bars)

        monkeypatch.setattr(formula_eval, 'load_ohlcv', spy)
        _bt(lookback_bars=800)
        assert captured['requested_bars'] == 800 + ind_mod._BACKTEST_WARMUP_MARGIN_BARS


class TestBacktestCacheKeyVersioning:
    def test_cache_key_changes_with_engine_version(self, monkeypatch):
        import carrac.mcp.tools.indicators as ind_mod

        key_v_current = ind_mod._backtest_cache_key('BTC', 'close < close[1]', '1D', 500, 5)
        monkeypatch.setattr(ind_mod, '_BACKTEST_CACHE_VERSION', 999999)
        key_v_bumped = ind_mod._backtest_cache_key('BTC', 'close < close[1]', '1D', 500, 5)

        assert key_v_current != key_v_bumped

    def test_cache_key_stable_for_identical_inputs(self):
        import carrac.mcp.tools.indicators as ind_mod

        k1 = ind_mod._backtest_cache_key('BTC', 'close < close[1]', '1D', 500, 5)
        k2 = ind_mod._backtest_cache_key('BTC', 'close < close[1]', '1D', 500, 5)
        assert k1 == k2
