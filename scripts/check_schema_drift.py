#!/usr/bin/env python3
"""Drift guard — compare two Postgres schemas at the column level.

Fails (exit 1) if the two databases differ in their public+quant tables or
columns (name, data type, nullability). Used by the CI ``pg-portability`` job
to assert that a FRESH repo bootstrap (scripts/init_postgres.pgsql via the
alembic baseline, then ``alembic upgrade head``) reproduces the real prod
schema captured in tests/schema/carrac_pg_schema.sql — so the drift that let
a past boolean=integer bug hide (widget_tokens missing ``revoked`` on a
fresh build) can never silently return.

Usage:
    check_schema_drift.py <expected_dsn> <actual_dsn>
      expected = the true prod schema (the committed dump, loaded)
      actual   = a fresh repo bootstrap

Compares columns only — NOT indexes/constraints/defaults (out of scope; the
bug class that matters is missing/renamed/wrong-type columns).
"""

from __future__ import annotations

import argparse

import psycopg2

_SCHEMAS = ("public", "quant")


def _columns(dsn: str) -> dict:
    conn = psycopg2.connect(dsn, connect_timeout=5)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT table_schema, table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = ANY(%s)
            ORDER BY 1, 2, 3
            """,
            (list(_SCHEMAS),),
        )
        out: dict = {}
        for sch, tbl, col, typ, nul in cur.fetchall():
            out.setdefault((sch, tbl), {})[col] = (typ, nul)
        return out
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("expected_dsn", help="the true prod schema (the committed dump, loaded)")
    parser.add_argument("actual_dsn", help="a fresh repo bootstrap")
    args = parser.parse_args()

    expected = _columns(args.expected_dsn)   # true prod schema
    actual = _columns(args.actual_dsn)       # fresh repo bootstrap

    problems: list[str] = []

    te, ta = set(expected), set(actual)
    for t in sorted(te - ta):
        problems.append(f"MISSING TABLE (in prod, not in fresh bootstrap): {t[0]}.{t[1]}")
    for t in sorted(ta - te):
        problems.append(f"EXTRA TABLE (in fresh bootstrap, not in prod): {t[0]}.{t[1]}")

    for t in sorted(te & ta):
        ce, ca = expected[t], actual[t]
        for c in sorted(set(ce) - set(ca)):
            problems.append(f"MISSING COLUMN {t[0]}.{t[1]}.{c} ({ce[c][0]})")
        for c in sorted(set(ca) - set(ce)):
            problems.append(f"EXTRA COLUMN {t[0]}.{t[1]}.{c} ({ca[c][0]})")
        for c in sorted(set(ce) & set(ca)):
            if ce[c] != ca[c]:
                problems.append(
                    f"COLUMN MISMATCH {t[0]}.{t[1]}.{c}: prod={ce[c]} bootstrap={ca[c]}"
                )

    if problems:
        print("SCHEMA DRIFT — the repo cannot rebuild prod:")
        for p in problems:
            print(f"  • {p}")
        print(
            "\nFix: update scripts/init_postgres.pgsql (or add a migration) to match "
            "prod, and refresh tests/schema/carrac_pg_schema.sql from the deployed database."
        )
        return 1

    print(f"OK — fresh bootstrap matches prod schema ({len(te)} tables, columns identical).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
