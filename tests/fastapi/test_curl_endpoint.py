"""Curl chart endpoint — dispatcher + router integration (spec §7/§8).

Covers the CurlChartPath ASGI dispatcher (carrac/asgi.py) end-to-end through
the FastAPI router. The Rust renderer is mocked — its own contract is covered
by golden tests in the renderer's own repo; here we pin routing, UA dispatch, grammar
errors, precedence, and header behavior.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hosted_site_url(monkeypatch):
    """These tests pin page structure/copy against the hosted instance's
    address; the generated links follow SITE_URL, so say which instance."""
    monkeypatch.setenv("SITE_URL", "https://carrac.cc")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

CURL_UA = {"User-Agent": "curl/8.5.0"}
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/125.0"}


@pytest.fixture(autouse=True)
def fake_render(monkeypatch):
    """Mock the render bridge and the symbol whitelist.

    The conftest points DATA_DIR at an empty tmp dir, so the dynamic symbol
    maps (and therefore the registry) are empty in tests — patch a fixed
    whitelist. The render bridge is mocked too: no binary, no data, no Redis.
    """
    from carrac.curl_charts import registry

    monkeypatch.setattr(registry, "_load_symbols", lambda: {"btc", "eth"})
    registry._invalidate()

    async def fake(symbol, bucket, opts):
        return f"CHART {symbol} {bucket.label} cols={opts.cols}\n"

    monkeypatch.setattr(
        "carrac.fastapi_app.routers.curl_charts.render_chart", fake
    )
    yield
    registry._invalidate()  # don't leak the fake whitelist into other files


class TestUADispatch:
    def test_curl_gets_chart_with_funnel_footer(self, asgi_client):
        r = asgi_client.get("/btc", headers=CURL_UA)
        assert r.status_code == 200
        assert "CHART BTC 1d" in r.text
        assert "carrac.cc" in r.text  # funnel footer appended
        assert r.headers["content-type"].startswith("text/plain")
        assert r.headers["cache-control"] == "public, max-age=60"

    def test_browser_gets_302_to_landing(self, asgi_client):
        r = asgi_client.get("/btc", headers=BROWSER_UA, follow_redirects=False)
        assert r.status_code == 302
        assert "utm_source=terminal" in r.headers["location"]

    def test_empty_ua_gets_chart(self, asgi_client):
        # Symbol paths are UA-agnostic at the dispatcher; the router only
        # redirects *browser-like* UAs — empty UA is a script, serve text.
        r = asgi_client.get("/eth", headers={"User-Agent": ""})
        assert r.status_code == 200
        assert "CHART ETH" in r.text


class TestGrammar:
    def test_window_suffix(self, asgi_client):
        r = asgi_client.get("/btc@10d", headers=CURL_UA)
        assert "CHART BTC 10d" in r.text

    def test_window_snaps_to_bucket(self, asgi_client):
        r = asgi_client.get("/btc@4w", headers=CURL_UA)
        assert "CHART BTC 30d" in r.text

    def test_cols_option(self, asgi_client):
        r = asgi_client.get("/btc?cols=120", headers=CURL_UA)
        assert "cols=120" in r.text

    def test_quiet_drops_funnel_footer(self, asgi_client):
        r = asgi_client.get("/btc?q", headers=CURL_UA)
        assert "CHART BTC" in r.text
        assert "carrac.cc" not in r.text

    def test_bad_window_400_no_store(self, asgi_client):
        r = asgi_client.get("/btc@blorp", headers=CURL_UA)
        assert r.status_code == 400
        assert ":help" in r.text
        assert r.headers["cache-control"] == "no-store"


class TestPrecedence:
    """Existing pages must never be swallowed by the curl dispatcher."""

    @pytest.mark.parametrize(
        "path", ["/mcp", "/market-regime", "/contact", "/login"]
    )
    def test_flask_pages_untouched(self, asgi_client, path):
        r = asgi_client.get(path, headers=BROWSER_UA, follow_redirects=False)
        assert "CHART" not in (r.text or "")
        ct = r.headers.get("content-type", "")
        assert not ct.startswith("text/plain"), f"{path} swallowed by curl route"

    def test_unknown_symbol_falls_through_to_flask(self, asgi_client):
        # Whitelist-gated dispatch: an untracked single segment is NOT ours —
        # Flask serves its normal 404 page (pinned design trade-off, spec §7).
        r = asgi_client.get("/xyzzy", headers=CURL_UA)
        assert "CHART" not in (r.text or "")
        assert r.status_code == 404

    def test_non_get_methods_ignored(self, asgi_client):
        # Dispatcher is GET-only; a POST to a symbol path must not hit FastAPI.
        r = asgi_client.post("/btc", headers=CURL_UA)
        assert "CHART" not in (r.text or "")


class TestSpecialPages:
    def test_help(self, asgi_client):
        r = asgi_client.get("/:help", headers=CURL_UA)
        assert r.status_code == 200
        assert "@10d" in r.text and "?ema" in r.text

    def test_assets_lists_tracked_symbols(self, asgi_client):
        r = asgi_client.get("/:assets", headers=CURL_UA)
        assert r.status_code == 200
        assert "btc" in r.text

    def test_bare_domain_overview_for_curl(self, asgi_client):
        r = asgi_client.get("/", headers=CURL_UA)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert "curl carrac.cc" in r.text

    def test_bare_domain_html_for_browser(self, asgi_client):
        r = asgi_client.get("/", headers=BROWSER_UA, follow_redirects=False)
        assert not r.headers.get("content-type", "").startswith("text/plain")

    def test_bare_domain_html_for_python_clients(self, asgi_client):
        # Non-curl programmatic UAs keep getting the Flask landing — only
        # explicit terminal tools (curl/wget/httpie) get the overview table.
        r = asgi_client.get(
            "/", headers={"User-Agent": "python-httpx/0.27"}, follow_redirects=False
        )
        assert not r.headers.get("content-type", "").startswith("text/plain")


class TestFunnelPackage:
    """curl-funnel package: /reg dual-mode, /:mcp, pure displayed URLs.

    Contract: anything a human READS/TYPES is a pure URL (no query strings);
    utm tags ride only invisible surfaces — browser 302 Locations and the QR
    payload. Attribution for typed entries comes from the short path itself.
    """

    def test_reg_curl_gets_register_page_with_qr(self, asgi_client):
        r = asgi_client.get("/reg", headers=CURL_UA)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert "create your free carrac account" in r.text.lower()
        # displayed URL is pure — nobody types query strings
        assert "carrac.cc/reg" in r.text
        assert "utm" not in r.text
        # ANSI QR present (half-block glyphs)
        assert "▀" in r.text or "█" in r.text
        # funnel entry point: not cacheable
        assert r.headers["cache-control"] == "no-store"

    def test_reg_plain_mode_skips_qr(self, asgi_client):
        r = asgi_client.get("/reg?T", headers=CURL_UA)
        assert r.status_code == 200
        assert "carrac.cc/reg" in r.text
        assert "utm" not in r.text
        # plain mode: URL only, no QR glyphs
        assert "▀" not in r.text and "█" not in r.text

    def test_reg_browser_302_to_register(self, asgi_client):
        r = asgi_client.get("/reg", headers=BROWSER_UA, follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers["location"]
        assert "carrac.cc/register?utm_source=curl" in loc
        assert r.headers["cache-control"] == "no-store"

    def test_mcp_terminal_page_has_endpoint(self, asgi_client):
        r = asgi_client.get("/:mcp", headers=CURL_UA)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        # canonical MCP endpoint URL (mirrors templates/guide.html #mcp)
        assert "https://carrac.cc/mcp" in r.text
        assert "X-API-Key" in r.text
        # ends with the register pointer
        assert "carrac.cc/reg" in r.text

    def test_mcp_browser_302(self, asgi_client):
        r = asgi_client.get("/:mcp", headers=BROWSER_UA, follow_redirects=False)
        assert r.status_code == 302
        assert "utm_source=terminal" in r.headers["location"]

    def test_footer_links_are_pure_urls(self, asgi_client):
        # the footer rides on the chart response; every link must be typeable
        r = asgi_client.get("/btc", headers=CURL_UA)
        assert r.status_code == 200
        assert "carrac.cc" in r.text
        assert "carrac.cc/mcp" in r.text
        assert "carrac.cc/reg" in r.text
        assert "utm" not in r.text

    def test_help_page_is_pure(self, asgi_client):
        r = asgi_client.get("/:help", headers=CURL_UA)
        assert r.status_code == 200
        assert "utm" not in r.text

    def test_reg_tui_terminal_gets_register_page(self, asgi_client):
        r = asgi_client.get("/reg-tui", headers=CURL_UA)
        assert r.status_code == 200
        assert "create your free carrac account" in r.text.lower()
        assert "utm" not in r.text
        assert r.headers["cache-control"] == "no-store"

    def test_reg_tui_browser_302_carries_tui_source(self, asgi_client):
        r = asgi_client.get("/reg-tui", headers=BROWSER_UA, follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers["location"]
        assert "/register" in loc
        assert "utm_source=tui" in loc
        assert r.headers["cache-control"] == "no-store"

    def test_head_requests_resolve_not_404(self, asgi_client):
        # Link-preview bots and uptime/link checkers probe the funnel paths with
        # HEAD; these must resolve (200), not fall through to a Flask 404.
        for path in ("/reg", "/reg-tui", "/btc"):
            r = asgi_client.head(path, headers=CURL_UA)
            assert r.status_code == 200, f"HEAD {path} -> {r.status_code}"

    def test_head_browser_reg_still_redirects(self, asgi_client):
        # A browser HEAD-ing /reg should still get the 302 to /register, not 404.
        r = asgi_client.head("/reg", headers=BROWSER_UA, follow_redirects=False)
        assert r.status_code == 302
        assert "/register" in r.headers["location"]


class TestErrorMapping:
    def test_data_unavailable_503_no_store(self, asgi_client, monkeypatch):
        from carrac.curl_charts.render import DataUnavailable

        async def boom(symbol, bucket, opts):
            raise DataUnavailable("breaker tripped")

        monkeypatch.setattr(
            "carrac.fastapi_app.routers.curl_charts.render_chart", boom
        )
        r = asgi_client.get("/btc", headers=CURL_UA)
        assert r.status_code == 503
        assert "warming up" in r.text
        assert r.headers["cache-control"] == "no-store"

    def test_render_failed_503(self, asgi_client, monkeypatch):
        from carrac.curl_charts.render import RenderFailed

        async def boom(symbol, bucket, opts):
            raise RenderFailed("spawn failed")

        monkeypatch.setattr(
            "carrac.fastapi_app.routers.curl_charts.render_chart", boom
        )
        r = asgi_client.get("/btc", headers=CURL_UA)
        assert r.status_code == 503
        assert "carrac.cc" in r.text
