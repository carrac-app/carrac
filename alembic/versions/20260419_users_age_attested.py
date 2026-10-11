"""Add users.age_attested_at + users.age_attested_ip.

EU + US regulated-instrument rules want an affirmative 18+
attestation at signup. We record (a) the timestamp when the user ticked
the box and (b) the IP, same pattern as tos_accepted_at / tos_accepted_ip.

Existing users are backfilled to "now" — the changelog entry states that
continuing to use the Platform constitutes retroactive attestation. Going
forward, the register handler requires the checkbox before create_user()
is called.

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-04-19
"""

from alembic import op


revision = 'm3n4o5p6q7r8'
down_revision = 'l2m3n4o5p6q7'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS age_attested_at TIMESTAMP
    """)
    op.execute("""
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS age_attested_ip VARCHAR(45)
    """)
    # Backfill existing users so NOT NULL isn't violated when we later tighten.
    op.execute("""
        UPDATE users
        SET age_attested_at = now()
        WHERE age_attested_at IS NULL
    """)


def downgrade():
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS age_attested_ip")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS age_attested_at")
