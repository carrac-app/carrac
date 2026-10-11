"""Chart shares (kind='chart') — END-TO-END through the strangler ASGI app.

Same strangler-parity discipline as the play +
trade shares: the FastAPI-live chart-share endpoints are exercised through
``asgi_client`` with a real per-user API key (``member_key`` fixture in
conftest).

``build_chart_payload`` (the server-side candle assembler) is monkeypatched
to a deterministic OHLCV series so these tests don't touch the network /
exchange clients.

Covers: 401 anon, mint 201 + absolute URLs + frozen candles, always-mint
(no get-or-create — two posts = two slugs), 404 on no-data, shared quota
402, slug-scoped revoke → public 410 across runtimes.
"""

from __future__ import annotations

import pytest

from carrac.plays import shares as sh


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
    """Patch build_chart_payload at the import site used by the freezer."""
    import carrac.charts.service as svc

    def _fake(symbol, *, mode="htf", timeframe=None, days=None):
        return _fake_payload(symbol, timeframe or "1D")

    monkeypatch.setattr(svc, "build_chart_payload", _fake)
    return _fake


def test_mint_requires_auth(asgi_client, patch_chart_payload):
    resp = asgi_client.post("/api/chart/share",
                            json={"symbol": "BTC", "timeframe": "1D"})
    assert resp.status_code in (401, 302)


def test_mint_returns_absolute_payload_and_frozen_candles(
        asgi_client, member_key, patch_chart_payload):
    headers, _uid = member_key
    resp = asgi_client.post("/api/chart/share",
                            json={"symbol": "btc", "timeframe": "1D"},
                            headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["url"].startswith("http") and data["url"].endswith(f"/s/{data['slug']}")
    assert data["image_url"].endswith(f"/s/{data['slug']}/card.png")
    assert data["snapshot_symbol"] == "BTC"
    assert data["snapshot_timeframe"] == "1D"

    snap = sh.get_share_by_slug(data["slug"])["snapshot"]
    # all 130 fake candles frozen into the snapshot (under the 500 cap)
    assert snap["candle_count"] == 130
    assert len(snap["candles"]) == 130
    assert snap["last_price"] is not None
    assert "change_pct" in snap


def test_each_post_mints_a_new_slug(asgi_client, member_key, patch_chart_payload):
    headers, _uid = member_key
    a = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                         headers=headers).json()
    b = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                         headers=headers).json()
    assert a["slug"] != b["slug"]   # a chart share is a moment in time


def test_no_chart_data_404(asgi_client, member_key, monkeypatch):
    import carrac.charts.service as svc
    monkeypatch.setattr(svc, "build_chart_payload",
                        lambda *a, **k: {"ohlcv": [], "symbol": "ZZZ", "timeframe": "1D"})
    headers, _uid = member_key
    resp = asgi_client.post("/api/chart/share",
                            json={"symbol": "ZZZ", "timeframe": "1D"}, headers=headers)
    assert resp.status_code == 404


def test_extra_field_rejected(asgi_client, member_key, patch_chart_payload):
    headers, _uid = member_key
    resp = asgi_client.post("/api/chart/share",
                            json={"symbol": "BTC", "timeframe": "1D", "candles": [1, 2, 3]},
                            headers=headers)
    assert resp.status_code == 400   # extra="forbid" → 422 normalized to 400


def test_quota_pool_shared(asgi_client, member_key, patch_chart_payload, monkeypatch):
    headers, _uid = member_key
    monkeypatch.setitem(sh.SHARE_QUOTA_BY_TIER, "member", 1)

    first = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                             headers=headers)
    assert first.status_code == 201
    second = asgi_client.post("/api/chart/share", json={"symbol": "ETH", "timeframe": "4h"},
                              headers=headers)
    assert second.status_code == 402
    body = second.json()
    assert body["limit_type"] == "share"
    assert body["current"] == 1 and body["max"] == 1


def test_revoke_by_slug_gives_410(asgi_client, client, member_key, patch_chart_payload):
    headers, _uid = member_key
    slug = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers).json()["slug"]
    assert client.get(f"/s/{slug}").status_code == 200

    resp = asgi_client.delete(f"/api/chart/share/{slug}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["revoked"] == 1

    # Public reads via the Flask client (main thread, per-test DB) — see the
    # play/trade revoke tests for why not asgi_client here.
    assert client.get(f"/s/{slug}").status_code == 410
    assert client.get(f"/api/s/{slug}").status_code == 410


def test_cannot_revoke_another_users_share(asgi_client, member_key, patch_chart_payload):
    headers, _uid = member_key
    slug = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers).json()["slug"]
    # Revoke scoped by user_id: a different user's revoke is a no-op (0 rows).
    n = sh.revoke_share_by_slug(slug, _uid + 9999)
    assert n == 0


# ── option C: client-captured image ──

def _png_data_url(w=400, h=200, color=(19, 23, 34)):
    """A real (tiny) PNG as a data URL, like the client capture."""
    import base64
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _jpeg_data_url(w=400, h=200, color=(19, 23, 34)):
    """A real (tiny) JPEG as a data URL — the second accepted capture format
    (decode_chart_capture was generalized from PNG-only)."""
    import base64
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _jpeg_bytes(w=400, h=200, color=(19, 23, 34)):
    """Raw (tiny) JPEG bytes — stands in for the screenshot service's
    response body."""
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()


def test_capture_mint_composites_and_serves_card(asgi_client, client, member_key):
    """A client capture is composited into the brand frame and served as the
    card — no build_chart_payload involved (no patch needed)."""
    headers, _uid = member_key
    resp = asgi_client.post(
        "/api/chart/share",
        json={"symbol": "BTC", "timeframe": "1D", "image": _png_data_url()},
        headers=headers)
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    share = sh.get_share_by_slug(slug)
    assert share["snapshot"]["source"] == "capture"
    assert share["image_path"]            # composited + pinned at mint

    # Public card is the composited JPEG (1200×630), served via
    # Flask at the stable /card.png URL with an image/jpeg Content-Type.
    card = client.get(f"/s/{slug}/card.png")
    assert card.status_code == 200
    assert card.headers["Content-Type"] == "image/jpeg"

    # The public page shows the image (capture branch), not the candle script.
    page = client.get(f"/s/{slug}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert 'class="capture"' in body
    assert "share-chart.js" not in body   # no client re-render on capture pages


def test_capture_accepts_jpeg_within_cap(asgi_client, client, member_key):
    """JPEG captures are accepted — same brand-frame
    composite path as PNG, just a different source format."""
    headers, _uid = member_key
    resp = asgi_client.post(
        "/api/chart/share",
        json={"symbol": "BTC", "timeframe": "1D", "image": _jpeg_data_url()},
        headers=headers)
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    share = sh.get_share_by_slug(slug)
    assert share["snapshot"]["source"] == "capture"
    assert share["image_path"]            # composited + pinned at mint

    # Composite output is always a JPEG regardless of the source
    # capture format (PNG or JPEG both composite + re-encode to the card JPEG).
    card = client.get(f"/s/{slug}/card.png")
    assert card.status_code == 200
    assert card.headers["Content-Type"] == "image/jpeg"


def test_capture_rejects_unsupported_format(asgi_client, member_key):
    """GIF (or anything outside the PNG/JPEG allow-list) is rejected — PNG
    and JPEG are the only accepted capture formats."""
    headers, _uid = member_key
    resp = asgi_client.post(
        "/api/chart/share",
        json={"symbol": "BTC", "timeframe": "1D",
              "image": "data:image/gif;base64,"
                       "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="},
        headers=headers)
    assert resp.status_code == 400


def test_capture_rejects_oversize(asgi_client, member_key):
    import base64
    headers, _uid = member_key
    # ~5 MB of base64 (PNG header but huge) → over the 4 MB decoded cap.
    big = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024)).decode()
    resp = asgi_client.post(
        "/api/chart/share",
        json={"symbol": "BTC", "timeframe": "1D", "image": big},
        headers=headers)
    assert resp.status_code == 400


# ── server-side (Playwright) render, no-capture path ──

def test_server_render_available_persists_source_and_omits_lw_chart(
        asgi_client, client, member_key, patch_chart_payload, monkeypatch):
    """When the screenshot service returns bytes, the mint composites them
    into the brand frame, flips snapshot['source'] to 'server_render' AND
    PERSISTS it (the easy-to-forget step). Page rendering (interactive
    frozen-chart viewer, default ON): since the frozen snapshot
    ALSO carries candles, the public page renders the interactive klinecharts
    viewer, not the legacy lightweight-charts client re-render — the
    'server_render' source persists for the OG card / <noscript> + data-hero
    fallback, it no longer steers which branch the page takes."""
    # Isolate the screenshot mint from the chart context — no real scanner/quant lookups here.
    monkeypatch.setattr(sh, "_build_chart_context", lambda sym: {})
    monkeypatch.setattr(sh, "_render_via_screenshot_service",
                        lambda *a, **k: _jpeg_bytes())

    headers, _uid = member_key
    resp = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers)
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    share = sh.get_share_by_slug(slug)
    assert share["snapshot"]["source"] == "server_render"
    assert share["image_path"]   # composited + pinned at mint, same as capture

    card = client.get(f"/s/{slug}/card.png")
    assert card.status_code == 200
    assert card.headers["Content-Type"] == "image/jpeg"   # composited card is JPEG

    page = client.get(f"/s/{slug}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    # Positively pin the viewer branch (candles present -> interactive
    # klinecharts viewer, not the static hero image nor the legacy client).
    assert 'id="share-viewer"' in body
    assert 'id="chart-share-data"' in body
    assert 'id="lw-chart"' not in body
    assert "lightweight-charts.min.js" not in body


def test_server_render_returns_none_keeps_old_behavior(
        asgi_client, client, member_key, patch_chart_payload, monkeypatch):
    """Screenshot service unavailable/disabled (returns None) — the MINT
    behaves EXACTLY as it did before the screenshot mint: candles-only snapshot, no
    source flip, no image_path. Page rendering has since changed with
    the interactive frozen-chart viewer (default ON): a candles-only
    snapshot now gets the klinecharts-based viewer, not the legacy
    lightweight-charts branch — verified separately with the kill switch
    OFF (tests/test_play_shares.py::test_share_page_kill_switch_restores_legacy)."""
    monkeypatch.setattr(sh, "_build_chart_context", lambda sym: {})
    monkeypatch.setattr(sh, "_render_via_screenshot_service",
                        lambda *a, **k: None)

    headers, _uid = member_key
    resp = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers)
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    share = sh.get_share_by_slug(slug)
    assert share["snapshot"].get("source") is None
    assert not share.get("image_path")

    page = client.get(f"/s/{slug}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "share-chart-viewer.js" in body
    assert 'id="chart-share-data"' in body


def test_server_render_raises_keeps_old_behavior(
        asgi_client, client, member_key, patch_chart_payload, monkeypatch):
    """Even if _render_via_screenshot_service raised (contract violation /
    unexpected bug), create_chart_share's outer guard degrades the mint the
    SAME way a clean None would — never a 500. Page rendering: see the
    sibling test above re: the interactive viewer superseding the
    legacy candle-only branch for candles-bearing snapshots."""
    monkeypatch.setattr(sh, "_build_chart_context", lambda sym: {})

    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(sh, "_render_via_screenshot_service", _boom)

    headers, _uid = member_key
    resp = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers)
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    share = sh.get_share_by_slug(slug)
    assert share["snapshot"].get("source") is None
    assert not share.get("image_path")

    page = client.get(f"/s/{slug}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "share-chart-viewer.js" in body
    assert 'id="chart-share-data"' in body


# ── per-asset context (replaces the brochure) ──

def test_context_frozen_at_mint_from_monkeypatched_sources(
        asgi_client, client, member_key, patch_chart_payload, monkeypatch):
    # Isolate the chart context from the screenshot mint — no real localhost:5000 network attempt.
    monkeypatch.setattr(sh, "_render_via_screenshot_service", lambda *a, **k: None)
    monkeypatch.setattr(sh, "_chart_context_confluence",
                        lambda sym: {"side": "resistance", "price": 65000.0,
                                    "label": "POC", "strength": 3.0, "tier": "high"})
    monkeypatch.setattr(sh, "_chart_context_scanner",
                        lambda sym: {"verdict": "STRONG", "score": 9, "direction": "long"})
    monkeypatch.setattr(sh, "_chart_context_regime_asset",
                        lambda sym: {"label": "risk-on squeeze", "confidence": 0.8, "scope": "asset"})
    monkeypatch.setattr(sh, "_chart_context_regime_global",
                        lambda: {"label": "expansion", "scope": "market"})

    headers, _uid = member_key
    resp = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers)
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    snap = sh.get_share_by_slug(slug)["snapshot"]
    assert snap["context"]["confluence"]["side"] == "resistance"
    assert snap["context"]["confluence"]["price"] == 65000.0
    assert snap["context"]["scanner"] == {"verdict": "STRONG", "score": 9, "direction": "long"}
    # BTC → the asset-specific derivatives regime, not the global HMM one.
    assert snap["context"]["regime"] == {"label": "risk-on squeeze", "confidence": 0.8, "scope": "asset"}

    page = client.get(f"/s/{slug}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    # Context is frozen into the snapshot (asserted above) but the page
    # renders the rotating "On Carrac you can" ribbon (no "Did you know?"
    # eyebrow) instead of context tiles.
    assert "On Carrac you can" in body
    assert "What is Carrac.cc?" not in body   # brochure stays gone on chart shares


def test_context_regime_branch_btc_eth_vs_other(
        asgi_client, member_key, patch_chart_payload, monkeypatch):
    """BTC/ETH get the asset-specific derivatives regime; any other symbol
    gets the global macro HMM regime — never mislabeled as asset-specific."""
    monkeypatch.setattr(sh, "_render_via_screenshot_service", lambda *a, **k: None)
    monkeypatch.setattr(sh, "_chart_context_confluence", lambda sym: None)
    monkeypatch.setattr(sh, "_chart_context_scanner", lambda sym: None)
    monkeypatch.setattr(sh, "_chart_context_regime_asset",
                        lambda sym: {"label": f"{sym} derivatives regime", "scope": "asset"})
    monkeypatch.setattr(sh, "_chart_context_regime_global",
                        lambda: {"label": "global macro regime", "scope": "market"})

    headers, _uid = member_key

    btc_slug = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                                headers=headers).json()["slug"]
    btc_ctx = sh.get_share_by_slug(btc_slug)["snapshot"]["context"]
    assert btc_ctx["regime"] == {"label": "BTC derivatives regime", "scope": "asset"}

    sol_slug = asgi_client.post("/api/chart/share", json={"symbol": "SOL", "timeframe": "1D"},
                                headers=headers).json()["slug"]
    sol_ctx = sh.get_share_by_slug(sol_slug)["snapshot"]["context"]
    assert sol_ctx["regime"] == {"label": "global macro regime", "scope": "market"}


def test_context_all_sources_fail_mint_still_succeeds(
        asgi_client, client, member_key, patch_chart_payload, monkeypatch):
    monkeypatch.setattr(sh, "_render_via_screenshot_service", lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError("source unavailable")
    monkeypatch.setattr(sh, "_chart_context_confluence", _boom)
    monkeypatch.setattr(sh, "_chart_context_scanner", _boom)
    monkeypatch.setattr(sh, "_chart_context_regime_asset", _boom)
    monkeypatch.setattr(sh, "_chart_context_regime_global", _boom)

    headers, _uid = member_key
    resp = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers)
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    snap = sh.get_share_by_slug(slug)["snapshot"]
    assert snap["context"] == {}

    page = client.get(f"/s/{slug}")
    assert page.status_code == 200   # renders fine with the context block hidden


def test_brochure_absent_on_chart_share_page(asgi_client, client, member_key,
                                             patch_chart_payload, monkeypatch):
    """Chart shares carry no generic feature brochure. The page shows the
    rotating "On Carrac you can" ribbon (no "Did you know?" eyebrow) instead of
    per-asset context tiles (context is still frozen into the snapshot
    server-side for future use)."""
    monkeypatch.setattr(sh, "_render_via_screenshot_service", lambda *a, **k: None)
    headers, _uid = member_key
    slug = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers).json()["slug"]
    body = client.get(f"/s/{slug}").get_data(as_text=True)
    assert "What is Carrac.cc?" not in body
    assert "On Carrac you can" in body


# ── "max-res hero": /s/<slug>/chart.jpg + template fallback ──

def test_chart_jpg_hero_200_for_server_render(
        asgi_client, client, member_key, patch_chart_payload, monkeypatch):
    """A server-render mint persists the RAW pre-composite bytes as the page
    hero (carrac.plays.shares._save_full_image); /s/<slug>/chart.jpg serves
    them directly — not the letterboxed 1200x630 card."""
    monkeypatch.setattr(sh, "_build_chart_context", lambda sym: {})
    monkeypatch.setattr(sh, "_render_via_screenshot_service",
                        lambda *a, **k: _jpeg_bytes())

    headers, _uid = member_key
    slug = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers).json()["slug"]

    resp = client.get(f"/s/{slug}/chart.jpg")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert resp.data[:3] == b"\xff\xd8\xff"
    assert "public" in resp.headers.get("Cache-Control", "")


def test_chart_jpg_hero_404_when_absent(
        asgi_client, client, member_key, patch_chart_payload, monkeypatch):
    """The candles-only fallback mint (no capture, no server-render bytes)
    never writes a hero file — the route 404s rather than serving nothing."""
    monkeypatch.setattr(sh, "_render_via_screenshot_service", lambda *a, **k: None)
    headers, _uid = member_key
    slug = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers).json()["slug"]
    assert client.get(f"/s/{slug}/chart.jpg").status_code == 404


def test_chart_jpg_hero_410_when_revoked(
        asgi_client, client, member_key, patch_chart_payload, monkeypatch):
    monkeypatch.setattr(sh, "_build_chart_context", lambda sym: {})
    monkeypatch.setattr(sh, "_render_via_screenshot_service",
                        lambda *a, **k: _jpeg_bytes())
    headers, _uid = member_key
    slug = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers).json()["slug"]
    assert client.get(f"/s/{slug}/chart.jpg").status_code == 200

    revoked = asgi_client.delete(f"/api/chart/share/{slug}", headers=headers)
    assert revoked.status_code == 200
    assert client.get(f"/s/{slug}/chart.jpg").status_code == 410


def test_hero_src_falls_back_to_card_url_without_full_image(
        asgi_client, client, member_key, patch_chart_payload, monkeypatch):
    """An old share (minted before this feature, or one whose hero file was
    otherwise lost) still has a composited card + persisted
    source='server_render' — the page must fall back to card_url rather than
    linking a hero file that no longer exists. Page rendering: since
    the frozen snapshot also carries candles, this renders the interactive
    viewer branch — the card_url fallback now surfaces via the viewer's
    data-hero attribute AND the <noscript> image, not a standalone hero
    <img> in the (unreached) static-hero branch."""
    import os

    from carrac.shares.card import card_dir

    monkeypatch.setattr(sh, "_build_chart_context", lambda sym: {})
    monkeypatch.setattr(sh, "_render_via_screenshot_service",
                        lambda *a, **k: _jpeg_bytes())
    headers, _uid = member_key
    slug = asgi_client.post("/api/chart/share", json={"symbol": "BTC", "timeframe": "1D"},
                            headers=headers).json()["slug"]

    # Simulate a pre-feature share: the composited card + 'server_render'
    # source remain, but the hero bytes are gone.
    for ext in ("jpg", "png"):
        p = os.path.join(card_dir(), f"{slug}_full.{ext}")
        if os.path.exists(p):
            os.remove(p)

    body = client.get(f"/s/{slug}").get_data(as_text=True)
    assert 'id="share-viewer"' in body   # viewer branch (candles present)
    assert f'data-hero="http://localhost/s/{slug}/card.png"' in body
    assert f'src="http://localhost/s/{slug}/card.png"' in body   # <noscript> fallback img
    assert "chart.jpg" not in body


def test_capture_path_writes_full_file_and_hero_route_serves_it(
        asgi_client, client, member_key):
    """The client-capture mint path (option C) also writes
    ``<slug>_full.<ext>`` — capture shares get the hero route too, not just
    server-render ones. A PNG capture yields a PNG hero (mimetype follows
    the actual bytes, not a fixed format)."""
    headers, _uid = member_key
    resp = asgi_client.post(
        "/api/chart/share",
        json={"symbol": "BTC", "timeframe": "1D", "image": _png_data_url()},
        headers=headers)
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    hero = client.get(f"/s/{slug}/chart.jpg")
    assert hero.status_code == 200
    assert hero.headers["Content-Type"] == "image/png"
    assert hero.data[:8] == b"\x89PNG\r\n\x1a\n"

    body = client.get(f"/s/{slug}").get_data(as_text=True)
    assert f'src="http://localhost/s/{slug}/chart.jpg"' in body


# ── theme + viewport (interactive frozen chart share) ──

def test_mint_persists_theme_and_viewport(asgi_client, member_key,
                                          patch_chart_payload):
    headers, uid = member_key
    resp = asgi_client.post(
        "/api/chart/share",
        json={"symbol": "BTC", "timeframe": "1D", "theme": "light_trading",
              "viewport": {"from_ts": 1700000000, "to_ts": 1700864000}},
        headers=headers)
    assert resp.status_code == 201
    slug = resp.json()["slug"]
    from carrac.plays.shares import get_share_by_slug
    share = get_share_by_slug(slug)
    snap = share["snapshot"]
    assert snap["theme"] == "light_trading"
    assert snap["viewport"] == {"from_ts": 1700000000, "to_ts": 1700864000}


def test_mint_rejects_malformed_viewport(asgi_client, member_key,
                                         patch_chart_payload):
    headers, _uid = member_key
    resp = asgi_client.post(
        "/api/chart/share",
        json={"symbol": "BTC", "timeframe": "1D",
              "viewport": {"from_ts": "junk"}},
        headers=headers)
    # FastAPI's default is 422; this app's validation_exception_handler
    # normalizes it to 400 (see exception_handlers.py + the sibling
    # test_extra_field_rejected above) to match the Flask-side error shape.
    assert resp.status_code == 400


def test_mint_rejects_unknown_fields(asgi_client, member_key,
                                     patch_chart_payload):
    headers, _uid = member_key
    resp = asgi_client.post(
        "/api/chart/share",
        json={"symbol": "BTC", "timeframe": "1D", "surprise": 1},
        headers=headers)
    assert resp.status_code == 400   # extra="forbid" → 422 normalized to 400
