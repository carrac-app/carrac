"""Drawing update answers 200 when the row carries real datetimes.

Postgres returns ``drawings.created_at`` / ``updated_at`` as ``datetime``;
SQLite returns strings, so the default suite cannot see a route that hands
the re-read row straight to ``JSONResponse``. The update is saved first, so
the client would see a failure for a change that was in fact stored.
``_pg_style_datetimes`` makes the real ``get_drawing_by_id`` return what
psycopg2 returns; the route is untouched.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from carrac.content_db import ContentDatabase


@pytest.fixture
def _pg_style_datetimes(monkeypatch):
    orig = ContentDatabase.get_drawing_by_id

    def as_psycopg2(self, drawing_id, user_id=None):
        row = orig(self, drawing_id, user_id=user_id)
        if row:
            for col in ("created_at", "updated_at"):
                value = row.get(col)
                if isinstance(value, str) and value:
                    row[col] = datetime.fromisoformat(value)
        return row

    monkeypatch.setattr(ContentDatabase, "get_drawing_by_id", as_psycopg2)


def test_update_answers_200_with_datetime_columns(asgi_client, member_key, _pg_style_datetimes):
    headers, _uid = member_key
    created = asgi_client.post(
        "/api/drawings",
        json={"asset": "BTCUSDT", "timeframe": "1D", "drawing_type": "horizontal_line",
              "price_level": 94000.0, "color": "#26a69a"},
        headers=headers,
    )
    assert created.status_code == 201, created.text

    resp = asgi_client.put(
        f"/api/drawings/{created.json()['drawing_id']}",
        json={"label": "resistance"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    drawing = resp.json()["drawing"]
    assert drawing["label"] == "resistance"
    assert datetime.fromisoformat(drawing["created_at"])
