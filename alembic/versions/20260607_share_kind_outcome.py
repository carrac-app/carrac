"""Generalize play_shares: kind rail + include_outcome toggle.

``kind`` (default ``'play'``) future-proofs the public /s/ surface for
non-play shares (chart snapshots) without a later table migration — the
resolver + card route dispatch on it. Only ``'play'`` is implemented today.

``include_outcome`` is the 4th visibility toggle (next to thesis /
conditions / plan): when ON and the play is terminal, the frozen snapshot
carries an ``outcome`` block (resolved_at + resolution). Defaults FALSE —
process-first sharing; results are opt-in.

Table had 0 rows at migration time (feature shipped backend-first), so
both ALTERs are instant.

Revision ID: pshare26060702
Revises: pshare26060201
Create Date: 2026-06-07
"""

from alembic import op

revision = 'pshare26060702'
down_revision = 'pshare26060201'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE play_shares "
        "ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'play'"
    )
    op.execute(
        "ALTER TABLE play_shares "
        "ADD COLUMN IF NOT EXISTS include_outcome BOOLEAN DEFAULT FALSE"
    )


def downgrade():
    op.execute("ALTER TABLE play_shares DROP COLUMN IF EXISTS include_outcome")
    op.execute("ALTER TABLE play_shares DROP COLUMN IF EXISTS kind")
