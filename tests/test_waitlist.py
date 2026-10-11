"""Tests for the invite-code waitlist.

Covers the waitlist DB functions (dedup, pick, status transitions), the register
route's paused-branch behavior (no code -> waitlist capture; valid code -> bypass
to the register form), and the invite email URL construction.

Uses the `app`/`client` fixtures so user_db points at the temp test DB.
"""

from __future__ import annotations



class TestWaitlistDB:
    def test_add_and_dedup(self, app):
        from user_db import user_db
        email = 'wl_dedup@test.com'
        r1 = user_db.add_to_waitlist(email, source='landing', ip='1.2.3.4')
        r2 = user_db.add_to_waitlist(email)
        assert r1['status'] == 'added'
        assert r2['status'] == 'exists'

    def test_invalid_email_rejected(self, app):
        from user_db import user_db
        assert user_db.add_to_waitlist('not-an-email')['status'] == 'invalid'
        assert user_db.add_to_waitlist('')['status'] == 'invalid'

    def test_pick_and_invite_flow(self, app):
        from user_db import user_db
        user_db.add_to_waitlist('wl_pick@test.com')
        picked = [p['email'] for p in user_db.pick_next_waitlist(50)]
        assert 'wl_pick@test.com' in picked
        assert user_db.mark_waitlisted_invited('wl_pick@test.com', 'ABCD1234', cohort='wave1') is True
        # No longer in the 'waiting' pool.
        picked2 = [p['email'] for p in user_db.pick_next_waitlist(50)]
        assert 'wl_pick@test.com' not in picked2
        # Second invite attempt on an already-invited row is a no-op.
        assert user_db.mark_waitlisted_invited('wl_pick@test.com', 'ZZZZ9999') is False

    def test_mark_registered_idempotent(self, app):
        from user_db import user_db
        user_db.add_to_waitlist('wl_reg@test.com')
        assert user_db.mark_waitlisted_registered('wl_reg@test.com') is True
        assert user_db.mark_waitlisted_registered('wl_reg@test.com') is False  # already registered

    def test_counts(self, app):
        from user_db import user_db
        base = user_db.count_waitlist()
        user_db.add_to_waitlist('wl_count@test.com')
        assert user_db.count_waitlist() == base + 1
        assert user_db.count_waitlist('waiting') >= 1


class TestRegisterWaitlistRoute:
    def test_paused_no_code_captures_to_db(self, client, app, monkeypatch,
                                           notifications):
        # A waitlist capture pings the operator (Telegram + ntfy)
        # requires that be declared rather than escaping to a phone.
        import carrac.auth.routes as r
        monkeypatch.setattr(r, 'signups_paused', lambda: True)
        resp = client.post('/register', data={'email': 'route_wl@test.com'})
        assert resp.status_code == 200
        from user_db import user_db
        picked = [p['email'] for p in user_db.pick_next_waitlist(100)]
        assert 'route_wl@test.com' in picked

    def test_paused_with_code_bypasses_to_register_form(self, client, app, monkeypatch):
        import carrac.auth.routes as r
        monkeypatch.setattr(r, 'signups_paused', lambda: True)
        resp = client.get('/register?code=ABCD1234')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # The register form (has password inputs), NOT the waitlist page.
        assert 'password' in body.lower()
        # Code is prefilled into the invite field.
        assert 'ABCD1234' in body

    def test_open_signup_without_invite_succeeds(self, client, app, monkeypatch):
        """BETA open signups: with signups NOT paused, registration works with no
        invite code (Turnstile is the bot-wall; unset in tests → check skipped)."""
        import carrac.auth.routes as r
        monkeypatch.setattr(r, 'signups_paused', lambda: False)
        monkeypatch.setattr(r, '_send_verification_email', lambda *a, **k: None)
        monkeypatch.setattr(r, 'on_user_signup', lambda *a, **k: None)
        resp = client.post('/register', data={
            'email': 'open_nocode@test.com', 'password': 'Zylq8Rmv2Kp!',
            'password_confirm': 'Zylq8Rmv2Kp!', 'agree_terms': 'on', 'age_attested': 'on',
        })
        assert resp.status_code == 200
        assert 'invalid or expired invite' not in resp.get_data(as_text=True).lower()
        from user_db import user_db
        assert user_db.get_user_by_email('open_nocode@test.com') is not None

    def test_open_signup_turnstile_rejects_without_token(self, client, app, monkeypatch):
        """When a Turnstile secret IS configured, a register POST with no token is
        rejected and no user is created (no CF network call — empty token short-circuits)."""
        import carrac.auth.routes as r
        monkeypatch.setattr(r, 'signups_paused', lambda: False)
        monkeypatch.setenv('TURNSTILE_SECRET_KEY', 'test-secret')
        resp = client.post('/register', data={
            'email': 'bot_notoken@test.com', 'password': 'Zylq8Rmv2Kp!',
            'password_confirm': 'Zylq8Rmv2Kp!', 'agree_terms': 'on', 'age_attested': 'on',
        })
        assert 'bot check failed' in resp.get_data(as_text=True).lower()
        from user_db import user_db
        assert user_db.get_user_by_email('bot_notoken@test.com') is None


class TestWaveRelease:
    def test_dry_run_invites_nobody(self, app):
        from user_db import user_db
        from carrac import waitlist_release
        user_db.add_to_waitlist('wave_dry@test.com')
        rc = waitlist_release.run(count=5, dry_run=True)
        assert rc == 0
        # dry-run must not change status — still 'waiting'.
        assert 'wave_dry@test.com' in [p['email'] for p in user_db.pick_next_waitlist(100)]

    def test_stats_path(self, app):
        from user_db import user_db
        # count helpers used by --stats must not raise.
        assert isinstance(user_db.count_waitlist(), int)
        assert isinstance(user_db.count_waitlist('waiting'), int)


class TestSendInviteEmail:
    def test_builds_register_url_with_code(self, monkeypatch):
        import carrac.email as em
        captured = {}

        def _fake_send(to, subject, html_body, text_body=None):
            captured.update(to=to, subject=subject, html=html_body, text=text_body)
            return True

        monkeypatch.setattr(em, 'send_email', _fake_send)
        assert em.send_invite_email('inv@test.com', 'WAVE1234', expires_days=10) is True
        assert captured['to'] == 'inv@test.com'
        assert 'WAVE1234' in captured['html']
        assert '/register?code=WAVE1234' in captured['html']
        assert 'WAVE1234' in captured['text']
