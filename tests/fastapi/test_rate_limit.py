"""429 shape verification for slowapi.

Uses an isolated ``FastAPI()`` app to avoid bleeding limits across the
production module-level singleton. ``storage_uri="memory://"`` makes the
test Redis-free.

This file also covers ``AsyncOffloadLimiter`` (carrac/fastapi_app/
limiter.py): the 429 contract must hold, AND the storage hit
(which would otherwise run synchronously inline on the event loop for async
routes) must run off it (via ``asyncio.to_thread``) — verified the same way the
rest of the codebase verifies offload elsewhere (``tests/fastapi/
test_async_offload_*.py``): patch the check, record ``threading.get_ident()``,
assert it differs from the test's own event-loop thread.
"""

from __future__ import annotations

import asyncio
import threading

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.requests import Request

from carrac.fastapi_app.exception_handlers import http_exception_handler
from carrac.fastapi_app.limiter import AsyncOffloadLimiter


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_rate_limit_returns_canonical_429():
    """1/minute limiter on a stub route — second hit must yield 429 with the
    canonical JSON shape and a ``Retry-After`` header."""

    async def _go():
        lim = Limiter(key_func=get_remote_address, storage_uri="memory://")
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, http_exception_handler)

        @app.get("/test-rl")
        @lim.limit("1/minute")
        async def _ep(request: Request):
            return {"ok": True}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            r1 = await c.get(
                "/test-rl", headers={"X-Forwarded-For": "1.2.3.4"}
            )
            r2 = await c.get(
                "/test-rl", headers={"X-Forwarded-For": "1.2.3.4"}
            )
            return r1, r2

    r1, r2 = _run(_go())
    assert r1.status_code == 200
    assert r2.status_code == 429
    body = r2.json()
    assert "error" in body
    assert "rate limit" in body["error"].lower()
    # Retry-After header must be present (canonical 429 contract).
    assert "Retry-After" in r2.headers or "retry-after" in r2.headers


def test_async_offload_limiter_returns_canonical_429():
    """Same 429 contract as above, but through the production
    ``AsyncOffloadLimiter`` subclass (not stock ``slowapi.Limiter``) — a
    1/minute limit on an ``async def`` route must still 429 with the
    canonical JSON shape + Retry-After after the budget is exhausted."""

    async def _go():
        lim = AsyncOffloadLimiter(key_func=get_remote_address, storage_uri="memory://")
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, http_exception_handler)
        # Registered so we also exercise _should_exempt for the decorated
        # route (pins that functools.wraps must preserve the
        # func's __module__/__name__ so the middleware doesn't ALSO run a
        # second, redundant, blocking check for the same request).
        app.add_middleware(SlowAPIMiddleware)

        @app.get("/test-rl-offload")
        @lim.limit("1/minute")
        async def _ep(request: Request):
            return {"ok": True}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            r1 = await c.get(
                "/test-rl-offload", headers={"X-Forwarded-For": "5.6.7.8"}
            )
            r2 = await c.get(
                "/test-rl-offload", headers={"X-Forwarded-For": "5.6.7.8"}
            )
            return r1, r2

    r1, r2 = _run(_go())
    assert r1.status_code == 200
    assert r2.status_code == 429
    body = r2.json()
    assert "error" in body
    assert "rate limit" in body["error"].lower()
    assert "Retry-After" in r2.headers or "retry-after" in r2.headers


def test_async_offload_limiter_check_runs_off_event_loop_thread():
    """Pins the core contract: for an ``async def`` route, the storage
    check (``Limiter._check_request_limit``, which is what walks into the
    blocking Redis/limits-library hit) must NOT run on the event-loop thread.

    Patches ``_check_request_limit`` to record ``threading.get_ident()`` —
    same pattern as ``tests/fastapi/test_async_offload_small_routers.py`` —
    and asserts the recorded thread differs from the loop-owning thread the
    test itself runs on (``asgi_client``'s event loop runs on the calling
    thread; see ``tests/fastapi/conftest.py``).
    """
    main_ident = threading.get_ident()

    async def _go():
        lim = AsyncOffloadLimiter(key_func=get_remote_address, storage_uri="memory://")
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, http_exception_handler)
        app.add_middleware(SlowAPIMiddleware)

        recorded = {}
        orig_check = lim._check_request_limit

        def _recording_check(request, endpoint_func, in_middleware=True):
            recorded["ident"] = threading.get_ident()
            recorded["n"] = recorded.get("n", 0) + 1
            return orig_check(request, endpoint_func, in_middleware)

        lim._check_request_limit = _recording_check

        @app.get("/test-rl-thread")
        @lim.limit("5/minute")
        async def _ep(request: Request):
            return {"ok": True}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            resp = await c.get(
                "/test-rl-thread", headers={"X-Forwarded-For": "9.9.9.9"}
            )
        return resp, recorded

    resp, recorded = _run(_go())
    assert resp.status_code == 200
    # Ran exactly once — confirms SlowAPIMiddleware's _should_exempt still
    # recognizes the decorated route (functools.wraps preserved identity)
    # and doesn't ALSO run its own redundant, still-synchronous check.
    assert recorded["n"] == 1
    assert recorded["ident"] != main_ident


def test_async_offload_limiter_sync_route_still_gated():
    """Sync (``def``) routes take the untouched slowapi path (no offload
    needed — FastAPI already thread-pools sync handlers) — confirm the
    429 contract still holds for those too, mirroring
    ``curl_charts.curl_overview``, the one sync route in this codebase
    carrying ``@limiter.limit(...)``."""

    async def _go():
        lim = AsyncOffloadLimiter(key_func=get_remote_address, storage_uri="memory://")
        app = FastAPI()
        app.state.limiter = lim
        app.add_exception_handler(RateLimitExceeded, http_exception_handler)

        @app.get("/test-rl-sync")
        @lim.limit("1/minute")
        def _ep(request: Request):
            return {"ok": True}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            r1 = await c.get(
                "/test-rl-sync", headers={"X-Forwarded-For": "4.3.2.1"}
            )
            r2 = await c.get(
                "/test-rl-sync", headers={"X-Forwarded-For": "4.3.2.1"}
            )
            return r1, r2

    r1, r2 = _run(_go())
    assert r1.status_code == 200
    assert r2.status_code == 429
