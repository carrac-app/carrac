"""Add play_shares table for public shareable Plays.

Frozen-snapshot share links: a Carrac user mints a short, unguessable public
slug for one of their Plays; anyone can open ``carrac.cc/s/<slug>`` with no
Carrac account. The Play is FROZEN into a JSON snapshot at mint time, so the
public artifact is immutable and reveals only the fields the creator opted to
share — even if the underlying play is later edited or deleted.

Writes (mint/update/revoke) are served by the FastAPI plays router; public
reads (/s/<slug>) by the Flask shares blueprint. Both call carrac/plays/shares.py.

Revision ID: pshare26060201
Revises: z6a7b8c9d0e1
Create Date: 2026-06-02
"""

from alembic import op

revision = 'pshare26060201'
down_revision = 'z6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS play_shares (
            id SERIAL PRIMARY KEY,
            play_id INTEGER NOT NULL REFERENCES plays(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,

            -- Public URL token: 8-char unambiguous nanoid (see shares.py).
            slug VARCHAR(12) NOT NULL UNIQUE,

            -- Frozen, opt-in-only view of the play at mint time.
            snapshot JSONB NOT NULL DEFAULT '{}',

            -- Field-visibility toggles (mirrored into the snapshot at freeze).
            include_thesis BOOLEAN DEFAULT TRUE,
            include_conditions BOOLEAN DEFAULT TRUE,
            include_plan BOOLEAN DEFAULT TRUE,

            -- Cached rendered card PNG (data/shared/yyyy/mm/<slug>.png).
            image_path TEXT,

            view_count INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            revoked_at TIMESTAMPTZ,   -- set => 410 Gone
            expires_at TIMESTAMPTZ    -- optional
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_play_shares_slug ON play_shares(slug)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_play_shares_user ON play_shares(user_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_play_shares_play ON play_shares(play_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS play_shares")
