"""Static write-column guard, run against the real schema.

Wraps ``scripts/check_write_columns.py`` (see its module docstring for the
full design rationale) as a pytest so it runs automatically in the
``pg-portability`` CI job alongside the rest of tests/pg/, using the SAME
Postgres connection (the shared ``pg_conn`` fixture) rather than opening a
second one.

This is a STATIC sweep over source files, not a test of runtime behaviour —
it doesn't touch any table data, so `pg_clean`'s autouse TRUNCATE has nothing
to interact with here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_write_columns as cwc  # noqa: E402


def test_all_write_site_columns_exist_in_schema(pg_conn):
    """Every INSERT/UPDATE column name application code writes — found by
    the literal-SQL sweep or the dynamic-construction registry — must exist
    in the real Postgres schema (public + quant), loaded by the CI job from
    tests/schema/carrac_pg_schema.sql before pytest runs.

    A failure here means some write site would silently no-op or 500 in
    prod.
    """
    report = cwc.evaluate(pg_conn)

    assert report.n_findings > 100, (
        f"only found {report.n_findings} column writes — the sweep's file "
        "walk or AST extraction likely broke and is silently covering "
        "far less than it should"
    )

    if report.problems:
        detail = "\n".join(f"  - {p}" for p in report.problems)
        pytest.fail(
            f"{len(report.problems)} write-site column(s) do not exist in "
            f"the schema:\n{detail}"
        )
