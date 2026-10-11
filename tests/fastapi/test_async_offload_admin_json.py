"""Async-offload coverage for the admin/billing JSON routers.

Covers 4 sibling router modules whose ``async def`` handlers were making
blocking synchronous calls (DB reads/writes, external Paddle HTTP, email
sends) directly on the uvicorn event-loop thread:

* ``carrac/fastapi_app/routers/admin_billing_json.py``
* ``carrac/fastapi_app/routers/admin_billing_overview_json.py``
* ``carrac/fastapi_app/routers/admin_usage_json.py``
* ``carrac/fastapi_app/routers/admin_waitlist_json.py``

Each test monkeypatches the narrowest blocking function a handler calls
with a wrapper that records ``threading.get_ident()``, then asserts the
recorded thread differs from the test's own thread (the ``asgi``-style
sync test-client fixtures below run their event loop synchronously on
the calling test thread — see ``tests/fastapi/conftest.py::asgi_client``
for the same recipe). A blocking call still running in-process would
record the *same* ident as the test thread; ``asyncio.to_thread`` proves
the call actually left the event loop.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import Iterator

import httpx
import pytest
from httpx import ASGITransport


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _sync_client(fastapi_app) -> Iterator[httpx.Client]:
    """Sync httpx.Client wrapper — same recipe as conftest.asgi_client /
    the sibling per-router test files (test_admin_usage.py etc)."""
    ac = httpx.AsyncClient(
        transport=ASGITransport(app=fastapi_app), base_url="http://testserver"
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


def _admin_user(user_id: int = 1) -> dict:
    return {"id": user_id, "email": "admin@test.dev", "is_admin": True, "tier": "admin"}


def _ensure_ai_usage_log_table():
    """Create ai_usage_log + users + api_keys in the pool DB (carrac.db).

    Mirrors ``tests/fastapi/test_admin_usage.py::_ensure_ai_usage_log_table`` —
    admin_costs / admin_usage query these via ``carrac.db_pool.get_conn()``,
    which is a *separate* SQLite file from ``user_db`` in the test environment.
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


@contextmanager
def _recording_get_conn_patch(monkeypatch):
    """Patch ``carrac.db_pool.get_conn`` with a thread-ident-recording
    wrapper that still delegates to the real pooled connection.

    Handlers do ``from carrac.db_pool import get_conn`` *inside* the
    function body, so patching the module attribute before the request
    is picked up correctly on every call (no caching at import time).
    """
    import carrac.db_pool as db_pool_module

    real_get_conn = db_pool_module.get_conn
    holder = {"ident": None}

    @contextmanager
    def _wrapped():
        holder["ident"] = threading.get_ident()
        with real_get_conn() as conn:
            yield conn

    monkeypatch.setattr(db_pool_module, "get_conn", _wrapped)
    yield holder


def _recorder(orig_fn):
    """Wrap a sync function to record the calling thread's ident."""
    holder = {"ident": None}

    def _wrapped(*args, **kwargs):
        holder["ident"] = threading.get_ident()
        return orig_fn(*args, **kwargs)

    return _wrapped, holder


# ===========================================================================
# admin_billing_json.py
# ===========================================================================


@pytest.fixture
def billing_app(app):  # noqa: ARG001 — Flask app fixture sets up env + DB
    """Minimal FastAPI app mounting only admin_billing_json's router."""
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.admin_billing_json import router
    from carrac.fastapi_app.deps.auth import classify_request, RateContext

    _ensure_ai_usage_log_table()

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
def billing_client(billing_app):
    yield from _sync_client(billing_app)


def test_billing_portal_paddle_http_call_offloaded(billing_app, billing_client, app, monkeypatch):
    """POST /api/billing/portal — the flagged ~10s Paddle HTTP call must
    run off the event-loop thread (July architecture-sweep finding)."""
    import secrets
    from user_db import user_db
    from carrac.config import Config
    from carrac.fastapi_app.deps.auth import require_login

    email = f"paddle-{secrets.token_hex(4)}@test.dev"
    uid = user_db.create_user(email, "StrongPass!123", tos_accepted=True)
    assert uid
    user_db.set_paddle_customer_id(uid, "ctm_test123")

    monkeypatch.setattr(Config, "PADDLE_API_KEY", "test_paddle_key")

    holder = {"ident": None}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"urls": {"general": {"overview": "https://paddle.example/portal"}}}}

    def _fake_post(url, headers=None, timeout=None):
        holder["ident"] = threading.get_ident()
        return _FakeResp()

    monkeypatch.setattr("requests.post", _fake_post)

    billing_app.dependency_overrides[require_login] = lambda: {"id": uid, "email": email}
    try:
        resp = billing_client.post("/api/billing/portal")
        assert resp.status_code == 200, resp.text
        assert resp.json()["url"] == "https://paddle.example/portal"
        assert holder["ident"] is not None, "Paddle POST was never called"
        assert holder["ident"] != threading.get_ident(), (
            "Paddle HTTP call ran on the event-loop thread — not offloaded"
        )
    finally:
        billing_app.dependency_overrides.pop(require_login, None)


def test_billing_status_db_read_offloaded(billing_app, billing_client, app, monkeypatch):
    """GET /api/billing/status — user_db reads (DB shape) must be offloaded."""
    import secrets
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_login

    email = f"billstat-{secrets.token_hex(4)}@test.dev"
    uid = user_db.create_user(email, "StrongPass!123", tos_accepted=True)
    assert uid

    wrapped, holder = _recorder(user_db.get_ai_balance)
    monkeypatch.setattr(user_db, "get_ai_balance", wrapped)

    billing_app.dependency_overrides[require_login] = lambda: {"id": uid, "email": email}
    try:
        resp = billing_client.get("/api/billing/status")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "balance_cents" in data
        assert "tier" in data
        assert holder["ident"] is not None
        assert holder["ident"] != threading.get_ident(), (
            "billing_status DB read ran on the event-loop thread — not offloaded"
        )
    finally:
        billing_app.dependency_overrides.pop(require_login, None)


def test_security_status_offloaded(billing_app, billing_client, monkeypatch):
    """GET /api/security-status — whole-body closure (locks + DB read)."""
    from carrac.fastapi_app.deps.auth import require_admin

    with _recording_get_conn_patch(monkeypatch) as holder:
        billing_app.dependency_overrides[require_admin] = lambda: _admin_user()
        try:
            resp = billing_client.get("/api/security-status")
            assert resp.status_code == 200, resp.text
            assert holder["ident"] is not None
            assert holder["ident"] != threading.get_ident(), (
                "security_status DB read ran on the event-loop thread — not offloaded"
            )
        finally:
            billing_app.dependency_overrides.pop(require_admin, None)


def test_admin_costs_offloaded(billing_app, billing_client, monkeypatch):
    """GET /api/admin/costs — big multi-query DB closure."""
    from carrac.fastapi_app.deps.auth import require_admin

    with _recording_get_conn_patch(monkeypatch) as holder:
        billing_app.dependency_overrides[require_admin] = lambda: _admin_user()
        try:
            resp = billing_client.get("/api/admin/costs?period=7d")
            assert resp.status_code == 200, resp.text
            assert holder["ident"] is not None
            assert holder["ident"] != threading.get_ident(), (
                "admin_costs DB read ran on the event-loop thread — not offloaded"
            )
        finally:
            billing_app.dependency_overrides.pop(require_admin, None)


def test_submit_feedback_create_offloaded(billing_app, billing_client, app, monkeypatch):
    """POST /api/feedback — user_db.create_feedback (DB write) offloaded."""
    import secrets
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_login

    email = f"fb-{secrets.token_hex(4)}@test.dev"
    uid = user_db.create_user(email, "StrongPass!123", tos_accepted=True)
    assert uid

    wrapped, holder = _recorder(user_db.create_feedback)
    monkeypatch.setattr(user_db, "create_feedback", wrapped)
    # Suppress the best-effort notifier's own network calls.
    monkeypatch.setattr(
        "carrac.admin.routes._notify_feedback_submission", lambda *a, **k: None
    )

    billing_app.dependency_overrides[require_login] = lambda: {"id": uid, "email": email}
    try:
        resp = billing_client.post(
            "/api/feedback",
            json={"type": "bug", "description": "Something broke"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        assert holder["ident"] is not None
        assert holder["ident"] != threading.get_ident(), (
            "create_feedback ran on the event-loop thread — not offloaded"
        )
    finally:
        billing_app.dependency_overrides.pop(require_login, None)


def test_topup_eligibility_offloaded(billing_app, billing_client, app, monkeypatch):
    """GET /api/billing/topup-check — _tier_allows_topup (DB lookup) offloaded."""
    import secrets
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_login
    import carrac.billing.routes as billing_routes

    email = f"topup-{secrets.token_hex(4)}@test.dev"
    uid = user_db.create_user(email, "StrongPass!123", tos_accepted=True)
    assert uid

    wrapped, holder = _recorder(billing_routes._tier_allows_topup)
    monkeypatch.setattr(billing_routes, "_tier_allows_topup", wrapped)

    billing_app.dependency_overrides[require_login] = lambda: {"id": uid, "email": email}
    try:
        resp = billing_client.get("/api/billing/topup-check")
        assert resp.status_code in (200, 403), resp.text
        assert holder["ident"] is not None
        assert holder["ident"] != threading.get_ident(), (
            "_tier_allows_topup ran on the event-loop thread — not offloaded"
        )
    finally:
        billing_app.dependency_overrides.pop(require_login, None)


# ===========================================================================
# admin_billing_overview_json.py
# ===========================================================================


@pytest.fixture
def overview_app(app):  # noqa: ARG001
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.admin_billing_overview_json import router
    from carrac.fastapi_app.deps.auth import classify_request, RateContext

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
def overview_client(overview_app):
    yield from _sync_client(overview_app)


def test_billing_overview_subscription_and_webhook_sections_offloaded(
    overview_app, overview_client, monkeypatch
):
    """Sections 1 (subscription counts) + 3 (webhook inbox) both call
    ``user_db._get_conn()`` — patch it once to prove both closures offload."""
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_admin

    wrapped, holder = _recorder(user_db._get_conn)
    monkeypatch.setattr(user_db, "_get_conn", wrapped)

    overview_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        resp = overview_client.get("/api/admin/billing/overview")
        assert resp.status_code == 200, resp.text
        assert holder["ident"] is not None
        assert holder["ident"] != threading.get_ident(), (
            "billing_overview _get_conn() ran on the event-loop thread — not offloaded"
        )
    finally:
        overview_app.dependency_overrides.pop(require_admin, None)


def test_billing_overview_expiring_soon_offloaded(overview_app, overview_client, monkeypatch):
    """Section 2 (expiring soon) — ``get_expiring_subscriptions`` offloaded."""
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_admin

    wrapped, holder = _recorder(user_db.get_expiring_subscriptions)
    monkeypatch.setattr(user_db, "get_expiring_subscriptions", wrapped)

    overview_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        resp = overview_client.get("/api/admin/billing/overview")
        assert resp.status_code == 200, resp.text
        assert holder["ident"] is not None
        assert holder["ident"] != threading.get_ident(), (
            "get_expiring_subscriptions ran on the event-loop thread — not offloaded"
        )
    finally:
        overview_app.dependency_overrides.pop(require_admin, None)


def test_billing_overview_topup_revenue_offloaded(overview_app, overview_client, monkeypatch):
    """Section 4 (top-up revenue) — ``get_platform_economics`` offloaded."""
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_admin

    wrapped, holder = _recorder(user_db.get_platform_economics)
    monkeypatch.setattr(user_db, "get_platform_economics", wrapped)

    overview_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        resp = overview_client.get("/api/admin/billing/overview")
        assert resp.status_code == 200, resp.text
        assert holder["ident"] is not None
        assert holder["ident"] != threading.get_ident(), (
            "get_platform_economics ran on the event-loop thread — not offloaded"
        )
    finally:
        overview_app.dependency_overrides.pop(require_admin, None)


# ===========================================================================
# admin_usage_json.py
# ===========================================================================


@pytest.fixture
def usage_app(app):  # noqa: ARG001
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.admin_usage_json import router
    from carrac.fastapi_app.deps.auth import classify_request, RateContext

    _ensure_ai_usage_log_table()

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
def usage_client(usage_app):
    yield from _sync_client(usage_app)


def test_admin_usage_cross_user_summary_offloaded(usage_app, usage_client, monkeypatch):
    """GET /api/admin/usage (no ?user_id) — cross-user DB closure offloaded."""
    from carrac.fastapi_app.deps.auth import require_admin

    with _recording_get_conn_patch(monkeypatch) as holder:
        usage_app.dependency_overrides[require_admin] = lambda: _admin_user()
        try:
            resp = usage_client.get("/api/admin/usage?period=7d")
            assert resp.status_code == 200, resp.text
            assert holder["ident"] is not None
            assert holder["ident"] != threading.get_ident(), (
                "admin_usage cross-user DB read ran on the event-loop thread — not offloaded"
            )
        finally:
            usage_app.dependency_overrides.pop(require_admin, None)


def test_admin_usage_user_drill_usage_payload_offloaded(usage_app, usage_client, monkeypatch):
    """GET /api/admin/usage?user_id=N — ``_user_drill``'s Redis/DB usage-payload
    block (get_user_by_id + build_usage_response) offloaded."""
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_admin

    wrapped, holder = _recorder(user_db.get_user_by_id)
    monkeypatch.setattr(user_db, "get_user_by_id", wrapped)

    usage_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        resp = usage_client.get("/api/admin/usage?user_id=9999&period=7d")
        assert resp.status_code == 200, resp.text
        assert holder["ident"] is not None
        assert holder["ident"] != threading.get_ident(), (
            "_user_drill usage-payload block ran on the event-loop thread — not offloaded"
        )
    finally:
        usage_app.dependency_overrides.pop(require_admin, None)


def test_admin_usage_user_drill_api_keys_offloaded(usage_app, usage_client, monkeypatch):
    """GET /api/admin/usage?user_id=N — ``_user_drill``'s api_keys block offloaded."""
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_admin

    wrapped, holder = _recorder(user_db.list_api_keys)
    monkeypatch.setattr(user_db, "list_api_keys", wrapped)

    usage_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        resp = usage_client.get("/api/admin/usage?user_id=9999&period=7d")
        assert resp.status_code == 200, resp.text
        assert holder["ident"] is not None
        assert holder["ident"] != threading.get_ident(), (
            "_user_drill api_keys block ran on the event-loop thread — not offloaded"
        )
    finally:
        usage_app.dependency_overrides.pop(require_admin, None)


# ===========================================================================
# admin_waitlist_json.py
# ===========================================================================


@pytest.fixture
def waitlist_app(app):  # noqa: ARG001
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware
    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.deps.auth import RateContext, classify_request
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.admin_waitlist_json import router as wl_router

    ta = FastAPI()
    ta.state.limiter = limiter
    ta.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(ta)
    ta.include_router(wl_router)
    ta.dependency_overrides[classify_request] = lambda: RateContext(
        pool="web_session", key="user:test", tier="admin"
    )
    return ta


@pytest.fixture
def waitlist_client(waitlist_app):
    yield from _sync_client(waitlist_app)


def test_admin_waitlist_get_offloaded(waitlist_app, waitlist_client, monkeypatch):
    """GET /api/admin/waitlist — count_waitlist calls offloaded."""
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_admin

    wrapped, holder = _recorder(user_db.count_waitlist)
    monkeypatch.setattr(user_db, "count_waitlist", wrapped)

    waitlist_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        resp = waitlist_client.get("/api/admin/waitlist")
        assert resp.status_code == 200, resp.text
        assert holder["ident"] is not None
        assert holder["ident"] != threading.get_ident(), (
            "admin_waitlist_get count_waitlist ran on the event-loop thread — not offloaded"
        )
    finally:
        waitlist_app.dependency_overrides.pop(require_admin, None)


def test_admin_waitlist_release_dry_run_offloaded(waitlist_app, waitlist_client, monkeypatch):
    """POST /api/admin/waitlist/release (dry_run) — pick_next_waitlist offloaded."""
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_admin

    user_db.add_to_waitlist("offload_dry@test.dev", source="test")

    wrapped, holder = _recorder(user_db.pick_next_waitlist)
    monkeypatch.setattr(user_db, "pick_next_waitlist", wrapped)

    waitlist_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        resp = waitlist_client.post(
            "/api/admin/waitlist/release", json={"count": 1, "dry_run": True}
        )
        assert resp.status_code == 200, resp.text
        assert holder["ident"] is not None
        assert holder["ident"] != threading.get_ident(), (
            "pick_next_waitlist ran on the event-loop thread — not offloaded"
        )
    finally:
        waitlist_app.dependency_overrides.pop(require_admin, None)


def test_admin_waitlist_release_real_loop_offloaded(waitlist_app, waitlist_client, monkeypatch):
    """POST /api/admin/waitlist/release (dry_run=False) — the per-pick invite
    loop (email send + DB writes) runs as ONE offloaded closure."""
    from user_db import user_db
    from carrac.fastapi_app.deps.auth import require_admin

    email = "offload_real@test.dev"
    user_db.add_to_waitlist(email, source="test")

    holder = {"ident": None}

    def _fake_send_invite_email(addr, code, expires_days=14):
        holder["ident"] = threading.get_ident()
        return True

    monkeypatch.setattr("carrac.email.send_invite_email", _fake_send_invite_email, raising=False)
    # Suppress CRM / funnel side-effects (network calls not under test here).
    try:
        import carrac.waitlist_release as _wlr
        monkeypatch.setattr(_wlr, "_espo_mark_invited", lambda email, cohort: None)
    except (ImportError, AttributeError):
        pass

    waitlist_app.dependency_overrides[require_admin] = lambda: _admin_user(user_id=1)
    try:
        resp = waitlist_client.post(
            "/api/admin/waitlist/release", json={"count": 1, "dry_run": False}
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["released"]) >= 1
        assert holder["ident"] is not None, "send_invite_email was never called"
        assert holder["ident"] != threading.get_ident(), (
            "waitlist release invite loop ran on the event-loop thread — not offloaded"
        )
    finally:
        waitlist_app.dependency_overrides.pop(require_admin, None)
