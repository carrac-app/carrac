"""Add prompt/response hash columns + conversation_id to ai_usage_log.

When a user claims "the AI told me to short and I lost money",
we currently have no way to retrieve what the model actually said. This
migration adds three cheap columns:

  prompt_sha256    — SHA-256 hex of the rendered system+user prompt
  response_sha256  — SHA-256 hex of the full assistant reply
  conversation_id  — links multiple turns in one chat session

Raw prompt/response bodies are written to data/ai-logs/<YYYY-MM-DD>/
<user_id>/<conversation_id>.jsonl by the settle() path. Retention is 30
days, enforced by scripts/ai_log_retention_prune.py run from cron.

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r8
Create Date: 2026-04-19
"""

from alembic import op


revision = 'n4o5p6q7r8s9'
down_revision = 'm3n4o5p6q7r8'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE ai_usage_log
        ADD COLUMN IF NOT EXISTS prompt_sha256   VARCHAR(64),
        ADD COLUMN IF NOT EXISTS response_sha256 VARCHAR(64),
        ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(64)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_ai_usage_log_conversation
            ON ai_usage_log (user_id, conversation_id)
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_ai_usage_log_conversation")
    op.execute("ALTER TABLE ai_usage_log DROP COLUMN IF EXISTS conversation_id")
    op.execute("ALTER TABLE ai_usage_log DROP COLUMN IF EXISTS response_sha256")
    op.execute("ALTER TABLE ai_usage_log DROP COLUMN IF EXISTS prompt_sha256")
