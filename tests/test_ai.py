"""
Tests for the AI assistant module.

Covers:
- Billing: sanitize_for_llm, calculate_cost_cents, reserve/settle
- Features: system prompt building, feature config
- Context builder: shared context, viewport context, asset list, scenarios
- Pattern matching: normalization, correlation, overlap removal
- Trade journal: CRUD, stats, ownership
- Routes: /api/ai/balance, /api/ai/assets, /api/ai/chat, /api/ai/pattern-match, /api/ai/journal
"""

import os
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ========== Billing Tests ==========

class TestBilling:
    """Tests for carrac/ai/billing.py"""

    def test_sanitize_for_llm_normal(self):
        from carrac.ai.billing import sanitize_for_llm
        assert sanitize_for_llm("Hello world") == "Hello world"

    def test_sanitize_for_llm_strips_control_chars(self):
        from carrac.ai.billing import sanitize_for_llm
        result = sanitize_for_llm("Hello\x00\x01\x7fworld")
        assert "\x00" not in result
        assert "\x01" not in result
        assert "\x7f" not in result
        assert "Helloworld" == result

    def test_sanitize_for_llm_preserves_newlines_tabs(self):
        from carrac.ai.billing import sanitize_for_llm
        result = sanitize_for_llm("line1\nline2\ttab")
        assert "line1\nline2\ttab" == result

    def test_sanitize_for_llm_max_length(self):
        from carrac.ai.billing import sanitize_for_llm
        long_text = "a" * 2000
        result = sanitize_for_llm(long_text, max_length=100)
        # Hardened version keeps the first `max_length` chars then appends a
        # "[...truncated]" marker. Assert both halves.
        assert result.startswith("a" * 100)
        assert "[...truncated]" in result

    def test_sanitize_for_llm_nfkc_normalize(self):
        from carrac.ai.billing import sanitize_for_llm
        # Fullwidth A should normalize to regular A
        result = sanitize_for_llm("\uff21BC")
        assert result == "ABC"

    def test_sanitize_for_llm_non_string(self):
        from carrac.ai.billing import sanitize_for_llm
        assert sanitize_for_llm(None) == ""
        assert sanitize_for_llm(123) == ""

    def test_sanitize_for_llm_empty(self):
        from carrac.ai.billing import sanitize_for_llm
        assert sanitize_for_llm("   ") == ""

    def test_calculate_cost_cents(self):
        """Cost calculation with default markup rates."""
        from flask import Flask
        app = Flask(__name__)
        app.config['AI_INPUT_RATE_PER_M'] = 16.0
        app.config['AI_OUTPUT_RATE_PER_M'] = 80.0
        with app.app_context():
            from carrac.ai.billing import calculate_cost_cents
            # 1000 input + 500 output
            # cost = (1000 * 16 + 500 * 80) / 1_000_000 = 0.056 USD = 5.6 cents → 6
            cost = calculate_cost_cents(1000, 500)
            assert cost == 6

    def test_calculate_cost_cents_minimum(self):
        """Minimum cost is 1 cent."""
        from flask import Flask
        app = Flask(__name__)
        app.config['AI_INPUT_RATE_PER_M'] = 16.0
        app.config['AI_OUTPUT_RATE_PER_M'] = 80.0
        with app.app_context():
            from carrac.ai.billing import calculate_cost_cents
            cost = calculate_cost_cents(1, 1)
            assert cost == 1  # minimum 1 cent

    def test_calculate_cost_cents_large_output(self):
        """Typical query: ~2000 input, ~1000 output."""
        from flask import Flask
        app = Flask(__name__)
        app.config['AI_INPUT_RATE_PER_M'] = 16.0
        app.config['AI_OUTPUT_RATE_PER_M'] = 80.0
        with app.app_context():
            from carrac.ai.billing import calculate_cost_cents
            # (2000 * 16 + 1000 * 80) / 1e6 = 0.112 USD = 11.2c → 11
            cost = calculate_cost_cents(2000, 1000)
            assert cost == 11


# ========== Features Tests ==========

class TestFeatures:
    """Tests for carrac/ai/features.py"""

    def test_all_features_defined(self):
        from carrac.ai.features import FEATURES
        expected = {'chat', 'viewport', 'validate', 'challenge', 'checklist', 'confluence', 'trade_design', 'report_followup', 'news_analysis'}
        assert set(FEATURES.keys()) == expected

    def test_feature_has_required_keys(self):
        from carrac.ai.features import FEATURES
        for name, feat in FEATURES.items():
            assert 'name' in feat, f"{name} missing 'name'"
            assert 'system_instruction' in feat, f"{name} missing 'system_instruction'"
            assert 'requires_asset' in feat, f"{name} missing 'requires_asset'"
            assert 'requires_scenario' in feat, f"{name} missing 'requires_scenario'"
            assert 'max_output_tokens' in feat, f"{name} missing 'max_output_tokens'"

    def test_chat_does_not_require_asset(self):
        from carrac.ai.features import FEATURES
        assert FEATURES['chat']['requires_asset'] is False

    def test_validate_requires_scenario(self):
        from carrac.ai.features import FEATURES
        assert FEATURES['validate']['requires_scenario'] is True
        assert FEATURES['challenge']['requires_scenario'] is True
        assert FEATURES['checklist']['requires_scenario'] is True

    def test_confluence_does_not_require_scenario(self):
        from carrac.ai.features import FEATURES
        assert FEATURES['confluence']['requires_scenario'] is False

    def test_build_system_prompt_basic(self):
        from carrac.ai.features import build_system_prompt
        prompt = build_system_prompt('chat')
        assert 'Carrac AI' in prompt
        assert len(prompt) > 100

    def test_build_system_prompt_with_context(self):
        from carrac.ai.features import build_system_prompt
        prompt = build_system_prompt(
            'viewport',
            shared_context='BTC: $70,000',
            viewport_context='RSI: 45',
        )
        assert 'BTC: $70,000' in prompt
        assert 'RSI: 45' in prompt
        assert 'CURRENT MARKET STATE' in prompt
        assert 'ASSET DATA' in prompt

    def test_build_system_prompt_with_scenario(self):
        from carrac.ai.features import build_system_prompt
        prompt = build_system_prompt(
            'validate',
            scenario_context='Long BTC @ $65,000',
        )
        assert 'Long BTC @ $65,000' in prompt
        assert "USER'S SCENARIO" in prompt

    def test_get_max_tokens(self):
        from carrac.ai.features import get_max_tokens, FEATURES
        assert get_max_tokens('chat') == FEATURES['chat']['max_output_tokens']
        assert get_max_tokens('checklist') == FEATURES['checklist']['max_output_tokens']
        assert get_max_tokens('validate') == FEATURES['validate']['max_output_tokens']
        assert get_max_tokens('nonexistent') == FEATURES['chat']['max_output_tokens']

    def test_get_feature_config(self):
        from carrac.ai.features import get_feature_config, FEATURES
        cfg = get_feature_config('viewport')
        assert cfg['name'] == FEATURES['viewport']['name']
        assert cfg['requires_asset'] is True


# ========== Pattern Match Tests ==========

class TestPatternMatch:
    """Tests for carrac/ai/pattern_match.py"""

    def test_normalize_shape_basic(self):
        from carrac.ai.pattern_match import _normalize_shape
        prices = np.array([100.0, 102.0, 104.0, 103.0, 105.0])
        result = _normalize_shape(prices)
        assert result is not None
        assert len(result) == 5  # Same length as input
        assert abs(np.mean(result)) < 1e-10  # zero mean
        assert abs(np.std(result) - 1.0) < 1e-10  # unit std

    def test_normalize_shape_flat(self):
        from carrac.ai.pattern_match import _normalize_shape
        prices = np.array([100.0, 100.0, 100.0, 100.0])
        result = _normalize_shape(prices)
        assert result is None  # zero range → None

    def test_normalize_shape_too_short(self):
        from carrac.ai.pattern_match import _normalize_shape
        result = _normalize_shape(np.array([100.0]))
        assert result is None

    def test_pearson_identical(self):
        from carrac.ai.pattern_match import _pearson
        a = np.array([1.0, 2.0, 3.0, 4.0])
        r = _pearson(a, a)
        assert abs(r - np.dot(a, a) / len(a)) < 1e-10

    def test_pearson_proportional(self):
        from carrac.ai.pattern_match import _pearson, _normalize_shape
        prices_a = np.array([100.0, 102.0, 104.0, 103.0, 105.0])
        prices_b = np.array([200.0, 204.0, 208.0, 206.0, 210.0])
        a = _normalize_shape(prices_a)
        b = _normalize_shape(prices_b)
        # Proportional prices → identical shape → perfect correlation
        r = _pearson(a, b)
        assert r > 0.99

    def test_remove_overlapping(self):
        from carrac.ai.pattern_match import _remove_overlapping
        candidates = [
            {'index': 0, 'correlation': 0.9},
            {'index': 5, 'correlation': 0.85},   # overlaps with 0 if window=10
            {'index': 15, 'correlation': 0.8},    # no overlap
        ]
        result = _remove_overlapping(candidates, window_size=10)
        assert len(result) == 2
        assert result[0]['correlation'] == 0.9
        assert result[1]['correlation'] == 0.8

    def test_remove_overlapping_empty(self):
        from carrac.ai.pattern_match import _remove_overlapping
        assert _remove_overlapping([], 10) == []

    def test_find_similar_patterns_insufficient_data(self):
        """Pattern match with no data returns empty matches."""
        with patch('carrac.ai.pattern_match._get_historical_bars', return_value=None):
            from carrac.ai.pattern_match import find_similar_patterns
            result = find_similar_patterns('BTC', '1D')
            assert result['matches'] == []
            assert result['bars_searched'] == 0

    def test_find_similar_patterns_synthetic(self):
        """Pattern match with synthetic repeating data finds matches."""
        # Create synthetic data: a repeating sine wave
        n = 500
        bars = []
        for i in range(n):
            price = 100 + 10 * np.sin(i * 0.1)
            bars.append({
                'timestamp': f'2024-01-{(i % 28) + 1:02d}',
                'open': float(price),
                'high': float(price + 1),
                'low': float(price - 1),
                'close': float(price),
                'volume': 1000.0,
            })

        with patch('carrac.ai.pattern_match._get_historical_bars', return_value=bars):
            from carrac.ai.pattern_match import find_similar_patterns
            result = find_similar_patterns('TEST', '1D', window_size=20, min_correlation=0.8)
            assert len(result['matches']) > 0
            assert result['elapsed_ms'] >= 0
            # Sine wave repeats → high correlation matches expected
            assert result['matches'][0]['correlation'] >= 0.8


# ========== Trade Journal Tests ==========

class TestTradeJournal:
    """Tests for trade journal in content_db.py"""

    @pytest.fixture
    def journal_db(self, tmp_path, monkeypatch):
        import carrac.db_pool as _cdb
        _cdb._db_type = None  # Reset cached type
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        from carrac.content_db import ContentDatabase
        db = ContentDatabase(str(tmp_path / 'test_drawings.db'))
        return db

    def test_log_trade_open(self, journal_db):
        entry_id = journal_db.log_trade_open(
            user_id=1, asset='BTC', timeframe='1D',
            direction='long', entry_price=65000,
            target_price=70000, stop_price=63000,
        )
        assert entry_id > 0

    def test_log_trade_open_rr_calculation(self, journal_db):
        journal_db.log_trade_open(
            user_id=1, asset='BTC', timeframe='1D',
            direction='long', entry_price=65000,
            target_price=70000, stop_price=63000,
        )
        trades = journal_db.get_open_trades(1)
        assert len(trades) == 1
        # Risk = 2000, Reward = 5000, R:R = 2.5
        assert trades[0]['rr_planned'] == 2.5

    def test_log_trade_close(self, journal_db):
        entry_id = journal_db.log_trade_open(
            user_id=1, asset='ETH', timeframe='4h',
            direction='short', entry_price=3000,
            target_price=2500, stop_price=3200,
        )
        success = journal_db.log_trade_close(
            entry_id, exit_price=2600, exit_reason='target',
        )
        assert success

        # Should no longer be in open trades
        open_trades = journal_db.get_open_trades(1)
        assert len(open_trades) == 0

        # Should appear in history
        history = journal_db.get_trade_history(1, days=7)
        assert len(history) == 1
        assert history[0]['exit_price'] == 2600
        assert history[0]['exit_reason'] == 'target'
        # Short trade: entry=3000, exit=2600, risk=|3200-3000|=200
        # Direction-aware: pnl = entry - exit = 400, rr = 400/200 = 2.0
        assert history[0]['rr_actual'] == 2.0

    def test_get_open_trades_filtered_by_asset(self, journal_db):
        journal_db.log_trade_open(user_id=1, asset='BTC', timeframe='1D',
                                  direction='long', entry_price=65000)
        journal_db.log_trade_open(user_id=1, asset='ETH', timeframe='1D',
                                  direction='long', entry_price=3000)
        btc_trades = journal_db.get_open_trades(1, asset='BTC')
        assert len(btc_trades) == 1
        assert btc_trades[0]['asset'] == 'BTC'

    def test_get_journal_stats(self, journal_db):
        # Create and close 3 trades: 2 wins, 1 loss
        for i, (asset, entry, exit_p) in enumerate([
            ('BTC', 65000, 67000),   # win
            ('ETH', 3000, 3200),     # win
            ('BTC', 68000, 66000),   # loss
        ]):
            eid = journal_db.log_trade_open(
                user_id=1, asset=asset, timeframe='1D',
                direction='long', entry_price=entry,
                stop_price=entry - 1000, target_price=entry + 3000,
            )
            journal_db.log_trade_close(eid, exit_price=exit_p, exit_reason='manual')

        stats = journal_db.get_journal_stats(1, days=7)
        assert stats['trades'] == 3
        assert stats['wins'] == 2
        assert stats['losses'] == 1
        assert stats['win_rate'] == pytest.approx(66.7, abs=0.1)

    def test_find_journal_by_drawing(self, journal_db):
        eid = journal_db.log_trade_open(
            user_id=1, asset='BTC', timeframe='1D',
            direction='long', entry_price=65000,
            drawing_id=42,
        )
        found = journal_db.find_journal_by_drawing(42)
        assert found is not None
        assert found['id'] == eid

        not_found = journal_db.find_journal_by_drawing(999)
        assert not_found is None

    def test_user_isolation(self, journal_db):
        journal_db.log_trade_open(user_id=1, asset='BTC', timeframe='1D',
                                  direction='long', entry_price=65000)
        journal_db.log_trade_open(user_id=2, asset='BTC', timeframe='1D',
                                  direction='short', entry_price=65000)

        user1_trades = journal_db.get_open_trades(1)
        user2_trades = journal_db.get_open_trades(2)
        assert len(user1_trades) == 1
        assert len(user2_trades) == 1
        assert user1_trades[0]['direction'] == 'long'
        assert user2_trades[0]['direction'] == 'short'


# ========== Context Builder Tests ==========

class TestContextBuilder:
    """Tests for carrac/ai/context_builder.py"""

    def test_summarize_asset(self):
        from carrac.ai.context_builder import _summarize_asset
        asset = {
            'symbol': 'BTC',
            'latest_price': 68000,
            'consensio': {
                'total': {'positions': 5, 'max_positions': 9},
                '1D': {'signal': 3},
                '3D': {'signal': -1},
                '1W': {'signal': -2},
            },
            'weekly_rsi': 35.0,
            'ema_ribbon': {'trend': 'bear'},
            'adx': {'adx': 38},
            'funding_rate': -0.01,
            'cpr': {'price_position': 'above'},
            'realtime': {'change_pct': -3.5},
        }
        result = _summarize_asset(asset)
        assert 'BTC' in result
        assert '$68,000.00' in result
        assert 'wRSI=35.0' in result
        assert 'EMA Ribbon=bear' in result
        assert 'ADX=38' in result
        assert '24h=-3.5%' in result

    def test_format_drawings(self):
        from carrac.ai.context_builder import _format_drawings
        drawings = [
            {'drawing_type': 'zone', 'label': 'Support', 'price_level': 65000, 'price_level_upper': 66000},
            {'drawing_type': 'trendline', 'label': '', 'price_level': 70000, 'price_level_upper': None},
        ]
        result = _format_drawings(drawings)
        assert 'zone' in result
        # Labels are now XML-quarantined via escape_user_text, so we
        # match the tag wrapper + text rather than the raw quoted string.
        assert '<drawing_label>' in result and 'Support' in result
        assert '$65,000.00' in result
        assert 'trendline' in result

    def test_format_drawings_limit(self):
        from carrac.ai.context_builder import _format_drawings
        drawings = [{'drawing_type': 'zone', 'label': f'Zone {i}', 'price_level': i * 100, 'price_level_upper': None}
                    for i in range(20)]
        result = _format_drawings(drawings)
        assert '... and 5 more' in result

    def test_format_alerts(self):
        from carrac.ai.context_builder import _format_alerts
        alerts = [
            {'condition': 'crosses_above', 'price': 70000, 'label': 'Breakout'},
            {'condition': 'crosses_below', 'price': 60000, 'label': ''},
        ]
        result = _format_alerts(alerts)
        assert 'crosses_above' in result
        assert '$70,000.00' in result
        assert '(Breakout)' in result

    def test_shared_context_caching(self):
        from carrac.ai import context_builder
        # Reset cache
        context_builder._shared_context = {'text': '', 'updated_at': 0}

        with patch.object(context_builder, '_build_shared_context', return_value='test context') as mock_build:
            # First call should build
            result1 = context_builder.get_shared_context()
            assert result1 == 'test context'
            assert mock_build.call_count == 1

            # Second call within TTL should use cache
            result2 = context_builder.get_shared_context()
            assert result2 == 'test context'
            assert mock_build.call_count == 1  # not called again

    def test_get_asset_list_from_cache(self):
        from carrac.ai.context_builder import get_asset_list
        mock_cache = MagicMock()
        mock_cache.get.return_value = {
            'assets': [
                {'symbol': 'BTC'}, {'symbol': 'ETH'}, {'symbol': ''}, {'symbol': 'SPX'},
            ]
        }
        with patch('carrac.precompute.response_cache', mock_cache), \
             patch('database.OHLCVDatabase') as mock_db_cls:
            result = get_asset_list()
        assert result == ['BTC', 'ETH', 'SPX']
        mock_cache.get.assert_called_once_with('htf')
        mock_db_cls.assert_not_called()

    def test_get_asset_list_falls_back_to_database(self):
        from carrac.ai.context_builder import get_asset_list
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        with patch('carrac.precompute.response_cache', mock_cache), \
             patch('database.OHLCVDatabase') as mock_db_cls:
            mock_db_cls.return_value.get_all_symbols.return_value = ['BTC', 'SOL']
            result = get_asset_list()
        assert result == ['BTC', 'SOL']

    def test_get_user_scenarios_empty(self):
        from carrac.ai.context_builder import get_user_scenarios
        with patch('carrac.content_db.get_content_db') as mock_db:
            mock_db.return_value.drawing_list.return_value = []
            result = get_user_scenarios(1)
            assert result == []


# ========== Route Tests ==========

class TestAIRoutes:
    """Tests for carrac/ai/routes.py endpoints."""

    @pytest.fixture
    def ai_client(self, client):
        """Authenticated client with credits."""
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'test@example.com'
            sess['is_admin'] = False
            sess['tier'] = 'premium'
        return client

    def test_ai_page_removed(self, ai_client):
        resp = ai_client.get('/ai')
        assert resp.status_code == 404

    def test_balance_endpoint(self, ai_client):
        with patch('user_db.user_db') as mock_udb:
            mock_udb.get_ai_balance.return_value = 500
            resp = ai_client.get('/api/ai/balance')
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['balance_cents'] == 500
            assert data['balance'] == '$5.00'

    def test_assets_endpoint(self, ai_client):
        with patch('carrac.ai.context_builder.get_asset_list', return_value=['BTC', 'ETH']):
            with patch('carrac.ai.context_builder.get_user_scenarios', return_value=[]):
                resp = ai_client.get('/api/ai/assets')
                assert resp.status_code == 200
                data = resp.get_json()
                assert 'BTC' in data['assets']
                assert data['scenarios'] == []

    # /api/ai/chat is served by the FastAPI sub-app (no Flask
    # handler). Chat behavior tests now live in
    # tests/fastapi/test_ai_chat.py and use the asgi_client fixture.

    def test_pattern_match_requires_asset(self, ai_client):
        resp = ai_client.post('/api/ai/pattern-match',
                              json={},
                              content_type='application/json')
        assert resp.status_code == 400

    def test_pattern_match_insufficient_credits(self, ai_client):
        with patch('carrac.ai.billing.check_and_reserve', return_value=False):
            resp = ai_client.post('/api/ai/pattern-match',
                                  json={'asset': 'BTC'},
                                  content_type='application/json')
            assert resp.status_code == 402

    def test_journal_get(self, ai_client):
        with patch('carrac.content_db.get_content_db') as mock:
            db = MagicMock()
            db.get_open_trades.return_value = []
            db.get_trade_history.return_value = []
            db.get_journal_stats.return_value = {'trades': 0, 'wins': 0, 'losses': 0, 'win_rate': 0, 'avg_rr': 0}
            mock.return_value = db
            resp = ai_client.get('/api/ai/journal')
            assert resp.status_code == 200
            data = resp.get_json()
            assert 'open' in data
            assert 'history' in data
            assert 'stats' in data

    def test_journal_open(self, ai_client):
        with patch('carrac.content_db.get_content_db') as mock:
            db = MagicMock()
            db.log_trade_open.return_value = 42
            db.count_trades_this_month.return_value = 0
            mock.return_value = db
            resp = ai_client.post('/api/ai/journal/open',
                                  json={'asset': 'BTC', 'direction': 'long', 'entry_price': 65000},
                                  content_type='application/json')
            assert resp.status_code == 200
            assert resp.get_json()['id'] == 42

    def test_journal_open_requires_asset(self, ai_client):
        resp = ai_client.post('/api/ai/journal/open',
                              json={'direction': 'long'},
                              content_type='application/json')
        assert resp.status_code == 400

    def test_journal_close_requires_id(self, ai_client):
        resp = ai_client.post('/api/ai/journal/close',
                              json={},
                              content_type='application/json')
        assert resp.status_code == 400

    # /api/ai/weekly-review is served by the FastAPI sub-app (no Flask
    # handler). Behavior tests in tests/fastapi/test_ai_aux.py.

    def test_trade_design_valid_feature(self, ai_client):
        """trade_design is accepted as a valid feature (not silently downgraded to chat)."""
        from carrac.ai.routes import VALID_FEATURES
        assert 'trade_design' in VALID_FEATURES


class TestCopilotToolTierGate:
    """Copilot execute_tool / get_tools_for_focus must respect the
    tool's min_tier (and its `feature` knob when set).
    Without this, a lower tier could have Copilot call a paid-only tool
    through the MCP registry which doesn't re-check tier when reached via
    registry.call_tool.

    quant_get_data/quant_get_correlations/backtest_get_historical_analog moved
    from premium-only to the `quant_engine` knob (True for member and up) —
    these tests now use indicator_run_formula/scanner_get_transitions (genuinely
    premium-only: custom_formulas / non-quant scanner data, both untouched
    by that change) for the "still denied" cases, and assert quant_get_data
    now passes for member.
    """

    def test_execute_tool_denies_when_tier_insufficient(self, app):
        from carrac.ai.copilot_tools import execute_tool
        import json as _json
        with app.app_context():
            # indicator_run_formula IS the custom formula engine
            # (TIER_LIMITS['custom_formulas'] is False for member) — genuinely
            # premium-only, unaffected by that change.
            out = execute_tool('indicator_run_formula', {'symbol': 'BTC', 'expression': 'RSI(14)'},
                               user_id=1, user_tier='member')
            payload = _json.loads(out)
            assert payload.get('upgrade_required') is True
            assert payload.get('required_tier') == 'premium'

    def test_execute_tool_allows_premium_user(self, app, monkeypatch):
        """Premium tier passes the gate — call_tool is reached."""
        from carrac.ai.copilot_tools import execute_tool
        called = {'flag': False}
        def _fake_call(name, args):
            called['flag'] = True
            return {'ok': True}
        monkeypatch.setattr('carrac.mcp.registry.call_tool', _fake_call)
        with app.app_context():
            out = execute_tool('indicator_run_formula', {}, user_id=1, user_tier='premium')
            assert called['flag']
            assert 'ok' in out

    def test_execute_tool_allows_member_for_quant_engine_tool(self, app, monkeypatch):
        """quant_get_data is member-accessible via quant_engine."""
        from carrac.ai.copilot_tools import execute_tool
        called = {'flag': False}
        def _fake_call(name, args):
            called['flag'] = True
            return {'ok': True}
        monkeypatch.setattr('carrac.mcp.registry.call_tool', _fake_call)
        with app.app_context():
            out = execute_tool('quant_get_data', {}, user_id=1, user_tier='member')
            assert called['flag']
            assert 'ok' in out

    def test_execute_tool_denies_free_for_quant_engine_tool(self, app):
        """Free tier (quant_engine=False) is still denied."""
        from carrac.ai.copilot_tools import execute_tool
        import json as _json
        with app.app_context():
            out = execute_tool('quant_get_data', {}, user_id=1, user_tier='free')
            payload = _json.loads(out)
            assert payload.get('upgrade_required') is True

    def test_get_tools_for_focus_filters_premium_only_for_member(self, app):
        from carrac.ai.copilot_tools import get_tools_for_focus
        with app.app_context():
            # quant_get_data is no longer premium-only — member
            # now sees it under the 'report' focus.
            report_member = {t['name'] for t in get_tools_for_focus('report', user_tier='member')}
            assert 'quant_get_data' in report_member

            # scanner_get_transitions remains genuinely premium-only (scanner
            # state-change feed, not a quant_engine/backtester surface) —
            # still excluded for member under the 'scanner' focus.
            scanner_member = {t['name'] for t in get_tools_for_focus('scanner', user_tier='member')}
            scanner_premium = {t['name'] for t in get_tools_for_focus('scanner', user_tier='premium')}
            assert 'scanner_get_transitions' in scanner_premium
            assert 'scanner_get_transitions' not in scanner_member


class TestCopilotToolRegistryIntegrity:
    """copilot_tools.py's READ_TOOLS/WRITE_TOOLS frozensets and the
    13-area FOCUS_TOOLS dict are 116 string literals looked up via
    TOOL_REGISTRY.get(name) (get_tools_for_focus). A stale name after any
    rename makes the tool silently VANISH from that focus area — no
    exception, no test failure, just a missing tool in the in-app Copilot.
    This test existed nowhere before this file; it exists now specifically
    so the next rename can't reintroduce the same silent-vanish bug.
    """

    def test_read_tools_all_registered(self, app):
        from carrac.ai.copilot_tools import READ_TOOLS
        from carrac.mcp.registry import TOOL_REGISTRY
        from carrac.mcp.protocol import _ensure_tools_loaded
        with app.app_context():
            _ensure_tools_loaded()
            missing = sorted(n for n in READ_TOOLS if n not in TOOL_REGISTRY)
            assert not missing, f"READ_TOOLS names missing from TOOL_REGISTRY: {missing}"

    def test_write_tools_all_registered(self, app):
        from carrac.ai.copilot_tools import WRITE_TOOLS
        from carrac.mcp.registry import TOOL_REGISTRY
        from carrac.mcp.protocol import _ensure_tools_loaded
        with app.app_context():
            _ensure_tools_loaded()
            missing = sorted(n for n in WRITE_TOOLS if n not in TOOL_REGISTRY)
            assert not missing, f"WRITE_TOOLS names missing from TOOL_REGISTRY: {missing}"

    def test_focus_tools_all_registered(self, app):
        from carrac.ai.copilot_tools import FOCUS_TOOLS
        from carrac.mcp.registry import TOOL_REGISTRY
        from carrac.mcp.protocol import _ensure_tools_loaded
        with app.app_context():
            _ensure_tools_loaded()
            for focus, names in FOCUS_TOOLS.items():
                missing = sorted(n for n in names if n not in TOOL_REGISTRY)
                assert not missing, (
                    f"FOCUS_TOOLS[{focus!r}] names missing from TOOL_REGISTRY: {missing}"
                )

    def test_focus_tools_are_never_legacy_aliases(self):
        """FOCUS_TOOLS/READ_TOOLS/WRITE_TOOLS must reference CANONICAL names
        only — a legacy name here would still technically dispatch (aliases
        resolve at call_tool time), but get_tools_for_focus() does a raw
        TOOL_REGISTRY.get(name) with NO alias resolution, so a legacy name
        would silently vanish from the LLM's tool list exactly like a truly
        unknown name would."""
        from carrac.ai.copilot_tools import READ_TOOLS, WRITE_TOOLS, FOCUS_TOOLS
        from carrac.mcp.registry import LEGACY_TOOL_ALIASES
        all_names = set(READ_TOOLS) | set(WRITE_TOOLS) | {
            n for names in FOCUS_TOOLS.values() for n in names
        }
        legacy_used = sorted(all_names & set(LEGACY_TOOL_ALIASES))
        assert not legacy_used, f"copilot_tools.py references legacy names: {legacy_used}"


# /api/ai/execute-action tier-gate test moved to
# tests/fastapi/test_ai_aux.py (served by the FastAPI sub-app).


# ========== Trade Design Tests ==========

class TestTradeDesign:
    """Tests for the Trade Design feature mode."""

    def test_feature_definition(self):
        from carrac.ai.features import FEATURES
        td = FEATURES['trade_design']
        assert td['requires_asset'] is True
        assert td['requires_scenario'] is False
        assert td['max_output_tokens'] == 6000
        assert 'thesis' in td['system_instruction'].lower()
        assert 'DUAL-DIRECTION SCORECARD' in td['system_instruction']

    def test_build_system_prompt_trade_design(self):
        from carrac.ai.features import build_system_prompt
        prompt = build_system_prompt('trade_design')
        assert 'Carrac AI' in prompt  # BASE_PROMPT
        assert 'THESIS HANDLING' in prompt
        assert 'CONVICTION ANALYSIS' in prompt

    def test_dual_score_formatter(self):
        from carrac.ai.context_blocks import _format_dual_score_for_prompt
        long_data = {
            'symbol': 'BTC', 'direction': 'long', 'total': 8, 'verdict': 'GOOD',
            'criteria': {
                'trend': {'score': 2, 'evidence': 'All TFs bullish'},
                'momentum': {'score': 1, 'evidence': 'EMAs bullish, MACD flat'},
                'rsi': {'score': 1, 'evidence': 'wRSI 52'},
                'structure': {'score': 2, 'evidence': 'HH/HL'},
                'level': {'score': 1, 'evidence': 'S1 nearby'},
                'macro': {'score': 1, 'evidence': 'Neutral, F&G=45'},
            },
            'vetoes': [],
        }
        short_data = {
            'symbol': 'BTC', 'direction': 'short', 'total': 4, 'verdict': 'WEAK',
            'criteria': {
                'trend': {'score': 0, 'evidence': 'All TFs bullish (opposes short)'},
                'momentum': {'score': 1, 'evidence': 'EMAs bullish'},
                'rsi': {'score': 1, 'evidence': 'wRSI 52'},
                'structure': {'score': 0, 'evidence': 'HH/HL (opposes short)'},
                'level': {'score': 1, 'evidence': 'R1 nearby'},
                'macro': {'score': 1, 'evidence': 'Neutral, F&G=45'},
            },
            'vetoes': ['All TFs unanimously opposed — capped at WEAK'],
        }
        result = _format_dual_score_for_prompt(long_data, short_data)
        assert 'DUAL-DIRECTION CONDITIONS ALIGNED: BTC' in result
        assert 'LONG = 8/12 conditions (GOOD tier)' in result
        assert 'SHORT = 4/12 conditions (WEAK tier)' in result
        assert 'LONG BREAKDOWN:' in result
        assert 'SHORT BREAKDOWN:' in result
        assert 'All TFs unanimously opposed' in result

    def test_dual_score_formatter_with_trade_plan(self):
        from carrac.ai.context_blocks import _format_dual_score_for_prompt
        long_data = {
            'symbol': 'ETH', 'direction': 'long', 'total': 6, 'verdict': 'NEUTRAL',
            'criteria': {n: {'score': 1, 'evidence': 'test'} for n in
                         ('trend', 'momentum', 'rsi', 'structure', 'level', 'macro')},
            'vetoes': [],
            'trade_plan': {'entry': 3500.0, 'sl': 3300.0, 'sl_pct': 5.7,
                           'tp1': 3800.0, 'rr1': 1.5, 'tp2': 4100.0, 'rr2': 3.0},
            'setup': {'type': 'Pullback', 'readiness': 'ready', 'confidence': 0.72},
        }
        short_data = {
            'symbol': 'ETH', 'direction': 'short', 'total': 5, 'verdict': 'WEAK',
            'criteria': {n: {'score': 1, 'evidence': 'test'} for n in
                         ('trend', 'momentum', 'rsi', 'structure', 'level', 'macro')},
            'vetoes': [],
        }
        result = _format_dual_score_for_prompt(long_data, short_data)
        assert 'Entry=$3,500.00' in result
        assert 'SL=$3,300.00' in result
        assert 'TP1=$3,800.00' in result
        assert 'Pullback' in result
        assert '72%' in result

    def test_context_tiers_trade_design_block_list(self):
        """trade_design feature overrides page_blocks with custom set."""
        from carrac.ai.context_tiers import PAGE_BLOCKS
        # Verify the override is not hardcoded in PAGE_BLOCKS
        assert 'trade_design' not in PAGE_BLOCKS
        # The override happens dynamically in assemble_context() when feature='trade_design'

    def test_scorer_block_dual_mode(self):
        """ScorerBlock respects _dual config flag."""
        from carrac.ai.context_blocks import ScorerBlock
        block = ScorerBlock()
        # No asset → empty
        assert block.fetch(1, '', '1D', {'_dual': True}) == ''
        # With asset but no cache → empty (graceful)
        with patch('carrac.ai.context_blocks.ScorerBlock.fetch', return_value=''):
            result = block.fetch(1, 'BTC', '1D', {'_dual': True})
            assert result == ''


# ========== Report Follow-Up Tests ==========

class TestReportFollowUp:
    """Tests for the Report Follow-Up feature mode."""

    def test_feature_definition(self):
        from carrac.ai.features import FEATURES
        rf = FEATURES['report_followup']
        assert rf['requires_asset'] is False
        assert rf['requires_scenario'] is False
        assert rf['max_output_tokens'] == 3000
        assert 'morning report' in rf['system_instruction'].lower()

    def test_report_followup_in_valid_features(self):
        from carrac.ai.routes import VALID_FEATURES
        assert 'report_followup' in VALID_FEATURES

    def test_build_system_prompt_report(self):
        from carrac.ai.features import build_system_prompt
        prompt = build_system_prompt('report_followup')
        assert 'Carrac AI' in prompt
        assert 'morning report' in prompt.lower()
        assert 'PERSONAL SECTION' in prompt

    def test_full_report_block_registered(self):
        from carrac.ai.context_blocks import BLOCK_REGISTRY
        assert 'full_report' in BLOCK_REGISTRY
        block = BLOCK_REGISTRY['full_report']
        assert block.tier_minimum == 'member'
        assert block.token_estimate == 1500

    def test_full_report_block_fetch_no_report(self):
        from carrac.ai.context_blocks import FullReportBlock
        block = FullReportBlock()
        with patch('carrac.content_db.get_content_db') as mock_db:
            mock_db.return_value.get_latest_report.return_value = None
            result = block.fetch(1, '', '1D', {})
            assert result == ''

    def test_full_report_block_fetch_with_report(self):
        from carrac.ai.context_blocks import FullReportBlock
        block = FullReportBlock()
        mock_report = {
            'id': 1,
            'content': 'BTC analysis: bullish structure forming above 65K support.',
            'created_at': '2026-04-01 06:00:00',
        }
        with patch('carrac.content_db.get_content_db') as mock_db:
            db = mock_db.return_value
            db.get_latest_report.return_value = mock_report
            db.get_personal_section.return_value = None
            result = block.fetch(1, '', '1D', {})
            assert 'MORNING ANALYSIS (2026-04-01)' in result
            assert 'BTC analysis' in result
            assert 'PERSONAL SECTION' not in result

    def test_full_report_block_with_personal_section(self):
        from carrac.ai.context_blocks import FullReportBlock
        block = FullReportBlock()
        mock_report = {'id': 1, 'content': 'Macro overview.', 'created_at': '2026-04-01 06:00:00'}
        mock_personal = {'content': 'Your BTC long scenario looks strong.'}
        with patch('carrac.content_db.get_content_db') as mock_db:
            db = mock_db.return_value
            db.get_latest_report.return_value = mock_report
            db.get_personal_section.return_value = mock_personal
            result = block.fetch(1, '', '1D', {})
            assert 'PERSONAL SECTION' in result
            assert 'Your BTC long' in result

    def test_full_report_block_truncation(self):
        from carrac.ai.context_blocks import FullReportBlock
        block = FullReportBlock()
        long_content = 'A' * 7000  # Over 6000 char limit
        mock_report = {'id': 1, 'content': long_content, 'created_at': '2026-04-01 06:00:00'}
        with patch('carrac.content_db.get_content_db') as mock_db:
            db = mock_db.return_value
            db.get_latest_report.return_value = mock_report
            db.get_personal_section.return_value = None
            result = block.fetch(1, '', '1D', {})
            assert '[... report truncated]' in result
            # Content should be capped
            assert len(result) < 7500

    def test_context_tiers_report_followup_block_list(self):
        """report_followup feature overrides page_blocks with custom set."""
        from carrac.ai.context_tiers import PAGE_BLOCKS
        assert 'report_followup' not in PAGE_BLOCKS
