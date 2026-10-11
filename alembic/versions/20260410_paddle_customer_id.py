"""Add paddle_customer_id column to users table.

Stores the Paddle customer ID after first checkout, used to generate
customer portal sessions (subscription management) and match webhooks.

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-04-10
"""

from alembic import op

revision = 'f6g7h8i9j0k1'
down_revision = 'e5f6g7h8i9j0'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS paddle_customer_id TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_paddle_cid ON users(paddle_customer_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_users_paddle_cid")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS paddle_customer_id")
