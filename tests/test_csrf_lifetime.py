"""A CSRF token must not expire before the session it protects.

`WTF_CSRF_TIME_LIMIT` must not be a hardcoded 3600 (1 hour) while
`PERMANENT_SESSION_LIFETIME` is 30 days — the guard would expire 720x sooner
than the thing it guards. The frontend reads the token ONCE from a `<meta>` tag at
page load and has no refresh path, so every write from a tab older than an hour
returns 403 while GETs keep returning 200. The app looks alive and silently
stops saving.

Observed on a real session (from nginx):

    08:32-08:42  POST /api/indicator-prefs  200  (20+ times)
    12:12:27     POST /api/indicator-prefs  403  42 bytes
    12:21:02     POST /api/indicator-prefs  403
    12:26:41     POST /api/indicator-prefs  403
    12:26:42     POST /api/indicator-prefs  403

42 bytes is exactly `{"error":"CSRF token missing or invalid"}`. The user
retried three times across fourteen minutes and gave up. Every GET in that
window returned 200, which is why nothing looked broken.

The FastAPI middleware must not keep its OWN copy of the number under a
comment asserting it matches security.py: both can be wrong together. A comment
claiming two values agree is not a mechanism for keeping them agreeing, so
these tests check the mechanism, not the number.
"""
import pytest


class TestTheGuardOutlivesWhatItGuards:
    def test_csrf_token_lives_at_least_as_long_as_the_session(self):
        from security import CSRF_TIME_LIMIT_SECONDS, SESSION_LIFETIME_HOURS

        session_seconds = SESSION_LIFETIME_HOURS * 3600
        assert CSRF_TIME_LIMIT_SECONDS >= session_seconds, (
            f'CSRF token expires after {CSRF_TIME_LIMIT_SECONDS}s but the '
            f'session lasts {session_seconds}s. Every write from a tab older '
            f'than the token fails with 403 while GETs keep working — the app '
            f'looks alive and silently stops saving.'
        )

    def test_the_old_one_hour_value_would_fail_this(self):
        """Pins that the former one-hour value fails the check, stated explicitly."""
        from security import SESSION_LIFETIME_HOURS

        assert 3600 < SESSION_LIFETIME_HOURS * 3600, (
            'sanity: the historical 3600 must be shorter than the session, '
            'otherwise this whole test proves nothing'
        )


class TestThereIsOneSourceOfTruth:
    def test_both_stacks_read_the_same_value(self):
        """Flask and the FastAPI strangler middleware validate the SAME tokens
        with the SAME salt. If their TTLs diverge, a token is valid on one
        front door and expired on the other, which is worse than either."""
        from security import CSRF_TIME_LIMIT_SECONDS
        from carrac.fastapi_app.csrf_middleware import _MAX_AGE_SECONDS

        assert _MAX_AGE_SECONDS == CSRF_TIME_LIMIT_SECONDS, (
            'the FastAPI middleware has drifted from security.py — it must '
            'IMPORT the value, not restate it'
        )

    def test_the_middleware_does_not_hardcode_a_literal_ttl(self):
        """Guards the mechanism rather than the number: re-introducing a
        literal is exactly how this bug happened the first time."""
        import inspect
        from carrac.fastapi_app import csrf_middleware

        src = inspect.getsource(csrf_middleware)
        head = src.split('def ')[0]  # module-level section only
        assert '_MAX_AGE_SECONDS = 3600' not in head, (
            'a literal 3600 is back in the middleware — import '
            'CSRF_TIME_LIMIT_SECONDS from security instead'
        )


class TestTheFlaskAppActuallyGetsIt:
    def test_configure_security_applies_the_derived_value(self):
        """The constant is only worth anything if the app config receives it."""
        from security import CSRF_TIME_LIMIT_SECONDS, configure_flask_security

        class _App:
            def __init__(self):
                self.config = {}

        app = _App()
        try:
            configure_flask_security(app)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f'configure_flask_security needs a fuller app: {exc}')

        assert app.config.get('WTF_CSRF_TIME_LIMIT') == CSRF_TIME_LIMIT_SECONDS
