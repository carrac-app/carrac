"""Drawing writes publish a cross-device sync event, tagged with the
writing tab's ``X-Sync-Session`` so that tab can ignore its own echo."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_delete_by_local_id_publishes_sync(key_client):
    client = key_client("member")
    db = MagicMock()
    db.delete_drawing_by_local_id.return_value = True

    with patch("carrac.content_db.get_content_db", return_value=db), \
         patch("carrac.sync.events.publish_drawing_event") as publish:
        r = client.delete("/api/drawings/by-local-id",
                          json={"asset": "ETH", "local_id": "overlay_123"},
                          headers={"X-Sync-Session": "tab_abc"})

    assert r.status_code == 200
    assert r.json()["success"] is True
    db.delete_drawing_by_local_id.assert_called_once_with(
        "ETH", "overlay_123", user_id=client.user_id)
    publish.assert_called_once_with(client.user_id, ["ETH"], "tab_abc")


def test_delete_of_missing_drawing_publishes_nothing(key_client):
    client = key_client("member")
    db = MagicMock()
    db.delete_drawing_by_local_id.return_value = False

    with patch("carrac.content_db.get_content_db", return_value=db), \
         patch("carrac.sync.events.publish_drawing_event") as publish:
        r = client.delete("/api/drawings/by-local-id",
                          json={"asset": "ETH", "local_id": "nonexistent"})

    assert r.json()["success"] is False
    publish.assert_not_called()


def test_delete_by_local_id_requires_asset_and_local_id(key_client):
    r = key_client("member").delete("/api/drawings/by-local-id", json={"asset": "ETH"})
    assert r.status_code == 400


def test_upsert_publishes_sync(key_client):
    client = key_client("member")
    with patch("carrac.sync.events.publish_drawing_event") as publish:
        r = client.post("/api/drawings/upsert",
                        json={"asset": "BTC", "timeframe": "1D",
                              "drawing_type": "trendline",
                              "metadata": {"local_id": "test1"}},
                        headers={"X-Sync-Session": "tab_xyz"})

    assert r.status_code in (200, 201), r.text
    publish.assert_called_once_with(client.user_id, ["BTC"], "tab_xyz")


def test_failed_sync_publish_is_logged_and_does_not_fail_the_write(key_client, caplog):
    client = key_client("member")
    with patch("carrac.sync.events.publish_drawing_event",
               side_effect=RuntimeError("redis down")), \
         caplog.at_level("WARNING", logger="carrac.fastapi_app.routers.drawings"):
        r = client.post("/api/drawings/upsert",
                        json={"asset": "BTC", "timeframe": "1D",
                              "drawing_type": "trendline",
                              "metadata": {"local_id": "test2"}},
                        headers={"X-Sync-Session": "tab_xyz"})

    assert r.status_code in (200, 201), r.text
    warnings = [rec for rec in caplog.records
                if rec.levelname == "WARNING" and "sync event" in rec.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert f"user_id={client.user_id}" in msg
    assert "kind=drawing" in msg
    assert "BTC" in msg
    assert warnings[0].exc_info is not None
