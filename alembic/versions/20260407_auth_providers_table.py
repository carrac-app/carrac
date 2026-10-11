"""Add auth_providers table for social login (Google/Facebook/Apple).

Backend-only — UI is exposed separately.
Routes are gated behind the SOCIAL_AUTH_ENABLED feature flag (off by default).

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6g7h8i9
Create Date: 2026-04-07
"""

from alembic import op

revision = 'e5f6g7h8i9j0'
down_revision = 'd4e5f6g7h8i9'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS auth_providers (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            provider        VARCHAR(16) NOT NULL CHECK (provider IN ('google','facebook','apple')),
            provider_uid    TEXT NOT NULL,
            email_at_link   TEXT,
            email_verified  BOOLEAN DEFAULT FALSE,
            display_name    TEXT,
            raw_profile     TEXT,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            last_login_at   TIMESTAMPTZ,
            UNIQUE (provider, provider_uid),
            UNIQUE (user_id, provider)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_auth_providers_user ON auth_providers(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_auth_providers_lookup ON auth_providers(provider, provider_uid)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS auth_providers")
