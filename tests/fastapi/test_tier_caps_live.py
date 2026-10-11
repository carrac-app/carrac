"""Tier caps on the live write routes: the monthly journal-trade cap, the
paid-only notification channels and cross-device preference sync."""

from __future__ import annotations

import pytest


@pytest.fixture
def journal_db(tmp_path, monkeypatch):
    """A fresh ContentDatabase that the journal router uses for this test."""
    import carrac.db_pool as db_pool
    from carrac.content_db import ContentDatabase

    db_pool._db_type = None
    monkeypatch.setenv("DB_TYPE", "sqlite")
    db = ContentDatabase(db_path=str(tmp_path / "journal_cap.db"))
    monkeypatch.setattr("carrac.content_db.get_content_db", lambda: db)
    return db


def _log_trades(db, user_id, n):
    for _ in range(n):
        db.log_trade_open(user_id=user_id, asset="BTC", timeframe="1D",
                          direction="long", entry_price=100)


@pytest.fixture
def session_user(api_user, monkeypatch):
    """Factory: log the ASGI client in as a new user of ``tier`` (the journal
    write routes are session-only) and return the user id."""
    from contextlib import ExitStack

    from carrac.fastapi_app import fastapi_app
    from carrac.fastapi_app.routers.journal import journal_user

    stack = ExitStack()

    def _login(tier):
        _headers, uid = api_user(tier)
        fastapi_app.dependency_overrides[journal_user] = lambda: {"id": uid, "tier": tier}
        stack.callback(fastapi_app.dependency_overrides.pop, journal_user, None)
        monkeypatch.setattr("carrac.fastapi_app.deps.auth.get_request_tier",
                            lambda request: tier)
        return uid

    yield _login
    stack.close()


def test_free_tier_is_refused_past_the_monthly_journal_cap(asgi_client, session_user, journal_db):
    uid = session_user("free")
    _log_trades(journal_db, uid, 20)  # free's journal_trades_monthly

    resp = asgi_client.post("/api/journal/trades", json={"asset": "BTC", "entry_price": 100})

    assert resp.status_code == 403, resp.text
    data = resp.json()
    assert data["limit_type"] == "journal_trade"
    assert (data["current"], data["max"]) == (20, 20)
    assert "upgrade" in data["error"].lower()


def test_member_is_not_held_to_the_free_cap(asgi_client, session_user, journal_db):
    uid = session_user("member")
    _log_trades(journal_db, uid, 20)

    resp = asgi_client.post("/api/journal/trades", json={"asset": "BTC", "entry_price": 100})

    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize("channel", ["telegram", "discord", "webhook"])
def test_free_tier_enabling_a_paid_channel_gets_upgrade_required(key_client, channel):
    resp = key_client("free").post("/api/notification-settings",
                                   json={channel: {"enabled": True}})

    assert resp.status_code == 403, resp.text
    assert resp.json()["upgrade_required"] is True


@pytest.mark.parametrize("key", ["display_options", "morning_report_prefs"])
def test_free_tier_cannot_sync_preferences(key_client, key):
    resp = key_client("free").post("/api/preferences/sync", json={"key": key, "value": {}})
    assert resp.status_code == 403, resp.text


def test_member_can_sync_preferences(key_client):
    from unittest.mock import patch

    with patch("carrac.sync.events.publish_preference_event"):
        resp = key_client("member").post("/api/preferences/sync",
                                         json={"key": "display_options", "value": {}})
    assert resp.status_code == 200, resp.text
