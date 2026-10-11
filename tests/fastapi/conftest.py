"""Test fixtures for the FastAPI sub-app suite.

Provides an ``asgi_client`` (httpx.AsyncClient + ASGITransport bound to
the strangler-mounted ``asgi_app``) so tests exercise the same routing
that production traffic hits — FastAPI sub-app for migrated paths,
Flask for everything else.

The ``app`` fixture from ``tests/conftest.py`` (Flask test client) is
unaffected; existing Flask tests keep working in parallel during the
strangler migration.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Iterator

import httpx
import pytest
from httpx import ASGITransport


@pytest.fixture
def api_user(app):  # noqa: ARG001 — needs the Flask app env first
    """Factory: create a user of ``tier`` with a real per-user API key.

    Returns ``(headers, user_id)``. Requests through ``asgi_client`` with
    these headers resolve to that user exactly as a production API-key caller
    does, on both the FastAPI and the Flask side of the dispatcher.
    ``X-API-Key`` also bypasses the CSRF middleware (by design, matching real
    API clients).
    """
    from user_db import user_db
    from carrac.apikeys.auth import generate_api_key, hash_api_key

    def _create(tier: str = "member") -> tuple[dict, int]:
        email = f"api-{secrets.token_hex(4)}@test.dev"
        uid = user_db.create_user(email, "StrongPass!123", tos_accepted=True)
        assert uid
        user_db.set_user_tier(uid, tier)
        raw = generate_api_key()   # canonical cvx_k_<32 hex> format
        key_id = user_db.create_api_key(uid, hash_api_key(raw),
                                        token_suffix=raw[-4:], name="tests")
        assert key_id
        return {"X-API-Key": raw}, uid

    return _create


@pytest.fixture
def member_key(api_user):
    """``(headers, user_id)`` for a member-tier API-key user."""
    return api_user("member")


class KeyClient:
    """``asgi_client`` that sends one user's API key on every request."""

    def __init__(self, client, headers: dict, user_id: int) -> None:
        self._client = client
        self._headers = headers
        self.user_id = user_id

    def _send(self, method: str, path: str, **kwargs):
        headers = {**self._headers, **kwargs.pop("headers", {})}
        return self._client.request(method, path, headers=headers, **kwargs)

    def get(self, path: str, **kwargs):
        return self._send("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self._send("POST", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self._send("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs):
        return self._send("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self._send("DELETE", path, **kwargs)


@pytest.fixture
def key_client(asgi_client, api_user):
    """Factory: a :class:`KeyClient` for a new user of ``tier``."""
    def _create(tier: str = "member") -> KeyClient:
        headers, uid = api_user(tier)
        return KeyClient(asgi_client, headers, uid)

    return _create


@pytest.fixture
def asgi_app(app):  # noqa: ARG001 — depends on Flask app fixture for env init
    """Return the strangler ASGI app composed of FastAPI + Flask.

    The Flask ``app`` fixture is requested as a dependency so its
    environment setup (TESTING, SECRET_KEY, DATA_DIR, etc.) runs first;
    ``carrac.asgi`` constructs its own Flask app via ``create_app()``
    so its config picks up those env vars.
    """
    # Importing inside the fixture means ``carrac.asgi.create_app()``
    # runs against the test environment set up by the parent ``app``
    # fixture in ``tests/conftest.py``.
    from carrac.asgi import FLASK_WSGI_EXECUTOR, asgi_app as _app

    # Flask requests run on one long-lived executor thread, and the SQLite
    # connections are thread-local. ``rebind_user_db`` (run by the ``app``
    # fixture) only drops the connections of the thread that calls it, so
    # without this the Flask side would keep reading the previous test's
    # database.
    FLASK_WSGI_EXECUTOR.submit(_drop_thread_local_sqlite_conns).result()

    # Rate-limit windows are keyed per user id, and every test's fresh
    # database hands out the same ids, so counts would otherwise carry over
    # from one test to the next.
    from carrac.fastapi_app.limiter import limiter
    limiter.reset()
    return _app


def _drop_thread_local_sqlite_conns() -> None:
    from carrac import db_pool
    from user_db import user_db

    for local in (db_pool._sqlite_local, user_db._local):
        conn = getattr(local, "conn", None)
        if conn is not None:
            conn.close()
            local.conn = None


@pytest.fixture
def asgi_client(asgi_app) -> Iterator[httpx.Client]:
    """Sync httpx.Client driving the strangler ASGI app.

    Sync wrapper around ``httpx.AsyncClient`` so existing pytest-style
    tests don't need ``pytest-asyncio``. Use ``client.get(...)`` exactly
    like the Flask test client.
    """
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


@pytest.fixture
def login_as(asgi_app):  # noqa: ARG001 — needs the Flask app env first
    """Context-manager factory that logs the ASGI client in as ``user``.

    ``require_login`` is a FastAPI dependency, so it has to be replaced via
    ``dependency_overrides`` (patching the name in ``deps.auth`` does nothing
    once the router has bound it). Journal routes authenticate through
    ``journal_user`` instead, so that is overridden as well. ``get_request_tier`` is called directly by
    the AI routers, so it is patched in each router module that imports it.
    """
    from contextlib import contextmanager
    from unittest.mock import patch

    from carrac.fastapi_app import fastapi_app
    from carrac.fastapi_app.deps.auth import require_login
    from carrac.fastapi_app.routers.journal import journal_user

    @contextmanager
    def _login(user: dict):
        fastapi_app.dependency_overrides[require_login] = lambda: user
        fastapi_app.dependency_overrides[journal_user] = lambda: user
        patches = [
            patch(f"carrac.fastapi_app.routers.{mod}.get_request_tier",
                  return_value=user.get("tier", "free"))
            for mod in ("journal_ai", "ai_aux")
        ]
        for p in patches:
            p.start()
        try:
            yield
        finally:
            for p in patches:
                p.stop()
            fastapi_app.dependency_overrides.pop(require_login, None)
            fastapi_app.dependency_overrides.pop(journal_user, None)

    return _login
