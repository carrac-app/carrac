"""Backtest persistence — the one place that owns both backtest tables.

Carrac saves two genuinely different things, and it needs both:

  * a SAVED STRATEGY  — the recipe. Entry/exit formulas, direction, sizing,
    costs. Mutable, reusable, an *input*. Table: ``strategies``.
  * a SAVED RUN       — the outcome. The parameters plus the numbers they
    produced, frozen. Insert-only, an *output*. Table: ``backtest_runs``.

The SQL for both tables lives here rather than inline in ``user_db.py``
(``strategies``) and ``carrac/mcp/tools/backtests.py`` (``backtest_runs``), so
the web UI and the MCP tools see recipes and outcomes alike.

This module is the equivalent of ``carrac/plays/models.py``: a single module
owns the SQL, both entry points import from here, and a new surface gets the
whole picture for free.

⚠ THE TWO TABLES DO NOT LIVE ON THE SAME CONNECTION IN DEV/TEST.
In production both are in one Postgres database — ``user_db`` resolves through
``carrac.db_pool._init_pg_pool``, the same pool. Under SQLite they are separate
FILES: ``backtest_runs`` in ``data/carrac.db`` (db_pool) and ``strategies``
in ``data/users.db`` (user_db). A SQL JOIN across them would work in production
and be impossible to test in CI, which runs SQLite by default. So: runs go
through ``db_pool``, strategies delegate to ``user_db``, and anything that
needs both is combined in Python.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from carrac.db_pool import get_conn, get_db_type, adapt_query, returning_id, get_last_id
from carrac.mcp.backtest_models import MODEL_HOLD_N, MODEL_FULL_SIMULATION

logger = logging.getLogger('carrac.backtests')

# Models that may be written to backtest_runs. Historical Analog is a query
# against the Quant DB, not a run anyone owns, so it is deliberately absent.
PERSISTABLE_MODELS = frozenset({MODEL_HOLD_N, MODEL_FULL_SIMULATION})

# Summary fields lifted out of the stored result blob for list views, per
# model — the two models report genuinely different headline numbers and must
# never be presented in the same column.
# NOTE the casing difference is real, not a typo: the Hold-N engine emits
# snake_case, the Full-Sim engine is a port of the JS one and kept its
# camelCase keys (carrac/backtest_sim.py::_compute_stats).
_SUMMARY_FIELDS = {
    MODEL_HOLD_N: ('hit_rate', 'mean_return', 'trades'),
    MODEL_FULL_SIMULATION: ('winRate', 'totalReturnPercent', 'trades',
                            'maxDrawdownPercent', 'sharpeRatio'),
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _json_loads(val):
    """Deserialize a JSON column that may arrive as str (SQLite) or dict (PG)."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (ValueError, TypeError):
        return {}


def _iso(val):
    return val.isoformat() if hasattr(val, 'isoformat') else val


# ── backtest_runs — saved RESULTS ────────────────────────────────────────────

_RUN_COLUMNS = (
    'id, user_id, name, symbol, expression, timeframe, lookback_bars, '
    'hold_bars, result, model, config, created_at'
)


def ensure_runs_table(conn):
    """Create backtest_runs if missing — SQLite dev/test only.

    Production gets the table (and the model/config columns) from
    alembic; this shim keeps the columns in step so a SQLite run exercises the
    same shape the migration produces.
    """
    if get_db_type() != 'sqlite':
        return
    conn.execute('''
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name VARCHAR(255),
            symbol VARCHAR(50) NOT NULL,
            expression TEXT NOT NULL,
            timeframe VARCHAR(10) NOT NULL,
            lookback_bars INTEGER NOT NULL,
            hold_bars INTEGER,
            result TEXT NOT NULL,
            model VARCHAR(40) NOT NULL DEFAULT 'Hold-N Signal Test',
            config TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    ''')
    # Older SQLite files were created without these two columns.
    for ddl in (
        "ALTER TABLE backtest_runs ADD COLUMN model VARCHAR(40) "
        "NOT NULL DEFAULT 'Hold-N Signal Test'",
        'ALTER TABLE backtest_runs ADD COLUMN config TEXT',
    ):
        try:
            conn.execute(ddl)
        except Exception:
            pass  # already present — SQLite has no ADD COLUMN IF NOT EXISTS


def _row_to_dict(row, columns):
    if isinstance(row, dict):
        return row
    return dict(zip(columns, row))


_RUN_KEYS = tuple(c.strip() for c in _RUN_COLUMNS.split(','))


def save_run(user_id, *, symbol, expression, timeframe, result,
             lookback_bars, hold_bars=None, name=None,
             model=MODEL_HOLD_N, config=None):
    """Persist one completed backtest. Returns the new run_id.

    ``model`` is stored explicitly rather than inferred on read: a Hold-N hit
    rate and a Full-Sim win rate are not the same measurement, and a row that
    cannot say which it is invites exactly the comparison the glossary forbids.
    """
    if model not in PERSISTABLE_MODELS:
        raise ValueError(
            f'{model!r} cannot be persisted as a run — expected one of '
            f'{sorted(PERSISTABLE_MODELS)}'
        )
    with get_conn() as conn:
        ensure_runs_table(conn)
        cursor = conn.cursor()
        ret = returning_id()
        cursor.execute(adapt_query(
            f'INSERT INTO backtest_runs '
            f'(user_id, name, symbol, expression, timeframe, lookback_bars, '
            f'hold_bars, result, model, config) '
            f'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) {ret}'
        ), (
            user_id,
            (name or None) and name[:255],
            symbol[:50],
            expression,
            timeframe[:10],
            lookback_bars,
            hold_bars,
            json.dumps(result),
            model,
            json.dumps(config) if config is not None else None,
        ))
        return get_last_id(cursor)


def list_runs(user_id, symbol=None, limit=20, model=None):
    """Lightweight run summaries, newest first. No full result blobs."""
    limit = min(max(1, int(limit or 20)), 100)
    clauses = ['user_id = ?']
    params = [user_id]
    if symbol:
        clauses.append('symbol = ?')
        params.append(symbol.upper().strip())
    if model:
        clauses.append('model = ?')
        params.append(model)
    params.append(limit)

    with get_conn() as conn:
        ensure_runs_table(conn)
        cursor = conn.cursor()
        cursor.execute(adapt_query(
            f'SELECT {_RUN_COLUMNS} FROM backtest_runs '
            f'WHERE {" AND ".join(clauses)} '
            f'ORDER BY created_at DESC LIMIT ?'
        ), tuple(params))
        rows = cursor.fetchall()

    out = []
    for row in rows:
        r = _row_to_dict(row, _RUN_KEYS)
        result_data = _json_loads(r['result']) or {}
        row_model = r.get('model') or MODEL_HOLD_N
        summary = {
            'run_id': r['id'],
            'name': r['name'],
            'model': row_model,
            'symbol': r['symbol'],
            'expression': r['expression'],
            'timeframe': r['timeframe'],
            'lookback_bars': r['lookback_bars'],
            'hold_bars': r['hold_bars'],
            'created_at': _iso(r['created_at']),
        }
        for field in _SUMMARY_FIELDS.get(row_model, ()):
            summary[field] = result_data.get(field)
        out.append(summary)
    return out


def get_run(user_id, run_id):
    """One full run, or None if it does not exist or belongs to someone else."""
    with get_conn() as conn:
        ensure_runs_table(conn)
        cursor = conn.cursor()
        cursor.execute(adapt_query(
            f'SELECT {_RUN_COLUMNS} FROM backtest_runs WHERE id = ?'
        ), (int(run_id),))
        row = cursor.fetchone()

    if not row:
        return None
    r = _row_to_dict(row, _RUN_KEYS)
    if r['user_id'] != user_id:
        return None  # same answer as not-found — don't confirm the id exists

    return {
        'run_id': r['id'],
        'name': r['name'],
        'model': r.get('model') or MODEL_HOLD_N,
        'symbol': r['symbol'],
        'expression': r['expression'],
        'timeframe': r['timeframe'],
        'lookback_bars': r['lookback_bars'],
        'hold_bars': r['hold_bars'],
        'config': _json_loads(r.get('config')),
        'result': _json_loads(r['result']) or {},
        'created_at': _iso(r['created_at']),
    }


def delete_run(user_id, run_id):
    """Delete one run. Ownership-scoped. Returns True if a row went."""
    with get_conn() as conn:
        ensure_runs_table(conn)
        cursor = conn.cursor()
        cursor.execute(adapt_query(
            'DELETE FROM backtest_runs WHERE id = ? AND user_id = ?'
        ), (int(run_id), user_id))
        return (cursor.rowcount or 0) > 0


# ── strategies — saved CONFIGURATIONS ────────────────────────────────────────
#
# Delegated to user_db rather than reimplemented: in SQLite this table is in a
# different file from backtest_runs, and user_db owns that connection. See the
# module docstring.

def list_strategies(user_id):
    from user_db import user_db
    return user_db.get_strategies(user_id)


def count_strategies(user_id):
    from user_db import user_db
    return user_db.count_strategies(user_id)


def delete_strategy(user_id, strategy_id):
    from user_db import user_db
    return user_db.delete_strategy(user_id, strategy_id)


def save_strategy(user_id, data):
    """Create or update a saved strategy. Returns the strategy id, or None.

    ``user_db.save_strategy`` returns only a bool, so the id it minted was
    unreachable by any caller — fine for a browser that immediately re-lists,
    useless for an API consumer that wants to reference what it just saved.
    The id is minted here and passed down so it can be returned.
    """
    payload = dict(data)
    payload.setdefault('id', str(uuid.uuid4())[:8])
    from user_db import user_db
    ok = user_db.save_strategy(user_id, payload)
    return payload['id'] if ok else None
