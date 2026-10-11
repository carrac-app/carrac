"""Journal auto-sync from exchanges — connections + fill ledger + dedupe suggestions.

Three new tables supporting read-only Binance/Hyperliquid fill sync into the
trading journal:

- exchange_connections: one row per (user, exchange). Credentials are
  Fernet-encrypted at rest (carrac.secret_box — reused, not a
  new crypto mechanism) and never returned after save. cursor_data carries
  the per-symbol FIFO resume state + last-fill cursor as JSON so incremental
  syncs never need to replay full history.
- exchange_sync_fills: raw-fill idempotency ledger. UNIQUE(connection_id,
  exchange_trade_id) is the safety net that makes re-running a sync over an
  overlapping fetch window a no-op instead of a duplicate import.
- exchange_import_suggestions: the dedupe-vs-manual-entry queue — a fill
  that looks like it might already be hand-journaled is parked here
  ('pending') instead of auto-imported, and surfaced in the UI as
  "import anyway / skip".

See carrac/journal/exchange_connections.py and carrac/journal/exchange_sync.py.

Revision ID: excsync260718
Revises: drwlocalid260716
Create Date: 2026-07-18
"""
from alembic import op

revision = 'excsync260718'
down_revision = 'drwlocalid260716'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS exchange_connections (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            exchange VARCHAR(20) NOT NULL,
            label VARCHAR(50),
            credentials_encrypted TEXT NOT NULL,
            masked_identifier VARCHAR(80),
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            consecutive_errors INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_sync_at TIMESTAMPTZ,
            last_sync_trades_imported INTEGER NOT NULL DEFAULT 0,
            cursor_data TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (user_id, exchange)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_exchange_connections_user ON exchange_connections(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_exchange_connections_status ON exchange_connections(status)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS exchange_sync_fills (
            id SERIAL PRIMARY KEY,
            connection_id INTEGER NOT NULL REFERENCES exchange_connections(id) ON DELETE CASCADE,
            exchange_trade_id VARCHAR(64) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            fill_time TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (connection_id, exchange_trade_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_exchange_sync_fills_conn ON exchange_sync_fills(connection_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS exchange_import_suggestions (
            id SERIAL PRIMARY KEY,
            connection_id INTEGER NOT NULL REFERENCES exchange_connections(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            exchange VARCHAR(20) NOT NULL,
            symbol VARCHAR(20) NOT NULL,
            direction VARCHAR(10) NOT NULL,
            entry_price DOUBLE PRECISION,
            exit_price DOUBLE PRECISION,
            size DOUBLE PRECISION,
            fees DOUBLE PRECISION,
            opened_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ,
            matched_trade_id INTEGER,
            journal_trade_id INTEGER,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            resolved_at TIMESTAMPTZ
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_exchange_import_suggestions_user "
        "ON exchange_import_suggestions(user_id, status)"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS exchange_import_suggestions")
    op.execute("DROP TABLE IF EXISTS exchange_sync_fills")
    op.execute("DROP TABLE IF EXISTS exchange_connections")
