"""
Tests for cross-device drawing sync and polling optimizations.

Covers:
- Sync poll endpoint (session filtering, timestamp comparison)
- Sync debug endpoint
"""
import json
from unittest.mock import patch, MagicMock



# ── Sync Poll Endpoint ──────────────────────────────────────────────


class TestSyncPoll:
    """Test /api/sync/poll endpoint."""

    def test_poll_requires_auth(self, client):
        r = client.get('/api/sync/poll?since=0&sid=tab1')
        assert r.status_code == 401

    def test_poll_no_changes(self, auth_client):
        with patch('carrac.sync.routes.get_last_change', return_value=(0, None)), \
             patch('carrac.sync.routes.get_last_pref_change', return_value=(0, None)):
            r = auth_client.get('/api/sync/poll?since=0&sid=tab1')
            assert r.status_code == 200
            data = r.get_json()
            assert data['changed'] is False
            assert data['ts'] == 0

    def test_poll_detects_change_from_other_session(self, auth_client):
        detail = {'ts': 100.0, 'assets': ['BTC'], 'session_id': 'tab_other'}
        with patch('carrac.sync.routes.get_last_change', return_value=(100.0, detail)), \
             patch('carrac.sync.routes.get_last_pref_change', return_value=(0, None)):
            r = auth_client.get('/api/sync/poll?since=50&sid=tab_mine')
            data = r.get_json()
            assert data['changed'] is True
            assert data['assets'] == ['BTC']
            assert data['session_id'] == 'tab_other'

    def test_poll_ignores_own_session(self, auth_client):
        detail = {'ts': 100.0, 'assets': ['ETH'], 'session_id': 'tab_mine'}
        with patch('carrac.sync.routes.get_last_change', return_value=(100.0, detail)), \
             patch('carrac.sync.routes.get_last_pref_change', return_value=(0, None)):
            r = auth_client.get('/api/sync/poll?since=50&sid=tab_mine')
            data = r.get_json()
            assert data['changed'] is False

    def test_poll_no_change_when_ts_not_advanced(self, auth_client):
        detail = {'ts': 50.0, 'assets': ['BTC'], 'session_id': 'tab_other'}
        with patch('carrac.sync.routes.get_last_change', return_value=(50.0, detail)), \
             patch('carrac.sync.routes.get_last_pref_change', return_value=(0, None)):
            r = auth_client.get('/api/sync/poll?since=50&sid=tab_mine')
            data = r.get_json()
            assert data['changed'] is False

    def test_poll_pref_change_detected(self, auth_client):
        pref_detail = {'ts': 200.0, 'session_id': 'tab_other'}
        with patch('carrac.sync.routes.get_last_change', return_value=(0, None)), \
             patch('carrac.sync.routes.get_last_pref_change', return_value=(200.0, pref_detail)):
            r = auth_client.get('/api/sync/poll?since=0&pref_since=100&sid=tab_mine')
            data = r.get_json()
            assert data['changed'] is False
            assert data['prefs_changed'] is True

    def test_poll_pref_ignores_own_session(self, auth_client):
        pref_detail = {'ts': 200.0, 'session_id': 'tab_mine'}
        with patch('carrac.sync.routes.get_last_change', return_value=(0, None)), \
             patch('carrac.sync.routes.get_last_pref_change', return_value=(200.0, pref_detail)):
            r = auth_client.get('/api/sync/poll?since=0&pref_since=100&sid=tab_mine')
            data = r.get_json()
            assert data['prefs_changed'] is False


# ── Sync Event Publishing (Unit) ────────────────────────────────────


class TestSyncEvents:
    """Test publish_drawing_event and get_last_change with mocked Redis."""

    def test_publish_and_get(self):
        from carrac.sync.events import publish_drawing_event
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_redis.ping.return_value = True

        with patch('carrac.sync.events._get_redis', return_value=mock_redis):
            publish_drawing_event('user1', ['BTC', 'ETH'], 'tab_abc')

        # Verify pipeline SET calls
        assert mock_pipe.set.call_count == 2
        # First call: timestamp key
        ts_call = mock_pipe.set.call_args_list[0]
        assert 'user1' in ts_call[0][0]
        # Second call: detail key with payload
        detail_call = mock_pipe.set.call_args_list[1]
        payload = json.loads(detail_call[0][1])
        assert payload['assets'] == ['BTC', 'ETH']
        assert payload['session_id'] == 'tab_abc'
        mock_pipe.execute.assert_called_once()

    def test_get_last_change_no_redis(self):
        from carrac.sync.events import get_last_change
        with patch('carrac.sync.events._get_redis', return_value=None):
            ts, detail = get_last_change('user1')
            assert ts == 0
            assert detail is None


# ── Rate Limit ──────────────────────────────────────────────────────


class TestSyncRateLimit:
    """Test sync endpoint rate limiting is not too aggressive."""

    def test_poll_allows_burst(self, auth_client):
        """12 rapid polls (1 minute of 5s polling) should not be rate-limited."""
        with patch('carrac.sync.routes.get_last_change', return_value=(0, None)), \
             patch('carrac.sync.routes.get_last_pref_change', return_value=(0, None)):
            for _ in range(12):
                r = auth_client.get('/api/sync/poll?since=0&sid=tab1')
                assert r.status_code == 200, f"Rate limited after {_+1} requests"
