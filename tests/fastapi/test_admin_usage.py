"""Tests for GET /api/admin/usage.

Covers:
- Anonymous → 302 redirect (no session).
- Non-admin → 403.
- Admin session → 200 + correct payload shape.
- ai_usage_log row seeded → appears in top_ai_users.
- ?period parsing (7d / 30d / all).
- ?user_id=N drill → per-user shape.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from httpx import ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_ai_usage_log_table():
    """Create ai_usage_log + users in the pool DB (carrac.db in tests).

    In production both tables live in the same PostgreSQL DB. In tests
    db_pool uses carrac.db while user_db uses users.db; we need the
    ai_usage_log table and a users view in the pool DB so the JOIN query
    in admin_usage_json can work.
    """
    from carrac.db_pool import get_conn

    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                tier TEXT DEFAULT 'free'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                feature TEXT NOT NULL,
                asset TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_cents INTEGER DEFAULT 0,
                charged_cents INTEGER DEFAULT 0,
                model TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                token_suffix TEXT,
                scope TEXT DEFAULT 'full',
                last_used_at TIMESTAMP,
                revoked INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Minimal FastAPI test app — mirrors test_journal_ai_gate pattern.
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app(app):  # noqa: ARG001 — Flask app fixture sets up env + DB
    """Minimal FastAPI app mounting only the admin_usage_json router."""
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.admin_usage_json import router
    from carrac.fastapi_app.deps.auth import classify_request, RateContext

    # Ensure the tables that admin_usage_json queries via get_conn() exist
    # in the pool's SQLite DB (carrac.db).  In production all tables live
    # in the same PostgreSQL instance; in tests the pool uses a separate file.
    _ensure_ai_usage_log_table()

    ta = FastAPI()
    ta.state.limiter = limiter
    ta.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(ta)
    ta.include_router(router)

    # Provide a default rate context so the limiter has a key bucket.
    ta.dependency_overrides[classify_request] = lambda: RateContext(
        pool="web_session", key="user:test", tier="admin"
    )
    return ta


@pytest.fixture
def api_client(test_app):
    """Sync httpx client driving the minimal FastAPI test app."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
# Router import smoke
# ---------------------------------------------------------------------------


def test_admin_usage_router_importable():
    from carrac.fastapi_app.routers import admin_usage_json

    assert admin_usage_json.router is not None


# ---------------------------------------------------------------------------
# MIGRATED_PATHS entry — a route served by FastAPI must be listed there, or
# Flask keeps answering it.
# ---------------------------------------------------------------------------


def test_admin_usage_in_migrated_paths():
    from carrac.asgi import MIGRATED_PATHS

    assert "/api/admin/usage" in MIGRATED_PATHS, (
        "GET /api/admin/usage must be added to MIGRATED_PATHS in carrac/asgi.py"
    )


# ---------------------------------------------------------------------------
# Auth gate — anonymous (no require_admin override → real dep fires → redirect)
# ---------------------------------------------------------------------------


def test_admin_usage_anonymous_redirects(test_app, api_client):
    """Without an admin override the real require_admin dep redirects to /login."""
    # Clear any override so the real dep runs.
    from carrac.fastapi_app.deps.auth import require_admin
    test_app.dependency_overrides.pop(require_admin, None)

    resp = api_client.get("/api/admin/usage", follow_redirects=False)
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Auth gate — non-admin
# ---------------------------------------------------------------------------


def test_admin_usage_non_admin_403(test_app, api_client):
    """Non-admin authenticated user → 403."""
    _login_as_non_admin(test_app)
    resp = api_client.get("/api/admin/usage", follow_redirects=False)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Happy path — summary shape
# ---------------------------------------------------------------------------


def test_admin_usage_200_shape(test_app, api_client):
    """Admin GET /api/admin/usage → 200 with expected top-level keys."""
    _login_as_admin(test_app)
    resp = api_client.get("/api/admin/usage?period=7d")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["period"] == "7d"
    assert isinstance(data["top_ai_users"], list)
    assert isinstance(data["recent_api_keys"], list)
    assert "ai_queries" in data["totals"]
    assert "active_api_keys" in data["totals"]
    assert isinstance(data["errors"], list)


# ---------------------------------------------------------------------------
# ai_usage_log row → appears in top_ai_users
# ---------------------------------------------------------------------------


def test_top_ai_users_includes_seeded_user(test_app, api_client, app):
    """Seed an ai_usage_log row; the user must appear in top_ai_users.

    The admin_usage endpoint queries via get_conn() (carrac.db in tests),
    so we seed both the users and ai_usage_log rows directly in that DB.
    """
    from carrac.db_pool import adapt_query, get_conn

    # Insert a user and an ai_usage_log row directly into the pool DB.
    with get_conn() as conn:
        cursor = conn.execute(
            adapt_query(
                "INSERT INTO users (email, tier) VALUES (?, ?)"
            ),
            ("aiuser@test.dev", "free"),
        )
        uid = cursor.lastrowid
        conn.execute(
            adapt_query(
                """
                INSERT INTO ai_usage_log
                    (user_id, feature, asset, input_tokens, output_tokens,
                     cost_cents, charged_cents, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (uid, "copilot", "BTC", 100, 50, 5, 5, "claude-haiku"),
        )
        conn.commit()

    _login_as_admin(test_app)
    resp = api_client.get("/api/admin/usage?period=all")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    user_ids = [u["user_id"] for u in data["top_ai_users"]]
    assert uid in user_ids, (
        f"Seeded user {uid} not found in top_ai_users: {data['top_ai_users']}"
    )
    seeded = next(u for u in data["top_ai_users"] if u["user_id"] == uid)
    assert seeded["queries"] >= 1


# ---------------------------------------------------------------------------
# ?period parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("period", ["7d", "30d", "all"])
def test_period_param_accepted(test_app, api_client, period):
    """All valid period values → 200 with period echoed back."""
    _login_as_admin(test_app)
    resp = api_client.get(f"/api/admin/usage?period={period}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["period"] == period


def test_unknown_period_defaults_gracefully(test_app, api_client):
    """Unknown period string → treated as 7d fallback, still returns 200."""
    _login_as_admin(test_app)
    resp = api_client.get("/api/admin/usage?period=bogus")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# ?user_id=N drill
# ---------------------------------------------------------------------------


def test_user_id_drill_shape(test_app, api_client, app):
    """?user_id=N → per-user shape with user_id + ai_summary + api_keys + errors.

    Redis is not available in tests so build_usage_response will degrade
    gracefully; errors list may contain a note about Redis.  The ai_summary
    and api_keys come from get_conn() (carrac.db) so they will be empty
    but the keys must be present.
    """
    _login_as_admin(test_app)
    # Use a fixed user_id (no matching DB row is fine — queries return empty
    # sets; user_db.get_user_by_id returns None → tier defaults to 'free').
    resp = api_client.get("/api/admin/usage?user_id=9999&period=7d")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "user_id" in data
    assert "errors" in data
    assert isinstance(data["errors"], list)
    assert "ai_summary" in data
    assert "api_keys" in data
