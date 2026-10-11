"""Flask/FastAPI parity for the screenshot + chart-headless
front doors (fastapi_app/routers/screenshot_public.py).

Mirrors tests/parity/test_chart_public_parity.py's strategy: every test
class drives BOTH the Flask test client (``client``/``auth_client``, from
tests/conftest.py) and the strangler-mounted ASGI app (``asgi_client``, same
routing production traffic hits) against the SAME mocked state, and asserts
the two responses agree. This file lives under tests/parity/, not
tests/fastapi/, so tests/fastapi/conftest.py's fixtures aren't in scope —
the ``asgi_app``/``asgi_client`` fixtures are duplicated here, self-contained
(same reasoning tests/parity/test_chart_public_parity.py already documents).

Unlike /api/chart, none of these three routes has session-cookie auth, so
every ``asgi_client`` call below authenticates via the shared API key
(``SCREENSHOT_API_KEY``, forced to ``test-api-key-for-testing`` in
tests/conftest.py) — the ONLY auth mode these routes support.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Iterator

import httpx
import pytest
from httpx import ASGITransport

API_KEY = "test-api-key-for-testing"  # forced in tests/conftest.py
API_KEY_HEADERS = {"X-Internal-Key": API_KEY}

_NONCE_RE = re.compile(rb'nonce="[^"]*"')


def _strip_nonce(body: bytes) -> bytes:
    """chart_headless.html embeds a freshly-generated CSP nonce
    (security.generate_csp_nonce) per request — it legitimately differs
    between the separate Flask call and the FastAPI-replayed call, same as
    tests/parity/test_chart_public_parity.py's _strip_volatile() drops
    fields that tick between two separate calls."""
    return _NONCE_RE.sub(b'nonce="X"', body)


# ---------------------------------------------------------------------------
# asgi_client fixture — self-contained (see module docstring).
# ---------------------------------------------------------------------------

@pytest.fixture
def asgi_app(app):  # noqa: ARG001 — depends on the Flask app fixture for env init
    from carrac.asgi import asgi_app as _app
    return _app


@pytest.fixture
def asgi_client(asgi_app) -> Iterator[httpx.Client]:
    transport = ASGITransport(app=asgi_app)
    async_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")

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


def _cache_paths(asset, timeframe, width, height):
    cache_dir = os.path.join("data", "screenshots")
    return cache_dir, os.path.join(cache_dir, f"{asset}_{timeframe}_{width}x{height}.jpg")


def _cleanup_cache(asset, timeframe, width, height):
    _, cache_file = _cache_paths(asset, timeframe, width, height)
    if os.path.exists(cache_file):
        os.remove(cache_file)


# ---------------------------------------------------------------------------
# 1. Unauthenticated / bad-key / ?key= — GET /api/screenshot/<asset>/<tf>
# ---------------------------------------------------------------------------

class TestScreenshotAuthParity:
    def test_no_key_401_matches(self, client, asgi_client):
        flask_resp = client.get("/api/screenshot/BTC/1D")
        fastapi_resp = asgi_client.get("/api/screenshot/BTC/1D")
        assert flask_resp.status_code == fastapi_resp.status_code == 401
        assert flask_resp.get_json() == fastapi_resp.json()

    def test_bad_key_401_matches(self, client, asgi_client):
        bad_headers = {"X-API-Key": "definitely-not-real"}
        flask_resp = client.get("/api/screenshot/BTC/1D", headers=bad_headers)
        fastapi_resp = asgi_client.get("/api/screenshot/BTC/1D", headers=bad_headers)
        assert flask_resp.status_code == fastapi_resp.status_code == 401
        assert flask_resp.get_json() == fastapi_resp.json()

    def test_url_param_key_400_matches(self, client, asgi_client):
        flask_resp = client.get("/api/screenshot/BTC/1D?key=whatever")
        fastapi_resp = asgi_client.get("/api/screenshot/BTC/1D?key=whatever")
        assert flask_resp.status_code == fastapi_resp.status_code == 400
        assert flask_resp.get_json() == fastapi_resp.json()


# ---------------------------------------------------------------------------
# 2. Successful render (mocked Playwright) — body + headers match
# ---------------------------------------------------------------------------

class TestScreenshotRenderParity:
    def test_mocked_render_body_and_headers_match(self, client, asgi_client, monkeypatch):
        from carrac.reports import screenshots as scr

        monkeypatch.setattr(
            scr, "_render_screenshot_via_playwright",
            lambda *a, **k: b"\xff\xd8\xff\xdb-fake-jpeg",
        )

        asset, tf, w, h = "ZZPF0", "1D", 340, 200
        try:
            flask_resp = client.get(
                f"/api/screenshot/{asset}/{tf}?width={w}&height={h}", headers=API_KEY_HEADERS,
            )
            assert flask_resp.status_code == 200, flask_resp.data[:200]

            fastapi_resp = asgi_client.get(
                f"/api/screenshot/{asset}/{tf}?width={w}&height={h}", headers=API_KEY_HEADERS,
            )
            assert fastapi_resp.status_code == 200, fastapi_resp.text[:200]

            # Both requests are cache MISS -> HIT in sequence (Flask writes
            # the disk cache file first; FastAPI's replay then reads it back).
            assert flask_resp.headers.get("X-Cache") == "MISS"
            assert fastapi_resp.headers.get("X-Cache") == "HIT"
            assert flask_resp.data == fastapi_resp.content
            assert flask_resp.headers.get("Content-Type") == fastapi_resp.headers.get("content-type")
        finally:
            _cleanup_cache(asset, tf, w, h)


# ---------------------------------------------------------------------------
# 3. /chart-headless/<asset>/<timeframe> parity
# ---------------------------------------------------------------------------

class TestChartHeadlessParity:
    def test_no_key_401_matches(self, client, asgi_client):
        flask_resp = client.get("/chart-headless/BTC/1D")
        fastapi_resp = asgi_client.get("/chart-headless/BTC/1D")
        assert flask_resp.status_code == fastapi_resp.status_code == 401

    def test_authed_200_body_matches(self, client, asgi_client):
        flask_resp = client.get("/chart-headless/BTC/1D", headers=API_KEY_HEADERS)
        fastapi_resp = asgi_client.get("/chart-headless/BTC/1D", headers=API_KEY_HEADERS)
        assert flask_resp.status_code == fastapi_resp.status_code == 200
        assert _strip_nonce(flask_resp.get_data()) == _strip_nonce(fastapi_resp.content)
        assert flask_resp.headers.get("Content-Type") == fastapi_resp.headers.get("content-type")


# ---------------------------------------------------------------------------
# 4. /chart-headless/share/<slug> parity — 401 / 404 / 410 / 200
# ---------------------------------------------------------------------------

def _fake_payload(symbol="BTC", timeframe="1D", n=130, base=60000.0):
    ohlcv = []
    for i in range(n):
        o = base + i * 10
        c = o + (5 if i % 2 == 0 else -5)
        ohlcv.append({
            "time": 1700000000 + i * 86400,
            "open": o, "high": max(o, c) + 8, "low": min(o, c) - 8, "close": c,
        })
    return {"ohlcv": ohlcv, "symbol": symbol.upper(), "timeframe": timeframe}


@pytest.fixture
def patch_chart_payload(monkeypatch):
    import carrac.charts.service as svc

    def _fake(symbol, *, mode="htf", timeframe=None, days=None):
        return _fake_payload(symbol, timeframe or "1D")

    monkeypatch.setattr(svc, "build_chart_payload", _fake)
    return _fake


class TestChartHeadlessShareParity:
    def test_no_key_401_matches(self, client, asgi_client):
        flask_resp = client.get("/chart-headless/share/abcdefgh")
        fastapi_resp = asgi_client.get("/chart-headless/share/abcdefgh")
        assert flask_resp.status_code == fastapi_resp.status_code == 401

    def test_unknown_slug_404_matches(self, client, asgi_client):
        flask_resp = client.get("/chart-headless/share/zzzzzzzz", headers=API_KEY_HEADERS)
        fastapi_resp = asgi_client.get("/chart-headless/share/zzzzzzzz", headers=API_KEY_HEADERS)
        assert flask_resp.status_code == fastapi_resp.status_code == 404

    def test_malformed_slug_404_matches(self, client, asgi_client):
        flask_resp = client.get("/chart-headless/share/AB", headers=API_KEY_HEADERS)
        fastapi_resp = asgi_client.get("/chart-headless/share/AB", headers=API_KEY_HEADERS)
        assert flask_resp.status_code == fastapi_resp.status_code == 404

    def test_revoked_share_410_matches(self, client, asgi_client, app, patch_chart_payload):
        from carrac.plays import shares as sh

        share = sh.create_chart_share(9188, "ETH", "1D")
        assert share is not None
        n = sh.revoke_share_by_slug(share["slug"], 9188)
        assert n == 1

        flask_resp = client.get(f"/chart-headless/share/{share['slug']}", headers=API_KEY_HEADERS)
        fastapi_resp = asgi_client.get(f"/chart-headless/share/{share['slug']}", headers=API_KEY_HEADERS)
        assert flask_resp.status_code == fastapi_resp.status_code == 410

    def test_valid_share_200_body_matches(self, client, asgi_client, app, patch_chart_payload):
        from carrac.plays import shares as sh

        share = sh.create_chart_share(9189, "SOL", "1D")
        assert share is not None

        flask_resp = client.get(f"/chart-headless/share/{share['slug']}", headers=API_KEY_HEADERS)
        fastapi_resp = asgi_client.get(f"/chart-headless/share/{share['slug']}", headers=API_KEY_HEADERS)
        assert flask_resp.status_code == fastapi_resp.status_code == 200
        assert _strip_nonce(flask_resp.get_data()) == _strip_nonce(fastapi_resp.content)
