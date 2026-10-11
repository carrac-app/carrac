"""The Log Trade form posts ``setup_type`` and ``notes``; create stores both.

The body below is exactly what ``saveTrade()`` in static/js/journal-page.js
sends, nulls included. With either field missing from ``TradeCreateRequest``
(``extra="forbid"``) the whole trade is rejected with 400.
"""

from __future__ import annotations

from carrac.content_db import get_content_db
from tests.fastapi.test_async_offload_journal import _user, login_as  # noqa: F401


def test_create_accepts_and_persists_setup_type_and_notes(asgi_client, login_as):  # noqa: F811
    uid = 90713
    login_as(_user(uid))

    resp = asgi_client.post(
        "/api/journal/trades",
        json={
            "asset": "BTC", "timeframe": "1D", "direction": "long",
            "entry_price": 60000, "stop_price": None, "target_price": None,
            "size": None, "fees": None, "setup_type": "breakout",
            "confidence": 3, "mood": 3, "notes": "clean retest of the level",
            "account_tag": None,
        },
    )

    assert resp.status_code == 201, resp.text
    row = get_content_db().get_journal_entry(resp.json()["id"])
    assert row["setup_type"] == "breakout"
    assert row["notes"] == "clean retest of the level"
