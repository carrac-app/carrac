"""Tests for the /brief email-capture band.

`POST /api/brief-subscribe` reuses the beta-waitlist infra
(`user_db.add_to_waitlist` + `on_waitlist_join`), tagged with
source='brief-digest' so it's distinguishable from the register-page
waitlist in the admin /admin/waitlist view. This is a "be first" list, not
a live subscription — delivery doesn't exist yet, so the /brief copy is
deliberately honest about that.

The template band (`templates/morning_report_editorial.html`) must render
ONLY under the `public_brief` guard — never on the authed `/report/morning`
page (same template, different route).
"""
from __future__ import annotations

from pathlib import Path

import pytest


EDITORIAL = Path("templates/morning_report_editorial.html").read_text()


# ---------------------------------------------------------------------------
# Template: band guarded under public_brief, honest copy, nonce'd JS
# ---------------------------------------------------------------------------

class TestSubscribeBandTemplateGuard:
    def test_band_guarded_by_public_brief(self):
        # The band markup + its script must both sit inside the SAME
        # public_brief-gated block (a single {% if %} ... {% endif %} span).
        start = EDITORIAL.index('id="ed-subscribe"')
        guard_idx = EDITORIAL.rindex("{% if public_brief is defined and public_brief %}", 0, start)
        endif_idx = EDITORIAL.index("{% endif %}", start)
        assert guard_idx < start < endif_idx
        # The fetch call to the new endpoint must be inside that same span.
        fetch_idx = EDITORIAL.index("/api/brief-subscribe")
        assert guard_idx < fetch_idx < endif_idx

    def test_band_has_honest_be_first_copy(self):
        assert ("Want this brief in your inbox? Email delivery is coming — "
                "leave your address and be first.") in EDITORIAL

    def test_band_copy_has_no_fake_urgency_or_advice_language(self):
        # Grab just the band's own paragraph text (not the whole file) so this
        # doesn't false-positive on unrelated copy elsewhere in the template.
        band_start = EDITORIAL.index('id="ed-subscribe"')
        band_end = EDITORIAL.index('subscribe-msg', band_start) + 200
        band_html = EDITORIAL[band_start:band_end].lower()
        for banned in ("limited time", "act now", "hurry", "you should",
                       "recommend", "buy", "sell", "guaranteed"):
            assert banned not in band_html

    def test_band_script_is_nonced(self):
        start = EDITORIAL.index('id="ed-subscribe"')
        script_idx = EDITORIAL.index("<script", start)
        script_tag_end = EDITORIAL.index(">", script_idx)
        script_open_tag = EDITORIAL[script_idx:script_tag_end]
        assert 'nonce="{{ csp_nonce | default(\'\') }}"' in script_open_tag

    def test_success_state_swaps_to_on_the_list(self):
        assert "You're on the list." in EDITORIAL

    def test_band_posts_json_to_brief_subscribe_endpoint(self):
        assert "/api/brief-subscribe" in EDITORIAL
        assert "method: 'POST'" in EDITORIAL


# ---------------------------------------------------------------------------
# Template: band absent when public_brief is not set (the authed page)
# ---------------------------------------------------------------------------

class TestSubscribeBandAbsentOnAuthedReport:
    @pytest.fixture(autouse=True)
    def _no_redis(self, monkeypatch):
        import carrac.redis_cache as rc
        monkeypatch.setattr(rc, 'get_redis', lambda: None)

    @pytest.fixture(autouse=True)
    def _fast_quant_db_miss(self, monkeypatch):
        import carrac.db_pool as db_pool

        def _raise(*a, **k):
            raise RuntimeError('no quant DB in this test environment')

        monkeypatch.setattr(db_pool, 'init_quant_pool', _raise)

    def test_admin_editorial_preview_has_no_subscribe_band(self, admin_client):
        """Admin ?editorial=1 preview renders the SAME template without
        public_brief — the band, its id, and its endpoint must be absent."""
        resp = admin_client.get('/report/morning?editorial=1')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="ed-subscribe"' not in html
        assert '/api/brief-subscribe' not in html
        assert "You're on the list." not in html

    def test_public_brief_route_has_the_band(self, client):
        """Sanity check the other direction: /brief (public_brief=True) DOES
        render the band."""
        resp = client.get('/brief')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert 'id="ed-subscribe"' in html
        assert '/api/brief-subscribe' in html


# ---------------------------------------------------------------------------
# Route: POST /api/brief-subscribe
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("notifications")
class TestBriefSubscribeRoute:
    """The route genuinely notifies the operator on every capture
    (`_log_beta_waitlist` fires Telegram + ntfy), so these exercise a real
    send path. The `notifications` fixture declares that and captures it —
    without it the suite pushed a "beta waitlist" alert per test run."""

    def test_valid_email_returns_ok(self, client):
        resp = client.post('/api/brief-subscribe', json={'email': 'brief_sub@test.com'})
        assert resp.status_code == 200
        assert resp.get_json() == {'ok': True}

    def test_valid_email_logs_to_waitlist_with_brief_digest_source(self, client):
        client.post('/api/brief-subscribe', json={'email': 'brief_source@test.com'})
        from user_db import user_db
        rows = user_db.get_waitlist()
        row = next((r for r in rows if r['email'] == 'brief_source@test.com'), None)
        assert row is not None
        assert row['source'] == 'brief-digest'

    def test_form_encoded_body_also_works(self, client):
        resp = client.post('/api/brief-subscribe', data={'email': 'brief_form@test.com'})
        assert resp.status_code == 200
        assert resp.get_json() == {'ok': True}
        from user_db import user_db
        picked = [p['email'] for p in user_db.pick_next_waitlist(200)]
        assert 'brief_form@test.com' in picked

    def test_invalid_email_returns_400(self, client):
        resp = client.post('/api/brief-subscribe', json={'email': 'not-an-email'})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body['ok'] is False
        assert 'error' in body

    def test_missing_email_returns_400(self, client):
        resp = client.post('/api/brief-subscribe', json={})
        assert resp.status_code == 400
        assert resp.get_json()['ok'] is False

    def test_no_body_returns_400_not_500(self, client):
        resp = client.post('/api/brief-subscribe')
        assert resp.status_code == 400
        assert resp.get_json()['ok'] is False

    def test_email_is_lowercased_and_trimmed(self, client):
        client.post('/api/brief-subscribe', json={'email': '  Brief_Case@Test.com  '})
        from user_db import user_db
        picked = [p['email'] for p in user_db.pick_next_waitlist(200)]
        assert 'brief_case@test.com' in picked

    def test_duplicate_post_is_idempotent_no_crash(self, client):
        r1 = client.post('/api/brief-subscribe', json={'email': 'brief_dupe@test.com'})
        r2 = client.post('/api/brief-subscribe', json={'email': 'brief_dupe@test.com'})
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.get_json() == {'ok': True}
        assert r2.get_json() == {'ok': True}
        from user_db import user_db
        rows = [r for r in user_db.get_waitlist() if r['email'] == 'brief_dupe@test.com']
        assert len(rows) == 1

    def test_notification_failures_are_non_fatal(self, client, monkeypatch):
        """Telegram/ntfy side-effects (fired inside `_log_beta_waitlist`) must
        never turn a subscribe attempt into a 500 — same non-fatal posture as
        the register-page waitlist capture; the route wraps all of this."""
        def _boom(*a, **k):
            raise RuntimeError('notification channel down')

        import carrac.notifications.telegram as tg
        import carrac.notifications.ntfy as ntfy
        monkeypatch.setattr(tg, 'send_telegram_message', _boom)
        monkeypatch.setattr(ntfy, 'send_ntfy', _boom)
        resp = client.post('/api/brief-subscribe', json={'email': 'brief_notif@test.com'})
        assert resp.status_code == 200
        assert resp.get_json() == {'ok': True}

    def test_route_is_rate_limited_and_csrf_exempt(self):
        """Rate limiting is globally OFF in TestConfig (RATELIMIT_ENABLED=False
        — flask-limiter's init_app short-circuits entirely when disabled, so
        there's no before_request hook to exercise for a real 429 in this
        suite); assert the wiring at the source level instead, same
        source-text-assertion convention test_report_editorial_flag.py and
        test_report_editorial_template.py already use for guard contracts."""
        import inspect
        import carrac.auth.routes as r
        src = inspect.getsource(r)
        route_idx = src.index("def brief_subscribe(")
        decorators = src[src.rindex("@auth_bp.route('/api/brief-subscribe'", 0, route_idx):route_idx]
        assert "@limiter.limit(\"5 per minute\")" in decorators
        assert "@csrf.exempt" in decorators

    def test_qualified_view_name_registered_with_limiter(self):
        """Confirms the decorator actually took effect against the live
        Limiter instance (not just present in source) — flask-limiter marks
        every `@limiter.limit(...)`-decorated view in `_marked_for_limiting`
        at import/registration time, independent of RATELIMIT_ENABLED."""
        from carrac.extensions import limiter
        assert any('brief_subscribe' in name for name in limiter._marked_for_limiting)
