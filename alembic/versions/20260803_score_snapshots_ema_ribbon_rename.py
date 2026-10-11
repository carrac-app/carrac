"""score_snapshots: rename larsson_* columns to ema_ribbon_*.

Prod Postgres's score_snapshots table was created with the pre-rename
internal names larsson_trend / larsson_gap_pct, while every Python writer
(carrac/content_db.py::insert_score_snapshot, built from
carrac/scanner/snapshot.py::_do_capture) has only ever written
ema_ribbon_trend / ema_ribbon_gap_pct. Every INSERT into score_snapshots on
Postgres has therefore failed with "column ... does not exist" since the
table was created — silently, because carrac/scanner/snapshot.py wraps the
capture in a best-effort try/except that only logs a warning. No Python code
anywhere reads or writes larsson_* (grep-verified), so the app's names are
the ones being kept; the table is renamed to match.

SQLite dev/test never had the larsson_* names — its dynamic init in
content_db.py already creates ema_ribbon_trend / ema_ribbon_gap_pct directly
— so this migration is a Postgres-only concern.

Idempotent by construction: the baseline migration (fa5a891c4577) executes
scripts/init_postgres.pgsql live at migrate time, and that file
creates ema_ribbon_* directly. So on a FRESH
database this migration runs after the table already has the new names, and
must no-op rather than error. We inspect the live columns and only rename
when the old name is present and the new name is not.

Revision ID: emaribbon260803
Revises: stratparams260803
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = 'emaribbon260803'
down_revision = 'stratparams260803'
branch_labels = None
depends_on = None

_RENAMES = [
    ('larsson_trend', 'ema_ribbon_trend'),
    ('larsson_gap_pct', 'ema_ribbon_gap_pct'),
]


def _existing_columns(bind, table_name):
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return set()
    return {col['name'] for col in inspector.get_columns(table_name)}


def upgrade():
    bind = op.get_bind()
    existing = _existing_columns(bind, 'score_snapshots')
    for old_name, new_name in _RENAMES:
        if old_name in existing and new_name not in existing:
            op.alter_column('score_snapshots', old_name, new_column_name=new_name)


def downgrade():
    bind = op.get_bind()
    existing = _existing_columns(bind, 'score_snapshots')
    for old_name, new_name in _RENAMES:
        if new_name in existing and old_name not in existing:
            op.alter_column('score_snapshots', new_name, new_column_name=old_name)
