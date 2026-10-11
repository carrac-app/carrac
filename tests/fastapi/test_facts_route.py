"""GET /api/facts/{symbol} route tests.

Mirrors tests/fastapi/test_chart_internal.py pattern:
  asgi_client + X-API-Key header auth via SCREENSHOT_API_KEY env var.

Covers:
* 401 without any API key
* 401 with an invalid API key
* 404 when get_asset_facts returns None
* 200 + bundle JSON + Cache-Control header with a valid key + stubbed bundle
* Router import smoke
* /api/facts/ in MIGRATED_PREFIXES
"""

from __future__ import annotations

from unittest.mock import patch



_MOCK_BUNDLE = {
    "symbol": "BTC",
    "ma_stack": {"signal": "bullish", "score": 3},
    "rsi": {"value": 55.0, "zone": "neutral"},
}


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


def test_facts_router_imports_cleanly():
    from carrac.fastapi_app.routers import facts  # noqa: F401


def test_facts_prefix_in_migrated_prefixes():
    from carrac.asgi import MIGRATED_PREFIXES
    assert "/api/facts/" in MIGRATED_PREFIXES


# ---------------------------------------------------------------------------
# Auth gate tests
# ---------------------------------------------------------------------------


def test_facts_no_key_returns_401(asgi_client):
    """GET /api/facts/BTC without any key → 401."""
    resp = asgi_client.get("/api/facts/BTC")
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert "error" in body


def test_facts_invalid_key_returns_401(asgi_client):
    """GET /api/facts/BTC with a wrong key → 401."""
    resp = asgi_client.get(
        "/api/facts/BTC",
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 404 path — bundle not cached
# ---------------------------------------------------------------------------


def test_facts_not_cached_returns_404(asgi_client, monkeypatch):
    """get_asset_facts returning None → 404."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-key-facts-404")

    with patch("carrac.facts.wire.get_asset_facts", return_value=None):
        resp = asgi_client.get(
            "/api/facts/UNKNOWN",
            headers={"X-Internal-Key": "test-key-facts-404"},
        )

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert "error" in body
    assert body.get("symbol") == "UNKNOWN"


# ---------------------------------------------------------------------------
# Happy path — 200 + bundle + Cache-Control
# ---------------------------------------------------------------------------


def test_facts_valid_key_returns_bundle(asgi_client, monkeypatch):
    """GET /api/facts/BTC with valid key + cached bundle → 200 + JSON + Cache-Control."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-key-facts-200")

    with patch("carrac.facts.wire.get_asset_facts", return_value=_MOCK_BUNDLE):
        resp = asgi_client.get(
            "/api/facts/BTC",
            headers={"X-Internal-Key": "test-key-facts-200"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["symbol"] == "BTC"
    assert "ma_stack" in body
    assert "rsi" in body
    cc = resp.headers.get("cache-control", "")
    assert "max-age=1800" in cc
    assert "stale-while-revalidate=300" in cc


def test_facts_x_internal_key_accepted(asgi_client, monkeypatch):
    """X-Internal-Key header is also accepted."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-internal-facts")

    with patch("carrac.facts.wire.get_asset_facts", return_value=_MOCK_BUNDLE):
        resp = asgi_client.get(
            "/api/facts/ETH",
            headers={"X-Internal-Key": "test-internal-facts"},
        )

    assert resp.status_code == 200, resp.text


def test_facts_symbol_uppercased(asgi_client, monkeypatch):
    """Symbol is uppercased before lookup."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-key-upper")
    captured: list[str] = []

    def _stub(sym, r=None):
        captured.append(sym)
        return _MOCK_BUNDLE

    with patch("carrac.facts.wire.get_asset_facts", side_effect=_stub):
        asgi_client.get(
            "/api/facts/btc",
            headers={"X-Internal-Key": "test-key-upper"},
        )

    assert captured and captured[0] == "BTC"
