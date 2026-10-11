"""CacheHeadersMiddleware unit tests.

The middleware mirrors ``carrac/cache_headers.py::apply_cache_headers``'s
API branch. Tests use isolated FastAPI apps so the production middleware
stack doesn't leak in; only the cache layer is exercised.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from httpx import ASGITransport, AsyncClient

from carrac.fastapi_app.cache_middleware import CacheHeadersMiddleware


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _build_app(path: str, method: str = "get"):
    """Tiny FastAPI app: middleware + a single ok handler at ``path``."""
    app = FastAPI()
    app.add_middleware(CacheHeadersMiddleware)
    handler = lambda: JSONResponse({"ok": True})  # noqa: E731
    getattr(app, method)(path)(handler)
    return app


def test_write_no_cache():
    """``POST /api/plays`` matches the write policy → ``no-cache`` header."""

    async def _go():
        app = _build_app("/api/plays", method="post")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.post("/api/plays")

    r = _run(_go())
    assert "no-cache" in r.headers["cache-control"]


def test_private_get_plays_active():
    """``GET /api/plays/active`` → ``private, max-age=30``."""

    async def _go():
        app = _build_app("/api/plays/active")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/plays/active")

    r = _run(_go())
    # The /api/plays prefix matches first in the policy table.
    assert "private" in r.headers["cache-control"]
    assert "max-age=30" in r.headers["cache-control"]


def test_private_get_scanner():
    """``GET /api/scanner`` → ``private, max-age=300, stale-while-revalidate=60``.

    Scanner is tier-gated (free=5 rows, premium=full): ``public`` lets shared
    nginx/CF caches cross-serve one user's response to another.  Must be
    ``private``, as for the report endpoints.
    """

    async def _go():
        app = _build_app("/api/scanner")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/scanner")

    r = _run(_go())
    assert (
        r.headers["cache-control"]
        == "private, max-age=300, stale-while-revalidate=60"
    )


def test_no_match_get_status_falls_back_to_no_cache():
    """``GET /api/status`` has no GET entry → falls back to NO_CACHE."""

    async def _go():
        app = _build_app("/api/status")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/status")

    r = _run(_go())
    assert "no-cache" in r.headers["cache-control"]
    assert "no-store" in r.headers["cache-control"]


def test_handler_set_cache_control_is_preserved():
    """If a handler sets ``Cache-Control`` itself, the middleware does not overwrite."""

    async def _go():
        app = FastAPI()
        app.add_middleware(CacheHeadersMiddleware)

        @app.get("/api/scanner")
        async def _h():
            return Response(
                '{"ok":1}',
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/scanner")

    r = _run(_go())
    assert r.headers["cache-control"] == "no-store"


def test_chart_internal_skip():
    """``/api/chart-internal/<symbol>`` is skipped — handler manages own headers."""

    async def _go():
        app = FastAPI()
        app.add_middleware(CacheHeadersMiddleware)

        @app.get("/api/chart-internal/BTC")
        async def _h():
            # Return without setting Cache-Control deliberately.
            return JSONResponse({"ok": True})

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/chart-internal/BTC")

    r = _run(_go())
    # Middleware skipped → no Cache-Control header set by us.
    assert "cache-control" not in r.headers


def test_non_api_path_skipped():
    """Paths outside ``/api`` / ``/poll`` / ``/widget`` get no Cache-Control."""

    async def _go():
        app = _build_app("/some/other/path")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/some/other/path")

    r = _run(_go())
    assert "cache-control" not in r.headers


# -- Error-response gate: non-2xx must never get a cacheable policy header --


def _build_app_with_status(path: str, status_code: int, method: str = "get"):
    """Tiny FastAPI app that returns a fixed status code at ``path``."""
    app = FastAPI()
    app.add_middleware(CacheHeadersMiddleware)

    async def handler():
        return Response(status_code=status_code)

    getattr(app, method)(path)(handler)
    return app


def test_error_responses_get_no_store_not_policy_header_401():
    """401 on /api/report/morning/latest must NOT receive the public 3600s policy."""

    async def _go():
        app = _build_app_with_status("/api/report/morning/latest", 401)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/report/morning/latest")

    r = _run(_go())
    cc = r.headers.get("cache-control", "")
    assert "no-store" in cc, f"401 must have no-store, got: {cc!r}"
    assert "public" not in cc, f"401 must NOT have public, got: {cc!r}"
    assert "max-age=3600" not in cc, f"401 must NOT have max-age=3600, got: {cc!r}"


def test_error_responses_get_no_store_not_policy_header_403():
    """403 on a public endpoint must get no-store, not the public policy."""

    async def _go():
        app = _build_app_with_status("/api/scanner", 403)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/scanner")

    r = _run(_go())
    cc = r.headers.get("cache-control", "")
    assert "no-store" in cc
    assert "public" not in cc


def test_error_responses_get_no_store_not_policy_header_404():
    """404 on an API endpoint must get no-store."""

    async def _go():
        app = _build_app_with_status("/api/quant/health", 404)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/quant/health")

    r = _run(_go())
    cc = r.headers.get("cache-control", "")
    assert "no-store" in cc
    assert "public" not in cc


def test_error_responses_get_no_store_not_policy_header_500():
    """500 on an API endpoint must get no-store."""

    async def _go():
        app = _build_app_with_status("/api/report/morning/latest", 500)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/report/morning/latest")

    r = _run(_go())
    cc = r.headers.get("cache-control", "")
    assert "no-store" in cc
    assert "public" not in cc


def test_200_responses_still_get_policy_header():
    """200 on /api/report/morning/latest gets the (now private) policy header."""

    async def _go():
        app = FastAPI()
        app.add_middleware(CacheHeadersMiddleware)

        @app.get("/api/report/morning/latest")
        async def _h():
            return JSONResponse({"ok": True}, status_code=200)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            return await c.get("/api/report/morning/latest")

    r = _run(_go())
    cc = r.headers.get("cache-control", "")
    # Report responses vary by auth — `private` forbids the nginx shared
    # cache from cross-serving one caller's report to another.
    assert "private" in cc
    assert "public" not in cc
    assert "max-age=300" in cc
