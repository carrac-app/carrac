"""Async offload tests for watchlist.py and preferences.py.

``async def`` FastAPI handlers that make blocking synchronous ``user_db``
calls stall the uvicorn event loop for ALL concurrent requests. This suite
proves each handler's DB access now runs on a worker thread
(``asyncio.to_thread``), not on the event-loop thread.

Recipe: the ``asgi_client`` fixture (tests/fastapi/conftest.py) runs the
event loop on the TEST's own thread, so ``threading.get_ident()`` called
from inside the test IS the event-loop thread ident. We monkeypatch the
narrowest ``UserDatabase`` method(s) each handler calls to record
``threading.get_ident()`` before delegating to a canned return value, then
assert: (1) the response is OK, and (2) the recorded ident differs from the
test/event-loop thread's ident — proving the blocking call ran off-loop.

Auth: ``member_key`` fixture (tests/fastapi/conftest.py) — a real per-user
API key in the test DB. ``X-API-Key`` resolves via ``auth_or_key`` to the
``api_key`` source branch and bypasses CSRF (matches production API
clients), same pattern used by the plays/trades share-endpoint suites.
"""

from __future__ import annotations

import os
import threading

from user_db import UserDatabase


def _patch_recording(monkeypatch, method_name, return_value=None):
    """Patch ``UserDatabase.<method_name>`` to record the calling thread.

    Returns a dict that gains an ``"ident"`` key (the calling thread's
    ``threading.get_ident()``) the first time the patched method runs.
    """
    recorded: dict[str, int] = {}

    def _wrapper(self, *args, **kwargs):  # noqa: ARG001 — self required for class-level patch
        recorded["ident"] = threading.get_ident()
        return return_value

    monkeypatch.setattr(UserDatabase, method_name, _wrapper)
    return recorded


def _assert_offloaded(recorded: dict, method_name: str) -> None:
    assert "ident" in recorded, f"{method_name} was never called"
    assert recorded["ident"] != threading.get_ident(), (
        f"{method_name} ran on the event-loop thread — blocking call not offloaded"
    )


# ---------------------------------------------------------------------------
# watchlist.py — 5 handlers
# ---------------------------------------------------------------------------


def test_save_watchlist_list_offloaded(asgi_client, member_key, monkeypatch):
    """POST /api/watchlist (flat list body) → save_flat_watchlist offloaded."""
    headers, _uid = member_key
    recorded = _patch_recording(monkeypatch, "save_flat_watchlist", return_value=True)

    resp = asgi_client.post("/api/watchlist", json=["BTC", "ETH"], headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}
    _assert_offloaded(recorded, "save_flat_watchlist")


def test_add_to_watchlist_offloaded(asgi_client, member_key, monkeypatch):
    """POST /api/watchlist/add → add_to_watchlist + get_flat_watchlist closure offloaded."""
    headers, _uid = member_key
    add_recorded = _patch_recording(monkeypatch, "add_to_watchlist", return_value=True)
    _patch_recording(monkeypatch, "get_flat_watchlist", return_value=["BTC"])

    resp = asgi_client.post("/api/watchlist/add", json={"symbol": "BTC"}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    _assert_offloaded(add_recorded, "add_to_watchlist")


def test_remove_from_watchlist_offloaded(asgi_client, member_key, monkeypatch):
    """POST /api/watchlist/remove → remove_from_watchlist + get_flat_watchlist closure offloaded."""
    headers, _uid = member_key
    remove_recorded = _patch_recording(monkeypatch, "remove_from_watchlist", return_value=True)
    _patch_recording(monkeypatch, "get_flat_watchlist", return_value=[])

    resp = asgi_client.post("/api/watchlist/remove", json={"symbol": "BTC"}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    _assert_offloaded(remove_recorded, "remove_from_watchlist")


def test_reorder_watchlist_offloaded(asgi_client, member_key, monkeypatch):
    """POST /api/watchlist/reorder (raw JSON array body) → save_flat_watchlist offloaded."""
    headers, _uid = member_key
    recorded = _patch_recording(monkeypatch, "save_flat_watchlist", return_value=True)

    resp = asgi_client.post("/api/watchlist/reorder", json=["ETH", "BTC"], headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}
    _assert_offloaded(recorded, "save_flat_watchlist")


def test_save_watchlist_groups_offloaded(asgi_client, member_key, monkeypatch):
    """POST /api/watchlist/groups → save_watchlist_groups offloaded."""
    headers, _uid = member_key
    recorded = _patch_recording(monkeypatch, "save_watchlist_groups", return_value=True)

    body = {"groups": [{"id": "g1", "name": "Group 1", "symbols": ["BTC"]}]}
    resp = asgi_client.post("/api/watchlist/groups", json=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}
    _assert_offloaded(recorded, "save_watchlist_groups")


# ---------------------------------------------------------------------------
# preferences.py — 3 handlers (get_preferences + save_preferences closure shape)
# ---------------------------------------------------------------------------


def test_indicator_prefs_offloaded(asgi_client, member_key, monkeypatch):
    """POST /api/indicator-prefs → get_preferences+save_preferences closure offloaded."""
    headers, _uid = member_key
    get_recorded = _patch_recording(monkeypatch, "get_preferences", return_value={})
    save_recorded = _patch_recording(monkeypatch, "save_preferences", return_value=True)

    resp = asgi_client.post(
        "/api/indicator-prefs", json={"indicators": {"RSI": True}}, headers=headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}
    _assert_offloaded(get_recorded, "get_preferences")
    _assert_offloaded(save_recorded, "save_preferences")


def test_preferences_sync_offloaded(asgi_client, member_key, monkeypatch):
    """POST /api/preferences/sync → get_preferences+save_preferences closure offloaded."""
    headers, _uid = member_key
    get_recorded = _patch_recording(monkeypatch, "get_preferences", return_value={})
    save_recorded = _patch_recording(monkeypatch, "save_preferences", return_value=True)

    resp = asgi_client.post(
        "/api/preferences/sync",
        json={"key": "display_options", "value": {"greenRedCandles": True}},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}
    _assert_offloaded(get_recorded, "get_preferences")
    _assert_offloaded(save_recorded, "save_preferences")


def test_settings_save_offloaded(asgi_client, member_key, monkeypatch):
    """POST /api/settings/save → get_preferences+save_preferences closure offloaded."""
    headers, _uid = member_key
    get_recorded = _patch_recording(monkeypatch, "get_preferences", return_value={})
    save_recorded = _patch_recording(monkeypatch, "save_preferences", return_value=True)

    resp = asgi_client.post(
        "/api/settings/save",
        json={"key": "chart_prefs", "value": {"lastViewedAsset": "BTC"}},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}
    _assert_offloaded(get_recorded, "get_preferences")
    _assert_offloaded(save_recorded, "save_preferences")


# ---------------------------------------------------------------------------
# preferences.py — bonus: shared-admin-key resolver DB lookup offloaded
# ---------------------------------------------------------------------------


def test_indicator_prefs_admin_resolver_offloaded(asgi_client, monkeypatch):
    """The shared-admin-key branch of ``_resolve_user_id_or_admin`` calls
    ``user_db.get_user_by_email`` directly in the handler body (via
    ``asyncio.to_thread``) — must also run off the event-loop thread.

    ``SCREENSHOT_API_KEY`` (set to 'test-api-key-for-testing' in
    tests/conftest.py) is the shared admin key — ``auth_or_key`` resolves
    it to ``source="api_key"`` with ``user_id=None`` (no per-user DB row),
    forcing ``_resolve_user_id_or_admin`` down the ADMIN_EMAIL lookup path.
    """
    monkeypatch.setenv("ADMIN_EMAIL", "admin@test.dev")
    email_recorded = _patch_recording(monkeypatch, "get_user_by_email", return_value={"id": 7})
    _patch_recording(monkeypatch, "get_preferences", return_value={})
    _patch_recording(monkeypatch, "save_preferences", return_value=True)

    headers = {"X-Internal-Key": os.environ["SCREENSHOT_API_KEY"]}

    resp = asgi_client.post("/api/indicator-prefs", json={}, headers=headers)

    assert resp.status_code == 200, resp.text
    _assert_offloaded(email_recorded, "get_user_by_email")
