"""Admin waitlist management JSON endpoints — test suite.

Covers:
- Routing entry assertions (the paths are listed in carrac.asgi).
- Router import smoke test.
- Pydantic model validation.
- GET /api/admin/waitlist: counts + entries for admin, 403 for non-admin.
- POST /api/admin/waitlist/release with dry_run=True (default): does NOT send
  emails, does NOT mark rows as invited, returns preview picks.
- POST /api/admin/waitlist/release with dry_run=False: actually invites (mocked).
"""

from __future__ import annotations

import asyncio
from typing import Iterator

import httpx
import pytest
from httpx import ASGITransport


# ---------------------------------------------------------------------------
# Routing table assertions — must be wired into asgi.py
# ---------------------------------------------------------------------------


def test_waitlist_get_in_migrated_paths():
    from carrac.asgi import MIGRATED_PATHS

    assert "/api/admin/waitlist" in MIGRATED_PATHS, (
        "GET /api/admin/waitlist must be in MIGRATED_PATHS"
    )


def test_waitlist_release_post_in_migrated_write_paths():
    from carrac.asgi import MIGRATED_WRITE_PATHS

    assert ("/api/admin/waitlist/release", frozenset({"POST"})) in MIGRATED_WRITE_PATHS, (
        "POST /api/admin/waitlist/release must be in MIGRATED_WRITE_PATHS"
    )


# ---------------------------------------------------------------------------
# Router import smoke
# ---------------------------------------------------------------------------


def test_admin_waitlist_router_importable():
    from carrac.fastapi_app.routers import admin_waitlist_json

    assert admin_waitlist_json.router is not None


# ---------------------------------------------------------------------------
# Pydantic model validation
# ---------------------------------------------------------------------------


def test_release_request_defaults():
    from carrac.fastapi_app.routers.admin_waitlist_json import WaitlistReleaseRequest

    body = WaitlistReleaseRequest(count=5)
    assert body.count == 5
    assert body.dry_run is True  # safe default
    assert body.cohort is None


def test_release_request_explicit_fields():
    from carrac.fastapi_app.routers.admin_waitlist_json import WaitlistReleaseRequest

    body = WaitlistReleaseRequest(count=10, cohort="wave1", dry_run=False)
    assert body.count == 10
    assert body.cohort == "wave1"
    assert body.dry_run is False


def test_release_request_count_bounds():
    import pydantic

    from carrac.fastapi_app.routers.admin_waitlist_json import WaitlistReleaseRequest

    with pytest.raises(pydantic.ValidationError):
        WaitlistReleaseRequest(count=0)
    with pytest.raises(pydantic.ValidationError):
        WaitlistReleaseRequest(count=101)


def test_release_request_extra_forbid():
    import pydantic

    from carrac.fastapi_app.routers.admin_waitlist_json import WaitlistReleaseRequest

    with pytest.raises(pydantic.ValidationError):
        WaitlistReleaseRequest(count=5, surprise="bad")


# ---------------------------------------------------------------------------
# Minimal test app factory — mirrors test_journal_ai_gate.py pattern
# ---------------------------------------------------------------------------


@pytest.fixture
def waitlist_app(app):  # noqa: ARG001 — Flask app fixture sets up env
    """Minimal FastAPI app with only the admin_waitlist router + required middleware."""
    from fastapi import FastAPI
    from slowapi.middleware import SlowAPIMiddleware

    from carrac.fastapi_app import exception_handlers
    from carrac.fastapi_app.deps.auth import RateContext, classify_request
    from carrac.fastapi_app.limiter import limiter
    from carrac.fastapi_app.routers.admin_waitlist_json import router as wl_router

    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.add_middleware(SlowAPIMiddleware)
    exception_handlers.install(test_app)
    test_app.include_router(wl_router)

    def _stub_classify():
        return RateContext(pool="web_session", key="user:test", tier="admin")

    test_app.dependency_overrides[classify_request] = _stub_classify
    return test_app


@pytest.fixture
def waitlist_client(waitlist_app) -> Iterator[httpx.Client]:
    """Sync httpx.Client driving the minimal waitlist FastAPI app."""
    ac = httpx.AsyncClient(
        transport=ASGITransport(app=waitlist_app), base_url="http://testserver"
    )

    class _Sync:
        def __init__(self, c: httpx.AsyncClient) -> None:
            self._c = c
            self._loop = asyncio.new_event_loop()

        def __getattr__(self, name: str):
            method = getattr(self._c, name)

            def call(*args, **kwargs):
                return self._loop.run_until_complete(method(*args, **kwargs))

            return call

        def close(self) -> None:
            self._loop.run_until_complete(self._c.aclose())
            self._loop.close()

    w = _Sync(ac)
    yield w
    w.close()


def _admin_user(user_id: int = 999) -> dict:
    return {"id": user_id, "email": "admin@test.dev", "is_admin": True, "tier": "admin"}


# ---------------------------------------------------------------------------
# GET /api/admin/waitlist
# ---------------------------------------------------------------------------


def test_get_waitlist_anonymous_redirects(waitlist_client, waitlist_app):
    """No session → 302 redirect (require_admin raises redirect)."""
    # With no override, require_admin will see no session → redirect.
    from carrac.fastapi_app.deps.auth import require_admin

    waitlist_app.dependency_overrides.pop(require_admin, None)
    resp = waitlist_client.get("/api/admin/waitlist", follow_redirects=False)
    assert resp.status_code == 302


def test_get_waitlist_non_admin_403(waitlist_client, waitlist_app):
    """Session without admin flag → 403."""
    from fastapi import HTTPException

    from carrac.fastapi_app.deps.auth import require_admin

    def _non_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    waitlist_app.dependency_overrides[require_admin] = _non_admin
    try:
        resp = waitlist_client.get("/api/admin/waitlist", follow_redirects=False)
        assert resp.status_code == 403
    finally:
        waitlist_app.dependency_overrides.pop(require_admin, None)


def test_get_waitlist_admin_returns_counts_and_entries(waitlist_client, waitlist_app):
    """Admin GET /api/admin/waitlist returns counts dict + entries list."""
    from user_db import user_db

    from carrac.fastapi_app.deps.auth import require_admin

    admin_user = _admin_user()
    waitlist_app.dependency_overrides[require_admin] = lambda: admin_user

    # Seed two waiting rows.
    user_db.add_to_waitlist("wtest1@test.dev", source="test")
    user_db.add_to_waitlist("wtest2@test.dev", source="test")

    try:
        resp = waitlist_client.get("/api/admin/waitlist")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "counts" in data, f"'counts' missing from response: {data}"
        assert "entries" in data, f"'entries' missing from response: {data}"
        counts = data["counts"]
        assert "waiting" in counts
        assert "invited" in counts
        assert "registered" in counts
        assert "total" in counts
        assert counts["total"] >= 2
        assert isinstance(data["entries"], list)
    finally:
        waitlist_app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# POST /api/admin/waitlist/release  — dry_run=True (safe default)
# ---------------------------------------------------------------------------


def test_release_dry_run_does_not_send_email_or_mark_invited(
    waitlist_client, waitlist_app, monkeypatch
):
    """POST with dry_run=True returns preview without any side-effects.

    Asserts:
    - No email send function is called.
    - count_waitlist('invited') is unchanged.
    - Response contains dry_run=True and a released list.
    """
    from user_db import user_db

    from carrac.fastapi_app.deps.auth import require_admin

    admin_user = _admin_user()
    waitlist_app.dependency_overrides[require_admin] = lambda: admin_user

    user_db.add_to_waitlist("dryrun_a@test.dev", source="test")
    user_db.add_to_waitlist("dryrun_b@test.dev", source="test")

    invited_before = user_db.count_waitlist("invited")
    email_calls: list[str] = []

    def _fake_send_email(addr, code, expires_days=14):
        email_calls.append(addr)
        return True

    monkeypatch.setattr("carrac.email.send_invite_email", _fake_send_email, raising=False)

    try:
        resp = waitlist_client.post(
            "/api/admin/waitlist/release",
            json={"count": 2, "dry_run": True},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["dry_run"] is True
        assert data["requested"] == 2
        assert isinstance(data["released"], list)
        assert isinstance(data["errors"], list)

        # No emails must have been sent.
        assert email_calls == [], f"Expected no emails sent in dry run, got: {email_calls}"

        # Invited count must be unchanged.
        invited_after = user_db.count_waitlist("invited")
        assert invited_after == invited_before, (
            f"invited count changed in dry run: {invited_before} -> {invited_after}"
        )
    finally:
        waitlist_app.dependency_overrides.pop(require_admin, None)


def test_release_dry_run_true_by_default(waitlist_client, waitlist_app, monkeypatch):
    """Omitting dry_run defaults to True — no emails, no DB writes."""
    from user_db import user_db

    from carrac.fastapi_app.deps.auth import require_admin

    admin_user = _admin_user()
    waitlist_app.dependency_overrides[require_admin] = lambda: admin_user

    user_db.add_to_waitlist("default_dry@test.dev", source="test")
    invited_before = user_db.count_waitlist("invited")

    email_calls: list[str] = []

    def _fake_send_email(addr, code, expires_days=14):
        email_calls.append(addr)
        return True

    monkeypatch.setattr("carrac.email.send_invite_email", _fake_send_email, raising=False)

    try:
        resp = waitlist_client.post(
            "/api/admin/waitlist/release",
            json={"count": 1},  # no dry_run field → defaults to True
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["dry_run"] is True, "Default dry_run should be True"
        assert email_calls == [], "Default dry_run must not send emails"
        assert user_db.count_waitlist("invited") == invited_before
    finally:
        waitlist_app.dependency_overrides.pop(require_admin, None)


# ---------------------------------------------------------------------------
# POST /api/admin/waitlist/release  — dry_run=False (live invites, mocked)
# ---------------------------------------------------------------------------


def test_release_real_invites_marks_invited(waitlist_client, waitlist_app, monkeypatch):
    """POST with dry_run=False sends email and marks the row as invited."""
    from user_db import user_db

    from carrac.fastapi_app.deps.auth import require_admin

    admin_user = _admin_user(user_id=1)
    waitlist_app.dependency_overrides[require_admin] = lambda: admin_user

    email = "real_invite@test.dev"
    user_db.add_to_waitlist(email, source="test")
    invited_before = user_db.count_waitlist("invited")

    email_calls: list[str] = []

    def _fake_send_email(addr, code, expires_days=14):
        email_calls.append(addr)
        return True

    monkeypatch.setattr("carrac.email.send_invite_email", _fake_send_email, raising=False)
    monkeypatch.setattr(user_db, "generate_invite_code", lambda created_by, expires_days=14: "TESTCODE123")

    # Suppress CRM / funnel side-effects.
    try:
        import carrac.waitlist_release as _wlr
        monkeypatch.setattr(_wlr, "_espo_mark_invited", lambda email, cohort: None)
    except (ImportError, AttributeError):
        pass

    try:
        resp = waitlist_client.post(
            "/api/admin/waitlist/release",
            json={"count": 1, "dry_run": False},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["dry_run"] is False
        assert len(data["released"]) >= 1
        assert len(email_calls) >= 1, "Expected at least one email to be sent"
        assert user_db.count_waitlist("invited") > invited_before
    finally:
        waitlist_app.dependency_overrides.pop(require_admin, None)


def test_release_anonymous_redirects(waitlist_client, waitlist_app):
    """Anonymous POST must 302 redirect."""
    from carrac.fastapi_app.deps.auth import require_admin

    waitlist_app.dependency_overrides.pop(require_admin, None)
    resp = waitlist_client.post(
        "/api/admin/waitlist/release",
        json={"count": 5},
        follow_redirects=False,
    )
    assert resp.status_code == 302
