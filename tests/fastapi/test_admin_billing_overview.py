"""Tests for GET /api/admin/billing/overview.

Covers:
- 200 + JSON shape for admin caller.
- 403 for a non-admin session.
- 302 redirect for anonymous caller.
- MRR > 0 when a member user with future subscription_expires_at is present.
- Graceful degradation: DB error → 200 with errors list, not 500.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_user():
    return {"id": 1, "email": "admin@example.test", "tier": "admin", "is_admin": True}


def _nonadmin_user():
    return {"id": 2, "email": "user@example.test", "tier": "member", "is_admin": False}


# ---------------------------------------------------------------------------
# Minimal test-app fixture (same pattern as test_ai_chat.py / test_journal_ai_gate.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app(app):  # noqa: ARG001 — Flask app fixture sets up env
    """Minimal FastAPI app with only the billing overview router.

    Uses the real exception handlers + limiter so 4xx shapes match prod.
    classify_request is stubbed because there is no CarracSessionMiddleware
    in this test app (same pattern as test_ai_chat.py).
    """
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.admin_billing_overview_json import router
    from carrac.fastapi_app.deps.auth import classify_request, RateContext

    fa = FastAPI()
    fa.state.limiter = limiter
    fa.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(fa)
    fa.include_router(router)

    def _stub_classify():
        return RateContext(pool="web_session", key="user:test", tier="admin")

    fa.dependency_overrides[classify_request] = _stub_classify
    return fa


@pytest.fixture
def api_client(test_app):
    """Sync httpx client bound to the minimal test FastAPI app."""
    ac = httpx.AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    )

    class _SyncWrapper:
        def __init__(self, c):
            self._c = c
            self._loop = asyncio.new_event_loop()

        def __getattr__(self, n):
            m = getattr(self._c, n)
            return lambda *a, **k: self._loop.run_until_complete(m(*a, **k))

        def close(self):
            self._loop.run_until_complete(self._c.aclose())
            self._loop.close()

    w = _SyncWrapper(ac)
    yield w
    w.close()


# ---------------------------------------------------------------------------
# Import / router smoke (no fixtures needed)
# ---------------------------------------------------------------------------


def test_admin_billing_overview_router_importable():
    from carrac.fastapi_app.routers import admin_billing_overview_json

    assert admin_billing_overview_json.router is not None


def test_admin_billing_overview_router_has_get_route():
    from carrac.fastapi_app.routers.admin_billing_overview_json import router

    paths = {r.path for r in router.routes}
    assert "/api/admin/billing/overview" in paths


def test_prices_constant_values():
    from carrac.fastapi_app.routers.admin_billing_overview_json import PRICES

    assert PRICES["member"] == 3499
    assert PRICES["premium"] == 6900


# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------


def test_billing_overview_anonymous_redirects(test_app, api_client):
    """No auth dep override → require_admin redirects unauthenticated to /login."""
    resp = api_client.get("/api/admin/billing/overview", follow_redirects=False)
    assert resp.status_code == 302


def test_billing_overview_non_admin_returns_403(test_app, api_client):
    """Non-admin user → 403."""
    from fastapi import HTTPException
    from carrac.fastapi_app.deps.auth import require_admin

    def _forbidden():
        raise HTTPException(status_code=403, detail="Admin access required")

    test_app.dependency_overrides[require_admin] = _forbidden
    try:
        resp = api_client.get("/api/admin/billing/overview", follow_redirects=False)
        assert resp.status_code == 403
    finally:
        test_app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# Happy path — admin user, shape assertions
# ---------------------------------------------------------------------------


def test_billing_overview_returns_200_and_shape(test_app, api_client):
    """Admin GET → 200 + all required top-level keys present."""
    from carrac.fastapi_app.deps.auth import require_admin

    test_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        resp = api_client.get("/api/admin/billing/overview")
        assert resp.status_code == 200
        data = resp.json()

        for key in (
            "active_subscriptions",
            "mrr_usd",
            "arr_usd",
            "paddle_configured",
            "expiring_soon",
            "webhook_inbox",
            "topup_revenue_30d_usd",
            "totals",
        ):
            assert key in data, f"missing key: {key}"
    finally:
        test_app.dependency_overrides.pop(require_admin, None)


def test_billing_overview_active_subscriptions_shape(test_app, api_client):
    """active_subscriptions has member, premium, total; total == member + premium."""
    from carrac.fastapi_app.deps.auth import require_admin

    test_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        data = api_client.get("/api/admin/billing/overview").json()
        active = data["active_subscriptions"]
        assert "member" in active
        assert "premium" in active
        assert "total" in active
        assert active["total"] == active["member"] + active["premium"]
    finally:
        test_app.dependency_overrides.pop(require_admin, None)


def test_billing_overview_webhook_inbox_shape(test_app, api_client):
    """webhook_inbox has backlog, pending_race, recent (list)."""
    from carrac.fastapi_app.deps.auth import require_admin

    test_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        data = api_client.get("/api/admin/billing/overview").json()
        inbox = data["webhook_inbox"]
        assert "backlog" in inbox
        assert "pending_race" in inbox
        assert isinstance(inbox["recent"], list)
    finally:
        test_app.dependency_overrides.pop(require_admin, None)


def test_billing_overview_totals_shape(test_app, api_client):
    """totals has paying_users, free_users, trialing == 0."""
    from carrac.fastapi_app.deps.auth import require_admin

    test_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        data = api_client.get("/api/admin/billing/overview").json()
        totals = data["totals"]
        assert "paying_users" in totals
        assert "free_users" in totals
        assert "trialing" in totals
        assert totals["trialing"] == 0
    finally:
        test_app.dependency_overrides.pop(require_admin, None)


def test_billing_overview_arr_is_mrr_times_12(test_app, api_client):
    """ARR == MRR * 12 (within float tolerance)."""
    from carrac.fastapi_app.deps.auth import require_admin

    test_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        data = api_client.get("/api/admin/billing/overview").json()
        assert abs(data["arr_usd"] - data["mrr_usd"] * 12) < 0.01
    finally:
        test_app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# Active subscription seeding — MRR > 0 when member present
# ---------------------------------------------------------------------------


def test_billing_overview_mrr_positive_with_active_member(test_app, api_client, app):
    """Seed a member with future subscription_expires_at → member >= 1, mrr > 0."""
    import secrets
    from datetime import datetime, timedelta, timezone

    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_admin
    from carrac.fastapi_app.routers.admin_billing_overview_json import PRICES

    email = f"billing-test-{secrets.token_hex(4)}@test.dev"
    uid = user_db.create_user(email, "StrongPass!123", tos_accepted=True)
    assert uid, "Failed to create test user"
    user_db.set_user_tier(uid, "member")

    future_exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    try:
        from carrac.db_pool import adapt_query, get_conn

        with get_conn() as conn:
            conn.execute(
                adapt_query("UPDATE users SET subscription_expires_at = ? WHERE id = ?"),
                (future_exp, uid),
            )
            conn.commit()
    except Exception:
        pass  # if direct write fails we still assert below

    test_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        resp = api_client.get("/api/admin/billing/overview")
        assert resp.status_code == 200
        data = resp.json()

        assert data["active_subscriptions"]["member"] >= 1, (
            "Expected at least 1 active member subscription"
        )
        assert data["mrr_usd"] > 0, "Expected positive MRR with at least one member"
        assert data["mrr_usd"] >= PRICES["member"] / 100.0
    finally:
        test_app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# Graceful degradation on DB error
# ---------------------------------------------------------------------------


def test_billing_overview_degrades_on_db_error(test_app, api_client):
    """DB/_get_conn error → 200 with errors list, not 500."""
    from carrac.fastapi_app.deps.auth import require_admin
    from user_db import user_db as _udb

    test_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        with patch.object(_udb, "_get_conn", side_effect=Exception("simulated DB failure")):
            resp = api_client.get("/api/admin/billing/overview")
            assert resp.status_code == 200
            data = resp.json()
            assert "errors" in data
            assert len(data["errors"]) > 0
    finally:
        test_app.dependency_overrides.pop(require_admin, None)
