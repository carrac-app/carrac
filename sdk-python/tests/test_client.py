"""Unit tests for the Python SDK (httpx MockTransport — no network)."""

import pytest
import httpx

from carrac import Client, AuthError, RateLimitError, NotFoundError


def _mock_client(handler):
    """Create a Client backed by a MockTransport handler."""
    transport = httpx.MockTransport(handler)
    c = Client(api_key="test-key", base_url="http://localhost:5002")
    c._http = httpx.Client(
        base_url=c.base_url,
        transport=transport,
        headers={"X-API-Key": c.api_key, "Accept": "application/json"},
    )
    return c


class TestAuth:
    def test_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("CARRAC_API_KEY", raising=False)
        monkeypatch.delenv("CONVEXITY_API_KEY", raising=False)
        with pytest.raises(ValueError):
            Client()

    def test_uses_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("CARRAC_API_KEY", "cvx_k_fromenv")
        c = Client(base_url="http://localhost:5002")
        assert c.api_key == "cvx_k_fromenv"

    def test_legacy_env_var_fallback(self, monkeypatch):
        monkeypatch.delenv("CARRAC_API_KEY", raising=False)
        monkeypatch.setenv("CONVEXITY_API_KEY", "cvx_k_fromenv")
        c = Client(base_url="http://localhost:5002")
        assert c.api_key == "cvx_k_fromenv"


class TestBaseUrl:
    def test_no_default_host(self, monkeypatch):
        monkeypatch.delenv("CARRAC_BASE_URL", raising=False)
        monkeypatch.delenv("CONVEXITY_BASE_URL", raising=False)
        with pytest.raises(ValueError, match="base URL"):
            Client(api_key="k")

    def test_env_fallback_and_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("CARRAC_BASE_URL", "http://env.example:1234/")
        assert Client(api_key="k").base_url == "http://env.example:1234"


class TestResources:
    def test_charts_get_builds_correct_url(self):
        captured = {}

        def handler(req):
            captured["url"] = str(req.url)
            captured["headers"] = dict(req.headers)
            return httpx.Response(200, json={"candles": [], "symbol": "BTC"})

        c = _mock_client(handler)
        out = c.charts.get("BTC", timeframe="1h", indicators="CPR,RSI")
        assert "/api/chart-internal/BTC" in captured["url"]
        assert "timeframe=1h" in captured["url"]
        assert "indicators=CPR%2CRSI" in captured["url"]
        assert captured["headers"]["x-api-key"] == "test-key"
        assert out["symbol"] == "BTC"

    def test_plays_create_posts_json(self):
        captured = {}

        def handler(req):
            captured["method"] = req.method
            captured["body"] = req.content.decode()
            return httpx.Response(201, json={"id": 99, "title": "Test"})

        c = _mock_client(handler)
        out = c.plays.create(title="Test", type="directional", primary_asset="BTC")
        assert captured["method"] == "POST"
        assert '"title":"Test"' in captured["body"] or '"title": "Test"' in captured["body"]
        assert out["id"] == 99

    def test_composite_batch_serializes_list_assets(self):
        captured = {}

        def handler(req):
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"assets": {}})

        c = _mock_client(handler)
        c.composite.batch(["BTC", "ETH"], include=["market", "scanner"])
        assert "assets=BTC%2CETH" in captured["url"]
        assert "include=market%2Cscanner" in captured["url"]

    def test_usage_get_includes_history_param(self):
        captured = {}

        def handler(req):
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"user_id": 1, "rest": {}, "mcp": {}})

        c = _mock_client(handler)
        c.usage.get(history=False, days=7)
        assert "history=false" in captured["url"]
        assert "days=7" in captured["url"]


class TestErrorHandling:
    def test_401_raises_auth_error(self):
        def handler(req):
            return httpx.Response(401, json={"error": "bad key"})

        c = _mock_client(handler)
        c.max_retries = 0
        with pytest.raises(AuthError) as exc:
            c.charts.get("BTC")
        assert exc.value.status_code == 401

    def test_404_raises_notfound(self):
        def handler(req):
            return httpx.Response(404, json={"error": "not found"})

        c = _mock_client(handler)
        c.max_retries = 0
        with pytest.raises(NotFoundError):
            c.plays.get(999)

    def test_429_raises_after_retries(self, monkeypatch):
        monkeypatch.setattr("carrac.client.time.sleep", lambda s: None)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "2"},
                                  json={"error": "rate limited", "limit": 60,
                                        "window": "minute", "retry_after": 2})

        c = _mock_client(handler)
        c.max_retries = 2
        with pytest.raises(RateLimitError) as exc:
            c.charts.get("BTC")
        assert calls["n"] == 3  # initial + 2 retries
        # retry_after comes from the Retry-After header, not the JSON body.
        assert exc.value.retry_after == 2

    def test_5xx_backoff_starts_at_one_second(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr("carrac.client.time.sleep", sleeps.append)
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] <= 3:
                return httpx.Response(500, text="oops")
            return httpx.Response(200, json={"ok": True})

        c = _mock_client(handler)
        c.max_retries = 3
        c.charts.get("BTC")
        assert calls["n"] == 4
        assert sleeps == [1, 2, 4]


class TestPaths:
    def test_alerts_delete_builds_correct_url(self):
        captured = {}

        def handler(req):
            captured["method"] = req.method
            captured["path"] = req.url.path
            return httpx.Response(204)

        c = _mock_client(handler)
        c.alerts.delete(42)
        assert captured["method"] == "DELETE"
        assert captured["path"] == "/api/alerts/42"

    def test_alerts_toggle_posts_to_correct_url(self):
        captured = {}

        def handler(req):
            captured["method"] = req.method
            captured["path"] = req.url.path
            return httpx.Response(200, json={"id": 7, "active": False})

        c = _mock_client(handler)
        c.alerts.toggle(7)
        assert captured["method"] == "POST"
        assert captured["path"] == "/api/alerts/7/toggle"

    def test_keys_revoke_builds_correct_url(self):
        captured = {}

        def handler(req):
            captured["method"] = req.method
            captured["path"] = req.url.path
            return httpx.Response(200, json={"success": True})

        c = _mock_client(handler)
        c.keys.revoke(5)
        assert captured["method"] == "DELETE"
        assert captured["path"] == "/api/keys/5"
