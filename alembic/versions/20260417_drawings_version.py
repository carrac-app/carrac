"""Add `version` column to drawings for optimistic-concurrency sync.

Poll-based drawing sync was last-write-wins across devices,
so concurrent edits could clobber each other silently. Add a monotonically
incrementing version column that clients pass back via If-Match; the API
returns 409 on mismatch.

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-04-17
"""

from alembic import op


revision = 'i9j0k1l2m3n4'
down_revision = 'h8i9j0k1l2m3'
branch_labels = None
depends_on = None


def upgrade():
    # Postgres path. The SQLite path is handled by the in-place ALTER TABLE
    # migration in carrac/content_db.py:_init_database, which is
    # idempotent and runs on every boot.
    op.execute("""
        ALTER TABLE drawings
        ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1
    """)


def downgrade():
    op.execute("ALTER TABLE drawings DROP COLUMN IF EXISTS version")
