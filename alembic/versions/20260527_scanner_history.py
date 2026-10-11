"""scanner_history table — capture-first scanner calibration.

Universe-wide daily snapshot of scanner scores: one row per symbol×direction per
UTC day, for forward-return calibration later. Capture-only — NO regime reweighting
(that is separate work). Populated by
``carrac/scanner/scorer/history.upsert_daily_snapshot`` via the precompute hook.

Revision ID: w3x4y5z6a7b8
Revises: v2w3x4y5z6a7
Create Date: 2026-05-27
"""
from alembic import op


revision = 'w3x4y5z6a7b8'
down_revision = 'v2w3x4y5z6a7'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS scanner_history (
            id BIGSERIAL PRIMARY KEY,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            captured_date DATE NOT NULL,
            symbol VARCHAR(50) NOT NULL,
            direction VARCHAR(10) NOT NULL,
            regime VARCHAR(20) NOT NULL,
            total INTEGER NOT NULL,
            verdict_v1 VARCHAR(20) NOT NULL,
            criteria JSONB NOT NULL,
            vetoes JSONB,
            price NUMERIC(20,8),
            bar_settled BOOLEAN NOT NULL DEFAULT FALSE,
            CONSTRAINT scanner_history_daily_unique UNIQUE (symbol, captured_date, direction)
        )
    """)
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_scanner_history_captured_at '
        'ON scanner_history(captured_at DESC)'
    )
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_scanner_history_symbol_date '
        'ON scanner_history(symbol, captured_date DESC)'
    )


def downgrade():
    op.execute('DROP INDEX IF EXISTS idx_scanner_history_symbol_date')
    op.execute('DROP INDEX IF EXISTS idx_scanner_history_captured_at')
    op.execute('DROP TABLE IF EXISTS scanner_history')
