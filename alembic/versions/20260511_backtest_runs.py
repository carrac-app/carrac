"""backtest_runs table — persist MCP backtest history per user.

Creates a durable table for saving backtest run results from
`backtest_save_run` MCP tool. Users can later retrieve and compare
runs via `list_backtest_runs` and `get_backtest_run`.

Revision ID: t0u1v2w3x4y5
Revises: s9t0u1v2w3x4
Create Date: 2026-05-11
"""

from alembic import op
import sqlalchemy as sa


revision = 't0u1v2w3x4y5'
down_revision = 's9t0u1v2w3x4'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(255),
            symbol VARCHAR(50) NOT NULL,
            expression TEXT NOT NULL,
            timeframe VARCHAR(10) NOT NULL,
            lookback_bars INTEGER NOT NULL,
            hold_bars INTEGER NOT NULL,
            result JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_backtest_runs_user_id ON backtest_runs(user_id)'
    )
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_backtest_runs_created_at ON backtest_runs(created_at DESC)'
    )


def downgrade():
    op.execute('DROP INDEX IF EXISTS idx_backtest_runs_created_at')
    op.execute('DROP INDEX IF EXISTS idx_backtest_runs_user_id')
    op.execute('DROP TABLE IF EXISTS backtest_runs')
