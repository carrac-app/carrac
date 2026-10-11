"""Async-offload audit stragglers.

Three blocking sync calls were found executing directly inside ``async def``
FastAPI handlers (not wrapped in ``asyncio.to_thread`` and not behind an
async client), across the routers covered by this audit:

* ``ai_aux.py::ai_weekly_review`` — ``check_global_budget()`` hits Redis
  synchronously (``carrac.ai.billing.get_global_ai_spend_cents_today`` does
  ``client.get(...)``) before the SSE response is even built.
* ``chart_internal.py::get_chart_data_internal`` — ``_verify_key(request)``
  calls ``resolve_api_key`` which, for per-user ``cvx_k_`` keys, hits
  ``user_db.verify_api_key_token`` (a DB read) synchronously.
* ``facts.py::get_facts_endpoint`` — ``bump_usage(...)`` hits Redis
  synchronously (``client.incr`` / ``client.expire``).

Each test below monkeypatches the narrowest blocking function the handler
calls with a wrapper that records ``threading.get_ident()`` before
delegating to the real implementation, then asserts the recorded ident
differs from the test's own thread ident (the ``asgi_client``/``api_client``
fixtures run the event loop on the test's own thread, so
``threading.get_ident()`` called directly in the test IS the event-loop
thread). If the blocking call still executes on the event-loop thread, the
idents match and the test fails.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# ai_aux.py — POST /api/ai/weekly-review — check_global_budget() offload
# ---------------------------------------------------------------------------
#
# require_login is a FastAPI Depends() captured by direct function reference
# at router-definition time — patching the module attribute does not affect
# an already-built Dependant (see the xfail integration tests in
# test_ai_aux.py / test_journal_ai.py). The working pattern (test_journal_ai_gate.py)
# builds a minimal FastAPI app with just this router and uses
# dependency_overrides[require_login] instead.


@pytest.fixture
def weekly_review_app(app):  # noqa: ARG001 — Flask app fixture sets up env
    """Minimal FastAPI app including just the ai_aux router."""
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.ai_aux import router as ai_aux_router
    from carrac.fastapi_app.deps.auth import classify_request, RateContext

    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(test_app)
    test_app.include_router(ai_aux_router)

    def _stub_classify():
        return RateContext(pool="web_session", key="user:test", tier="member")

    test_app.dependency_overrides[classify_request] = _stub_classify
    return test_app


@pytest.fixture
def weekly_review_client(weekly_review_app):
    import asyncio
    import httpx
    from httpx import ASGITransport

    ac = httpx.AsyncClient(
        transport=ASGITransport(app=weekly_review_app), base_url="http://testserver"
    )

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


def _login_premium(weekly_review_app, user_id=42):
    from carrac.fastapi_app.deps.auth import require_login

    user = {"id": user_id, "email": f"u{user_id}@example.com", "is_admin": False, "tier": "premium"}
    weekly_review_app.dependency_overrides[require_login] = lambda: user


def test_weekly_review_check_global_budget_runs_off_event_loop_thread(
    weekly_review_client, weekly_review_app
):
    """``check_global_budget()`` must run in a worker thread, not the event loop.

    Uses the (False, ...) branch so the handler 503s immediately after the
    call — no need to mock the journal DB / billing chain past that point.
    """
    _login_premium(weekly_review_app)

    main_thread_ident = threading.get_ident()
    recorded: dict = {}

    def _wrapper():
        recorded["ident"] = threading.get_ident()
        return False, {"spent_cents": 3000, "cap_cents": 3000}

    with patch("carrac.auth.decorators.get_tier_limits", return_value={"weekly_review": True}), \
         patch("carrac.ai.billing.check_global_budget", side_effect=_wrapper):
        resp = weekly_review_client.post("/api/ai/weekly-review", json={})

    assert resp.status_code == 503, resp.text
    assert recorded.get("ident") is not None, "check_global_budget was never called"
    assert recorded["ident"] != main_thread_ident, (
        "check_global_budget ran on the event-loop thread — blocking Redis call "
        "must be wrapped in asyncio.to_thread"
    )


# ---------------------------------------------------------------------------
# chart_internal.py — GET /api/chart-internal/{symbol} — _verify_key offload
# ---------------------------------------------------------------------------


_MOCK_CHART_PAYLOAD = {
    "ohlcv": [{"time": 1700000000, "open": 30000.0, "high": 31000.0, "low": 29000.0, "close": 30500.0}],
    "volume": [],
    "rsi": [],
    "macd": [],
    "symbol": "BTC",
    "timeframe": "1D",
    "cpr": None,
    "weekly_cpr": None,
    "daily_cpr": None,
    "indicators": {},
    "indicators_ext": {},
}


def test_chart_internal_verify_key_runs_off_event_loop_thread(asgi_client, monkeypatch):
    """``_verify_key`` (→ ``resolve_api_key``, which hits user_db for cvx_k_
    per-user keys) must run in a worker thread, not the event loop thread."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-thread-audit-key")

    from carrac.auth.decorators import resolve_api_key as _real_resolve_api_key

    main_thread_ident = threading.get_ident()
    recorded: dict = {}

    def _wrapper(key, allow_shared=False):
        recorded["ident"] = threading.get_ident()
        return _real_resolve_api_key(key, allow_shared=allow_shared)

    with patch("carrac.auth.decorators.resolve_api_key", side_effect=_wrapper), \
         patch("carrac.charts.service.build_chart_payload", return_value=_MOCK_CHART_PAYLOAD):
        resp = asgi_client.get(
            "/api/chart-internal/BTC",
            headers={"X-Internal-Key": "test-thread-audit-key"},
        )

    assert resp.status_code == 200, resp.text
    assert recorded.get("ident") is not None, "resolve_api_key was never called"
    assert recorded["ident"] != main_thread_ident, (
        "_verify_key/resolve_api_key ran on the event-loop thread — blocking "
        "auth DB lookup must be wrapped in asyncio.to_thread"
    )


# ---------------------------------------------------------------------------
# facts.py — GET /api/facts/{symbol} — bump_usage offload
# ---------------------------------------------------------------------------


_MOCK_FACTS_BUNDLE = {
    "symbol": "BTC",
    "ma_stack": {"signal": "bullish", "score": 3},
    "rsi": {"value": 55.0, "zone": "neutral"},
}


def test_facts_bump_usage_runs_off_event_loop_thread(asgi_client, monkeypatch):
    """``bump_usage`` (Redis INCR/EXPIRE) must run in a worker thread."""
    monkeypatch.setenv("SCREENSHOT_API_KEY", "test-thread-facts-key")

    main_thread_ident = threading.get_ident()
    recorded: dict = {}

    def _wrapper(endpoint, tier):
        recorded["ident"] = threading.get_ident()
        return 1

    with patch("carrac.facts.wire.get_asset_facts", return_value=_MOCK_FACTS_BUNDLE), \
         patch("carrac.facts.cost.bump_usage", side_effect=_wrapper):
        resp = asgi_client.get(
            "/api/facts/BTC",
            headers={"X-Internal-Key": "test-thread-facts-key"},
        )

    assert resp.status_code == 200, resp.text
    assert recorded.get("ident") is not None, "bump_usage was never called"
    assert recorded["ident"] != main_thread_ident, (
        "bump_usage ran on the event-loop thread — blocking Redis call must "
        "be wrapped in asyncio.to_thread"
    )
