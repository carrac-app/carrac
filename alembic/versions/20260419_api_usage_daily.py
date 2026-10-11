"""Add api_usage_daily rollup table.

Per-user, per-surface daily call counts, meant to be populated by a nightly
writer that drains Redis day-keyed hashes (mcd:usage:YYYY-MM-DD:u:<uid>)
into durable storage.

/api/usage reads directly from Redis (35-day TTL).

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-04-19
"""

from alembic import op


revision = 'k1l2m3n4o5p6'
down_revision = 'j0k1l2m3n4o5'
branch_labels = None
depends_on = None


def upgrade():
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


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_api_usage_daily_date")
    op.execute("DROP INDEX IF EXISTS idx_api_usage_daily_user_date")
    op.execute("DROP TABLE IF EXISTS api_usage_daily")
