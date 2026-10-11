"""``/api/indicator-prefs`` resolves an API-key caller to the right account:
a per-user key to its own owner, and only the shared internal key (which
carries no user) to the admin account the morning engine writes as."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def admin_id(api_user, monkeypatch):
    from user_db import user_db

    _headers, uid = api_user("admin")
    monkeypatch.setenv("ADMIN_EMAIL", user_db.get_user_by_id(uid)["email"])
    return uid


def _indicator_prefs(user_id):
    from user_db import user_db
    return user_db.get_preferences(user_id).get("indicator_prefs", {})


def test_per_user_key_writes_its_own_prefs_not_the_admins(key_client, admin_id):
    client = key_client("free")
    with patch("carrac.sync.events.publish_preference_event"):
        resp = client.post("/api/indicator-prefs", json={"indicators": {"RSI": "poisoned"}})
    assert resp.status_code == 200, resp.text

    assert _indicator_prefs(client.user_id)["indicators"] == {"RSI": "poisoned"}
    assert "poisoned" not in str(_indicator_prefs(admin_id))


def test_per_user_key_reads_its_own_prefs(key_client, admin_id):
    client = key_client("free")
    with patch("carrac.sync.events.publish_preference_event"):
        client.post("/api/indicator-prefs", json={"indicators": {"EMA": True}})

    resp = client.get("/api/indicator-prefs")
    assert resp.status_code == 200, resp.text
    assert resp.json()["indicators"] == {"EMA": True}


def test_shared_internal_key_resolves_to_the_admin(asgi_client, admin_id):
    headers = {"X-Internal-Key": os.environ["SCREENSHOT_API_KEY"]}
    with patch("carrac.sync.events.publish_preference_event"):
        resp = asgi_client.post("/api/indicator-prefs", headers=headers,
                                json={"indicators": {"CPR": True}})
    assert resp.status_code == 200, resp.text
    assert _indicator_prefs(admin_id)["indicators"] == {"CPR": True}

    resp = asgi_client.get("/api/indicator-prefs", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["indicators"] == {"CPR": True}
