"""pending_paddle_events quarantine table.

Holds transaction.completed / subscription.activated events that arrive
before the paddle_customer_id → user mapping is confirmed. Drained by the
customer.created handler once the mapping is established.

Revision ID: r8s9t0u1v2w3
Revises: q7r8s9t0u1v2
Create Date: 2026-04-20
"""

from alembic import op
import sqlalchemy as sa

revision = 'r8s9t0u1v2w3'
down_revision = 'q7r8s9t0u1v2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'pending_paddle_events',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('paddle_customer_id', sa.Text, nullable=False),
        sa.Column('event_id', sa.Text, nullable=False, unique=True),
        sa.Column('event_type', sa.Text, nullable=False),
        sa.Column('event_json', sa.JSON, nullable=False),
        sa.Column('received_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('processed_at', sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index('ix_pending_paddle_customer', 'pending_paddle_events', ['paddle_customer_id'])
    op.create_index('ix_pending_paddle_unprocessed', 'pending_paddle_events', ['processed_at'])


def downgrade():
    op.drop_index('ix_pending_paddle_unprocessed', table_name='pending_paddle_events')
    op.drop_index('ix_pending_paddle_customer', table_name='pending_paddle_events')
    op.drop_table('pending_paddle_events')
