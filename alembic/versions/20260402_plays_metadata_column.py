"""Add metadata TEXT column to plays table.

Stores optional trade-level fields (conviction, entry range, stop loss,
targets) and milestones (checklist) as JSON. Default '{}' for new rows.

Revision ID: c3d4e5f6g7h9
Revises: c3d4e5f6g7h8
Create Date: 2026-04-02

Note: Revision id 'c3d4e5f6g7h8' belongs to 20260401_add_plays_table.py; this
file uses c3d4e5f6g7h9 (chained AFTER add_plays_table) so the chain can
advance. The ALTER TABLE statement is already idempotent so this
runs cleanly on databases that may have the column from elsewhere.
"""

from alembic import op

revision = 'c3d4e5f6g7h9'
down_revision = 'c3d4e5f6g7h8'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE plays ADD COLUMN IF NOT EXISTS metadata TEXT DEFAULT '{}'
    """)


def downgrade():
    op.execute("ALTER TABLE plays DROP COLUMN IF EXISTS metadata")
