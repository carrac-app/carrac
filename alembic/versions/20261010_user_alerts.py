"""user_alerts — user alerts of every kind, one row per alert.

Alerts were a JSON file under DATA_DIR that every web worker and the worker
container rewrote whole, so a write from one process could undo a write from
another. This table replaces it. carrac/alerts/store.py owns it, and on first
use imports an existing price_alerts.json into it when the table is empty.

Columns hold what every evaluator filters on; the kind-specific rest of an
alert stays verbatim in `data`. Ids are random 32-bit values (they appear in
URLs), which is why `id` is BIGINT without a sequence.

Revision ID: useralerts261010
Revises: dropusage260826
Create Date: 2026-10-10
"""
from alembic import op

revision = 'useralerts261010'
down_revision = 'dropusage260826'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS user_alerts (
            id         BIGINT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            alert_type TEXT NOT NULL DEFAULT 'price',
            symbol     TEXT,
            active     BOOLEAN NOT NULL DEFAULT TRUE,
            triggered  BOOLEAN NOT NULL DEFAULT FALSE,
            data       JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_user_alerts_user ON user_alerts (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_user_alerts_type_active "
               "ON user_alerts (alert_type, active)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS user_alerts")
