"""Restore ai_usage_log.created_at DEFAULT now().

The column had no DEFAULT, so every insert wrote NULL created_at (269/328 rows
NULL; only 03-06..03-15 had timestamps). The endpoint returned 200 and rows
persisted, but every time-windowed query (the weekly usage review's cost block,
billing audits) silently excluded them — making LLM spend look like $0 since
mid-March. Restoring the default fixes all insert paths (log_ai_usage + any
future writer) without touching the hot insert code.

Historical NULL rows are left as-is (their true timestamps are unrecoverable —
there is no other time column to backfill from).

Revision ID: z6a7b8c9d0e1
Revises: y5z6a7b8c9d0
Create Date: 2026-05-29
"""
from alembic import op

revision = 'z6a7b8c9d0e1'
down_revision = 'y5z6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE ai_usage_log ALTER COLUMN created_at SET DEFAULT now()")
    # Remove a leftover diagnostic row that was inserted with a NULL created_at.
    op.execute("DELETE FROM ai_usage_log WHERE feature = '__diag_logcost__'")


def downgrade():
    op.execute("ALTER TABLE ai_usage_log ALTER COLUMN created_at DROP DEFAULT")
