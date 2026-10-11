"""Add FDR + bootstrap-CI columns to quant.correlations.

Credibility layer for the Quant Engine. Rolling Spearman rows
already carry `p_value`; these get augmented with:
  - q_value     — Benjamini-Hochberg corrected p, per (ts, method, window)
  - ci_low/high — 95% CI via Fisher z-transform
  - significant — boolean: q<0.05 AND n_obs>=30 AND |r|>=0.1

All columns nullable so historical rows don't block the migration;
the nightly engine refills them forward and a one-shot backfill
handles the last 365 days.

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-04-18
"""

from alembic import op


revision = 'j0k1l2m3n4o5'
down_revision = 'i9j0k1l2m3n4'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE quant.correlations
        ADD COLUMN IF NOT EXISTS q_value     NUMERIC,
        ADD COLUMN IF NOT EXISTS ci_low      NUMERIC,
        ADD COLUMN IF NOT EXISTS ci_high     NUMERIC,
        ADD COLUMN IF NOT EXISTS significant BOOLEAN
    """)


def downgrade():
    op.execute("""
        ALTER TABLE quant.correlations
        DROP COLUMN IF EXISTS q_value,
        DROP COLUMN IF EXISTS ci_low,
        DROP COLUMN IF EXISTS ci_high,
        DROP COLUMN IF EXISTS significant
    """)
