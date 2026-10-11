"""CarracSessionMiddleware: sync redis calls off the event loop.

Tests that:
1. A request without a session cookie yields an empty session dict.
2. A request with a valid session cookie round-trips correctly (session
   data is available on request.state.session).
3. The TTL refresh (expire) fires for authenticated sessions.
4. A failing Redis GET degrades gracefully (empty session, no 500).

We test the middleware directly (unit) and via a minimal Starlette app
so we don't depend on the full ASGI stack for these focused checks.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import msgspec.msgpack
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode_session(data: dict) -> bytes:
    """Encode a session dict the same way Flask-Session / msgspec does."""
    return msgspec.msgpack.encode(data)


def _make_app_with_middleware(redis_client):
    """Build a minimal Starlette app wrapped in CarracSessionMiddleware."""
    from carrac.fastapi_app.session_middleware import CarracSessionMiddleware

    async def _session_echo(request: Request):
        session = getattr(request.state, "session", {})
        return JSONResponse({"session": dict(session)})

    app = Starlette(routes=[Route("/echo", _session_echo)])
    # Inject the pre-built mock redis client by patching __init__
    wrapped = CarracSessionMiddleware(app, redis_url=None)
    wrapped._redis = redis_client
    return wrapped


# ---------------------------------------------------------------------------
# no-cookie request → empty session
# ---------------------------------------------------------------------------


def test_no_cookie_yields_empty_session():
    """Request without a session cookie → state.session == {}."""
    mock_redis = MagicMock()
    mock_redis.get.return_value = None

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/echo")
    assert resp.status_code == 200
    assert resp.json()["session"] == {}
    # Redis GET should NOT have been called (no sid in cookie)
    mock_redis.get.assert_not_called()


# ---------------------------------------------------------------------------
# valid session cookie → session data available
# ---------------------------------------------------------------------------


def test_valid_session_cookie_populates_state():
    """Valid session cookie → request.state.session contains decoded data."""
    session_data = {"user_id": 42, "email": "test@example.com", "tier": "premium"}
    encoded = _encode_session(session_data)

    sid = "abc123"
    mock_redis = MagicMock()
    mock_redis.get.return_value = encoded

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/echo", cookies={"session": sid})

    assert resp.status_code == 200
    body = resp.json()["session"]
    assert body["user_id"] == 42
    assert body["email"] == "test@example.com"

    # Redis was called with the right key
    mock_redis.get.assert_called_once_with(f"mcd:session:{sid}")


# ---------------------------------------------------------------------------
# TTL refresh fires for authenticated sessions
# ---------------------------------------------------------------------------


def test_ttl_refresh_fires_for_authenticated_session():
    """After a request with user_id in session, expire() is called on the key."""
    session_data = {"user_id": 99, "email": "u@example.com"}
    encoded = _encode_session(session_data)

    sid = "mysid456"
    mock_redis = MagicMock()
    mock_redis.get.return_value = encoded

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    client.get("/echo", cookies={"session": sid})

    from carrac.fastapi_app.session_middleware import SESSION_TTL_SECONDS, KEY_PREFIX
    mock_redis.expire.assert_called_once_with(f"{KEY_PREFIX}{sid}", SESSION_TTL_SECONDS)


# ---------------------------------------------------------------------------
# no TTL refresh when session has no user_id
# ---------------------------------------------------------------------------


def test_no_ttl_refresh_for_unauthenticated_session():
    """Session without user_id → expire() NOT called (matches Flask behaviour)."""
    session_data = {"cart_id": "xyz"}  # no user_id
    encoded = _encode_session(session_data)

    sid = "anonsid"
    mock_redis = MagicMock()
    mock_redis.get.return_value = encoded

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    client.get("/echo", cookies={"session": sid})

    mock_redis.expire.assert_not_called()


# ---------------------------------------------------------------------------
# Redis GET failure → empty session, no 500
# ---------------------------------------------------------------------------


def test_redis_get_failure_degrades_gracefully():
    """Redis GET raising an exception → empty session, request still 200."""
    mock_redis = MagicMock()
    mock_redis.get.side_effect = ConnectionError("Redis down")

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/echo", cookies={"session": "somesid"})

    assert resp.status_code == 200
    assert resp.json()["session"] == {}


# ---------------------------------------------------------------------------
# Redis expire failure is swallowed (no 500)
# ---------------------------------------------------------------------------


def test_redis_expire_failure_is_swallowed():
    """expire() raising → logged at DEBUG, but response is 200."""
    session_data = {"user_id": 7}
    encoded = _encode_session(session_data)

    mock_redis = MagicMock()
    mock_redis.get.return_value = encoded
    mock_redis.expire.side_effect = ConnectionError("timeout")

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/echo", cookies={"session": "sidsid"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# corrupt blob → empty session, no 500
# ---------------------------------------------------------------------------


def test_corrupt_session_blob_yields_empty_session():
    """Corrupt msgpack data → empty session (Flask-Session parity)."""
    mock_redis = MagicMock()
    mock_redis.get.return_value = b"\xff\xfe\x00corrupt"

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/echo", cookies={"session": "corruptsid"})
    assert resp.status_code == 200
    assert resp.json()["session"] == {}


# ---------------------------------------------------------------------------
# Activity heartbeat dispatched for FastAPI-routed requests
# ---------------------------------------------------------------------------


def test_activity_heartbeat_dispatched_for_authenticated_session():
    """Authenticated FastAPI request → on_activity(user_id, email) fires once."""
    session_data = {"user_id": 42, "email": "test@example.com"}
    encoded = _encode_session(session_data)

    mock_redis = MagicMock()
    mock_redis.get.return_value = encoded

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    with patch("carrac.integrations.on_activity") as mock_activity:
        resp = client.get("/echo", cookies={"session": "hbsid1"})

    assert resp.status_code == 200
    mock_activity.assert_called_once_with(42, "test@example.com")


def test_no_heartbeat_without_email():
    """Session with user_id but no email → heartbeat NOT dispatched
    (matches the Flask hook's user_id+email guard)."""
    session_data = {"user_id": 42}
    encoded = _encode_session(session_data)

    mock_redis = MagicMock()
    mock_redis.get.return_value = encoded

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    with patch("carrac.integrations.on_activity") as mock_activity:
        client.get("/echo", cookies={"session": "hbsid2"})

    mock_activity.assert_not_called()


def test_no_heartbeat_for_anonymous_request():
    """No session cookie → heartbeat NOT dispatched."""
    mock_redis = MagicMock()
    mock_redis.get.return_value = None

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    with patch("carrac.integrations.on_activity") as mock_activity:
        client.get("/echo")

    mock_activity.assert_not_called()


def test_heartbeat_failure_does_not_break_request():
    """on_activity raising → request still 200 (heartbeat is best-effort)."""
    session_data = {"user_id": 7, "email": "u@example.com"}
    encoded = _encode_session(session_data)

    mock_redis = MagicMock()
    mock_redis.get.return_value = encoded

    app = _make_app_with_middleware(mock_redis)
    client = TestClient(app, raise_server_exceptions=True)
    with patch("carrac.integrations.on_activity", side_effect=RuntimeError("espo down")):
        resp = client.get("/echo", cookies={"session": "hbsid3"})

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# non-http scope (websocket) passes through unchanged
# ---------------------------------------------------------------------------


def test_non_http_scope_passes_through():
    """Non-HTTP scopes (e.g. lifespan) are forwarded without touching Redis."""
    from carrac.fastapi_app.session_middleware import CarracSessionMiddleware
    import asyncio

    mock_redis = MagicMock()

    received = []

    async def _inner(scope, receive, send):
        received.append(scope["type"])

    mw = CarracSessionMiddleware(_inner)
    mw._redis = mock_redis

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mw({"type": "lifespan"}, None, None))
    finally:
        loop.close()

    assert received == ["lifespan"]
    mock_redis.get.assert_not_called()
