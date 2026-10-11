"""Report-save routes do not treat any logged-in session as the internal engine key.

`save_general_report` / `save_weekly_report` / `save_afternoon_report` /
`save_week_ahead_report` accept the internal API key or an admin session. A
free-tier or paid session must not be able to overwrite the shared report
content served on the public /brief page.

This drives the real routes end-to-end (Flask test client, no mocks):
  - a non-admin session (member or premium tier) must be rejected — 403
  - an admin session must still be able to manual-save — 200
  - a valid internal API key (header) must still work — 200
  - the deprecated `?key=` URL fallback must be rejected outright — 400
  - a fully unauthenticated request must still be rejected — 401
"""

from __future__ import annotations

import os

import pytest


_CONTENT = {
    'content': '## The Story\nauth regression test.\n\n'
               '## Market Snapshot\n| Asset | Price |\n| BTC | 100 |\n',
    'assets': ['BTC'],
    'generation_time_ms': 1,
}

_SAVE_ROUTES = [
    '/api/report/general',
    '/api/report/weekly',
    '/api/report/afternoon',
    '/api/report/week_ahead',
    # The PRIMARY route, and the one the earlier fix missed. It feeds
    # the public /brief page and was the only save_* route still accepting any
    # logged-in session as equivalent to the engine key. Its absence from THIS
    # list is precisely why the gap survived — the fix was applied to the four
    # routes the test named. A sweep is only as wide as the list it iterates.
    '/api/report/morning',
]


def _member_session(client, *, tier='member'):
    with client.session_transaction() as sess:
        sess['user_id'] = 7
        sess['email'] = 'member@example.com'
        sess['is_admin'] = False
        sess['tier'] = tier
    return client


def _admin_session(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['email'] = 'admin@example.com'
        sess['is_admin'] = True
        sess['tier'] = 'admin'
    return client


class TestSaveRoutesAdminOnlyFallback:
    """All four save_* routes must reject a non-admin session but keep
    allowing an admin session (legitimate manual save) and the internal key.
    """

    @pytest.mark.parametrize('route', _SAVE_ROUTES)
    def test_non_admin_member_session_rejected(self, client, app, route):
        _member_session(client, tier='member')
        resp = client.post(route, json=_CONTENT)
        assert resp.status_code == 403, f'{route}: {resp.get_data(as_text=True)}'

    @pytest.mark.parametrize('route', _SAVE_ROUTES)
    def test_non_admin_premium_session_rejected(self, client, app, route):
        # Any logged-in user is rejected, not just free tier —
        # a paying-but-non-admin account must be rejected too.
        _member_session(client, tier='premium')
        resp = client.post(route, json=_CONTENT)
        assert resp.status_code == 403, f'{route}: {resp.get_data(as_text=True)}'

    @pytest.mark.parametrize('route', _SAVE_ROUTES)
    def test_admin_session_still_allowed(self, client, app, route, notifications):
        # The only case here that actually SAVES — a successful save pings the
        # operator via Telegram, so the notification guard needs it declared. The 403/401
        # cases never reach the sender.
        _admin_session(client)
        resp = client.post(route, json=_CONTENT)
        assert resp.status_code == 200, f'{route}: {resp.get_data(as_text=True)}'
        assert (resp.get_json() or {}).get('report_id') is not None

    @pytest.mark.parametrize('route', _SAVE_ROUTES)
    def test_unauthenticated_rejected(self, client, app, route):
        resp = client.post(route, json=_CONTENT)
        assert resp.status_code == 401, f'{route}: {resp.get_data(as_text=True)}'


class TestSaveGeneralReportKeyAuth:
    """Focused coverage of the internal-key path on the representative
    /api/report/general route (mirrors across the other three save_* routes
    since they share the exact same auth block)."""

    def test_internal_key_header_allowed(self, client, app):
        key = os.environ['SCREENSHOT_API_KEY']
        resp = client.post(
            '/api/report/general', json=_CONTENT,
            headers={'X-Internal-Key': key},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert (resp.get_json() or {}).get('report_id') is not None

    def test_api_key_header_rejected(self, client, app):
        """The shared key on the public X-API-Key header must
        NOT authenticate this route — only X-Internal-Key does (see
        test_internal_key_header_allowed above)."""
        key = os.environ['SCREENSHOT_API_KEY']
        resp = client.post(
            '/api/report/general', json=_CONTENT,
            headers={'X-API-Key': key},
        )
        assert resp.status_code == 401, resp.get_data(as_text=True)

    def test_url_key_param_rejected(self, client, app):
        # The deprecated `?key=` fallback must be rejected outright — even
        # with a VALID key — not silently authenticated.
        key = os.environ['SCREENSHOT_API_KEY']
        resp = client.post(
            f'/api/report/general?key={key}', json=_CONTENT,
        )
        assert resp.status_code == 400, resp.get_data(as_text=True)

    def test_invalid_key_header_rejected(self, client, app):
        resp = client.post(
            '/api/report/general', json=_CONTENT,
            headers={'X-Internal-Key': 'not-the-right-key'},
        )
        assert resp.status_code == 401, resp.get_data(as_text=True)


def _free_user_api_key(client):
    """Log in as a free-tier user, mint a real per-user ``cvx_k_`` key, then
    drop the session so the request is authenticated by the KEY alone."""
    with client.session_transaction() as sess:
        sess['user_id'] = 2
        sess['email'] = 'free@example.com'
        sess['is_admin'] = False
        sess['tier'] = 'free'
    # Clear leftover keys so the 5-active-key cap isn't tripped across the suite.
    resp = client.get('/api/keys')
    if resp.status_code == 200:
        for k in resp.get_json().get('keys', []):
            if not k.get('revoked'):
                client.delete(f'/api/keys/{k["id"]}')
    resp = client.post('/api/keys', json={'name': 'sec-audit'})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    key = resp.get_json()['key']
    assert key.startswith('cvx_k_')
    with client.session_transaction() as sess:
        sess.clear()
    return key


class TestPerUserKeyRejectedOnEngineRoutes:
    """Engine / credits / shared-report endpoints gate on
    ``verify_admin_key()``. A self-minted per-user API key — which ANY
    free-tier user can create via POST /api/keys — must NOT be accepted as
    engine/admin auth (accepting it would let it read cross-user PII,
    enumerate users, drain another user's AI credits, and deface shared
    report content).
    """

    def test_verify_admin_key_unit(self, app):
        from carrac.auth.decorators import verify_admin_key
        key = os.environ['SCREENSHOT_API_KEY']
        with app.test_request_context(headers={'X-Internal-Key': key}):
            assert verify_admin_key(key) is True
        with app.test_request_context(headers={'X-API-Key': key}):
            assert verify_admin_key(key) is False
        with app.test_request_context():
            assert verify_admin_key('cvx_k_0000000000000000000000000000dead') is False
            assert verify_admin_key('') is False
            assert verify_admin_key(None) is False

    def test_per_user_key_rejected_on_credits_debit(self, client, app):
        key = _free_user_api_key(client)
        resp = client.post('/api/credits/debit',
                           json={'user_id': 1, 'amount_cents': 100},
                           headers={'X-API-Key': key})
        assert resp.status_code == 401, resp.get_data(as_text=True)

    def test_per_user_key_rejected_on_eligible_users(self, client, app):
        key = _free_user_api_key(client)
        resp = client.get('/api/engine/eligible-users', headers={'X-API-Key': key})
        assert resp.status_code == 401, resp.get_data(as_text=True)

    def test_per_user_key_rejected_on_user_sources(self, client, app):
        key = _free_user_api_key(client)
        resp = client.get('/api/engine/user-sources/1', headers={'X-API-Key': key})
        assert resp.status_code == 401, resp.get_data(as_text=True)

    def test_per_user_key_rejected_on_shared_report_save(self, client, app):
        key = _free_user_api_key(client)
        resp = client.post('/api/report/general', json=_CONTENT,
                           headers={'X-API-Key': key})
        assert resp.status_code in (401, 403), resp.get_data(as_text=True)

    def test_shared_admin_key_still_accepted_on_credits_debit(self, client, app):
        # amount_cents<=0 fails validation (400) AFTER auth — proves the shared
        # admin key still passes the auth gate (would be 401 if rejected).
        key = os.environ['SCREENSHOT_API_KEY']
        resp = client.post('/api/credits/debit',
                           json={'user_id': 1, 'amount_cents': 0},
                           headers={'X-Internal-Key': key})
        assert resp.status_code != 401, resp.get_data(as_text=True)
