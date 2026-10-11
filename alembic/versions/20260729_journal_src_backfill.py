"""Backfill a src: origin tag onto every untagged trade_journal row.

Every journal entry must carry exactly one ``src:`` tag naming the
decision-maker that produced it (src:agent / src:manual / src:scanner /
src:copy), so trades stay attributable for the head-to-head performance
analysis.  Automated clients emit their own tag; the MCP layer defaults new
rows to ``src:manual``.  This migration closes the historical gap: any
pre-existing row whose ``tags`` JSON carries no ``src:`` entry is
rewritten to include ``src:manual``.

The ``tags`` column is a nullable TEXT holding a JSON array (or NULL).  Three
cases are handled:
  * NULL / empty            -> '["src:manual"]'
  * a JSON array with a src: -> left alone
  * a JSON array without one -> 'src:manual' appended

Idempotent: rows that already carry a ``src:`` tag are skipped by the WHERE
clause, so re-running is a no-op.

Irreversible by design — downgrade() cannot distinguish a backfilled
'src:manual' from one a user set deliberately, so it is a no-op rather than a
lossy strip.

Revision ID: jrnlsrc260729
Revises: mcpintent260729
Create Date: 2026-07-29
"""

from alembic import op

revision = 'jrnlsrc260729'
down_revision = 'mcpintent260729'
branch_labels = None
depends_on = None


def upgrade():
    # Case 1 — no tags at all.
    op.execute(
        """
        UPDATE trade_journal
           SET tags = '["src:manual"]'
         WHERE tags IS NULL
            OR btrim(tags) = ''
            OR btrim(tags) = '[]'
        """
    )
    # Case 2 — a tag array that carries no src: entry.  Matching on the JSON
    # text is sufficient here: '"src:' can only appear as the start of a tag
    # string, because _validate_tags strips and length-caps every element.
    op.execute(
        """
        UPDATE trade_journal
           SET tags = regexp_replace(btrim(tags), '\\]\\s*$', ',"src:manual"]')
         WHERE tags IS NOT NULL
           AND btrim(tags) LIKE '[%]'
           AND btrim(tags) <> '[]'
           AND tags NOT LIKE '%"src:%'
        """
    )


def downgrade():
    # Intentionally a no-op: a backfilled 'src:manual' is indistinguishable
    # from a deliberately-set one, so stripping would destroy real data.
    pass
