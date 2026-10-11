"""Add reports.generation_date column + unique index for idempotency.

Prevents duplicate rows when the morning-report cron retries or the operator
manually triggers a re-run.  The (report_type, generation_date) unique
constraint means one row per report type per UTC calendar day; save_report()
uses INSERT ... ON CONFLICT DO UPDATE to update the existing row rather than
appending a duplicate that would confuse /latest.

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-04-19
"""

from alembic import op


revision = 'o5p6q7r8s9t0'
down_revision = 'n4o5p6q7r8s9'
branch_labels = None
depends_on = None


def upgrade():
    # Add the generation_date column (UTC calendar day key)
    op.execute("""
        ALTER TABLE reports
        ADD COLUMN IF NOT EXISTS generation_date DATE
    """)

    # Back-fill existing rows from created_at
    op.execute("""
        UPDATE reports
        SET generation_date = created_at::date
        WHERE generation_date IS NULL
    """)

    # Dedupe before creating the unique index: the morning cron could write
    # multiple rows for the same UTC day (retries, manual re-runs). Keep the newest row per
    # (report_type, generation_date) and discard the older duplicates —
    # the latest row reflects the final report as /latest already serves it.
    op.execute("""
        DELETE FROM reports r
        USING (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY report_type, generation_date
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM reports
            WHERE generation_date IS NOT NULL
        ) dup
        WHERE r.id = dup.id AND dup.rn > 1
    """)

    # Unique index: one row per (report_type, generation_date).
    # Use a partial index (WHERE generation_date IS NOT NULL) so that any
    # legacy NULL rows don't block the constraint from being created.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_type_day
        ON reports (report_type, generation_date)
        WHERE generation_date IS NOT NULL
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_reports_type_day")
    op.execute("ALTER TABLE reports DROP COLUMN IF EXISTS generation_date")
