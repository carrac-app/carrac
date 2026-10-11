"""
Auth sweep — every protected endpoint rejects unauthenticated access.

Requests go through the composed ASGI app, so each path is answered by the
runtime that serves it in production (Flask or FastAPI).
"""

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client(app):  # noqa: ARG001 — needs the Flask app env first
    from carrac.asgi import asgi_app
    return TestClient(asgi_app, follow_redirects=False)


# Endpoints that require authentication (login_required or stronger).
# Format: (method, path, expected_status_without_auth)
PROTECTED_ENDPOINTS = [
    # Dashboard
    ('GET', '/chart/BTC/1D', 302),

    # Charts
    ('GET', '/api/chart/BTC', 302),

    # Assets
    ('GET', '/api/watchlist', 302),
    ('POST', '/api/watchlist/add', 302),
    ('GET', '/api/usage', 302),
    ('GET', '/api/user/me', 302),
    ('POST', '/api/user/me', 302),

    # Alerts
    ('GET', '/api/alerts', 302),
    ('POST', '/api/alerts', 302),
    ('GET', '/api/push/vapid-key', 302),
    # CSRF refuses the anonymous POST before the login check runs.
    ('POST', '/api/push/subscribe', 400),

    # Journal (tested in test_journal.py via the DB layer)

    # AI
    ('POST', '/api/ai/chat', 302),
    ('GET', '/api/ai/balance', 302),
    ('GET', '/api/ai/assets', 302),
    ('POST', '/api/ai/pattern-match', 302),


    # Widget
    ('GET', '/api/widget/tokens', 302),
    ('POST', '/api/widget/tokens', 302),

    # Admin (should reject non-admin)
    ('GET', '/api/security-status', 302),

    # Quant DB
    ('GET', '/api/quant/health', 302),
    ('GET', '/api/quant/metrics', 302),
    ('GET', '/api/quant/series', 302),
    ('GET', '/api/quant/zscores', 302),
    ('GET', '/api/quant/snapshot', 302),
    ('GET', '/api/quant/correlations', 302),
    ('GET', '/api/quant/leadlag', 302),
    ('GET', '/api/quant/regime', 302),
    ('GET', '/api/quant/conditional', 302),
    ('GET', '/api/quant/analogues', 302),
]

# Endpoints that should be PUBLIC (no auth required).
PUBLIC_ENDPOINTS = [
    ('GET', '/api/symbol-lookup?q=BTC', [200]),
    ('GET', '/health', [200]),
]


class TestAuthSweep:
    """Verify protected endpoints reject unauthenticated requests."""

    @pytest.mark.parametrize('method,path,expected', PROTECTED_ENDPOINTS,
                             ids=[f"{m} {p}" for m, p, _ in PROTECTED_ENDPOINTS])
    def test_unauthenticated_rejected(self, client, method, path, expected):
        """Protected endpoint should redirect to login without auth."""
        if method == 'GET':
            resp = client.get(path)
        elif method == 'POST':
            resp = client.post(path, json={})
        elif method == 'DELETE':
            resp = client.delete(path)
        else:
            resp = client.get(path)

        assert resp.status_code in (expected, 401, 403), \
            f"{method} {path}: got {resp.status_code}, expected {expected}/401/403"


class TestPublicEndpoints:
    """Verify public endpoints are accessible without auth."""

    @pytest.mark.parametrize('method,path,expected', PUBLIC_ENDPOINTS,
                             ids=[f"{m} {p}" for m, p, _ in PUBLIC_ENDPOINTS])
    def test_public_accessible(self, client, method, path, expected):
        resp = client.get(path)
        assert resp.status_code in expected, \
            f"{method} {path}: got {resp.status_code}, expected one of {expected}"
