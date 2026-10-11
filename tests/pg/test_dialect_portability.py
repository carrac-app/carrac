"""SQLite-vs-Postgres dialect portability on real Postgres.

Two dialect bugs a SQLite-only CI is structurally blind to:

1. ``ai_review.nl_query_stream`` (paid NL journal query) fed the LLM a
   SQLite-only prompt (``:user_id`` marker + ``strftime``/``json_extract``) and
   executed the result against prod Postgres → every query 500'd
   (``syntax error at or near ":"`` / undefined function), swallowed as
   "Query execution failed" while the user was still charged.
2. ``admin_billing_json`` read a RealDictCursor row positionally (``row[0]``)
   → ``KeyError`` on Postgres, swallowed → the admin panel silently showed a
   blank "last report".

These run ONLY in the pg-portability job (``CARRAC_TEST_DB=postgres``).
"""

from __future__ import annotations

import secrets

import pytest

from carrac.db_pool import get_conn
from carrac.journal import ai_review


def _mk_user(udb) -> int:
    uid = udb.create_user(
        f"pg-{secrets.token_hex(4)}@test.dev", "StrongPass!123", tos_accepted=True
    )
    assert uid
    return uid


# ── Finding B: NL-query execution path on Postgres ─────────────────────────

def test_pg_named_param_marker_executes(udb, cdb):
    """The %(user_id)s marker the fixed prompt asks for must run on Postgres."""
    uid = _mk_user(udb)
    cdb.log_trade_open(uid, "BTC", "1D", "long", 65000.0)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM trade_journal WHERE user_id = %(user_id)s",
            {"user_id": uid},
        ).fetchall()
    assert rows and rows[0]["n"] == 1


def test_pg_date_function_executes(udb, cdb):
    """PG date guidance (to_char/date_trunc) must run where SQLite strftime can't."""
    uid = _mk_user(udb)
    cdb.log_trade_open(uid, "BTC", "1D", "long", 65000.0)

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT to_char(opened_at, 'YYYY-MM') AS ym, COUNT(*) AS n "
            "FROM trade_journal WHERE user_id = %(user_id)s GROUP BY ym",
            {"user_id": uid},
        ).fetchall()
    assert rows and rows[0]["n"] == 1


def test_normalize_makes_sqlite_marker_run_on_pg(udb, cdb):
    """End-to-end teeth: a stray ':user_id' (what an LLM may emit) is
    normalized and then executes on Postgres — pinning the fix."""
    uid = _mk_user(udb)
    cdb.log_trade_open(uid, "BTC", "1D", "long", 65000.0)

    raw = "SELECT id FROM trade_journal WHERE user_id = :user_id"
    fixed = ai_review._normalize_nl_sql(raw)          # -> %(user_id)s on PG
    with get_conn() as conn:
        rows = conn.execute(fixed, {"user_id": uid}).fetchall()
    assert len(rows) == 1


def test_raw_sqlite_marker_still_fails_on_pg(udb, pg_conn):
    """Guard the premise: the un-normalized ':user_id' really is a PG error,
    so the normalizer above is load-bearing (not decoration)."""
    import psycopg2

    with pytest.raises(psycopg2.Error):
        with pg_conn.cursor() as cur:
            cur.execute("SELECT 1 WHERE 1 = :user_id", {"user_id": 1})


# ── Finding A: admin_billing RealDictRow positional access ─────────────────

def test_reports_read_by_name_on_pg(pg_conn):
    """admin_billing_json reads id/created_at by NAME — row[0] KeyErrors on PG."""

    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO reports (report_type, content) "
                    "VALUES ('morning', 'x') RETURNING id")
        rid = cur.fetchone()[0]

    with get_conn() as conn:  # PgConnWrapper -> RealDictCursor, like production
        row = conn.execute(
            "SELECT id, created_at FROM reports WHERE id = %(id)s",
            {"id": rid},
        ).fetchone()

    assert row["id"] == rid          # the fixed access
    assert "created_at" in row
    with pytest.raises(KeyError):    # positional access fails on RealDictCursor rows (admin_billing)
        _ = row[0]
