"""Drop the dead history-snapshot tables.

options_history, heatmap_history and pattern_log were fed exclusively by
a report-host script (save_history_snapshot.py) POSTing to
POST /api/history/snapshot (the history blueprint). That script was
never deployed and stopped posting long ago, so the tables are dead:
they hold a handful of stale rows (options_history 13, heatmap_history 60,
pattern_log 1). Their only reader,
carrac/content_db.py::build_history_context, has had the
corresponding read/write helpers removed in this same change — it now
returns only the sentiment half, which sentiment_history/regime_history
(unaffected, written independently by carrac/precompute.py) still feed.

The liquidation-heatmap capability moved on to quant.liq_heatmap_snapshots
(carrac/liq_heatmap.py) — untouched by this migration.

DESTRUCTIVE: this DROPs three tables. Data is NOT recoverable via
downgrade() — downgrade() only recreates the empty table structures (matching
scripts/init_postgres.pgsql prior to this change) so the schema can roll
back if something else unexpectedly still depends on their presence. The
real recovery path for the *data* is the pre-promote pg_dump taken by
`make promote` before this migration runs, restored with
scripts/restore-carrac.sh — not this downgrade.

Revision ID: drophist260803
Revises: emaribbon260803
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = 'drophist260803'
down_revision = 'emaribbon260803'
branch_labels = None
depends_on = None

_TABLES = ('options_history', 'heatmap_history', 'pattern_log')


def _existing_tables(bind):
    inspector = sa.inspect(bind)
    return set(inspector.get_table_names())


def upgrade():
    bind = op.get_bind()
    existing = _existing_tables(bind)

    if 'heatmap_history' in existing:
        op.execute('DROP INDEX IF EXISTS idx_heatmap_date')
    if 'pattern_log' in existing:
        op.execute('DROP INDEX IF EXISTS idx_pattern_status')

    for table in _TABLES:
        if table in existing:
            op.execute(f'DROP TABLE IF EXISTS {table}')


def downgrade():
    """Recreate the table structures only — no data. See module docstring:
    the real recovery path for the dropped rows is the pre-promote pg_dump,
    not this downgrade."""
    bind = op.get_bind()
    existing = _existing_tables(bind)

    if 'options_history' not in existing:
        op.execute("""
            CREATE TABLE IF NOT EXISTS options_history (
                date TEXT PRIMARY KEY,
                btc_price REAL,
                btc_iv REAL,
                btc_dvol REAL,
                btc_max_pain REAL,
                btc_put_call_ratio REAL,
                eth_iv REAL,
                eth_dvol REAL,
                eth_max_pain REAL,
                raw_json TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

    if 'heatmap_history' not in existing:
        op.execute("""
            CREATE TABLE IF NOT EXISTS heatmap_history (
                id SERIAL PRIMARY KEY,
                date TEXT NOT NULL,
                price_low REAL NOT NULL,
                price_high REAL NOT NULL,
                direction TEXT NOT NULL,
                rank INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        op.execute('CREATE INDEX IF NOT EXISTS idx_heatmap_date ON heatmap_history(date)')

    if 'pattern_log' not in existing:
        op.execute("""
            CREATE TABLE IF NOT EXISTS pattern_log (
                id SERIAL PRIMARY KEY,
                pattern TEXT NOT NULL,
                asset TEXT,
                timeframe TEXT,
                date_identified TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                date_resolved TEXT,
                notes TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        op.execute('CREATE INDEX IF NOT EXISTS idx_pattern_status ON pattern_log(status)')
