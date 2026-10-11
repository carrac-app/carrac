"""assemble_asset_detail parallel sub-fetches.

Tests that:
1. The response from /api/asset/{symbol}/detail has the expected keys
   (shape-parity with baseline).
2. The mocked sub-fetches are all called (nothing dropped in
   the parallel rewrite).
3. Errors in individual sub-fetches degrade gracefully (availability
   dict reflects the failure, the rest of the response is intact).

Uses asgi_client (FastAPI strangler) so the real FastAPI route is hit.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Expected top-level keys (golden shape from the docstring of assemble_asset_detail)
# ---------------------------------------------------------------------------
EXPECTED_KEYS = {
    "symbol",
    "price",
    "change_24h",
    "technicals",
    "levels",
    "scanner",
    "sentiment",
    "plays",
    "alerts",
    "drawings",
    "positions",
    "availability",
}


# ---------------------------------------------------------------------------
# Shared mock data
# ---------------------------------------------------------------------------

def _make_mocks():
    """Return a dict of patch targets → mock return values."""
    htf_asset = {
        "symbol": "BTC",
        "consensio": {
            "total": {"positions": 5, "max_positions": 6},
            "1D": {"signal": "bullish"},
            "3D": {"signal": "bullish"},
            "1W": {"signal": "neutral"},
        },
        "weekly_rsi": 62.5,
        "ema_ribbon": {"trend": "bullish"},
        "adx": {"adx": 28.3},
        "cpr": {"pivot": 80000.0, "tc": 81000.0, "bc": 79000.0},
        "pdh": 82000.0,
        "pdl": 78000.0,
        "latest_price": 80000.0,
    }
    scanner_result = {
        "BTC": {
            "long": {"total": 8, "verdict": "STRONG"},
            "short": {"total": 2, "verdict": "AVOID"},
            "best_verdict": "STRONG",
        }
    }
    regime = {"regime": "Risk-On", "score": 0.75}
    prices = {"BTC": {"price": 80500.0, "change_pct": 1.2}}
    plays = [
        {
            "id": 1,
            "title": "BTC Long",
            "status": "watching",
            "direction": "long",
            "conditions": {"rules": [{"met": True}, {"met": False}]},
        }
    ]
    alerts = [{"id": "a1", "symbol": "BTC"}, {"id": "a2", "symbol": "BTC"}]
    drawings = {"1D": [{"drawing_type": "trendline", "label": "R1", "price_level": 82000}]}
    positions = [
        {
            "id": 10,
            "asset": "BTC",
            "direction": "long",
            "entry_price": 79000.0,
            "stop_price": 77000.0,
            "target_price": 90000.0,
            "opened_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    return dict(
        htf_asset=htf_asset,
        scanner_result=scanner_result,
        regime=regime,
        prices=prices,
        plays=plays,
        alerts=alerts,
        drawings=drawings,
        positions=positions,
    )


# ---------------------------------------------------------------------------
# Helpers — patch all the sub-fetch callables inside assemblers
# ---------------------------------------------------------------------------

def _patch_assemblers(m, monkeypatch):
    """Patch all external I/O inside carrac.composite.assemblers."""
    import carrac.composite.assemblers as asm

    htf_by_symbol = {"BTC": m["htf_asset"]}
    scanner_by_symbol = m["scanner_result"]
    prices = m["prices"]

    monkeypatch.setattr(asm, "_get_htf_by_symbol", lambda: htf_by_symbol)
    monkeypatch.setattr(asm, "_get_scanner_by_symbol", lambda: scanner_by_symbol)
    monkeypatch.setattr(asm, "_get_prices", lambda: prices)

    # get_cached_regime
    fake_regime_cache = MagicMock(return_value=m["regime"])
    monkeypatch.setattr(
        "carrac.composite.assemblers.get_cached_regime" if False else "carrac.caches.get_cached_regime",
        fake_regime_cache,
        raising=False,
    )

    # list_plays
    monkeypatch.setattr(
        "carrac.plays.models.list_plays",
        lambda user_id, asset=None: m["plays"],
        raising=False,
    )

    # get_user_alerts
    all_alerts = m["alerts"] + [{"id": "a3", "symbol": "ETH"}]
    monkeypatch.setattr(
        "push_notifications.get_user_alerts",
        lambda uid: all_alerts,
        raising=False,
    )

    # content_db
    fake_content_db = MagicMock()
    fake_content_db.get_all_drawings_grouped.return_value = {"BTC": m["drawings"]}
    fake_content_db.get_open_trades.return_value = m["positions"]
    monkeypatch.setattr(asm, "_get_content_db", lambda: fake_content_db)

    return fake_content_db


# ---------------------------------------------------------------------------
# Shape-parity test via asgi_client
# ---------------------------------------------------------------------------


def test_asset_detail_response_shape(asgi_client, monkeypatch):
    """GET /api/asset/BTC/detail → 200 with all expected keys present."""

    m = _make_mocks()
    _patch_assemblers(m, monkeypatch)

    from carrac.fastapi_app import fastapi_app
    from carrac.fastapi_app.deps.auth import auth_or_key

    # auth_or_key is a FastAPI dependency: it has to be replaced through
    # dependency_overrides, patching the name does nothing once the route
    # has captured it.
    fastapi_app.dependency_overrides[auth_or_key] = lambda: {
        "user": {"id": 1, "email": "test@example.com"},
        "source": "session",
        "tier": "premium",
    }
    try:
        resp = asgi_client.get("/api/asset/BTC/detail")
    finally:
        fastapi_app.dependency_overrides.pop(auth_or_key, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    missing = EXPECTED_KEYS - set(body.keys())
    assert not missing, f"Missing keys in response: {missing}"
    assert body["symbol"] == "BTC"


# ---------------------------------------------------------------------------
# Direct assemble_asset_detail shape/key test (no HTTP layer)
# ---------------------------------------------------------------------------


def test_assemble_asset_detail_keys(monkeypatch):
    """assemble_asset_detail returns all expected top-level keys."""
    import carrac.composite.assemblers as asm

    m = _make_mocks()
    _patch_assemblers(m, monkeypatch)

    # Patch get_cached_regime inside the closure
    import carrac.caches as _caches
    monkeypatch.setattr(_caches, "get_cached_regime", lambda: m["regime"])

    result = asm.assemble_asset_detail("BTC", user_id=1)

    missing = EXPECTED_KEYS - set(result.keys())
    assert not missing, f"Missing keys: {missing}"
    assert result["symbol"] == "BTC"


def test_assemble_asset_detail_price_populated(monkeypatch):
    """Price and change_24h are populated from mock prices."""
    import carrac.composite.assemblers as asm
    import carrac.caches as _caches

    m = _make_mocks()
    _patch_assemblers(m, monkeypatch)
    monkeypatch.setattr(_caches, "get_cached_regime", lambda: m["regime"])

    result = asm.assemble_asset_detail("BTC", user_id=1)
    assert result["price"] == 80500.0
    assert result["change_24h"] == 1.2


def test_assemble_asset_detail_plays_summarized(monkeypatch):
    """Plays are returned as summaries with conditions_met computed."""
    import carrac.composite.assemblers as asm
    import carrac.caches as _caches

    m = _make_mocks()
    _patch_assemblers(m, monkeypatch)
    monkeypatch.setattr(_caches, "get_cached_regime", lambda: m["regime"])

    result = asm.assemble_asset_detail("BTC", user_id=1)
    assert len(result["plays"]) == 1
    play = result["plays"][0]
    assert play["id"] == 1
    assert play["conditions_met"] == "1/2"


def test_assemble_asset_detail_drawings_flattened(monkeypatch):
    """Drawings are returned as a flat list (not nested by TF)."""
    import carrac.composite.assemblers as asm
    import carrac.caches as _caches

    m = _make_mocks()
    _patch_assemblers(m, monkeypatch)
    monkeypatch.setattr(_caches, "get_cached_regime", lambda: m["regime"])

    result = asm.assemble_asset_detail("BTC", user_id=1)
    assert isinstance(result["drawings"], list)
    assert len(result["drawings"]) >= 1
    assert result["drawings"][0]["timeframe"] == "1D"


def test_assemble_asset_detail_positions_enriched(monkeypatch):
    """Positions have unrealized_pnl_pct injected from current price."""
    import carrac.composite.assemblers as asm
    import carrac.caches as _caches

    m = _make_mocks()
    _patch_assemblers(m, monkeypatch)
    monkeypatch.setattr(_caches, "get_cached_regime", lambda: m["regime"])

    result = asm.assemble_asset_detail("BTC", user_id=1)
    assert len(result["positions"]) == 1
    pos = result["positions"][0]
    assert "unrealized_pnl_pct" in pos
    # BTC price 80500, entry 79000 → (80500-79000)/79000 * 100 ≈ 1.9
    assert pos["unrealized_pnl_pct"] == pytest.approx(1.9, rel=0.01)


def test_assemble_asset_detail_sub_fetch_error_degrades_gracefully(monkeypatch):
    """A failing sub-fetch (plays) marks availability 'error' but rest is intact."""
    import carrac.composite.assemblers as asm
    import carrac.caches as _caches

    m = _make_mocks()
    _patch_assemblers(m, monkeypatch)
    monkeypatch.setattr(_caches, "get_cached_regime", lambda: m["regime"])

    # Break list_plays
    monkeypatch.setattr(
        "carrac.plays.models.list_plays",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")),
        raising=False,
    )

    result = asm.assemble_asset_detail("BTC", user_id=1)
    assert result["plays"] == []
    assert result["availability"].get("plays") == "error"
    # Other keys still present and valid
    assert result["symbol"] == "BTC"
    assert result["price"] == 80500.0


def test_assemble_asset_detail_availability_all_ok(monkeypatch):
    """When all sub-fetches succeed, availability has no 'error' entries."""
    import carrac.composite.assemblers as asm
    import carrac.caches as _caches

    m = _make_mocks()
    _patch_assemblers(m, monkeypatch)
    monkeypatch.setattr(_caches, "get_cached_regime", lambda: m["regime"])

    result = asm.assemble_asset_detail("BTC", user_id=1)
    errors = {k: v for k, v in result["availability"].items() if v == "error"}
    assert not errors, f"Unexpected errors in availability: {errors}"
