"""Free-tier leak guard on the LIVE morning-report endpoint.

`/api/report/morning/latest` is served by **FastAPI** in production (strangler
``MIGRATED_PATHS``), not Flask — so this is the handler real free-tier traffic
hits. The handler must not fetch the 'morning' report for every tier: that would leak the
operator's positions (Action Dashboard) to free accounts (the free→general
split must live in this handler, not only in the shadowed Flask twin). This suite drives the FastAPI
handler directly and asserts a free tier is served the operator-neutral
'general' report (never 'morning'), while a paid tier still gets 'morning'.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport

_MORNING_WITH_POSITIONS = (
    "## The Story\nMacro narrative; nothing operator-private here.\n\n"
    "## Action Dashboard\n"
    "\U0001f534 **BTC Position Risk** — Short from $75,736 is +6.2% in-the-money.\n\n"
    "## Market Snapshot\n| Asset | Price |\n| BTC | 71287 |\n"
)
_GENERAL_NEUTRAL = (
    "## The Story\nNeutral shared macro overview.\n\n"
    "## Market Snapshot\n| Asset | Price |\n| BTC | 71287 |\n"
)


@pytest.fixture
def reports_app(app):  # noqa: ARG001 — Flask app fixture sets up env (DATA_DIR etc.)
    """Minimal FastAPI app including just the reports router (gate-test pattern)."""
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.reports import router as reports_router
    from carrac.fastapi_app.deps.auth import (
        classify_request, auth_or_key, RateContext,
    )

    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(test_app)
    test_app.include_router(reports_router)
    test_app.dependency_overrides[classify_request] = lambda: RateContext(
        pool="web_session", key="user:test", tier="free")
    test_app.dependency_overrides[auth_or_key] = lambda: {"user_id": None}
    return test_app


@pytest.fixture
def reports_client(reports_app):
    ac = httpx.AsyncClient(
        transport=ASGITransport(app=reports_app), base_url="http://testserver")

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


def _seed(report_type, content):
    from carrac.content_db import get_content_db
    get_content_db().save_report(
        content=content, assets_included=[],
        generation_time_ms=0, report_type=report_type)


def _as_tier(tier, personal_report):
    """Patch the route's tier resolution to a fixed tier + personal_report."""
    return (
        patch("carrac.fastapi_app.routers.reports.get_request_tier",
              return_value=tier),
        patch("carrac.auth.decorators.get_tier_limits",
              return_value={"personal_report": personal_report}),
    )


class TestFastapiMorningLatestFreeTier:
    def test_free_no_general_does_not_leak_morning(self, reports_client):
        _seed("morning", _MORNING_WITH_POSITIONS)  # NB: NO general seeded
        p1, p2 = _as_tier("free", "none")
        with p1, p2:
            r = reports_client.get("/api/report/morning/latest")
        assert r.status_code == 200, r.text
        content = (r.json() or {}).get("content") or ""
        assert "Action Dashboard" not in content
        assert "75,736" not in content

    def test_free_served_general_when_present(self, reports_client):
        _seed("morning", _MORNING_WITH_POSITIONS)
        _seed("general", _GENERAL_NEUTRAL)
        p1, p2 = _as_tier("free", "none")
        with p1, p2:
            r = reports_client.get("/api/report/morning/latest")
        assert r.status_code == 200, r.text
        content = (r.json() or {}).get("content") or ""
        assert "Neutral shared macro overview" in content
        assert "Action Dashboard" not in content
        assert "75,736" not in content

    def test_paid_gets_neutralized_fallback_not_operator_book(self, reports_client):
        # The operator's morning report is ADMIN-ONLY. A paid tier with no
        # general row gets the morning report only through the
        # operator-neutral filter — content present, operator book absent.
        _seed("morning", _MORNING_WITH_POSITIONS)
        p1, p2 = _as_tier("premium", "full")
        with p1, p2:
            r = reports_client.get("/api/report/morning/latest")
        assert r.status_code == 200, r.text
        content = (r.json() or {}).get("content") or ""
        assert content.strip(), "paid tier must not be served an empty report"
        assert "Action Dashboard" not in content
        assert "75,736" not in content
