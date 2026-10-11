"""The live /api/report/morning/latest keeps report internals for the operator.

A stored report's ``metadata`` holds the narration script, the critic's
review, generation cost, model names, per-phase results, data-source error
details and the raw X-post candidates. The operator (admin tier) gets all of
it. Every other logged-in caller gets only what the report pages read from it:
the per-source ok/error status that drives the degraded-data banner, plus the
filtered ``x_candidates`` at the top level.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from httpx import ASGITransport

_SCRIPT = "Your BTC short from $75,736 is working; keep the stop at $79,872."
_CRITIC = "Critic: paragraph three overstates the ETF flow."
_RAW_CANDIDATE = "BTC DVOL just printed 62, the highest since March."
_SAFE_CANDIDATE = "BTC closed above its 200-day average for the first time in a month."

_METADATA = {
    "model": "opus:x+sonnet:y+grok:z",
    "cost": 1.23,
    "input_tokens": 1000,
    "audio_script": _SCRIPT,
    "display_script": _SCRIPT,
    "critic_review": _CRITIC,
    "phase_results": {"briefing": {"ok": True, "detail": "internal"}},
    "data_sources": {
        "chart_data": {"status": "ok", "detail": "41 pairs"},
        "x_feed": {"status": "error", "detail": "HTTPError 401 for https://internal.example/feed"},
    },
    "x_candidates": [_RAW_CANDIDATE, _SAFE_CANDIDATE],
}

_GENERAL = {
    "content": "## The Story\nNeutral overview.\n\n## Market Snapshot\n| BTC | 71287 |\n",
    "created_at": "2026-06-10",
    "id": 50,
    "summary": None,
    "audio_script": None,
    "metadata": json.dumps(_METADATA),
}
_MORNING = {**_GENERAL, "id": 51}


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def get_latest_report(self, report_type=None, max_age_days=None):  # noqa: ARG002
        return self._rows.get(report_type)

    def get_personal_section(self, report_id, user_id):  # noqa: ARG002
        return None


class _LoggedInSession:
    """Stands in for CarracSessionMiddleware: a logged-in browser session."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from starlette.datastructures import State
            scope.setdefault("state", {})
            scope["state"] = State({"session": {"user_id": 5}})
        await self.app(scope, receive, send)


@pytest.fixture
def client(app):  # noqa: ARG001 — Flask app fixture sets the env first
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.deps.auth import RateContext, auth_or_key, classify_request
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.reports import router

    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.add_middleware(SlowAPIMiddleware)
    test_app.add_middleware(_LoggedInSession)
    exception_handlers.install(test_app)
    test_app.include_router(router)
    test_app.dependency_overrides[classify_request] = lambda: RateContext(
        pool="web_session", key="user:5", tier="free")
    test_app.dependency_overrides[auth_or_key] = lambda: {"user_id": 5}
    limiter.reset()

    loop = asyncio.new_event_loop()
    ac = httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url="http://t")

    def get(path):
        return loop.run_until_complete(ac.get(path))

    yield get
    loop.run_until_complete(ac.aclose())
    loop.close()


def _serve(client, monkeypatch, tier, rows):
    from carrac import content_db as cdb
    import carrac.fastapi_app.routers.reports as rr

    monkeypatch.setenv("ENABLE_MORNING_REPORT", "true")
    monkeypatch.setattr(cdb, "get_content_db", lambda: _FakeDB(rows))
    monkeypatch.setattr(rr, "get_request_tier", lambda request: tier)
    r = client("/api/report/morning/latest")
    assert r.status_code == 200
    return r.json()


@pytest.mark.parametrize("tier,rows", [
    ("free", {"general": _GENERAL}),
    ("premium", {"general": _GENERAL}),
    ("premium", {"morning": _MORNING}),  # paid fallback to the neutral morning cut
])
def test_non_operator_gets_no_report_internals(client, monkeypatch, tier, rows):
    data = _serve(client, monkeypatch, tier, rows)
    blob = json.dumps(data)
    for private in (_SCRIPT, _CRITIC, _RAW_CANDIDATE, "opus:x", "phase_results",
                    "critic_review", '"cost"', "internal.example", "41 pairs"):
        assert private not in blob, private
    assert data["metadata"] == {
        "data_sources": {"chart_data": {"status": "ok"}, "x_feed": {"status": "error"}},
    }
    assert data["x_candidates"] == [_SAFE_CANDIDATE]


def test_operator_gets_the_full_metadata(client, monkeypatch):
    data = _serve(client, monkeypatch, "admin", {"morning": _MORNING})
    assert data["metadata"] == _METADATA


def test_admin_previewing_as_general_is_served_the_public_cut(client, monkeypatch):
    from carrac import content_db as cdb
    import carrac.fastapi_app.routers.reports as rr

    monkeypatch.setattr(cdb, "get_content_db", lambda: _FakeDB({"general": _GENERAL}))
    monkeypatch.setattr(rr, "get_request_tier", lambda request: "admin")
    data = client("/api/report/morning/latest?as=general").json()
    assert _CRITIC not in json.dumps(data)
    assert set(data["metadata"]) == {"data_sources"}


def test_metadata_without_data_sources_is_omitted_for_non_operators(client, monkeypatch):
    row = {**_GENERAL, "metadata": json.dumps({"x_candidates": [_SAFE_CANDIDATE],
                                                "dedup": {"dup_pair_count": 2}})}
    data = _serve(client, monkeypatch, "free", {"general": row})
    assert "metadata" not in data
    assert data["x_candidates"] == [_SAFE_CANDIDATE]
