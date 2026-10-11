"""Pins POST /api/journal/goals serializing the goals dict.

``ContentDatabase.upsert_goals`` binds its
``goals`` argument straight into SQL (``goals: str``), so the endpoint must
pre-serialize with ``json.dumps``: passing ``body.goals`` (a Pydantic ``dict``) raw
makes SQLite raise ``ProgrammingError: type 'dict' is not supported`` and
psycopg2 equally cannot adapt ``dict``, so every call would 500.

This test drives the endpoint end-to-end against the REAL ContentDatabase
(no stubbing) and asserts the row round-trips as parseable JSON text.
"""

from __future__ import annotations

import json

import pytest

from carrac.content_db import get_content_db


@pytest.fixture
def login_as():
    """Override ``journal_user`` on the live FastAPI singleton for one test."""
    from carrac.fastapi_app import fastapi_app
    from carrac.fastapi_app.routers.journal import journal_user

    def _do(user: dict) -> dict:
        fastapi_app.dependency_overrides[journal_user] = lambda: user
        return user

    yield _do
    fastapi_app.dependency_overrides.pop(journal_user, None)


def test_set_goals_persists_json_text(asgi_client, login_as):
    login_as({"id": 4242, "email": "goals@example.com", "is_admin": False,
              "tier": "member"})
    goals = {"target_r": 10, "max_trades_per_day": 3, "notes": "stay small"}

    resp = asgi_client.post(
        "/api/journal/goals",
        json={"month": "2026-07", "goals": goals},
    )
    assert resp.status_code == 200, resp.text
    goal_id = resp.json()["id"]
    assert goal_id

    db = get_content_db()
    conn = db._get_conn()
    row = conn.execute(
        "SELECT goals FROM journal_goals WHERE user_id = ? AND month = ?",
        (4242, "2026-07"),
    ).fetchone()
    assert row is not None
    stored = row["goals"]
    assert isinstance(stored, str), (
        f"goals column must store JSON text, got {type(stored).__name__}"
    )
    assert json.loads(stored) == goals


def test_set_goals_upsert_updates_existing_row(asgi_client, login_as):
    login_as({"id": 4243, "email": "goals2@example.com", "is_admin": False,
              "tier": "member"})

    first = asgi_client.post(
        "/api/journal/goals",
        json={"month": "2026-07", "goals": {"target_r": 5}},
    )
    assert first.status_code == 200, first.text

    second = asgi_client.post(
        "/api/journal/goals",
        json={"month": "2026-07", "goals": {"target_r": 8}},
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]

    db = get_content_db()
    conn = db._get_conn()
    row = conn.execute(
        "SELECT goals FROM journal_goals WHERE user_id = ? AND month = ?",
        (4243, "2026-07"),
    ).fetchone()
    assert json.loads(row["goals"]) == {"target_r": 8}
