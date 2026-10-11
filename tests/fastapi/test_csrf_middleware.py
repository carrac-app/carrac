"""``CarracCSRFMiddleware`` parity + cross-runtime tests.

The middleware mirrors ``flask_wtf.csrf.validate_csrf`` for the FastAPI
write surface. Each test exercises one branch of the bypass / enforce
matrix.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any, Mapping

import itsdangerous
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import State

from carrac.fastapi_app.csrf_middleware import (
    CarracCSRFMiddleware,
    _SAFE_METHODS,
    _SKIP_PREFIXES,
)

SECRET = "test-secret-key-for-csrf-suite"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _raw_csrf() -> str:
    return hashlib.sha1(os.urandom(64)).hexdigest()


def _wire_token(raw: str, secret: str = SECRET) -> str:
    return itsdangerous.URLSafeTimedSerializer(secret, salt="wtf-csrf-token").dumps(raw)


class _FakeSessionMiddleware:
    """Minimal ASGI middleware mirroring ``CarracSessionMiddleware``'s
    state-population contract for tests.

    Sets ``scope["state"].session`` to the configured dict on every request
    so CSRF middleware can read it. Skips Redis/cookies entirely.
    """

    def __init__(self, app, session: Mapping[str, Any]):
        self.app = app
        self._session = dict(session)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        existing = scope.get("state")
        if isinstance(existing, State):
            existing.session = dict(self._session)
        else:
            scope["state"] = State({"session": dict(self._session)})
        await self.app(scope, receive, send)


def _make_app(session: Mapping[str, Any] | None = None, *, secret: str = SECRET) -> FastAPI:
    app = FastAPI()

    @app.post("/api/plays")
    async def _create_play():
        return {"status": "created", "id": 42}

    @app.post("/api/widget/tokens")
    async def _create_widget_token():
        return {"status": "created"}

    @app.post("/api/drawings")
    async def _create_drawing():
        return {"status": "created"}

    @app.get("/api/plays")
    async def _list_plays():
        return {"plays": []}

    # Order: CSRF inner, fake-session outer (matches production LIFO ordering).
    app.add_middleware(CarracCSRFMiddleware, secret_key=secret)
    if session is not None:
        app.add_middleware(_FakeSessionMiddleware, session=session)
    return app


# ---------------------------------------------------------------------------
# Bypass branches.
# ---------------------------------------------------------------------------


def test_safe_methods_bypass():
    """GET/HEAD/OPTIONS never go through CSRF — even on enforced paths."""
    app = _make_app({"user_id": 1, "csrf_token": _raw_csrf()})

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.get("/api/plays")

    r = _run(_go())
    assert r.status_code == 200


def test_safe_methods_set_matches_spec():
    """Sanity: middleware's safe-method set matches the doc."""
    assert _SAFE_METHODS == frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def test_anonymous_bypass():
    """No session.user_id → CSRF skipped (matches Flask gate)."""
    app = _make_app(session={})  # empty session — anonymous

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/api/plays", json={})

    r = _run(_go())
    assert r.status_code == 200


def test_xapikey_bypass():
    """X-API-Key present + non-widget path → CSRF skipped."""
    app = _make_app({"user_id": 1, "csrf_token": _raw_csrf()})

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/api/plays", json={}, headers={"X-API-Key": "test-key"})

    r = _run(_go())
    assert r.status_code == 200


def test_widget_path_no_xapikey_bypass():
    """X-API-Key on /api/widget/tokens still requires a CSRF token."""
    app = _make_app({"user_id": 1, "csrf_token": _raw_csrf()})

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(
                "/api/widget/tokens",
                json={},
                headers={"X-API-Key": "test-key"},
            )

    r = _run(_go())
    assert r.status_code == 403
    assert r.json() == {"error": "CSRF token missing or invalid"}


def test_skip_prefix_bypass():
    """Drawings is on the allowlist (Flask is csrf.exempt + no manual CSRF)."""
    app = _make_app({"user_id": 1, "csrf_token": _raw_csrf()})

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/api/drawings", json={})

    r = _run(_go())
    assert r.status_code == 200


def test_skip_prefixes_does_not_swallow_watchlist():
    """``/api/assets`` allowlist must NOT match ``/api/watchlist*``."""
    # Concrete safety check Agent A's spec called out: prefix allowlist
    # is string-startswith, so /api/assets DOES NOT match /api/watchlist.
    sample = "/api/watchlist"
    assert not any(sample.startswith(p) for p in _SKIP_PREFIXES)


# ---------------------------------------------------------------------------
# Enforcement branches.
# ---------------------------------------------------------------------------


def test_session_no_token_rejects():
    """Authenticated session without an X-CSRFToken header → 403."""
    app = _make_app({"user_id": 1, "csrf_token": _raw_csrf()})

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/api/plays", json={})

    r = _run(_go())
    assert r.status_code == 403
    body = r.json()
    assert body == {"error": "CSRF token missing or invalid"}


def test_session_valid_token_accepts():
    """Authenticated session + valid wire token → handler runs (200)."""
    raw = _raw_csrf()
    app = _make_app({"user_id": 1, "csrf_token": raw})

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(
                "/api/plays", json={}, headers={"X-CSRFToken": _wire_token(raw)}
            )

    r = _run(_go())
    assert r.status_code == 200


def test_session_invalid_signature_rejects():
    """Token signed with the wrong secret → 403."""
    raw = _raw_csrf()
    app = _make_app({"user_id": 1, "csrf_token": raw})
    bad_wire = _wire_token(raw, secret="not-the-server-secret")

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/api/plays", json={}, headers={"X-CSRFToken": bad_wire})

    r = _run(_go())
    assert r.status_code == 403


def test_session_token_for_different_user_rejects():
    """Token signature is valid but unwraps to a different raw value → 403."""
    server_raw = _raw_csrf()
    other_raw = _raw_csrf()
    app = _make_app({"user_id": 1, "csrf_token": server_raw})

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # Caller submits a wire token wrapping a different raw value.
            return await c.post(
                "/api/plays", json={}, headers={"X-CSRFToken": _wire_token(other_raw)}
            )

    r = _run(_go())
    assert r.status_code == 403


def test_xcsrftoken_underscore_alias():
    """Accept ``X-CSRF-Token`` with the dash variant for parity."""
    raw = _raw_csrf()
    app = _make_app({"user_id": 1, "csrf_token": raw})

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(
                "/api/plays", json={}, headers={"X-CSRF-Token": _wire_token(raw)}
            )

    r = _run(_go())
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Cross-runtime parity: token issued by Flask must validate in FastAPI.
# ---------------------------------------------------------------------------


def test_flask_issued_token_validates_via_middleware():
    """Generate a token the way Flask does; assert middleware accepts it."""
    raw = _raw_csrf()
    app = _make_app({"user_id": 1, "csrf_token": raw})
    # Mimic what flask_wtf.generate_csrf does at template render time:
    flask_wire = itsdangerous.URLSafeTimedSerializer(SECRET, salt="wtf-csrf-token").dumps(raw)

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(
                "/api/plays", json={}, headers={"X-CSRFToken": flask_wire}
            )

    r = _run(_go())
    assert r.status_code == 200


def test_secret_key_missing_fails_closed(monkeypatch):
    """If SECRET_KEY is empty, every CSRF-eligible request 403s."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    raw = _raw_csrf()
    # Construct middleware with an empty secret directly to exercise the
    # placeholder-secret branch.
    app = FastAPI()

    @app.post("/api/plays")
    async def _h():
        return {"ok": True}

    app.add_middleware(CarracCSRFMiddleware, secret_key="")
    app.add_middleware(_FakeSessionMiddleware, session={"user_id": 1, "csrf_token": raw})

    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(
                "/api/plays", json={}, headers={"X-CSRFToken": _wire_token(raw)}
            )

    r = _run(_go())
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# The /api/v2 mount. ``carrac/asgi.py`` mounts the same FastAPI app at
# ``/api/v2`` and at the root, so every route answers at both paths. The
# check must judge the path the route sees, not the outer URL.
# ---------------------------------------------------------------------------


def _make_dispatcher(session: Mapping[str, Any]):
    from starlette.applications import Starlette
    from starlette.routing import Mount

    inner = _make_app(session)
    return Starlette(routes=[Mount("/api/v2", app=inner), Mount("/", app=inner)])


def _post(app, path: str, headers: dict | None = None):
    async def _go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(path, json={}, headers=headers or {})

    return _run(_go())


def test_v2_mount_write_without_token_rejects():
    app = _make_dispatcher({"user_id": 1, "csrf_token": _raw_csrf()})
    r = _post(app, "/api/v2/api/plays")
    assert r.status_code == 403
    assert r.json() == {"error": "CSRF token missing or invalid"}


def test_v2_mount_write_with_token_accepts():
    raw = _raw_csrf()
    app = _make_dispatcher({"user_id": 1, "csrf_token": raw})
    r = _post(app, "/api/v2/api/plays", {"X-CSRFToken": _wire_token(raw)})
    assert r.status_code == 200
    assert r.json() == {"status": "created", "id": 42}


def test_v2_mount_widget_tokens_keep_no_api_key_bypass():
    app = _make_dispatcher({"user_id": 1, "csrf_token": _raw_csrf()})
    r = _post(app, "/api/v2/api/widget/tokens", {"X-API-Key": "test-key"})
    assert r.status_code == 403


def test_v2_mount_follows_the_same_skip_list_as_the_root_path():
    app = _make_dispatcher({"user_id": 1, "csrf_token": _raw_csrf()})
    assert _post(app, "/api/v2/api/drawings").status_code == 200
    assert _post(app, "/api/drawings").status_code == 200


def test_root_path_unchanged_through_the_dispatcher():
    raw = _raw_csrf()
    app = _make_dispatcher({"user_id": 1, "csrf_token": raw})
    assert _post(app, "/api/plays").status_code == 403
    assert _post(app, "/api/plays", {"X-CSRFToken": _wire_token(raw)}).status_code == 200


def test_skip_list_has_no_v2_blanket_entry():
    assert not any("/api/v2".startswith(p) or p.startswith("/api/v2") for p in _SKIP_PREFIXES)
