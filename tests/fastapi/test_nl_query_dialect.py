"""NL journal-query dialect portability (SQLite tests, Postgres production).

The natural-language journal query lets the LLM write SQL that ``nl_query_stream``
then executes against the LIVE backend (SQLite in tests, Postgres in production). The
prompt must not hard-code SQLite syntax (``:user_id`` marker, ``strftime``,
``json_extract``); on Postgres ``:user_id`` is a syntax error and those funcs
don't exist, so EVERY query would 500 in production — swallowed as "Query execution
failed" while the paid user was still charged.

These tests assert the prompt + the defensive normalizer follow whichever
dialect is live. They run in the normal SQLite suite (no Postgres needed);
the end-to-end execution against real Postgres is pinned in
``tests/pg/test_dialect_portability.py``.
"""

import pytest

from carrac.journal import ai_review


@pytest.fixture
def as_postgres(monkeypatch):
    # The helpers import get_db_type from carrac.db_pool at call time, so
    # patching the source attribute is enough.
    monkeypatch.setattr("carrac.db_pool.get_db_type", lambda: "postgresql")


@pytest.fixture
def as_sqlite(monkeypatch):
    monkeypatch.setattr("carrac.db_pool.get_db_type", lambda: "sqlite")


# ── prompt is dialect-aware ────────────────────────────────────────────────

def test_prompt_postgres_uses_pg_param_and_functions(as_postgres):
    p = ai_review._nl_sql_prompt()
    assert "%(user_id)s" in p, "Postgres needs %(user_id)s, not :user_id"
    assert ":user_id" not in p
    assert "PostgreSQL" in p
    assert ("to_char" in p) or ("date_trunc" in p)
    # SQLite-only funcs must NOT be instructed on Postgres
    assert "strftime" not in p
    assert "json_extract" not in p


def test_prompt_sqlite_uses_sqlite_param_and_functions(as_sqlite):
    p = ai_review._nl_sql_prompt()
    assert ":user_id" in p
    assert "%(user_id)s" not in p
    assert "SQLite" in p
    assert "strftime" in p
    assert "json_extract" in p


def test_prompt_keeps_full_table_docs(as_sqlite):
    # The dialect refactor must not drop the schema guidance the model relies on.
    p = ai_review._nl_sql_prompt()
    for token in ("trade_journal", "journal_sessions", "score_snapshots",
                  "JOIN pattern", "RESPOND WITH VALID JSON ONLY"):
        assert token in p


# ── defensive param-marker normalization ───────────────────────────────────

def test_normalize_translates_marker_on_postgres(as_postgres):
    sql = "SELECT id FROM trade_journal WHERE user_id = :user_id ORDER BY closed_at"
    out = ai_review._normalize_nl_sql(sql)
    assert ":user_id" not in out
    assert "WHERE user_id = %(user_id)s" in out


def test_normalize_noop_on_sqlite(as_sqlite):
    sql = "SELECT id FROM trade_journal WHERE user_id = :user_id"
    assert ai_review._normalize_nl_sql(sql) == sql
