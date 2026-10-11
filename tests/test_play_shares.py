"""Shareable Plays — model-layer + public-route + router-shape tests.

Strategy (mirrors tests/fastapi/test_plays_subroutes.py): the FastAPI write
endpoints delegate to carrac/plays/shares.py, so DB behaviour is covered at the
model layer; the public /s/<slug> read path is covered through the Flask test
client (no auth); the FastAPI endpoints get a router-shape + Pydantic check.
"""

from __future__ import annotations

import os

import pytest

from carrac.plays.models import play_create, update_play
from carrac.plays import shares as sh


_UID = 7


def _make_play(uid=_UID, title="Bearish H&S Breakdown"):
    return play_create(
        uid,
        title=title,
        thesis="BTC rejects SMA200 at 98k, targets 88k.",
        primary_asset="BTC",
        direction="short",
        type="directional",
        timeframe="swing",
        action_plan="Scale in on the retest; trail under structure.",
        conditions={
            "logic": "all",
            "rules": [
                {"type": "price_level", "met": True, "label": "Price < 96000"},
                {"type": "manual", "met": False, "label": "Daily close < 94500"},
            ],
        },
    )


# ── model layer: freeze / reuse / revoke / update / quota ──

def test_create_share_freezes_snapshot(app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])

    assert share is not None
    assert len(share["slug"]) == 8
    assert share["slug"].isalnum()

    snap = share["snapshot"]
    assert snap["title"] == "Bearish H&S Breakdown"
    assert snap["primary_asset"] == "BTC"
    assert snap["status"] == "watching"
    assert snap["conditions_met"] == 1
    assert snap["conditions_total"] == 2
    # include_thesis defaults True → thesis present
    assert snap["thesis"].startswith("BTC rejects")
    # conditions opted-in with met flags
    labels = {c["label"]: c["met"] for c in snap["conditions"]}
    assert labels["Price < 96000"] is True
    assert labels["Daily close < 94500"] is False
    # never leak ownership
    assert "user_id" not in snap


def test_share_is_frozen_after_play_edit(app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])

    update_play(play["id"], _UID, title="EDITED TITLE")

    fetched = sh.get_share_by_slug(share["slug"])
    assert fetched["snapshot"]["title"] == "Bearish H&S Breakdown"  # frozen, not EDITED


def test_create_share_reuses_existing_active(app):
    play = _make_play()
    a = sh.create_share(_UID, play["id"])
    b = sh.create_share(_UID, play["id"])
    assert a["slug"] == b["slug"]
    assert sh.count_active_shares(_UID) == 1


def test_create_share_unknown_play_returns_none(app):
    assert sh.create_share(_UID, 999999) is None


def test_revoke_share(app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])
    assert sh.count_active_shares(_UID) == 1

    revoked = sh.revoke_share(play["id"], _UID)
    assert revoked == 1
    assert sh.count_active_shares(_UID) == 0

    # Slug still resolves but is marked revoked (caller turns this into 410).
    fetched = sh.get_share_by_slug(share["slug"])
    assert fetched["revoked_at"] is not None


def test_update_share_toggle_refreezes(app):
    play = _make_play()
    sh.create_share(_UID, play["id"])

    updated = sh.update_share(play["id"], _UID, include_thesis=False)
    assert updated["include_thesis"] is False
    assert "thesis" not in updated["snapshot"]
    # conditions still present (untouched toggle)
    assert "conditions" in updated["snapshot"]


def test_share_quota_helper():
    assert sh.share_quota("free") == 3
    assert sh.share_quota("member") == 25
    assert sh.share_quota("premium") == 999
    assert sh.share_quota(None) == 3


# ── public Flask routes (no auth) ──

def test_public_share_page_no_auth(client, app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])

    resp = client.get(f"/s/{share['slug']}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "BTC" in body
    assert "WATCHING" in body
    assert "Not financial advice" in body
    assert "What is Carrac?" in body
    assert "70+ indicators" in body  # copy fixed from 49


def test_public_share_json(client, app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])

    resp = client.get(f"/api/s/{share['slug']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["slug"] == share["slug"]
    assert data["snapshot"]["primary_asset"] == "BTC"


def test_revoked_share_returns_410(client, app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])
    sh.revoke_share(play["id"], _UID)

    resp = client.get(f"/s/{share['slug']}")
    assert resp.status_code == 410


def test_unknown_slug_404(client, app):
    resp = client.get("/s/zzzzzzzz")  # regex-valid but nonexistent
    assert resp.status_code == 404


def test_malformed_slug_404(client, app):
    resp = client.get("/s/!!bad!!")  # fails the slug regex before any DB hit
    assert resp.status_code == 404


# ── FastAPI router shape + Pydantic ──

def test_share_router_has_all_three_endpoints():
    from carrac.fastapi_app.routers.plays import router

    paths = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
    assert ("/api/plays/{play_id}/share", ("POST",)) in paths
    assert ("/api/plays/{play_id}/share", ("PUT",)) in paths
    assert ("/api/plays/{play_id}/share", ("DELETE",)) in paths


def test_share_request_rejects_extra_fields():
    from pydantic import ValidationError

    from carrac.fastapi_app.models.plays import PlayShareRequest

    PlayShareRequest(include_thesis=False)  # ok, partial
    PlayShareRequest()  # ok, empty

    with pytest.raises(ValidationError):
        PlayShareRequest(surprise="x")


# ── Toggles-on-reuse + outcome + levels + kind + enrichment ──

def test_create_share_with_toggles_applies_on_reuse(app):
    """POST with explicit toggles must apply them, not silently return the earlier share."""
    play = _make_play()
    a = sh.create_share(_UID, play["id"])
    assert "thesis" in a["snapshot"]

    b = sh.create_share(_UID, play["id"], include_thesis=False)
    assert b["slug"] == a["slug"]              # same active share…
    assert b["include_thesis"] is False        # …with the new toggle applied
    assert "thesis" not in b["snapshot"]
    assert sh.count_active_shares(_UID) == 1


def test_share_kind_defaults_to_play(app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])
    assert share["kind"] == "play"


def test_outcome_requires_toggle_and_terminal_status(app):
    from carrac.plays.models import transition_status

    play = _make_play()
    # Toggled on while non-terminal: no outcome block in the snapshot.
    share = sh.create_share(_UID, play["id"], include_outcome=True)
    assert share["include_outcome"] is True
    assert "outcome" not in share["snapshot"]

    # Terminal + refresh (all-None update re-freezes): outcome appears.
    transition_status(play["id"], _UID, "completed", resolution="Target hit for +2R")
    refreshed = sh.update_share(play["id"], _UID)
    snap = refreshed["snapshot"]
    assert snap["outcome"]["status"] == "completed"
    assert snap["outcome"]["resolution"] == "Target hit for +2R"

    # Toggle off: outcome leaves the snapshot.
    hidden = sh.update_share(play["id"], _UID, include_outcome=False)
    assert "outcome" not in hidden["snapshot"]


def test_outcome_default_off(app):
    from carrac.plays.models import transition_status

    play = _make_play()
    transition_status(play["id"], _UID, "completed", resolution="done")
    share = sh.create_share(_UID, play["id"])  # defaults
    assert share["include_outcome"] is False
    assert "outcome" not in share["snapshot"]


def test_levels_frozen_under_plan_toggle_sanitized(app):
    play = play_create(
        _UID, title="Levels play", primary_asset="ETH",
        metadata={"entry_low": "2400", "entry_high": 2520.5, "stop_loss": 2300,
                  "targets": [2800, "3000", "junk", None], "conviction": "high"},
    )
    share = sh.create_share(_UID, play["id"])
    levels = share["snapshot"]["levels"]
    assert levels["entry_low"] == 2400.0
    assert levels["entry_high"] == 2520.5
    assert levels["stop_loss"] == 2300.0
    assert levels["targets"] == [2800.0, 3000.0]   # junk + None dropped
    assert "conviction" not in levels              # only level fields leak

    no_plan = sh.update_share(play["id"], _UID, include_plan=False)
    assert "levels" not in no_plan["snapshot"]


def test_refresh_snapshot_via_all_none_update(app):
    play = _make_play()
    sh.create_share(_UID, play["id"])
    update_play(play["id"], _UID, title="NEW TITLE")

    refreshed = sh.update_share(play["id"], _UID)   # all-None = refresh
    assert refreshed["snapshot"]["title"] == "NEW TITLE"


def test_list_plays_enriched_with_share(app):
    from carrac.plays.models import list_plays

    shared_play = _make_play(title="Shared one")
    unshared_play = _make_play(title="Private one")
    share = sh.create_share(_UID, shared_play["id"])

    plays = {p["id"]: p for p in list_plays(_UID)}
    assert plays[shared_play["id"]]["share"]["slug"] == share["slug"]
    assert plays[shared_play["id"]]["share"]["view_count"] == 0
    assert plays[unshared_play["id"]]["share"] is None


def test_get_shares_for_plays_empty(app):
    assert sh.get_shares_for_plays(_UID, []) == {}


# ── card.png route ──

def test_card_png_renders_and_caches(client, app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])

    resp = client.get(f"/s/{share['slug']}/card.png")
    assert resp.status_code == 200
    assert resp.content_type == "image/png"
    assert "public" in resp.headers.get("Cache-Control", "")
    assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

    # Path persisted -> second request serves the cached file.
    again = sh.get_share_by_slug(share["slug"])
    assert again["image_path"]
    resp2 = client.get(f"/s/{share['slug']}/card.png")
    assert resp2.status_code == 200


def test_card_png_revoked_410_and_malformed_404(client, app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])
    sh.revoke_share(play["id"], _UID)
    assert client.get(f"/s/{share['slug']}/card.png").status_code == 410
    assert client.get("/s/!!bad!!/card.png").status_code == 404


def test_card_png_render_failure_falls_back(client, app, monkeypatch):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])

    import carrac.shares.card as card_mod
    def _boom(_share):
        raise RuntimeError("pillow exploded")
    monkeypatch.setattr(card_mod, "render_card_to_file", _boom)

    resp = client.get(f"/s/{share['slug']}/card.png")
    assert resp.status_code == 302
    assert "/static/og-dashboard.png" in resp.headers["Location"]


def test_toggle_change_invalidates_card_cache(client, app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])
    client.get(f"/s/{share['slug']}/card.png")
    assert sh.get_share_by_slug(share["slug"])["image_path"]

    sh.update_share(play["id"], _UID, include_thesis=False)
    assert sh.get_share_by_slug(share["slug"])["image_path"] is None


# ── OG meta + UTM + page sections ──

def test_share_page_og_urls_absolute(client, app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])

    body = client.get(f"/s/{share['slug']}").get_data(as_text=True)
    assert f'property="og:image" content="http://localhost/s/{share["slug"]}/card.png"' in body
    assert f'property="og:url" content="http://localhost/s/{share["slug"]}"' in body
    assert 'property="og:image:width" content="1200"' in body
    assert 'name="twitter:image" content="http://localhost/s/' in body


def _mint_candle_chart_share(monkeypatch, n=30):
    import carrac.charts.service as svc
    monkeypatch.setenv("CHART_SHARE_SERVER_RENDER", "0")
    monkeypatch.setattr(
        svc, "build_chart_payload",
        lambda *a, **k: {
            "symbol": "BTC", "timeframe": "1D",
            "ohlcv": [{"time": 1700000000 + i * 86400, "open": 100 + i,
                       "high": 103 + i, "low": 97 + i, "close": 101 + i}
                      for i in range(n)]})
    return sh.create_chart_share(_UID, "BTC", "1D", theme="trading_dark")


def test_share_page_interactive_viewer(client, app, monkeypatch):
    share = _mint_candle_chart_share(monkeypatch)
    resp = client.get(f"/s/{share['slug']}")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # All five viewer scripts load (klinecharts + the pack + registry that
    # drive it, plus the shared frozen-render + viewer-bootstrap modules).
    assert "klinecharts.min.js" in html
    assert "carrac-indicator-pack.js" in html
    assert "indicator-registry.js" in html
    assert "kline-share-render.js" in html
    assert "share-chart-viewer.js" in html
    assert 'id="chart-share-data"' in html
    # legacy branch replaced (the #lw-chart CSS rule itself is static
    # boilerplate shared with the kill-switch-off path, so check the DOM
    # marker rather than the bare substring)
    assert 'id="lw-chart"' not in html


def test_share_page_kill_switch_restores_legacy(client, app, monkeypatch):
    share = _mint_candle_chart_share(monkeypatch)
    monkeypatch.setenv("SHARE_INTERACTIVE_ENABLED", "0")
    resp = client.get(f"/s/{share['slug']}")
    html = resp.get_data(as_text=True)
    assert "share-chart-viewer.js" not in html
    assert 'id="lw-chart"' in html                   # byte-family of old page


def test_share_page_kill_switch_off_server_render_with_candles_shows_hero(
        client, app, monkeypatch):
    """Kill switch OFF + a snapshot carrying BOTH candles and a persisted
    source='server_render' (the shape create_chart_share's screenshot path
    produces once a screenshot succeeds) must render the static hero
    branch, not the viewer — SHARE_INTERACTIVE_ENABLED=0 has to win
    regardless of which source populated the candle-bearing snapshot."""
    share = _mint_candle_chart_share(monkeypatch)
    snapshot = dict(share["snapshot"])
    snapshot["source"] = "server_render"
    sh._persist_snapshot(share["id"], snapshot)
    monkeypatch.setenv("SHARE_INTERACTIVE_ENABLED", "0")

    resp = client.get(f"/s/{share['slug']}")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'id="share-viewer"' not in html
    assert 'class="chart hero zoomable"' in html


def test_share_page_cta_utm_tagged(client, app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])
    body = client.get(f"/s/{share['slug']}").get_data(as_text=True)
    assert f"utm_source=share&amp;utm_medium=play_share&amp;utm_campaign={share['slug']}" in body


def test_share_page_outcome_section(client, app):
    from carrac.plays.models import transition_status

    play = _make_play()
    transition_status(play["id"], _UID, "completed", resolution="Ran to target, +2R")
    share = sh.create_share(_UID, play["id"], include_outcome=True)

    body = client.get(f"/s/{share['slug']}").get_data(as_text=True)
    assert "Outcome" in body
    assert "Ran to target, +2R" in body
    assert "COMPLETED" in body


def test_share_page_levels_section(client, app):
    play = play_create(
        _UID, title="Levels page play", primary_asset="ETH",
        metadata={"entry_low": 2400, "stop_loss": 2300, "targets": [2800]},
    )
    share = sh.create_share(_UID, play["id"])
    body = client.get(f"/s/{share['slug']}").get_data(as_text=True)
    assert "Levels" in body
    assert "2400" in body and "2300" in body and "2800" in body


def test_share_error_pages_branded(client, app):
    play = _make_play()
    share = sh.create_share(_UID, play["id"])
    sh.revoke_share(play["id"], _UID)

    gone = client.get(f"/s/{share['slug']}")
    assert gone.status_code == 410
    assert "Share revoked" in gone.get_data(as_text=True)

    missing = client.get("/s/zzzzzz99")
    assert missing.status_code == 404
    assert "Share not found" in missing.get_data(as_text=True)


# ── MCP: share_manage registration + scope ──
#
# share_play/unshare_play (along with share/unshare for chart and trade) are
# plain helper functions now, absorbed into share_manage
# (entity_type=play, action=share|unshare) — no longer standalone tools.

def test_mcp_share_tools_registered():
    import carrac.mcp.tools.sharing  # noqa: F401 — registers on import
    from carrac.mcp.registry import TOOL_REGISTRY

    assert "share_manage" in TOOL_REGISTRY
    assert "share_play" not in TOOL_REGISTRY
    assert "unshare_play" not in TOOL_REGISTRY


def test_mcp_share_tools_are_write_scoped():
    """Read-only API keys must NOT be able to publish/revoke public artifacts."""
    from carrac.mcp.protocol import WRITE_TOOLS

    assert "share_manage" in WRITE_TOOLS


def test_mcp_legacy_share_names_dispatch_to_share_manage():
    from carrac.mcp.registry import resolve_tool_name
    assert resolve_tool_name("share_play") == "share_manage"
    assert resolve_tool_name("unshare_play") == "share_manage"
    assert resolve_tool_name("share_chart") == "share_manage"
    assert resolve_tool_name("unshare_chart") == "share_manage"
    assert resolve_tool_name("share_trade") == "share_manage"
    assert resolve_tool_name("unshare_trade") == "share_manage"


def test_mcp_share_manage_roundtrip_play(app, bind_mcp_user):
    """share_manage(entity_type=play) dispatches to the same share_play/
    unshare_play helpers test_mcp_share_play_roundtrip exercises directly."""
    from carrac.mcp.registry import TOOL_REGISTRY

    bind_mcp_user(_UID)
    play = _make_play(title="MCP share via share_manage")
    fn = TOOL_REGISTRY["share_manage"]["handler"]
    out = fn({"entity_type": "play", "action": "share", "play_id": play["id"]})
    assert out["url"].endswith(f"/s/{out['slug']}")

    revoked = fn({"entity_type": "play", "action": "unshare", "play_id": play["id"]})
    assert revoked["revoked"] == 1


def test_mcp_share_play_roundtrip(app, bind_mcp_user):
    import carrac.mcp.tools.plays as mcp_plays

    bind_mcp_user(_UID)
    play = _make_play(title="MCP share")
    out = mcp_plays.share_play({"play_id": play["id"], "include_outcome": True})
    assert out["url"].endswith(f"/s/{out['slug']}")
    assert out["include_outcome"] is True

    revoked = mcp_plays.unshare_play({"play_id": play["id"]})
    assert revoked["revoked"] == 1


def test_json_dumps_handles_datetime():
    """Postgres returns datetime objects for play timestamps (SQLite gives
    TEXT) — the snapshot serializer must not choke (a 500 on mint)."""
    from datetime import datetime, timezone

    out = sh._json_dumps({"created_at": datetime(2026, 6, 7, tzinfo=timezone.utc),
                          "title": "x"})
    assert "2026-06-07" in out


# ── trade shares (kind='trade'): pure-function freezer + card render ──

class TestTradeFreezer:
    """Server-side freezer helpers — no DB, dict in / dict out. The hard
    privacy rule (size/fees never leak) is enforced by whitelist
    construction; these lock it in."""

    def _trade(self, **kw):
        base = dict(
            asset="BTC", direction="long", timeframe="1D",
            entry_price=65000, exit_price=70000, rr_actual=2.5,
            exit_reason="tp_hit", opened_at="2026-06-01 10:00:00",
            closed_at="2026-06-04 18:00:00", notes="held the retest",
            size=2.0, fees=15.5, pnl_currency=4321.0, account_tag="main",
        )
        base.update(kw)
        return base

    def test_pnl_pct_long(self):
        assert sh._pnl_pct(self._trade()) == pytest.approx(7.69, abs=0.01)

    def test_pnl_pct_short_is_inverted(self):
        t = self._trade(direction="short", entry_price=70000, exit_price=65000)
        # short profits when price drops -> positive
        assert sh._pnl_pct(t) == pytest.approx(7.14, abs=0.01)

    def test_pnl_pct_none_without_prices(self):
        assert sh._pnl_pct(self._trade(exit_price=None)) is None

    def test_hold_human(self):
        assert sh._hold_human("2026-06-01 10:00:00", "2026-06-04 18:00:00") == "3d 8h"
        assert sh._hold_human("2026-06-01 10:00:00", "2026-06-01 15:12:00") == "5h 12m"
        assert sh._hold_human("2026-06-01 10:00:00", "2026-06-01 10:42:00") == "42m"
        assert sh._hold_human(None, "2026-06-01 10:00:00") is None

    def test_snapshot_never_carries_account_size(self):
        snap = sh._freeze_trade_snapshot(self._trade(), include_levels=True,
                                         include_note=True)
        for forbidden in ("size", "fees", "pnl_currency", "account_tag",
                          "account"):
            assert forbidden not in snap
        blob = str(snap)
        assert "15.5" not in blob and "4321" not in blob and "main" not in blob

    def test_levels_toggle_off_hides_prices(self):
        snap = sh._freeze_trade_snapshot(self._trade(), include_levels=False,
                                         include_note=False)
        assert "levels" not in snap
        assert "note" not in snap

    def test_note_truncated_and_opt_in(self):
        snap = sh._freeze_trade_snapshot(self._trade(notes="x" * 999),
                                         include_levels=True, include_note=True)
        assert len(snap["note"]) == 500

    def test_snapshot_datetime_serializes(self):
        """closed_at is a datetime on Postgres — _json_dumps must survive it
        (a 500 on mint)."""
        from datetime import datetime, timezone
        t = self._trade(closed_at=datetime(2026, 6, 4, 18, tzinfo=timezone.utc),
                        opened_at=datetime(2026, 6, 1, 10, tzinfo=timezone.utc))
        snap = sh._freeze_trade_snapshot(t, include_levels=True,
                                         include_note=False)
        out = sh._json_dumps(snap)
        assert "2026-06-04" in out
        assert snap["hold"] == "3d 8h"


class TestTradeCardRender:
    def _share(self, **snap_kw):
        snap = dict(asset="BTC", direction="long", timeframe="1D",
                    pnl_pct=7.69, rr_actual=2.5, exit_reason="tp_hit",
                    closed_at="2026-06-04 18:00:00", hold="3d 8h",
                    levels={"entry_price": 65000, "exit_price": 70000})
        snap.update(snap_kw)
        return {"kind": "trade", "slug": "trd12345", "snapshot": snap}

    def test_renders_win_card(self):
        from carrac.shares.card import render_card
        img = render_card(self._share())
        assert img.size == (1200, 630)

    def test_renders_loss_card(self):
        from carrac.shares.card import render_card
        img = render_card(self._share(pnl_pct=-3.2, rr_actual=-1.0,
                                      exit_reason="sl_hit"))
        assert img.size == (1200, 630)

    def test_renders_with_missing_optionals(self):
        from carrac.shares.card import render_card
        img = render_card({"kind": "trade", "slug": "trd9", "snapshot": {
            "asset": "ETH", "direction": "short", "pnl_pct": None}})
        assert img.size == (1200, 630)

    def test_renders_breakeven_card(self):
        """pnl_pct == 0.0 is FLAT, not a win or a loss — the
        hero must show a plain '0.0%', not '+0.0%'."""
        from carrac.shares.card import render_card
        img = render_card(self._share(pnl_pct=0.0, rr_actual=0.0))
        assert img.size == (1200, 630)


class TestTradeCardRenderSmoke:
    """Flagship card: file-based render-smoke checks for the
    four snapshot shapes a real trade share can produce — win, loss,
    breakeven and the fully-degraded minimal-fields case. Asserts the file
    lands on disk at the right dimensions, not pixel content (that's what
    the sample PNGs in the scratchpad are for)."""

    def _render(self, tmp_path, monkeypatch, slug, snapshot):
        from carrac.shares.card import render_card_to_file
        from PIL import Image
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        share = {"kind": "trade", "slug": slug, "snapshot": snapshot}
        path = render_card_to_file(share)
        assert os.path.exists(path)
        assert Image.open(path).size == (1200, 630)

    def test_win(self, tmp_path, monkeypatch):
        self._render(tmp_path, monkeypatch, "trdwin001", {
            "asset": "BTC", "direction": "long", "timeframe": "1D",
            "pnl_pct": 7.69, "rr_actual": 2.5, "exit_reason": "tp_hit",
            "closed_at": "2026-06-04 18:00:00", "hold": "3d 8h",
            "levels": {"entry_price": 65000, "exit_price": 70000}})

    def test_loss(self, tmp_path, monkeypatch):
        self._render(tmp_path, monkeypatch, "trdloss001", {
            "asset": "ETH", "direction": "short", "timeframe": "4h",
            "pnl_pct": -3.2, "rr_actual": -1.0, "exit_reason": "sl_hit",
            "closed_at": "2026-06-04 18:00:00", "hold": "5h 12m"})

    def test_breakeven(self, tmp_path, monkeypatch):
        self._render(tmp_path, monkeypatch, "trdflat001", {
            "asset": "SOL", "direction": "long", "timeframe": "1D",
            "pnl_pct": 0.0, "rr_actual": 0.0, "exit_reason": "manual_close",
            "hold": "1d 2h"})

    def test_minimal_fields(self, tmp_path, monkeypatch):
        self._render(tmp_path, monkeypatch, "trdmin001", {
            "asset": "ETH", "direction": "short", "pnl_pct": None})


# ── chart shares (kind='chart'): freezer + candle card render ──

class TestChartFreezer:
    def _payload(self, n=130):
        return {
            "symbol": "BTC", "timeframe": "1D",
            "ohlcv": [
                {"time": 1700000000 + i * 86400, "open": 100 + i,
                 "high": 100 + i + 2, "low": 100 + i - 2, "close": 100 + i + 1}
                for i in range(n)
            ],
        }

    def test_freeze_caps_candles_and_computes_change(self, monkeypatch):
        import carrac.charts.service as svc
        monkeypatch.setattr(svc, "build_chart_payload",
                            lambda *a, **k: self._payload(530))
        snap = sh._freeze_chart_snapshot("btc", "1D")
        assert snap["symbol"] == "BTC"
        assert snap["candle_count"] == 500          # capped from 530
        assert len(snap["candles"]) == 500
        assert snap["last_price"] == self._payload(530)["ohlcv"][-1]["close"]
        assert snap["change_pct"] is not None       # rises over the window
        assert snap["change_pct"] > 0

    def test_freeze_none_without_data(self, monkeypatch):
        import carrac.charts.service as svc
        monkeypatch.setattr(svc, "build_chart_payload",
                            lambda *a, **k: {"ohlcv": [], "symbol": "ZZZ", "timeframe": "1D"})
        assert sh._freeze_chart_snapshot("ZZZ", "1D") is None

    def test_indicator_descriptors_sanitized(self):
        out = sh._sanitize_chart_indicators(
            ["ema_ribbon", {"key": "bb", "params": {"len": 20, "junk": [1, 2]}}, {"x": 1}])
        # NOTE: "junk" is itself a list of primitives (<=12 items), so under
        # the calcParams-list allowance it now passes the whitelist too —
        # updated from the brief's claim that this assertion is unaffected.
        assert out == [{"key": "ema_ribbon"},
                       {"key": "bb", "params": {"len": 20, "junk": [1, 2]}}]

    def test_freeze_merges_volume_by_time(self, monkeypatch):
        """build_chart_payload's volume list drops NaN-volume bars entirely
        (carrac/charts/service.py::build_chart_payload) — after any gap, matching by
        raw list index pairs each bar with the WRONG volume entry. The merge
        must match by the bar's ``time`` key instead, so a dropped bar (3)
        gets no volume and every later bar (4..9) still gets its own,
        correctly-matched value rather than a value shifted by one."""
        import carrac.charts.service as svc
        p = self._payload(10)
        # Simulate the NaN drop: bar index 3 has NO volume entry at all.
        p["volume"] = [{"time": c["time"], "value": 100.0 + i}
                       for i, c in enumerate(p["ohlcv"]) if i != 3]
        monkeypatch.setattr(svc, "build_chart_payload", lambda *a, **k: p)
        snap = sh._freeze_chart_snapshot("btc", "1D")
        assert "volume" not in snap["candles"][3]
        for i in range(4, 10):
            assert snap["candles"][i]["volume"] == 100.0 + i
        assert snap["candles"][0]["volume"] == 100.0

    def test_freeze_viewport_windowing(self, monkeypatch):
        """Freeze window covers the sharer's viewport, not just the tail."""
        import carrac.charts.service as svc
        p = self._payload(600)  # times 1700000000 .. +599 days
        monkeypatch.setattr(svc, "build_chart_payload", lambda *a, **k: p)
        # Viewport ends at bar index 99 — far in the past.
        to_ts = p["ohlcv"][99]["time"]
        snap = sh._freeze_chart_snapshot(
            "btc", "1D", viewport={"from_ts": p["ohlcv"][50]["time"],
                                   "to_ts": to_ts})
        times = [c["time"] for c in snap["candles"]]
        assert to_ts in times                       # viewport end included
        # a few trailing bars for context, but NOT the full recent tail
        assert max(times) <= p["ohlcv"][99 + 20]["time"]
        assert snap["viewport"] == {"from_ts": p["ohlcv"][50]["time"],
                                    "to_ts": to_ts}

    def test_freeze_carries_theme_and_pack_data(self, monkeypatch):
        import carrac.charts.service as svc
        p = self._payload(10)
        p["cpr"] = {"pivot": 105.0}
        p["indicators"] = {"daily_ema": [1, 2, 3]}
        p["indicators_ext"] = {"huge": "not-whitelisted"}
        monkeypatch.setattr(svc, "build_chart_payload", lambda *a, **k: p)
        snap = sh._freeze_chart_snapshot("btc", "1D", theme="light_trading")
        assert snap["theme"] == "light_trading"
        assert snap["pack_data"]["cpr"] == {"pivot": 105.0}
        assert snap["pack_data"]["indicators"] == {"daily_ema": [1, 2, 3]}
        assert "indicators_ext" not in snap["pack_data"]  # whitelist only

    def test_freeze_drops_oversize_pack_data(self, monkeypatch):
        import carrac.charts.service as svc
        p = self._payload(10)
        p["indicators"] = {"blob": "x" * 500_000}
        monkeypatch.setattr(svc, "build_chart_payload", lambda *a, **k: p)
        snap = sh._freeze_chart_snapshot("btc", "1D")
        assert snap["pack_data"] == {}              # dropped, share survives

    def test_sanitize_allows_calcparams_lists(self):
        out = sh._sanitize_chart_indicators(
            [{"key": "RSI", "params": {"calcParams": [14], "junk": {"a": 1}}}])
        assert out == [{"key": "RSI", "params": {"calcParams": [14]}}]

    def test_capture_path_also_freezes_candles(self, monkeypatch):
        import carrac.charts.service as svc
        monkeypatch.setattr(svc, "build_chart_payload",
                            lambda *a, **k: self._payload(30))
        # 1x1 valid PNG bytes
        import base64
        png = base64.b64decode(
            b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
            b"2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        share = sh.create_chart_share(_UID, "BTC", "1D", capture_png=png,
                                      theme="classic_dark")
        snap = share["snapshot"]
        assert snap["source"] == "capture"
        assert snap["candle_count"] == 30           # structured payload now frozen
        assert snap["theme"] == "classic_dark"


class TestChartCardRender:
    def _share(self, **snap_kw):
        snap = {
            "symbol": "BTC", "timeframe": "1D", "candle_count": 60,
            "last_price": 64000, "change_pct": 5.2,
            "candles": [
                {"time": 1700000000 + i * 86400, "open": 100 + i,
                 "high": 100 + i + 3, "low": 100 + i - 3, "close": 100 + i + 1}
                for i in range(60)
            ],
        }
        snap.update(snap_kw)
        return {"kind": "chart", "slug": "chrt0001", "snapshot": snap}

    def test_renders_chart_card(self):
        from carrac.shares.card import render_card
        img = render_card(self._share())
        assert img.size == (1200, 630)

    def test_renders_with_no_candles(self):
        from carrac.shares.card import render_card
        img = render_card({"kind": "chart", "slug": "chrt2",
                           "snapshot": {"symbol": "ETH", "timeframe": "4h", "candles": []}})
        assert img.size == (1200, 630)

    def test_card_slices_long_snapshots(self):
        from carrac.shares.card import render_card
        img = render_card(self._share(
            candle_count=500,
            candles=[{"time": 1700000000 + i * 86400, "open": 100 + i,
                      "high": 100 + i + 3, "low": 100 + i - 3,
                      "close": 100 + i + 1} for i in range(500)]))
        assert img.size == (1200, 630)              # renders, no crash


class TestChartCapture:
    """Option C — client-captured chart image gate + brand-frame composite."""

    def _png_bytes(self, w=300, h=150):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (w, h), (19, 23, 34)).save(buf, format="PNG")
        return buf.getvalue()

    def _data_url(self, png=None):
        import base64
        return "data:image/png;base64," + base64.b64encode(png or self._png_bytes()).decode()

    def test_decode_valid_png(self):
        raw = sh.decode_chart_capture(self._data_url())
        assert raw.startswith(b"\x89PNG\r\n\x1a\n")

    def test_decode_valid_jpeg(self):
        """JPEG is accepted alongside PNG."""
        import base64
        import io
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (300, 150), (19, 23, 34)).save(buf, format="JPEG")
        data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        raw = sh.decode_chart_capture(data_url)
        assert raw.startswith(b"\xff\xd8\xff")

    def test_decode_rejects_unsupported_mime(self):
        """GIF (or anything outside the PNG/JPEG allow-list) is rejected."""
        with pytest.raises(sh.ChartImageError):
            sh.decode_chart_capture(
                "data:image/gif;base64,"
                "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")

    def test_decode_rejects_non_dataurl(self):
        with pytest.raises(sh.ChartImageError):
            sh.decode_chart_capture("just some text")

    def test_decode_rejects_bad_base64(self):
        with pytest.raises(sh.ChartImageError):
            sh.decode_chart_capture("data:image/png;base64,@@@not-base64@@@")

    def test_decode_rejects_png_mime_but_not_png_bytes(self):
        import base64
        fake = "data:image/png;base64," + base64.b64encode(b"hello not a png").decode()
        with pytest.raises(sh.ChartImageError):
            sh.decode_chart_capture(fake)

    def test_decode_rejects_oversize(self):
        import base64
        big = "data:image/png;base64," + base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024)).decode()
        with pytest.raises(sh.ChartImageError):
            sh.decode_chart_capture(big)

    def test_composite_card_is_1200x630(self, tmp_path, monkeypatch):
        from carrac.shares.card import render_capture_card_to_file
        from PIL import Image
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        share = {"slug": "capx0001",
                 "snapshot": {"symbol": "BTC", "timeframe": "1D", "source": "capture"}}
        path = render_capture_card_to_file(share, self._png_bytes(1500, 600))
        assert Image.open(path).size == (1200, 630)
