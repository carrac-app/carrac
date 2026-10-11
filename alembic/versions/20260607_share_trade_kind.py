"""Generalize play_shares for kind='trade': nullable play_id + trade_id FK.

Trade-result cards share the play-share rail.
``play_id`` loses NOT NULL (chart shares will have no subject row at all;
trade shares set ``trade_id`` instead). ``trade_id`` references
``trade_journal`` with ON DELETE CASCADE — deleting a trade removes its
share, same as plays (public URL then 404s).

Both ALTERs are instant (table holds a handful of rows).

Revision ID: pshare26060703
Revises: pshare26060702
Create Date: 2026-06-07
"""

from alembic import op

revision = 'pshare26060703'
down_revision = 'pshare26060702'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE play_shares ALTER COLUMN play_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE play_shares "
        "ADD COLUMN IF NOT EXISTS trade_id INTEGER "
        "REFERENCES trade_journal(id) ON DELETE CASCADE"
    )


def downgrade():
    op.execute("ALTER TABLE play_shares DROP COLUMN IF EXISTS trade_id")
    # Restoring NOT NULL requires no NULL play_id rows (revoke/delete trade
    # shares first); acceptable for a dev-only downgrade.
    op.execute("DELETE FROM play_shares WHERE play_id IS NULL")
    op.execute("ALTER TABLE play_shares ALTER COLUMN play_id SET NOT NULL")
