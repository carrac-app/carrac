"""One Content-Security-Policy for Flask and FastAPI; relaxed only for headless charts."""

from __future__ import annotations

import re

import httpx
from httpx import ASGITransport

from carrac.csp import build_csp


def _directive(csp: str, name: str) -> str:
    return next(d.strip() for d in csp.split(";") if d.strip().startswith(name + " "))


def _without_nonce(csp: str) -> str:
    return re.sub(r" 'nonce-[^']+'", "", csp)


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


def _csp_for(path: str) -> str:
    import asyncio

    from carrac.fastapi_app.middleware import ResponseHeadersMiddleware

    async def _go():
        transport = ASGITransport(app=ResponseHeadersMiddleware(_ok))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return (await c.get(path)).headers["content-security-policy"]

    return asyncio.new_event_loop().run_until_complete(_go())


def test_fastapi_route_gets_strict_script_src():
    script = _directive(_csp_for("/api/anything"), "script-src")
    assert "'unsafe-inline'" not in script
    assert "'unsafe-eval'" not in script


def test_headless_route_alone_is_relaxed():
    script = _directive(_csp_for("/chart-headless/BTC/1D"), "script-src")
    assert "'unsafe-inline'" in script
    assert "'unsafe-eval'" in script
    share = _directive(_csp_for("/chart-headless/share/abcd1234"), "script-src")
    assert "'unsafe-eval'" in share
    lookalike = _directive(_csp_for("/chart-headless-x/BTC/1D"), "script-src")
    assert "'unsafe-eval'" not in lookalike


def test_real_asgi_app_serves_strict_then_relaxed(asgi_client):
    strict = asgi_client.get("/api/screenshot/ZZCSP/1D")
    assert strict.status_code == 401
    assert "'unsafe-eval'" not in _directive(
        strict.headers["content-security-policy"], "script-src"
    )
    relaxed = asgi_client.get(
        "/chart-headless/BTC/1D", headers={"X-Internal-Key": "test-api-key-for-testing"}
    )
    assert relaxed.status_code == 200
    assert "'unsafe-eval'" in _directive(
        relaxed.headers["content-security-policy"], "script-src"
    )


def test_flask_and_fastapi_emit_the_same_strict_policy(client):
    flask_csp = client.get("/").headers["Content-Security-Policy"]
    assert "'nonce-" in flask_csp
    assert _without_nonce(flask_csp) == _csp_for("/api/anything")
    assert _csp_for("/api/anything") == build_csp()


_OPTIONAL_ENV = (
    "POSTHOG_API_KEY", "POSTHOG_HOST", "PADDLE_API_KEY", "PADDLE_CLIENT_TOKEN",
    "CLOUDFLARE_WEB_ANALYTICS",
)


def _clean_env(monkeypatch):
    for name in _OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)


def test_unconfigured_install_allows_no_optional_third_party(monkeypatch):
    _clean_env(monkeypatch)
    csp = build_csp()
    for host in ("posthog.com", "cloudflareinsights.com", "paddle.com"):
        assert host not in csp
    # The libraries the pages load at runtime stay allowed.
    assert "https://cdn.jsdelivr.net" in _directive(csp, "script-src")
    assert "https://unpkg.com" in _directive(csp, "script-src")


def test_posthog_hosts_follow_the_api_key(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    csp = build_csp()
    assert "https://eu-assets.i.posthog.com" in _directive(csp, "script-src")
    assert "https://eu.i.posthog.com" in _directive(csp, "connect-src")
    assert "paddle.com" not in csp and "cloudflareinsights.com" not in csp


def test_self_hosted_posthog_origin_is_allowed_only_when_keyed(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("POSTHOG_HOST", "https://ph.example.org/")
    assert "ph.example.org" not in build_csp()
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    csp = build_csp()
    assert "https://ph.example.org" in _directive(csp, "script-src")
    assert "https://ph.example.org" in _directive(csp, "connect-src")


def test_paddle_needs_both_the_api_key_and_the_client_token(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("PADDLE_API_KEY", "key")
    assert "paddle.com" not in build_csp()
    monkeypatch.setenv("PADDLE_CLIENT_TOKEN", "tok")
    csp = build_csp()
    for directive in ("script-src", "connect-src", "frame-src"):
        assert "https://*.paddle.com" in _directive(csp, directive)


def test_cloudflare_insights_follows_its_switch(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_WEB_ANALYTICS", "0")
    assert "cloudflareinsights.com" not in build_csp()
    monkeypatch.setenv("CLOUDFLARE_WEB_ANALYTICS", "1")
    assert "https://static.cloudflareinsights.com" in _directive(build_csp(), "script-src")
