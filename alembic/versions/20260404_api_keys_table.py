"""Add api_keys table for per-user API key management.

Enables multiple users to authenticate to REST API and MCP server
with their own keys, inheriting tier-based rate limits.

Revision ID: d4e5f6g7h8i9
Revises: c3d4e5f6g7h9
Create Date: 2026-04-04

Note: down_revision is c3d4e5f6g7h9, chaining after the renumbered
plays_metadata_column migration. Two migrations must never share a revision
id: a duplicate leaves the database unable to advance past c3d4e5f6g7h8.
"""

from alembic import op

revision = 'd4e5f6g7h8i9'
down_revision = 'c3d4e5f6g7h9'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            token_suffix TEXT NOT NULL,
            name TEXT DEFAULT 'Default',
            scope VARCHAR(20) DEFAULT 'full',
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            last_used_at TIMESTAMPTZ,
            revoked BOOLEAN DEFAULT FALSE
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(token_hash)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS api_keys")
