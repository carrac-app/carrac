"""Persist the assembled briefing that produced each shared report.

WHY: `reports.content` stores the generated text but the ~9-source briefing
(facts block, calendar table, Levels in Focus, quant context, chart data,
sentiment, RS rankings, macro research, yesterday's report) that produced it
was assembled in memory and discarded. When a published report
claimed a BTC GEX flip point of 165,694 against a real 69,109, it could not be
diagnosed as a prompt bug vs. a data bug because the source was gone — same
for measuring the Haiku fact verifier's false-positive rate offline.

SCOPE — shared/market reports ONLY (morning, afternoon, weekly, week_ahead,
general). Deliberately NOT wired into `personal_report_sections` — those
carry a user's positions, journal notes and trader profile, materially more
sensitive than a shared market briefing and already treated as untrusted
input (see carrac/grounding/escape.py). See carrac/content_db.py::
save_report and carrac/reports/routes.py::save_personal_section for the
explicit comments on this split.

Additive and nullable — backward compatible, safe on prod, no backfill for
existing rows (their briefing is genuinely gone; NULL is the honest value).

Revision ID: srcbrief260803
Revises: drophist260803
Create Date: 2026-08-03
"""

from alembic import op

revision = 'srcbrief260803'
down_revision = 'drophist260803'
branch_labels = None
depends_on = None


def upgrade():
    # Same idiom as 20260728_api_key_internal_flag.py — additive, nullable,
    # instant on Postgres, no table rewrite, no lock contention.
    op.execute(
        "ALTER TABLE reports ADD COLUMN IF NOT EXISTS source_briefing TEXT"
    )


def downgrade():
    op.execute("ALTER TABLE reports DROP COLUMN IF EXISTS source_briefing")
