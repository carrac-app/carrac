"""Add users.flags JSONB column.

Per-user feature/onboarding flags. They gate first-event
funnel captures (`first_chart_view`, `first_copilot_message`, etc.) so
they fire exactly once per user without re-firing on subsequent visits.

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-04-19
"""

from alembic import op


revision = 'l2m3n4o5p6q7'
down_revision = 'k1l2m3n4o5p6'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS flags JSONB NOT NULL DEFAULT '{}'::jsonb
    """)


def downgrade():
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS flags")
