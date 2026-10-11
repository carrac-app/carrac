"""
Unit tests for carrac/charts/indicators_extended.py.

Covers: All 24 extended indicator snapshot computations,
timeframe filtering, edge cases, and the main entry point.
"""

import pytest
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_df(n=300, trend='up', seed=42):
    """Create deterministic OHLCV DataFrame for indicator testing."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end='2026-01-01', periods=n, freq='D')

    drift = {'up': 0.15, 'down': -0.15, 'sideways': 0.0}[trend]
    close = 100 + np.cumsum(rng.standard_normal(n) * 0.5 + drift)
    high = close + np.abs(rng.standard_normal(n)) * 0.5
    low = close - np.abs(rng.standard_normal(n)) * 0.5
    open_ = close + rng.standard_normal(n) * 0.3
    volume = rng.integers(1000, 10000, n).astype(float)

    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume},
        index=dates,
    )


@pytest.fixture
def uptrend():
    return _make_df(300, 'up')


@pytest.fixture
def downtrend():
    return _make_df(300, 'down')


@pytest.fixture
def sideways():
    return _make_df(300, 'sideways')


@pytest.fixture
def short_df():
    """Too few bars for most indicators."""
    return _make_df(5, 'up')


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

class TestBollingerBands:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _bollinger_bands
        snap = _bollinger_bands(uptrend)
        assert snap is not None
        assert 'basis' in snap
        assert 'upper' in snap
        assert 'lower' in snap
        assert 'bandwidth_pct' in snap
        assert 'pct_b' in snap
        assert snap['position'] in ('above_upper', 'below_lower', 'inside')
        assert snap['upper'] > snap['basis'] > snap['lower']

    def test_insufficient_data(self, short_df):
        from carrac.charts.indicators_extended import _bollinger_bands
        assert _bollinger_bands(short_df) is None


# ---------------------------------------------------------------------------
# TTM Squeeze
# ---------------------------------------------------------------------------

class TestTTMSqueeze:
    def test_basic_output(self, sideways):
        from carrac.charts.indicators_extended import _ttm_squeeze
        snap = _ttm_squeeze(sideways)
        assert snap is not None
        assert 'active' in snap
        assert 'bars' in snap
        assert 'momentum' in snap
        assert isinstance(snap['active'], bool)
        assert isinstance(snap['bars'], int)
        assert snap['bars'] >= 0
        # bars_since_release must always be present (None or int),
        # even on old-style expectations that only checked the first 3 keys.
        assert 'bars_since_release' in snap
        assert snap['bars_since_release'] is None or isinstance(snap['bars_since_release'], int)

    def test_insufficient_data(self, short_df):
        from carrac.charts.indicators_extended import _ttm_squeeze
        assert _ttm_squeeze(short_df) is None

    def test_still_active_has_no_release_yet(self, sideways):
        # When currently squeezed, bars_since_release is None — nothing has
        # released yet (this fixture's tight synthetic range typically stays
        # squeezed for its whole tail; if not, active gates the assertion).
        from carrac.charts.indicators_extended import _ttm_squeeze
        snap = _ttm_squeeze(sideways)
        if snap['active']:
            assert snap['bars_since_release'] is None


# ---------------------------------------------------------------------------
# _bars_since_last_qualifying_squeeze
# ---------------------------------------------------------------------------

class TestBarsSinceLastQualifyingSqueeze:
    """Pure-algorithm tests, independent of BB/KC price synthesis — hand-built
    boolean squeeze sequences (oldest-first, same order/semantics as
    `_ttm_squeeze`'s `squeeze` Series)."""

    def _fn(self, *a, **kw):
        from carrac.charts.indicators_extended import _bars_since_last_qualifying_squeeze
        return _bars_since_last_qualifying_squeeze(*a, **kw)

    def test_released_two_bars_ago(self):
        # A 4-bar qualifying squeeze (>= min_duration=3) that ended 2 bars
        # before the latest bar.
        bars = [False] * 5 + [True] * 4 + [False] * 3
        assert self._fn(bars, min_duration=3) == 2

    def test_released_ten_bars_ago(self):
        bars = [True] * 4 + [False] * 10
        assert self._fn(bars, min_duration=3) == 9

    def test_immediately_after_release(self):
        bars = [True] * 3 + [False]
        assert self._fn(bars, min_duration=3) == 0

    def test_never_squeezed_returns_none(self):
        bars = [False] * 10
        assert self._fn(bars, min_duration=3) is None

    def test_sub_threshold_blip_does_not_count_as_a_real_squeeze(self):
        # Only a 2-bar True run (< min_duration=3) — not a real squeeze, so
        # there's nothing to have "released" from.
        bars = [False] * 5 + [True, True] + [False] * 3
        assert self._fn(bars, min_duration=3) is None

    def test_currently_in_squeeze_returns_none(self):
        # Latest bar is still True — nothing has released yet, regardless of
        # whether this run has reached min_duration.
        bars = [False] * 5 + [True] * 5
        assert self._fn(bars, min_duration=3) is None

    def test_empty_sequence_returns_none(self):
        assert self._fn([], min_duration=3) is None

    def test_only_the_most_recent_run_is_considered(self):
        # An earlier qualifying run exists further back, but the most recent
        # True run (closer to the tail) is what bars_since_release measures.
        bars = [True, True, True, True] + [False] * 20 + [True, True, True] + [False] * 4
        assert self._fn(bars, min_duration=3) == 3


# ---------------------------------------------------------------------------
# Ichimoku
# ---------------------------------------------------------------------------

class TestIchimoku:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _ichimoku
        snap = _ichimoku(uptrend)
        assert snap is not None
        assert 'cloud_position' in snap
        assert 'cloud_color' in snap
        assert snap['cloud_color'] in ('bullish', 'bearish')
        assert 'tk_cross' in snap
        assert snap['cloud_top'] >= snap['cloud_bottom']

    def test_insufficient_data(self, short_df):
        from carrac.charts.indicators_extended import _ichimoku
        assert _ichimoku(short_df) is None


# ---------------------------------------------------------------------------
# PSAR
# ---------------------------------------------------------------------------

class TestPSAR:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _psar
        snap = _psar(uptrend)
        assert snap is not None
        assert snap['direction'] in ('long', 'short')
        assert isinstance(snap['value'], float)
        assert isinstance(snap['flip'], bool)

    def test_both_directions_possible(self, uptrend, downtrend):
        from carrac.charts.indicators_extended import _psar
        up_snap = _psar(uptrend)
        down_snap = _psar(downtrend)
        # Both should produce valid direction values
        assert up_snap['direction'] in ('long', 'short')
        assert down_snap['direction'] in ('long', 'short')

    def test_insufficient_data(self, short_df):
        from carrac.charts.indicators_extended import _psar
        # 5 bars is enough for PSAR (minimum 3)
        snap = _psar(short_df)
        assert snap is not None


# ---------------------------------------------------------------------------
# SuperTrend
# ---------------------------------------------------------------------------

class TestSuperTrend:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _supertrend
        snap = _supertrend(uptrend)
        assert snap is not None
        assert snap['direction'] in ('long', 'short')
        assert isinstance(snap['value'], float)
        assert isinstance(snap['flip'], bool)

    def test_insufficient_data(self, short_df):
        from carrac.charts.indicators_extended import _supertrend
        assert _supertrend(short_df) is None


# ---------------------------------------------------------------------------
# Stochastic
# ---------------------------------------------------------------------------

class TestStochastic:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _stochastic
        snap = _stochastic(uptrend)
        assert snap is not None
        assert 0 <= snap['k'] <= 100
        assert snap['zone'] in ('oversold', 'overbought', 'neutral')

    def test_insufficient_data(self, short_df):
        from carrac.charts.indicators_extended import _stochastic
        assert _stochastic(short_df) is None


# ---------------------------------------------------------------------------
# StochRSI
# ---------------------------------------------------------------------------

class TestStochRSI:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _stoch_rsi
        snap = _stoch_rsi(uptrend)
        assert snap is not None
        assert 'k' in snap
        assert 'zone' in snap

    def test_insufficient_data(self, short_df):
        from carrac.charts.indicators_extended import _stoch_rsi
        assert _stoch_rsi(short_df) is None


# ---------------------------------------------------------------------------
# OBV
# ---------------------------------------------------------------------------

class TestOBV:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _obv
        snap = _obv(uptrend)
        assert snap is not None
        assert snap['trend'] in ('rising', 'falling', 'flat')
        assert snap['divergence'] in ('bullish', 'bearish', 'none')

    def test_insufficient_data(self, short_df):
        from carrac.charts.indicators_extended import _obv
        assert _obv(short_df) is None


# ---------------------------------------------------------------------------
# Auto Fibonacci
# ---------------------------------------------------------------------------

class TestAutoFib:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _auto_fib
        snap = _auto_fib(uptrend)
        assert snap is not None
        assert 'swing_high' in snap
        assert 'swing_low' in snap
        assert snap['swing_high'] > snap['swing_low']
        assert 'levels' in snap
        assert 0.618 in snap['levels']
        assert snap['trend'] in ('uptrend', 'downtrend')

    def test_uptrend_levels_ascending(self, uptrend):
        from carrac.charts.indicators_extended import _auto_fib
        snap = _auto_fib(uptrend)
        levels = snap['levels']
        assert levels[0] < levels[0.5] < levels[1.0]


# ---------------------------------------------------------------------------
# Additional Indicators
# ---------------------------------------------------------------------------

class TestKeltner:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _keltner
        snap = _keltner(uptrend)
        assert snap is not None
        assert snap['upper'] > snap['basis'] > snap['lower']
        assert snap['position'] in ('above_upper', 'below_lower', 'inside')


class TestBBWPercentile:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _bbw_percentile
        snap = _bbw_percentile(uptrend)
        assert snap is not None
        assert 0 <= snap['percentile'] <= 100
        assert isinstance(snap['compression'], bool)


class TestATRPercentile:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _atr_percentile
        snap = _atr_percentile(uptrend)
        assert snap is not None
        assert 0 <= snap['percentile'] <= 100


class TestWilliamsR:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _williams_r
        snap = _williams_r(uptrend)
        assert snap is not None
        assert -100 <= snap['value'] <= 0
        assert snap['zone'] in ('oversold', 'overbought', 'neutral')


class TestCCI:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _cci
        snap = _cci(uptrend)
        assert snap is not None
        assert 'value' in snap
        assert snap['zone'] in ('oversold', 'overbought', 'neutral')


class TestDonchian:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _donchian
        snap = _donchian(uptrend)
        assert snap is not None
        assert snap['upper'] > snap['lower']
        assert snap['mid'] == pytest.approx((snap['upper'] + snap['lower']) / 2, abs=0.01)


class TestHullMA:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _hull_ma
        snap = _hull_ma(uptrend)
        assert snap is not None
        assert snap['direction'] in ('rising', 'falling')


class TestALMA:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _alma
        snap = _alma(uptrend)
        assert snap is not None
        assert snap['direction'] in ('rising', 'falling')


class TestKAMA:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _kama
        snap = _kama(uptrend)
        assert snap is not None
        assert snap['direction'] in ('rising', 'falling')


class TestEMACross:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _ema_cross
        snap = _ema_cross(uptrend)
        assert snap is not None
        assert snap['state'] in ('bullish', 'bearish')
        assert snap['cross'] in ('bullish', 'bearish', None)
        assert snap['bars_since_cross'] >= 0


class TestAlligator:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _alligator
        snap = _alligator(uptrend)
        assert snap is not None
        assert snap['state'] in ('eating_bullish', 'eating_bearish', 'sleeping', 'mixed')
        assert 'jaw' in snap and 'teeth' in snap and 'lips' in snap


class TestConsensio:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _consensio
        snap = _consensio(uptrend)
        assert snap is not None
        assert -3 <= snap['signal'] <= 3

    def test_signal_range(self, uptrend):
        from carrac.charts.indicators_extended import _consensio
        snap = _consensio(uptrend)
        assert -3 <= snap['signal'] <= 3


class TestConsensioTruthTableParity:
    """_consensio()'s `signal` field must match
    consensio.indicators.consensio_core.ConsensioCalculator.calculate_signal()'s
    MA-ordering truth table EXACTLY — hand-transcribed from
    consensio_core.py::calculate_signal (its docstring + if/elif chain), not a divergent
    price-inclusive ordering computed independently (which would return -1
    for an M>S>L ordering where the canonical calculator returns +2).

    Truth table (S/M/L = short/medium/long MA values; no price interaction,
    no magnitude weighting — pure ordering; any tie -> 0):
        S>M>L -> +3   M>S>L -> +2   S>L>M -> +1
        M>L>S -> -1   L>S>M -> -2   L>M>S -> -3
    """

    TRUTH_TABLE = [
        ((30, 20, 10), 3),   # S > M > L
        ((20, 30, 10), 2),   # M > S > L
        ((30, 10, 20), 1),   # S > L > M
        ((10, 30, 20), -1),  # M > L > S
        ((20, 10, 30), -2),  # L > S > M
        ((10, 20, 30), -3),  # L > M > S
        ((20, 20, 20), 0),   # tie -> no ordering holds
    ]

    def test_all_orderings_match_consensio_core(self, uptrend, monkeypatch):
        """Pin ConsensioCalculator's own MA outputs to each hand-built (s, m, l)
        triple (via its _calculate_ma hook) and confirm _consensio()'s signal
        agrees with the ordering that triple represents — exercising the
        REAL (unmodified) calculate_signal() ordering logic, not a re-
        implementation of it."""
        from carrac.charts.indicators_extended import _consensio
        from consensio.indicators.consensio_core import ConsensioCalculator

        period_to_slot = {3: 's', 7: 'm', 30: 'lo'}

        for (s, m, lo), expected in self.TRUTH_TABLE:
            slot_values = {'s': s, 'm': m, 'lo': lo}

            def fake_calculate_ma(self, series, period, _vals=slot_values):
                return pd.Series(_vals[period_to_slot[period]], index=series.index)

            monkeypatch.setattr(ConsensioCalculator, '_calculate_ma', fake_calculate_ma)
            snap = _consensio(uptrend, period_s=3, period_m=7, period_l=30)
            assert snap is not None
            assert snap['signal'] == expected, (
                f"s={s} m={m} l={lo}: expected {expected}, got {snap['signal']}"
            )


class TestSRChannels:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _sr_channels
        snap = _sr_channels(uptrend)
        # May return None if no pivots detected in synthetic data
        if snap is not None:
            assert isinstance(snap, list)
            assert all('level' in c and 'strength' in c for c in snap)


class TestFVG:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _fvg
        snap = _fvg(uptrend)
        # FVGs may not exist in low-volatility synthetic data
        if snap is not None:
            assert 'bullish_count' in snap
            assert 'bearish_count' in snap


class TestNR7:
    def test_basic_output(self, uptrend):
        from carrac.charts.indicators_extended import _nr7
        snap = _nr7(uptrend)
        assert snap is not None
        assert isinstance(snap['active'], bool)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

class TestComputeIndicatorSnapshot:
    def test_returns_dict(self, uptrend):
        from carrac.charts.indicators_extended import compute_indicator_snapshot
        result = compute_indicator_snapshot(uptrend, tf='1D')
        assert isinstance(result, dict)
        # Should include at least the high-value subset
        assert 'boll' in result
        assert 'ttm_squeeze' in result
        assert 'psar' in result
        assert 'supertrend' in result
        assert 'ichimoku' in result
        assert 'obv' in result
        assert 'auto_fib' in result

    def test_skips_fast_oscillators_on_weekly(self, uptrend):
        from carrac.charts.indicators_extended import compute_indicator_snapshot
        result = compute_indicator_snapshot(uptrend, tf='1W')
        assert 'stoch' not in result
        assert 'stoch_rsi' not in result
        assert 'williams_r' not in result
        assert 'cci' not in result
        # Trend indicators should still be present
        assert 'psar' in result
        assert 'ichimoku' in result

    def test_daily_includes_all(self, uptrend):
        from carrac.charts.indicators_extended import compute_indicator_snapshot
        result = compute_indicator_snapshot(uptrend, tf='1D')
        assert 'stoch' in result
        assert 'williams_r' in result
        assert 'cci' in result

    def test_empty_df(self):
        from carrac.charts.indicators_extended import compute_indicator_snapshot
        df = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
        result = compute_indicator_snapshot(df, tf='1D')
        assert result == {}

    def test_short_df(self, short_df):
        from carrac.charts.indicators_extended import compute_indicator_snapshot
        result = compute_indicator_snapshot(short_df, tf='1D')
        # Most indicators should fail gracefully, psar might work
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Drawing serialization helpers
# ---------------------------------------------------------------------------

class TestDrawingSerializers:
    """Test the new drawing serialization helpers in drawings.py."""

    def test_describe_trendline_ascending(self):
        from carrac.drawings.routes import _describe_trendline
        import time
        now_ms = time.time() * 1000
        d = {
            'metadata': {
                'points': [
                    {'timestamp': now_ms - 86400000 * 30, 'value': 80000},
                    {'timestamp': now_ms - 86400000 * 15, 'value': 90000},
                ],
                'extend_data': {},
            }
        }
        with pytest.MonkeyPatch.context() as m:
            m.setattr('carrac.drawings.routes._get_asset_price', lambda a: 95000)
            desc = _describe_trendline(d, 'BTC', watched=False)
        assert desc is not None
        assert 'ascending' in desc
        assert 'WATCHED' not in desc
        assert 'Current line value' in desc

    def test_describe_trendline_watched(self):
        from carrac.drawings.routes import _describe_trendline
        import time
        now_ms = time.time() * 1000
        d = {
            'metadata': {
                'points': [
                    {'timestamp': now_ms - 86400000 * 30, 'value': 80000},
                    {'timestamp': now_ms - 86400000 * 15, 'value': 90000},
                ],
                'extend_data': {'watch': True},
            }
        }
        with pytest.MonkeyPatch.context() as m:
            m.setattr('carrac.drawings.routes._get_asset_price', lambda a: 95000)
            desc = _describe_trendline(d, 'BTC', watched=True)
        assert desc is not None
        assert 'WATCHED' in desc

    def test_describe_parallel_channel(self):
        from carrac.drawings.routes import _describe_parallel_channel
        import time
        now_ms = time.time() * 1000
        d = {
            'metadata': {
                'kline_name': 'parallelChannel',
                'points': [
                    {'timestamp': now_ms - 86400000 * 30, 'value': 80000},
                    {'timestamp': now_ms - 86400000 * 15, 'value': 90000},
                    {'timestamp': now_ms - 86400000 * 25, 'value': 75000},
                ],
            }
        }
        with pytest.MonkeyPatch.context() as m:
            m.setattr('carrac.drawings.routes._get_asset_price', lambda a: 92000)
            desc = _describe_parallel_channel(d, 'BTC')
        assert desc is not None
        assert 'parallel channel' in desc
        assert 'upper' in desc
        assert 'mid' in desc
        assert 'lower' in desc

    def test_describe_fibonacci(self):
        from carrac.drawings.routes import _describe_fibonacci
        d = {
            'metadata': {
                'kline_name': 'fibonacciRetrace',
                'points': [
                    {'value': 100000},
                    {'value': 60000},
                ],
            }
        }
        with pytest.MonkeyPatch.context() as m:
            m.setattr('carrac.drawings.routes._get_asset_price', lambda a: 82000)
            desc = _describe_fibonacci(d, 'BTC')
        assert desc is not None
        assert 'Fib retracement' in desc
        assert '61.8%' in desc
        assert '38.2%' in desc
        assert 'between' in desc

    def test_describe_pitchfork(self):
        from carrac.drawings.routes import _describe_pitchfork
        import time
        now_ms = time.time() * 1000
        d = {
            'metadata': {
                'kline_name': 'pitchfork',
                'points': [
                    {'timestamp': now_ms - 86400000 * 60, 'value': 70000},
                    {'timestamp': now_ms - 86400000 * 30, 'value': 90000},
                    {'timestamp': now_ms - 86400000 * 30, 'value': 80000},
                ],
            }
        }
        with pytest.MonkeyPatch.context() as m:
            m.setattr('carrac.drawings.routes._get_asset_price', lambda a: 88000)
            desc = _describe_pitchfork(d, 'BTC')
        assert desc is not None
        assert 'pitchfork' in desc
        assert 'median' in desc

    def test_trendline_insufficient_points(self):
        from carrac.drawings.routes import _describe_trendline
        d = {'metadata': {'points': [{'timestamp': 1, 'value': 100}]}}
        assert _describe_trendline(d, 'BTC') is None

    def test_fibonacci_equal_values(self):
        from carrac.drawings.routes import _describe_fibonacci
        d = {'metadata': {'points': [{'value': 100}, {'value': 100}]}}
        assert _describe_fibonacci(d, 'BTC') is None


# ---------------------------------------------------------------------------
# Signal extractor — extended indicator signals
# ---------------------------------------------------------------------------

class TestSignalExtractorExtended:
    """Test that signal extractor reads indicators_ext correctly."""

    def _make_chart_with_ext(self, ext_data):
        """Build mock chart response with indicators_ext."""
        return {
            'ohlcv': [
                {'timestamp': 1700000000 + i * 86400,
                 'open': 50000, 'high': 50500, 'low': 49500,
                 'close': 50000 + i * 10, 'volume': 1000}
                for i in range(100)
            ],
            'indicators': {
                'smaLines': {'sma20': [{'value': 49000}] * 100, 'sma200': [{'value': 45000}] * 100},
                'ema_ribbon': {'fast': [{'value': 50000}], 'slow': [{'value': 49000}], 'fill': [{'trend': 'BULL'}]},
            },
            'rsi': [{'value': 55}] * 100,
            'macd': {'line': [{'value': 100}] * 100, 'signal': [{'value': 90}] * 100, 'histogram': [{'value': 10}] * 100},
            'cpr': {'pivot': 50000, 'tc': 50500, 'bc': 49500},
            'volume': [{'value': 1000}] * 100,
            'indicators_ext': ext_data,
        }

    def test_ttm_squeeze_signal(self):
        from morning_engine.signal_extractor import _extract_asset_tf_signals
        data = self._make_chart_with_ext({'ttm_squeeze': {'active': True, 'bars': 5, 'momentum': 'bullish'}})
        signals = _extract_asset_tf_signals(data, 'BTC', '1D')
        assert signals['ttm_squeeze'] == 'active'
        assert signals['ttm_squeeze_bars'] == 5
        assert signals['ttm_squeeze_momentum'] == 'bullish'

    def test_psar_flip_signal(self):
        from morning_engine.signal_extractor import _extract_asset_tf_signals
        data = self._make_chart_with_ext({'psar': {'direction': 'long', 'value': 48000, 'flip': True}})
        signals = _extract_asset_tf_signals(data, 'BTC', '1D')
        assert signals['psar_direction'] == 'long'
        assert signals['psar_flip'] is True

    def test_ichimoku_signal(self):
        from morning_engine.signal_extractor import _extract_asset_tf_signals
        data = self._make_chart_with_ext({
            'ichimoku': {'cloud_position': 'above_bullish', 'cloud_color': 'bullish', 'tk_cross': 'bullish',
                         'tenkan': 50000, 'kijun': 49000, 'cloud_top': 48000, 'cloud_bottom': 46000}
        })
        signals = _extract_asset_tf_signals(data, 'BTC', '1D')
        assert signals['ichimoku_cloud'] == 'above_bullish'
        assert signals['ichimoku_tk_cross'] == 'bullish'

    def test_obv_divergence_signal(self):
        from morning_engine.signal_extractor import _extract_asset_tf_signals
        data = self._make_chart_with_ext({'obv': {'trend': 'falling', 'divergence': 'bearish'}})
        signals = _extract_asset_tf_signals(data, 'BTC', '1D')
        assert signals['obv_divergence'] == 'bearish'
        assert signals['obv_trend'] == 'falling'

    def test_neutral_signals_skipped(self):
        from morning_engine.signal_extractor import _extract_asset_tf_signals
        data = self._make_chart_with_ext({
            'stoch': {'k': 50, 'd': 50, 'zone': 'neutral'},
            'obv': {'trend': 'flat', 'divergence': 'none'},
        })
        signals = _extract_asset_tf_signals(data, 'BTC', '1D')
        assert 'stoch_zone' not in signals
        assert 'obv_divergence' not in signals
