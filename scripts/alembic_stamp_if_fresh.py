#!/usr/bin/env python3
"""Smart migration runner — stamps existing databases, migrates fresh ones.

Cases:
  1. Fresh DB (no tables) → alembic upgrade head (creates everything)
  2. Already tracked → alembic upgrade head (runs pending migrations)
  3. Existing DB, no alembic_version → REFUSE unless ALEMBIC_ADOPT=1 is set
     (a restore from an old backup would hit this
     branch and silently skip every migration, which is catastrophic. The
     opt-in flag forces the operator to acknowledge what's happening.)

Also enforces the quant-schema precondition — see _check_quant_schema.
"""
import os
import sys
import subprocess


if __name__ == "__main__":
    # Answer --help before anything below is imported or touched.
    import argparse
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# The `quant` schema is created ONLY by `python -m quant.schema`,
# never by alembic — and two migrations ALTER quant tables while guarding only
# their COLUMNS, not the tables themselves:
#   20260418_correlations_fdr_ci   → ALTER TABLE quant.correlations
#   20260525_quant_released_at_lookahead → ALTER TABLE quant.sentiment,
#                                          CREATE INDEX on quant.signals/zscores
# So `alembic upgrade head` against a database without quant dies partway with
# a bare UndefinedTable, and a fresh database that skips the manual apply
# leaves every quant endpoint returning 500 (quant/api/routes.py wraps its SQL
# in a blanket except) until someone notices the smoke test failing.
#
# Bringing quant under alembic has no clean placement: early enough to fix the
# ordering means editing an already-shipped migration's down_revision and
# inserting a node prod has walked past, where it would never execute; at the
# head it runs but does not fix the ordering at all. So the decision was to
# leave ownership where it is and fail LOUDLY and EARLY instead.
_QUANT_REQUIRED_TABLES = ('correlations', 'sentiment', 'signals', 'zscores')


def _check_quant_schema(cur):
    """Return a human-readable problem string, or None if quant is healthy.

    Checked before any migration runs so the operator gets the actual remedy
    instead of an UndefinedTable traceback from a migration file they did not
    write, or a fleet of 500s discovered later via the smoke test.
    """
    cur.execute(
        "SELECT EXISTS(SELECT 1 FROM information_schema.schemata "
        "WHERE schema_name = 'quant')"
    )
    if not cur.fetchone()[0]:
        return "the `quant` schema does not exist"

    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'quant'"
    )
    present = {row[0] for row in cur.fetchall()}
    missing = [t for t in _QUANT_REQUIRED_TABLES if t not in present]
    if missing:
        return "the `quant` schema is missing: " + ", ".join(missing)
    return None


def _ensure_quant_schema(cur):
    """Create the quant schema when it is absent, then re-verify.

    The original behaviour was to refuse and print the
    `python -m quant.schema` remedy. That is the right alarm for a half-built
    database, but for a database that is simply NEW it is a manual step a
    newcomer has no way to know about — so `docker compose up` never reached a
    working schema. Creating it here removes the step without weakening the
    guard: the check below still runs afterwards, and a creation that does not
    actually fix the problem still fails loudly with the original message.

    ``apply_schema`` is idempotent (CREATE ... IF NOT EXISTS throughout), so
    this is safe on an existing database that is only missing a table or two.
    """
    problem = _check_quant_schema(cur)
    if problem is None:
        return None
    print(f"quant schema incomplete ({problem}) — creating it...")
    try:
        from quant.schema import apply_schema
        apply_schema()
    except Exception as exc:  # noqa: BLE001 — re-checked and reported below
        return f"{problem}; automatic creation failed: {exc}"
    return _check_quant_schema(cur)


def main():
    db_type = os.getenv('DB_TYPE', 'postgresql')
    if db_type != 'postgresql':
        print("Alembic migrations only apply to PostgreSQL. Skipping.")
        return 0

    database_url = os.getenv('DATABASE_URL', '')
    if not database_url:
        print("ERROR: DATABASE_URL not set.")
        return 1

    import psycopg2
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    cur = conn.cursor()

    # Check if alembic_version table exists
    cur.execute("""
        SELECT EXISTS(
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'alembic_version'
        )
    """)
    has_alembic = cur.fetchone()[0]

    # Check if schema tables exist (users as canary)
    cur.execute("""
        SELECT EXISTS(
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'users'
        )
    """)
    has_schema = cur.fetchone()[0]

    quant_problem = _ensure_quant_schema(cur)

    conn.close()

    if quant_problem and os.getenv('SKIP_QUANT_CHECK', '').strip().lower() not in ('1', 'true', 'yes'):
        print(
            f"ERROR: {quant_problem}.",
            file=sys.stderr,
        )
        print(
            "Alembic does not own the quant schema, but two migrations alter "
            "quant tables — so `alembic upgrade head` would fail partway with a "
            "bare UndefinedTable, and the app would return 500 from every quant "
            "endpoint until someone noticed smoke failing.",
            file=sys.stderr,
        )
        print(
            "Automatic creation was attempted and did not resolve it, so this "
            "is a real problem (permissions, or a partially-built schema).\n"
            "Fix: apply it by hand, then re-run this.\n"
            "  prod:    docker exec carrac python -m quant.schema\n"
            "  local:   python -m quant.schema\n"
            "Override with SKIP_QUANT_CHECK=1 only if you know the pending "
            "migrations do not touch quant.",
            file=sys.stderr,
        )
        return 3

    if has_schema and not has_alembic:
        adopt = os.getenv('ALEMBIC_ADOPT', '').strip().lower() in ('1', 'true', 'yes')
        if not adopt:
            print(
                "ERROR: schema exists but alembic_version is missing. "
                "This would silently skip every migration — catastrophic for "
                "restore-from-old-backup scenarios. Refusing to stamp head.",
                file=sys.stderr,
            )
            print(
                "If this is a legitimate first-time adoption of an existing "
                "database, re-run with ALEMBIC_ADOPT=1. "
                "Otherwise, investigate why alembic_version was dropped.",
                file=sys.stderr,
            )
            return 2

        print("Existing database detected (ALEMBIC_ADOPT=1) — stamping baseline revision...")
        result = subprocess.run(
            [sys.executable, '-m', 'alembic', 'stamp', 'head'],
            cwd=PROJECT_ROOT,
        )
        if result.returncode != 0:
            print("ERROR: Failed to stamp baseline.")
            return 1
        print("Stamped to head. Future migrations will run normally.")
        return 0

    # Fresh DB or already tracked — run upgrade
    print("Running alembic upgrade head...")
    result = subprocess.run(
        [sys.executable, '-m', 'alembic', 'upgrade', 'head'],
        cwd=PROJECT_ROOT,
    )
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
