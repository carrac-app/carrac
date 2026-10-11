"""Async-offload coverage for carrac/fastapi_app/routers/plays.py.

Every ``async def`` handler in ``plays.py`` calls into blocking sync DB
layers (``carrac.plays.models`` / ``carrac.plays.shares`` /
``carrac.plays.routes`` / ``carrac.plays.monitor``). A blocking call made
directly inside an ``async def`` handler stalls the uvicorn event loop for
ALL concurrent requests — it must run via ``asyncio.to_thread(...)``.

These tests monkeypatch the narrowest DB-layer function/method each
handler calls — at the module where the router looks it up (the handlers
do a late ``from module import fn`` inside the function body, so patching
the module attribute before the request is issued is picked up) — with a
wrapper that records ``threading.get_ident()`` before delegating to the
real implementation (or, where running the real heavy compute is
impractical in a unit test, a fast stub). The ``asgi_client`` fixture
(``tests/fastapi/conftest.py``) drives the strangler ASGI app via
``run_until_complete`` on the TEST's own thread, so the event-loop thread
ident == ``threading.get_ident()`` inside the test body. A wrapped call
must therefore be recorded on a DIFFERENT thread.
"""

from __future__ import annotations

import threading

from carrac.auth import decorators as auth_decorators
from carrac.plays import models as plays_models
from carrac.plays import routes as plays_routes
from carrac.plays import shares as plays_shares
from carrac.plays.monitor import plays_monitor


def _recorder(orig_fn):
    """Wrap ``orig_fn``: record the calling thread ident, delegate to the real impl."""
    holder = {"thread_id": None}

    def _wrapped(*args, **kwargs):
        holder["thread_id"] = threading.get_ident()
        return orig_fn(*args, **kwargs)

    return _wrapped, holder


def _assert_offloaded(holder):
    assert holder["thread_id"] is not None, "wrapped DB function was never called"
    assert holder["thread_id"] != threading.get_ident(), (
        "DB call ran on the event-loop thread — not offloaded via asyncio.to_thread"
    )


def _play_for(uid, **kwargs):
    defaults = dict(
        title="Async offload test play",
        thesis="Offload thesis.",
        primary_asset="BTC",
        direction="long",
        conditions={"logic": "all", "rules": []},
    )
    defaults.update(kwargs)
    return plays_models.play_create(uid, **defaults)


# ---------------------------------------------------------------------------
# GET /api/plays/active
# ---------------------------------------------------------------------------


def test_active_plays_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    _play_for(uid)

    wrapped, holder = _recorder(plays_models.get_active_plays)
    monkeypatch.setattr(plays_models, "get_active_plays", wrapped)

    resp = asgi_client.get("/api/plays/active", headers=headers)
    assert resp.status_code == 200
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# POST /api/plays
# ---------------------------------------------------------------------------


def test_create_play_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key

    wrapped, holder = _recorder(plays_models.play_create)
    monkeypatch.setattr(plays_models, "play_create", wrapped)

    resp = asgi_client.post(
        "/api/plays",
        json={"title": "New offload play", "primary_asset": "BTC"},
        headers=headers,
    )
    assert resp.status_code == 201
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# PUT /api/plays/{id}
# ---------------------------------------------------------------------------


def test_update_play_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    play = _play_for(uid)

    wrapped, holder = _recorder(plays_models.update_play)
    monkeypatch.setattr(plays_models, "update_play", wrapped)

    resp = asgi_client.put(
        f"/api/plays/{play['id']}",
        json={"title": "Updated title"},
        headers=headers,
    )
    assert resp.status_code == 200
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# DELETE /api/plays/{id}
# ---------------------------------------------------------------------------


def test_delete_play_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    play = _play_for(uid)

    wrapped, holder = _recorder(plays_models.play_delete)
    monkeypatch.setattr(plays_models, "play_delete", wrapped)

    resp = asgi_client.delete(f"/api/plays/{play['id']}", headers=headers)
    assert resp.status_code == 200
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# PUT /api/plays/{id}/status
# ---------------------------------------------------------------------------


def test_transition_play_status_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    play = _play_for(uid)

    wrapped, holder = _recorder(plays_models.transition_status)
    monkeypatch.setattr(plays_models, "transition_status", wrapped)

    resp = asgi_client.put(
        f"/api/plays/{play['id']}/status",
        json={"status": "acting"},
        headers=headers,
    )
    assert resp.status_code == 200
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# POST /api/plays/{id}/link-drawing — the IDOR fix lives inside
# ``carrac.plays.models.link_drawing`` (ownership check on the drawing).
# Wrapping the WHOLE function call preserves that check identically — it
# just now runs inside the offloaded thread.
# ---------------------------------------------------------------------------


def test_link_drawing_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    play = _play_for(uid)

    from carrac.content_db import get_content_db

    db = get_content_db()
    drawing_id = db.save_drawing(
        asset="BTC", timeframe="1D", drawing_type="horizontal_line",
        price_level=100000, user_id=uid,
    )

    wrapped, holder = _recorder(plays_models.link_drawing)
    monkeypatch.setattr(plays_models, "link_drawing", wrapped)

    resp = asgi_client.post(
        f"/api/plays/{play['id']}/link-drawing",
        json={"drawing_id": drawing_id},
        headers=headers,
    )
    assert resp.status_code == 200
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# POST /api/plays/{id}/unlink-drawing
# ---------------------------------------------------------------------------


def test_unlink_drawing_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    play = _play_for(uid)

    from carrac.content_db import get_content_db

    db = get_content_db()
    drawing_id = db.save_drawing(
        asset="BTC", timeframe="1D", drawing_type="horizontal_line",
        price_level=100000, user_id=uid,
    )
    # Real (unpatched) link first so there's something to unlink.
    plays_models.link_drawing(play["id"], uid, drawing_id)

    wrapped, holder = _recorder(plays_models.unlink_drawing)
    monkeypatch.setattr(plays_models, "unlink_drawing", wrapped)

    resp = asgi_client.post(
        f"/api/plays/{play['id']}/unlink-drawing",
        json={"drawing_id": drawing_id},
        headers=headers,
    )
    assert resp.status_code == 200
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# POST /api/plays/{id}/evaluate
# ---------------------------------------------------------------------------


def test_evaluate_play_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    play = _play_for(uid)

    fake_cached = {
        "assets": [
            {"symbol": "BTC", "realtime": {"price": 100000}, "latest_price": 100000},
        ]
    }
    # Real Redis-backed response_cache isn't populated in tests — stub the
    # market-data fetch so the handler's DB-block (get_play / persist /
    # get_play) actually runs end to end instead of 503-ing early.
    monkeypatch.setattr(plays_monitor, "_get_cached_data", lambda: fake_cached)

    wrapped, holder = _recorder(plays_models.get_play)
    monkeypatch.setattr(plays_models, "get_play", wrapped)

    resp = asgi_client.post(f"/api/plays/{play['id']}/evaluate", headers=headers)
    assert resp.status_code == 200
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# POST /api/plays/{id}/backtest
# ---------------------------------------------------------------------------


def test_backtest_play_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    play = _play_for(uid)

    # Tier gate is orthogonal to the offload behaviour under test — bypass it.
    monkeypatch.setattr(
        auth_decorators, "get_tier_limits", lambda tier="free": {"backtester": True}
    )

    holder = {"thread_id": None}

    def _fake_run_backtest(play_arg, lookback, hold):
        holder["thread_id"] = threading.get_ident()
        return {
            "symbol": "BTC", "timeframe": "1D", "expression": "true",
            "conditions_used": [], "conditions_skipped": [],
            "trades": 0, "hit_rate": None, "mean_return": None,
            "median_return": None, "sharpe_like": None, "max_drawdown": None,
            "final_equity_multiple": None, "lookback_bars": lookback,
            "hold_bars": hold, "cached": False, "message": None,
        }

    monkeypatch.setattr(plays_routes, "_run_play_backtest", _fake_run_backtest)

    resp = asgi_client.post(
        f"/api/plays/{play['id']}/backtest", json={}, headers=headers
    )
    assert resp.status_code == 200
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# POST /api/plays/{id}/share
# ---------------------------------------------------------------------------


def test_create_play_share_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    play = _play_for(uid)

    wrapped, holder = _recorder(plays_shares.create_share)
    monkeypatch.setattr(plays_shares, "create_share", wrapped)

    resp = asgi_client.post(f"/api/plays/{play['id']}/share", json={}, headers=headers)
    assert resp.status_code == 201
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# PUT /api/plays/{id}/share
# ---------------------------------------------------------------------------


def test_update_play_share_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    play = _play_for(uid)
    asgi_client.post(f"/api/plays/{play['id']}/share", json={}, headers=headers)

    wrapped, holder = _recorder(plays_shares.update_share)
    monkeypatch.setattr(plays_shares, "update_share", wrapped)

    resp = asgi_client.put(
        f"/api/plays/{play['id']}/share",
        json={"include_plan": False},
        headers=headers,
    )
    assert resp.status_code == 200
    _assert_offloaded(holder)


# ---------------------------------------------------------------------------
# DELETE /api/plays/{id}/share
# ---------------------------------------------------------------------------


def test_revoke_play_share_offloaded(asgi_client, member_key, monkeypatch):
    headers, uid = member_key
    play = _play_for(uid)
    asgi_client.post(f"/api/plays/{play['id']}/share", json={}, headers=headers)

    wrapped, holder = _recorder(plays_shares.revoke_share)
    monkeypatch.setattr(plays_shares, "revoke_share", wrapped)

    resp = asgi_client.delete(f"/api/plays/{play['id']}/share", headers=headers)
    assert resp.status_code == 200
    _assert_offloaded(holder)
