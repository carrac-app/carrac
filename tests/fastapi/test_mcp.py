"""FastAPI MCP router tests.

The Flask blueprint at carrac/mcp/__init__.py stays for the
parity period, so the existing tests/test_mcp.py (Flask test_client) still
exercises the dispatcher and tool surfaces. These tests cover the
strangler-routed FastAPI handler reached through ``asgi_client``:

* dispatch entry in ``MIGRATED_WRITE_PATHS``
* auth gate parity (no key → 401, invalid key → 401, valid key → 200)
* parse-error / notification / unknown-method JSON-RPC envelopes
* contextvar plumbing (set/get caller in the runtime helper)
* MCPRequest Pydantic envelope rejects unknown top-level fields

Tier-gate / tool-call coverage stays in the Flask suite — same dispatcher
runs in both. Tool execution is the highest-leverage thing to NOT
duplicate; we only assert that the handler reaches it.
"""

from __future__ import annotations

import os

import pytest

from carrac.asgi import MIGRATED_WRITE_PATHS, ExactPath


API_KEY = os.environ.get("SCREENSHOT_API_KEY", "test-api-key-for-testing")


def _jsonrpc(method: str, params=None, req_id: int = 1) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        body["params"] = params
    return body


# ---------------------------------------------------------------------------
# Strangler-dispatch unit tests
# ---------------------------------------------------------------------------


def test_mcp_in_migrated_write_paths():
    assert ("/mcp", frozenset({"POST"})) in MIGRATED_WRITE_PATHS


def test_mcp_exact_path_matches_post_only():
    ep = ExactPath("/mcp", app=None, methods=frozenset({"POST"}))
    base = {"type": "http", "path": "/mcp"}
    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})
    assert post_match.value > 0
    assert get_match.value == 0


# ---------------------------------------------------------------------------
# Pydantic envelope
# ---------------------------------------------------------------------------


def test_mcp_request_rejects_extra_top_level_field():
    import pydantic

    from carrac.fastapi_app.models.mcp import MCPRequest

    with pytest.raises(pydantic.ValidationError) as exc_info:
        MCPRequest(jsonrpc="2.0", method="initialize", id=1, surprise=42)
    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" for e in errors)


def test_mcp_request_accepts_minimal_call():
    from carrac.fastapi_app.models.mcp import MCPRequest

    req = MCPRequest(method="tools/list", id=2)
    assert req.method == "tools/list"
    assert req.jsonrpc == "2.0"


# ---------------------------------------------------------------------------
# Runtime helper unit tests
# ---------------------------------------------------------------------------


def test_runtime_set_and_get_caller():
    from carrac.mcp.runtime import (
        get_mcp_caller_tier,
        get_mcp_caller_user_id,
        reset_mcp_caller,
        set_mcp_caller,
    )

    set_mcp_caller(42, "premium")
    try:
        assert get_mcp_caller_user_id() == 42
        assert get_mcp_caller_tier() == "premium"
    finally:
        reset_mcp_caller()


def test_runtime_default_tier_is_free_when_unset():
    from carrac.mcp.runtime import get_mcp_caller_tier, reset_mcp_caller

    reset_mcp_caller()
    # Outside a request context the helper defaults to least-privilege 'free'
    # (hardened from 'admin' in the pre-beta audit — no privilege-by-default).
    assert get_mcp_caller_tier() == "free"


# ---------------------------------------------------------------------------
# HTTP integration via asgi_client (Flask blueprint still serves the
# fallback — the strangler routes /mcp to FastAPI per MIGRATED_WRITE_PATHS).
# ---------------------------------------------------------------------------


def test_mcp_no_auth_returns_401(asgi_client):
    resp = asgi_client.post("/mcp", json=_jsonrpc("initialize"))
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"]["code"] == -32000
    assert "Authentication required" in data["error"]["message"]


def test_mcp_invalid_key_returns_401(asgi_client):
    resp = asgi_client.post(
        "/mcp",
        json=_jsonrpc("initialize"),
        headers={"X-API-Key": "definitely-not-a-real-key"},
    )
    assert resp.status_code == 401


def test_mcp_initialize_with_valid_admin_key(asgi_client, monkeypatch):
    """The shared admin key path — verify the handler reaches dispatch
    and returns the JSON-RPC envelope shape."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-admin-key")
    resp = asgi_client.post(
        "/mcp",
        json=_jsonrpc("initialize"),
        headers={"X-Internal-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 1
    assert "result" in data
    assert data["result"]["serverInfo"]["name"] == "carrac"


def test_mcp_bearer_token_auth(asgi_client, monkeypatch):
    """``Authorization: Bearer`` resolves the same as the public
    X-API-Key header (``from_internal=False`` — see ``_extract_api_key``), so
    the shared admin key sent this way is now rejected, not accepted."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "bearer-test-key")
    resp = asgi_client.post(
        "/mcp",
        json=_jsonrpc("initialize"),
        headers={"Authorization": "Bearer bearer-test-key"},
    )
    assert resp.status_code == 401


def test_mcp_invalid_json_returns_jsonrpc_parse_error(asgi_client, monkeypatch):
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-admin-key")
    resp = asgi_client.post(
        "/mcp",
        content=b"not json",
        headers={
            "X-Internal-Key": "test-admin-key",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["code"] == -32700


def test_mcp_notification_returns_204(asgi_client, monkeypatch):
    """Notifications (no `id`) return 204 with no body."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-admin-key")
    body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    resp = asgi_client.post(
        "/mcp",
        json=body,
        headers={"X-Internal-Key": "test-admin-key"},
    )
    assert resp.status_code == 204


def test_mcp_unknown_method_returns_method_not_found(asgi_client, monkeypatch):
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-admin-key")
    resp = asgi_client.post(
        "/mcp",
        json=_jsonrpc("totally/unknown"),
        headers={"X-Internal-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"]["code"] == -32601


def test_mcp_tools_list_returns_registered_tools(asgi_client, monkeypatch):
    """tools/list reaches the registry — same set of 75 tools the Flask
    suite asserts on."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-admin-key")
    resp = asgi_client.post(
        "/mcp",
        json=_jsonrpc("tools/list"),
        headers={"X-Internal-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    tools = data["result"]["tools"]
    assert len(tools) > 50  # 75 expected; loose bound for resilience
    names = {t["name"] for t in tools}
    # Pick a few well-known tools to verify dispatch reached the registry:
    assert "market_get_sentiment" in names
    assert "market_search_symbol" in names


# ── Wave-2 ports of skip-marked Flask tests (post-migration cleanup) ─────


def test_mcp_shared_key_reaches_non_user_scoped_tool(asgi_client, monkeypatch):
    """Shared admin key resolves to (None, 'admin'), not (1, 'admin').
    A non-user-scoped tool (scanner_get_transitions) must still dispatch successfully.
    The shared key must not default to user_id=1 (that would leak admin
    data to any caller of a user-scoped tool) — that path ToolErrors,
    but non-user-scoped tools must keep working."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-admin-key")
    monkeypatch.setattr(
        "carrac.indicator_state.get_recent_transitions",
        lambda limit, symbol, min_significance: [],
    )

    resp = asgi_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "scanner_get_transitions", "arguments": {}},
        },
        headers={"X-Internal-Key": "test-admin-key"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" not in data, f"shared key dispatch errored: {data}"
    # Tools/call wraps the tool result in content[0].text JSON.
    text = data["result"]["content"][0]["text"]
    import json as _json
    payload = _json.loads(text)
    assert payload == {"transitions": [], "count": 0}


def test_mcp_429_after_member_burst(asgi_client, monkeypatch):
    """Member tier: 61st request in a minute → 429 + JSON-RPC -32001.
    Patches resolve_api_key (imported lazily inside the handler) to inject
    a synthetic member-tier user, and forces the memory rate-limit backend
    so the test is hermetic against any locally-running Redis."""
    monkeypatch.setattr(
        "carrac.auth.decorators.resolve_api_key",
        lambda key, allow_shared=False: (True, 999, "member", None, False),
    )
    # Force memory backend so the test doesn't share state with Redis
    # (get_redis is imported lazily inside _run_windows).
    monkeypatch.setattr("carrac.redis_cache.get_redis", lambda: None)

    from carrac.rate_limit import _mem_buckets
    _mem_buckets.clear()

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }
    headers = {"X-API-Key": "cvx_k_" + "a" * 32}

    for i in range(60):
        resp = asgi_client.post("/mcp", json=body, headers=headers)
        assert resp.status_code == 200, f"request {i + 1} failed: {resp.text}"

    resp = asgi_client.post("/mcp", json=body, headers=headers)
    assert resp.status_code == 429
    data = resp.json()
    assert data["error"]["code"] == -32001
    assert "Retry-After" in resp.headers

    _mem_buckets.clear()


def test_mcp_no_429_for_internal_key_despite_member_tier(asgi_client, monkeypatch):
    """CARRAC internal-key flag: an is_internal key on member tier must survive
    well past the 60/min member budget without a 429 — same control shape as
    test_mcp_429_after_member_burst, but proves the flag (not tier) exempts it."""
    monkeypatch.setattr(
        "carrac.auth.decorators.resolve_api_key",
        lambda key, allow_shared=False: (True, 998, "member", None, True),
    )
    monkeypatch.setattr("carrac.redis_cache.get_redis", lambda: None)

    from carrac.rate_limit import _mem_buckets
    _mem_buckets.clear()

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"X-API-Key": "cvx_k_" + "e" * 32}

    for i in range(200):
        resp = asgi_client.post("/mcp", json=body, headers=headers)
        assert resp.status_code == 200, f"request {i + 1} unexpectedly failed: {resp.text}"

    _mem_buckets.clear()


def test_resolve_api_key_decorators_returns_five_tuple(app, monkeypatch):
    """CARRAC internal-key flag: the FastAPI-facing resolver must return a
    5-tuple (ok, user_id, tier, scope, is_internal) — 3 real call sites
    (mcp.py, chart_internal.py, facts.py) unpack it positionally."""
    import hashlib
    from carrac.auth.decorators import resolve_api_key
    from user_db import user_db

    with app.app_context():
        uid = user_db.create_user(
            "decorators-internal@example.com", "StrongPass!123", tos_accepted=True
        )
        raw = "cvx_k_" + ("c" * 32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        assert user_db.create_api_key(
            uid, token_hash, token_suffix=raw[-4:], name="agent-test2", is_internal=True
        )
        ok, ret_uid, tier, scope, is_internal = resolve_api_key(raw)
        assert ok is True
        assert ret_uid == uid
        assert is_internal is True
