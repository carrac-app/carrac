"""Add index on paddle_subscription_id column.

Mirrors the idx_users_paddle_cid index on paddle_customer_id,
preventing full-table scans when looking up users by subscription ID.

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-04-13
"""

from alembic import op

revision = 'h8i9j0k1l2m3'
down_revision = 'g7h8i9j0k1l2'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_paddle_sid ON users(paddle_subscription_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_users_paddle_sid")
