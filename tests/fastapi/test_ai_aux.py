"""Tests for ai_aux (weekly-review SSE).

Tests cover:

Dispatcher shim unit tests (no network, no app context):
- ExactPath matching logic for ``POST /api/ai/weekly-review``.
- MIGRATED_WRITE_PATHS entry.

Pydantic model tests:
- ``WeeklyReviewRequest`` with ``extra="forbid"``.

Integration tests via ``asgi_client`` (login is injected with ``login_as``):
- Tier gate (non-Premium user → 403).
- Global daily budget cap → 503.
- No closed trades this week → 400.
- Insufficient credits → 402.
- Happy-path SSE: yields ``{"t": ...}`` chunks then ``{"done": True}``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pydantic
import pytest

from carrac.asgi import (
    MIGRATED_WRITE_PATHS,
    ExactPath,
)


# ---------------------------------------------------------------------------
# Dispatcher shim unit tests (no routing, no DB)
# ---------------------------------------------------------------------------


def test_weekly_review_in_migrated_write_paths():
    """``POST /api/ai/weekly-review`` must be in MIGRATED_WRITE_PATHS."""
    assert (
        "/api/ai/weekly-review",
        frozenset({"POST"}),
    ) in MIGRATED_WRITE_PATHS


def test_weekly_review_exact_path_matches_post_only():
    """ExactPath shim matches POST but not GET/PUT."""
    ep = ExactPath(
        "/api/ai/weekly-review", app=None, methods=frozenset({"POST"})
    )
    base = {"type": "http", "path": "/api/ai/weekly-review"}

    post_match, _ = ep.matches({**base, "method": "POST"})
    get_match, _ = ep.matches({**base, "method": "GET"})
    put_match, _ = ep.matches({**base, "method": "PUT"})

    assert post_match.value > 0, "POST should match"
    assert get_match.value == 0, "GET should not match (stays on Flask)"
    assert put_match.value == 0, "PUT should not match"


def test_weekly_review_does_not_match_wrong_path():
    """ExactPath does not match a sibling path."""
    ep = ExactPath(
        "/api/ai/weekly-review", app=None, methods=frozenset({"POST"})
    )
    scope = {"type": "http", "path": "/api/ai/weekly-review/extra", "method": "POST"}
    match, _ = ep.matches(scope)
    assert match.value == 0


# ---------------------------------------------------------------------------
# Pydantic model unit tests (no routing required)
# ---------------------------------------------------------------------------


def test_weekly_review_request_empty_body_valid():
    """Empty body (no fields) is valid — handler reads no body data."""
    from carrac.fastapi_app.models.ai_aux import WeeklyReviewRequest

    req = WeeklyReviewRequest()
    assert req is not None


def test_weekly_review_request_extra_field_rejected():
    """``extra="forbid"`` rejects stray fields."""
    from carrac.fastapi_app.models.ai_aux import WeeklyReviewRequest

    with pytest.raises(pydantic.ValidationError) as exc_info:
        WeeklyReviewRequest(unexpected="bad")
    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" for e in errors)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

# The tests below make real HTTP round-trips through the ASGI dispatcher;
# login is injected with the ``login_as`` fixture (tests/fastapi/conftest.py).


@pytest.fixture()
def premium_user_dict():
    return {"id": 42, "email": "user@example.com", "tier": "premium", "is_admin": False}


def test_weekly_review_unauthenticated(asgi_client):
    """Unauthenticated request → 302 redirect to /login."""
    resp = asgi_client.post(
        "/api/ai/weekly-review",
        json={},
        follow_redirects=False,
    )
    # Under Flask the route redirects; under FastAPI require_login does the same.
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("location", "")


def test_weekly_review_tier_gate_non_premium(asgi_client, login_as, premium_user_dict):
    """Non-Premium user is blocked with 403 + upgrade_required."""
    free_user = {**premium_user_dict, "id": 99, "tier": "free"}
    with login_as(free_user):
        with patch("carrac.auth.decorators.get_tier_limits") as mock_limits:
            mock_limits.return_value = {"weekly_review": False}
            resp = asgi_client.post("/api/ai/weekly-review", json={})

    assert resp.status_code == 403
    detail = resp.json().get("detail", resp.json())
    if isinstance(detail, dict):
        assert detail.get("upgrade_required") is True


def test_weekly_review_global_cap(asgi_client, login_as, premium_user_dict):
    """Global daily budget cap → 503."""
    with login_as(premium_user_dict):
        with patch("carrac.auth.decorators.get_tier_limits") as mock_limits:
            mock_limits.return_value = {"weekly_review": True}
            with patch("carrac.ai.billing.check_global_budget") as mock_budget:
                mock_budget.return_value = (
                    False,
                    {"spent_cents": 3000, "cap_cents": 3000},
                )
                resp = asgi_client.post("/api/ai/weekly-review", json={})

    assert resp.status_code == 503


def test_weekly_review_no_trades(asgi_client, login_as, premium_user_dict):
    """No closed trades this week → 400."""
    with login_as(premium_user_dict):
        with patch("carrac.auth.decorators.get_tier_limits") as mock_limits:
            mock_limits.return_value = {"weekly_review": True}
            with patch("carrac.ai.billing.check_global_budget") as mock_budget:
                mock_budget.return_value = (True, {})
                with patch("carrac.content_db.get_content_db") as mock_db_factory:
                    mock_db = MagicMock()
                    mock_db.get_journal_stats.return_value = {
                        "trades": 0, "wins": 0, "losses": 0,
                        "win_rate": 0, "avg_rr": 0, "best_rr": 0, "worst_rr": 0,
                    }
                    mock_db.get_trade_history.return_value = []
                    mock_db_factory.return_value = mock_db
                    resp = asgi_client.post("/api/ai/weekly-review", json={})

    assert resp.status_code == 400


def test_weekly_review_insufficient_credits(asgi_client, login_as, premium_user_dict):
    """Insufficient credits → 402."""
    with login_as(premium_user_dict):
        with patch("carrac.auth.decorators.get_tier_limits") as mock_limits:
            mock_limits.return_value = {"weekly_review": True}
            with patch("carrac.ai.billing.check_global_budget") as mock_budget:
                mock_budget.return_value = (True, {})
                with patch("carrac.content_db.get_content_db") as mock_db_factory:
                    mock_db = MagicMock()
                    mock_db.get_journal_stats.return_value = {
                        "trades": 2, "wins": 1, "losses": 1,
                        "win_rate": 50.0, "avg_rr": 1.0,
                        "best_rr": 2.0, "worst_rr": -1.0,
                    }
                    mock_db.get_trade_history.return_value = [
                        {"asset": "BTC", "direction": "long", "entry_price": 50000,
                         "exit_price": 52000, "rr_actual": 2.0, "exit_reason": "tp_hit",
                         "setup_type": "breakout", "closed_at": "2026-05-05"},
                    ]
                    mock_db_factory.return_value = mock_db
                    with patch("carrac.ai.billing.check_and_reserve", return_value=False):
                        resp = asgi_client.post("/api/ai/weekly-review", json={})

    assert resp.status_code == 402


def test_weekly_review_happy_path_sse(asgi_client, login_as, premium_user_dict):
    """Happy path: SSE yields text chunks then ``{done: True}``."""

    def _fake_stream(messages, max_tokens=2000):
        yield ("Weekly review line 1.", None)
        yield ("Weekly review line 2.", {"prompt_tokens": 100, "completion_tokens": 50})

    with login_as(premium_user_dict):
        with patch("carrac.auth.decorators.get_tier_limits") as mock_limits:
            mock_limits.return_value = {"weekly_review": True}
            with patch("carrac.ai.billing.check_global_budget") as mock_budget:
                mock_budget.return_value = (True, {})
                with patch("carrac.content_db.get_content_db") as mock_db_factory:
                    mock_db = MagicMock()
                    mock_db.get_journal_stats.return_value = {
                        "trades": 2, "wins": 1, "losses": 1,
                        "win_rate": 50.0, "avg_rr": 1.0,
                        "best_rr": 2.0, "worst_rr": -1.0,
                    }
                    mock_db.get_trade_history.return_value = [
                        {"asset": "BTC", "direction": "long", "entry_price": 50000,
                         "exit_price": 52000, "rr_actual": 2.0, "exit_reason": "tp_hit",
                         "setup_type": "breakout", "closed_at": "2026-05-05"},
                    ]
                    mock_db_factory.return_value = mock_db
                    with patch("carrac.ai.billing.check_and_reserve", return_value=True):
                        with patch("carrac.ai.llm_client.stream_chat", side_effect=_fake_stream):
                            with patch("carrac.ai.billing.settle", return_value=5):
                                with patch("user_db.user_db") as mock_udb:
                                    mock_udb.get_ai_balance.return_value = 2500
                                    resp = asgi_client.post(
                                        "/api/ai/weekly-review", json={}
                                    )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    raw = resp.text
    assert 'data: ' in raw
    assert '"t":' in raw
    assert '"done": true' in raw or '"done":true' in raw


# ---------------------------------------------------------------------------
# POST /api/ai/execute-action
# ---------------------------------------------------------------------------


def test_execute_action_in_migrated_write_paths():
    assert (
        "/api/ai/execute-action",
        frozenset({"POST"}),
    ) in MIGRATED_WRITE_PATHS


def test_execute_action_request_extra_field_rejected():
    from carrac.fastapi_app.models.ai import ExecuteActionRequest

    with pytest.raises(pydantic.ValidationError) as exc_info:
        ExecuteActionRequest(tool="play_create", arguments={}, surprise=1)
    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" for e in errors)


def test_execute_action_request_default_arguments_empty_dict():
    from carrac.fastapi_app.models.ai import ExecuteActionRequest

    req = ExecuteActionRequest(tool="play_create")
    assert req.arguments == {}


def test_execute_action_unauthenticated(asgi_client):
    """Unauthenticated → 302 redirect to /login (Flask parity)."""
    resp = asgi_client.post(
        "/api/ai/execute-action",
        json={"tool": "play_create", "arguments": {}},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# POST /api/ai/context-preview
# ---------------------------------------------------------------------------


def test_context_preview_in_migrated_write_paths():
    assert (
        "/api/ai/context-preview",
        frozenset({"POST"}),
    ) in MIGRATED_WRITE_PATHS


def test_context_preview_request_extra_field_rejected():
    from carrac.fastapi_app.models.ai import ContextPreviewRequest

    with pytest.raises(pydantic.ValidationError) as exc_info:
        ContextPreviewRequest(asset="BTC", surprise=1)
    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" for e in errors)


def test_context_preview_request_defaults():
    from carrac.fastapi_app.models.ai import ContextPreviewRequest

    req = ContextPreviewRequest()
    assert req.timeframe == "1D"
    assert req.asset == ""
    assert req.thesis_text == ""
    assert req.recipe_id is None


def test_context_preview_unauthenticated(asgi_client):
    """Unauthenticated → 302 redirect to /login (Flask parity)."""
    resp = asgi_client.post(
        "/api/ai/context-preview",
        json={},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("location", "")


# ---------------------------------------------------------------------------
# Execute-action applies the same admin pass-through as /api/ai/chat
# ---------------------------------------------------------------------------
# A minimal FastAPI app with just the ai_aux router (same shape as
# tests/fastapi/test_ai_chat.py), so require_login can be overridden the way
# FastAPI supports — patching the module attribute after the router captured
# Depends(require_login) does nothing (the weekly-review tests above use the
# ``login_as`` fixture, which overrides the dependency on the real app).


@pytest.fixture()
def _aux_app():
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.ai_aux import router as aux_router
    from carrac.fastapi_app.deps.auth import classify_request, RateContext

    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(test_app)
    test_app.include_router(aux_router)
    test_app.dependency_overrides[classify_request] = lambda: RateContext(
        pool="web_session", key="user:test", tier="member")
    yield test_app
    test_app.dependency_overrides.clear()


def _aux_post(test_app, user, payload):
    import asyncio
    import httpx
    from carrac.fastapi_app.deps.auth import require_login
    test_app.dependency_overrides[require_login] = lambda: user

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=test_app),
                                     base_url="http://test") as c:
            return await c.post("/api/ai/execute-action", json=payload)

    return asyncio.run(_go())


def test_execute_action_member_blocked_when_flag_off(_aux_app, premium_user_dict, monkeypatch):
    """Flag OFF, non-admin → 403 with ai_chat_disabled (unchanged behaviour)."""
    monkeypatch.setattr("carrac.integrations.is_ai_chat_enabled_flag", lambda: False)
    resp = _aux_post(_aux_app, dict(premium_user_dict, is_admin=False),
                     {"tool": "play_create", "arguments": {}})
    assert resp.status_code == 403, resp.text
    assert resp.json().get("ai_chat_disabled") is True


def test_execute_action_admin_passes_when_flag_off(_aux_app, premium_user_dict, monkeypatch):
    """Sibling sweep: flag OFF, admin → the kill-switch does not fire.
    Whatever the later gates return, it is not the ai_chat_disabled 403."""
    monkeypatch.setattr("carrac.integrations.is_ai_chat_enabled_flag", lambda: False)
    resp = _aux_post(_aux_app, dict(premium_user_dict, is_admin=True),
                     {"tool": "play_create", "arguments": {}})
    assert not (resp.status_code == 403 and resp.json().get("ai_chat_disabled") is True), resp.text
