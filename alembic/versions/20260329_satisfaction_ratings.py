"""Add satisfaction_ratings table for monthly user satisfaction surveys.

Stores rating (1-5) and optional comment per user, with full history.
Queried by the satisfaction survey toast to check eligibility.

Revision ID: a1b2c3d4e5f6
Revises: 9c3d4e5f6a7b
Create Date: 2026-03-29
"""

from alembic import op

revision = 'a1b2c3d4e5f6'
down_revision = '9c3d4e5f6a7b'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS satisfaction_ratings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
            comment TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_satisfaction_user ON satisfaction_ratings(user_id, created_at DESC)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS satisfaction_ratings")
