"""Tests for GET /api/admin/intents and /api/admin/intents/unmet.

Covers:
- Anonymous → redirect/401 (no session).
- Non-admin → 403.
- Admin session → 200 + correct payload shape.
- Seeded rows appear in the live feed and respect tool/tier/q filters.
- The unmet-needs panel only returns rows with a non-null unmet_need.
- /admin/intents HTML page renders (Flask).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx import ASGITransport

from carrac.mcp import tools  # noqa: F401 — ensure TOOL_REGISTRY is populated for the tools/tiers filter lists


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_rows(app):  # noqa: ARG001 — Flask app fixture sets up env + DB
    """Create mcp_intent_log in the pool DB (carrac.db in tests) and seed
    a few rows. Mirrors test_admin_usage.py's _ensure_ai_usage_log_table."""
    from carrac.db_pool import adapt_query, get_conn

    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_intent_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tool TEXT NOT NULL,
                tier TEXT NOT NULL,
                user_ref TEXT,
                user_intent TEXT,
                unmet_need TEXT,
                latency_ms REAL NOT NULL,
                ok INTEGER NOT NULL,
                result_empty INTEGER NOT NULL DEFAULT 0,
                error_kind TEXT,
                prev_tool TEXT
            )
            """
        )
        conn.execute(
            adapt_query(
                """
                INSERT INTO mcp_intent_log
                    (tool, tier, user_ref, user_intent, unmet_need, latency_ms, ok, result_empty, error_kind, prev_tool)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            ('scanner_get_screen', 'member', 'hash-a', 'find bullish setups', None, 42.5, 1, 0, None, None),
        )
        conn.execute(
            adapt_query(
                """
                INSERT INTO mcp_intent_log
                    (tool, tier, user_ref, user_intent, unmet_need, latency_ms, ok, result_empty, error_kind, prev_tool)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            ('get_started', 'free', 'hash-b', 'onboarding check', 'no sector filter', 12.1, 1, 0, None, 'scanner_get_screen'),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Minimal FastAPI test app — mirrors tests/fastapi/test_admin_usage.py
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app(app):
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.admin_intents_json import router
    from carrac.fastapi_app.deps.auth import classify_request, RateContext

    _seed_rows(app)

    ta = FastAPI()
    ta.state.limiter = limiter
    ta.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(ta)
    ta.include_router(router)

    ta.dependency_overrides[classify_request] = lambda: RateContext(
        pool="web_session", key="user:test", tier="admin"
    )
    return ta


@pytest.fixture
def api_client(test_app):
    ac = httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://testserver"
    )
    loop = asyncio.new_event_loop()

    class _Sync:
        def __getattr__(self, name):
            method = getattr(ac, name)
            return lambda *a, **k: loop.run_until_complete(method(*a, **k))

        def close(self):
            loop.run_until_complete(ac.aclose())
            loop.close()

    wrapper = _Sync()
    yield wrapper
    wrapper.close()


def _login_as_admin(test_app):
    from carrac.fastapi_app.deps.auth import require_admin

    test_app.dependency_overrides[require_admin] = lambda: {
        "id": 1,
        "email": "admin@test.dev",
        "is_admin": True,
        "tier": "admin",
    }


def _login_as_non_admin(test_app):
    from carrac.fastapi_app.deps.auth import require_admin
    from fastapi import HTTPException

    def _deny():
        raise HTTPException(status_code=403, detail="Admin access required")

    test_app.dependency_overrides[require_admin] = _deny


# ---------------------------------------------------------------------------
# Router import + MIGRATED_PATHS wiring
# ---------------------------------------------------------------------------


def test_admin_intents_router_importable():
    from carrac.fastapi_app.routers import admin_intents_json

    assert admin_intents_json.router is not None


def test_admin_intents_in_migrated_paths():
    from carrac.asgi import MIGRATED_PATHS

    assert "/api/admin/intents" in MIGRATED_PATHS
    assert "/api/admin/intents/unmet" in MIGRATED_PATHS


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------


def test_intents_anonymous_redirects(test_app, api_client):
    from carrac.fastapi_app.deps.auth import require_admin
    test_app.dependency_overrides.pop(require_admin, None)

    resp = api_client.get("/api/admin/intents", follow_redirects=False)
    assert resp.status_code == 302


def test_intents_non_admin_403(test_app, api_client):
    _login_as_non_admin(test_app)
    resp = api_client.get("/api/admin/intents", follow_redirects=False)
    assert resp.status_code == 403


def test_unmet_non_admin_403(test_app, api_client):
    _login_as_non_admin(test_app)
    resp = api_client.get("/api/admin/intents/unmet", follow_redirects=False)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Happy path — live feed
# ---------------------------------------------------------------------------


def test_intents_200_shape(test_app, api_client):
    _login_as_admin(test_app)
    resp = api_client.get("/api/admin/intents")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data["rows"], list)
    assert data["count"] == len(data["rows"])
    assert "scanner_get_screen" in data["tools"]
    assert "member" in data["tiers"]
    assert len(data["rows"]) >= 2


def test_intents_filter_by_tool(test_app, api_client):
    _login_as_admin(test_app)
    resp = api_client.get("/api/admin/intents?tool=scanner_get_screen")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert all(r["tool"] == "scanner_get_screen" for r in data["rows"])
    assert len(data["rows"]) >= 1


def test_intents_filter_by_tier(test_app, api_client):
    _login_as_admin(test_app)
    resp = api_client.get("/api/admin/intents?tier=free")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert all(r["tier"] == "free" for r in data["rows"])


def test_intents_free_text_search(test_app, api_client):
    _login_as_admin(test_app)
    resp = api_client.get("/api/admin/intents?q=onboarding")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["rows"]) >= 1
    assert all("onboarding" in (r["user_intent"] or "") for r in data["rows"])


def test_intents_invalid_date_400(test_app, api_client):
    _login_as_admin(test_app)
    resp = api_client.get("/api/admin/intents?date=not-a-date")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Happy path — unmet needs (separate/pinned panel)
# ---------------------------------------------------------------------------


def test_unmet_only_returns_rows_with_unmet_need(test_app, api_client):
    _login_as_admin(test_app)
    resp = api_client.get("/api/admin/intents/unmet")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["rows"]) >= 1
    assert all(r["unmet_need"] for r in data["rows"])
    assert any(r["tool"] == "get_started" for r in data["rows"])


# ---------------------------------------------------------------------------
# Flask HTML page (/admin/intents)
# ---------------------------------------------------------------------------


class TestAdminIntentsPage:
    def test_page_requires_admin(self, client):
        response = client.get('/admin/intents')
        assert response.status_code in (302, 401)

    def test_page_requires_admin_not_user(self, auth_client):
        response = auth_client.get('/admin/intents')
        assert response.status_code in (302, 401, 403)

    def test_page_accessible_by_admin(self, admin_client, monkeypatch):
        import user_db as _udb
        monkeypatch.setattr(_udb.user_db, 'get_user_by_id', lambda uid: {
            'id': 'admin', 'email': 'admin@example.com', 'is_admin': True, 'is_active': True,
        })
        response = admin_client.get('/admin/intents')
        assert response.status_code == 200, (
            f"Expected 200 for admin /admin/intents, got {response.status_code}"
        )
        assert b'Intent' in response.data
