"""Add audio_script TEXT column to reports and personal_report_sections.

Persists the conversational audio briefing script in a dedicated column
instead of burying it in the metadata JSON blob. Also creates the
personal_report_sections table if missing from PostgreSQL.

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-01
"""

from alembic import op

revision = 'b2c3d4e5f6g7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # Create personal_report_sections if missing (was only in SQLite schema)
    op.execute("""
        CREATE TABLE IF NOT EXISTS personal_report_sections (
            id SERIAL PRIMARY KEY,
            report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            audio_script TEXT,
            cost_cents INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(report_id, user_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_personal_report_user
        ON personal_report_sections(user_id, report_id)
    """)

    # Add audio_script column to reports
    op.execute("""
        ALTER TABLE reports ADD COLUMN IF NOT EXISTS audio_script TEXT
    """)

    # Add audio_script column to personal_report_sections
    # (may already exist from the CREATE TABLE above, safe due to IF NOT EXISTS)
    op.execute("""
        DO $$
        BEGIN
            ALTER TABLE personal_report_sections ADD COLUMN audio_script TEXT;
        EXCEPTION WHEN duplicate_column THEN
            NULL;
        END $$
    """)

    # Backfill: extract audio_script from metadata JSON into the new column
    op.execute("""
        UPDATE reports
        SET audio_script = metadata::json->>'audio_script'
        WHERE metadata IS NOT NULL
          AND audio_script IS NULL
          AND metadata::json->>'audio_script' IS NOT NULL
    """)


def downgrade():
    op.execute("ALTER TABLE reports DROP COLUMN IF EXISTS audio_script")
    op.execute("ALTER TABLE personal_report_sections DROP COLUMN IF EXISTS audio_script")
