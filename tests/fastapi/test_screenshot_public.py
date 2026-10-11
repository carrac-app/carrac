"""Screenshot / chart-headless FastAPI front door.

See tests/parity/test_screenshot_public_parity.py for Flask/FastAPI response
parity coverage (headers, body, auth, 404/410 slug semantics). This file
covers: router wiring/match-order, auth rejection across all three routes,
the 503-busy passthrough, the rate-limit-parity mechanics
(verified in isolation, since the shared app's Flask-Limiter is disabled
under TestConfig — see the class docstring below), and the
thread-teardown pin.
"""

from __future__ import annotations

import asyncio
import threading
import time
import concurrent.futures

import httpx
import pytest
from httpx import ASGITransport

API_KEY = "test-api-key-for-testing"  # forced in tests/conftest.py
API_KEY_HEADERS = {"X-Internal-Key": API_KEY}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Router import + match-order smoke tests
# ---------------------------------------------------------------------------


def test_screenshot_public_router_imports_cleanly():
    from carrac.fastapi_app.routers import screenshot_public  # noqa: F401

    paths = [r.path for r in screenshot_public.router.routes]
    assert "/api/screenshot/{asset}/{timeframe}" in paths
    assert "/chart-headless/{asset}/{timeframe}" in paths
    assert "/chart-headless/share/{slug}" in paths


def test_chart_headless_share_route_registered_before_plain_route():
    """Starlette resolves ambiguous path-param routes by REGISTRATION ORDER
    (unlike Werkzeug's Flask url_map, which prefers static segments
    regardless of order) — /chart-headless/share/{slug} MUST be declared
    before /chart-headless/{asset}/{timeframe} or a share URL would be
    mis-captured as asset='share', timeframe=<slug>."""
    from carrac.fastapi_app.routers import screenshot_public

    paths = [r.path for r in screenshot_public.router.routes]
    assert paths.index("/chart-headless/share/{slug}") < paths.index(
        "/chart-headless/{asset}/{timeframe}"
    )


def test_kill_switch_enabled_by_default():
    from carrac.asgi import SCREENSHOT_ASGI_ENABLED, MIGRATED_SCREENSHOT_PREFIXES

    assert SCREENSHOT_ASGI_ENABLED is True
    assert MIGRATED_SCREENSHOT_PREFIXES == ("/api/screenshot/", "/chart-headless/")


def test_kill_switch_disabled_env_yields_empty_prefixes(monkeypatch):
    import importlib
    import carrac.asgi as asgi_mod

    monkeypatch.setenv("SCREENSHOT_ASGI_ENABLED", "0")
    try:
        importlib.reload(asgi_mod)
        assert asgi_mod.SCREENSHOT_ASGI_ENABLED is False
        assert asgi_mod.MIGRATED_SCREENSHOT_PREFIXES == ()
    finally:
        monkeypatch.delenv("SCREENSHOT_ASGI_ENABLED", raising=False)
        importlib.reload(asgi_mod)
        assert asgi_mod.SCREENSHOT_ASGI_ENABLED is True


# ---------------------------------------------------------------------------
# Auth rejection — all three routes, via the real strangler ASGI app
# ---------------------------------------------------------------------------


class TestScreenshotAuth:
    def test_missing_key_401(self, asgi_client):
        resp = asgi_client.get("/api/screenshot/BTC/1D")
        assert resp.status_code == 401
        assert resp.json() == {"error": "Unauthorized"}

    def test_bad_key_401(self, asgi_client):
        resp = asgi_client.get("/api/screenshot/BTC/1D", headers={"X-API-Key": "nope"})
        assert resp.status_code == 401

    def test_url_param_key_rejected_400(self, asgi_client):
        resp = asgi_client.get("/api/screenshot/BTC/1D?key=whatever")
        assert resp.status_code == 400
        assert "X-API-Key" in resp.json()["error"]

    def test_internal_key_header_honoured(self, asgi_client):
        """The shared-key change widened screenshot_chart's key extraction to prefer
        X-Internal-Key (falling back to X-API-Key) — see
        carrac/fastapi_app/routers/screenshot_public.py's module docstring.
        A caller sending ONLY X-Internal-Key now authenticates (no 401)."""
        resp = asgi_client.get(
            "/api/screenshot/BTC/1D", headers={"X-Internal-Key": API_KEY}
        )
        assert resp.status_code != 401


class TestChartHeadlessAuth:
    def test_missing_key_401(self, asgi_client):
        resp = asgi_client.get("/chart-headless/BTC/1D")
        assert resp.status_code == 401

    def test_api_key_header_accepted(self, asgi_client):
        resp = asgi_client.get("/chart-headless/BTC/1D", headers=API_KEY_HEADERS)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_internal_key_header_also_accepted(self, asgi_client):
        resp = asgi_client.get(
            "/chart-headless/BTC/1D", headers={"X-Internal-Key": API_KEY}
        )
        assert resp.status_code == 200


class TestChartHeadlessShareAuth:
    def test_missing_key_401(self, asgi_client):
        resp = asgi_client.get("/chart-headless/share/abcdefgh")
        assert resp.status_code == 401

    def test_unknown_slug_404(self, asgi_client):
        resp = asgi_client.get(
            "/chart-headless/share/zzzzzzzz", headers=API_KEY_HEADERS
        )
        assert resp.status_code == 404

    def test_malformed_slug_404(self, asgi_client):
        resp = asgi_client.get("/chart-headless/share/AB", headers=API_KEY_HEADERS)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 503 busy passthrough survives the front door
# ---------------------------------------------------------------------------


class TestBusyPassthrough:
    def test_queue_timeout_503_with_retry_after(self, asgi_app, monkeypatch):
        from carrac.reports import screenshots as scr

        monkeypatch.setattr(scr, "SCREENSHOT_MAX_CONCURRENCY", 1)
        monkeypatch.setattr(scr, "_render_semaphore", threading.BoundedSemaphore(1))
        monkeypatch.setattr(scr, "_active_renders", 0)
        monkeypatch.setattr(scr, "SCREENSHOT_QUEUE_TIMEOUT_SEC", 0.3)

        held = scr._render_semaphore.acquire(blocking=False)
        assert held, "test setup: could not acquire the fresh semaphore"
        try:
            async def _go():
                transport = ASGITransport(app=asgi_app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    return await client.get(
                        "/api/screenshot/ZZFD1/1D", headers=API_KEY_HEADERS
                    )

            resp = _run(_go())
            assert resp.status_code == 503
            assert resp.headers.get("Retry-After") == "0"
            assert "busy" in resp.json()["error"].lower()
        finally:
            scr._render_semaphore.release()


# ---------------------------------------------------------------------------
# Param passthrough + successful render round-trip
# ---------------------------------------------------------------------------


class TestScreenshotRenderRoundTrip:
    def test_mocked_render_returns_200_and_caches(self, asgi_app, monkeypatch):
        import os

        from carrac.reports import screenshots as scr

        calls = []

        def fake_render(asset, timeframe, indicators, width, height, quality, api_key):
            calls.append((asset, timeframe, indicators, width, height, quality))
            return b"\xff\xd8\xff\xdb-fake-jpeg"

        monkeypatch.setattr(scr, "_render_screenshot_via_playwright", fake_render)

        cache_file = os.path.join("data", "screenshots", "ZZFD2_1D_320x180.jpg")

        async def _go():
            transport = ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r1 = await client.get(
                    "/api/screenshot/ZZFD2/1D?width=320&height=180&indicators=CPR",
                    headers=API_KEY_HEADERS,
                )
                r2 = await client.get(
                    "/api/screenshot/ZZFD2/1D?width=320&height=180&indicators=CPR",
                    headers=API_KEY_HEADERS,
                )
                return r1, r2

        try:
            r1, r2 = _run(_go())
            assert r1.status_code == 200
            assert r1.headers.get("X-Cache") == "MISS"
            assert r1.headers.get("content-type") == "image/jpeg"
            assert r2.status_code == 200
            assert r2.headers.get("X-Cache") == "HIT"
            assert len(calls) == 1, "second request should be served from disk cache"
            assert calls[0][:3] == ("ZZFD2", "1D", "CPR")
        finally:
            if os.path.exists(cache_file):
                os.remove(cache_file)


# ---------------------------------------------------------------------------
# Rate-limit mechanics — verified in isolation
# ---------------------------------------------------------------------------
#
# tests/conftest.py's `app` fixture builds the Flask app via
# `create_app(TestConfig)`, and `TestConfig.RATELIMIT_ENABLED = False`
# (carrac/config.py). flask_limiter.Limiter's `.enabled` flag lives on the
# SHARED `carrac.extensions.limiter` singleton, so once ANY test's `app`
# fixture has run (a dependency of the `asgi_client`/`asgi_app` fixtures
# above), rate limiting is globally disabled for the rest of the test
# session — an intentional test-suite property (deterministic tests, no
# real limits interfering), not something these tests can or should fight.
# Mirrors tests/fastapi/test_chart_public.py's TestInnerLimiterExemption,
# which solves the identical problem by building an ISOLATED Flask app +
# fresh Limiter wired the same way as production, rather than asserting
# against the shared, test-disabled singleton.


class TestRateLimitReplayMechanics:
    @staticmethod
    def _make_isolated_app():
        from flask import Flask, jsonify
        from flask_limiter import Limiter
        from flask_limiter.util import get_remote_address

        probe_app = Flask(__name__)
        probe_limiter = Limiter(
            app=probe_app, key_func=get_remote_address, storage_uri="memory://"
        )

        @probe_app.route("/probe")
        @probe_limiter.limit("3 per minute")
        def probe():
            return jsonify({"ok": True})

        return probe_app, probe

    def test_direct_call_replay_enforces_the_limit(self):
        """Control: flask_limiter's LimitDecorator wraps the view function
        ITSELF (not just a before_request hook) — calling the decorated
        view directly inside test_request_context (screenshot_public.py's
        replay idiom), never through Flask's full dispatch cycle, still
        raises RateLimitExceeded once the limit is hit."""
        from flask_limiter.errors import RateLimitExceeded

        app, probe = self._make_isolated_app()
        statuses = []
        for _ in range(5):
            try:
                with app.test_request_context(
                    "/probe", environ_overrides={"REMOTE_ADDR": "203.0.113.5"}
                ):
                    probe()
                statuses.append("ok")
            except RateLimitExceeded:
                statuses.append("limited")

        assert statuses.count("limited") > 0, (
            "expected the direct-call replay to eventually raise "
            f"RateLimitExceeded, got {statuses}"
        )
        assert statuses[:3] == ["ok", "ok", "ok"]

    def test_environ_override_keys_by_real_remote_addr(self):
        """Two different REMOTE_ADDR overrides get INDEPENDENT buckets —
        proving _client_ip()'s real-IP threading in screenshot_public.py
        actually matters (without it every distinct real caller replayed
        through the front door would share one bucket)."""
        from flask_limiter.errors import RateLimitExceeded

        app, probe = self._make_isolated_app()

        def _hit(ip):
            with app.test_request_context("/probe", environ_overrides={"REMOTE_ADDR": ip}):
                probe()

        for _ in range(3):
            _hit("198.51.100.1")

        with pytest.raises(RateLimitExceeded):
            _hit("198.51.100.1")

        # A different IP is unaffected by the first IP's exhausted bucket.
        _hit("198.51.100.2")

    def test_rate_limit_exceeded_converts_to_429_via_get_response(self, monkeypatch):
        """Unit-level: screenshot_public._replay_flask_screenshot must catch
        the RateLimitExceeded HTTPException screenshot_chart's decorator
        raises and render it via get_response(), not let it fall through to
        the generic except-Exception 500 handler."""
        from flask_limiter.errors import RateLimitExceeded
        from flask_limiter.wrappers import Limit as _Limit
        from limits import parse

        import carrac.reports.screenshots as scr_mod
        from carrac.fastapi_app.routers import screenshot_public as sp

        def _raise_rate_limited(asset, timeframe):
            limit = _Limit(parse("10/minute"), lambda: "203.0.113.5", None)
            raise RateLimitExceeded(limit)

        monkeypatch.setattr(scr_mod, "screenshot_chart", _raise_rate_limited)

        status_code, body, headers = sp._replay_flask_screenshot(
            "BTC", "1D", "", {}, "203.0.113.5",
        )
        assert status_code == 429, body[:200]


# ---------------------------------------------------------------------------
# Thread-teardown survives asyncio.to_thread (point 2)
# ---------------------------------------------------------------------------


class _FakePW:
    def stop(self):
        pass


class _FakePage:
    def __init__(self, thread_idents):
        self._thread_idents = thread_idents

    def route(self, pattern, handler):
        pass

    def goto(self, url, timeout=None):
        self._thread_idents.append(threading.get_ident())

    def wait_for_function(self, *a, **k):
        pass

    def title(self):
        return "READY"

    def text_content(self, *a, **k):
        return ""

    def screenshot(self, **k):
        return b"\xff\xd8\xff\xdb-fake-jpeg"


class _FakeContext:
    def __init__(self, thread_idents):
        self._thread_idents = thread_idents

    def new_page(self):
        return _FakePage(self._thread_idents)

    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, thread_idents):
        self._thread_idents = thread_idents

    def new_context(self, **kwargs):
        return _FakeContext(self._thread_idents)

    def close(self):
        pass


class TestThreadTeardownSurvivesAsyncioToThread:
    """Point 2 (the caveat): _drop_cached_browser_for_this_thread()
    exists because Playwright's sync API leaves a
    lingering asyncio event loop on whatever OS thread launched the browser.
    It protects asgiref's WsgiToAsgi worker-thread pool and, equally,
    asyncio.to_thread's default-executor pool, which also reuses worker
    threads across calls. This test renders once
    (with a monkeypatched Playwright — no real browser), then immediately
    issues a SECOND to_thread-routed request forced onto the exact same OS
    thread (a single-worker executor guarantees reuse, not just probabilistic
    reuse), and asserts it succeeds — i.e. the teardown leaves the thread
    clean for whatever runs on it next, exactly as it already did for
    asgiref."""

    def test_render_then_reused_thread_survives_second_to_thread_call(
        self, asgi_app, monkeypatch
    ):
        import os

        from carrac.reports import screenshots as scr

        thread_idents = []

        def fake_get_persistent_browser():
            slot = getattr(scr._browser_local, "slot", None)
            if slot is not None:
                return slot["browser"]
            browser = _FakeBrowser(thread_idents)
            slot = {
                "pw": _FakePW(),
                "browser": browser,
                "last_used": time.time(),
                "thread_name": threading.current_thread().name,
            }
            scr._browser_local.slot = slot
            with scr._browsers_registry_lock:
                scr._browsers_registry.append(slot)
            return browser

        monkeypatch.setattr(scr, "_get_persistent_browser", fake_get_persistent_browser)

        cache_file = os.path.join("data", "screenshots", "ZZTD1_1D_330x190.jpg")

        async def _go():
            loop = asyncio.get_event_loop()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            loop.set_default_executor(executor)

            transport = ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r1 = await client.get(
                    "/api/screenshot/ZZTD1/1D?width=330&height=190",
                    headers=API_KEY_HEADERS,
                )
                # Second to_thread-routed request on the SAME (single-worker)
                # thread pool — reuses the chart-headless front door, also
                # wired through asyncio.to_thread.
                r2 = await client.get(
                    "/chart-headless/BTC/1D", headers=API_KEY_HEADERS
                )
                return r1, r2

        try:
            r1, r2 = _run(_go())
            assert r1.status_code == 200, r1.text
            assert r2.status_code == 200, r2.text
            assert len(thread_idents) == 1
            assert scr._browsers_registry == [], (
                "teardown did not clear the thread-local browser registry "
                "after the render — a lingering slot here is exactly the "
                "failure mode of the old stopgap"
            )
        finally:
            if os.path.exists(cache_file):
                os.remove(cache_file)
