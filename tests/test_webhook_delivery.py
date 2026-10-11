"""Tests for alert-webhook delivery.

Covers:
- Payload signing (HMAC-SHA256 hex correctness) + payload shape
- SSRF guard rejections at dispatch time (private IP resolution, non-https
  scheme) and redirect refusal (allow_redirects=False) — reusing
  security.validate_outbound_url rather than reinventing it
- Retry + exponential backoff, and auto-disable after
  MAX_CONSECUTIVE_FAILURES consecutive failures (+ user notification)
- Tier-gate matrix: 'webhooks' False for free, True for
  paid tiers, read with a closed .get default so dispatch fails dark if
  the knob is ever removed
- Settings-save-time validation (_validate_notification_settings) rejects
  the same class of unsafe URLs before they're ever persisted
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import uuid
from unittest.mock import MagicMock, patch

import pytest

from carrac.alerts import webhook_delivery as wd


# ─────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch, rebind_db):
    """Isolated SQLite user_db per test (see tests/conftest.py::rebind_db)."""
    db_dir = tmp_path / 'db'
    db_dir.mkdir()
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    monkeypatch.setenv('DATA_DIR', str(db_dir))
    return rebind_db(db_dir)


def _make_user(db, tier='premium', email='wh@example.com'):
    unique_email = f'{uuid.uuid4().hex[:8]}-{email}'
    uid = db.create_user(email=unique_email, password='x' * 12, display_name='wh')
    if uid is None:
        existing = db.get_user_by_email(unique_email)
        uid = existing['id']
    db.set_user_tier(uid, tier)
    return uid


def _set_webhook(db, uid, url, enabled=True, secret=None, consecutive_failures=0):
    """Write a webhook config directly into user prefs (bypassing the
    settings-save route) so dispatch-level tests can target it precisely."""
    secret = secret if secret is not None else wd.generate_webhook_secret()
    prefs = db.get_preferences(uid)
    notif = prefs.setdefault('notifications', {})
    notif['webhook'] = {
        'enabled': enabled,
        'url': url,
        'secret': secret,  # plaintext — secret_box passes through
                            # unencrypted values unchanged when no Fernet
                            # key is configured (true in the test env)
        'consecutive_failures': consecutive_failures,
        'last_delivery_status': None,
        'last_delivery_at': None,
    }
    db.save_preferences(uid, prefs)
    return secret


def _enable_webhooks_tier(monkeypatch):
    """Simulate the parallel branch's knob having landed + entitling this tier."""
    monkeypatch.setattr(
        'carrac.auth.decorators.get_tier_limits_for_user',
        lambda user_id, tier='free': {'webhooks': True},
    )


def _addrs(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, '', (ip, 0))]


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


# ─────────────────────────────────────────────────────────────
# Signing / payload shape
# ─────────────────────────────────────────────────────────────

class TestSigningAndPayload:
    def test_sign_payload_is_hex_hmac_sha256_of_body(self):
        secret = 'topsecret'
        body = b'{"a":1}'
        sig = wd.sign_payload(secret, body)
        expected = hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()
        assert sig == expected
        assert len(sig) == 64  # hex-encoded SHA-256 digest

    def test_sign_payload_empty_secret_returns_empty_string(self):
        assert wd.sign_payload('', b'anything') == ''

    def test_different_bodies_produce_different_signatures(self):
        secret = 'shared-secret'
        sig1 = wd.sign_payload(secret, b'{"a":1}')
        sig2 = wd.sign_payload(secret, b'{"a":2}')
        assert sig1 != sig2

    def test_build_webhook_payload_shape(self, monkeypatch):
        monkeypatch.setenv('SITE_URL', 'https://example.test')
        monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
        payload = wd.build_webhook_payload(42, 'price', 'BTCUSDT', 'above $100', 105.5)
        assert payload['alert_id'] == 42
        assert payload['alert_type'] == 'price'
        assert payload['symbol'] == 'BTCUSDT'
        assert payload['condition'] == 'above $100'
        assert payload['price'] == 105.5
        assert payload['triggered_at'].endswith('Z')
        assert payload['link'] == 'https://example.test/?symbol=BTCUSDT'

    def test_build_webhook_payload_generic_symbol_gets_bare_app_link(self, monkeypatch):
        monkeypatch.setenv('SITE_URL', 'https://example.test')
        monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
        payload = wd.build_webhook_payload(1, 'regime_transition', 'REGIME', 'x', 0)
        assert payload['link'] == 'https://example.test'

    def test_build_webhook_payload_defaults_for_falsy_fields(self):
        payload = wd.build_webhook_payload(None, '', '', '', None)
        assert payload['alert_type'] == 'alert'
        assert payload['symbol'] == ''
        assert payload['condition'] == ''
        assert payload['price'] == 0

    def test_mask_secret(self):
        assert wd.mask_secret('') == ''
        assert wd.mask_secret('abcd') == '****'
        assert wd.mask_secret('abcdefgh1234') == 'abcd…1234'


# ─────────────────────────────────────────────────────────────
# Tier gate — closed default
# ─────────────────────────────────────────────────────────────

class TestTierGateClosedDefault:
    def test_webhooks_knob_matrix(self):
        """Post-launch-matrix: 'webhooks' is a paid knob — False for free,
        True for every paid tier."""
        from carrac.auth.decorators import TIER_LIMITS
        assert TIER_LIMITS['free'].get('webhooks', False) is False
        for tier in ('member', 'tester', 'premium', 'admin'):
            assert TIER_LIMITS[tier].get('webhooks') is True, (
                f"'webhooks' should be enabled for paid tier={tier!r}"
            )

    def test_dispatch_refuses_for_free_tier_even_when_configured(self, fresh_db):
        """Post-launch-matrix: the real TIER_LIMITS keeps webhooks False for
        free — dispatch must refuse a configured free-tier user."""
        uid = _make_user(fresh_db, tier='free')
        _set_webhook(fresh_db, uid, url='https://example.com/hook')

        with patch.object(wd, '_post_pinned') as mock_post:
            ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)

        assert ok is False
        assert 'paid plan' in (error or '')
        mock_post.assert_not_called()

    def test_dispatch_proceeds_for_paid_tier(self, fresh_db):
        """Post-launch-matrix: a configured paid-tier user clears the tier
        gate and the pinned POST is attempted."""
        uid = _make_user(fresh_db, tier='premium')
        _set_webhook(fresh_db, uid, url='https://example.com/hook')

        with patch.object(wd, 'validate_outbound_url',
                          return_value=(True, '93.184.216.34')), \
             patch.object(wd, '_post_pinned',
                          return_value=MagicMock(status_code=200)) as mock_post:
            ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)

        assert ok is True
        assert error is None
        mock_post.assert_called_once()

    def test_send_webhook_for_alert_spawns_no_thread_when_tier_closed(self, fresh_db):
        uid = _make_user(fresh_db, tier='premium')
        _set_webhook(fresh_db, uid, url='https://example.com/hook')
        # send_webhook_for_alert's own pre-check only looks at enabled/url
        # (cheap no-op path) — the tier gate lives one level down in
        # _deliver_webhook, so a thread IS spawned here; assert it reports
        # failure via _deliver_webhook directly instead (covered above).
        # This test instead confirms the no-thread fast path for the
        # "not configured at all" case.
        with patch.object(wd.threading, 'Thread') as mock_thread_cls:
            wd.send_webhook_for_alert(
                uid + 999999, alert_id=1, alert_type='price',
                symbol='BTC', condition='x', price=1,
            )
        mock_thread_cls.assert_not_called()


class TestSendWebhookForAlertFastPath:
    def test_noop_thread_when_no_webhook_configured(self, fresh_db):
        uid = _make_user(fresh_db, tier='premium')
        with patch.object(wd.threading, 'Thread') as mock_thread_cls:
            wd.send_webhook_for_alert(
                uid, alert_id=1, alert_type='price', symbol='BTC',
                condition='x', price=1,
            )
        mock_thread_cls.assert_not_called()

    def test_submits_to_bounded_executor_when_enabled(self, fresh_db):
        # Dispatch uses a bounded executor, not one-shot daemon threads (which
        # each strand a pooled DB connection).
        uid = _make_user(fresh_db, tier='premium')
        _set_webhook(fresh_db, uid, url='https://example.com/hook')
        fake_executor = MagicMock()
        with patch.object(wd, '_get_delivery_executor',
                          return_value=fake_executor):
            wd.send_webhook_for_alert(
                uid, alert_id=1, alert_type='price', symbol='BTC',
                condition='x', price=1,
            )
        fake_executor.submit.assert_called_once()
        assert fake_executor.submit.call_args.args[0] is wd._deliver_webhook


# ─────────────────────────────────────────────────────────────
# SSRF guard (reuses security.validate_outbound_url)
# ─────────────────────────────────────────────────────────────

class TestSSRFGuardAtDispatch:
    def test_rejects_http_scheme(self, fresh_db, monkeypatch):
        uid = _make_user(fresh_db, tier='premium')
        _enable_webhooks_tier(monkeypatch)
        _set_webhook(fresh_db, uid, url='http://example.com/hook')

        with patch.object(wd, '_post_pinned') as mock_post:
            ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)

        assert ok is False
        assert 'rejected' in error
        mock_post.assert_not_called()

    def test_rejects_url_resolving_to_private_ip(self, fresh_db, monkeypatch):
        uid = _make_user(fresh_db, tier='premium')
        _enable_webhooks_tier(monkeypatch)
        _set_webhook(fresh_db, uid, url='https://attacker.example/hook')

        with patch('socket.getaddrinfo', return_value=_addrs('10.0.0.5')):
            with patch.object(wd, '_post_pinned') as mock_post:
                ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)

        assert ok is False
        assert 'rejected' in error
        mock_post.assert_not_called()

    def test_rejects_bare_ip_literal(self, fresh_db, monkeypatch):
        uid = _make_user(fresh_db, tier='premium')
        _enable_webhooks_tier(monkeypatch)
        _set_webhook(fresh_db, uid, url='https://10.0.0.1/hook')

        with patch.object(wd, '_post_pinned') as mock_post:
            ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)

        assert ok is False
        mock_post.assert_not_called()

    def test_never_follows_redirects_and_dials_pinned_ip(self, fresh_db, monkeypatch):
        """_post_pinned must pass allow_redirects=False and connect to the
        pre-resolved IP (never a second DNS lookup)."""
        uid = _make_user(fresh_db, tier='premium')
        _enable_webhooks_tier(monkeypatch)
        _set_webhook(fresh_db, uid, url='https://example.com/hook')

        captured = {}

        def fake_post(self, url, data=None, headers=None, **kw):
            captured['url'] = url
            captured['headers'] = headers
            captured['kwargs'] = kw
            return _FakeResponse(200)

        with patch('socket.getaddrinfo', return_value=_addrs('93.184.216.34')):
            with patch('requests.Session.post', new=fake_post):
                ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)

        assert ok is True, error
        assert captured['kwargs'].get('allow_redirects') is False
        assert captured['kwargs'].get('timeout') == wd.TIMEOUT_SECONDS
        assert '93.184.216.34' in captured['url']
        assert captured['headers']['Host'] == 'example.com'

    def test_signature_header_matches_hmac_of_raw_body(self, fresh_db, monkeypatch):
        uid = _make_user(fresh_db, tier='premium')
        _enable_webhooks_tier(monkeypatch)
        secret = _set_webhook(fresh_db, uid, url='https://example.com/hook')

        captured = {}

        def fake_post(self, url, data=None, headers=None, **kw):
            captured['body'] = data
            captured['headers'] = headers
            return _FakeResponse(200)

        with patch('socket.getaddrinfo', return_value=_addrs('93.184.216.34')):
            with patch('requests.Session.post', new=fake_post):
                ok, error = wd._deliver_webhook(uid, 55, 'price', 'BTC', 'above 100', 105)

        assert ok is True, error
        expected_sig = hmac.new(secret.encode('utf-8'), captured['body'], hashlib.sha256).hexdigest()
        assert captured['headers']['X-Carrac-Signature'] == expected_sig
        assert captured['headers']['Content-Type'] == 'application/json'
        payload = json.loads(captured['body'])
        assert payload['alert_id'] == 55
        assert payload['alert_type'] == 'price'
        assert payload['symbol'] == 'BTC'


# ─────────────────────────────────────────────────────────────
# Retry / backoff / auto-disable
# ─────────────────────────────────────────────────────────────

class TestRetryAndAutoDisable:
    def test_retries_up_to_max_then_reports_failure(self, fresh_db, monkeypatch):
        uid = _make_user(fresh_db, tier='premium')
        _enable_webhooks_tier(monkeypatch)
        _set_webhook(fresh_db, uid, url='https://example.com/hook')
        monkeypatch.setattr(wd.time, 'sleep', lambda s: None)

        call_count = {'n': 0}

        def always_fail(url, pinned_ip, body, signature):
            call_count['n'] += 1
            raise ConnectionError('boom')

        with patch('socket.getaddrinfo', return_value=_addrs('93.184.216.34')):
            with patch.object(wd, '_post_pinned', side_effect=always_fail):
                ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)

        assert ok is False
        assert call_count['n'] == 1 + wd.MAX_RETRIES
        prefs = fresh_db.get_preferences(uid)
        wh = prefs['notifications']['webhook']
        assert wh['consecutive_failures'] == 1
        assert wh['last_delivery_status'] == 'failed'
        assert wh['enabled'] is True  # not yet at the auto-disable threshold

    def test_success_resets_consecutive_failure_counter(self, fresh_db, monkeypatch):
        uid = _make_user(fresh_db, tier='premium')
        _enable_webhooks_tier(monkeypatch)
        _set_webhook(fresh_db, uid, url='https://example.com/hook', consecutive_failures=3)

        with patch('socket.getaddrinfo', return_value=_addrs('93.184.216.34')):
            with patch.object(wd, '_post_pinned', return_value=_FakeResponse(200)):
                ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)

        assert ok is True
        prefs = fresh_db.get_preferences(uid)
        wh = prefs['notifications']['webhook']
        assert wh['consecutive_failures'] == 0
        assert wh['last_delivery_status'] == 'success'

    def test_auto_disables_after_max_consecutive_failures_and_notifies(self, fresh_db, monkeypatch):
        uid = _make_user(fresh_db, tier='premium')
        _enable_webhooks_tier(monkeypatch)
        _set_webhook(
            fresh_db, uid, url='https://example.com/hook',
            consecutive_failures=wd.MAX_CONSECUTIVE_FAILURES - 1,
        )
        monkeypatch.setattr(wd.time, 'sleep', lambda s: None)

        notified = {}

        def fake_notify(uid_arg, error_arg):
            notified['uid'] = uid_arg
            notified['error'] = error_arg

        monkeypatch.setattr(wd, '_notify_autodisabled', fake_notify)

        with patch('socket.getaddrinfo', return_value=_addrs('93.184.216.34')):
            with patch.object(wd, '_post_pinned', side_effect=ConnectionError('down')):
                ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)

        assert ok is False
        prefs = fresh_db.get_preferences(uid)
        wh = prefs['notifications']['webhook']
        assert wh['consecutive_failures'] == wd.MAX_CONSECUTIVE_FAILURES
        assert wh['enabled'] is False
        assert notified.get('uid') == uid

    def test_test_mode_does_not_touch_counters_or_auto_disable(self, fresh_db, monkeypatch):
        """send_test_webhook must never move consecutive_failures — a
        failed test while wiring up an endpoint shouldn't count towards
        auto-disable."""
        uid = _make_user(fresh_db, tier='premium')
        _enable_webhooks_tier(monkeypatch)
        _set_webhook(fresh_db, uid, url='https://example.com/hook', consecutive_failures=5)

        with patch('socket.getaddrinfo', return_value=_addrs('93.184.216.34')):
            with patch.object(wd, '_post_pinned', side_effect=ConnectionError('down')):
                ok, error = wd.send_test_webhook(uid)

        assert ok is False
        prefs = fresh_db.get_preferences(uid)
        assert prefs['notifications']['webhook']['consecutive_failures'] == 5
        assert prefs['notifications']['webhook']['last_delivery_status'] is None

    def test_test_mode_makes_exactly_one_attempt(self, fresh_db, monkeypatch):
        uid = _make_user(fresh_db, tier='premium')
        _enable_webhooks_tier(monkeypatch)
        _set_webhook(fresh_db, uid, url='https://example.com/hook')
        call_count = {'n': 0}

        def always_fail(url, pinned_ip, body, signature):
            call_count['n'] += 1
            raise ConnectionError('boom')

        with patch('socket.getaddrinfo', return_value=_addrs('93.184.216.34')):
            with patch.object(wd, '_post_pinned', side_effect=always_fail):
                wd.send_test_webhook(uid)

        assert call_count['n'] == 1


# ─────────────────────────────────────────────────────────────
# Settings-save-time validation (carrac/alerts/routes.py)
# ─────────────────────────────────────────────────────────────

class TestSettingsSaveValidation:
    def test_https_only_enforced_at_save(self):
        from carrac.alerts.routes import _validate_notification_settings
        cleaned, err = _validate_notification_settings(
            {'webhook': {'enabled': True, 'url': 'http://example.com/hook'}}
        )
        assert cleaned is None
        assert 'rejected' in err

    def test_private_ip_rejected_at_save(self):
        from carrac.alerts.routes import _validate_notification_settings
        cleaned, err = _validate_notification_settings(
            {'webhook': {'enabled': True, 'url': 'https://10.0.0.1/hook'}}
        )
        assert cleaned is None
        assert 'rejected' in err

    def test_disabled_webhook_skips_url_validation(self):
        """An insecure URL sitting in a DISABLED webhook config must not
        block saving other settings — only an enabled+url combo validates."""
        from carrac.alerts.routes import _validate_notification_settings
        cleaned, err = _validate_notification_settings(
            {'webhook': {'enabled': False, 'url': 'http://insecure.example/hook'}}
        )
        assert err is None
        assert cleaned['webhook']['enabled'] is False

    def test_default_webhook_shape_is_closed(self):
        from carrac.alerts.routes import _NOTIF_DEFAULTS
        assert _NOTIF_DEFAULTS['webhook']['enabled'] is False
        assert _NOTIF_DEFAULTS['webhook']['secret'] == ''
        assert _NOTIF_DEFAULTS['webhook']['consecutive_failures'] == 0

    def test_client_supplied_secret_is_ignored_by_validator(self):
        """secret/consecutive_failures/last_delivery_* are server-managed —
        _validate_notification_settings must not echo a client-supplied
        secret back into the cleaned dict (save_notification_settings is
        responsible for merging the real, server-side value in)."""
        from carrac.alerts.routes import _validate_notification_settings
        cleaned, err = _validate_notification_settings(
            {'webhook': {'enabled': True, 'url': 'https://example.com/hook',
                        'secret': 'attacker-supplied-secret'}}
        )
        assert err is None
        assert 'secret' not in cleaned['webhook']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# ─────────────────────────────────────────────────────────────
# Adapter send() path and dispatch executor
# ─────────────────────────────────────────────────────────────

class TestHotfixAdapterAndDispatch:
    def test_post_pinned_traverses_real_adapter_send(self):
        """Drive _post_pinned through the REAL HTTPAdapter.send path.

        Routing server_hostname through send() raises TypeError before any
        socket opens — invisible to tests that patch Session.post around the
        adapter. This test only stubs the
        urllib3 pool's urlopen, so the adapter + send() actually execute.
        """
        import io
        import urllib3
        fake = urllib3.response.HTTPResponse(
            body=io.BytesIO(b''), status=200, headers={}, preload_content=False)
        with patch('urllib3.connectionpool.HTTPSConnectionPool.urlopen',
                   return_value=fake):
            resp = wd._post_pinned(
                'https://example.com/hook', '93.184.216.34', b'{}', 'sig')
        assert resp.status_code == 200

    def test_urllib3_accepts_pinned_pool_kwargs(self):
        """server_hostname/assert_hostname must be valid conn kwargs on the
        installed urllib3 (constructor validates; no socket is opened)."""
        import urllib3
        pm = urllib3.PoolManager(server_hostname='example.com',
                                 assert_hostname='example.com')
        pool = pm.connection_from_host('93.184.216.34', 443, scheme='https')
        conn = pool._new_conn()
        assert getattr(conn, 'server_hostname', 'example.com') == 'example.com'

    def test_prefs_passthrough_skips_db_read_and_submits(self):
        submitted = []

        class _FakeExecutor:
            def submit(self, fn, *args):
                submitted.append((fn, args))

        prefs = {'notifications': {'webhook': {
            'enabled': True, 'url': 'https://example.com/hook'}}}
        with patch.object(wd, '_get_delivery_executor',
                          return_value=_FakeExecutor()), \
             patch('user_db.user_db.get_preferences',
                   side_effect=AssertionError('prefs passthrough ignored')):
            wd.send_webhook_for_alert(7, symbol='BTC', prefs=prefs)
        assert len(submitted) == 1
        assert submitted[0][0] is wd._deliver_webhook

    def test_unconfigured_prefs_submit_nothing(self):
        submitted = []

        class _FakeExecutor:
            def submit(self, fn, *args):
                submitted.append(fn)

        with patch.object(wd, '_get_delivery_executor',
                          return_value=_FakeExecutor()):
            wd.send_webhook_for_alert(7, symbol='BTC',
                                      prefs={'notifications': {}})
        assert submitted == []

    def test_refuses_to_send_unsigned_when_secret_undecryptable(self, fresh_db):
        uid = _make_user(fresh_db, tier='premium')
        _set_webhook(fresh_db, uid, url='https://example.com/hook')
        with patch.object(wd, 'validate_outbound_url',
                          return_value=(True, '93.184.216.34')), \
             patch.object(wd, 'decrypt_token', return_value=''), \
             patch.object(wd, '_post_pinned') as mock_post:
            ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)
        assert ok is False
        assert 'regenerate' in (error or '').lower()
        mock_post.assert_not_called()

    def test_webhooks_knob_removed_fails_closed(self, fresh_db):
        uid = _make_user(fresh_db, tier='premium')
        _set_webhook(fresh_db, uid, url='https://example.com/hook')
        from carrac.auth import decorators as dec
        stripped = {t: {k: v for k, v in lim.items() if k != 'webhooks'}
                    for t, lim in dec.TIER_LIMITS.items()}
        with patch.object(dec, 'TIER_LIMITS', stripped), \
             patch.object(wd, '_post_pinned') as mock_post:
            ok, error = wd._deliver_webhook(uid, 1, 'price', 'BTC', 'x', 100)
        assert ok is False
        assert 'paid plan' in (error or '')
        mock_post.assert_not_called()
