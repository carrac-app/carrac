"""The shared ``login_as`` fixture must authenticate journal routes.

Journal routes depend on ``journal_user`` (session OR per-user API key), not
on ``require_login``, so a fixture that only overrides ``require_login``
leaves them answering 401 to a logged-in test client.
"""

from __future__ import annotations


def test_login_as_authenticates_journal_routes(asgi_client, login_as):
    user = {"id": 5151, "email": "login-as@example.com", "is_admin": False,
            "tier": "member"}
    with login_as(user):
        resp = asgi_client.post(
            "/api/journal/goals",
            json={"month": "2026-07", "goals": {"target_r": 4}},
        )
    assert resp.status_code == 200, resp.text


def test_journal_route_is_401_without_login(asgi_client):
    resp = asgi_client.post(
        "/api/journal/goals",
        json={"month": "2026-07", "goals": {"target_r": 4}},
    )
    assert resp.status_code == 401, resp.text
