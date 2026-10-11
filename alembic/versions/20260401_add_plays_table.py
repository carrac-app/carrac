"""Add plays table for thesis-first market belief tracking.

Plays replace Scenarios with a simpler two-axis model:
  Type (directional/macro/event) × Approach (single/scaled/defensive)

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-04-01
"""

from alembic import op

revision = 'c3d4e5f6g7h8'
down_revision = 'b2c3d4e5f6g7'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS plays (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,

            -- Core
            title TEXT NOT NULL,
            thesis TEXT,
            type VARCHAR(20) NOT NULL DEFAULT 'directional',
            approach VARCHAR(20) NOT NULL DEFAULT 'single',
            direction VARCHAR(20) DEFAULT 'long',

            -- Assets
            primary_asset VARCHAR(20),
            related_assets JSONB DEFAULT '[]',
            trigger_asset VARCHAR(20),

            -- Tracking
            conditions JSONB DEFAULT '{"rules":[],"logic":"all"}',
            invalidation JSONB,
            timeframe VARCHAR(20),
            expiry TIMESTAMPTZ,

            -- Action plan
            action_plan TEXT,
            linked_drawing_ids JSONB DEFAULT '[]',

            -- Lifecycle
            status VARCHAR(20) NOT NULL DEFAULT 'watching',
            status_changed_at TIMESTAMPTZ,
            resolved_at TIMESTAMPTZ,
            resolution TEXT,

            -- Morning report
            report_tracking BOOLEAN DEFAULT TRUE,
            last_report_note TEXT,

            -- Score snapshot
            scorer_snapshot JSONB,

            -- Timestamps
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_plays_user_status ON plays(user_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_plays_asset ON plays(primary_asset)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_plays_updated ON plays(updated_at DESC)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS plays")
