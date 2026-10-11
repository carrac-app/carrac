"""scanner_history: capture setup_type + setup_confidence.

Adds two nullable columns so per-archetype IC can be measured going forward. The
scanner already classifies a setup archetype (trend_continuation / pullback /
breakout / reversal / mean_reversion / range) per asset×direction via
classify_setup() (carrac/scanner/scorer/setups.py) and attaches it to the
score_asset() result as result['setup'] — but scanner_history never captured it,
so ic.py can measure per-CRITERION IC but never per-ARCHETYPE IC.

Forward-only: existing rows stay NULL. setup_type cannot be reconstructed after
the fact — classify_setup() needs the full live asset_data snapshot, not just the
6 stored criterion scores. Per-archetype IC starts accruing from apply-day.

Revision ID: shsetup26071401
Revises: pshare26060703
Create Date: 2026-07-14
"""
from alembic import op


revision = 'shsetup26071401'
down_revision = 'pshare26060703'
branch_labels = None
depends_on = None


def upgrade():
    # Additive, nullable, instant on Postgres — no table rewrite, no lock contention.
    op.execute(
        "ALTER TABLE scanner_history "
        "ADD COLUMN IF NOT EXISTS setup_type VARCHAR(30)"
    )
    op.execute(
        "ALTER TABLE scanner_history "
        "ADD COLUMN IF NOT EXISTS setup_confidence NUMERIC(3,2)"
    )


def downgrade():
    op.execute("ALTER TABLE scanner_history DROP COLUMN IF EXISTS setup_confidence")
    op.execute("ALTER TABLE scanner_history DROP COLUMN IF EXISTS setup_type")
