"""FastAPI /api/ai/chat endpoint tests.

Covers:
- Pydantic body shape: extra="forbid" rejects unknown fields → 400.
- Auth gate: un-logged-in request → 302 redirect to /login.
- Tier gate: free user (monthly_cap == 0) → 403 + upgrade_required.
- Monthly cap: over-cap → 429.
- Concurrency slot: second request from same user with slot held → 429.
- Insufficient credits: check_and_reserve returns False → 402.
- Empty body / missing message → 400.
- Happy-path SSE: POST, read SSE chunks, verify ctx + done events.
- Mid-stream client disconnect: settle() called once with disconnected=True,
  release_chat_slot called, no double-charge.

Auth is injected via FastAPI's dependency_overrides mechanism so we don't
need Redis or real session cookies — each test injects the exact user dict
it wants the ``require_login`` dep to return.

Patch targets use the source module path (not the router import path) because
the router uses ``from X import Y`` inside function bodies — patching at the
source intercepts all callers.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse_events(text: str) -> list[dict]:
    """Parse SSE text/event-stream body into a list of decoded JSON dicts."""
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


def _inject_user(asgi_app_fixture, user_dict: dict | None):
    """Override the auth_or_key dependency on the FastAPI sub-app.

    Passes an auth dict back from the dep when not None (authenticated).
    Raises 401 when None (unauthenticated) — auth_or_key returns 401 for JSON
    callers (httpx sends Content-Type: application/json so _is_json_client
    is True) rather than the 302 that require_login produces.

    IMPORTANT: override functions must NOT use *args/**kwargs — FastAPI
    inspects the signature and would treat 'args'/'kwargs' as query params,
    causing 400 validation errors.  Use no-arg closures instead.
    """
    from carrac.fastapi_app.deps.auth import auth_or_key

    if user_dict is None:
        # Unauthenticated: replicate auth_or_key behaviour for JSON clients.
        def _unauth():
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Authentication required")
        asgi_app_fixture.dependency_overrides[auth_or_key] = _unauth
    else:
        def _auth():
            return user_dict
        asgi_app_fixture.dependency_overrides[auth_or_key] = _auth


def _clear_overrides(asgi_app_fixture):
    asgi_app_fixture.dependency_overrides.clear()


def _member_user(user_id=42, tier="member"):
    """Return a fake auth dict (session path).

    auth_or_key returns {"source": "session", "user": {...}, "tier": ...}.
    ai.py reads user_id via auth["user"]["id"] for the session path.
    """
    return {
        "source": "session",
        "user": {
            "id": user_id,
            "email": f"user{user_id}@example.com",
            "is_admin": False,
            "tier": tier,
        },
        "tier": tier,
    }


# ---------------------------------------------------------------------------
# Fixture that gives us the fastapi_app with isolated dependency overrides.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_overrides(asgi_app):
    """Clear test-specific FastAPI dependency overrides after every test.

    Preserves the classify_request stub that was installed by the asgi_app
    fixture — that stub is needed for all tests (no SessionMiddleware).
    """
    from carrac.fastapi_app.deps.auth import classify_request

    # Save the classify_request stub before each test runs.
    _classify_stub = asgi_app.dependency_overrides.get(classify_request)
    yield
    # After the test: clear all overrides, then restore classify stub.
    asgi_app.dependency_overrides.clear()
    if _classify_stub is not None:
        asgi_app.dependency_overrides[classify_request] = _classify_stub


@pytest.fixture(autouse=True)
def _enable_ai_chat(monkeypatch):
    """Most tests here exercise chat internals (billing/cap/stream), which only
    run when the in-app chat is enabled. The chat is feature-flagged OFF by
    default now, so force the flag ON for the suite. The dedicated kill-switch
    test patches it back OFF explicitly.
    """
    monkeypatch.setattr("carrac.integrations.is_ai_chat_enabled_flag", lambda: True)


# We need the raw fastapi_app (not the strangler wrapper) to set overrides.
# For tests we build a minimal test app that includes just the ai router
# and shared middleware/exception handlers.
@pytest.fixture
def asgi_app(app):  # noqa: ARG001 — depends on Flask app for env setup
    """Minimal FastAPI test app that includes the ai router.

    Uses the same exception handlers + limiter as the production app so
    Pydantic 422→400 and rate-limit shapes work as expected.

    Also pre-overrides classify_request with a no-arg stub returning a
    fixed member RateContext — the test app has no CarracSessionMiddleware
    so the real classify_request would fail trying to read request.state.session.

    NOTE: override functions must be no-arg closures.  FastAPI inspects
    function signatures and treats *args/**kwargs as query parameters,
    producing 400 validation errors.
    """
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.ai import router as ai_router
    from carrac.fastapi_app.deps.auth import classify_request, RateContext

    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(test_app)
    test_app.include_router(ai_router)

    # Stub classify_request so tests don't need CarracSessionMiddleware.
    def _stub_classify():
        return RateContext(pool="web_session", key="user:test", tier="member")

    test_app.dependency_overrides[classify_request] = _stub_classify
    return test_app


@pytest.fixture
def api_client(asgi_app):
    """Sync httpx client bound to the test FastAPI app."""
    import asyncio
    import httpx
    from httpx import ASGITransport

    async_client = httpx.AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://testserver",
    )

    class _SyncWrapper:
        def __init__(self, ac):
            self._ac = ac
            self._loop = asyncio.new_event_loop()

        def __getattr__(self, name):
            method = getattr(self._ac, name)

            def call(*args, **kwargs):
                return self._loop.run_until_complete(method(*args, **kwargs))

            return call

        def close(self):
            self._loop.run_until_complete(self._ac.aclose())
            self._loop.close()

    wrapper = _SyncWrapper(async_client)
    yield wrapper
    wrapper.close()


# ---------------------------------------------------------------------------
# Auth gate tests
# ---------------------------------------------------------------------------


class TestAuthGate:
    def test_unauthenticated_redirects_to_login(self, api_client, asgi_app):
        """No session → 401 for JSON callers (auth_or_key behaviour)."""
        _inject_user(asgi_app, None)  # unauthenticated
        r = api_client.post("/api/ai/chat", json={"message": "hello"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Pydantic body shape (use api_client with auth injected)
# ---------------------------------------------------------------------------


class TestBodyShape:
    def test_extra_field_rejected(self, api_client, asgi_app):
        """Unknown body field → 400 via extra='forbid'."""
        _inject_user(asgi_app, _member_user())
        with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})), \
             patch("carrac.ai.chat_slot.acquire_chat_slot", return_value=True), \
             patch("carrac.ai.chat_slot.release_chat_slot"):
            r = api_client.post(
                "/api/ai/chat",
                json={"message": "hello", "unknown_field": "bad"},
            )
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"
        assert "error" in r.json()

    def test_missing_message_field_rejected(self, api_client, asgi_app):
        """Missing required 'message' field → 400."""
        _inject_user(asgi_app, _member_user())
        with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})):
            r = api_client.post("/api/ai/chat", json={"feature": "chat"})
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"
        assert "error" in r.json()

    def test_message_too_long_rejected(self, api_client, asgi_app):
        """message > 1000 chars → 400 (Pydantic max_length=1000)."""
        _inject_user(asgi_app, _member_user())
        with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})):
            r = api_client.post(
                "/api/ai/chat",
                json={"message": "x" * 1001},
            )
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# Global daily cap
# ---------------------------------------------------------------------------


class TestGlobalDailyCap:
    def test_global_cap_hit_returns_503(self, api_client, asgi_app):
        """check_global_budget returns (False, ...) → 503 with global_cap: True."""
        _inject_user(asgi_app, _member_user())
        with patch("carrac.ai.billing.check_global_budget",
                   return_value=(False, {"spent_cents": 3000, "cap_cents": 3000})):
            r = api_client.post("/api/ai/chat", json={"message": "hello"})
        assert r.status_code == 503, f"Expected 503, got {r.status_code}: {r.text}"
        body = r.json()
        assert body.get("global_cap") is True


# ---------------------------------------------------------------------------
# Tier gate
# ---------------------------------------------------------------------------


class TestAiChatKillSwitch:
    def test_disabled_returns_403_before_any_work(self, api_client, asgi_app, monkeypatch):
        """ai_chat_enabled flag OFF → 403 for everyone, short-circuiting billing/cap."""
        monkeypatch.setattr("carrac.integrations.is_ai_chat_enabled_flag", lambda: False)
        _inject_user(asgi_app, _member_user())
        # Even if the global budget would allow it, the kill-switch wins first.
        with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})):
            r = api_client.post("/api/ai/chat", json={"message": "hello"})
        assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
        assert r.json().get("ai_chat_disabled") is True

    def test_admin_passes_with_flag_off(self, api_client, asgi_app, monkeypatch):
        """Flag OFF, admin session → the kill-switch does NOT fire.
        The request proceeds to the next gate (global budget), which we make
        fail so the test proves the switch was passed without running a stream."""
        monkeypatch.setattr("carrac.integrations.is_ai_chat_enabled_flag", lambda: False)
        admin = _member_user(user_id=1, tier="admin")
        admin["user"]["is_admin"] = True
        _inject_user(asgi_app, admin)
        with patch("carrac.ai.billing.check_global_budget",
                   return_value=(False, {"spent_cents": 1, "cap_cents": 1})):
            r = api_client.post("/api/ai/chat", json={"message": "hello"})
        assert r.status_code == 503, f"Expected the budget gate (503), got {r.status_code}: {r.text}"
        assert r.json().get("ai_chat_disabled") is None

    def test_ai_chat_allowed_rule(self, monkeypatch):
        from carrac import integrations
        monkeypatch.setattr(integrations, "is_ai_chat_enabled_flag", lambda: False)
        assert integrations.ai_chat_allowed(True) is True
        assert integrations.ai_chat_allowed(False) is False
        assert integrations.ai_chat_allowed(None) is False
        monkeypatch.setattr(integrations, "is_ai_chat_enabled_flag", lambda: True)
        assert integrations.ai_chat_allowed(False) is True

        def _boom():
            raise RuntimeError("posthog down")
        monkeypatch.setattr(integrations, "is_ai_chat_enabled_flag", _boom)
        assert integrations.ai_chat_allowed(False) is False
        assert integrations.ai_chat_allowed(True) is True


class TestTierGate:
    def test_free_user_blocked_with_403(self, api_client, asgi_app):
        """Free user (monthly_cap == 0) → 403 with upgrade_required."""
        _inject_user(asgi_app, _member_user(user_id=99, tier="free"))
        with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})), \
             patch("carrac.auth.decorators.get_tier_limits",
                   return_value={"ai_queries_monthly": 0}), \
             patch("carrac.integrations.on_funnel_event", MagicMock()):
            r = api_client.post("/api/ai/chat", json={"message": "hello"})
        assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
        body = r.json()
        assert body.get("upgrade_required") is True


# ---------------------------------------------------------------------------
# Monthly cap
# ---------------------------------------------------------------------------


class TestMonthlyCap:
    def test_over_monthly_cap_returns_429(self, api_client, asgi_app):
        """User at/over monthly query cap → 429 with limit_type: ai_monthly."""
        _inject_user(asgi_app, _member_user(user_id=77))
        mock_udb = MagicMock()
        mock_udb.get_monthly_ai_query_count.return_value = 10
        with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})), \
             patch("carrac.auth.decorators.get_tier_limits",
                   return_value={"ai_queries_monthly": 10}), \
             patch("user_db.user_db", mock_udb), \
             patch("carrac.integrations.on_funnel_event", MagicMock()):
            r = api_client.post("/api/ai/chat", json={"message": "hello"})
        assert r.status_code == 429, f"Expected 429, got {r.status_code}: {r.text}"
        body = r.json()
        assert body.get("limit_type") == "ai_monthly"


# ---------------------------------------------------------------------------
# Concurrency slot
# ---------------------------------------------------------------------------


class TestConcurrencySlot:
    def test_slot_busy_returns_429(self, api_client, asgi_app):
        """acquire_chat_slot returns False (slot held) → 429."""
        _inject_user(asgi_app, _member_user())
        with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})), \
             patch("carrac.auth.decorators.get_tier_limits",
                   return_value={"ai_queries_monthly": 999}), \
             patch("carrac.ai.chat_slot.acquire_chat_slot", return_value=False):
            r = api_client.post("/api/ai/chat", json={"message": "hello"})
        assert r.status_code == 429, f"Expected 429, got {r.status_code}: {r.text}"
        body = r.json()
        assert "in progress" in body.get("error", "").lower()


# ---------------------------------------------------------------------------
# Insufficient credits
# ---------------------------------------------------------------------------


class TestInsufficientCredits:
    def test_no_credits_returns_402(self, api_client, asgi_app):
        """check_and_reserve returns False → 402 with need_credits."""
        _inject_user(asgi_app, _member_user())
        mock_udb = MagicMock()
        mock_udb.get_monthly_ai_query_count.return_value = 0
        with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})), \
             patch("carrac.auth.decorators.get_tier_limits",
                   return_value={"ai_queries_monthly": 999}), \
             patch("carrac.ai.chat_slot.acquire_chat_slot", return_value=True), \
             patch("carrac.ai.chat_slot.release_chat_slot"), \
             patch("user_db.user_db", mock_udb), \
             patch("carrac.ai.billing.check_and_reserve", return_value=False):
            r = api_client.post("/api/ai/chat", json={"message": "hello"})
        assert r.status_code == 402, f"Expected 402, got {r.status_code}: {r.text}"
        body = r.json()
        assert body.get("need_credits") is True


# ---------------------------------------------------------------------------
# Happy-path SSE
# ---------------------------------------------------------------------------


class TestHappyPathSSE:
    def test_sse_yields_ctx_text_chunks_and_done(self, api_client, asgi_app):
        """Full happy path: ctx event, text chunks, done event with charged+bal."""
        _inject_user(asgi_app, _member_user())

        async def _fake_astream(*args, **kwargs):
            yield ("text", "Hello", None)
            yield ("text", " world", None)
            yield ("usage", None, {"prompt_tokens": 10, "completion_tokens": 5})

        mock_udb = MagicMock()
        mock_udb.get_monthly_ai_query_count.return_value = 0
        mock_udb.get_ai_balance.return_value = 500
        mock_udb.add_message = MagicMock()
        mock_udb.auto_title_conversation = MagicMock()
        mock_udb.get_conversation_meta.return_value = None
        mock_udb.refund_ai_credits = MagicMock()

        with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})), \
             patch("carrac.auth.decorators.get_tier_limits",
                   return_value={"ai_queries_monthly": 999}), \
             patch("carrac.ai.chat_slot.acquire_chat_slot", return_value=True), \
             patch("carrac.ai.chat_slot.release_chat_slot"), \
             patch("carrac.ai.billing.check_and_reserve", return_value=True), \
             patch("user_db.user_db", mock_udb), \
             patch("carrac.ai.context_tiers.assemble_context",
                   return_value=("ctx text", 100, [])), \
             patch("carrac.ai.features.build_system_prompt", return_value="system"), \
             patch("carrac.ai.features.get_max_tokens", return_value=2000), \
             patch("carrac.ai.copilot_tools.should_include_tools", return_value=False), \
             patch("carrac.ai.llm_client.astream_chat_with_tools",
                   side_effect=_fake_astream), \
             patch("carrac.ai.billing.settle", MagicMock(return_value=5)), \
             patch("carrac.grounding.build_facts_dict", return_value={}), \
             patch("carrac.grounding.grounded_system_prompt",
                   side_effect=lambda sp, **kw: sp), \
             patch("carrac.grounding.facts_block", return_value=""):
            r = api_client.post("/api/ai/chat", json={"message": "Hello AI"})

        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        ct = r.headers.get("content-type", "")
        assert "text/event-stream" in ct, f"Expected SSE content-type, got: {ct}"

        events = _sse_events(r.text)

        ctx_events = [e for e in events if "ctx" in e]
        assert len(ctx_events) >= 1, "Expected at least one ctx event"

        text_events = [e for e in events if "t" in e]
        assert len(text_events) == 2
        assert text_events[0]["t"] == "Hello"
        assert text_events[1]["t"] == " world"

        done_events = [e for e in events if e.get("done") is True]
        assert len(done_events) == 1
        done = done_events[0]
        assert done.get("bal") == 500
        assert done.get("charged") == 5

    def test_sse_response_headers(self, api_client, asgi_app):
        """SSE response must include Cache-Control: no-cache and X-Accel-Buffering: no."""
        _inject_user(asgi_app, _member_user())

        async def _fake_astream(*args, **kwargs):
            yield ("usage", None, {"prompt_tokens": 1, "completion_tokens": 1})

        mock_udb = MagicMock()
        mock_udb.get_monthly_ai_query_count.return_value = 0
        mock_udb.get_ai_balance.return_value = 100
        mock_udb.add_message = MagicMock()
        mock_udb.auto_title_conversation = MagicMock()
        mock_udb.get_conversation_meta.return_value = None
        mock_udb.refund_ai_credits = MagicMock()

        with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})), \
             patch("carrac.auth.decorators.get_tier_limits",
                   return_value={"ai_queries_monthly": 999}), \
             patch("carrac.ai.chat_slot.acquire_chat_slot", return_value=True), \
             patch("carrac.ai.chat_slot.release_chat_slot"), \
             patch("carrac.ai.billing.check_and_reserve", return_value=True), \
             patch("user_db.user_db", mock_udb), \
             patch("carrac.ai.context_tiers.assemble_context",
                   return_value=("ctx text", 50, [])), \
             patch("carrac.ai.features.build_system_prompt", return_value="system"), \
             patch("carrac.ai.features.get_max_tokens", return_value=2000), \
             patch("carrac.ai.copilot_tools.should_include_tools", return_value=False), \
             patch("carrac.ai.llm_client.astream_chat_with_tools",
                   side_effect=_fake_astream), \
             patch("carrac.ai.billing.settle", MagicMock(return_value=1)), \
             patch("carrac.grounding.build_facts_dict", return_value={}), \
             patch("carrac.grounding.grounded_system_prompt",
                   side_effect=lambda sp, **kw: sp), \
             patch("carrac.grounding.facts_block", return_value=""):
            r = api_client.post("/api/ai/chat", json={"message": "hi"})

        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache"
        assert r.headers.get("x-accel-buffering") == "no"


# ---------------------------------------------------------------------------
# Mid-stream client disconnect
# ---------------------------------------------------------------------------


class TestMidStreamDisconnect:
    def test_disconnect_triggers_settle_with_disconnected_flag(self, api_client, asgi_app):
        """On CancelledError (client disconnect):
        - settle(disconnected=True) called exactly once
        - release_chat_slot called in finally block
        - No double-charge (settle called at most once)
        """
        _inject_user(asgi_app, _member_user())

        settle_mock = MagicMock(return_value=5)
        release_mock = MagicMock()

        async def _fake_astream(*args, **kwargs):
            yield ("text", "partial response", None)
            raise asyncio.CancelledError()

        mock_udb = MagicMock()
        mock_udb.get_monthly_ai_query_count.return_value = 0
        mock_udb.get_ai_balance.return_value = 500
        mock_udb.add_message = MagicMock()
        mock_udb.auto_title_conversation = MagicMock()
        mock_udb.get_conversation_meta.return_value = None
        mock_udb.refund_ai_credits = MagicMock()

        try:
            with patch("carrac.ai.billing.check_global_budget", return_value=(True, {})), \
                 patch("carrac.auth.decorators.get_tier_limits",
                       return_value={"ai_queries_monthly": 999}), \
                 patch("carrac.ai.chat_slot.acquire_chat_slot", return_value=True), \
                 patch("carrac.ai.chat_slot.release_chat_slot", release_mock), \
                 patch("carrac.ai.billing.check_and_reserve", return_value=True), \
                 patch("user_db.user_db", mock_udb), \
                 patch("carrac.ai.context_tiers.assemble_context",
                       return_value=("ctx text", 100, [])), \
                 patch("carrac.ai.features.build_system_prompt", return_value="system"), \
                 patch("carrac.ai.features.get_max_tokens", return_value=2000), \
                 patch("carrac.ai.copilot_tools.should_include_tools", return_value=False), \
                 patch("carrac.ai.llm_client.astream_chat_with_tools",
                       side_effect=_fake_astream), \
                 patch("carrac.ai.billing.settle", settle_mock), \
                 patch("carrac.grounding.build_facts_dict", return_value={}), \
                 patch("carrac.grounding.grounded_system_prompt",
                       side_effect=lambda sp, **kw: sp), \
                 patch("carrac.grounding.facts_block", return_value=""):
                api_client.post("/api/ai/chat", json={"message": "Hello AI"})
        except Exception:
            pass  # CancelledError propagation is acceptable

        # settle(disconnected=True) MUST have been called at least once
        disconnect_settles = [
            c for c in settle_mock.call_args_list
            if c.kwargs.get("disconnected") is True
        ]
        assert len(disconnect_settles) >= 1, (
            f"settle(disconnected=True) expected. All calls: {settle_mock.call_args_list}"
        )

        # No double-charge: settle called at most once total
        assert settle_mock.call_count <= 1, (
            f"settle() called {settle_mock.call_count} times — expected 1"
        )

        # release_chat_slot must be called in finally
        assert release_mock.call_count >= 1, "release_chat_slot not called in finally"
