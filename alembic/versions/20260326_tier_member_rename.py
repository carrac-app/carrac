"""Rename 'premium' tier to 'member' for non-admin users.

Part of the Free / Member ($34.99) / Premium ($69.00) tier restructuring.
Existing paid users (tier='premium') become 'member'. Admins and testers unaffected.

Revision ID: 8b2c3d4e5f6a
Revises: fa5a891c4577
Create Date: 2026-03-26
"""

from alembic import op

revision = '8b2c3d4e5f6a'
down_revision = 'fa5a891c4577'
branch_labels = None
depends_on = None


def upgrade():
    # Rename existing 'premium' users to 'member' (admins keep admin via is_admin flag)
    op.execute("""
        UPDATE users
        SET tier = 'member'
        WHERE tier = 'premium'
          AND (is_admin = false OR is_admin IS NULL)
    """)


def downgrade():
    # WARNING: this migration's upgrade() rewrote every non-admin premium
    # user to tier='member'. At downgrade time we cannot safely distinguish
    # "was premium before the rename" from "signed up as member afterwards"
    # without a tier_history table — which doesn't exist. A blanket
    # re-tier (UPDATE users SET tier='premium' WHERE tier='member') would
    # upgrade every member, including accounts that were never premium,
    # which is unsafe.
    #
    # Safe behaviour: no-op. If a real rollback is needed, operator must
    # restore from a pg_dump taken before upgrade() ran. See the backup
    # off-site pull job (daily 3:30 UTC) for candidates.
    pass
