"""Tests for GET /api/liq-heatmap — FastAPI liquidation-heatmap endpoint.

Uses asgi_client to exercise the live strangler routing (FastAPI sub-app),
same pattern as test_confluence_endpoint.py. Monkeypatches
carrac.liq_heatmap.get_heatmap_series to control persisted-cycle state
without touching the real quant DB.
"""

from __future__ import annotations


_ADMIN_HDRS = {"X-Internal-Key": "test-api-key-for-testing"}

_FAKE_OK_PAYLOAD = {
    "symbol": "BTC",
    "band_pct": 0.25,
    "n_buckets": 150,
    "paint_quantile": 0.85,
    "cycles": [
        {
            "ts": "2026-07-18T00:00:00+00:00",
            "price": 65000.0,
            "band_low": 48750.0,
            "band_high": 81250.0,
            "bands": [
                {"price_low": 63000.0, "price_high": 63500.0,
                 "intensity_long": 0.9, "intensity_short": 0.0},
                {"price_low": 67000.0, "price_high": 67500.0,
                 "intensity_long": 0.0, "intensity_short": 0.8},
            ],
        },
    ],
}


class TestLiqHeatmapEndpoint:
    def test_200_for_covered_symbol(self, asgi_client, monkeypatch):
        import carrac.liq_heatmap as lh

        monkeypatch.setattr(lh, "get_heatmap_series", lambda symbol, hours=None: ("ok", _FAKE_OK_PAYLOAD))

        r = asgi_client.get(
            "/api/liq-heatmap",
            params={"symbol": "BTC"},
            headers=_ADMIN_HDRS,
        )
        assert r.status_code == 200, r.text
        body = r.json()

        assert body["symbol"] == "BTC"
        assert body["band_pct"] == 0.25
        assert body["n_buckets"] == 150
        assert "paint_quantile" in body

        cycles = body["cycles"]
        assert isinstance(cycles, list)
        assert len(cycles) == 1
        cycle = cycles[0]
        assert "ts" in cycle and "price" in cycle and "bands" in cycle
        for band in cycle["bands"]:
            assert set(band.keys()) == {
                "price_low", "price_high", "intensity_long", "intensity_short",
            }

    def test_503_when_not_yet_computed(self, asgi_client, monkeypatch):
        import carrac.liq_heatmap as lh

        monkeypatch.setattr(lh, "get_heatmap_series", lambda symbol, hours=None: ("cold", None))

        r = asgi_client.get(
            "/api/liq-heatmap",
            params={"symbol": "BTC"},
            headers=_ADMIN_HDRS,
        )
        assert r.status_code == 503, r.text
        body = r.json()
        assert "error" in body
        assert "cycles" in body

    def test_404_for_uncovered_symbol(self, asgi_client, monkeypatch):
        import carrac.liq_heatmap as lh

        monkeypatch.setattr(lh, "get_heatmap_series", lambda symbol, hours=None: ("not_covered", None))

        r = asgi_client.get(
            "/api/liq-heatmap",
            params={"symbol": "SOMEOBSCURECOIN"},
            headers=_ADMIN_HDRS,
        )
        assert r.status_code == 404, r.text
        body = r.json()
        assert "error" in body
        assert "covered_symbols" in body
        assert "BTC" in body["covered_symbols"]

    def test_401_when_unauthenticated(self, asgi_client, monkeypatch):
        import carrac.liq_heatmap as lh

        monkeypatch.setattr(lh, "get_heatmap_series", lambda symbol, hours=None: ("ok", _FAKE_OK_PAYLOAD))

        r = asgi_client.get(
            "/api/liq-heatmap",
            params={"symbol": "BTC"},
        )
        assert r.status_code in (401, 302), r.text

    def test_hours_param_forwarded(self, asgi_client, monkeypatch):
        import carrac.liq_heatmap as lh

        captured = {}

        def _fake(symbol, hours=None):
            captured["symbol"] = symbol
            captured["hours"] = hours
            return "ok", _FAKE_OK_PAYLOAD

        monkeypatch.setattr(lh, "get_heatmap_series", _fake)

        r = asgi_client.get(
            "/api/liq-heatmap",
            params={"symbol": "btc", "hours": 48},
            headers=_ADMIN_HDRS,
        )
        assert r.status_code == 200, r.text
        assert captured["symbol"] == "btc"
        assert captured["hours"] == 48
