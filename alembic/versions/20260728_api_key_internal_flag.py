"""Add is_internal flag to api_keys (metering/analytics exclusion for an internal key).

An internal automation client calls Carrac's MCP HTTP API with its own per-user API key.
This flag lets that one key be excluded from: (a) tier metering / rate-limit accounting
(carrac/rest_rate_limit.py, carrac/fastapi_app/routers/mcp.py), (b) usage analytics
(carrac/usage.py's mcd:usage: daily rollup), and (c) the weekly usage review's LLM/usage spend
attribution (ai_usage_log rows via carrac/ai/billing.py::settle()). Nothing else about
API-key behaviour changes — every existing key defaults to is_internal=FALSE and is
completely unaffected.

Revision ID: apikeyint260728
Revises: excsync260718
Create Date: 2026-07-28
"""
from alembic import op

revision = 'apikeyint260728'
down_revision = 'excsync260718'
branch_labels = None
depends_on = None


def upgrade():
    # Additive, nullable-with-default, instant on Postgres — no table rewrite, no lock
    # contention (same idiom as 20260714_scanner_history_setup_type.py).
    op.execute(
        "ALTER TABLE api_keys "
        "ADD COLUMN IF NOT EXISTS is_internal BOOLEAN DEFAULT FALSE"
    )


def downgrade():
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS is_internal")
