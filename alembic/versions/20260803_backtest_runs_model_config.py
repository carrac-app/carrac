"""backtest_runs learns which model produced it, and can hold a Full-Sim config.

Until now `backtest_runs` could only describe a Hold-N Signal Test: a single
`expression`, a mandatory `hold_bars`, and nothing to say which of Carrac's
three backtest models the row came from (the reader hardcoded
MODEL_HOLD_N because nothing else could ever be written).

Full Simulation (`backtest_simulate`) has an entry AND an exit
expression, stops/targets, sizing and costs, and no concept of hold_bars — so
it had no persistence path at all. Rather than invent a third table, this
widens the run-history table to record the model explicitly and carry the
producing configuration as JSON.

Three changes, all backward compatible:
  * `model`      — NOT NULL, defaults to the Hold-N name so every existing row
                   is correctly labelled without a backfill pass.
  * `config`     — nullable JSONB, the full parameter set that produced the run.
  * `hold_bars`  — NOT NULL dropped; meaningless for Full Simulation.

`strategies` is deliberately NOT touched. It stores a reusable configuration,
not a result.

Revision ID: btmodel260803
Revises: jrnlsrc260729
Create Date: 2026-08-03
"""

from alembic import op


revision = 'btmodel260803'
down_revision = 'jrnlsrc260729'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE backtest_runs
        ADD COLUMN IF NOT EXISTS model VARCHAR(40) NOT NULL
        DEFAULT 'Hold-N Signal Test'
    """)
    op.execute('ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS config JSONB')
    # Full Simulation has no hold_bars. Existing Hold-N rows keep theirs.
    op.execute('ALTER TABLE backtest_runs ALTER COLUMN hold_bars DROP NOT NULL')
    # History is browsed per-user, newest first, and now filtered by model.
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_backtest_runs_user_model '
        'ON backtest_runs(user_id, model, created_at DESC)'
    )


def downgrade():
    op.execute('DROP INDEX IF EXISTS idx_backtest_runs_user_model')
    # Rows written by Full Simulation have no hold_bars; give them the Hold-N
    # default rather than failing the NOT NULL restore.
    op.execute('UPDATE backtest_runs SET hold_bars = 5 WHERE hold_bars IS NULL')
    op.execute('ALTER TABLE backtest_runs ALTER COLUMN hold_bars SET NOT NULL')
    op.execute('ALTER TABLE backtest_runs DROP COLUMN IF EXISTS config')
    op.execute('ALTER TABLE backtest_runs DROP COLUMN IF EXISTS model')
