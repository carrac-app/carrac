"""Saved strategies can hold the whole setup, not just the formulas.

This adds ONE `params` JSONB column holding the full configuration snapshot
rather than ten more scalar columns. The panel's parameter set is still moving
(the modelling assumptions were just added, and Strategy Lab work may
add more) — a JSON bag absorbs that without a migration each time, and the
alternative is being back here for an eleventh column.

The seven existing scalars are kept and still written, so anything reading the
old shape (the Flask route's response, the MCP tool, existing rows) keeps
working. Readers overlay `params` on top of the scalars, so an old row without
`params` behaves exactly as it does today.

This is also the FIRST alembic migration `strategies` has ever had — its DDL
has only ever lived in scripts/init_postgres.pgsql (applied at DB creation, or
via the baseline blob migration) and user_db.py's SQLite-only inline schema.
Both are updated to match so a fresh database and a migrated one agree.

Revision ID: stratparams260803
Revises: btmodel260803
Create Date: 2026-08-03
"""

from alembic import op


revision = 'stratparams260803'
down_revision = 'btmodel260803'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE strategies ADD COLUMN IF NOT EXISTS params JSONB')


def downgrade():
    op.execute('ALTER TABLE strategies DROP COLUMN IF EXISTS params')
