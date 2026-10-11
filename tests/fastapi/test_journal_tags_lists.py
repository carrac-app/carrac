"""FastAPI journal update/close answer with ``tags`` as lists, not JSON text."""

from __future__ import annotations

import json

from carrac.content_db import get_content_db

UID = 70772


def _open_trade() -> int:
    return get_content_db().log_trade_open(
        user_id=UID, asset="BTC", timeframe="1D", direction="long",
        entry_price=100.0, tags=json.dumps(["src:manual"]),
    )


def test_update_returns_tag_lists(asgi_client, login_as):
    tid = _open_trade()
    with login_as({"id": UID, "email": "t@example.com", "tier": "member"}):
        resp = asgi_client.patch(
            f"/api/journal/trades/{tid}",
            json={"tags": ["src:manual", "swing"], "mistake_tags": ["fomo"]},
        )
    assert resp.status_code == 200, resp.text
    entry = resp.json()["entry"]
    assert entry["tags"] == ["src:manual", "swing"]
    assert entry["mistake_tags"] == ["fomo"]


def test_close_returns_tag_lists(asgi_client, login_as, monkeypatch):
    monkeypatch.setattr("carrac.integrations.on_trade_closed", lambda *a, **kw: None)
    tid = _open_trade()
    with login_as({"id": UID, "email": "t@example.com", "tier": "member"}):
        resp = asgi_client.post(
            f"/api/journal/trades/{tid}/close",
            json={"exit_price": 110.0, "exit_reason": "tp_hit"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["entry"]["tags"] == ["src:manual"]


def test_create_with_tag_list_persists_and_reads_back_as_list(asgi_client, login_as):
    with login_as({"id": UID, "email": "t@example.com", "tier": "member"}):
        resp = asgi_client.post(
            "/api/journal/trades",
            json={"asset": "BTC", "timeframe": "1D", "direction": "long",
                  "entry_price": 100.0, "tags": ["swing"]},
        )
    assert resp.status_code == 201, resp.text
    stored = get_content_db().get_journal_entry(resp.json()["id"])
    assert json.loads(stored["tags"]) == ["swing"]
