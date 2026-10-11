"""Drop the dead api_usage_daily table.

The writer was explicitly deferred (see 20260419_api_usage_daily.py's own
docstring) and never written. The table is the sole orphan in the schema: no
INSERT, UPDATE or COPY anywhere in the repo, and carrac/usage.py says so at
the top of its own module — the live /api/usage reads Redis day-keyed hashes
(mcd:usage:YYYY-MM-DD:u:<uid>, 35-day TTL), which remain untouched by this
migration and are still the only source of per-user call counts.

The table is empty (SELECT count(*) FROM api_usage_daily -> 0), so there is
no data to lose; it never held a row.

The GDPR erasure list in user_db.py::anonymize_user DELETEd from it, which
was harmless; the entry is removed with the table (the list must cover
every user_id-bearing table, and this one no longer exists).

DESTRUCTIVE in form but not in substance: downgrade() recreates the table
exactly as 20260419 built it, so anything unexpectedly depending on its
presence can roll back. As with the earlier history-snapshot table drop, the recovery path for *data* is
the pre-promote pg_dump taken by `make promote`, not this downgrade.

AFTER PROMOTE: refresh tests/schema/carrac_pg_schema.sql from staging, or
scripts/check_schema_drift.py goes red. The api_usage_daily exemption in
tests/test_export_coverage.py comes out with that refresh, not before —
it is keyed off the dump, which still describes the deployed schema until
this migration runs.

Revision ID: dropusage260826
Revises: mcperrdet260824
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = 'dropusage260826'
down_revision = 'mcperrdet260824'
branch_labels = None
depends_on = None


def _table_exists(bind) -> bool:
    return 'api_usage_daily' in set(sa.inspect(bind).get_table_names())


def upgrade():
    if not _table_exists(op.get_bind()):
        return
    op.execute('DROP INDEX IF EXISTS idx_api_usage_daily_date')
    op.execute('DROP INDEX IF EXISTS idx_api_usage_daily_user_date')
    op.execute('DROP TABLE IF EXISTS api_usage_daily')


def downgrade():
    """Recreate the empty structure only — it never held data. See the module
    docstring: prod count was 0 at the time this migration was written."""
    if _table_exists(op.get_bind()):
        return
    op.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_daily (
            id           BIGSERIAL PRIMARY KEY,
            user_id      INTEGER NOT NULL,
            date         DATE    NOT NULL,
            surface      VARCHAR(16) NOT NULL,  -- 'rest' | 'mcp'
            calls_total  INTEGER NOT NULL DEFAULT 0,
            top_endpoints JSONB,                -- optional breakdown {path: count}
            created_at   TIMESTAMP WITH TIME ZONE DEFAULT now(),
            UNIQUE (user_id, date, surface)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_api_usage_daily_user_date
            ON api_usage_daily (user_id, date DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_api_usage_daily_date
            ON api_usage_daily (date DESC)
    """)
