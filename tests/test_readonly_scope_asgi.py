"""Scope enforcement on FastAPI routes.

The Flask ``_enforce_api_key_scope`` before_request hook blocks writes for
read-only API keys; FastAPI routes enforce the same rule, so a ``cvx_k_`` key
with scope ``read-only`` cannot POST/PUT/DELETE through any FastAPI-live route.

This test hits FastAPI-live paths through the strangler ``asgi_client`` using
a real DB-backed read-only API key (same pattern as ``member_key`` in
tests/fastapi/conftest.py) to confirm:

1. A POST with a read-only key returns 403 with the canonical message.
2. A GET with the same key is not blocked (scope gate is write-only).
3. A POST with a full-scope key on the same route is not blocked by the scope
   gate (it may still 4xx for unrelated reasons, but not a scope 403).

Routes exercised: ``POST /api/watchlist/add`` (FastAPI-live, uses
``auth_or_key``).  The watchlist endpoint needs only a valid user_id and a
JSON body — no additional fixtures, no database seeding beyond the user row.

False-green risk: the existing ``TestReadOnlyApiKeyScope`` in
``tests/test_auth_idor.py`` uses the FLASK test client and monkeypatches
``verify_api_key`` / ``g.api_key_scope`` — those patches do NOT reach the
FastAPI classify_request → pure_resolvers path.  This test uses the ASGI
client with a real key verified through ``user_db.verify_api_key_token`` and
``carrac.auth.pure_resolvers.resolve_api_key`` — the exact path that
enforces the scope on FastAPI.

NOTE: ``asgi_client`` and ``asgi_app`` are inlined here (instead of requiring
tests/fastapi/conftest.py) so this file is self-contained and discoverable
from tests/ root.  The fixtures are identical copies of those in
tests/fastapi/conftest.py — see that file for the rationale.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Iterator

import httpx
import pytest
from httpx import ASGITransport


# ---------------------------------------------------------------------------
# Inlined ASGI transport fixtures (mirrors tests/fastapi/conftest.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def asgi_app(app):  # noqa: ARG001 — depends on Flask app fixture for env init
    """Return the strangler ASGI app composed of FastAPI + Flask."""
    from carrac.asgi import asgi_app as _app
    return _app


@pytest.fixture
def asgi_client(asgi_app) -> Iterator[httpx.Client]:
    """Sync httpx.Client driving the strangler ASGI app."""
    transport = ASGITransport(app=asgi_app)
    async_client = httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    )

    class _SyncWrapper:
        def __init__(self, ac: httpx.AsyncClient) -> None:
            self._ac = ac
            self._loop = asyncio.new_event_loop()

        def __getattr__(self, name: str):
            method = getattr(self._ac, name)

            def call(*args, **kwargs):
                return self._loop.run_until_complete(method(*args, **kwargs))

            return call

        def close(self) -> None:
            self._loop.run_until_complete(self._ac.aclose())
            self._loop.close()

    wrapper = _SyncWrapper(async_client)
    yield wrapper
    wrapper.close()


# ---------------------------------------------------------------------------
# Key fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ro_key(app):  # noqa: ARG001 — app sets DATA_DIR / user_db singleton
    """Real per-user API key with scope='read-only' in the test DB.

    Returns ``(headers, user_id)``.  Mirrors the ``member_key`` fixture in
    ``tests/fastapi/conftest.py`` but stores the key with scope='read-only'.
    """
    from user_db import user_db
    from carrac.apikeys.auth import generate_api_key, hash_api_key

    email = f"ro-{secrets.token_hex(4)}@test.dev"
    uid = user_db.create_user(email, "StrongPass!123", tos_accepted=True)
    assert uid, "create_user returned falsy"
    user_db.set_user_tier(uid, "member")

    raw = generate_api_key()  # canonical cvx_k_<32 hex> format
    key_id = user_db.create_api_key(
        uid,
        hash_api_key(raw),
        token_suffix=raw[-4:],
        name="ro-scope-test",
        scope="read-only",
    )
    assert key_id, "create_api_key returned falsy"
    return {"X-API-Key": raw}, uid


@pytest.fixture
def full_key(app):  # noqa: ARG001
    """Real per-user API key with scope='full' — control arm for scope tests."""
    from user_db import user_db
    from carrac.apikeys.auth import generate_api_key, hash_api_key

    email = f"full-{secrets.token_hex(4)}@test.dev"
    uid = user_db.create_user(email, "StrongPass!123", tos_accepted=True)
    assert uid
    user_db.set_user_tier(uid, "member")

    raw = generate_api_key()
    key_id = user_db.create_api_key(
        uid,
        hash_api_key(raw),
        token_suffix=raw[-4:],
        name="full-scope-test",
        scope="full",
    )
    assert key_id
    return {"X-API-Key": raw}, uid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReadOnlyScopeAsgi:
    """Read-only API key scope enforcement through the FastAPI strangler."""

    def test_post_blocked_for_read_only_key(self, asgi_client, ro_key):
        """POST /api/watchlist/add with read-only key must return 403."""
        headers, _uid = ro_key
        resp = asgi_client.post(
            "/api/watchlist/add",
            json={"symbol": "BTC"},
            headers=headers,
        )
        assert resp.status_code == 403, (
            f"Expected 403 for read-only key on POST, got {resp.status_code}: "
            f"{resp.text[:300]}"
        )
        body = resp.json()
        # The exception_handlers.py normalises HTTPException(detail=str) to
        # {"error": str}, mirroring Flask's jsonify({...}) shape.
        assert body.get("error") == "read-only API key cannot perform write operations", (
            f"Unexpected 403 body: {body}"
        )

    def test_get_not_blocked_for_read_only_key(self, asgi_client, ro_key):
        """GET requests must NOT be blocked by the read-only scope gate.

        Asserts that the ASGI client does not return the scope-gate 403 on a
        GET.  A 200, 302, 401, 404, or any other status except a scope-gate
        403 is acceptable.
        """
        headers, _uid = ro_key
        # /api/watchlist is a Flask-handled GET (via strangler fallthrough) —
        # but the scope gate lives in the FastAPI dep, which runs before the
        # WSGI fallback for FastAPI-classified paths.  For a plain GET the
        # scope check in _reject_readonly_write must be silent regardless.
        resp = asgi_client.get(
            "/api/watchlist",
            headers=headers,
        )
        body = (
            resp.json()
            if "application/json" in resp.headers.get("content-type", "")
            else {}
        )
        assert not (
            resp.status_code == 403
            and body.get("error") == "read-only API key cannot perform write operations"
        ), f"GET was blocked by scope gate — should not be: {resp.status_code} {body}"

    def test_post_not_blocked_for_full_scope_key(self, asgi_client, full_key):
        """POST with a full-scope key must NOT be blocked by the scope gate.

        The key may still receive a non-200 response for other reasons (e.g.
        validation, rate limit), but the scope-gate 403 specifically must not
        appear.
        """
        headers, _uid = full_key
        resp = asgi_client.post(
            "/api/watchlist/add",
            json={"symbol": "ETH"},
            headers=headers,
        )
        body = (
            resp.json()
            if "application/json" in resp.headers.get("content-type", "")
            else {}
        )
        assert not (
            resp.status_code == 403
            and body.get("error") == "read-only API key cannot perform write operations"
        ), f"Full-scope key was rejected by read-only scope gate: {resp.status_code} {body}"
