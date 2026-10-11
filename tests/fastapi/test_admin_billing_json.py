"""Admin/billing JSON endpoints tests.

Covers:
- Strangler routing entries (MIGRATED_PATHS / MIGRATED_WRITE_PATHS).
- Pydantic body models for /api/feedback + /api/admin/assets.
- Smoke tests via ``asgi_client``.
"""

from __future__ import annotations

import pydantic
import pytest

from carrac.asgi import (
    MIGRATED_PATHS,
    MIGRATED_WRITE_PATHS,
)


# ---------------------------------------------------------------------------
# Routing entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/security-status",
        "/api/admin/costs",
        "/api/admin/assets",
        "/api/billing/topup-check",
        "/api/billing/status",
    ],
)
def test_get_paths_in_migrated_paths(path):
    assert path in MIGRATED_PATHS, f"GET {path} should be in MIGRATED_PATHS"


@pytest.mark.parametrize(
    "path,methods",
    [
        ("/api/csp-report", frozenset({"POST"})),
        ("/api/feedback", frozenset({"POST"})),
        ("/api/admin/assets", frozenset({"POST"})),
        ("/api/billing/portal", frozenset({"POST"})),
    ],
)
def test_post_paths_in_migrated_write_paths(path, methods):
    assert (path, methods) in MIGRATED_WRITE_PATHS, (
        f"({path}, {methods}) should be in MIGRATED_WRITE_PATHS"
    )


def test_admin_assets_delete_in_migrated_write_prefixes():
    """``DELETE /api/admin/assets/<pair>`` lives behind a prefix shim
    (slash-bearing path param)."""
    from carrac.asgi import MIGRATED_WRITE_PREFIXES

    matched = [
        p for p, m in MIGRATED_WRITE_PREFIXES
        if p == "/api/admin/assets/" and "DELETE" in m
    ]
    assert matched, "/api/admin/assets/ DELETE prefix entry missing"


# ---------------------------------------------------------------------------
# Pydantic body models
# ---------------------------------------------------------------------------


def test_feedback_request_minimal_valid():
    from carrac.fastapi_app.models.admin_billing import FeedbackRequest

    body = FeedbackRequest(type="bug", description="Something broke")
    assert body.type == "bug"
    assert body.description == "Something broke"


def test_feedback_request_extra_forbid():
    from carrac.fastapi_app.models.admin_billing import FeedbackRequest

    with pytest.raises(pydantic.ValidationError):
        FeedbackRequest(type="bug", description="x", surprise_field="bad")


def test_add_asset_request_defaults():
    from carrac.fastapi_app.models.admin_billing import AddAssetRequest

    body = AddAssetRequest(pair="SOL/1D")
    assert body.pair == "SOL/1D"
    assert body.category is None
    assert body.ratio_eligible is False


def test_add_asset_request_extra_forbid():
    from carrac.fastapi_app.models.admin_billing import AddAssetRequest

    with pytest.raises(pydantic.ValidationError):
        AddAssetRequest(pair="SOL/1D", weird="no")


# ---------------------------------------------------------------------------
# Router import smoke
# ---------------------------------------------------------------------------


def test_admin_billing_router_importable():
    from carrac.fastapi_app.routers import admin_billing_json

    assert admin_billing_json.router is not None


# ---------------------------------------------------------------------------
# Integration tests via asgi_client
# ---------------------------------------------------------------------------



def test_csp_report_accepts_browser_payload(asgi_client):
    """CSP reports come with content-type=application/csp-report and no auth."""
    payload = {
        "csp-report": {
            "violated-directive": "script-src 'self'",
            "blocked-uri": "https://evil.example/x.js",
            "document-uri": "https://carrac.cc/chart",
        }
    }
    resp = asgi_client.post(
        "/api/csp-report",
        content=__import__("json").dumps(payload),
        headers={"content-type": "application/csp-report"},
    )
    assert resp.status_code == 204



def test_feedback_requires_login(asgi_client):
    """Anonymous POST /api/feedback → 302 redirect to /login (Flask parity)."""
    resp = asgi_client.post(
        "/api/feedback",
        json={"type": "bug", "description": "Test"},
        follow_redirects=False,
    )
    assert resp.status_code == 302



def test_billing_status_requires_login(asgi_client):
    """Anonymous GET /api/billing/status → 302 redirect."""
    resp = asgi_client.get("/api/billing/status", follow_redirects=False)
    assert resp.status_code == 302



def test_security_status_requires_admin(asgi_client):
    """Anonymous GET /api/security-status → 302 redirect."""
    resp = asgi_client.get("/api/security-status", follow_redirects=False)
    assert resp.status_code == 302
