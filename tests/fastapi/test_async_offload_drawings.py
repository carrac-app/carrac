"""Drawings router async-offload coverage.

The six ``async def`` handlers in ``carrac/fastapi_app/routers/drawings.py``
made blocking ``ContentDatabase`` calls directly on the request coroutine,
stalling the uvicorn event loop for every concurrent request while a
drawing read/write was in flight. Each handler must run its DB work via
``asyncio.to_thread`` instead.

Recipe: monkeypatch the narrowest ``ContentDatabase`` method at CLASS level
with a wrapper that records ``threading.get_ident()`` before delegating to
the original implementation, then drive the endpoint through ``asgi_client``
(real per-user API key auth via the ``member_key`` fixture). The
``asgi_client`` fixture runs its event loop on the *test's own thread*, so
the offloaded DB call must show a *different* thread ident than the test
thread — proving the work left the event loop.
"""

from __future__ import annotations

import threading

import pytest

from carrac.content_db import ContentDatabase


def _record_ident(monkeypatch, method_name):
    """Monkeypatch ``ContentDatabase.<method_name>`` to record its caller thread.

    Returns the list idents are appended to (one entry per call).
    """
    idents: list[int] = []
    original = getattr(ContentDatabase, method_name)

    def wrapper(self, *args, **kwargs):
        idents.append(threading.get_ident())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ContentDatabase, method_name, wrapper)
    return idents


def _create_body(asset="BTCUSDT", timeframe="1D", **overrides):
    body = {
        "asset": asset,
        "timeframe": timeframe,
        "drawing_type": "horizontal_line",
        "price_level": 94000.0,
        "color": "#26a69a",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# POST /api/drawings — create
# ---------------------------------------------------------------------------


def test_create_drawing_offloads_save_drawing(asgi_client, member_key, monkeypatch):
    headers, _uid = member_key
    idents = _record_ident(monkeypatch, "save_drawing")
    main_ident = threading.get_ident()

    resp = asgi_client.post("/api/drawings", json=_create_body(), headers=headers)

    assert resp.status_code == 201, resp.text
    assert resp.json()["success"] is True
    assert idents, "ContentDatabase.save_drawing was never called"
    assert all(i != main_ident for i in idents), (
        "save_drawing ran on the event-loop thread — blocking call not offloaded"
    )


# ---------------------------------------------------------------------------
# PUT /api/drawings/{id} — update
# ---------------------------------------------------------------------------


def test_update_drawing_offloads_update_drawing(asgi_client, member_key, monkeypatch):
    headers, _uid = member_key

    create_resp = asgi_client.post("/api/drawings", json=_create_body(), headers=headers)
    assert create_resp.status_code == 201, create_resp.text
    drawing_id = create_resp.json()["drawing_id"]

    idents = _record_ident(monkeypatch, "drawing_update")
    main_ident = threading.get_ident()

    resp = asgi_client.put(
        f"/api/drawings/{drawing_id}",
        json={"label": "resistance"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    assert idents, "ContentDatabase.drawing_update was never called"
    assert all(i != main_ident for i in idents), (
        "drawing_update ran on the event-loop thread — blocking call not offloaded"
    )


# ---------------------------------------------------------------------------
# DELETE /api/drawings/{id} — delete
# ---------------------------------------------------------------------------


def test_delete_drawing_offloads_delete_drawing(asgi_client, member_key, monkeypatch):
    headers, _uid = member_key

    create_resp = asgi_client.post("/api/drawings", json=_create_body(), headers=headers)
    assert create_resp.status_code == 201, create_resp.text
    drawing_id = create_resp.json()["drawing_id"]

    idents = _record_ident(monkeypatch, "drawing_delete")
    main_ident = threading.get_ident()

    resp = asgi_client.delete(f"/api/drawings/{drawing_id}", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    assert idents, "ContentDatabase.drawing_delete was never called"
    assert all(i != main_ident for i in idents), (
        "drawing_delete ran on the event-loop thread — blocking call not offloaded"
    )


# ---------------------------------------------------------------------------
# DELETE /api/drawings/by-local-id
# ---------------------------------------------------------------------------


def test_delete_drawing_by_local_id_offloads(asgi_client, member_key, monkeypatch):
    headers, _uid = member_key

    create_resp = asgi_client.post(
        "/api/drawings",
        json=_create_body(metadata={"local_id": "overlay_abc"}),
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text

    idents = _record_ident(monkeypatch, "delete_drawing_by_local_id")
    main_ident = threading.get_ident()

    resp = asgi_client.request(
        "DELETE",
        "/api/drawings/by-local-id",
        json={"asset": "BTCUSDT", "local_id": "overlay_abc"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert idents, "ContentDatabase.delete_drawing_by_local_id was never called"
    assert all(i != main_ident for i in idents), (
        "delete_drawing_by_local_id ran on the event-loop thread — blocking call not offloaded"
    )


# ---------------------------------------------------------------------------
# POST /api/drawings/upsert — create-or-update by local_id
# ---------------------------------------------------------------------------


def test_upsert_drawing_create_branch_offloads_find_drawing_by_local_id(
    asgi_client, member_key, monkeypatch
):
    headers, _uid = member_key
    idents = _record_ident(monkeypatch, "find_drawing_by_local_id")
    main_ident = threading.get_ident()

    resp = asgi_client.post(
        "/api/drawings/upsert",
        json=_create_body(metadata={"local_id": "upsert_new_1"}),
        headers=headers,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["action"] == "created"
    assert idents, "ContentDatabase.find_drawing_by_local_id was never called"
    assert all(i != main_ident for i in idents), (
        "find_drawing_by_local_id ran on the event-loop thread — blocking call not offloaded"
    )


def test_upsert_drawing_update_branch_offloads_update_drawing(
    asgi_client, member_key, monkeypatch
):
    headers, _uid = member_key

    create_resp = asgi_client.post(
        "/api/drawings/upsert",
        json=_create_body(metadata={"local_id": "upsert_existing_1"}),
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text

    idents = _record_ident(monkeypatch, "drawing_update")
    main_ident = threading.get_ident()

    resp = asgi_client.post(
        "/api/drawings/upsert",
        json=_create_body(
            metadata={"local_id": "upsert_existing_1"}, price_level=95000.0
        ),
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "updated"
    assert idents, "ContentDatabase.drawing_update was never called"
    assert all(i != main_ident for i in idents), (
        "drawing_update ran on the event-loop thread — blocking call not offloaded"
    )


# ---------------------------------------------------------------------------
# POST /api/drawings/bulk — bulk create
# ---------------------------------------------------------------------------


def test_bulk_create_drawings_offloads_save_drawing(asgi_client, member_key, monkeypatch):
    headers, _uid = member_key
    idents = _record_ident(monkeypatch, "save_drawing")
    main_ident = threading.get_ident()

    resp = asgi_client.post(
        "/api/drawings/bulk",
        json={
            "drawings": [
                _create_body(asset="ETHUSDT", timeframe="4H"),
                _create_body(asset="SOLUSDT", timeframe="1H"),
            ]
        },
        headers=headers,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created_count"] == 2
    assert len(idents) >= 2, "ContentDatabase.save_drawing not called for each bulk item"
    assert all(i != main_ident for i in idents), (
        "save_drawing ran on the event-loop thread — blocking call not offloaded"
    )


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
