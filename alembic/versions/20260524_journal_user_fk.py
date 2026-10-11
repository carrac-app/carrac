"""Hardening: add user_id FK to all 7 journal tables referencing users(id) ON DELETE CASCADE.

Tables patched: trade_journal, journal_sessions, journal_screenshots,
setup_checklists, journal_views, journal_playbooks, journal_goals.

Safety: the migration first deletes any orphaned rows (user_id values that do
not exist in the users table) before adding the constraint.  This is the
conservative choice — orphaned journal rows belong to no user and can never be
reached by a normal user session, so deleting them is safer than nulling
user_id (which the NOT NULL constraint would reject anyway).

Idempotent: each ADD CONSTRAINT is wrapped in a DO $$…$$ block that checks
pg_constraint first, so re-running the migration after a partial failure is
safe.

Revision ID: u1v2w3x4y5z6
Revises: t0u1v2w3x4y5
Create Date: 2026-05-24
"""

from alembic import op

revision = 'u1v2w3x4y5z6'
down_revision = 't0u1v2w3x4y5'
branch_labels = None
depends_on = None

# The 7 tables that need the FK.  Each tuple is (table_name, constraint_name).
_TABLES = [
    ('trade_journal',       'fk_trade_journal_user_id'),
    ('journal_sessions',    'fk_journal_sessions_user_id'),
    ('journal_screenshots', 'fk_journal_screenshots_user_id'),
    ('setup_checklists',    'fk_setup_checklists_user_id'),
    ('journal_views',       'fk_journal_views_user_id'),
    ('journal_playbooks',   'fk_journal_playbooks_user_id'),
    ('journal_goals',       'fk_journal_goals_user_id'),
]


def upgrade():
    for table, constraint in _TABLES:
        # Step 1 — delete orphaned rows (user_id not in users).
        # We do this unconditionally; on a clean DB the DELETE touches 0 rows.
        op.execute(f"""
            DELETE FROM {table}
            WHERE user_id NOT IN (SELECT id FROM users)
        """)

        # Step 2 — add the FK if it does not already exist.
        op.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = '{constraint}'
                      AND conrelid = '{table}'::regclass
                ) THEN
                    ALTER TABLE {table}
                        ADD CONSTRAINT {constraint}
                        FOREIGN KEY (user_id)
                        REFERENCES users(id)
                        ON DELETE CASCADE;
                END IF;
            END
            $$;
        """)


def downgrade():
    for table, constraint in reversed(_TABLES):
        op.execute(f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = '{constraint}'
                      AND conrelid = '{table}'::regclass
                ) THEN
                    ALTER TABLE {table} DROP CONSTRAINT {constraint};
                END IF;
            END
            $$;
        """)
