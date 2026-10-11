"""Beta waitlist table (invite-code waitlist).

Promotes the flat data/private/beta_waitlist.jsonl capture to a real, deduped
table so the wave-release job can pick the next N, issue invite codes, and track
status (waiting -> invited -> registered). One row per email (UNIQUE).

Revision ID: y5z6a7b8c9d0
Revises: x4y5z6a7b8c9
Create Date: 2026-05-29
"""
from alembic import op

revision = 'y5z6a7b8c9d0'
down_revision = 'x4y5z6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS waitlist (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'waiting'
                CHECK(status IN ('waiting', 'invited', 'registered')),
            source TEXT,
            cohort TEXT,
            invite_code TEXT,
            ip TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            invited_at TIMESTAMPTZ,
            registered_at TIMESTAMPTZ
        )
    """)
    # Wave-release picks the oldest 'waiting' rows first.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_waitlist_status ON waitlist(status, created_at)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_waitlist_status")
    op.execute("DROP TABLE IF EXISTS waitlist")
