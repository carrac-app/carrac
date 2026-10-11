"""Tests for GET /api/confluence — FastAPI confluence zones endpoint.

Uses asgi_client to exercise the live strangler routing (FastAPI sub-app).
Monkeypatches carrac.caches.get_cached_scanner to control scanner cache state.
"""

from __future__ import annotations


_ADMIN_HDRS = {"X-Internal-Key": "test-api-key-for-testing"}

# ---------------------------------------------------------------------------
# Fake scanner data — matches the shape expected by get_symbol_confluence:
#   data['results'][*]['long']['confluences']['all'][*] = list of zone dicts
# ---------------------------------------------------------------------------

_SUPPORT_ZONE = {
    "price": 90.0,
    "label": "S1",
    "label_friendly": "Support 1",
    "sources": ["ema200"],
    "members": ["ema200"],
    "strength": 3,
    "tier": "strong",
    "side": "support",
    "span": 0.01,
    "count": 2,
}
_RESISTANCE_ZONE = {
    "price": 110.0,
    "label": "R1",
    "label_friendly": "Resistance 1",
    "sources": ["pdh"],
    "members": ["pdh"],
    "strength": 2,
    "tier": "moderate",
    "side": "resistance",
    "span": 0.01,
    "count": 1,
}

_FAKE_SCANNER_DATA = {
    "computed_at": "2026-06-13T10:00:00",
    "macro": {},
    "results": [
        {
            "symbol": "BTC",
            "price": 100.0,
            "long": {
                "confluences": {
                    "all": [_SUPPORT_ZONE, _RESISTANCE_ZONE],
                },
                "total": 75,
                "verdict": "GO",
            },
            "short": {
                "confluences": {
                    "all": [],  # dedup — long zones cover both directions
                },
                "total": 25,
                "verdict": "AVOID",
            },
            "best_verdict": "GO",
        }
    ],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConfluenceEndpoint:
    def test_200_for_known_symbol(self, asgi_client, monkeypatch):
        """GET /api/confluence?symbol=BTC returns 200 with zones (no tf param)."""
        import carrac.caches as caches

        monkeypatch.setattr(caches, "get_cached_scanner", lambda: _FAKE_SCANNER_DATA)

        r = asgi_client.get(
            "/api/confluence",
            params={"symbol": "BTC"},
            headers=_ADMIN_HDRS,
        )
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["symbol"] == "BTC"
        assert body["price"] == 100.0
        assert "tf" not in body
        assert body["computed_at"] == "2026-06-13T10:00:00"

        # zones list must contain all deduped zones
        zones = body["zones"]
        assert isinstance(zones, list)
        assert len(zones) >= 1

        # each zone carries required enriched fields
        for zone in zones:
            assert "label_friendly" in zone, f"missing label_friendly in {zone}"
            assert "tier" in zone, f"missing tier in {zone}"
            assert "members" in zone, f"missing members in {zone}"

        # supports and resistances are properly split
        assert "supports" in body
        assert "resistances" in body
        for z in body["supports"]:
            assert z["side"] == "support"
        for z in body["resistances"]:
            assert z["side"] == "resistance"

    def test_503_when_scanner_cache_is_none(self, asgi_client, monkeypatch):
        """GET /api/confluence returns 503 when scanner cache is cold (None)."""
        import carrac.caches as caches

        monkeypatch.setattr(caches, "get_cached_scanner", lambda: None)

        r = asgi_client.get(
            "/api/confluence",
            params={"symbol": "BTC"},
            headers=_ADMIN_HDRS,
        )
        assert r.status_code == 503, r.text
        # Custom exception handler (exception_handlers.py) unwraps dict details
        # directly into the response body — no "detail" wrapper key.
        body = r.json()
        assert "error" in body
        assert "zones" in body

    def test_404_for_unknown_symbol(self, asgi_client, monkeypatch):
        """GET /api/confluence returns 404 for a symbol not in the scanner universe."""
        import carrac.caches as caches

        monkeypatch.setattr(caches, "get_cached_scanner", lambda: _FAKE_SCANNER_DATA)

        r = asgi_client.get(
            "/api/confluence",
            params={"symbol": "UNKNOWN"},
            headers=_ADMIN_HDRS,
        )
        assert r.status_code == 404, r.text
        # Custom exception handler unwraps dict details directly into the body.
        body = r.json()
        assert "error" in body

    def test_401_when_unauthenticated(self, asgi_client, monkeypatch):
        """GET /api/confluence without auth credentials returns 401."""
        import carrac.caches as caches

        monkeypatch.setattr(caches, "get_cached_scanner", lambda: _FAKE_SCANNER_DATA)

        r = asgi_client.get(
            "/api/confluence",
            params={"symbol": "BTC"},
        )
        assert r.status_code in (401, 302), r.text

    def test_lowercase_symbol_normalized(self, asgi_client, monkeypatch):
        """Symbol is case-insensitively resolved (btc → BTC); an unknown extra
        query param (tf) is harmlessly ignored, not echoed."""
        import carrac.caches as caches

        monkeypatch.setattr(caches, "get_cached_scanner", lambda: _FAKE_SCANNER_DATA)

        r = asgi_client.get(
            "/api/confluence",
            params={"symbol": "btc", "tf": "4h"},
            headers=_ADMIN_HDRS,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["symbol"] == "BTC"
        assert "tf" not in body
