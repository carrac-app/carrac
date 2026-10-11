"""paddle_webhook_inbox — durable event inbox + DLQ for Paddle webhooks.

Every Paddle webhook is written to this table before processing.
Failed events get a last_error + retry_count; after 3 retries they
become DLQ items surfaced via Telegram admin notification.

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-04-20
"""

from alembic import op
import sqlalchemy as sa

revision = 's9t0u1v2w3x4'
down_revision = 'r8s9t0u1v2w3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'paddle_webhook_inbox',
        sa.Column('id', sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column('event_id', sa.Text, nullable=False, unique=True),
        sa.Column('event_type', sa.Text, nullable=False),
        sa.Column('payload', sa.JSON, nullable=False),
        sa.Column('received_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('processed_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text, nullable=True),
        sa.Column('retry_count', sa.Integer, nullable=False, server_default='0'),
    )
    op.create_index('ix_paddle_inbox_unprocessed', 'paddle_webhook_inbox',
                    ['processed_at', 'retry_count'])
    op.create_index('ix_paddle_inbox_event_type', 'paddle_webhook_inbox', ['event_type', 'received_at'])


def downgrade():
    op.drop_index('ix_paddle_inbox_event_type', table_name='paddle_webhook_inbox')
    op.drop_index('ix_paddle_inbox_unprocessed', table_name='paddle_webhook_inbox')
    op.drop_table('paddle_webhook_inbox')
