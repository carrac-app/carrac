"""Pins that six small routers offload blocking calls off the event loop.

Covers: scanner.py (3 handlers), reports.py (1), widget.py (2),
chart_share.py (2), curl_charts.py (1 reachable), billing_paddle.py (1).

Each test monkeypatches the narrowest blocking call a handler makes with a
wrapper that records ``threading.get_ident()``, drives the endpoint through
``asgi_client`` (whose event loop runs on the test's own thread — see
``tests/fastapi/conftest.py``), and asserts the recorded ident differs from
the test thread's ident — i.e. the blocking call ran on a worker thread via
``asyncio.to_thread``, not on the event loop thread.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time

import pytest

_ADMIN_HDRS = {"X-Internal-Key": "test-api-key-for-testing"}


def _main_ident() -> int:
    return threading.get_ident()


# ---------------------------------------------------------------------------
# scanner.py — GET /api/scanner
# ---------------------------------------------------------------------------


def test_get_scanner_cache_read_offloaded(asgi_client, monkeypatch):

    recorded = {}
    fake_data = {
        "results": [{"symbol": "BTC", "best_verdict": "BUY", "long": {}, "short": {}}],
        "macro": {},
        "computed_at": "2026-07-09T00:00:00Z",
        "event_risk": None,
    }

    def _fake_get_cached_scanner(allow_stale=False):
        recorded["ident"] = threading.get_ident()
        return fake_data

    monkeypatch.setattr("carrac.caches.get_cached_scanner", _fake_get_cached_scanner)

    resp = asgi_client.get("/api/scanner", headers=_ADMIN_HDRS)
    assert resp.status_code == 200
    assert recorded["ident"] != _main_ident()


# ---------------------------------------------------------------------------
# scanner.py — GET /api/confluence
# ---------------------------------------------------------------------------


def test_get_confluence_offloaded(asgi_client, monkeypatch):
    recorded = {}

    def _fake_get_symbol_confluence(symbol):
        recorded["ident"] = threading.get_ident()
        return (
            {
                "symbol": "BTC",
                "price": 60000.0,
                "computed_at": "2026-07-09T00:00:00Z",
                "all": [],
                "supports": [],
                "resistances": [],
            },
            "ok",
        )

    monkeypatch.setattr(
        "carrac.scanner.confluence_query.get_symbol_confluence",
        _fake_get_symbol_confluence,
    )

    resp = asgi_client.get("/api/confluence?symbol=BTC", headers=_ADMIN_HDRS)
    assert resp.status_code == 200
    assert recorded["ident"] != _main_ident()


# ---------------------------------------------------------------------------
# scanner.py — GET /api/relative-strength
# ---------------------------------------------------------------------------


def test_relative_strength_compute_offloaded(asgi_client, monkeypatch):
    import carrac.charts.routes as charts_routes

    recorded = {}

    monkeypatch.setattr(charts_routes, "_get_cached_rs", lambda: None)

    def _fake_compute():
        recorded["ident"] = threading.get_ident()
        return [{"symbol": "BTC", "rs": 1.0}]

    monkeypatch.setattr(charts_routes, "_compute_rs_rankings", _fake_compute)
    monkeypatch.setattr(charts_routes, "_set_cached_rs", lambda rankings: None)

    resp = asgi_client.get("/api/relative-strength", headers=_ADMIN_HDRS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert recorded["ident"] != _main_ident()


# ---------------------------------------------------------------------------
# reports.py — GET /api/report/morning/latest
# ---------------------------------------------------------------------------


class _FakeReportDB:
    def get_latest_report(self, report_type=None):
        return {
            "content": "## The Story\nhello\n",
            "created_at": "2026-07-09",
            "id": 1,
            "summary": None,
            "audio_script": None,
            "metadata": None,
        }

    def get_personal_section(self, report_id, user_id):
        return None


def test_morning_report_db_read_offloaded(asgi_client, monkeypatch):
    from carrac import content_db as cdb
    import carrac.fastapi_app.routers.reports as rr

    recorded = {}

    class _RecordingDB(_FakeReportDB):
        def get_latest_report(self, report_type=None):
            recorded["ident"] = threading.get_ident()
            return super().get_latest_report(report_type)

    monkeypatch.setattr(cdb, "get_content_db", lambda: _RecordingDB())
    monkeypatch.setattr(rr, "get_request_tier", lambda request: "premium")

    resp = asgi_client.get("/api/report/morning/latest", headers=_ADMIN_HDRS)
    assert resp.status_code == 200
    assert recorded["ident"] != _main_ident()


# ---------------------------------------------------------------------------
# widget.py — POST /api/widget/tokens
# ---------------------------------------------------------------------------


def test_widget_create_token_db_offloaded(asgi_client, member_key):
    headers, _uid = member_key
    import user_db as udb_mod

    recorded = {}
    orig_method = udb_mod.user_db.list_widget_tokens
    try:
        udb_mod.user_db.list_widget_tokens = lambda user_id: (
            recorded.__setitem__("ident", threading.get_ident()) or orig_method(user_id)
        )
        resp = asgi_client.post(
            "/api/widget/tokens", json={"name": "test-token"}, headers=headers
        )
    finally:
        udb_mod.user_db.list_widget_tokens = orig_method

    assert resp.status_code == 201
    assert recorded["ident"] != _main_ident()


# ---------------------------------------------------------------------------
# widget.py — DELETE /api/widget/tokens/{token_id}
# ---------------------------------------------------------------------------


def test_widget_revoke_token_db_offloaded(asgi_client, member_key):
    headers, _uid = member_key
    import user_db as udb_mod

    create_resp = asgi_client.post(
        "/api/widget/tokens", json={"name": "to-revoke"}, headers=headers
    )
    assert create_resp.status_code == 201
    token_id = create_resp.json()["id"]

    recorded = {}
    orig_method = udb_mod.user_db.revoke_widget_token
    try:
        udb_mod.user_db.revoke_widget_token = lambda tid, uid: (
            recorded.__setitem__("ident", threading.get_ident()) or orig_method(tid, uid)
        )
        resp = asgi_client.delete(f"/api/widget/tokens/{token_id}", headers=headers)
    finally:
        udb_mod.user_db.revoke_widget_token = orig_method

    assert resp.status_code == 200
    assert recorded["ident"] != _main_ident()


# ---------------------------------------------------------------------------
# chart_share.py — POST /api/chart/share
# ---------------------------------------------------------------------------


def _fake_chart_payload(symbol="BTC", timeframe="1D", n=130, base=60000.0):
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
        return _fake_chart_payload(symbol, timeframe or "1D")

    monkeypatch.setattr(svc, "build_chart_payload", _fake)
    return _fake


def test_chart_share_mint_db_offloaded(asgi_client, member_key, patch_chart_payload, monkeypatch):
    headers, _uid = member_key
    import carrac.plays.shares as shares_mod

    # This test is about thread-offload, not the chart-share render path — skip the
    # real screenshot-service/quant-DB network attempts they'd otherwise make.
    monkeypatch.setattr(shares_mod, "_render_via_screenshot_service", lambda *a, **k: None)
    monkeypatch.setattr(shares_mod, "_build_chart_context", lambda sym: {})

    recorded = {}
    orig = shares_mod.count_active_shares

    def _wrapped(user_id):
        recorded["ident"] = threading.get_ident()
        return orig(user_id)

    # count_active_shares is imported lazily inside the handler (from
    # carrac.plays.shares import ...), so patch the source module attribute.
    import unittest.mock as mock
    with mock.patch.object(shares_mod, "count_active_shares", _wrapped):
        resp = asgi_client.post(
            "/api/chart/share",
            json={"symbol": "BTC", "timeframe": "1D"},
            headers=headers,
        )

    assert resp.status_code == 201
    assert recorded["ident"] != _main_ident()


# ---------------------------------------------------------------------------
# chart_share.py — DELETE /api/chart/share/{slug}
# ---------------------------------------------------------------------------


def test_chart_share_revoke_db_offloaded(asgi_client, member_key, patch_chart_payload, monkeypatch):
    headers, _uid = member_key
    import carrac.plays.shares as _shares_mod_early
    monkeypatch.setattr(_shares_mod_early, "_render_via_screenshot_service", lambda *a, **k: None)
    monkeypatch.setattr(_shares_mod_early, "_build_chart_context", lambda sym: {})
    mint = asgi_client.post(
        "/api/chart/share",
        json={"symbol": "BTC", "timeframe": "1D"},
        headers=headers,
    )
    slug = mint.json()["slug"]

    import carrac.plays.shares as shares_mod

    recorded = {}
    orig = shares_mod.revoke_share_by_slug

    import unittest.mock as mock

    def _wrapped(slug_, user_id):
        recorded["ident"] = threading.get_ident()
        return orig(slug_, user_id)

    with mock.patch.object(shares_mod, "revoke_share_by_slug", _wrapped):
        resp = asgi_client.delete(f"/api/chart/share/{slug}", headers=headers)

    assert resp.status_code == 200
    assert recorded["ident"] != _main_ident()


# ---------------------------------------------------------------------------
# curl_charts.py — GET /{symbol_spec}  (registry.resolve offload)
# ---------------------------------------------------------------------------


def test_curl_chart_registry_resolve_offloaded(asgi_client, monkeypatch):
    from carrac.curl_charts import registry
    import carrac.fastapi_app.routers.curl_charts as cc

    monkeypatch.setattr(registry, "_load_symbols", lambda: {"btc"})
    registry._invalidate()

    recorded = {}
    orig_resolve = registry.resolve

    def _wrapped_resolve(symbol):
        recorded["ident"] = threading.get_ident()
        return orig_resolve(symbol)

    monkeypatch.setattr(registry, "resolve", _wrapped_resolve)

    async def fake_render(symbol, bucket, opts):
        return f"CHART {symbol} {bucket.label} cols={opts.cols}\n"

    monkeypatch.setattr(cc, "render_chart", fake_render)

    resp = asgi_client.get("/btc", headers={"User-Agent": "curl/8.5.0"})
    assert resp.status_code == 200
    assert "CHART BTC" in resp.text
    assert recorded["ident"] != _main_ident()

    registry._invalidate()


# ---------------------------------------------------------------------------
# billing_paddle.py — POST /api/billing/paddle-webhook
# ---------------------------------------------------------------------------

_PADDLE_SECRET = "test-paddle-webhook-secret-offload"


def _sign(body: str, secret: str, ts: int | None = None) -> str:
    if ts is None:
        ts = int(time.time())
    signed_payload = f"{ts}:{body}"
    h1 = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


def test_billing_paddle_dispatch_and_inbox_offloaded(asgi_client, monkeypatch):
    import carrac.fastapi_app.routers.billing_paddle as bp

    monkeypatch.setattr(bp.Config, "PADDLE_WEBHOOK_SECRET", _PADDLE_SECRET)

    recorded = {}

    def _fake_inbox_record(event_id, event_type, payload):
        recorded["inbox"] = threading.get_ident()

    def _fake_mark_processed(event_id):
        recorded["mark"] = threading.get_ident()

    def _fake_handle_customer_created(data, event_id):
        recorded["dispatch"] = threading.get_ident()

    monkeypatch.setattr(bp, "_inbox_record", _fake_inbox_record)
    monkeypatch.setattr(bp, "_inbox_mark_processed", _fake_mark_processed)
    monkeypatch.setattr(bp, "_handle_customer_created", _fake_handle_customer_created)

    payload = {
        "event_type": "customer.created",
        "event_id": "evt-offload-001",
        "data": {"id": "ctm_test", "custom_data": {"user_id": "99"}},
    }
    body = json.dumps(payload)
    sig = _sign(body, _PADDLE_SECRET)

    resp = asgi_client.post(
        "/api/billing/paddle-webhook",
        content=body.encode("utf-8"),
        headers={"Content-Type": "application/json", "Paddle-Signature": sig},
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert recorded["inbox"] != _main_ident()
    assert recorded["dispatch"] != _main_ident()
    assert recorded["mark"] != _main_ident()
