"""Postgres portability suite — fixtures + collection gate.

These tests exercise the write-surface SQL against a REAL Postgres loaded
with the true prod schema (tests/schema/carrac_pg_schema.sql), catching the
SQL-dialect bugs that SQLite CI is structurally blind to — e.g. a
boolean=integer comparison on widget_tokens, or an unserialized dict written
to journal goals; both pass on SQLite and 500 on Postgres.

Activation: the suite is COLLECTED ONLY when ``CARRAC_TEST_DB=postgres`` and
``DATABASE_URL`` point at a reachable Postgres (the CI ``pg-portability`` job
sets both; locally, run against a throwaway postgres:16). In the normal
SQLite run these files are skipped at collection time, so ``pytest tests/``
stays green with zero Postgres dependency.

Isolation: the schema is loaded once; each test runs inside its own SAVEPOINT-
free TRUNCATE cycle over the touched tables (see ``pg_clean``). Tests create
their own users/rows so ordering is irrelevant.
"""

from __future__ import annotations

import os

import pytest

_PG_MODE = os.environ.get("CARRAC_TEST_DB", "").lower() in ("postgres", "postgresql")

# Skip the whole directory unless explicitly in Postgres mode — keeps the
# default SQLite `pytest tests/` run free of any Postgres requirement.
if not _PG_MODE:
    collect_ignore_glob = ["*"]


def _dsn() -> "str | None":
    for var in ("TEST_DATABASE_URL", "DATABASE_URL"):
        val = os.environ.get(var, "").strip()
        if val.startswith("postgresql"):
            return val
    return None


# Tables the portability tests write to — truncated between tests. CASCADE
# handles FK dependents (api_keys, journal_*, play_shares, drawings, …).
_TRUNCATE_TABLES = [
    "users",
    "widget_tokens",
    "journal_goals",
    "trade_journal",
    "journal_sessions",
    "plays",
    "drawings",
]


@pytest.fixture(scope="session")
def pg_conn():
    """Session psycopg2 connection to the CI Postgres (autocommit)."""
    dsn = _dsn()
    if dsn is None:
        pytest.skip("CARRAC_TEST_DB=postgres but no TEST_DATABASE_URL/DATABASE_URL set")
    import psycopg2

    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable for portability suite: {exc}")
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def pg_clean(pg_conn):
    """Truncate the write-surface tables before each test (RESTART IDENTITY)."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "TRUNCATE TABLE {} RESTART IDENTITY CASCADE".format(
                ", ".join(_TRUNCATE_TABLES)
            )
        )
    yield


@pytest.fixture
def udb():
    """The user_db singleton, bound to Postgres (DB_TYPE set by root conftest)."""
    from carrac.db_pool import get_db_type

    assert get_db_type() == "postgresql", (
        "portability suite must run in Postgres mode (CARRAC_TEST_DB=postgres)"
    )
    from user_db import user_db

    return user_db


@pytest.fixture
def cdb():
    """The ContentDatabase singleton (journal goals / drawings), Postgres-bound."""
    from carrac.content_db import get_content_db

    return get_content_db()
