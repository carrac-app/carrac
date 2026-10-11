"""Journal close and update answer 200 when the row carries real datetimes.

Postgres returns ``opened_at`` / ``closed_at`` as ``datetime``; SQLite returns
strings, so the default suite cannot see a route that hands the row straight
to ``JSONResponse``. The route then saves the change and answers 500 while
serializing. ``_pg_style_datetimes`` makes the real ``get_journal_entry``
return what psycopg2 returns; the routes themselves are untouched.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from carrac.content_db import ContentDatabase, get_content_db
from tests.fastapi.test_async_offload_journal import _user, login_as  # noqa: F401


@pytest.fixture
def _pg_style_datetimes(monkeypatch):
    orig = ContentDatabase.get_journal_entry

    def as_psycopg2(self, journal_id):
        row = orig(self, journal_id)
        if row:
            for col in ("opened_at", "closed_at"):
                value = row.get(col)
                if isinstance(value, str) and value:
                    row[col] = datetime.fromisoformat(value)
        return row

    monkeypatch.setattr(ContentDatabase, "get_journal_entry", as_psycopg2)


def _open_trade(uid: int) -> int:
    return get_content_db().log_trade_open(
        user_id=uid, asset="ETH", timeframe="1D", direction="long", entry_price=2000.0,
    )


def test_close_answers_200_with_datetime_columns(asgi_client, login_as, _pg_style_datetimes, monkeypatch):  # noqa: F811
    monkeypatch.setattr("carrac.integrations.on_trade_closed", lambda *a, **kw: None)
    uid = 90711
    trade_id = _open_trade(uid)
    login_as(_user(uid))

    resp = asgi_client.post(
        f"/api/journal/trades/{trade_id}/close",
        json={"exit_price": 2200.0, "exit_reason": "tp_hit"},
    )

    assert resp.status_code == 200, resp.text
    entry = resp.json()["entry"]
    assert entry["exit_price"] == 2200.0
    assert datetime.fromisoformat(entry["closed_at"])


def test_update_answers_200_with_datetime_columns(asgi_client, login_as, _pg_style_datetimes):  # noqa: F811
    uid = 90712
    trade_id = _open_trade(uid)
    login_as(_user(uid))

    resp = asgi_client.patch(f"/api/journal/trades/{trade_id}", json={"notes": "x"})

    assert resp.status_code == 200, resp.text
    entry = resp.json()["entry"]
    assert entry["notes"] == "x"
    assert datetime.fromisoformat(entry["opened_at"])
