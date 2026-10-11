"""drawings: backfill metadata.local_id for MCP/REST-created rows (stuck-drawing fix).

Every single-delete UI path (keyboard Delete, right-click Delete, Drawings-tab
delete) removes a drawing BY metadata->>'local_id' via
DELETE /api/drawings/by-local-id — but the MCP and REST creation paths never set
one. Such rows are silently undeletable from the chart (the DELETE matches 0
rows, the 200 {"success": false} response goes unchecked client-side) and
resurrect on every chart load under the synthetic client id 'srv_<id>'.
Symptom: the "stuck Position long @ 0.00" drawings on ETH/HYG/XLF/XLP/XRP.

save_drawing() now backfills local_id at insert time; this migration backfills
the already-existing rows with the identical 'srv_<id>' convention so the
stuck drawings become deletable through the normal UI paths.

Revision ID: drwlocalid260716
Revises: shsetup26071401
Create Date: 2026-07-16
"""
from alembic import op


revision = 'drwlocalid260716'
down_revision = 'shsetup26071401'
branch_labels = None
depends_on = None


def upgrade():
    # Postgres-only expressions (staging + prod are Postgres). metadata is a
    # TEXT column holding JSON: NULL/empty become a fresh object, existing
    # objects get the key set. Idempotent — rows with a local_id are untouched.
    op.execute("""
        UPDATE drawings
        SET metadata = CASE
            WHEN metadata IS NULL OR metadata = ''
                THEN jsonb_build_object('local_id', 'srv_' || id)::text
            ELSE jsonb_set(metadata::jsonb, '{local_id}', to_jsonb('srv_' || id::text))::text
        END
        WHERE metadata IS NULL OR metadata = ''
           OR (metadata::jsonb ->> 'local_id') IS NULL
    """)


def downgrade():
    # Data backfill — deliberately no-op on downgrade (stripping local_id
    # would re-break UI deletes for these rows).
    pass
