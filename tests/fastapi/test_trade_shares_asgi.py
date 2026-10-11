"""Trade-result shares (kind='trade') — END-TO-END through the strangler.

Same strangler-parity discipline as the play
shares: the FastAPI-live trade-share endpoints are exercised
through ``asgi_client`` with a real per-user API key (``member_key`` fixture
in conftest), not a Flask twin.

Covers: 401 anon, open-trade 400, mint 201 + absolute URLs + privacy
(size/fees never leak), get-or-create reuse, PATCH toggle/refresh semantics,
402 quota on the SHARED per-user pool (a play share + a trade share count
together), DELETE revoke → public 410 across runtimes, cross-user 404.
"""

from __future__ import annotations

import pytest

from carrac.plays import shares as sh
from carrac.plays.models import play_create


@pytest.fixture
def trade_db(app):  # noqa: ARG001 — app sets DATA_DIR before we bind the singleton
    """ContentDatabase singleton bound to THIS test's DATA_DIR.

    ``get_content_db()`` is a process-global singleton that freezes its path
    on first use; reset it so trades created here land in the same DB the
    endpoints read (mirrors the user_db rebind in tests/conftest.py).
    """
    import carrac.content_db as cdb

    cdb._db_instance = None
    db = cdb.get_content_db()
    yield db
    cdb._db_instance = None


def _closed_trade(db, uid, *, asset="BTC", direction="long",
                  entry_price=65000, stop_price=63000, target_price=70000,
                  exit_price=70000, exit_reason="tp_hit", notes=None,
                  size=1.5, fees=12.34):
    """Create + close a trade owned by ``uid`` with account-size fields set
    (so tests can assert size/fees never reach the public snapshot)."""
    tid = db.log_trade_open(
        user_id=uid, asset=asset, timeframe="1D", direction=direction,
        entry_price=entry_price, stop_price=stop_price,
        target_price=target_price,
    )
    db.trade_update(tid, size=size, fees=fees)
    db.log_trade_close(journal_id=tid, exit_price=exit_price,
                       exit_reason=exit_reason, notes=notes)
    return tid


def _open_trade(db, uid, **kw):
    return db.log_trade_open(
        user_id=uid, asset="ETH", timeframe="4h", direction="long",
        entry_price=3000, stop_price=2900, target_price=3300, **kw)


# ── auth + closed-ness gates ──

def test_mint_requires_auth(asgi_client, trade_db):
    resp = asgi_client.post("/api/journal/trades/1/share", json={})
    assert resp.status_code in (401, 302)


def test_open_trade_cannot_be_shared(asgi_client, member_key, trade_db):
    headers, uid = member_key
    tid = _open_trade(trade_db, uid)
    resp = asgi_client.post(f"/api/journal/trades/{tid}/share", json={},
                            headers=headers)
    assert resp.status_code == 400


def test_unknown_trade_404(asgi_client, member_key, trade_db):
    headers, _uid = member_key
    resp = asgi_client.post("/api/journal/trades/999999/share", json={},
                            headers=headers)
    assert resp.status_code == 404


def test_cannot_share_someone_elses_trade(asgi_client, member_key, trade_db):
    headers, uid = member_key
    other = _closed_trade(trade_db, uid + 1000)
    resp = asgi_client.post(f"/api/journal/trades/{other}/share", json={},
                            headers=headers)
    assert resp.status_code == 404  # not-owned == not-found


# ── mint payload + privacy ──

def test_mint_returns_absolute_payload(asgi_client, member_key, trade_db):
    headers, uid = member_key
    tid = _closed_trade(trade_db, uid)

    resp = asgi_client.post(f"/api/journal/trades/{tid}/share", json={},
                            headers=headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["url"].startswith("http") and data["url"].endswith(f"/s/{data['slug']}")
    assert data["image_url"].endswith(f"/s/{data['slug']}/card.png")
    assert data["include_levels"] is True   # default ON
    assert data["include_note"] is False    # default OFF
    assert data["snapshot_asset"] == "BTC"
    # +7.69% on a 65k→70k long
    assert data["snapshot_pnl_pct"] == pytest.approx(7.69, abs=0.01)


def test_share_page_still_has_brochure(asgi_client, client, member_key, trade_db):
    """The brochure was replaced with per-asset context tiles
    on CHART shares only — trade shares (this file) keep the generic
    feature brochure unchanged."""
    headers, uid = member_key
    tid = _closed_trade(trade_db, uid)
    slug = asgi_client.post(f"/api/journal/trades/{tid}/share", json={},
                            headers=headers).json()["slug"]
    body = client.get(f"/s/{slug}").get_data(as_text=True)
    assert "What is Carrac?" in body


def test_snapshot_never_leaks_account_size(asgi_client, member_key, trade_db):
    headers, uid = member_key
    tid = _closed_trade(trade_db, uid, size=3.0, fees=99.5,
                        notes="a private note")

    slug = asgi_client.post(f"/api/journal/trades/{tid}/share", json={},
                            headers=headers).json()["slug"]
    snap = sh.get_share_by_slug(slug)["snapshot"]
    blob = str(snap)
    assert "3.0" not in blob and "99.5" not in blob
    for forbidden in ("size", "fees", "pnl_currency", "account"):
        assert forbidden not in snap
    # note OFF by default -> not present
    assert "note" not in snap
    # levels ON by default -> entry/exit present
    assert snap["levels"]["entry_price"] == 65000
    assert snap["levels"]["exit_price"] == 70000


def test_short_direction_sign(asgi_client, member_key, trade_db):
    headers, uid = member_key
    # short 70k -> 65k = +7.14% (profit); rr from risk(2k) reward(5k)
    tid = _closed_trade(trade_db, uid, direction="short",
                        entry_price=70000, stop_price=72000,
                        target_price=65000, exit_price=65000)
    data = asgi_client.post(f"/api/journal/trades/{tid}/share", json={},
                            headers=headers).json()
    assert data["snapshot_pnl_pct"] == pytest.approx(7.14, abs=0.01)
    assert data["snapshot_pnl_pct"] > 0   # short that dropped = a win


# ── reuse + toggle semantics ──

def test_mint_reuse_and_explicit_toggles_apply(asgi_client, member_key, trade_db):
    headers, uid = member_key
    tid = _closed_trade(trade_db, uid, notes="my close note")

    first = asgi_client.post(f"/api/journal/trades/{tid}/share", json={},
                             headers=headers).json()
    again = asgi_client.post(f"/api/journal/trades/{tid}/share",
                             json={"include_note": True}, headers=headers).json()
    assert again["slug"] == first["slug"]      # same active share
    assert again["include_note"] is True       # toggle applied on reuse

    snap = sh.get_share_by_slug(first["slug"])["snapshot"]
    assert snap.get("note") == "my close note"


def test_patch_toggles_and_404_without_share(asgi_client, member_key, trade_db):
    headers, uid = member_key
    tid = _closed_trade(trade_db, uid)

    # No active share yet -> 404.
    resp = asgi_client.patch(f"/api/journal/trades/{tid}/share",
                             json={"include_levels": False}, headers=headers)
    assert resp.status_code == 404

    asgi_client.post(f"/api/journal/trades/{tid}/share", json={}, headers=headers)
    resp = asgi_client.patch(f"/api/journal/trades/{tid}/share",
                             json={"include_levels": False}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["include_levels"] is False
    # levels now hidden in the re-frozen snapshot
    slug = resp.json()["slug"]
    assert "levels" not in sh.get_share_by_slug(slug)["snapshot"]


# ── shared quota pool across kinds ──

def test_quota_pool_shared_with_play_shares(asgi_client, member_key, trade_db,
                                            monkeypatch):
    headers, uid = member_key
    monkeypatch.setitem(sh.SHARE_QUOTA_BY_TIER, "member", 1)

    # Spend the single slot on a PLAY share...
    play = play_create(uid, title="quota play", primary_asset="BTC",
                       direction="long",
                       conditions={"logic": "all", "rules": []})
    assert asgi_client.post(f"/api/plays/{play['id']}/share", json={},
                            headers=headers).status_code == 201

    # ...the TRADE share now hits the same pool -> 402.
    tid = _closed_trade(trade_db, uid)
    resp = asgi_client.post(f"/api/journal/trades/{tid}/share", json={},
                            headers=headers)
    assert resp.status_code == 402
    body = resp.json()              # flat dict (strangler parity)
    assert body["limit_type"] == "share"
    assert body["current"] == 1 and body["max"] == 1


# ── revoke → cross-runtime 410 ──

def test_revoke_via_fastapi_gives_410_on_flask_public_page(
        asgi_client, client, member_key, trade_db):
    headers, uid = member_key
    tid = _closed_trade(trade_db, uid)

    slug = asgi_client.post(f"/api/journal/trades/{tid}/share", json={},
                            headers=headers).json()["slug"]
    assert client.get(f"/s/{slug}").status_code == 200

    resp = asgi_client.delete(f"/api/journal/trades/{tid}/share", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["revoked"] == 1

    # Public reads via the Flask ``client`` — /s/ + /api/s/ are Flask-only
    # blueprint routes; reading them through asgi_client would hit the
    # strangler WSGI-fallthrough worker thread (stale per-test sqlite conn).
    assert client.get(f"/s/{slug}").status_code == 410
    assert client.get(f"/api/s/{slug}").status_code == 410
