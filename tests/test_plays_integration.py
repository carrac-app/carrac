"""
Tests for Plays integration — AI context block, context tiers, morning engine formatter.
"""

import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def _reset_sqlite_conn():
    """Reset SQLite thread-local connection between tests."""
    import carrac.db_pool as _cdb
    if hasattr(_cdb._sqlite_local, 'conn') and _cdb._sqlite_local.conn is not None:
        try:
            _cdb._sqlite_local.conn.close()
        except Exception:
            pass
        _cdb._sqlite_local.conn = None
    yield
    if hasattr(_cdb._sqlite_local, 'conn') and _cdb._sqlite_local.conn is not None:
        try:
            _cdb._sqlite_local.conn.close()
        except Exception:
            pass
        _cdb._sqlite_local.conn = None


class TestPlayBlock:
    """PlayBlock in AI context blocks."""

    def test_play_block_registered(self):
        from carrac.ai.context_blocks import BLOCK_REGISTRY
        assert 'plays' in BLOCK_REGISTRY
        block = BLOCK_REGISTRY['plays']
        assert block.id == 'plays'
        assert block.category == 'personal'

    def test_play_block_returns_empty_when_no_plays(self, app):
        from carrac.ai.context_blocks import BLOCK_REGISTRY
        block = BLOCK_REGISTRY['plays']
        with patch('carrac.plays.models.play_get_digest', return_value={'plays': [], 'summary': 'No active plays.'}):
            result = block.fetch('test-user', 'BTC', '1D', {})
        assert result == ''

    def test_play_block_formats_plays(self, app):
        from carrac.ai.context_blocks import BLOCK_REGISTRY
        block = BLOCK_REGISTRY['plays']
        mock_data = {
            'plays': [{
                'id': 1,
                'title': 'BTC Long from $62K',
                'thesis': 'Breakout above resistance',
                'type': 'directional',
                'approach': 'single',
                'direction': 'long',
                'primary_asset': 'BTC',
                'related_assets': [],
                'trigger_asset': None,
                'status': 'watching',
                'conditions_met': '2/3',
                'invalidation': None,
                'action_plan': 'Enter on daily close above 65K',
                'expiry': None,
                'timeframe': 'swing',
            }],
            'summary': '1 watching',
        }
        with patch('carrac.plays.models.play_get_digest', return_value=mock_data):
            result = block.fetch('test-user', 'BTC', '1D', {})
        assert 'BTC Long from $62K' in result
        assert 'directional/single' in result
        assert 'watching' in result
        assert 'Breakout above resistance' in result
        assert '2/3' in result
        assert 'Enter on daily close' in result

    def test_play_block_prioritizes_relevant_asset(self, app):
        from carrac.ai.context_blocks import BLOCK_REGISTRY
        block = BLOCK_REGISTRY['plays']
        mock_data = {
            'plays': [
                {'id': 1, 'title': 'ETH Play', 'type': 'directional', 'approach': 'single',
                 'direction': 'long', 'primary_asset': 'ETH', 'related_assets': [],
                 'trigger_asset': None, 'status': 'watching', 'conditions_met': '0/0',
                 'thesis': None, 'action_plan': None, 'invalidation': None,
                 'expiry': None, 'timeframe': None},
                {'id': 2, 'title': 'BTC Play', 'type': 'macro', 'approach': 'single',
                 'direction': 'long', 'primary_asset': 'BTC', 'related_assets': [],
                 'trigger_asset': None, 'status': 'acting', 'conditions_met': '3/3',
                 'thesis': None, 'action_plan': None, 'invalidation': None,
                 'expiry': None, 'timeframe': None},
            ],
            'summary': '1 acting, 1 watching',
        }
        with patch('carrac.plays.models.play_get_digest', return_value=mock_data):
            result = block.fetch('test-user', 'BTC', '1D', {})
        # BTC play should appear before ETH play
        btc_pos = result.find('BTC Play')
        eth_pos = result.find('ETH Play')
        assert btc_pos < eth_pos

    def test_play_block_handles_exception(self, app):
        from carrac.ai.context_blocks import BLOCK_REGISTRY
        block = BLOCK_REGISTRY['plays']
        with patch('carrac.plays.models.play_get_digest', side_effect=Exception('DB error')):
            result = block.fetch('test-user', 'BTC', '1D', {})
        assert result == ''


class TestContextTiers:
    """Plays integration in context tier system."""

    def test_plays_in_chart_page_blocks(self):
        from carrac.ai.context_tiers import PAGE_BLOCKS
        chart_ids = [b['id'] for b in PAGE_BLOCKS['chart']]
        assert 'plays' in chart_ids

    def test_plays_keyword_hints(self):
        from carrac.ai.context_tiers import detect_context_hints
        assert 'plays' in detect_context_hints('how are my plays looking?')
        assert 'plays' in detect_context_hints('check my play status')
        assert 'plays' in detect_context_hints('thesis review')
        assert 'plays' not in detect_context_hints('what is the current price?')

    def test_plays_keyword_hypothesis(self):
        from carrac.ai.context_tiers import detect_context_hints
        assert 'plays' in detect_context_hints('my hypothesis about BTC')
        assert 'plays' in detect_context_hints('update my hypotheses')


class TestMorningEngineFormatter:
    """Plays formatter for morning engine."""

    def test_format_plays_empty(self):
        from morning_engine.data_assembly import _format_plays
        result = _format_plays({'plays': []})
        assert result == ''

    def test_format_plays_with_data(self):
        from morning_engine.data_assembly import _format_plays
        data = {
            'plays': [{
                'id': 1,
                'title': 'DXY Breakdown BTC Rally',
                'thesis': 'Dollar weakening leads to crypto rally',
                'type': 'macro',
                'approach': 'single',
                'direction': 'long',
                'primary_asset': 'BTC',
                'status': 'watching',
                'conditions_met': '1/3',
                'action_plan': 'Enter when DXY below 103',
                'invalidation': {'type': 'price_level', 'label': 'DXY above 106', 'met': False},
                'expiry': '2026-04-15',
                'timeframe': 'swing',
            }],
            'summary': '1 watching',
        }
        result = _format_plays(data)
        assert 'DXY Breakdown' in result
        assert 'macro' in result
        assert 'Dollar weakening' in result
        assert '1/3' in result
        assert 'Enter when DXY below 103' in result
        assert 'DXY above 106' in result
        assert 'not met' in result
        assert '2026-04-15' in result

    def test_format_plays_error_response(self):
        from morning_engine.data_assembly import _format_plays
        result = _format_plays({'error': 'Not found'})
        assert result == ''

    def test_format_plays_invalidation_met(self):
        from morning_engine.data_assembly import _format_plays
        data = {
            'plays': [{
                'id': 1,
                'title': 'Test',
                'type': 'directional',
                'approach': 'single',
                'direction': 'long',
                'primary_asset': 'BTC',
                'status': 'invalidated',
                'conditions_met': '0/1',
                'thesis': None,
                'action_plan': None,
                'invalidation': {'type': 'price_level', 'label': 'Price below 50K', 'met': True},
                'expiry': None,
                'timeframe': None,
            }],
        }
        result = _format_plays(data)
        assert 'MET' in result


class TestQuickActions:
    """Plays quick action is registered."""

    def test_plays_quick_action_exists(self):
        """Verify plays quick action is in the JS source (basic text check)."""
        import os
        js_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'js', 'ai-panel.js')
        with open(js_path, 'r') as f:
            content = f.read()
        assert "'Plays'" in content or '"Plays"' in content
        assert 'active plays' in content.lower()
