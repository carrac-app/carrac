"""
Auth, session and privacy hardening tests.

change_password invalidates other sessions
/api/ai/chat rate limit keyed on user_id, not remote IP
ContentDatabase writes reject user_id=0; DB-layer ownership still atomic
Sentry send_default_pii=False + before_send scrubs PII
beta_waitlist.jsonl written to data/private/ with 0o600
email-only brute-force counter sends unlock email, not hard-block
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# change_password invalidates other sessions
# ---------------------------------------------------------------------------

class TestChangePasswordInvalidatesSessions:
    """In-app password change must kill other sessions."""

    def test_change_password_calls_invalidate(self, app, auth_client):
        """_invalidate_user_sessions must be called when password is changed."""
        import carrac.auth.routes as auth_routes
        from user_db import user_db as _udb

        called_with = []

        def _fake_invalidate(user_id):
            called_with.append(user_id)


        with app.test_request_context():
            with patch.object(auth_routes, '_invalidate_user_sessions', side_effect=_fake_invalidate) as mock_inv, \
                 patch.object(_udb, 'verify_user_password', return_value=True), \
                 patch.object(_udb, 'update_password', return_value=True):

                with auth_client.session_transaction() as sess:
                    sess['user_id'] = 99
                    sess['email'] = 'pw-test@example.com'

                resp = auth_client.post(
                    '/api/account/change-password',
                    json={
                        'current_password': 'OldP@ssw0rd!1',
                        'new_password': 'N3wStr0ng!Pw#9',
                        'confirm_password': 'N3wStr0ng!Pw#9',
                    },
                    content_type='application/json',
                )
                assert resp.status_code == 200, resp.get_data(as_text=True)
                assert mock_inv.called, "_invalidate_user_sessions was not called"

    def test_change_password_response_ok(self, app, auth_client):
        """Successful change returns ok=True."""
        import carrac.auth.routes as auth_routes
        from user_db import user_db as _udb

        with app.test_request_context():
            with patch.object(auth_routes, '_invalidate_user_sessions'), \
                 patch.object(_udb, 'verify_user_password', return_value=True), \
                 patch.object(_udb, 'update_password', return_value=True):

                with auth_client.session_transaction() as sess:
                    sess['user_id'] = 100
                    sess['email'] = 'pw-test2@example.com'

                resp = auth_client.post(
                    '/api/account/change-password',
                    json={
                        'current_password': 'OldP@ssw0rd!1',
                        'new_password': 'N3wStr0ng!Pw#9',
                        'confirm_password': 'N3wStr0ng!Pw#9',
                    },
                    content_type='application/json',
                )
                data = resp.get_json()
                assert data.get('ok') is True


# ---------------------------------------------------------------------------
# AI rate limit key is user-based, not IP-based
# ---------------------------------------------------------------------------

class TestAIChatRateLimitKey:
    """/api/ai/chat uses user-based rate limit key."""

    def test_rate_limit_key_returns_user_id(self, app):
        """_rate_limit_key_by_user returns 'user:<id>' when session has user_id."""
        from carrac.ai.routes import _rate_limit_key_by_user

        with app.test_request_context():
            from flask import session
            session['user_id'] = 42
            key = _rate_limit_key_by_user()
            assert key == 'user:42'

    def test_rate_limit_key_falls_back_to_ip(self, app):
        """_rate_limit_key_by_user falls back to remote IP when no session."""
        from carrac.ai.routes import _rate_limit_key_by_user

        with app.test_request_context('/'):
            # No session user — should return remote addr (fallback)
            key = _rate_limit_key_by_user()
            # Should NOT be 'user:None' or similar
            assert not key.startswith('user:None')

    def test_chat_endpoint_uses_user_key_func(self):
        """FastAPI /api/ai/chat keys slowapi on user_id.

        The handler moved from Flask to FastAPI; the
        per-user-keyed rate limit moved with it.
        """
        import carrac.fastapi_app.routers.ai as ai_router
        src = open(ai_router.__file__).read()
        assert '_rate_key_by_user' in src, (
            "FastAPI /api/ai/chat must key rate limit on user_id"
        )
        assert 'key_func=_rate_key_by_user' in src

    def test_execute_action_uses_user_key_func(self):
        """/api/ai/execute-action moved from Flask to FastAPI.

        The per-user-keyed slowapi limit moved with it; the invariant
        is preserved by the FastAPI handler.
        """
        import carrac.fastapi_app.routers.ai_aux as ai_aux_router
        src = open(ai_aux_router.__file__).read()
        assert '_rate_key_by_user' in src or 'key_func=_rate_key_by_user' in src, (
            "FastAPI /api/ai/execute-action must key rate limit on user_id"
        )


# ---------------------------------------------------------------------------
# Drawings update/delete reject user_id=0
# ---------------------------------------------------------------------------

class TestDrawingsUserIdZeroRejected:
    """ContentDatabase refuses a write with no real user (0 or None)."""

    def _make_content_db(self, tmp_path, monkeypatch):
        """Return an isolated ContentDatabase."""
        monkeypatch.setenv('DB_TYPE', 'sqlite')
        import carrac.db_pool as _dp
        _dp._db_type = None
        from carrac.content_db import ContentDatabase
        return ContentDatabase(db_path=str(tmp_path / 'drawings_test.db'))

    def test_db_update_drawing_rejects_zero(self, tmp_path, monkeypatch):
        """ContentDatabase.drawing_update asserts user_id != 0."""
        db = self._make_content_db(tmp_path, monkeypatch)
        did = db.save_drawing(
            asset='BTC', timeframe='1D',
            drawing_type='horizontal_line',
            price_level=50000.0, user_id=7,
        )
        with pytest.raises(AssertionError):
            db.drawing_update(did, user_id=0, price_level=99999.0)

    def test_db_update_drawing_rejects_none(self, tmp_path, monkeypatch):
        """ContentDatabase.drawing_update asserts user_id is not None."""
        db = self._make_content_db(tmp_path, monkeypatch)
        did = db.save_drawing(
            asset='BTC', timeframe='1D',
            drawing_type='horizontal_line',
            price_level=50000.0, user_id=7,
        )
        with pytest.raises(AssertionError):
            db.drawing_update(did, user_id=None, price_level=99999.0)

    def test_db_update_drawing_atomic_ownership(self, tmp_path, monkeypatch):
        """drawing_update with wrong user returns UPDATE_NOT_FOUND, not UPDATE_OK."""
        db = self._make_content_db(tmp_path, monkeypatch)
        did = db.save_drawing(
            asset='BTC', timeframe='1D',
            drawing_type='horizontal_line',
            price_level=50000.0, user_id=7,
        )
        status, _ = db.drawing_update(did, user_id=8, price_level=99999.0)
        assert status == db.UPDATE_NOT_FOUND, (
            "drawing_update allowed user 8 to modify user 7's drawing"
        )


# ---------------------------------------------------------------------------
# Sentry send_default_pii=False
# ---------------------------------------------------------------------------

class TestSentryPIIDisabled:
    """Sentry must not auto-attach PII."""

    def test_sentry_init_uses_no_pii(self):
        """carrac/__init__.py must pass send_default_pii=False to sentry_sdk.init."""
        init_src = open(
            str(ROOT / 'carrac' / '__init__.py')
        ).read()
        assert 'send_default_pii=False' in init_src, (
            "send_default_pii is not False in Sentry init — PII scrubbing not applied"
        )
        assert 'send_default_pii=True' not in init_src, (
            "send_default_pii=True still present in Sentry init"
        )

    def test_before_send_hook_registered(self):
        """A before_send hook must be passed to sentry_sdk.init."""
        init_src = open(
            str(ROOT / 'carrac' / '__init__.py')
        ).read()
        assert 'before_send=' in init_src, (
            "No before_send hook in Sentry init — PII scrubbing not active"
        )

    def test_before_send_scrubs_pii(self, app):
        """_sentry_before_send strips Authorization header, cookies, and key= params.

        The scrubber is module-level, so the real function is tested
        directly. (Scraping the source and exec'ing a window after _PII_KEYS
        would break the moment another constant was added next to it.)
        """
        from carrac import _sentry_before_send

        event = {
            'request': {
                'headers': {
                    'Authorization': 'Bearer ' + 'fake-test-value-xyz',
                    'Content-Type': 'application/json',
                },
                'cookies': {'session': 'abc123'},
                'query_string': 'key=secret123&asset=BTC',
            }
        }
        result = _sentry_before_send(event, {})
        hdrs = result['request']['headers']
        assert hdrs.get('Authorization') == '[Filtered]', (
            "Authorization header not scrubbed by before_send"
        )
        assert hdrs.get('Content-Type') == 'application/json', (
            "non-PII header was scrubbed — the filter is too broad"
        )
        assert result['request'].get('cookies') == '[Filtered]', (
            "cookies not scrubbed by before_send"
        )
        qs = result['request'].get('query_string', '')
        assert 'secret123' not in qs, "key= value not scrubbed from query_string"
        assert 'asset=BTC' in qs, "non-sensitive query param was destroyed"

    def test_set_user_hook_omits_email(self, monkeypatch, app, client):
        """Privacy fix: the _set_request_id before_request hook must call
        sentry_sdk.set_user() WITHOUT an 'email' key. Account email is PII
        the privacy policy says error tracking never carries.
        """
        import carrac
        import sentry_sdk

        # The hook only fires when the module-level _SENTRY_DSN is truthy.
        monkeypatch.setattr(carrac, '_SENTRY_DSN', 'https://fake@bugsink.example/1')
        monkeypatch.setenv('SENTRY_BROWSER_DSN', 'https://pub@bugsink.example/1')

        captured = {}

        def _fake_set_user(payload):
            captured.update(payload)

        monkeypatch.setattr(sentry_sdk, 'set_user', _fake_set_user)

        with client.session_transaction() as sess:
            sess['user_id'] = 'test-user'
            sess['email'] = 'leaky-pii@example.com'
            sess['tier'] = 'member'

        client.get('/')

        assert captured, "sentry_sdk.set_user was never invoked — hook not exercised"
        assert 'email' not in captured, (
            f"email leaked into Sentry set_user payload: {captured}"
        )
        assert captured.get('id') == 'test-user'
        assert captured.get('tier') == 'member'


# ---------------------------------------------------------------------------
# Error-tunnel must log forwarding failures instead of failing silently.
# ---------------------------------------------------------------------------

class TestErrorTunnelLogsFailures:
    """/api/error-tunnel forwards to the error tracker; a broken pipeline must not be
    invisible. Client contract (always 200, size guards) is unchanged.
    """

    def _make_tunnel_app(self, app):
        """/api/error-tunnel is only registered when _SENTRY_DSN was truthy at
        create_app() call time (module-level `if _SENTRY_DSN:` block). The
        `app` fixture's app was built with the real (test-env, unset) DSN, so
        it never got the route. Reuse the `app` fixture's already-applied
        monkeypatches (DATA_DIR, rebind_user_db, OHLCVDatabase.__init__) and
        build a second app with the DSN gate flipped on.
        """
        from carrac import create_app
        from carrac.config import TestConfig
        tunnel_app = create_app(TestConfig)
        tunnel_app.config['WTF_CSRF_ENABLED'] = False
        return tunnel_app

    def test_tunnel_returns_200_and_warns_on_forward_exception(self, monkeypatch, app):
        import carrac
        import requests

        monkeypatch.setattr(carrac, '_SENTRY_DSN', 'https://fake@bugsink.example/1')
        monkeypatch.setenv('SENTRY_BROWSER_DSN', 'https://pub@bugsink.example/1')
        tunnel_client = self._make_tunnel_app(app).test_client()

        def _raise_post(*a, **kw):
            raise ConnectionError("Bugsink unreachable (test)")

        monkeypatch.setattr(requests, 'post', _raise_post)

        warnings_logged = []
        monkeypatch.setattr(
            carrac.logger, 'warning',
            lambda *a, **kw: warnings_logged.append((a, kw)),
        )

        resp = tunnel_client.post(
            '/api/error-tunnel',
            data=b'{"dsn":"https://pub@bugsink.example/1"}\n{}',
            content_type='application/x-sentry-envelope',
        )

        assert resp.status_code == 200, "client contract broken — tunnel must always return 200"
        assert warnings_logged, "forward exception was swallowed silently — no logger.warning call"

    def test_tunnel_returns_200_and_warns_on_non_2xx_from_bugsink(self, monkeypatch, app):
        import carrac
        import requests
        from unittest.mock import MagicMock

        monkeypatch.setattr(carrac, '_SENTRY_DSN', 'https://fake@bugsink.example/1')
        monkeypatch.setenv('SENTRY_BROWSER_DSN', 'https://pub@bugsink.example/1')
        tunnel_client = self._make_tunnel_app(app).test_client()

        fake_resp = MagicMock()
        fake_resp.status_code = 500
        fake_resp.text = 'internal error'
        monkeypatch.setattr(requests, 'post', lambda *a, **kw: fake_resp)

        warnings_logged = []
        monkeypatch.setattr(
            carrac.logger, 'warning',
            lambda *a, **kw: warnings_logged.append((a, kw)),
        )

        resp = tunnel_client.post(
            '/api/error-tunnel',
            data=b'{"dsn":"https://pub@bugsink.example/1"}\n{}',
            content_type='application/x-sentry-envelope',
        )

        assert resp.status_code == 200, "client contract broken — tunnel must always return 200"
        assert warnings_logged, "non-2xx Bugsink response was swallowed silently — no logger.warning call"


# ---------------------------------------------------------------------------
# beta_waitlist.jsonl in data/private/ with 0o600
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("notifications")
class TestBetaWaitlistPrivateDir:
    """Waitlist file must be in data/private/ with restricted perms.

    `_log_beta_waitlist` also pings the operator (Telegram + ntfy); these
    tests are about the file, but they call the real function, so the
    `notifications` fixture captures the send instead of letting it out.
    """

    def test_waitlist_written_to_private_subdir(self, tmp_path, monkeypatch):
        """_log_beta_waitlist writes to data/private/beta_waitlist.jsonl."""
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        import carrac.auth.routes as ar
        # Re-import to pick up new DATA_DIR
        ar._log_beta_waitlist('test@example.com', '1.2.3.4')
        private_dir = tmp_path / 'private'
        assert private_dir.exists(), "data/private/ was not created"
        jsonl = private_dir / 'beta_waitlist.jsonl'
        assert jsonl.exists(), "beta_waitlist.jsonl not in data/private/"

    def test_waitlist_file_permissions(self, tmp_path, monkeypatch):
        """beta_waitlist.jsonl must have mode 0o600."""
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        import carrac.auth.routes as ar
        ar._log_beta_waitlist('perm@example.com', '1.2.3.4')
        jsonl = tmp_path / 'private' / 'beta_waitlist.jsonl'
        # Check permissions (skip on Windows — no meaningful chmod)
        if os.name != 'nt':
            mode = oct(jsonl.stat().st_mode & 0o777)
            assert mode == oct(0o600), (
                f"beta_waitlist.jsonl has mode {mode}, expected 0o600"
            )

    def test_private_dir_permissions(self, tmp_path, monkeypatch):
        """data/private/ must have mode 0o700."""
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        import carrac.auth.routes as ar
        ar._log_beta_waitlist('dirperm@example.com', '1.2.3.4')
        private_dir = tmp_path / 'private'
        if os.name != 'nt':
            mode = oct(private_dir.stat().st_mode & 0o777)
            assert mode == oct(0o700), (
                f"data/private/ has mode {mode}, expected 0o700"
            )

    def test_waitlist_contains_correct_entry(self, tmp_path, monkeypatch):
        """Written JSONL must contain the email and IP."""
        import json as _json
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        import carrac.auth.routes as ar
        ar._log_beta_waitlist('check@example.com', '10.0.0.1')
        jsonl = tmp_path / 'private' / 'beta_waitlist.jsonl'
        lines = jsonl.read_text().strip().splitlines()
        assert len(lines) >= 1
        entry = _json.loads(lines[-1])
        assert entry['email'] == 'check@example.com'
        assert entry['ip'] == '10.0.0.1'

    def test_telegram_ping_carries_no_email_address(self, tmp_path, monkeypatch,
                                                    notifications):
        """Telegram is a third-country processor with no DPA, and it
        was receiving every signup address in plaintext — undisclosed. The alert's
        job is 'someone signed up', not 'who'. The address stays in the JSONL and
        in ntfy (self-hosted, the operator's own infrastructure); only the Telegram hop
        loses it, which removes the transfer rather than documenting it.
        """
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        import carrac.auth.routes as ar
        ar._log_beta_waitlist('leaky@example.com', '10.0.0.1')

        tg = [n for n in notifications if 'telegram' in n['sender'].lower()]
        assert tg, "expected a Telegram send — the operator ping must still fire"
        for n in tg:
            blob = f"{n['args']}{n['kwargs']}"
            assert 'leaky@example.com' not in blob, (
                f"Telegram send still carries the address: {blob}"
            )
            assert '@' not in blob, (
                f"Telegram send looks like it still carries an address: {blob}"
            )

    def test_feedback_telegram_carries_neither_address_nor_body(self, monkeypatch,
                                                                notifications):
        """Sibling sweep. Fixing the waitlist ping alone would have
        left this one leaking: `_notify_feedback_submission` was sending the
        submitter's email AND up to 300 characters of their own words to
        api.telegram.org. Found by grepping every send_telegram_message call
        site rather than assuming the reported one was the only one.

        Billing and reports were checked the same way and interpolate no
        personal data — they send amounts, plan names and report ids.
        """
        from carrac.admin import routes as ar
        monkeypatch.setattr(ar.user_db, 'get_user', lambda _uid: {'tier': 'member'})

        secret_body = 'my private complaint about the charting UX'
        ar._notify_feedback_submission(
            fb_id=42, fb_type='bug', area='charts', description=secret_body,
            session={'email': 'submitter@example.com', 'user_id': 1},
        )

        tg = [n for n in notifications if 'telegram' in n['sender'].lower()]
        assert tg, "expected a Telegram send — the operator ping must still fire"
        blob = ''.join(f"{n['args']}{n['kwargs']}" for n in tg)
        assert 'submitter@example.com' not in blob, f"address leaked: {blob}"
        assert secret_body not in blob, f"user's own words leaked: {blob}"
        # Still useful to the operator.
        assert '42' in blob and 'charts' in blob

    def test_ntfy_still_carries_the_address(self, tmp_path, monkeypatch,
                                            notifications):
        """The control for the test above. ntfy is self-hosted on a
        private network, so it is not a third-party transfer and the
        operator still needs to know WHO signed up. A change that scrubbed both
        would have quietly cost the operator the signal."""
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        import carrac.auth.routes as ar
        ar._log_beta_waitlist('kept@example.com', '10.0.0.1')

        nt = [n for n in notifications if 'ntfy' in n['sender'].lower()]
        assert nt, "expected an ntfy send"
        assert any('kept@example.com' in f"{n['args']}{n['kwargs']}" for n in nt), (
            "ntfy no longer carries the address — the operator lost the signal"
        )


# ---------------------------------------------------------------------------
# Email-only brute-force sends unlock email, not hard-block
# ---------------------------------------------------------------------------

class TestBruteForceEmailOnlyUnlock:
    """Email-only counter triggers unlock email, not hard lockout."""

    @pytest.fixture
    def bf(self):
        """Fresh BruteForceProtection instance (no Redis)."""
        from security import BruteForceProtection
        return BruteForceProtection()

    def test_email_only_threshold_does_not_hard_block(self, bf):
        """After EMAIL_ONLY_THRESHOLD failed attempts from many IPs,
        is_locked should still return False (no hard block on email-only)."""
        from security import EMAIL_ONLY_THRESHOLD
        email = f"{uuid.uuid4().hex[:8]}-unlock@example.com"
        # Fire attempts from different IPs to trip email-only counter but
        # not any single (email+IP) counter.
        for i in range(EMAIL_ONLY_THRESHOLD + 1):
            # Only call _record_email_only_attempt directly to isolate
            bf._record_email_only_attempt(bf._get_email_key(email), email)
        # is_locked checks (email+IP) key only now — should not be locked
        locked, _ = bf.is_locked(email, '10.1.1.1')
        assert not locked, (
            "Email-only counter caused a hard lockout"
        )

    def test_email_only_threshold_fires_callback(self, bf):
        """When email-only threshold is hit, the unlock callback is called."""
        import security as sec
        from security import EMAIL_ONLY_THRESHOLD

        fired = []
        original_callbacks = list(sec._email_unlock_callbacks)
        try:
            sec._email_unlock_callbacks.clear()
            sec._email_unlock_callbacks.append(lambda e: fired.append(e))

            email = f"{uuid.uuid4().hex[:8]}-cb@example.com"
            email_key = bf._get_email_key(email)
            for _ in range(EMAIL_ONLY_THRESHOLD + 1):
                bf._record_email_only_attempt(email_key, email)

            assert len(fired) >= 1, (
                "Unlock callback was not fired when email-only threshold was reached"
            )
            assert fired[0] == email
        finally:
            sec._email_unlock_callbacks.clear()
            sec._email_unlock_callbacks.extend(original_callbacks)

    def test_clear_email_only_counter(self, bf):
        """clear_email_only_counter removes the email-only tracking data."""
        email = f"{uuid.uuid4().hex[:8]}-clear@example.com"
        email_key = bf._get_email_key(email)
        # Record some attempts
        for _ in range(5):
            bf._record_email_only_attempt(email_key, email)
        # Clear
        bf.clear_email_only_counter(email)
        # After clear the in-memory attempts list should be gone
        assert email_key not in bf._attempts or len(bf._attempts[email_key]) == 0

    def test_ip_pair_still_hard_blocks(self, bf):
        """Short lockout for (email+IP) pair must still hard-block."""
        from security import MAX_LOGIN_ATTEMPTS
        email = f"{uuid.uuid4().hex[:8]}-ipblock@example.com"
        ip = '1.2.3.4'
        for _ in range(MAX_LOGIN_ATTEMPTS + 1):
            bf.record_attempt(email, ip)
        locked, remaining = bf.is_locked(email, ip)
        assert locked, "Per-(email+IP) lockout no longer hard-blocks"

    def test_constants_updated(self):
        """EMAIL_ONLY_THRESHOLD and EMAIL_ONLY_WINDOW_MINUTES must be present."""
        from security import EMAIL_ONLY_THRESHOLD, EMAIL_ONLY_WINDOW_MINUTES
        assert EMAIL_ONLY_THRESHOLD >= 20, (
            f"EMAIL_ONLY_THRESHOLD={EMAIL_ONLY_THRESHOLD} is too low"
        )
        assert EMAIL_ONLY_WINDOW_MINUTES >= 30, (
            f"EMAIL_ONLY_WINDOW_MINUTES={EMAIL_ONLY_WINDOW_MINUTES} is too low"
        )

    def test_unlock_account_endpoint_exists(self, app):
        """GET /unlock-account/<token> must be a registered route."""
        rules = [str(r) for r in app.url_map.iter_rules()]
        assert any('unlock-account' in r for r in rules), (
            "unlock_account route not registered — unlock-email flow incomplete"
        )


# ---------------------------------------------------------------------------
# Sentry browser SDK must be self-hosted (no third-party CDN)
# ---------------------------------------------------------------------------

class TestSentrySelfHosted:
    """The browser SDK is vendored — no browser.sentry-cdn.com anywhere."""

    def test_csp_has_no_sentry_cdn(self, client):
        """CSP header must not allow-list browser.sentry-cdn.com in any directive."""
        resp = client.get('/')
        csp = resp.headers.get('Content-Security-Policy', '')
        assert csp, "Content-Security-Policy header missing from response"
        assert 'browser.sentry-cdn.com' not in csp, (
            "CSP still allow-lists browser.sentry-cdn.com — SDK should be self-hosted"
        )

    def test_index_template_loads_vendored_sdk(self):
        """index.html must load the SDK from static/js/vendor/, not the CDN."""
        tpl = (ROOT / 'templates' / 'index.html').read_text()
        assert 'browser.sentry-cdn.com' not in tpl, (
            "index.html still loads the Sentry SDK from the third-party CDN"
        )
        assert 'js/vendor/sentry.bundle.min.js' in tpl, (
            "index.html does not reference the vendored Sentry bundle"
        )
        assert "cache_version('js/vendor/sentry.bundle.min.js')" in tpl, (
            "vendored Sentry bundle is not cache-busted with ?v={{ cache_version(...) }}"
        )

    def test_vendored_bundle_present_and_authentic(self):
        """The vendored bundle must exist so cache_version doesn't fall back to time()."""
        bundle = ROOT / 'static' / 'js' / 'vendor' / 'sentry.bundle.min.js'
        assert bundle.exists(), "static/js/vendor/sentry.bundle.min.js missing"
        head = bundle.read_text(errors='replace')[:200]
        assert '@sentry/browser 8.48.0' in head, (
            "vendored bundle is not the pinned @sentry/browser 8.48.0 build"
        )


# ---------------------------------------------------------------------------
# Account deletion must kill the session — no ghost access
# ---------------------------------------------------------------------------

class TestDeleteAccountKillsSession:
    """After POST /api/account/delete the same cookie must be dead."""

    EMAIL = 'del-e2e@example.com'
    PASSWORD = 'StrongPass!123'

    def _create_verified_user(self):
        from user_db import user_db as udb
        # Same schema patch as tests/test_user_db_real.py's db fixture —
        # anonymize_user touches columns the base test schema lacks.
        conn = udb._get_conn()
        cur = conn.cursor()
        for ddl in (
            "ALTER TABLE users ADD COLUMN trader_profile TEXT",
            "ALTER TABLE users ADD COLUMN subscription_expires_at TIMESTAMP",
        ):
            try:
                cur.execute(ddl)
            except Exception:
                pass
        conn.commit()
        uid = udb.create_user(self.EMAIL, self.PASSWORD, tos_accepted=True)
        assert uid, "test user creation failed"
        # NB: set_email_verified's return reflects its token-table UPDATE
        # (0 rows for a fresh test user) — assert the actual flag instead.
        udb.set_email_verified(uid)
        assert udb.is_email_verified(uid), "email_verified flag not set"
        return uid

    def _login(self, client):
        resp = client.post('/login', data={
            'email': self.EMAIL, 'password': self.PASSWORD,
        })
        assert resp.status_code == 302, (
            f"real HTTP login failed: {resp.status_code} {resp.data[:200]!r}"
        )

    def test_delete_account_invalidates_current_session(self, app, client):
        """Sequential path: login → delete → same cookie must be unauthenticated."""
        self._create_verified_user()
        self._login(client)

        # Sanity: session is live before deletion
        r = client.get('/api/tier-info')
        assert r.status_code == 200, "authed sanity request failed pre-delete"

        r = client.post('/api/account/delete', json={
            'password': self.PASSWORD, 'confirm': 'DELETE MY ACCOUNT',
        })
        assert r.status_code == 200, f"delete failed: {r.status_code} {r.data[:200]!r}"

        # Ghost check: the same client (same cookie jar) must NOT be authed
        r = client.get('/settings')
        assert r.status_code in (301, 302, 401), (
            f"/settings still reachable after account deletion (got {r.status_code})"
        )
        r = client.get('/api/tier-info')
        assert r.status_code in (301, 302, 401), (
            f"/api/tier-info still authed after account deletion (got {r.status_code})"
        )

    def test_inflight_request_cannot_resurrect_deleted_session(self, app, client):
        """Race: a request that OPENED the session before deletion
        and SAVES after it (SESSION_REFRESH_EACH_REQUEST=True re-saves every
        response) re-upserts the full blob — user_id included — under the same
        sid. The same cookie must still be dead afterwards."""
        import time as _time
        from flask import request as flask_request

        self._create_verified_user()
        self._login(client)
        cookie = client.get_cookie('session')
        assert cookie is not None, "no session cookie after login"
        sid_cookie = cookie.value

        # In-flight request B opens the live session BEFORE the delete
        with app.test_request_context('/', headers={'Cookie': f'session={sid_cookie}'}):
            sess_b = app.session_interface.open_session(app, flask_request)
        assert sess_b.get('user_id'), "request B did not load the live session"

        # Request A: the account is deleted (session cleared + invalidated)
        r = client.post('/api/account/delete', json={
            'password': self.PASSWORD, 'confirm': 'DELETE MY ACCOUNT',
        })
        assert r.status_code == 200, f"delete failed: {r.status_code}"

        # Request B finishes AFTER the delete: per-request writers touch the
        # session, save_session re-upserts the blob under the same sid and
        # re-issues the cookie the browser just had expired.
        sess_b['_last_active'] = _time.time()
        resp_b = app.response_class()
        with app.test_request_context('/', headers={'Cookie': f'session={sid_cookie}'}):
            app.session_interface.save_session(app, sess_b, resp_b)
        client.set_cookie('session', sid_cookie)

        r = client.get('/settings')
        assert r.status_code in (301, 302, 401), (
            f"ghost session: /settings reachable after deletion via the "
            f"resurrection race (got {r.status_code})"
        )
        r = client.get('/api/tier-info')
        assert r.status_code in (301, 302, 401), (
            f"ghost session: /api/tier-info authed after deletion via the "
            f"resurrection race (got {r.status_code})"
        )


class TestApiDocsCsp:
    """/api/docs loads swagger-ui.css from unpkg, so style-src must allow it."""

    def test_style_src_allows_unpkg_for_swagger_ui(self, client):
        resp = client.get('/api/docs')
        assert resp.status_code == 200
        csp = resp.headers.get('Content-Security-Policy', '')
        style_src = next(d for d in csp.split(';') if d.strip().startswith('style-src'))
        assert 'https://unpkg.com' in style_src
        assert 'swagger-ui-dist' in resp.get_data(as_text=True)

    def test_fastapi_csp_matches(self):
        from carrac.fastapi_app import middleware
        style_src = next(d for d in middleware._CSP.decode().split(';')
                         if d.strip().startswith('style-src'))
        assert 'https://unpkg.com' in style_src
