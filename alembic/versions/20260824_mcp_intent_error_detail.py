"""mcp_intent_log.error_detail — record WHY a tool call failed.

`error_kind` stores `type(e).__name__` and nothing else
(carrac/mcp/registry.py::call_tool). Every failure of every tool is therefore
labelled with its exception CLASS, and the message — the part that says which
argument was wrong, which gate refused, which upstream 4xx'd — is discarded at
the moment it is known.

With only the class stored, a tool that fails on every call (for example
`log_trade` raising `ToolError`) shows up as thousands of identical rows.
Diagnosing it means reading the tool's source and guessing which of its
`raise ToolError(...)` lines fired, because the answer was never stored. A
failure record that proves something broke but cannot say what is the same
defect an alert dead-letter queue has when it drops the error text.

`error_kind` is deliberately left alone so existing GROUP BY queries and the
/api/admin/intents endpoint keep their low cardinality; the message goes in a
new nullable column beside it.

Revision ID: mcperrdet260824
Revises: srcbrief260803
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa

revision = 'mcperrdet260824'
down_revision = 'srcbrief260803'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE mcp_intent_log ADD COLUMN IF NOT EXISTS error_detail TEXT"
    )


def downgrade():
    op.execute("ALTER TABLE mcp_intent_log DROP COLUMN IF EXISTS error_detail")
