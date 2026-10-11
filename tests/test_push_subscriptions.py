"""Web-push subscriptions: what may be stored and sent to, the row-level store,
and the one-shot import of the old Redis map / JSON file."""
import json
import logging
import socket
import sqlite3
import threading
from unittest.mock import patch

import pytest

import push_notifications as pn
from carrac.alerts import push_store

FCM = 'https://fcm.googleapis.com/fcm/send/dGVzdC1lbmRwb2ludA:APA91bH-example'
KEYS = {'p256dh': 'test-p256dh-key', 'auth': 'test-auth-secret'}


def _sub(endpoint=FCM, keys=KEYS):
    return {'endpoint': endpoint, 'expirationTime': None,
            'keys': dict(keys) if keys is not None else None}


def _public_dns(host, *a, **k):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('142.250.74.10', 443))]


@pytest.fixture(autouse=True)
def _push_db(tmp_path, rebind_db, monkeypatch):
    rebind_db(tmp_path / 'db')
    data_dir = tmp_path / 'push-data'
    data_dir.mkdir()
    monkeypatch.setenv('DATA_DIR', str(data_dir))
    monkeypatch.setattr(push_store, '_import_checked', True)
    monkeypatch.setattr(push_store, '_redis', lambda: None)
    return data_dir


@pytest.fixture
def public_dns():
    with patch('socket.getaddrinfo', side_effect=_public_dns):
        yield


def _store_raw(user_id, subscription):
    """Write a row the way the legacy import does: no validation."""
    from user_db import user_db
    from carrac.db_pool import adapt_query
    with user_db.transaction() as conn:
        conn.cursor().execute(adapt_query(
            f'INSERT INTO {push_store.TABLE} (user_id, subscription, created_at) VALUES (?, ?, ?)'),
            (user_id, json.dumps(subscription), push_store._now()))


# ── what may be stored ──────────────────────────────────────────────────────

class TestSubscribeRoute:
    @pytest.fixture
    def member(self, client):
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'a@example.com'
            sess['tier'] = 'member'
        return client

    @pytest.mark.parametrize('endpoint', [
        'http://169.254.169.254/latest/meta-data/',
        'https://169.254.169.254/latest/meta-data/',
        'http://fcm.googleapis.com/fcm/send/abc',
        'https://localhost/push',
        'ftp://fcm.googleapis.com/x',
        '',
        None,
    ])
    def test_unsafe_endpoints_are_rejected(self, member, public_dns, endpoint):
        r = member.post('/api/push/subscribe', json={'subscription': _sub(endpoint=endpoint)})
        assert r.status_code == 400
        assert r.get_json()['error'].startswith('Invalid push subscription')
        assert pn.get_user_subscription('1') is None

    def test_an_endpoint_resolving_to_a_private_address_is_rejected(self, member):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.5', 443))]
        with patch('socket.getaddrinfo', return_value=private):
            r = member.post('/api/push/subscribe', json={'subscription': _sub()})
        assert r.status_code == 400
        assert pn.get_user_subscription('1') is None

    @pytest.mark.parametrize('keys', [
        {},
        {'p256dh': KEYS['p256dh']},
        {'auth': KEYS['auth']},
        {'p256dh': '', 'auth': KEYS['auth']},
        {'p256dh': 123, 'auth': KEYS['auth']},
        None,
    ])
    def test_missing_or_malformed_keys_are_rejected(self, member, public_dns, keys):
        r = member.post('/api/push/subscribe', json={'subscription': _sub(keys=keys)})
        assert r.status_code == 400
        assert 'keys' in r.get_json()['error']
        assert pn.get_user_subscription('1') is None

    def test_a_browser_subscription_is_stored(self, member, public_dns):
        r = member.post('/api/push/subscribe', json={'subscription': _sub()})
        assert r.status_code == 200, r.get_json()
        assert pn.get_user_subscription('1') == {'endpoint': FCM, 'keys': KEYS}

    def test_unsubscribe(self, member, public_dns):
        member.post('/api/push/subscribe', json={'subscription': _sub()})
        assert member.post('/api/push/unsubscribe').status_code == 200
        assert pn.get_user_subscription('1') is None


# ── what may be sent to ─────────────────────────────────────────────────────

class TestSendRefusesAnUnsafeStoredSubscription:
    @pytest.fixture(autouse=True)
    def _vapid(self, monkeypatch):
        monkeypatch.setattr(pn, 'VAPID_PRIVATE_KEY', 'test-private-key')

    def test_a_stored_metadata_endpoint_is_never_posted_to(self, public_dns):
        _store_raw(1, _sub(endpoint='http://169.254.169.254/latest/meta-data/'))
        with patch('pywebpush.webpush') as webpush:
            assert pn.send_push_notification('1', 'title', 'body') is False
        webpush.assert_not_called()

    def test_a_stored_subscription_without_keys_is_never_posted_to(self, public_dns):
        _store_raw(1, {'endpoint': FCM})
        with patch('pywebpush.webpush') as webpush:
            assert pn.send_push_notification('1', 'title', 'body') is False
        webpush.assert_not_called()

    def test_a_valid_subscription_is_posted_to_and_not_rewritten(self, public_dns):
        pn.add_subscription('1', _sub())
        before = _row(1)
        with patch('pywebpush.webpush') as webpush:
            assert pn.send_push_notification('1', 'title', 'body') is True
        assert webpush.call_args.kwargs['subscription_info'] == {'endpoint': FCM, 'keys': KEYS}
        assert _row(1) == before, 'a send must not write the subscription'


def _row(user_id):
    from user_db import user_db
    from carrac.db_pool import adapt_query
    with user_db.transaction() as conn:
        cur = conn.cursor()
        cur.execute(adapt_query(f'SELECT * FROM {push_store.TABLE} WHERE user_id = ?'), (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# ── the store ───────────────────────────────────────────────────────────────

class TestStore:
    def test_one_subscription_per_user_the_latest_wins(self, public_dns):
        pn.add_subscription('1', _sub())
        newer = _sub(endpoint=FCM + '-newer')
        pn.add_subscription('1', newer)
        assert pn.get_user_subscription('1')['endpoint'] == FCM + '-newer'

    def test_delete(self, public_dns):
        pn.add_subscription('1', _sub())
        pn.add_subscription('2', _sub())
        assert pn.remove_subscription('1') is True
        assert pn.remove_subscription('1') is False
        assert pn.get_user_subscription('1') is None
        assert pn.get_user_subscription('2') is not None

    def test_an_unknown_user_is_refused(self, public_dns):
        with pytest.raises(ValueError, match='no user matches'):
            pn.add_subscription('not-a-user', _sub())

    def test_a_failed_write_raises(self, public_dns):
        from user_db import user_db
        with user_db.transaction() as conn:
            conn.cursor().execute(f'DROP TABLE {push_store.TABLE}')
        with pytest.raises(sqlite3.OperationalError):
            pn.add_subscription('1', _sub())

    def test_concurrent_subscribes_of_different_users_all_land(self, public_dns):
        users = [str(i) for i in range(1, 17)]
        start = threading.Barrier(len(users))
        errors = []

        def _subscribe(uid):
            try:
                start.wait(5)
                pn.add_subscription(uid, _sub(endpoint=f'{FCM}-{uid}'))
            except Exception as exc:  # surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=_subscribe, args=(u,)) for u in users]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        missing = [u for u in users if pn.get_user_subscription(u) is None]
        assert missing == [], f'subscriptions dropped by a concurrent write: {missing}'


# ── one-shot import of the old Redis map / JSON file ────────────────────────

LEGACY = {
    '1': {'subscription': _sub(), 'created_at': '2026-09-01T10:00:00', 'last_notified': None},
    '2': {'subscription': _sub(endpoint=FCM + '-2'), 'created_at': '2026-09-02T10:00:00',
          'last_notified': '2026-09-03T10:00:00'},
    'None': {'subscription': _sub(), 'created_at': '2026-09-02T10:00:00', 'last_notified': None},
}


class _FakeRedis:
    def __init__(self, value):
        self.store = {push_store.LEGACY_REDIS_KEY: json.dumps(value).encode()} if value else {}

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


class TestLegacyImport:
    def test_imports_from_redis_retires_both_sources_and_logs(self, _push_db, monkeypatch, caplog):
        redis = _FakeRedis(LEGACY)
        monkeypatch.setattr(push_store, '_redis', lambda: redis)
        mirror = _push_db / 'push_subscriptions.json'
        mirror.write_text(json.dumps({'1': LEGACY['1']}))

        with caplog.at_level(logging.INFO, logger='carrac.alerts.push_store'):
            assert push_store.import_legacy() == 2

        assert pn.get_user_subscription('1') == _sub()
        assert pn.get_user_subscription('2')['endpoint'] == FCM + '-2'
        assert 'last_notified' not in json.dumps(_row(2), default=str)
        assert redis.store == {}
        assert not mirror.exists() and (_push_db / 'push_subscriptions.json.imported').exists()
        assert 'Imported 2 push subscriptions from redis' in caplog.text
        assert "Skipped 1 legacy push subscriptions whose user matches no user: ['None']" in caplog.text

    def test_imports_from_the_file_when_redis_has_nothing(self, _push_db):
        (_push_db / 'push_subscriptions.json').write_text(json.dumps(LEGACY))
        assert push_store.import_legacy() == 2
        assert pn.get_user_subscription('2') is not None

    def test_is_idempotent(self, _push_db, monkeypatch):
        redis = _FakeRedis(LEGACY)
        monkeypatch.setattr(push_store, '_redis', lambda: redis)
        assert push_store.import_legacy() == 2
        redis.store[push_store.LEGACY_REDIS_KEY] = json.dumps(LEGACY).encode()   # a straggler rewrote it
        assert push_store.import_legacy() is None
        assert pn.get_user_subscription('1') == _sub()

    def test_runs_on_first_use(self, _push_db, monkeypatch):
        (_push_db / 'push_subscriptions.json').write_text(json.dumps(LEGACY))
        monkeypatch.setattr(push_store, '_import_checked', False)
        assert pn.get_user_subscription('1') == _sub()

    def test_nothing_to_import(self):
        assert push_store.import_legacy() is None
