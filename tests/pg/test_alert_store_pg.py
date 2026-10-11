"""The alert store on real Postgres: JSONB and BIGINT round trip, row locks,
failures that raise without poisoning the connection, and the legacy import."""

from __future__ import annotations

import json
import threading

import psycopg2
import pytest
from psycopg2.extensions import TRANSACTION_STATUS_IDLE


@pytest.fixture(autouse=True)
def _clean_alerts(pg_conn, monkeypatch, tmp_path):
    from carrac.alerts import store
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE user_alerts")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "_import_checked", True)
    yield


def test_round_trip_keeps_types(udb):
    import push_notifications as pn

    created = pn.create_zscore_alert("1", "VIX", "above", 2.0, note="n")
    [stored] = pn.get_user_alerts("1")

    assert stored["id"] == created["id"]
    assert stored["user_id"] == "1" and stored["alert_type"] == "zscore"
    assert stored["threshold"] == 2.0 and stored["window"] == 90
    assert stored["active"] is True and stored["triggered"] is False
    assert "symbol" not in stored
    assert stored["created_at"].endswith("+00:00")


def test_a_large_id_fits(udb):
    from carrac.alerts import store
    store.insert({"id": 2**32 - 1, "user_id": "1", "symbol": "BTC", "active": True})
    assert store.list_alerts()[0]["id"] == 2**32 - 1


def test_save_changes_keeps_a_concurrent_edit(udb):
    import push_notifications as pn
    from carrac.alerts import store

    alert = pn.create_alert("1", "BTC", "above", 100.0)
    alerts = store.list_alerts(active_only=True)
    baseline = store.snapshot(alerts)
    pn.update_alert("1", alert["id"], {"note": "edited"})
    alerts[0]["triggered"] = True
    store.save_changes(alerts, baseline)

    [now] = store.list_alerts()
    assert now["note"] == "edited" and now["triggered"] is True


def test_concurrent_modifies_of_one_alert_both_land(udb):
    """SELECT ... FOR UPDATE makes the second writer wait for the first."""
    import push_notifications as pn
    from carrac.alerts import store

    alert = pn.create_alert("1", "BTC", "above", 100.0)
    holding = threading.Event()

    def _slow_note(a):
        a["note"] = "from A"
        holding.set()
        threading.Event().wait(0.5)

    a = threading.Thread(target=store.modify, args=(alert["id"], _slow_note))
    a.start()
    holding.wait(5)
    store.modify(alert["id"], lambda row: row.update(price=200.0))
    a.join()

    [now] = store.list_alerts()
    assert (now["note"], now["price"]) == ("from A", 200.0)


def test_delete_many_is_owner_scoped(udb):
    import push_notifications as pn

    mine = pn.create_alert("1", "BTC", "above", 1.0)
    theirs = pn.create_alert("2", "BTC", "above", 1.0)
    deleted, skipped = pn.delete_alerts_bulk("1", [mine["id"], theirs["id"]])
    assert deleted == [mine["id"]] and skipped == [theirs["id"]]
    assert len(pn.get_user_alerts("2")) == 1


def test_a_failed_write_raises_and_leaves_the_connection_clean(udb):
    from carrac.alerts import store

    store.insert({"id": 5, "user_id": "1", "symbol": "BTC", "active": True})
    with pytest.raises(psycopg2.errors.UniqueViolation):
        store.insert({"id": 5, "user_id": "1", "symbol": "ETH", "active": True})
    assert udb._local.pg_conn.info.transaction_status == TRANSACTION_STATUS_IDLE
    assert [a["symbol"] for a in store.list_alerts()] == ["BTC"]


def test_legacy_import(udb, tmp_path):
    from carrac.alerts import store

    path = tmp_path / "price_alerts.json"
    path.write_text(json.dumps([
        {"id": 7, "user_id": "1", "symbol": "BTC", "condition": "above", "price": 1.0,
         "active": True, "triggered": False, "created_at": "2026-09-01T10:00:00"},
        {"id": "a1b2c3d4", "user_id": "1", "symbol": "ETH", "alert_type": "formula",
         "expression": "close < 1", "active": True, "created_at": "2026-09-02T10:00:00Z"},
    ]))

    assert store.import_legacy_json() == 2
    assert (tmp_path / "price_alerts.json.imported").exists()
    assert [a["id"] for a in store.list_alerts()] == [7, int("a1b2c3d4", 16)]
    assert store.import_legacy_json() is None
