"""baseline schema

Represents the baseline Convexity schema.
This migration is NOT run on existing databases — they get stamped instead.
For fresh databases, init_postgres.pgsql handles creation via Docker entrypoint.

Revision ID: fa5a891c4577
Revises:
Create Date: 2026-03-25
"""
import os

from alembic import op

revision = 'fa5a891c4577'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Read and execute the full schema SQL.
    # Uses IF NOT EXISTS throughout so it's safe even if partially applied.
    schema_path = os.path.join(
        os.path.dirname(__file__), '..', '..', 'scripts', 'init_postgres.pgsql'
    )
    with open(schema_path) as f:
        sql = f.read()
    op.execute(sql)


def downgrade():
    # Dropping all tables is intentionally NOT implemented.
    # Use pg_dump/pg_restore for disaster recovery.
    raise RuntimeError("Cannot downgrade baseline — restore from backup instead")
