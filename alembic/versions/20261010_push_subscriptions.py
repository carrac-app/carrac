"""push_subscriptions — web-push subscriptions, one row per user.

Subscriptions were one JSON map in Redis, mirrored to a JSON file under
DATA_DIR, that every web worker and the worker container rewrote whole, so a
write from one process could drop a subscription another had just stored.
This table replaces both. carrac/alerts/push_store.py owns it, and on first
use imports the existing map into it when the table is empty.

A user has one subscription, the browser that subscribed last, so `user_id`
is the key. `subscription` holds the endpoint URL and the two encryption keys.

Revision ID: pushsubs261010
Revises: useralerts261010
Create Date: 2026-10-10
"""
from alembic import op

revision = 'pushsubs261010'
down_revision = 'useralerts261010'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            user_id      INTEGER PRIMARY KEY,
            subscription JSONB NOT NULL,
            created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS push_subscriptions")
