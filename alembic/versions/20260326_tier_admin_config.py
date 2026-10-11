"""Add tier_config and user_tier_overrides tables for dynamic tier management.

Allows admin to adjust tier limits without code deploys and set per-user overrides
for beta testers and VIPs.

Revision ID: 9c3d4e5f6a7b
Revises: 8b2c3d4e5f6a
Create Date: 2026-03-26
"""

import json

from alembic import op

revision = '9c3d4e5f6a7b'
down_revision = '8b2c3d4e5f6a'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS tier_config (
            tier TEXT PRIMARY KEY,
            limits_json JSONB NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER REFERENCES users(id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_tier_overrides (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            override_json JSONB NOT NULL DEFAULT '{}',
            reason TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER REFERENCES users(id)
        )
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS user_tier_overrides")
    op.execute("DROP TABLE IF EXISTS tier_config")
