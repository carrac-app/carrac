"""Auth dependency + session middleware test suite.

Covers the cross-cutting auth infrastructure:

* ``CarracSessionMiddleware`` reads ``mcd:session:<sid>`` Redis keys and
  populates ``request.state.session``.
* ``current_user`` / ``require_login`` / ``require_admin`` / ``require_tier``
  dependencies port the Flask decorators with ``request.state``-only reads.
* ``/api/v2/_whoami`` debug route — the cross-runtime gate.
* Canonical 429 JSON shape via ``http_exception_handler``.

The "cross-runtime session gate" test (``test_cross_runtime_session_gate``)
is the key gate. It seeds Redis with a msgpack blob in the EXACT
shape Flask-Session 0.8.0 writes today and verifies the FastAPI middleware
decodes it byte-compatibly. We don't drive a real Flask login in tests
because tests run with ``REDIS_URL`` unset → Flask-Session falls back to
filesystem mode, which is divergent from prod. The contract this test
guards is "FastAPI reads what Flask wrote", not "Flask login flow runs in
test", which is exercised separately in ``tests/auth/`` against the Flask
test client.
"""

from typing import Annotated, Any, Optional

import msgspec.msgpack
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from carrac.fastapi_app import exception_handlers
from carrac.fastapi_app.deps.auth import (
    current_user,
    require_admin,
    require_login,
    require_premium,
    require_tier,
)
from carrac.fastapi_app.session_middleware import (
    KEY_PREFIX,
    SESSION_TTL_SECONDS,
    CarracSessionMiddleware,
)


# ---------------------------------------------------------------------------
# Fake Redis — minimal subset of redis-py the middleware actually uses.
# ---------------------------------------------------------------------------


class FakeRedis:
    """In-memory stand-in for ``redis.Redis``: ``get``/``set``/``expire``."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    def expire(self, key: str, ttl: int) -> bool:
        if key not in self.store:
            return False
        self.ttls[key] = ttl
        return True


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


# ---------------------------------------------------------------------------
# App factory — minimal FastAPI app that mounts the real middleware + deps.
# ---------------------------------------------------------------------------


@pytest.fixture
def make_app(fake_redis, monkeypatch):
    """Build a minimal FastAPI app wired with the middleware/deps.

    Patches ``_build_redis_client`` to return the fake Redis so the middleware
    Starlette constructs at first-request time uses the in-memory store
    instead of touching a real Redis socket. Returns a factory because each
    test wants its own routes; sharing apps would smuggle state via the
    middleware-cached redis client.
    """
    from carrac.fastapi_app import session_middleware as sm

    monkeypatch.setattr(sm, "_build_redis_client", lambda url: fake_redis)

    def _factory() -> FastAPI:
        app = FastAPI()
        app.add_middleware(CarracSessionMiddleware, redis_url="redis://fake")
        exception_handlers.install(app)
        return app

    return _factory


@pytest.fixture
def app_with_whoami(make_app):
    """App that exposes the real /api/v2/_whoami router under root path."""
    from carrac.fastapi_app.routers.whoami import router as whoami_router

    app = make_app()
    app.include_router(whoami_router)
    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_session(
    fake_redis: FakeRedis,
    sid: str,
    data: dict[str, Any],
    ttl: int = SESSION_TTL_SECONDS,
) -> None:
    """Write a session blob in the same encoding Flask-Session uses.

    Mirrors ``flask_session.serializers.MsgSpecSerializer.encode`` —
    ``msgspec.msgpack.Encoder().encode(dict)`` — so the FastAPI middleware
    can decode it byte-compatibly.
    """
    payload = msgspec.msgpack.Encoder().encode(data)
    fake_redis.set(f"{KEY_PREFIX}{sid}", payload, ex=ttl)


# ---------------------------------------------------------------------------
# KEY GATE
# ---------------------------------------------------------------------------


def test_cross_runtime_session_gate(app_with_whoami, fake_redis, monkeypatch):
    """KEY GATE: a Flask-Session msgpack blob is decoded by the FastAPI middleware.

    Seeds Redis with the EXACT key/value shape Flask-Session 0.8.0 writes
    and asserts the new
    ``/api/v2/_whoami`` route returns the same user_id and tier.
    """
    monkeypatch.setenv("WHOAMI_DEBUG", "1")

    sid = "qd_E2sOLdIxUQS5BVeXk6f3rT_KbA8aZ-NGwHPnRpqo"  # 43-char base64url
    _seed_session(
        fake_redis,
        sid,
        {
            "user_id": 42,
            "email": "gate@example.com",
            "display_name": "Gate User",
            "is_admin": False,
            "tier": "premium",
            "_created_at": 1715251200.0,
            "_permanent": True,
        },
    )

    client = TestClient(app_with_whoami)
    resp = client.get("/_whoami", cookies={"session": sid})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "session"
    assert body["user_id"] == 42
    assert body["email"] == "gate@example.com"
    assert body["tier"] == "premium"
    assert body["is_admin"] is False

    # TTL refresh contract: middleware bumps the Redis TTL on active sessions.
    assert fake_redis.ttls[f"{KEY_PREFIX}{sid}"] == SESSION_TTL_SECONDS


# ---------------------------------------------------------------------------
# Session middleware behaviour
# ---------------------------------------------------------------------------


class TestSessionMiddleware:
    def test_no_cookie_yields_empty_session(self, app_with_whoami, monkeypatch):
        monkeypatch.setenv("WHOAMI_DEBUG", "1")
        client = TestClient(app_with_whoami)
        resp = client.get("/_whoami")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "anonymous"
        assert body["user_id"] is None
        assert body["tier"] == "free"

    def test_cookie_but_no_redis_key(self, app_with_whoami, monkeypatch):
        monkeypatch.setenv("WHOAMI_DEBUG", "1")
        client = TestClient(app_with_whoami)
        resp = client.get("/_whoami", cookies={"session": "stale_unknown_sid"})
        assert resp.status_code == 200
        assert resp.json()["source"] == "anonymous"

    def test_corrupt_blob_treated_as_no_session(
        self, app_with_whoami, fake_redis, monkeypatch
    ):
        """Malformed msgpack must NOT 500 — Flask-Session treats expired/corrupt
        blobs as no session, and the middleware must do the same."""
        monkeypatch.setenv("WHOAMI_DEBUG", "1")
        sid = "corrupt_sid_for_test_purposes_xxxxxxxxxxxxx"
        fake_redis.set(f"{KEY_PREFIX}{sid}", b"not-msgpack")
        client = TestClient(app_with_whoami)
        resp = client.get("/_whoami", cookies={"session": sid})
        assert resp.status_code == 200
        assert resp.json()["source"] == "anonymous"


# ---------------------------------------------------------------------------
# /api/v2/_whoami env-gating
# ---------------------------------------------------------------------------


class TestWhoamiGating:
    def test_404_without_env_flag(self, app_with_whoami, monkeypatch):
        monkeypatch.delenv("WHOAMI_DEBUG", raising=False)
        client = TestClient(app_with_whoami)
        resp = client.get("/_whoami")
        assert resp.status_code == 404

    @pytest.mark.parametrize("flag", ["", "0", "false", "no", "off"])
    def test_404_for_falsy_values(self, app_with_whoami, monkeypatch, flag):
        monkeypatch.setenv("WHOAMI_DEBUG", flag)
        client = TestClient(app_with_whoami)
        resp = client.get("/_whoami")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth dependency unit tests — exercised by tiny in-test routes.
# ---------------------------------------------------------------------------


def _attach_auth_routes(app: FastAPI) -> None:
    """Mount the auth deps under stub routes so we can drive them via TestClient."""

    @app.get("/_login_only")
    def login_only(user: Annotated[dict, Depends(require_login)]):
        return user

    @app.get("/_admin_only")
    def admin_only(user: Annotated[dict, Depends(require_admin)]):
        return user

    @app.get("/_premium_only")
    def premium_only(user: Annotated[dict, Depends(require_premium)]):
        return user

    @app.get("/_tester_only")
    def tester_only(
        user: Annotated[dict, Depends(require_tier("tester", "admin"))],
    ):
        return user

    @app.get("/_current_user")
    def current_user_route(user: Annotated[Optional[dict], Depends(current_user)]):
        return {"user": user}


@pytest.fixture
def app_with_auth(make_app):
    app = make_app()
    _attach_auth_routes(app)
    return app


class TestRequireLogin:
    def test_no_session_redirects(self, app_with_auth):
        client = TestClient(app_with_auth, follow_redirects=False)
        resp = client.get("/_login_only")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_no_session_with_json_accept_still_redirects(self, app_with_auth):
        """``login_required`` always redirects, even for JSON callers — port spec §1.1."""
        client = TestClient(app_with_auth, follow_redirects=False)
        resp = client.get("/_login_only", headers={"Accept": "application/json"})
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    def test_with_session_passes(self, app_with_auth, fake_redis):
        sid = "valid_login_sid_xxxxxxxxxxxxxxxxxxxxxxxxxxx"
        _seed_session(fake_redis, sid, {"user_id": 7, "tier": "free"})
        client = TestClient(app_with_auth)
        resp = client.get("/_login_only", cookies={"session": sid})
        assert resp.status_code == 200
        assert resp.json()["id"] == 7


class TestRequireAdmin:
    def test_no_session_redirects(self, app_with_auth):
        client = TestClient(app_with_auth, follow_redirects=False)
        resp = client.get("/_admin_only")
        assert resp.status_code == 302

    def test_non_admin_session_403(self, app_with_auth, fake_redis):
        sid = "nonadmin_sid_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        _seed_session(
            fake_redis, sid, {"user_id": 1, "is_admin": False, "tier": "member"}
        )
        client = TestClient(app_with_auth)
        resp = client.get("/_admin_only", cookies={"session": sid})
        assert resp.status_code == 403
        assert resp.json() == {"error": "Admin access required"}

    def test_admin_session_passes_when_db_confirms(
        self, app_with_auth, fake_redis, monkeypatch
    ):
        sid = "admin_sid_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        _seed_session(
            fake_redis, sid, {"user_id": 99, "is_admin": True, "tier": "admin"}
        )

        # Stub user_db.get_user_by_id to return an active admin.
        from user_db import user_db

        monkeypatch.setattr(
            user_db,
            "get_user_by_id",
            lambda uid: {"id": uid, "is_admin": True, "is_active": True},
        )
        client = TestClient(app_with_auth)
        resp = client.get("/_admin_only", cookies={"session": sid})
        assert resp.status_code == 200

    def test_admin_session_redirects_when_db_says_inactive(
        self, app_with_auth, fake_redis, monkeypatch
    ):
        sid = "stale_admin_sid_xxxxxxxxxxxxxxxxxxxxxxxxxx"
        _seed_session(
            fake_redis, sid, {"user_id": 99, "is_admin": True, "tier": "admin"}
        )
        from user_db import user_db

        monkeypatch.setattr(
            user_db,
            "get_user_by_id",
            lambda uid: {"id": uid, "is_admin": False, "is_active": True},
        )
        client = TestClient(app_with_auth, follow_redirects=False)
        resp = client.get("/_admin_only", cookies={"session": sid})
        assert resp.status_code == 302

    def test_admin_db_error_returns_503(
        self, app_with_auth, fake_redis, monkeypatch
    ):
        sid = "db_err_admin_sid_xxxxxxxxxxxxxxxxxxxxxxxxx"
        _seed_session(
            fake_redis, sid, {"user_id": 99, "is_admin": True, "tier": "admin"}
        )
        from user_db import user_db

        def boom(uid):
            raise RuntimeError("db down")

        monkeypatch.setattr(user_db, "get_user_by_id", boom)
        client = TestClient(app_with_auth)
        resp = client.get("/_admin_only", cookies={"session": sid})
        assert resp.status_code == 503
        assert "temporarily unavailable" in resp.json()["error"].lower()


class TestRequireTier:
    def test_premium_blocks_free(self, app_with_auth, fake_redis):
        sid = "free_user_sid_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        _seed_session(fake_redis, sid, {"user_id": 5, "tier": "free"})
        client = TestClient(app_with_auth)
        resp = client.get(
            "/_premium_only",
            cookies={"session": sid},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "Subscription required"
        assert body["upgrade_required"] is True
        assert body["current_tier"] == "free"
        assert "member" in body["required_tiers"]

    def test_premium_allows_member(self, app_with_auth, fake_redis):
        sid = "member_sid_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        _seed_session(fake_redis, sid, {"user_id": 5, "tier": "member"})
        client = TestClient(app_with_auth)
        resp = client.get("/_premium_only", cookies={"session": sid})
        assert resp.status_code == 200

    def test_tester_blocks_premium(self, app_with_auth, fake_redis):
        sid = "premium_only_user_sid_xxxxxxxxxxxxxxxxxxxxx"
        _seed_session(fake_redis, sid, {"user_id": 5, "tier": "premium"})
        client = TestClient(app_with_auth)
        resp = client.get(
            "/_tester_only",
            cookies={"session": sid},
            headers={"Accept": "application/json"},
        )
        # tester_only requires (tester, admin) — premium is excluded by design.
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# RateContext + classify_request
# ---------------------------------------------------------------------------


class TestClassification:
    def test_anonymous_pool_default(self, app_with_auth):
        client = TestClient(app_with_auth)
        resp = client.get("/_current_user")
        assert resp.status_code == 200
        assert resp.json() == {"user": None}

    def test_session_pool(self, app_with_auth, fake_redis):
        sid = "session_pool_sid_xxxxxxxxxxxxxxxxxxxxxxxxx"
        _seed_session(fake_redis, sid, {"user_id": 11, "tier": "tester"})
        client = TestClient(app_with_auth)
        resp = client.get("/_current_user", cookies={"session": sid})
        assert resp.status_code == 200
        assert resp.json()["user"]["id"] == 11
        assert resp.json()["user"]["tier"] == "tester"

    def test_api_key_classification_via_pure_resolver(
        self, app_with_whoami, monkeypatch
    ):
        """X-Internal-Key header with the shared admin secret resolves to
        tier=admin and pool=api_key without touching flask.g."""
        monkeypatch.setenv("WHOAMI_DEBUG", "1")
        monkeypatch.setenv("SCREENSHOT_API_KEY", "shared-test-key")
        client = TestClient(app_with_whoami)
        resp = client.get("/_whoami", headers={"X-Internal-Key": "shared-test-key"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "api_key"
        assert body["tier"] == "admin"
        assert body["user_id"] is None  # shared key has no user row

    def test_api_key_shared_on_public_header_not_admin(
        self, app_with_whoami, monkeypatch
    ):
        """The shared admin secret on X-API-Key (not
        X-Internal-Key) must not classify as api_key/admin — it falls
        through to the anonymous shape."""
        monkeypatch.setenv("WHOAMI_DEBUG", "1")
        monkeypatch.setenv("SCREENSHOT_API_KEY", "shared-test-key")
        client = TestClient(app_with_whoami)
        resp = client.get("/_whoami", headers={"X-API-Key": "shared-test-key"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "anonymous"
        assert body["user_id"] is None
        assert body["tier"] == "free"


# ---------------------------------------------------------------------------
# 429 handler shape
# ---------------------------------------------------------------------------


class TestRateLimitHandler:
    def test_429_with_retry_after_dict(self, make_app):
        from fastapi import HTTPException

        app = make_app()

        @app.get("/_rl")
        def rl():
            raise HTTPException(
                status_code=429,
                detail={"error": "Rate limit exceeded", "retry_after": 90},
            )

        resp = TestClient(app).get("/_rl")
        assert resp.status_code == 429
        assert resp.json() == {"error": "Rate limit exceeded", "retry_after": 90}
        assert resp.headers.get("retry-after") == "90"

    def test_429_with_string_detail_normalised_to_canonical(self, make_app):
        """Bare-string 429 details (slowapi raises e.g. ``"1 per 1 minute"``)
        are normalised to the canonical ``"rate limit exceeded"`` so clients
        get a stable shape regardless of upstream limiter."""
        from fastapi import HTTPException

        app = make_app()

        @app.get("/_rl_str")
        def rl_str():
            raise HTTPException(status_code=429, detail="too many requests")

        resp = TestClient(app).get("/_rl_str")
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"] == "rate limit exceeded"
        # Defensive Retry-After fallback (60s) when caller didn't set one.
        assert resp.headers.get("retry-after") == "60"
        assert body.get("retry_after") == 60


# ---------------------------------------------------------------------------
# Pure resolvers
# ---------------------------------------------------------------------------


class TestPureResolvers:
    def test_resolve_api_key_empty(self):
        from carrac.auth.pure_resolvers import resolve_api_key

        assert resolve_api_key("") is None
        assert resolve_api_key(None) is None  # type: ignore[arg-type]

    def test_resolve_api_key_shared(self, monkeypatch):
        """``allow_shared`` defaults to False — the shared key is
        refused unless the caller vouches it arrived via X-Internal-Key."""
        from carrac.auth.pure_resolvers import resolve_api_key

        monkeypatch.setenv("SCREENSHOT_API_KEY", "secret-shared-key")
        assert resolve_api_key("secret-shared-key") is None
        info = resolve_api_key("secret-shared-key", allow_shared=True)
        assert info == {"user_id": None, "tier": "admin", "scope": "full"}

    def test_resolve_api_key_constant_time(self, monkeypatch):
        """Wrong shared key must compare via hmac, not raw == — enforced by
        ``hmac.compare_digest`` inside the resolver. The visible behaviour is
        a clean ``None`` return without a timing oracle."""
        from carrac.auth.pure_resolvers import resolve_api_key

        monkeypatch.setenv("SCREENSHOT_API_KEY", "secret-shared-key")
        assert resolve_api_key("nope") is None
        assert resolve_api_key("secret-shared-keX") is None

    def test_resolve_api_key_invalid_user_prefix(self, monkeypatch):
        from carrac.auth.pure_resolvers import resolve_api_key

        # Non-cvx_k_ keys never reach the DB lookup branch.
        assert resolve_api_key("zzz_random_key") is None

    def test_resolve_api_key_carries_is_internal_flag(self, app):
        """CARRAC internal-key flag: pure_resolvers.resolve_api_key must surface
        is_internal from the DB row so rate_classify can act on it."""
        import hashlib
        from carrac.auth.pure_resolvers import resolve_api_key
        from user_db import user_db

        with app.app_context():
            uid = user_db.create_user(
                "resolve-internal@example.com", "StrongPass!123", tos_accepted=True
            )
            raw = "cvx_k_" + ("b" * 32)
            token_hash = hashlib.sha256(raw.encode()).hexdigest()
            assert user_db.create_api_key(
                uid, token_hash, token_suffix=raw[-4:], name="agent-test", is_internal=True
            )
            info = resolve_api_key(raw)
            assert info is not None
            assert info["is_internal"] is True
