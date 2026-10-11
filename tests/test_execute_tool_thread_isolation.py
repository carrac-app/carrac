"""execute_tool cross-user isolation under thread reuse.

Guards against flask.g thread-local leakage:
  - Pushing a Flask app context onto a thread-pool thread without popping it
    leaks state, because anyio/asyncio reuses threads: g.api_key_user_id
    set for User A would remain readable when User B's request landed on the
    same thread.
  - execute_tool therefore uses set_mcp_caller / reset_mcp_caller
    (ContextVar-based).  asyncio.to_thread propagates a *copy* of the
    calling context into the thread; mutations in the thread do not escape
    back and are discarded when the thread is returned to the pool.

These tests verify:
  1. execute_tool sets the MCP caller contextvar for the duration of the
     call, and the tool handler sees the correct user_id.
  2. Two simulated requests running on the same thread in sequence each
     see their own user_id — no stale data from the previous call.
  3. The contextvar is reset after execute_tool returns (defence-in-depth).
  4. The runtime getters return None / 'free' when no caller is set
     (no flask.g fallback).
"""

from __future__ import annotations

import asyncio



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seen_user_id_during_execute(tool_name: str, user_id: int, tier: str):
    """Call execute_tool and capture what get_mcp_caller_user_id returns
    inside the tool handler.  We monkeypatch call_tool to intercept."""
    from carrac.mcp.runtime import get_mcp_caller_user_id, get_mcp_caller_tier

    seen = {}

    def _fake_call_tool(name, arguments):
        seen["user_id"] = get_mcp_caller_user_id()
        seen["tier"] = get_mcp_caller_tier()
        return {"ok": True}

    import carrac.mcp.registry as _reg
    orig = _reg.call_tool

    def _patched_call_tool(name, arguments):
        return _fake_call_tool(name, arguments)

    _reg.call_tool = _patched_call_tool
    try:
        from carrac.ai.copilot_tools import execute_tool
        execute_tool(tool_name, {}, user_id=user_id, user_tier=tier)
    finally:
        _reg.call_tool = orig

    return seen


# ---------------------------------------------------------------------------
# Unit tests — synchronous (no asyncio.to_thread needed)
# ---------------------------------------------------------------------------


class TestExecuteToolContextVar:
    """execute_tool sets and clears the MCP caller contextvar correctly."""

    def test_tool_sees_correct_user_id(self, monkeypatch):
        """Tool handler reads the exact user_id passed to execute_tool."""
        from carrac.mcp.registry import TOOL_REGISTRY
        from carrac.mcp.runtime import get_mcp_caller_user_id, get_mcp_caller_tier


        # Register a minimal test tool so execute_tool can find it.
        TOOL_REGISTRY["_test_isolation"] = {
            "name": "_test_isolation",
            "description": "test",
            "inputSchema": {"type": "object", "properties": {}},
            "min_tier": "free",
            "handler": lambda args: {
                "user_id": get_mcp_caller_user_id(),
                "tier": get_mcp_caller_tier(),
            },
        }
        try:
            from carrac.ai.copilot_tools import execute_tool
            import json
            result = json.loads(execute_tool("_test_isolation", {}, user_id=42, user_tier="premium"))
            assert result["user_id"] == 42
            assert result["tier"] == "premium"
        finally:
            TOOL_REGISTRY.pop("_test_isolation", None)

    def test_contextvar_reset_after_execute_tool(self):
        """After execute_tool returns the contextvar is cleared (reset_mcp_caller)."""
        from carrac.mcp.runtime import (
            get_mcp_caller_user_id,
            get_mcp_caller_tier,
            reset_mcp_caller,
        )
        from carrac.mcp.registry import TOOL_REGISTRY

        reset_mcp_caller()  # start clean

        TOOL_REGISTRY["_test_reset"] = {
            "name": "_test_reset",
            "description": "test",
            "inputSchema": {"type": "object", "properties": {}},
            "min_tier": "free",
            "handler": lambda args: {},
        }
        try:
            from carrac.ai.copilot_tools import execute_tool
            execute_tool("_test_reset", {}, user_id=99, user_tier="member")
        finally:
            TOOL_REGISTRY.pop("_test_reset", None)

        # After the call, the contextvar must be None (reset_mcp_caller was called).
        assert get_mcp_caller_user_id() is None
        assert get_mcp_caller_tier() == "free"  # default when unset

    def test_contextvar_reset_even_on_tool_exception(self):
        """reset_mcp_caller runs in finally even if the tool raises."""
        from carrac.mcp.runtime import get_mcp_caller_user_id, reset_mcp_caller
        from carrac.mcp.registry import TOOL_REGISTRY

        reset_mcp_caller()

        TOOL_REGISTRY["_test_exc"] = {
            "name": "_test_exc",
            "description": "test",
            "inputSchema": {"type": "object", "properties": {}},
            "min_tier": "free",
            "handler": lambda args: (_ for _ in ()).throw(RuntimeError("oops")),
        }
        try:
            from carrac.ai.copilot_tools import execute_tool
            import json
            result = json.loads(execute_tool("_test_exc", {}, user_id=77, user_tier="member"))
            # execute_tool returns {"error": "oops"} — doesn't re-raise.
            assert "error" in result
        finally:
            TOOL_REGISTRY.pop("_test_exc", None)

        # Contextvar must still be cleared in the finally block.
        assert get_mcp_caller_user_id() is None


# ---------------------------------------------------------------------------
# Cross-user isolation under thread reuse
# ---------------------------------------------------------------------------


class TestCrossUserIsolation:
    """Prove that User B on a reused thread cannot see User A's identity.

    This is the core test for flask.g thread-local isolation.
    Strategy: run two execute_tool calls sequentially on the *same* thread
    (not via asyncio.to_thread — we're testing the ContextVar isolation
    directly) and assert each sees only its own user_id.
    """

    def test_sequential_calls_on_same_thread_are_isolated(self):
        """Two sequential execute_tool calls on the same thread see their own user_ids."""
        from carrac.mcp.runtime import get_mcp_caller_user_id, get_mcp_caller_tier, reset_mcp_caller
        from carrac.mcp.registry import TOOL_REGISTRY
        from carrac.ai.copilot_tools import execute_tool

        reset_mcp_caller()
        seen_calls = []

        def _handler(args):
            seen_calls.append({
                "user_id": get_mcp_caller_user_id(),
                "tier": get_mcp_caller_tier(),
            })
            return {}

        TOOL_REGISTRY["_test_seq"] = {
            "name": "_test_seq",
            "description": "test",
            "inputSchema": {"type": "object", "properties": {}},
            "min_tier": "free",
            "handler": _handler,
        }
        try:
            # Simulate User A's request
            execute_tool("_test_seq", {}, user_id=100, user_tier="premium")
            # Simulate User B's request — same thread, same function, different user
            execute_tool("_test_seq", {}, user_id=200, user_tier="member")
        finally:
            TOOL_REGISTRY.pop("_test_seq", None)

        assert len(seen_calls) == 2
        assert seen_calls[0]["user_id"] == 100, "User A should see user_id=100"
        assert seen_calls[0]["tier"] == "premium"
        assert seen_calls[1]["user_id"] == 200, "User B should see user_id=200, not User A's 100"
        assert seen_calls[1]["tier"] == "member"

    def test_asyncio_to_thread_isolation(self):
        """Two asyncio.to_thread calls see their own user context, not each other's.

        This directly tests the production path in routers/ai.py which calls
        asyncio.to_thread(execute_tool, tool_name, tool_args, user_id, ...).
        asyncio.to_thread uses copy_context() so each thread invocation starts
        with an independent copy of the ContextVar state — mutations in one
        thread cannot bleed into another.
        """
        from carrac.mcp.runtime import get_mcp_caller_user_id, reset_mcp_caller
        from carrac.mcp.registry import TOOL_REGISTRY
        from carrac.ai.copilot_tools import execute_tool

        reset_mcp_caller()
        results = {}

        def _handler_a(args):
            results["a"] = get_mcp_caller_user_id()
            return {}

        def _handler_b(args):
            results["b"] = get_mcp_caller_user_id()
            return {}

        TOOL_REGISTRY["_test_thread_a"] = {
            "name": "_test_thread_a",
            "description": "test",
            "inputSchema": {"type": "object", "properties": {}},
            "min_tier": "free",
            "handler": _handler_a,
        }
        TOOL_REGISTRY["_test_thread_b"] = {
            "name": "_test_thread_b",
            "description": "test",
            "inputSchema": {"type": "object", "properties": {}},
            "min_tier": "free",
            "handler": _handler_b,
        }
        try:
            async def _run():
                await asyncio.gather(
                    asyncio.to_thread(execute_tool, "_test_thread_a", {}, 301, "premium"),
                    asyncio.to_thread(execute_tool, "_test_thread_b", {}, 302, "member"),
                )

            asyncio.run(_run())
        finally:
            TOOL_REGISTRY.pop("_test_thread_a", None)
            TOOL_REGISTRY.pop("_test_thread_b", None)

        assert results.get("a") == 301, f"Thread A should see user 301, got {results.get('a')}"
        assert results.get("b") == 302, f"Thread B should see user 302, got {results.get('b')}"


# ---------------------------------------------------------------------------
# Runtime getters without flask.g fallback
# ---------------------------------------------------------------------------


class TestRuntimeNoFlaskGFallback:
    """get_mcp_caller_user_id / get_mcp_caller_tier read only the contextvar.

    The flask.g fallback was removed in the security fix.  These tests
    verify that behaviour outside a Flask request context is correct.
    """

    def test_user_id_is_none_when_unset(self):
        from carrac.mcp.runtime import get_mcp_caller_user_id, reset_mcp_caller
        reset_mcp_caller()
        assert get_mcp_caller_user_id() is None

    def test_tier_defaults_to_free_when_unset(self):
        from carrac.mcp.runtime import get_mcp_caller_tier, reset_mcp_caller
        reset_mcp_caller()
        assert get_mcp_caller_tier() == "free"

    def test_set_caller_then_reset_returns_defaults(self):
        from carrac.mcp.runtime import (
            get_mcp_caller_user_id,
            get_mcp_caller_tier,
            set_mcp_caller,
            reset_mcp_caller,
        )
        set_mcp_caller(55, "premium")
        assert get_mcp_caller_user_id() == 55
        assert get_mcp_caller_tier() == "premium"
        reset_mcp_caller()
        assert get_mcp_caller_user_id() is None
        assert get_mcp_caller_tier() == "free"
