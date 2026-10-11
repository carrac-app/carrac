"""
Scenario model — structured market hypotheses with conditions engine.

Handles both PostgreSQL (production) and SQLite (dev/test) backends.
"""

import json
import logging
from datetime import datetime, timezone

from carrac.db_pool import get_conn, get_db_type, adapt_query

logger = logging.getLogger('carrac.scenarios')

# ── Enums ──

STATUSES = frozenset({
    'draft', 'watching', 'approaching', 'triggered', 'in_trade',
    'accumulating', 'invalidated', 'resolved',
})

# Columns update_scenario() is allowed to write — hoisted to module level
# So scripts/check_write_columns.py can import this module and
# validate the dynamically-built UPDATE against the real Postgres schema.
SCENARIO_UPDATE_FIELDS = {
    'label', 'asset', 'timeframe', 'thesis', 'alternative_thesis', 'notes',
    'scenario_type', 'direction', 'conviction', 'entry_low', 'entry_high',
    'invalidation', 'targets', 'stop_loss', 'accumulation_budget',
    'accumulation_used', 'conditions', 'milestones', 'source', 'tags',
    'linked_drawing_ids', 'outcome', 'outcome_notes', 'journal_trade_id',
}

# State machine transitions
TRANSITIONS = {
    'draft':        {'watching'},
    'watching':     {'approaching', 'triggered', 'invalidated', 'resolved'},
    'approaching':  {'triggered', 'watching', 'invalidated', 'resolved'},
    'triggered':    {'in_trade', 'accumulating', 'watching', 'invalidated', 'resolved'},
    'in_trade':     {'resolved'},
    'accumulating': {'resolved'},
    'invalidated':  {'resolved', 'watching'},
    'resolved':     set(),  # terminal
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(obj):
    """Serialize to JSON string for DB storage."""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    return json.dumps(obj)


def _json_loads(val):
    """Deserialize JSON from DB."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return val


def _ensure_table(conn):
    """Create scenarios table if it doesn't exist (SQLite dev/test only)."""
    if get_db_type() != 'sqlite':
        return
    conn.execute('''
        CREATE TABLE IF NOT EXISTS scenarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            asset VARCHAR(20),
            timeframe VARCHAR(10),
            label TEXT,
            thesis TEXT,
            alternative_thesis TEXT,
            notes TEXT,
            scenario_type VARCHAR(20) NOT NULL DEFAULT 'trade_setup',
            direction VARCHAR(10),
            conviction INTEGER,
            entry_low REAL,
            entry_high REAL,
            invalidation REAL,
            targets TEXT DEFAULT '[]',
            stop_loss REAL,
            accumulation_budget REAL,
            accumulation_used REAL DEFAULT 0,
            conditions TEXT DEFAULT '{"rules": [], "logic": "all"}',
            milestones TEXT DEFAULT '[]',
            status VARCHAR(20) NOT NULL DEFAULT 'watching',
            status_changed_at TEXT,
            is_active BOOLEAN DEFAULT 1,
            journal_trade_id INTEGER,
            linked_drawing_ids TEXT DEFAULT '[]',
            outcome VARCHAR(20),
            outcome_notes TEXT,
            resolved_at TEXT,
            source VARCHAR(50),
            tags TEXT,
            screenshot_path TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    ''')


def _row_to_dict(row):
    """Convert DB row to scenario dict with JSON fields deserialized."""
    if row is None:
        return None
    d = dict(row)
    # Deserialize JSON fields
    for field in ('targets', 'conditions', 'linked_drawing_ids', 'milestones'):
        if field in d:
            d[field] = _json_loads(d[field])
    # Deserialize tags (PostgreSQL TEXT[] vs SQLite JSON string)
    if 'tags' in d and isinstance(d['tags'], str):
        d['tags'] = _json_loads(d['tags'])
    return d


# ── CRUD ──


def get_scenario(scenario_id, user_id=None):
    """Get a single scenario by ID. If user_id given, enforces ownership."""
    with get_conn() as conn:
        _ensure_table(conn)
        if user_id is not None:
            cur = conn.execute(
                adapt_query('SELECT * FROM scenarios WHERE id = ? AND user_id = ?'),
                (scenario_id, user_id),
            )
        else:
            cur = conn.execute(
                adapt_query('SELECT * FROM scenarios WHERE id = ?'),
                (scenario_id,),
            )
        return _row_to_dict(cur.fetchone())


def get_active_scenarios(user_id=None):
    """Get all non-resolved scenarios. If user_id=None, returns all users' (for engine)."""
    clauses = ["status NOT IN ('resolved', 'draft')"]
    params = []

    if user_id is not None:
        clauses.append('user_id = ?')
        params.append(user_id)

    where = ' AND '.join(clauses)
    sql = adapt_query(f'SELECT * FROM scenarios WHERE {where} ORDER BY updated_at DESC')

    with get_conn() as conn:
        _ensure_table(conn)
        cur = conn.execute(sql, params)
        return [_row_to_dict(r) for r in cur.fetchall()]


def update_scenario(scenario_id, user_id, **fields):
    """Update scenario fields. Returns updated scenario or None if not found/not owned."""
    # Verify ownership first
    existing = get_scenario(scenario_id, user_id)
    if not existing:
        return None

    allowed = SCENARIO_UPDATE_FIELDS

    sets = []
    params = []
    for key, val in fields.items():
        if key not in allowed:
            continue
        if key in ('targets', 'conditions', 'linked_drawing_ids', 'milestones'):
            val = _json_dumps(val)
        elif key == 'tags' and get_db_type() == 'sqlite':
            val = _json_dumps(val)
        sets.append(f'{key} = ?')
        params.append(val)

    if not sets:
        return existing

    sets.append('updated_at = ?')
    params.append(_now_iso())
    params.extend([scenario_id, user_id])

    set_clause = ', '.join(sets)
    sql = adapt_query(f'UPDATE scenarios SET {set_clause} WHERE id = ? AND user_id = ?')

    with get_conn() as conn:
        _ensure_table(conn)
        conn.execute(sql, params)

    return get_scenario(scenario_id, user_id)


def transition_status(scenario_id, user_id, new_status, *, outcome=None, outcome_notes=None):
    """Transition scenario to a new status. Validates state machine. Returns updated scenario or None."""
    existing = get_scenario(scenario_id, user_id)
    if not existing:
        return None

    current = existing['status']
    if new_status not in TRANSITIONS.get(current, set()):
        logger.warning(f"Invalid transition: {current} -> {new_status} for scenario {scenario_id}")
        return None

    now = _now_iso()
    sets = ['status = ?', 'status_changed_at = ?', 'updated_at = ?']
    params = [new_status, now, now]

    if new_status == 'resolved':
        sets.append('resolved_at = ?')
        params.append(now)
        if outcome:
            sets.append('outcome = ?')
            params.append(outcome)
        if outcome_notes:
            sets.append('outcome_notes = ?')
            params.append(outcome_notes)

    # Sync is_active with status
    if new_status in ('resolved', 'invalidated'):
        sets.append('is_active = ?')
        params.append(False)
    elif current in ('resolved', 'invalidated') and new_status == 'watching':
        sets.append('is_active = ?')
        params.append(True)

    params.extend([scenario_id, user_id])
    set_clause = ', '.join(sets)
    sql = adapt_query(f'UPDATE scenarios SET {set_clause} WHERE id = ? AND user_id = ?')

    with get_conn() as conn:
        _ensure_table(conn)
        conn.execute(sql, params)

    return get_scenario(scenario_id, user_id)


def link_drawing(scenario_id, user_id, drawing_id):
    """Add a drawing ID to a scenario's linked_drawing_ids."""
    existing = get_scenario(scenario_id, user_id)
    if not existing:
        return None

    linked = existing.get('linked_drawing_ids') or []
    if drawing_id not in linked:
        linked.append(drawing_id)
        return update_scenario(scenario_id, user_id, linked_drawing_ids=linked)
    return existing


def unlink_drawing(scenario_id, user_id, drawing_id):
    """Remove a drawing ID from a scenario's linked_drawing_ids."""
    existing = get_scenario(scenario_id, user_id)
    if not existing:
        return None

    linked = existing.get('linked_drawing_ids') or []
    if drawing_id in linked:
        linked.remove(drawing_id)
        return update_scenario(scenario_id, user_id, linked_drawing_ids=linked)
    return existing
