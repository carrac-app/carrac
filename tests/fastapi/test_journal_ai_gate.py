"""Free-tier gate on the token-costing journal-AI endpoints.

The four journal AI endpoints (trade ai-review, session ai-summary, weekly
review, NL ai-query) call an LLM and bill credits. They were
``require_login``-only, so any logged-in account — including FREE — could
trigger paid token spend. This suite verifies the gate added in journal_ai.py:
free tier (``ai_queries_monthly == 0``) → 403 + ``upgrade_required``; a paid
tier passes the gate. Mirrors the gate already covered for /api/ai/chat.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _user(user_id=99, tier="free"):
    return {"id": user_id, "email": f"u{user_id}@example.com", "is_admin": False, "tier": tier}


@pytest.fixture
def asgi_app(app):  # noqa: ARG001 — Flask app fixture sets up env
    """Minimal FastAPI app including just the journal_ai router."""
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.journal_ai import router as journal_router
    from carrac.fastapi_app.deps.auth import classify_request, RateContext

    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(test_app)
    test_app.include_router(journal_router)

    def _stub_classify():
        return RateContext(pool="web_session", key="user:test", tier="member")

    test_app.dependency_overrides[classify_request] = _stub_classify
    return test_app


@pytest.fixture
def api_client(asgi_app):
    import asyncio
    import httpx
    from httpx import ASGITransport

    ac = httpx.AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://testserver")

    class _Sync:
        def __init__(self, c):
            self._c = c
            self._loop = asyncio.new_event_loop()

        def __getattr__(self, n):
            m = getattr(self._c, n)
            return lambda *a, **k: self._loop.run_until_complete(m(*a, **k))

        def close(self):
            self._loop.run_until_complete(self._c.aclose())
            self._loop.close()

    w = _Sync(ac)
    yield w
    w.close()


def _login_as(asgi_app, user):
    from carrac.fastapi_app.deps.auth import require_login

    asgi_app.dependency_overrides[require_login] = lambda: user


JOURNAL_AI_ENDPOINTS = [
    ("/api/journal/trades/1/ai-review", {}),
    ("/api/journal/sessions/1/ai-summary", {}),
    ("/api/journal/ai-review/weekly", {"days": 7}),
    ("/api/journal/ai-query", {"question": "how did I do this week?"}),
]


class TestJournalAiFreeGate:
    @pytest.mark.parametrize("path,body", JOURNAL_AI_ENDPOINTS)
    def test_free_user_blocked_403(self, api_client, asgi_app, path, body):
        """Free tier (ai_queries_monthly == 0) → 403 + upgrade_required, before any LLM/DB work."""
        _login_as(asgi_app, _user(tier="free"))
        with patch("carrac.auth.decorators.get_tier_limits",
                   return_value={"ai_queries_monthly": 0}):
            r = api_client.post(path, json=body)
        assert r.status_code == 403, f"{path}: expected 403, got {r.status_code}: {r.text}"
        assert r.json().get("upgrade_required") is True, f"{path}: missing upgrade_required"

    def test_paid_user_passes_gate(self, api_client, asgi_app):
        """Member tier (ai_queries_monthly == 40) is NOT blocked by the gate."""
        _login_as(asgi_app, _user(user_id=7, tier="member"))
        with patch("carrac.auth.decorators.get_tier_limits",
                   return_value={"ai_queries_monthly": 40}), \
             patch("carrac.content_db.get_content_db", return_value=MagicMock()), \
             patch("carrac.journal.ai_review.weekly_review",
                   return_value={"summary": "ok", "stats": {}}):
            r = api_client.post("/api/journal/ai-review/weekly", json={"days": 7})
        assert r.status_code != 403, f"paid user should pass the gate, got 403: {r.text}"
