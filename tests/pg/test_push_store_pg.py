"""The push-subscription store on real Postgres: JSONB round trip, the
ON CONFLICT upsert, concurrent writers, and the legacy import."""

from __future__ import annotations

import json
import socket
import threading
from unittest.mock import patch

import pytest

ENDPOINT = "https://fcm.googleapis.com/fcm/send/pg-test"
KEYS = {"p256dh": "test-p256dh-key", "auth": "test-auth-secret"}


def _sub(endpoint=ENDPOINT):
    return {"endpoint": endpoint, "expirationTime": None, "keys": dict(KEYS)}


@pytest.fixture(autouse=True)
def _clean_subscriptions(pg_conn, monkeypatch, tmp_path):
    from carrac.alerts import push_store
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE push_subscriptions")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(push_store, "_import_checked", True)
    monkeypatch.setattr(push_store, "_redis", lambda: None)
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.74.10", 443))]
    with patch("socket.getaddrinfo", return_value=public):
        yield


def test_round_trip_and_upsert_replaces(udb):
    import push_notifications as pn

    pn.add_subscription("1", _sub())
    assert pn.get_user_subscription("1") == {"endpoint": ENDPOINT, "keys": KEYS}
    pn.add_subscription("1", _sub(ENDPOINT + "-newer"))
    assert pn.get_user_subscription("1")["endpoint"] == ENDPOINT + "-newer"
    assert pn.remove_subscription("1") is True
    assert pn.get_user_subscription("1") is None


def test_concurrent_subscribes_of_different_users_all_land(udb):
    import push_notifications as pn

    users = [str(i) for i in range(1, 9)]
    start = threading.Barrier(len(users))
    errors = []

    def _subscribe(uid):
        try:
            start.wait(5)
            pn.add_subscription(uid, _sub(f"{ENDPOINT}-{uid}"))
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_subscribe, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert all(pn.get_user_subscription(u) for u in users)


def test_legacy_import(udb, tmp_path):
    import push_notifications as pn
    from carrac.alerts import push_store

    path = tmp_path / "push_subscriptions.json"
    path.write_text(json.dumps({
        "1": {"subscription": _sub(), "created_at": "2026-09-01T10:00:00", "last_notified": None},
        "2": {"subscription": _sub(ENDPOINT + "-2"), "created_at": "2026-09-02T10:00:00Z"},
    }))

    assert push_store.import_legacy() == 2
    assert (tmp_path / "push_subscriptions.json.imported").exists()
    assert pn.get_user_subscription("2")["endpoint"] == ENDPOINT + "-2"
    assert push_store.import_legacy() is None
