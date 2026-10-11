"""Plays subroutes routing tests.

The strangler dispatcher's PrefixPath ``/api/plays/`` (which also accepts POST) routes the five subroutes (status / link-drawing /
unlink-drawing / evaluate / backtest) to FastAPI while leaving GETs and
the bare ``/api/plays/<id>`` route family on Flask.

DB-level handler behaviour is covered by the existing Flask handler
tests; the FastAPI router delegates to the same module-level helpers in
``carrac.plays.models`` and ``carrac.plays.routes``, so re-running
those tests on the FastAPI side is unnecessary.
"""

from __future__ import annotations


def test_plays_subroute_router_has_all_five_endpoints():
    from carrac.fastapi_app.routers.plays import router

    paths = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
    assert ("/api/plays/{play_id}/status", ("PUT",)) in paths
    assert ("/api/plays/{play_id}/link-drawing", ("POST",)) in paths
    assert ("/api/plays/{play_id}/unlink-drawing", ("POST",)) in paths
    assert ("/api/plays/{play_id}/evaluate", ("POST",)) in paths
    assert ("/api/plays/{play_id}/backtest", ("POST",)) in paths


def test_status_request_rejects_extra_fields():
    from pydantic import ValidationError

    from carrac.fastapi_app.models.plays import PlayStatusRequest

    PlayStatusRequest(status="watching")  # ok

    try:
        PlayStatusRequest(status="watching", surprise="x")
    except ValidationError:
        pass
    else:
        raise AssertionError("extra='forbid' did not reject unknown field")


def test_link_drawing_request_requires_int_id():
    from pydantic import ValidationError

    from carrac.fastapi_app.models.plays import PlayLinkDrawingRequest

    PlayLinkDrawingRequest(drawing_id=42)
    try:
        PlayLinkDrawingRequest(drawing_id=0)  # ge=1
    except ValidationError:
        pass
    else:
        raise AssertionError("ge=1 did not reject 0")


def test_backtest_request_optional_lookback_and_hold():
    from carrac.fastapi_app.models.plays import PlayBacktestRequest

    # Both fields optional — empty body is valid (handler clamps to defaults).
    body = PlayBacktestRequest()
    assert body.lookback_bars is None
    assert body.hold_bars is None

    body = PlayBacktestRequest(lookback_bars=500, hold_bars=10)
    assert body.lookback_bars == 500
    assert body.hold_bars == 10


# ---------------------------------------------------------------------------
# The stranger smoke found POST /evaluate answering 500 on a fresh
# Postgres clone. Every other play route runs its payload through
# jsonable_encoder; evaluate returned the raw dict, and on Postgres the play
# row carries real datetime objects (created_at / updated_at /
# status_changed_at) that JSONResponse cannot serialise. Tests run SQLite,
# where those columns come back as strings, so the suite never saw it — this
# test injects the Postgres shape on purpose.
# ---------------------------------------------------------------------------


def test_evaluate_serialises_a_postgres_shaped_play(asgi_client, member_key, monkeypatch):
    from datetime import datetime, timezone

    from carrac.plays import models as plays_models
    from carrac.plays.monitor import plays_monitor

    headers, uid = member_key
    play = plays_models.play_create(
        uid, title="Postgres shape", primary_asset="BTC", direction="long",
        conditions={"logic": "all", "rules": []},
    )
    stamp = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    real_get_play = plays_models.get_play

    def _pg_shaped(play_id, user_id):
        row = real_get_play(play_id, user_id)
        if row:
            row["created_at"] = stamp
            row["updated_at"] = stamp
            row["status_changed_at"] = stamp
        return row

    monkeypatch.setattr(plays_models, "get_play", _pg_shaped)
    monkeypatch.setattr(plays_monitor, "_get_cached_data", lambda: {
        "assets": [{"symbol": "BTC", "realtime": {"price": 100000}, "latest_price": 100000}],
    })

    resp = asgi_client.post(f"/api/plays/{play['id']}/evaluate", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["play"]["created_at"].startswith("2026-09-02T12:00:00")
