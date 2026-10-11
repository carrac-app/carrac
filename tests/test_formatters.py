"""Tests for carrac.formatters — compact response formatters for LLM/MCP."""

import json

import pytest

from carrac.formatters import (
    compact_chart,
    compact_scanner,
    evidence_scanner,
    compact_sentiment,
    compact_stream,
    compact_report,
    compact_plays,
    maybe_compact,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal realistic response dicts
# ---------------------------------------------------------------------------

@pytest.fixture
def chart_data():
    return {
        'symbol': 'BTC',
        'timeframe': '1D',
        'ohlcv': [
            {'time': 1, 'open': 82000, 'high': 83000, 'low': 81000, 'close': 82500},
            {'time': 2, 'open': 82500, 'high': 85000, 'low': 82000, 'close': 84250},
        ],
        'volume': [{'time': 1, 'value': 1000}, {'time': 2, 'value': 1200}],
        'rsi': [{'time': 1, 'value': 55.3}, {'time': 2, 'value': 62.1}],
        'markers': [{'time': 2, 'position': 'belowBar', 'shape': 'circle'}],
        'priceLines': [{'price': 80000, 'title': 'Support'}],
        'indicators': {
            'consensio': {
                '1D': {'signal': 'bullish', 'positions': 2},
                '3D': {'signal': 'bearish', 'positions': -1},
                '1W': {'signal': 'bullish', 'positions': 1},
                'total': 2,
            },
        },
        'cpr': {
            'pivot': 83000, 'tc': 83500, 'bc': 82500,
            'r1': 84000, 'r2': 85000, 'r3': 86000,
            's1': 82000, 's2': 81000, 's3': 80000,
            'history': [{'pivot': 82000}] * 48,
        },
        'weekly_cpr': {
            'pivot': 83200, 'r1': 84200, 's1': 82200,
            'history': [{'pivot': 83000}] * 52,
        },
        'daily_cpr': {
            'pivot': 83500, 'r1': 84500, 's1': 82800,
            'history': [{'pivot': 83400}] * 60,
        },
        'indicators_ext': {'adx': 28, 'atr': 500},
        'tier': 'curated',
        'live_ws': True,
        '_server_ts': 1234567890,
    }


@pytest.fixture
def scanner_data():
    return {
        'results': [
            {
                'symbol': 'BTC',
                'price': 84250,
                'best_verdict': 'STRONG',
                'long': {
                    'total': 10,
                    'verdict': 'STRONG',
                    'trend': 2, 'momentum': 2, 'rsi': 1, 'structure': 2, 'level': 2, 'macro': 1,
                    'confluences': {'all': [{'price': 82000, 'label': 'S1+VP', 'sources': ['S1', 'VP'], 'strength': 3}]},
                },
                'short': {
                    'total': 3,
                    'verdict': 'AVOID',
                    'trend': 0, 'momentum': 1, 'rsi': 1, 'structure': 0, 'level': 1, 'macro': 0,
                },
            },
            {
                'symbol': 'ETH',
                'price': 3400,
                'best_verdict': 'GOOD',
                'long': {'total': 8, 'verdict': 'GOOD'},
                'short': {'total': 4, 'verdict': 'WEAK'},
            },
        ],
        'macro': {'fear_greed': 62, 'regime': 'bull'},
        'computed_at': '2026-04-04T12:00:00Z',
    }


@pytest.fixture
def sentiment_data():
    return {
        'status': 'ok',
        'timestamp': '2026-04-04T12:00:00Z',
        'sentiment': {
            'fear_greed': {'value': 62, 'classification': 'Greed', 'color': '#3fb950'},
            'long_short': {
                'BTC': {'ratio': 0.95, 'longPct': 48.5, 'shortPct': 51.5, 'color': '#f85149'},
                'ETH': {'ratio': 1.05, 'longPct': 51.2, 'shortPct': 48.8, 'color': '#3fb950'},
            },
            'alt_season': {'index': 72, 'season': 'active', 'outperformers': 45, 'color': '#3fb950'},
            'mvrv_zscore': {'zscore': 2.3, 'zone': 'caution', 'color': '#f0883e'},
            'funding_rate': {'rate': 0.000125, 'rate_pct': 0.0125, 'color': '#adb5bd'},
            'open_interest': {'value': 5.2, 'value_usd': 495_000_000_000, 'change_pct': 2.5, 'color': '#adb5bd'},
            'regime': {'regime': 'bull', 'score': 8.2, 'color': '#3fb950', 'components': [{'name': 'MA', 'value': 1}]},
            'fred': {'unemployment': 3.8, 'inflation': 2.4},
        },
    }


@pytest.fixture
def stream_data():
    return {
        'entries': [
            {
                'symbol': 'BTC',
                'price': 84250,
                'zones': [
                    {'price': 82000, 'label': 'S1+VP', 'sources': ['S1', 'VP'], 'strength': 8, 'side': 'support', 'count': 2, 'tf': 'D'},
                    {'price': 81000, 'label': 'S2', 'sources': ['S2'], 'strength': 5, 'side': 'support', 'count': 1, 'tf': 'D'},
                    {'price': 80000, 'label': 'S3+BB_L', 'sources': ['S3', 'BB_L'], 'strength': 7, 'side': 'support', 'count': 2, 'tf': 'W'},
                    {'price': 79000, 'label': 'PWL', 'sources': ['PWL'], 'strength': 4, 'side': 'support', 'count': 1, 'tf': 'W'},
                    {'price': 86000, 'label': 'R1+BB_U', 'sources': ['R1', 'BB_U'], 'strength': 9, 'side': 'resistance', 'count': 2, 'tf': 'D'},
                    {'price': 88000, 'label': 'R2', 'sources': ['R2'], 'strength': 6, 'side': 'resistance', 'count': 1, 'tf': 'D'},
                ],
                'bb_squeeze_pct': 3.2,
                'pdh': 84800,
                'pdl': 82100,
                'signals': [
                    {'tf': 'D', 'type': 'bullish_engulfing', 'direction': 'long', 'strength': 8},
                    {'tf': '1h', 'type': 'bearish_engulfing', 'direction': 'short', 'strength': 5},
                ],
                'transitions': [
                    {'symbol': 'BTC', 'indicator': 'EMA_RIBBON', 'from': 'bearish', 'to': 'bullish', 'significance': 5},
                ],
            },
        ],
        'computed_at': '2026-04-04T12:00:00Z',
    }


@pytest.fixture
def report_data():
    return {
        'id': 89,
        'created_at': '2026-04-04T04:45:00Z',
        'content': '<h2>Market Overview</h2><p>Long HTML content...</p>' * 100,
        'summary': 'BTC holding above CPR, macro supportive',
        'audio_script': '**Key Point 1**: BTC above monthly CPR.\n**Key Point 2**: Risk-on regime persists.',
        'metadata': {'report_prices': {'BTC': 84000}},
    }


@pytest.fixture
def plays_data():
    return {
        'plays': [
            {
                'id': 15,
                'title': 'BTC long on pullback to CPR',
                'thesis': 'Monthly CPR support with aligned consensio should hold.',
                'type': 'directional',
                'approach': 'scaled',
                'direction': 'long',
                'primary_asset': 'BTC',
                'related_assets': ['ETH', 'SOL'],
                'status': 'watching',
                'conditions_met': '3/5',
                'conditions_logic': 'all',
                'conditions_detail': [
                    {'type': 'price_level', 'met': True, 'evidence': 'Price 84250 > 82000', 'label': 'Price above $82,000'},
                    {'type': 'indicator_state', 'met': True, 'evidence': 'EMA_RIBBON bullish on 1D TF', 'label': 'EMA Ribbon bullish (1D)'},
                    {'type': 'consensio_threshold', 'met': False, 'evidence': 'Positions: 2, need: 4', 'label': 'Consensio >= 4'},
                ],
                'invalidation': None,
                'action_plan': 'Scale in at 82500, 82000, 81500 with 1% risk per tranche',
                'expiry': '2026-04-15T00:00:00Z',
                'timeframe': 'swing',
            },
        ],
        'summary': '1 watching',
    }


# ---------------------------------------------------------------------------
# Unit tests — compactor functions
# ---------------------------------------------------------------------------

class TestCompactChart:
    def test_extracts_price_and_change(self, chart_data):
        result = compact_chart(chart_data)
        assert result['symbol'] == 'BTC'
        assert result['timeframe'] == '1D'
        assert result['price'] == 84250
        assert result['change_pct'] == pytest.approx(2.12, abs=0.01)

    def test_drops_ohlcv_arrays(self, chart_data):
        result = compact_chart(chart_data)
        assert 'ohlcv' not in result
        assert 'volume' not in result
        assert 'markers' not in result
        assert 'priceLines' not in result

    def test_extracts_rsi(self, chart_data):
        result = compact_chart(chart_data)
        assert result['rsi'] == 62.1

    def test_extracts_consensio(self, chart_data):
        result = compact_chart(chart_data)
        con = result['consensio']
        assert con['1d'] == 2
        assert con['3d'] == -1
        assert con['1w'] == 1
        assert con['total'] == 2

    def test_extracts_key_levels(self, chart_data):
        result = compact_chart(chart_data)
        kl = result['key_levels']
        assert kl['monthly_pivot'] == 83000
        assert kl['weekly_pivot'] == 83200
        assert kl['daily_pivot'] == 83500

    def test_drops_cpr_history(self, chart_data):
        result = compact_chart(chart_data)
        assert 'cpr' not in result
        assert 'weekly_cpr' not in result
        assert 'daily_cpr' not in result

    def test_keeps_indicators_ext(self, chart_data):
        result = compact_chart(chart_data)
        assert result['indicators_ext'] == {'adx': 28, 'atr': 500}

    def test_keeps_candle_count(self, chart_data):
        result = compact_chart(chart_data)
        assert result['candle_count'] == 2

    def test_handles_empty_data(self):
        result = compact_chart({})
        assert result['symbol'] is None
        assert result['price'] is None
        assert result['rsi'] is None


class TestCompactScanner:
    def test_keeps_scores_and_verdicts(self, scanner_data):
        result = compact_scanner(scanner_data)
        assert len(result['results']) == 2
        btc = result['results'][0]
        assert btc['symbol'] == 'BTC'
        assert btc['long_score'] == 10
        assert btc['long_verdict'] == 'STRONG'
        assert btc['short_score'] == 3
        assert btc['short_verdict'] == 'AVOID'

    def test_drops_criteria_detail(self, scanner_data):
        result = compact_scanner(scanner_data)
        btc = result['results'][0]
        assert 'trend' not in btc
        assert 'momentum' not in btc
        assert 'confluences' not in btc

    def test_keeps_macro(self, scanner_data):
        result = compact_scanner(scanner_data)
        assert result['macro']['fear_greed'] == 62


class TestEvidenceScanner:
    """The LLM-facing surface reports observations + evidence, never
    verdicts, position vocabulary, or the internal trade_plan."""

    @pytest.fixture
    def engine_data(self):
        """Mirrors the real engine shape: criteria = {name: {score, evidence}}."""
        return {
            'results': [
                {
                    'symbol': 'COPPER',
                    'price': 6.33,
                    'best_verdict': 'GOOD',
                    'long': {
                        'total': 9,
                        'verdict': 'GOOD',
                        'criteria': {
                            'trend': {'score': 2, 'evidence': 'EMA ribbon stacked, 9>21>55'},
                            'level': {'score': 2, 'evidence': 'price above monthly BC'},
                            'macro': {'score': 0, 'evidence': ''},
                        },
                        'vetoes': ['stretched vs 21EMA'],
                        'trade_plan': {'entry': 6.20, 'stop': 6.05, 'target': 6.90},
                    },
                    'short': {
                        'total': 3,
                        'verdict': 'AVOID',
                        'criteria': {
                            'trend': {'score': 0, 'evidence': 'ribbon opposes'},
                        },
                        'trade_plan': {'entry': 6.40, 'stop': 6.55},
                    },
                },
            ],
            'macro': {'fear_greed': 27},
            'computed_at': '2026-07-25T11:55:50Z',
        }

    def test_reports_conditions_and_evidence(self, engine_data):
        result = evidence_scanner(engine_data)
        row = result['results'][0]
        assert row['symbol'] == 'COPPER'
        assert row['bullish']['conditions_met'] == 9
        assert row['bullish']['conditions_total'] == 12
        assert row['bullish']['evidence']['trend'] == 'EMA ribbon stacked, 9>21>55'
        assert row['bearish']['conditions_met'] == 3

    def test_never_leaks_trade_plan(self, engine_data):
        """trade_plan is INTERNAL ONLY — brand rule, must never surface."""
        assert 'trade_plan' not in json.dumps(evidence_scanner(engine_data))

    def test_drops_verdict_and_position_vocabulary(self, engine_data):
        blob = json.dumps(evidence_scanner(engine_data))
        for retired in ('verdict', 'best_verdict', 'long_score', 'short_score'):
            assert retired not in blob
        # "long"/"short" describe a position; the surface uses bullish/bearish.
        row = evidence_scanner(engine_data)['results'][0]
        assert 'long' not in row and 'short' not in row

    def test_empty_evidence_strings_are_omitted(self, engine_data):
        row = evidence_scanner(engine_data)['results'][0]
        assert 'macro' not in row['bullish']['evidence']

    def test_include_evidence_false_keeps_counts_only(self, engine_data):
        row = evidence_scanner(engine_data, include_evidence=False)['results'][0]
        assert row['bullish']['conditions_met'] == 9
        assert 'evidence' not in row['bullish']
        assert 'vetoes' not in row['bullish']

    def test_preserves_pagination_counters(self, engine_data):
        engine_data['result_count'] = 1
        engine_data['total_available'] = 337
        result = evidence_scanner(engine_data, include_evidence=False)
        # compact_scanner dropped these, which is why market_get_overview's
        # documented result_count/total_available never appeared.
        assert result['result_count'] == 1
        assert result['total_available'] == 337

    def test_handles_missing_sides(self):
        result = evidence_scanner({'results': [{'symbol': 'X', 'price': 1}]})
        row = result['results'][0]
        assert row['bullish']['conditions_met'] is None
        assert row['bearish']['conditions_total'] == 12


class TestCompactSentiment:
    def test_flattens_sentiment(self, sentiment_data):
        result = compact_sentiment(sentiment_data)
        s = result['sentiment']
        assert s['fear_greed'] == 62
        assert s['fear_greed_label'] == 'Greed'
        assert s['funding_rate_pct'] == 0.0125
        assert s['mvrv_zscore'] == 2.3
        assert s['mvrv_zone'] == 'caution'
        assert s['alt_season_index'] == 72
        assert s['regime'] == 'bull'
        assert s['regime_score'] == 8.2

    def test_drops_colors(self, sentiment_data):
        result = compact_sentiment(sentiment_data)
        s = result['sentiment']
        assert 'color' not in str(s)

    def test_flattens_long_short_to_ratios(self, sentiment_data):
        result = compact_sentiment(sentiment_data)
        ls = result['sentiment']['long_short']
        assert ls['BTC'] == 0.95
        assert ls['ETH'] == 1.05

    def test_drops_fred(self, sentiment_data):
        result = compact_sentiment(sentiment_data)
        assert 'fred' not in result['sentiment']


class TestCompactStream:
    def test_top_3_per_side(self, stream_data):
        result = compact_stream(stream_data)
        entry = result['entries'][0]
        assert entry['symbol'] == 'BTC'
        assert len(entry['supports']) == 3
        assert len(entry['resistances']) == 2  # only 2 exist

    def test_sorted_by_strength(self, stream_data):
        result = compact_stream(stream_data)
        supports = result['entries'][0]['supports']
        strengths = [s['strength'] for s in supports]
        assert strengths == sorted(strengths, reverse=True)

    def test_drops_bb_squeeze(self, stream_data):
        result = compact_stream(stream_data)
        entry = result['entries'][0]
        assert 'bb_squeeze_pct' not in entry
        assert 'pdh' not in entry

    def test_compacts_signals(self, stream_data):
        result = compact_stream(stream_data)
        signals = result['entries'][0]['signals']
        assert len(signals) == 2
        assert signals[0] == {'tf': 'D', 'type': 'bullish_engulfing', 'direction': 'long'}


class TestCompactReport:
    def test_keeps_summary(self, report_data):
        result = compact_report(report_data)
        assert result['id'] == 89
        assert result['summary'] == 'BTC holding above CPR, macro supportive'
        assert 'Key Point 1' in result['audio_script']

    def test_drops_html_content(self, report_data):
        result = compact_report(report_data)
        assert 'content' not in result
        assert 'metadata' not in result


class TestCompactPlays:
    def test_keeps_essential_fields(self, plays_data):
        result = compact_plays(plays_data)
        play = result['plays'][0]
        assert play['id'] == 15
        assert play['title'] == 'BTC long on pullback to CPR'
        assert play['status'] == 'watching'
        assert play['conditions_met'] == '3/5'

    def test_drops_thesis_and_action_plan(self, plays_data):
        result = compact_plays(plays_data)
        play = result['plays'][0]
        assert 'thesis' not in play
        assert 'action_plan' not in play
        assert 'approach' not in play

    def test_compacts_conditions(self, plays_data):
        result = compact_plays(plays_data)
        conditions = result['plays'][0]['conditions']
        assert len(conditions) == 3
        assert conditions[0]['type'] == 'price_level'
        assert conditions[0]['met'] is True
        assert 'evidence' not in conditions[0]

    def test_keeps_summary(self, plays_data):
        result = compact_plays(plays_data)
        assert result['summary'] == '1 watching'


# ---------------------------------------------------------------------------
# maybe_compact helper
# ---------------------------------------------------------------------------

class TestMaybeCompact:
    @pytest.fixture
    def mini_app(self):
        from flask import Flask
        return Flask(__name__)

    def test_returns_original_when_not_compact(self, mini_app):
        with mini_app.test_request_context('/'):
            data = {'foo': 'bar', 'ohlcv': [1, 2, 3]}
            result = maybe_compact(data, lambda d: {'compact': True})
            assert result == data

    def test_applies_compactor_when_compact(self, mini_app):
        with mini_app.test_request_context('/?format=compact'):
            data = {'foo': 'bar'}
            result = maybe_compact(data, lambda d: {'compact': True})
            assert result == {'compact': True}

    def test_not_applied_for_other_formats(self, mini_app):
        with mini_app.test_request_context('/?format=json'):
            data = {'foo': 'bar'}
            result = maybe_compact(data, lambda d: {'compact': True})
            assert result == data
