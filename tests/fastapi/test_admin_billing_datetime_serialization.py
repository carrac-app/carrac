"""Security and billing status answer 200 when their rows carry real datetimes.

Postgres returns ``reports.created_at`` and ``users.subscription_expires_at``
as ``datetime`` objects; SQLite returns strings, so the default suite cannot
see a route that hands them straight to ``JSONResponse``. Each test feeds the
route the value psycopg2 returns, one layer below the route.
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import datetime, timezone

from tests.fastapi.test_async_offload_admin_json import (  # noqa: F401
    _admin_user,
    billing_app,
    billing_client,
)

_REPORT_AT = datetime(2026, 1, 2, 3, 4, 5)
_EXPIRES_AT = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)


class _Rows:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _LatestReportConn:
    def execute(self, *_args, **_kwargs):
        return _Rows({"id": 7, "created_at": _REPORT_AT})


def test_security_status_serializes_report_datetime(billing_app, billing_client, monkeypatch):  # noqa: F811
    from carrac.fastapi_app.deps.auth import require_admin

    @contextmanager
    def _pg_conn():
        yield _LatestReportConn()

    monkeypatch.setattr("carrac.db_pool.get_conn", _pg_conn)
    billing_app.dependency_overrides[require_admin] = lambda: _admin_user()
    try:
        resp = billing_client.get("/api/security-status")
    finally:
        billing_app.dependency_overrides.pop(require_admin, None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["last_report_id"] == 7
    assert datetime.fromisoformat(resp.json()["last_report_at"]) == _REPORT_AT


def test_billing_status_serializes_subscription_expiry(billing_app, billing_client, app, monkeypatch):  # noqa: F811
    from carrac.fastapi_app.deps.auth import require_login
    from user_db import user_db

    email = f"expiry-{secrets.token_hex(4)}@test.dev"
    uid = user_db.create_user(email, "StrongPass!123", tos_accepted=True)
    assert uid
    monkeypatch.setattr(
        user_db,
        "check_subscription_expiry",
        lambda _uid: {"tier": "member", "expires_at": _EXPIRES_AT, "expired": False},
    )

    billing_app.dependency_overrides[require_login] = lambda: {"id": uid, "email": email}
    try:
        resp = billing_client.get("/api/billing/status")
    finally:
        billing_app.dependency_overrides.pop(require_login, None)

    assert resp.status_code == 200, resp.text
    assert datetime.fromisoformat(resp.json()["expires_at"]) == _EXPIRES_AT
