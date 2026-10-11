"""The FastAPI front door ignored the session kill-epoch.

carrac/auth/routes.py::_reject_killed_sessions is a Flask-only
before_app_request hook. Both stacks read the SAME
mcd:session:<sid> blob, but nothing in carrac/fastapi_app/deps/auth.py or
session_middleware.py referenced killed_at / _created_at at all.

So: deactivate an abusive user, or a user hits log-out-everywhere. If the
Redis delete does not land, every Flask page correctly rejects the stale
cookie while /api/plays, /api/drawings, /api/journal/*, /api/alerts,
/api/watchlist and /api/chart/share keep working indefinitely.

These drive the fix end to end — real CarracSessionMiddleware, real
current_user dependency, session blobs in the exact msgpack encoding
Flask-Session writes. Nothing about the unit under test is mocked; only
Redis is faked, using the harness already established in test_auth.py.
"""
import time

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from carrac.auth.session_kill import (
    _KILL_TS_FALLBACK,
    mark_sessions_killed,
    session_is_killed,
    sessions_killed_at,
)
from carrac.fastapi_app.deps.auth import current_user, require_login

from tests.fastapi.test_auth import (  # noqa: F401  (fixtures)
    FakeRedis,
    _seed_session,
    fake_redis,
    make_app,
)

USER_ID = 4242
SID = 'sid-killed-epoch'


@pytest.fixture(autouse=True)
def _clean_kill_state():
    _KILL_TS_FALLBACK.clear()
    yield
    _KILL_TS_FALLBACK.clear()


@pytest.fixture
def app(make_app):  # noqa: F811
    a = make_app()

    @a.get('/probe')
    def _probe(user=Depends(current_user)):
        return {'user_id': (user or {}).get('id')}

    @a.get('/guarded')
    def _guarded(user=Depends(require_login)):
        return {'user_id': user['id']}

    return a


def _session_blob(created_at):
    return {
        'user_id': USER_ID,
        'email': 'victim@example.com',
        'tier': 'free',
        'is_admin': False,
        '_created_at': created_at,
    }


class TestSharedKillEpoch:
    """The mechanism itself, framework-free."""

    def test_unkilled_user_has_no_epoch(self):
        assert sessions_killed_at(USER_ID) == 0.0

    def test_marking_records_an_epoch(self):
        mark_sessions_killed(USER_ID)
        assert sessions_killed_at(USER_ID) > 0

    def test_session_created_before_the_kill_is_killed(self):
        old = time.time() - 60
        mark_sessions_killed(USER_ID)
        assert session_is_killed(_session_blob(old)) is True

    def test_session_created_after_the_kill_survives(self):
        mark_sessions_killed(USER_ID)
        fresh = time.time() + 60
        assert session_is_killed(_session_blob(fresh)) is False

    def test_session_without_created_at_fails_closed(self):
        """A blob that cannot prove when it was issued must not be trusted —
        older relics are exactly what a kill needs to catch."""
        mark_sessions_killed(USER_ID)
        blob = _session_blob(0)
        del blob['_created_at']
        assert session_is_killed(blob) is True

    def test_anonymous_session_is_never_killed(self):
        assert session_is_killed({}) is False
        assert session_is_killed({'foo': 'bar'}) is False


class TestFastApiHonoursTheKill:
    """A killed session is rejected through the real dependency."""

    def test_killed_session_is_not_a_current_user(self, app, fake_redis):  # noqa: F811
        _seed_session(fake_redis, SID, _session_blob(time.time() - 60))
        mark_sessions_killed(USER_ID)

        r = TestClient(app).get('/probe', cookies={'session': SID})
        assert r.json()['user_id'] is None, (
            'a session predating the kill epoch still resolved to a user on '
            'the FastAPI side'
        )

    def test_killed_session_cannot_reach_a_guarded_route(self, app, fake_redis):  # noqa: F811
        """This is the one that matters — the mutating surface."""
        _seed_session(fake_redis, SID, _session_blob(time.time() - 60))
        mark_sessions_killed(USER_ID)

        r = TestClient(app, follow_redirects=False).get(
            '/guarded', cookies={'session': SID})
        assert r.status_code in (302, 307, 401), (
            f'killed session reached a require_login route (got {r.status_code})'
        )

    def test_a_session_reissued_after_the_kill_still_works(self, app, fake_redis):  # noqa: F811
        """Login and change-password re-stamp _created_at; those must pass, or
        a password reset would lock the user out of their own account."""
        mark_sessions_killed(USER_ID)
        _seed_session(fake_redis, SID, _session_blob(time.time() + 60))

        r = TestClient(app).get('/probe', cookies={'session': SID})
        assert r.json()['user_id'] == USER_ID

    def test_never_killed_user_is_unaffected(self, app, fake_redis):  # noqa: F811
        """No epoch set — the common path must not pay for this."""
        _seed_session(fake_redis, SID, _session_blob(time.time() - 60))

        r = TestClient(app).get('/probe', cookies={'session': SID})
        assert r.json()['user_id'] == USER_ID


class TestLegacyFallbackScanIsNoLongerDeadCode:
    """Second half. A fallback matching f'"user_id":{id}' against the stored
    blob never hits: Flask-Session 0.8 stores msgpack, so that substring never
    occurs and the fallback would delete nothing — for exactly the
    pre-tracking sessions it exists to catch."""

    def test_the_old_json_substring_really_cannot_match_msgpack(self):
        """The JSON-substring match is dead, not merely fragile."""
        import msgspec.msgpack
        raw = msgspec.msgpack.Encoder().encode(_session_blob(time.time()))
        assert f'"user_id":{USER_ID}' not in str(raw)

    def test_msgpack_blob_is_matched_to_its_owner(self):
        import msgspec.msgpack
        from carrac.auth.routes import _session_blob_belongs_to
        raw = msgspec.msgpack.Encoder().encode(_session_blob(time.time()))
        assert _session_blob_belongs_to(raw, USER_ID) is True

    def test_another_users_blob_is_not_matched(self):
        import msgspec.msgpack
        from carrac.auth.routes import _session_blob_belongs_to
        raw = msgspec.msgpack.Encoder().encode(_session_blob(time.time()))
        assert _session_blob_belongs_to(raw, USER_ID + 1) is False

    def test_json_blob_still_matches_if_the_backend_is_swapped_back(self):
        import json
        from carrac.auth.routes import _session_blob_belongs_to
        raw = json.dumps(_session_blob(time.time())).encode()
        assert _session_blob_belongs_to(raw, USER_ID) is True

    def test_garbage_never_matches(self):
        from carrac.auth.routes import _session_blob_belongs_to
        assert _session_blob_belongs_to(b'\x00\x01not-a-blob', USER_ID) is False
        assert _session_blob_belongs_to(None, USER_ID) is False
