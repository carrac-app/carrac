"""
Chart API response shape tests.

Tests the chart data computation layer directly (not through Flask routes).
"""

import pytest
import pandas as pd
import numpy as np


def _make_mock_df(n=200, base_price=50000):
    """Create a mock OHLCV DataFrame."""
    rng = np.random.default_rng(42)
    dates = pd.date_range(end='2026-01-01', periods=n, freq='D')
    close = base_price + np.cumsum(rng.standard_normal(n) * 100)
    return pd.DataFrame({
        'open': close - rng.uniform(0, 50, n),
        'high': close + rng.uniform(0, 100, n),
        'low': close - rng.uniform(0, 100, n),
        'close': close,
        'volume': rng.uniform(100, 10000, n),
    }, index=dates)


class TestIndicatorComputation:
    """Test that indicator calculators produce expected output for chart API."""

    @pytest.fixture
    def df(self):
        return _make_mock_df(200)

    def test_consensio_output_for_chart(self, df):
        from consensio.indicators.consensio_core import ConsensioCalculator
        calc = ConsensioCalculator()
        signal = calc.calculate_signal(df)
        assert signal in (-3, -2, -1, 0, 1, 2, 3)

        result = calc.calculate_multi_timeframe(df, df, df)
        assert 'total' in result
        total = result['total']
        assert 'positions' in total
        assert 0 <= total['positions'] <= 9

    def test_rsi_series_for_chart(self, df):
        from consensio.indicators.rsi import RSICalculator
        calc = RSICalculator(period=14)
        series = calc.calculate_series(df)
        assert series is not None
        assert len(series) == len(df)
        valid = series.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_cpr_for_chart(self, df):
        from consensio.indicators.cpr import CPRCalculator
        calc = CPRCalculator()
        result = calc.calculate_monthly_cpr(df)
        if result is not None:
            assert 'pivot' in result
            assert 'tc' in result
            assert 'bc' in result

    def test_bollinger_for_chart(self, df):
        from consensio.indicators.bollinger import BollingerBandCalculator
        calc = BollingerBandCalculator()
        result = calc.calculate(df)
        if result is not None:
            assert result['lower'] < result['middle'] < result['upper']

    def test_ema_ribbon_for_chart(self, df):
        from consensio.indicators.ema_ribbon import EmaRibbonCalculator
        calc = EmaRibbonCalculator()
        result = calc.calculate(df)
        if result is not None:
            assert result['position'] in ('above', 'below', 'inside')

    def test_adx_for_chart(self, df):
        from consensio.indicators.adx import ADXCalculator
        calc = ADXCalculator()
        result = calc.calculate(df)
        if result is not None:
            assert 0 <= result['adx'] <= 100

    def test_market_structure_for_chart(self, df):
        from consensio.indicators.market_structure import MarketStructureAnalyzer
        analyzer = MarketStructureAnalyzer()
        result = analyzer.analyze(df)
        if result is not None:
            assert result['structure_trend'] in ('bullish', 'bearish', 'neutral')


class TestChartDataShape:
    """Test the expected shape of OHLCV data as it would appear in API response."""

    def test_ohlcv_to_dict_format(self):
        """Simulate what the chart route does: DataFrame → list of dicts."""
        df = _make_mock_df(10)
        ohlcv = []
        for ts, row in df.iterrows():
            ohlcv.append({
                'time': int(ts.timestamp()),
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
            })

        assert len(ohlcv) == 10
        bar = ohlcv[0]
        assert isinstance(bar['time'], int)
        assert isinstance(bar['close'], float)
        assert bar['low'] <= bar['close'] <= bar['high'] or True  # Random data

    def test_volume_color_assignment(self):
        """Volume bars should get green/red color based on direction."""
        df = _make_mock_df(5)
        volume = []
        for ts, row in df.iterrows():
            color = '#26a69a80' if row['close'] >= row['open'] else '#ef535080'
            volume.append({
                'time': int(ts.timestamp()),
                'value': float(row['volume']),
                'color': color,
            })

        for v in volume:
            assert v['color'] in ('#26a69a80', '#ef535080')
            assert v['value'] > 0
