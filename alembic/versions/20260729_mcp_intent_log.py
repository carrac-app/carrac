"""mcp_intent_log table — MCP request-intent telemetry.

Every MCP tools/call dispatch (carrac/mcp/registry.py::call_tool, via
carrac/mcp/runtime.py::log_intent_dispatch) writes one row here: the
model-authored `user_intent` paraphrase on every tool, `unmet_need` on the
~10 entry-point tools, plus latency/outcome/sequence metadata. `user_ref`
is HMAC-SHA256 pseudonymised (see runtime._hash_user_ref) — never a raw
user_id or email. 90-day retention, purged nightly by worker.py's
intent-log-purge scheduler (runtime.purge_old_intent_log).

Revision ID: mcpintent260729
Revises: apikeyint260728
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa

revision = 'mcpintent260729'
down_revision = 'apikeyint260728'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'mcp_intent_log',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('ts', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('tool', sa.Text, nullable=False),
        sa.Column('tier', sa.Text, nullable=False),
        # Pseudonymised (HMAC-SHA256), never a raw user_id or email. NULL for
        # the shared admin key (no per-user binding to pseudonymise).
        sa.Column('user_ref', sa.Text, nullable=True),
        sa.Column('user_intent', sa.Text, nullable=True),
        sa.Column('unmet_need', sa.Text, nullable=True),
        sa.Column('latency_ms', sa.Float, nullable=False),
        sa.Column('ok', sa.Boolean, nullable=False),
        sa.Column('result_empty', sa.Boolean, nullable=False, server_default=sa.text('FALSE')),
        sa.Column('error_kind', sa.Text, nullable=True),
        # Previous tool called by the same caller (rate-limit key shape) —
        # session/sequence reconstruction.
        sa.Column('prev_tool', sa.Text, nullable=True),
    )
    op.create_index('ix_mcp_intent_log_ts', 'mcp_intent_log', ['ts'])
    op.create_index('ix_mcp_intent_log_tool_ts', 'mcp_intent_log', ['tool', 'ts'])
    op.create_index('ix_mcp_intent_log_tier_ts', 'mcp_intent_log', ['tier', 'ts'])
    # Partial index — the admin "Unmet Needs" panel only ever reads rows
    # where unmet_need is set, which is a small fraction of the table.
    op.create_index(
        'ix_mcp_intent_log_unmet_ts',
        'mcp_intent_log',
        ['ts'],
        postgresql_where=sa.text('unmet_need IS NOT NULL'),
    )


def downgrade():
    op.drop_index('ix_mcp_intent_log_unmet_ts', table_name='mcp_intent_log')
    op.drop_index('ix_mcp_intent_log_tier_ts', table_name='mcp_intent_log')
    op.drop_index('ix_mcp_intent_log_tool_ts', table_name='mcp_intent_log')
    op.drop_index('ix_mcp_intent_log_ts', table_name='mcp_intent_log')
    op.drop_table('mcp_intent_log')
