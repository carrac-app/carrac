"""Quant look-ahead: released_at on sentiment + COT backfill + lookup indices.

Adds a `released_at` column to quant.sentiment so lagged-publication data
(CFTC COT, reported as-of Tuesday but published the following Friday) can be
aligned on its knowledge date instead of its as-of date. Backfills existing
COT rows (source='cftc') with ts + 3 days. Non-CFTC sentiment (e.g. fear/greed)
is real-time, so released_at stays NULL and analysis COALESCEs back to ts.

Also adds secondary indices on signals + zscores.

quant.macro already has a released_at column (populated by the FRED collector).

Revision ID: v2w3x4y5z6a7
Revises: u1v2w3x4y5z6
Create Date: 2026-05-25
"""

from alembic import op


revision = 'v2w3x4y5z6a7'
down_revision = 'u1v2w3x4y5z6'
branch_labels = None
depends_on = None


def upgrade():
    # Knowledge-date column for COT (and any future lagged sentiment source).
    op.execute(
        'ALTER TABLE quant.sentiment ADD COLUMN IF NOT EXISTS released_at TIMESTAMPTZ'
    )
    # Backfill existing COT rows: publish date = report (Tuesday) + 3 days (Friday).
    # Scoped to source='cftc' so real-time sentiment (fear/greed) stays NULL.
    op.execute("""
        UPDATE quant.sentiment
        SET released_at = (ts + INTERVAL '3 days')
        WHERE source = 'cftc' AND released_at IS NULL
    """)

    # Secondary indices for time-series reads (PK alone forces scans).
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_signals_lookup '
        'ON quant.signals(source, metric, asset, ts DESC)'
    )
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_zscores_lookup '
        'ON quant.zscores(domain, metric, asset, window_days, ts DESC)'
    )


def downgrade():
    op.execute('DROP INDEX IF EXISTS quant.idx_zscores_lookup')
    op.execute('DROP INDEX IF EXISTS quant.idx_signals_lookup')
    op.execute('ALTER TABLE quant.sentiment DROP COLUMN IF EXISTS released_at')
