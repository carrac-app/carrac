"""Routes that are public API but have no caller in the web app, the MCP tools or the SDKs.

Each one is documented in the OpenAPI document; these tests pin that the route
exists, that it is documented, and that it answers as documented.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def _as_tier(client, tier):
    with client.session_transaction() as sess:
        sess['user_id'] = f'{tier}-user'
        sess['email'] = f'{tier}@example.com'
        sess['is_admin'] = False
        sess['tier'] = tier
    return client


def _documented(path, method):
    from carrac.openapi import build_spec
    return method in build_spec()['paths'].get(path, {})


# (method, spec path, concrete path) for every API-only route kept in the public API
API_ONLY = [
    ('get', '/api/consensio/{symbol}', '/api/consensio/BTC'),
    ('get', '/api/weekly-signal', '/api/weekly-signal'),
    ('get', '/api/quant/leadlag', '/api/quant/leadlag'),
    ('get', '/api/quant/derivatives-regime', '/api/quant/derivatives-regime'),
    ('get', '/api/transitions', '/api/transitions'),
    ('get', '/api/drawings/all', '/api/drawings/all'),
    ('get', '/api/report/weekly/latest', '/api/report/weekly/latest'),
    ('get', '/api/report/week_ahead/latest', '/api/report/week_ahead/latest'),
    ('get', '/api/ai/assets', '/api/ai/assets'),
    ('delete', '/api/ai/conversations/{conv_id}', '/api/ai/conversations/1'),
    ('get', '/api/health/hyperliquid-ws', '/api/health/hyperliquid-ws'),
    ('get', '/api/health/ccxt-ws', '/api/health/ccxt-ws'),
]


@pytest.mark.parametrize('method,spec_path,path', API_ONLY)
def test_route_is_served_and_documented(client, method, spec_path, path):
    assert _documented(spec_path, method)
    resp = getattr(client, method)(path)
    # Anonymous callers are turned away by the auth gate, never a 404.
    assert resp.status_code in (302, 401, 403), resp.status_code


class TestConsensio:
    def _frame(self):
        return pd.DataFrame({'close': [1.0, 2.0, 3.0]})

    def test_unknown_symbol_is_404(self, auth_client):
        empty = {'1D': pd.DataFrame(), '3D': pd.DataFrame(), '1W': pd.DataFrame()}
        svc = MagicMock()
        svc.db.get_multi_timeframe_data.return_value = empty
        with patch('carrac.charts.routes.get_services', return_value=svc):
            resp = auth_client.get('/api/consensio/NOPE')
        assert resp.status_code == 404
        assert resp.get_json()['symbol'] == 'NOPE'

    def test_known_symbol_returns_values_and_price(self, auth_client):
        frame = self._frame()
        svc = MagicMock()
        svc.db.get_multi_timeframe_data.return_value = {'1D': frame, '3D': frame, '1W': frame}
        svc.consensio.calculate_multi_timeframe.return_value = {'consensio': 7}
        with patch('carrac.charts.routes.get_services', return_value=svc):
            resp = auth_client.get('/api/consensio/btc')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body == {'consensio': 7, 'symbol': 'BTC', 'latest_price': 3.0}


class TestWeeklySignal:
    def test_only_btc_has_a_signal(self, auth_client):
        resp = auth_client.get('/api/weekly-signal?symbol=ETH')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['signal'] == 'N/A' and body['symbol'] == 'ETH'

    def test_btc_returns_engine_result(self, auth_client):
        svc = MagicMock()
        svc.weekly_signal_engine.calculate.return_value = {'signal': 'NEUTRAL'}
        with patch('carrac.charts.routes.get_services', return_value=svc):
            resp = auth_client.get('/api/weekly-signal')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['signal'] == 'NEUTRAL' and body['symbol'] == 'BTC'

    def test_engine_failure_is_a_500_not_a_signal(self, auth_client):
        svc = MagicMock()
        svc.weekly_signal_engine.calculate.side_effect = RuntimeError('boom')
        with patch('carrac.charts.routes.get_services', return_value=svc):
            resp = auth_client.get('/api/weekly-signal')
        assert resp.status_code == 500
        assert resp.get_json()['signal'] == 'ERROR'


class TestQuantLeadlag:
    def test_missing_target_lists_the_available_ones(self, client):
        _as_tier(client, 'premium')
        resp = client.get('/api/quant/leadlag')
        assert resp.status_code == 400
        assert resp.get_json()['available_targets']

    def test_unconfigured_target_is_rejected(self, client):
        _as_tier(client, 'premium')
        resp = client.get('/api/quant/leadlag?target=NOT_A_TARGET')
        assert resp.status_code == 400
        assert 'no configured lead-lag pairs' in resp.get_json()['error']

    def test_free_tier_is_blocked(self, client):
        _as_tier(client, 'free')
        assert client.get('/api/quant/leadlag?target=BTC').status_code == 403


class TestDerivativesRegime:
    def _conn(self, row):
        cur = MagicMock()
        cur.fetchone.return_value = row
        conn = MagicMock()
        conn.cursor.return_value = cur
        conn.__enter__.return_value = conn
        return conn

    def test_no_rows_means_unknown_regime(self, client):
        _as_tier(client, 'premium')
        with patch('quant.api.routes._qe_cache') as cache, \
                patch('quant.api.routes.get_conn', return_value=self._conn(None)):
            cache.get.return_value = None
            resp = client.get('/api/quant/derivatives-regime')
        assert resp.status_code == 200
        assert resp.get_json() == {'by_asset': {}, 'regime': {'regime': 'unknown', 'confidence': 0}}

    def test_free_tier_is_blocked(self, client):
        _as_tier(client, 'free')
        assert client.get('/api/quant/derivatives-regime').status_code == 403


class TestTransitions:
    def test_free_tier_gets_the_upgrade_message(self, client):
        _as_tier(client, 'free')
        resp = client.get('/api/transitions')
        assert resp.status_code == 403
        assert resp.get_json()['upgrade_to'] == 'member'

    def test_member_gets_events_with_the_filters_applied(self, client):
        _as_tier(client, 'member')
        events = [{'symbol': 'BTC', 'indicator': 'trend', 'significance': 2}]
        with patch('carrac.indicator_state.get_recent_transitions', return_value=events) as get:
            resp = client.get('/api/transitions?symbol=BTC&min_significance=2&limit=999')
        assert resp.status_code == 200
        assert resp.get_json() == {'events': events, 'count': 1}
        get.assert_called_once_with(limit=200, symbol='BTC', min_significance=2)


class TestDeleteConversation:
    def test_deletes_only_the_callers_conversation(self, auth_client):
        with patch('user_db.user_db') as db:
            db.delete_conversation.return_value = True
            resp = auth_client.delete('/api/ai/conversations/5')
        assert resp.status_code == 200 and resp.get_json() == {'ok': True}
        db.delete_conversation.assert_called_once_with(5, 'test-user')


class TestStreamHealth:
    def test_hyperliquid_reports_the_state_file(self, auth_client):
        state = {'enabled': True, 'connected': True}
        with patch('data_sources.hyperliquid_ws.HyperliquidWSWorker.read_health_state',
                   return_value=state):
            resp = auth_client.get('/api/health/hyperliquid-ws')
        assert resp.status_code == 200 and resp.get_json() == state

    def test_exchange_streams_report_the_state_file(self, auth_client):
        state = {'enabled': False, 'reason': 'no health state file found'}
        with patch('data_sources.ccxt_ws.CcxtWSWorker.read_health_state', return_value=state):
            resp = auth_client.get('/api/health/ccxt-ws')
        assert resp.status_code == 200 and resp.get_json() == state
