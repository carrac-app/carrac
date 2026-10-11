"""audit_log table for admin actions (tier overrides, credit adjustments).

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-04-20
"""

from alembic import op
import sqlalchemy as sa

revision = 'q7r8s9t0u1v2'
down_revision = 'p6q7r8s9t0u1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'audit_log',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('ts', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('actor_user_id', sa.Integer, nullable=True),
        sa.Column('subject_user_id', sa.Integer, nullable=True),
        sa.Column('action', sa.Text, nullable=False),
        sa.Column('before_json', sa.JSON, nullable=True),
        sa.Column('after_json', sa.JSON, nullable=True),
        sa.Column('source', sa.Text, nullable=False),
        sa.Column('note', sa.Text, nullable=True),
    )
    op.create_index('ix_audit_log_subject_ts', 'audit_log', ['subject_user_id', 'ts'])
    op.create_index('ix_audit_log_action_ts', 'audit_log', ['action', 'ts'])


def downgrade():
    op.drop_index('ix_audit_log_action_ts', table_name='audit_log')
    op.drop_index('ix_audit_log_subject_ts', table_name='audit_log')
    op.drop_table('audit_log')
