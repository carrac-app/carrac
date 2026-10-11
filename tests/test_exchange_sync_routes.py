import pytest
"""Flask routes for exchange-sync connections/suggestions.

Uses the shared `app`/`client` fixtures (tests/conftest.py) so CSRF, session
auth, and blueprint registration are exercised exactly as in production.
journal_autosync is closed-default (absent from TIER_LIMITS), so most tests
monkeypatch carrac.auth.decorators.get_tier_limits to simulate the knob
being wired in by the parallel branch that owns TIER_LIMITS.

No live exchange API calls — BinanceAuthClient.check_read_only /
_request are always monkeypatched.
"""

from unittest.mock import patch



@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch):
    """The connect route now refuses (503) without an encryption
    key, so every route test runs with a real one; the refusal itself is
    asserted in TestConnectRefusesWithoutEncryptionKey."""
    from cryptography.fernet import Fernet
    import carrac.secret_box as tc
    monkeypatch.setenv('TELEGRAM_TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setattr(tc, '_fernet_instance', None)
    monkeypatch.setattr(tc, '_warned_missing_key', False)
    yield


def _login(client, user_id=501, tier='member'):
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['email'] = f'user{user_id}@example.com'
        sess['is_admin'] = False
        sess['tier'] = tier
    return client


def _unlocked_limits(*_args, **_kwargs):
    return {'journal_autosync': True}


def _locked_limits(*_args, **_kwargs):
    return {'journal_autosync': False}


class TestConnectionsLockedDefault:
    def _strip_knob(self, monkeypatch):
        from carrac.auth import decorators as dec
        stripped = {t: {k: v for k, v in lim.items() if k != 'journal_autosync'}
                    for t, lim in dec.TIER_LIMITS.items()}
        monkeypatch.setattr(dec, 'TIER_LIMITS', stripped)

    def test_list_is_locked_when_knob_removed(self, client, monkeypatch):
        self._strip_knob(monkeypatch)
        _login(client)
        resp = client.get('/api/journal/exchange-sync/connections')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['locked'] is True

    def test_connect_403_when_knob_removed(self, client, monkeypatch):
        self._strip_knob(monkeypatch)
        _login(client)
        resp = client.post(
            '/api/journal/exchange-sync/connections',
            json={'exchange': 'hyperliquid', 'address': '0x' + 'a' * 40},
        )
        assert resp.status_code == 403
        assert resp.get_json().get('upgrade_required') is True


class TestConnectHyperliquid:
    def test_connect_valid_address(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client)
        resp = client.post(
            '/api/journal/exchange-sync/connections',
            json={'exchange': 'hyperliquid', 'address': '0x' + 'b' * 40, 'label': 'main wallet'},
        )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body['exchange'] == 'hyperliquid'
        assert body['masked_identifier'] == '0x' + 'b' * 40
        assert 'credentials_encrypted' not in body
        assert 'cursor_data' not in body

    def test_connect_rejects_invalid_address(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client)
        resp = client.post(
            '/api/journal/exchange-sync/connections',
            json={'exchange': 'hyperliquid', 'address': 'not-an-address'},
        )
        assert resp.status_code == 400

    def test_connect_rejects_unknown_exchange(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client)
        resp = client.post(
            '/api/journal/exchange-sync/connections',
            json={'exchange': 'kraken', 'address': '0x' + 'c' * 40},
        )
        assert resp.status_code == 400


class TestConnectBinance:
    def test_connect_verifies_read_only_permission(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client)
        with patch(
            'carrac.journal.binance_auth_client.BinanceAuthClient._request',
            return_value={
                'enableReading': True, 'enableWithdrawals': False,
                'enableSpotAndMarginTrading': False, 'enableMargin': False,
                'enableFutures': False, 'permitsUniversalTransfer': False,
            },
        ):
            resp = client.post(
                '/api/journal/exchange-sync/connections',
                json={'exchange': 'binance', 'api_key': 'AAAABBBBCCCCDDDD', 'api_secret': 'shh'},
            )
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body['masked_identifier'].endswith('DDDD')
        assert 'shh' not in str(body)

    def test_connect_rejects_elevated_permission_key(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client)
        with patch(
            'carrac.journal.binance_auth_client.BinanceAuthClient._request',
            return_value={
                'enableReading': True, 'enableWithdrawals': True,
                'enableSpotAndMarginTrading': False, 'enableMargin': False,
                'enableFutures': False, 'permitsUniversalTransfer': False,
            },
        ):
            resp = client.post(
                '/api/journal/exchange-sync/connections',
                json={'exchange': 'binance', 'api_key': 'AAAABBBBCCCCDDDD', 'api_secret': 'shh'},
            )
        assert resp.status_code == 400
        assert 'permission' in resp.get_json()['error'].lower()

    def test_connect_missing_fields(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client)
        resp = client.post(
            '/api/journal/exchange-sync/connections',
            json={'exchange': 'binance', 'api_key': ''},
        )
        assert resp.status_code == 400


class TestConnectionLifecycle:
    def test_list_then_disconnect(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client, user_id=777)

        resp = client.post(
            '/api/journal/exchange-sync/connections',
            json={'exchange': 'hyperliquid', 'address': '0x' + 'd' * 40},
        )
        assert resp.status_code == 201
        conn_id = resp.get_json()['id']

        listed = client.get('/api/journal/exchange-sync/connections').get_json()
        assert listed['locked'] is False
        assert len(listed['connections']) == 1
        assert listed['connections'][0]['id'] == conn_id

        deleted = client.delete(f'/api/journal/exchange-sync/connections/{conn_id}')
        assert deleted.status_code == 200

        listed_after = client.get('/api/journal/exchange-sync/connections').get_json()
        assert listed_after['connections'] == []

    def test_disconnect_rejects_foreign_connection(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client, user_id=1)
        conn_id = client.post(
            '/api/journal/exchange-sync/connections',
            json={'exchange': 'hyperliquid', 'address': '0x' + 'e' * 40},
        ).get_json()['id']

        _login(client, user_id=2)
        resp = client.delete(f'/api/journal/exchange-sync/connections/{conn_id}')
        assert resp.status_code == 404

    def test_sync_now_rejects_error_status_connection(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client, user_id=888)
        conn_id = client.post(
            '/api/journal/exchange-sync/connections',
            json={'exchange': 'hyperliquid', 'address': '0x' + 'f' * 40},
        ).get_json()['id']

        from carrac.journal.exchange_connections import mark_sync_error, MAX_CONSECUTIVE_ERRORS
        for _ in range(MAX_CONSECUTIVE_ERRORS):
            mark_sync_error(conn_id, 'boom')

        resp = client.post(f'/api/journal/exchange-sync/connections/{conn_id}/sync-now')
        assert resp.status_code == 400


class TestSuggestionResolution:
    def test_resolve_requires_valid_action(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client, user_id=901)
        resp = client.post(
            '/api/journal/exchange-sync/suggestions/1/resolve', json={'action': 'bogus'}
        )
        assert resp.status_code == 400

    def test_resolve_not_found(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client, user_id=902)
        resp = client.post(
            '/api/journal/exchange-sync/suggestions/999999/resolve', json={'action': 'skip'}
        )
        assert resp.status_code == 404

    def test_resolve_skip_end_to_end(self, client, monkeypatch):
        monkeypatch.setattr('carrac.auth.decorators.get_tier_limits', _unlocked_limits)
        _login(client, user_id=903)
        conn_id = client.post(
            '/api/journal/exchange-sync/connections',
            json={'exchange': 'hyperliquid', 'address': '0x' + '1' * 40},
        ).get_json()['id']

        from datetime import datetime, timezone
        from carrac.journal.exchange_sync import create_suggestion, list_suggestions

        create_suggestion(
            connection_id=conn_id, user_id=903, exchange='hyperliquid', symbol='ETH',
            direction='long', entry_price=100, exit_price=110, size=1, fees=0.1,
            opened_at=datetime.now(timezone.utc), closed_at=datetime.now(timezone.utc),
            matched_trade_id=None,
        )
        sugg_id = list_suggestions(903)[0]['id']

        resp = client.post(
            f'/api/journal/exchange-sync/suggestions/{sugg_id}/resolve', json={'action': 'skip'}
        )
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'skipped'


# ── the connect route refuses, loudly, when encryption is unavailable ──

class TestConnectRefusesWithoutEncryptionKey:
    def test_connect_503_when_key_unset(self, client, monkeypatch):
        import carrac.secret_box as tc
        from carrac.journal import routes as jr
        monkeypatch.setattr(jr, '_exchange_sync_limits', _unlocked_limits)
        monkeypatch.delenv('TELEGRAM_TOKEN_ENCRYPTION_KEY', raising=False)
        monkeypatch.setattr(tc, '_fernet_instance', None)
        monkeypatch.setattr(tc, '_warned_missing_key', False)
        _login(client)
        resp = client.post(
            '/api/journal/exchange-sync/connections',
            json={'exchange': 'hyperliquid', 'address': '0x' + 'd' * 40},
        )
        assert resp.status_code == 503, resp.data
        assert 'encrypt' in resp.get_json()['error'].lower()
        listing = client.get('/api/journal/exchange-sync/connections').get_json()
        assert listing.get('connections', []) == []
