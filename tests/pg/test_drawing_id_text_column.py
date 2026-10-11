"""trade_journal.drawing_id is text, every caller passes an int.

``drawings.id`` is ``integer``; ``trade_journal.drawing_id`` is ``text`` (see
``tests/schema/carrac_pg_schema.sql``, dumped from live).  Writes survive that
mismatch — Postgres allows an integer→text cast in *assignment* context — but
comparisons do not::

    SELECT * FROM trade_journal WHERE drawing_id = 42
    ERROR: operator does not exist: text = integer

``adapt_query`` only rewrites ``?`` → ``%s``, so nothing coerces the parameter
on the way through.  SQLite is structurally blind to this: it applies column
affinity and quietly converts, so the whole SQLite suite passes.

Live reachability: ``/api/journal/trades/import`` is reachable again since
A later change restored it (``carrac/asgi.py`` EXCLUDED_SUBPATHS) and
``static/js/journal-page.js`` calls it, so this was a live 500 on the journal
page's import-from-drawings button.

These run ONLY in the pg-portability job (``CARRAC_TEST_DB=postgres``).
"""

from __future__ import annotations

import secrets


def _mk_user(udb) -> int:
    uid = udb.create_user(
        f"pg-{secrets.token_hex(4)}@test.dev", "StrongPass!123", tos_accepted=True
    )
    assert uid
    return uid


def test_drawing_id_column_is_still_text(pg_conn):
    """Ground the premise.

    If a migration ever makes this column integer, this test fails and the
    str() coercion in find_journal_by_drawing can be dropped — rather than
    being carried forever as cargo cult.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'trade_journal' AND column_name = 'drawing_id'"
        )
        assert cur.fetchone()[0] == "text"


def test_find_journal_by_drawing_accepts_an_int(udb, cdb):
    """Both callers pass ``d['id']``, an int from drawings."""
    uid = _mk_user(udb)
    drawing_id = 4242
    journal_id = cdb.log_trade_open(
        uid, "BTC", "1D", "long", 100.0, drawing_id=drawing_id
    )

    found = cdb.find_journal_by_drawing(drawing_id)

    assert found is not None, "int drawing_id must match the text column"
    assert found["id"] == journal_id


def test_find_journal_by_drawing_accepts_a_str(udb, cdb):
    """Callers that already stringify (the spot-promotion path) keep working."""
    uid = _mk_user(udb)
    journal_id = cdb.log_trade_open(
        uid, "ETH", "4H", "long", 50.0, drawing_id=777
    )

    assert cdb.find_journal_by_drawing("777")["id"] == journal_id


def test_find_journal_by_drawing_misses_cleanly(udb, cdb):
    """No row is None, not an error — the miss path is the common one."""
    _mk_user(udb)

    assert cdb.find_journal_by_drawing(999999) is None
