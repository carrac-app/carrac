"""Login must rotate the session id.

`carrac/auth/routes.py` has carried the comment `session.clear()  # Prevent
session fixation` for a long time. It does not prevent session fixation:
`session.clear()` empties the session dict but never issues a new session id,
so the cookie the browser already held before logging in ends up holding the
victim's `user_id`.

flask-session 0.8.0 ships the mechanism — `regenerate()` in
`flask_session.base` deletes the previous store entry, mints a new sid and
marks the session modified — and a call to it must exist in carrac/ (`rg regenerate carrac/`);
the library provides it, but it only helps if something calls it.

Severity is medium, deliberately: cookies are Secure/HttpOnly/Lax, so there is
no plain-HTTP injection path and no JS read. The live vectors are a subdomain
XSS and a shared or handed-over device. What makes it worth fixing is that the
comment asserts a protection that is absent — the next auditor reads it and
moves on, which is exactly what happened.
"""



def _sid_cookie(client):
    """The raw session cookie value the client is currently carrying."""
    jar = getattr(client, '_cookies', None)
    if not jar:
        return None
    for key, cookie in jar.items():
        name = key[2] if isinstance(key, tuple) else key
        if 'session' in str(name):
            return cookie.value
    return None


class TestLoginRotatesTheSessionId:
    """The acceptance test, driven through the REAL /login route
    over HTTP with a real user — no mocks, no monkeypatching of the unit."""

    EMAIL = 'fixation-e2e@example.com'
    PASSWORD = 'StrongPass!123'

    def _create_verified_user(self):
        from user_db import user_db as udb
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
        assert uid, 'test user creation failed'
        udb.set_email_verified(uid)
        assert udb.is_email_verified(uid), 'email_verified flag not set'
        return uid

    def test_the_session_cookie_changes_across_login(self, app, client):  # noqa: ARG002
        """Capture the cookie the browser carries BEFORE authenticating, log
        in, capture it again. The two must differ; identical cookies would be
        session fixation, where a cookie an attacker planted while the victim
        was anonymous identifies the victim's authenticated session."""
        self._create_verified_user()

        # Anonymous browsing issues a session cookie.
        client.get('/login')
        with client.session_transaction() as sess:
            sess['anonymous_marker'] = 'planted'
        before = _sid_cookie(client)
        assert before, 'no session cookie was issued on the anonymous request'

        resp = client.post('/login', data={
            'email': self.EMAIL, 'password': self.PASSWORD,
        })
        assert resp.status_code == 302, (
            f'real HTTP login failed: {resp.status_code} {resp.data[:200]!r}')

        after = _sid_cookie(client)
        assert after, 'no session cookie after login'
        assert after != before, (
            'the session id survived the anonymous -> authenticated transition '
            'unchanged: this IS session fixation, and the '
            '"# Prevent session fixation" comment at auth/routes.py is false'
        )

    def test_login_still_works_and_the_session_is_usable(self, app, client):  # noqa: ARG002
        """Rotation must not break the session it just rotated — the authed
        request after login has to succeed on the NEW cookie."""
        self._create_verified_user()
        client.get('/login')
        resp = client.post('/login', data={
            'email': self.EMAIL, 'password': self.PASSWORD,
        })
        assert resp.status_code == 302
        r = client.get('/api/tier-info')
        assert r.status_code == 200, (
            f'session unusable after rotation: {r.status_code} {r.data[:200]!r}')

    def test_the_anonymous_session_contents_do_not_survive(self, app, client):  # noqa: ARG002
        """session.clear() drops the anonymous session contents; this still holds
        once the session id is rotated."""
        self._create_verified_user()
        client.get('/login')
        with client.session_transaction() as sess:
            sess['anonymous_marker'] = 'planted'
        client.post('/login', data={
            'email': self.EMAIL, 'password': self.PASSWORD,
        })
        with client.session_transaction() as sess:
            assert 'anonymous_marker' not in sess
            assert sess.get('user_id')


class TestTheFixIsWired:
    """Built but never connected is the recurring defect here, and
    this one shipped as a COMMENT claiming a protection with no code behind
    it. These pin the wiring; the behavioural tests above pin the effect."""

    @staticmethod
    def _login_block():
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / 'carrac' / 'auth' / 'routes.py')
        text = src.read_text()
        start = text.index("session.clear()  # Drop anonymous state")
        return text[start:start + 6000]

    def test_login_rotates_the_session_id(self):
        block = self._login_block()
        assert 'regenerate' in block, (
            'login clears the session without rotating the sid — session '
            'fixation is back'
        )

    def test_rotation_happens_after_the_session_is_populated(self):
        """⚠ The trap this fix walked into. flask-session's regenerate()
        opens with `if session:` and an EMPTY session is falsy, so calling it
        directly after session.clear() rotates NOTHING — silently, with no
        error. Rotation must come after the keys are set."""
        block = self._login_block()
        clear_at = block.index('session.clear()')
        keys_at = block.index("session['user_id']")
        rot_at = block.index('regenerate')
        assert clear_at < keys_at < rot_at, (
            'regenerate() is called while the session is still empty — '
            'flask-session skips its whole body and the sid does NOT rotate'
        )

    def test_the_tracked_sid_is_the_post_rotation_one(self):
        """Session tracking reads the sid for track_user_session(). If
        rotation happened after that read, the killed-session registry would
        hold a sid that no longer exists."""
        block = self._login_block()
        rot = block.index('regenerate')
        track = block.index("getattr(session, 'sid', None)")
        assert rot < track, (
            'the sid is read for tracking BEFORE rotation — track_user_session '
            'would register a stale sid'
        )
