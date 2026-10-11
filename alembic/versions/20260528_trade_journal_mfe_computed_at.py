"""trade_journal: add mfe_computed_at for rolling open-trade MFE/MAE snapshots.

Revision ID: x4y5z6a7b8c9
Revises: w3x4y5z6a7b8
Create Date: 2026-05-28
"""
from alembic import op


revision = 'x4y5z6a7b8c9'
down_revision = 'w3x4y5z6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        'ALTER TABLE trade_journal ADD COLUMN mfe_computed_at TIMESTAMP'
    )


def downgrade():
    op.execute(
        'ALTER TABLE trade_journal DROP COLUMN mfe_computed_at'
    )
