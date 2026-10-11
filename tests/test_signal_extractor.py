"""
Unit tests for morning_engine/signal_extractor.py.

Covers: extract_all_signals, signal structure, market regime classification,
conflict detection, edge cases.
"""

import pytest


def _make_chart_response(price=50000, rsi=55, trend='UP', n_bars=100):
    """Build a mock chart API response dict matching /api/chart-internal format."""
    bars = []
    for i in range(n_bars):
        p = price + (i - n_bars // 2) * 10
        bars.append({
            'timestamp': 1700000000 + i * 86400,
            'open': p - 5, 'high': p + 20, 'low': p - 20,
            'close': p, 'volume': 1000 + i * 10,
        })

    return {
        'ohlcv': bars,
        'indicators': {
            'consensio': {
                '1D': {'signal': 2, 'position': 2},
                '3D': {'signal': 1, 'position': 1},
                '1W': {'signal': 1, 'position': 1},
                'total': 4,
            },
            'ema_ribbon_fill': [{'time': b['timestamp'], 'color': '#7cb518' if trend == 'UP' else '#f77f00'} for b in bars],
            'ema_ribbon_fast': [{'time': b['timestamp'], 'value': b['close'] + 100} for b in bars],
            'ema_ribbon_slow': [{'time': b['timestamp'], 'value': b['close'] - 100} for b in bars],
            'weekly_ma50': [{'time': b['timestamp'], 'value': b['close'] * 0.95} for b in bars],
            'weekly_ma100': [{'time': b['timestamp'], 'value': b['close'] * 0.90} for b in bars],
            'weekly_ma200': [{'time': b['timestamp'], 'value': b['close'] * 0.85} for b in bars],
            'bull_market_band': [{'time': b['timestamp'], 'value': b['close'] * 0.80} for b in bars],
        },
        'rsi': [{'time': b['timestamp'], 'value': rsi + (i % 10 - 5)} for i, b in enumerate(bars)],
        'macd': {
            'line': [{'value': 100 + i * 0.5} for i in range(n_bars)],
            'signal': [{'value': 95 + i * 0.5} for i in range(n_bars)],
            'histogram': [{'value': 5.0} for _ in range(n_bars)],
        },
        'cpr': {
            'pivot': price, 'tc': price + 500, 'bc': price - 500,
            'r1': price + 1000, 's1': price - 1000,
        },
        'volume': [{'time': b['timestamp'], 'value': b['volume']} for b in bars],
        'symbol': 'BTC',
        'timeframe': '1D',
    }


@pytest.fixture
def btc_chart():
    return _make_chart_response(50000, rsi=55, trend='UP')


@pytest.fixture
def spx_chart():
    return _make_chart_response(5000, rsi=60, trend='UP')


@pytest.fixture
def gold_chart():
    return _make_chart_response(2000, rsi=50, trend='UP')


class TestExtractAllSignals:
    """Test the main extract_all_signals function."""

    def test_basic_output_structure(self, btc_chart):
        from morning_engine.signal_extractor import extract_all_signals

        chart_raw = {('BTC', '1D'): btc_chart}
        result = extract_all_signals(chart_raw)

        assert 'per_asset' in result
        assert 'market_regime' in result
        assert 'signal_conflicts' in result
        assert 'summary_json' in result

    def test_per_asset_signal_keys(self, btc_chart):
        from morning_engine.signal_extractor import extract_all_signals

        chart_raw = {('BTC', '1D'): btc_chart}
        result = extract_all_signals(chart_raw)

        signals = result['per_asset']
        assert 'BTC' in signals
        btc_signal = signals['BTC']['1D']

        # Core keys every signal should have
        for key in ('asset', 'tf', 'status', 'price', 'rsi', 'rsi_zone',
                     'cpr_status', 'volume_signal'):
            assert key in btc_signal, f"Missing key: {key}"

    def test_rsi_zone_classification(self, btc_chart):
        from morning_engine.signal_extractor import extract_all_signals

        # Override RSI to oversold
        for bar in btc_chart['rsi']:
            bar['value'] = 25

        result = extract_all_signals({('BTC', '1D'): btc_chart})
        zone = result['per_asset']['BTC']['1D']['rsi_zone']
        assert zone == 'oversold'

    def test_rsi_overbought(self, btc_chart):
        from morning_engine.signal_extractor import extract_all_signals

        for bar in btc_chart['rsi']:
            bar['value'] = 75

        result = extract_all_signals({('BTC', '1D'): btc_chart})
        zone = result['per_asset']['BTC']['1D']['rsi_zone']
        assert zone == 'overbought'

    def test_multiple_assets(self, btc_chart, spx_chart):
        from morning_engine.signal_extractor import extract_all_signals

        chart_raw = {
            ('BTC', '1D'): btc_chart,
            ('SPX', '1D'): spx_chart,
        }
        result = extract_all_signals(chart_raw)

        assert 'BTC' in result['per_asset']
        assert 'SPX' in result['per_asset']

    def test_multiple_timeframes(self, btc_chart):
        from morning_engine.signal_extractor import extract_all_signals

        btc_weekly = _make_chart_response(50000, rsi=60, trend='UP')
        chart_raw = {
            ('BTC', '1D'): btc_chart,
            ('BTC', '1W'): btc_weekly,
        }
        result = extract_all_signals(chart_raw)

        assert '1D' in result['per_asset']['BTC']
        assert '1W' in result['per_asset']['BTC']

    def test_summary_json_is_compact(self, btc_chart):
        from morning_engine.signal_extractor import extract_all_signals
        import json

        result = extract_all_signals({('BTC', '1D'): btc_chart})
        summary = result['summary_json']

        # Should be valid JSON
        parsed = json.loads(summary)
        assert isinstance(parsed, dict)
        # Should be compact (no spaces)
        assert '  ' not in summary

    def test_market_regime_valid(self, btc_chart, spx_chart):
        from morning_engine.signal_extractor import extract_all_signals

        result = extract_all_signals({
            ('BTC', '1D'): btc_chart,
            ('SPX', '1D'): spx_chart,
        })

        assert result['market_regime'] in (
            'risk_on', 'risk_off', 'commodity_rotation',
            'stagflation', 'transitional'
        )

    def test_signal_conflicts_is_list(self, btc_chart):
        from morning_engine.signal_extractor import extract_all_signals

        result = extract_all_signals({('BTC', '1D'): btc_chart})
        assert isinstance(result['signal_conflicts'], list)

    def test_empty_chart_data(self):
        from morning_engine.signal_extractor import extract_all_signals

        result = extract_all_signals({})
        assert result['per_asset'] == {}
        assert result['market_regime'] in (
            'risk_on', 'risk_off', 'commodity_rotation',
            'stagflation', 'transitional'
        )

    def test_missing_indicators_graceful(self):
        from morning_engine.signal_extractor import extract_all_signals

        minimal = {
            'ohlcv': [{'timestamp': 1700000000, 'open': 100, 'high': 110,
                        'low': 90, 'close': 105, 'volume': 1000}],
            'indicators': {},
            'rsi': [],
            'macd': {},
            'cpr': {},
            'volume': [],
            'symbol': 'TEST',
            'timeframe': '1D',
        }
        # Should not raise
        result = extract_all_signals({('TEST', '1D'): minimal})
        assert 'TEST' in result['per_asset']

    def test_volume_signal_classification(self, btc_chart):
        from morning_engine.signal_extractor import extract_all_signals

        result = extract_all_signals({('BTC', '1D'): btc_chart})
        vol = result['per_asset']['BTC']['1D']['volume_signal']
        assert vol in ('high', 'low', 'normal')

    def test_cpr_status_values(self, btc_chart):
        from morning_engine.signal_extractor import extract_all_signals

        result = extract_all_signals({('BTC', '1D'): btc_chart})
        cpr = result['per_asset']['BTC']['1D']['cpr_status']
        assert cpr in ('above_tc', 'below_bc', 'inside')


class TestRangePositionNotInPrompt:
    """The model SOURCE must carry exactly ONE range metric.

    `range_position` is a 20-bar high/low position. The Market Snapshot
    "Range" column is a 90-day close percentile supplied separately by
    morning_engine.data_assembly.format_compact_asset_lines. When both were
    in the prompt the model copied the 20-bar one: a published table printed
    DXY 9% against a grounded 43%, and most assets mismatched.

    This is a *containment* test, not a behaviour test — the failure it
    guards has no local symptom. Nothing crashes when both numbers are in
    the prompt; a human just reads a wrong percentage on the public brief.
    """

    def _summary_for(self, primary_signals):
        from morning_engine.signal_extractor import _build_signal_summary
        from morning_engine.signal_extractor import PRIMARY_TF
        all_signals = {"DXY": {PRIMARY_TF: primary_signals}}
        return _build_signal_summary(all_signals, "risk_on", {}, [])

    def test_range_position_is_never_emitted(self):
        summary = self._summary_for({
            "status": "ok",
            "ma_stack": "bullish_partial",
            "range_position": 9.0,
        })
        assert "range_position" not in summary["asset_signals"]["DXY"], (
            "range_position leaked back into the model SOURCE — the report "
            "will copy the 20-bar figure into the 90-day Range column again"
        )

    def test_the_serialised_prompt_contains_no_range_position(self):
        """Belt and braces: the leak that mattered was in the JSON text.

        Asserting on the dict alone would miss a reintroduction under a
        nested key, which is how it reached the prompt the first time.
        """
        import json
        summary = self._summary_for({
            "status": "ok",
            "ma_stack": "bullish_partial",
            "range_position": 9.0,
        })
        assert "range_position" not in json.dumps(summary)

    def test_the_other_signals_still_survive(self):
        """The fix must remove ONE key, not quietly empty the block."""
        summary = self._summary_for({
            "status": "ok",
            "ma_stack": "bullish_partial",
            "rsi": 36.1,
            "bar_momentum_5": -0.05,
            "range_position": 9.0,
        })
        dxy = summary["asset_signals"]["DXY"]
        assert dxy["ma_stack"] == "bullish_partial"
        assert dxy["rsi"] == 36.1
        assert dxy["bar_momentum_5"] == -0.05
