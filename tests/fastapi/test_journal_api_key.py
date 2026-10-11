"""The journal REST API is usable with a per-user API key.

Every ``/api/journal/*`` route (and the data routes under ``/api/ai/journal``)
accepts ``X-API-Key`` exactly like drawings and plays do, resolves the caller
from the key rather than the session, and answers an unauthenticated JSON
caller with 401 instead of a redirect to the login page.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

TRADE = {"asset": "BTC", "direction": "long", "entry_price": 80000,
         "stop_price": 78000, "target_price": 90000}


def test_trade_lifecycle_with_api_key(key_client):
    c = key_client("member")

    created = c.post("/api/journal/trades", json=TRADE)
    assert created.status_code == 201, created.text
    tid = created.json()["id"]

    updated = c.patch(f"/api/journal/trades/{tid}",
                      json={"stop_price": 77000, "notes": "moved stop"})
    assert updated.status_code == 200, updated.text

    closed = c.post(f"/api/journal/trades/{tid}/close",
                    json={"exit_price": 85000})
    assert closed.status_code == 200, closed.text

    one = c.get(f"/api/journal/trades/{tid}")
    assert one.status_code == 200, one.text
    trade = one.json()
    assert trade["stop_price"] == 77000
    assert trade["notes"] == "moved stop"
    assert trade["exit_price"] == 85000
    assert trade["closed_at"]

    listed = c.get("/api/journal/trades")
    assert listed.status_code == 200, listed.text
    assert [t["id"] for t in listed.json()["history"]] == [tid]


def test_other_users_trade_is_not_visible_to_a_key(key_client):
    owner, intruder = key_client("member"), key_client("member")
    tid = owner.post("/api/journal/trades", json=TRADE).json()["id"]
    assert intruder.get(f"/api/journal/trades/{tid}").status_code == 404
    assert intruder.patch(f"/api/journal/trades/{tid}",
                          json={"notes": "x"}).status_code in (403, 404)


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/journal/trades"),
    ("GET", "/api/journal/analytics"),
    ("POST", "/api/journal/trades"),
    ("PATCH", "/api/journal/trades/1"),
    ("POST", "/api/journal/trades/1/close"),
    ("GET", "/api/ai/journal"),
])
def test_unauthenticated_json_caller_gets_401(asgi_client, method, path):
    resp = asgi_client.request(method, path, json={},
                               headers={"Accept": "application/json"},
                               follow_redirects=False)
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/journal/trades"),
    ("POST", "/api/journal/trades"),
])
def test_shared_internal_key_is_refused(asgi_client, method, path):
    headers = {"X-Internal-Key": os.environ["SCREENSHOT_API_KEY"],
               "Accept": "application/json"}
    resp = asgi_client.request(method, path, json=TRADE, headers=headers,
                               follow_redirects=False)
    assert resp.status_code == 401, resp.text


@pytest.fixture
def _pg_style_datetimes(monkeypatch):
    """Postgres hands back ``datetime`` objects; SQLite hands back text."""
    from carrac.content_db import ContentDatabase

    def convert(row):
        if isinstance(row, dict):
            for col in ("opened_at", "closed_at"):
                if isinstance(row.get(col), str):
                    row[col] = datetime.fromisoformat(row[col])
        return row

    for name in ("get_journal_entry", "get_trade_history", "get_open_trades"):
        original = getattr(ContentDatabase, name)

        def wrapped(self, *args, _orig=original, **kwargs):
            out = _orig(self, *args, **kwargs)
            if isinstance(out, list):
                return [convert(r) for r in out]
            return convert(out)

        monkeypatch.setattr(ContentDatabase, name, wrapped)


def test_journal_dates_are_iso_8601_everywhere(key_client, _pg_style_datetimes):
    c = key_client("member")
    tid = c.post("/api/journal/trades", json=TRADE).json()["id"]
    c.post(f"/api/journal/trades/{tid}/close", json={"exit_price": 85000})
    patched = c.patch(f"/api/journal/trades/{tid}", json={"notes": "n"}).json()["entry"]
    fetched = c.get(f"/api/journal/trades/{tid}").json()
    listed = c.get("/api/journal/trades").json()["history"][0]

    for label, row in (("patch", patched), ("get", fetched), ("list", listed)):
        for col in ("opened_at", "closed_at"):
            value = row[col]
            assert "GMT" not in value and "," not in value, f"{label}.{col}: {value}"
            datetime.fromisoformat(value)


def test_ai_journal_data_routes_accept_a_key(key_client):
    c = key_client("member")
    opened = c.post("/api/ai/journal/open",
                    json={"asset": "ETH", "entry_price": 2500})
    assert opened.status_code == 200, opened.text
    jid = opened.json()["id"]
    assert c.patch(f"/api/ai/journal/{jid}", json={"notes": "n"}).status_code == 200
    assert c.post("/api/ai/journal/close",
                  json={"id": jid, "exit_price": 2600}).status_code == 200
    got = c.get("/api/ai/journal")
    assert got.status_code == 200, got.text
    assert any(t["id"] == jid for t in got.json()["history"])
