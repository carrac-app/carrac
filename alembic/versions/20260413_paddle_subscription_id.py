"""Add paddle_subscription_id column to users table.

Stores the active Paddle subscription ID for matching webhook events
and managing subscription lifecycle.

Revision ID: g7h8i9j0k1l2
Revises: f6g7h8i9j0k1
Create Date: 2026-04-13
"""

from alembic import op

revision = 'g7h8i9j0k1l2'
down_revision = 'f6g7h8i9j0k1'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS paddle_subscription_id TEXT")


def downgrade():
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS paddle_subscription_id")
