"""
Play model — thesis-first market belief tracking.

Two-axis model: Type (directional/macro/event) × Approach (single/scaled/defensive).
Handles both PostgreSQL (production) and SQLite (dev/test) backends.
"""

import json
import logging
from datetime import datetime, timezone

from carrac.db_pool import get_conn, get_db_type, adapt_query, returning_id, get_last_id

logger = logging.getLogger('carrac.plays')

# ── Enums ──

PLAY_TYPES = frozenset({'directional', 'macro', 'event'})
PLAY_APPROACHES = frozenset({'single', 'scaled', 'defensive'})
PLAY_DIRECTIONS = frozenset({'long', 'short', 'neutral', 'conditional'})
PLAY_STATUSES = frozenset({'watching', 'acting', 'completed', 'invalidated', 'expired'})
PLAY_TIMEFRAMES = frozenset({'swing', 'position', 'until_event', 'open_ended'})

# Terminal statuses — plays in these states don't count toward tier limits
TERMINAL_STATUSES = frozenset({'completed', 'invalidated'})

# Columns update_play() is allowed to write — hoisted to module level
# So scripts/check_write_columns.py can import this module and
# validate the dynamically-built UPDATE against the real Postgres schema.
PLAY_UPDATE_FIELDS = {
    'title', 'thesis', 'type', 'approach', 'direction',
    'primary_asset', 'related_assets', 'trigger_asset',
    'conditions', 'invalidation', 'timeframe', 'expiry',
    'action_plan', 'linked_drawing_ids',
    'report_tracking', 'last_report_note', 'scorer_snapshot',
    'metadata',
}

# State machine transitions
TRANSITIONS = {
    'watching':     {'acting', 'completed', 'invalidated', 'expired'},
    'acting':       {'completed', 'invalidated'},
    'expired':      {'watching'},          # reactivatable
    'completed':    set(),                 # terminal
    'invalidated':  set(),                 # terminal
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


_JSON_FIELDS = ('related_assets', 'conditions', 'invalidation',
                'linked_drawing_ids', 'scorer_snapshot', 'metadata')


def _row_to_dict(row):
    """Convert DB row to play dict with JSON fields deserialized."""
    if row is None:
        return None
    d = dict(row)
    for field in _JSON_FIELDS:
        if field in d:
            d[field] = _json_loads(d[field])
    # SQLite returns 0/1 for booleans — normalize
    if 'report_tracking' in d:
        d['report_tracking'] = bool(d['report_tracking'])
    return d


def _ensure_table(conn):
    """Create plays table if it doesn't exist (SQLite dev/test only)."""
    if get_db_type() != 'sqlite':
        return
    conn.execute('''
        CREATE TABLE IF NOT EXISTS plays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            thesis TEXT,
            type VARCHAR(20) NOT NULL DEFAULT 'directional',
            approach VARCHAR(20) NOT NULL DEFAULT 'single',
            direction VARCHAR(20) DEFAULT 'long',
            primary_asset VARCHAR(20),
            related_assets TEXT DEFAULT '[]',
            trigger_asset VARCHAR(20),
            conditions TEXT DEFAULT '{"rules":[],"logic":"all"}',
            invalidation TEXT,
            timeframe VARCHAR(20),
            expiry TEXT,
            action_plan TEXT,
            linked_drawing_ids TEXT DEFAULT '[]',
            status VARCHAR(20) NOT NULL DEFAULT 'watching',
            status_changed_at TEXT,
            resolved_at TEXT,
            resolution TEXT,
            report_tracking BOOLEAN DEFAULT 1,
            last_report_note TEXT,
            scorer_snapshot TEXT,
            metadata TEXT DEFAULT '{}',
            created_at TEXT,
            updated_at TEXT
        )
    ''')


# ── CRUD ──

def play_create(user_id, *, title, thesis=None, type='directional',
                approach='single', direction='long', primary_asset=None,
                related_assets=None, trigger_asset=None, conditions=None,
                invalidation=None, timeframe=None, expiry=None,
                action_plan=None, linked_drawing_ids=None,
                report_tracking=True, scorer_snapshot=None, metadata=None):
    """Create a new play. Returns the created play dict."""
    now = _now_iso()

    with get_conn() as conn:
        _ensure_table(conn)

        sql = adapt_query('''
            INSERT INTO plays (
                user_id, title, thesis, type, approach, direction,
                primary_asset, related_assets, trigger_asset,
                conditions, invalidation, timeframe, expiry,
                action_plan, linked_drawing_ids,
                status, status_changed_at,
                report_tracking, scorer_snapshot, metadata,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?
            )
        ''') + returning_id()

        cur = conn.execute(sql, (
            user_id, title, thesis, type, approach, direction,
            primary_asset,
            _json_dumps(related_assets or []),
            trigger_asset,
            _json_dumps(conditions or {'rules': [], 'logic': 'all'}),
            _json_dumps(invalidation),
            timeframe, expiry,
            action_plan,
            _json_dumps(linked_drawing_ids or []),
            'watching', now,
            report_tracking,
            _json_dumps(scorer_snapshot),
            _json_dumps(metadata or {}),
            now, now,
        ))
        play_id = get_last_id(cur, conn)

    return get_play(play_id, user_id)


def get_play(play_id, user_id=None):
    """Get a single play by ID. If user_id given, enforces ownership."""
    with get_conn() as conn:
        _ensure_table(conn)
        if user_id is not None:
            cur = conn.execute(
                adapt_query('SELECT * FROM plays WHERE id = ? AND user_id = ?'),
                (play_id, user_id),
            )
        else:
            cur = conn.execute(
                adapt_query('SELECT * FROM plays WHERE id = ?'),
                (play_id,),
            )
        return _row_to_dict(cur.fetchone())


def list_plays(user_id, *, status=None, asset=None, play_type=None,
               include_resolved=False, limit=50, offset=0):
    """List plays for a user with optional filters."""
    clauses = ['user_id = ?']
    params = [user_id]

    if status:
        clauses.append('status = ?')
        params.append(status)
    elif not include_resolved:
        clauses.append("status NOT IN ('completed', 'invalidated')")

    if asset:
        clauses.append('primary_asset = ?')
        params.append(asset)

    if play_type:
        clauses.append('type = ?')
        params.append(play_type)

    where = ' AND '.join(clauses)
    sql = adapt_query(f'''
        SELECT * FROM plays WHERE {where}
        ORDER BY
            CASE status
                WHEN 'acting' THEN 1
                WHEN 'watching' THEN 2
                WHEN 'expired' THEN 3
                WHEN 'invalidated' THEN 4
                WHEN 'completed' THEN 5
            END,
            updated_at DESC
        LIMIT ? OFFSET ?
    ''')
    params.extend([limit, offset])

    with get_conn() as conn:
        _ensure_table(conn)
        cur = conn.execute(sql, params)
        plays = [_row_to_dict(r) for r in cur.fetchall()]

    # Attach active public-share state (slug + view count) per play so the
    # UI + MCP see shared-ness without extra round trips. Lazy import —
    # shares.py imports this module.
    if plays:
        from carrac.plays.shares import get_shares_for_plays
        share_map = get_shares_for_plays(user_id, [p['id'] for p in plays])
        for p in plays:
            p['share'] = share_map.get(p['id'])
    return plays


def get_active_plays(user_id=None):
    """Get all non-terminal plays. If user_id=None, returns all users' (for morning engine)."""
    clauses = ["status NOT IN ('completed', 'invalidated')"]
    params = []

    if user_id is not None:
        clauses.append('user_id = ?')
        params.append(user_id)

    where = ' AND '.join(clauses)
    sql = adapt_query(f'SELECT * FROM plays WHERE {where} ORDER BY updated_at DESC')

    with get_conn() as conn:
        _ensure_table(conn)
        cur = conn.execute(sql, params)
        return [_row_to_dict(r) for r in cur.fetchall()]


def update_play(play_id, user_id, **fields):
    """Update play fields. Returns updated play or None if not found/not owned."""
    existing = get_play(play_id, user_id)
    if not existing:
        return None

    allowed = PLAY_UPDATE_FIELDS

    sets = []
    params = []
    for key, val in fields.items():
        if key not in allowed:
            continue
        if key in _JSON_FIELDS:
            val = _json_dumps(val)
        sets.append(f'{key} = ?')
        params.append(val)

    if not sets:
        return existing

    sets.append('updated_at = ?')
    params.append(_now_iso())
    params.extend([play_id, user_id])

    set_clause = ', '.join(sets)
    sql = adapt_query(f'UPDATE plays SET {set_clause} WHERE id = ? AND user_id = ?')

    with get_conn() as conn:
        _ensure_table(conn)
        conn.execute(sql, params)

    return get_play(play_id, user_id)


def transition_status(play_id, user_id, new_status, *, resolution=None):
    """Transition play to a new status. Validates state machine. Returns updated play or None."""
    existing = get_play(play_id, user_id)
    if not existing:
        return None

    current = existing['status']
    if new_status not in TRANSITIONS.get(current, set()):
        logger.warning(f"Invalid transition: {current} -> {new_status} for play {play_id}")
        return None

    now = _now_iso()
    sets = ['status = ?', 'status_changed_at = ?', 'updated_at = ?']
    params = [new_status, now, now]

    # Set resolved_at for terminal states
    if new_status in TERMINAL_STATUSES:
        sets.append('resolved_at = ?')
        params.append(now)
        if resolution:
            sets.append('resolution = ?')
            params.append(resolution)

    # Clear resolved_at on reactivation (expired → watching)
    if current == 'expired' and new_status == 'watching':
        sets.append('resolved_at = ?')
        params.append(None)

    params.extend([play_id, user_id])
    set_clause = ', '.join(sets)
    sql = adapt_query(f'UPDATE plays SET {set_clause} WHERE id = ? AND user_id = ?')

    with get_conn() as conn:
        _ensure_table(conn)
        conn.execute(sql, params)

    return get_play(play_id, user_id)


def play_delete(play_id, user_id, cascade_drawings=False):
    """Hard-delete a play. Returns True if deleted.

    If cascade_drawings=True, also deletes drawings listed in linked_drawing_ids
    that are owned by the same user (ownership-checked inside content_db).
    """
    result = delete_plays_bulk([play_id], user_id, cascade_drawings=cascade_drawings)
    return result['plays_deleted'] == 1


def delete_plays_bulk(play_ids, user_id, cascade_drawings=False):
    """Hard-delete multiple plays.

    Silently skips IDs the user does not own.
    If cascade_drawings=True, also deletes drawings listed in each play's
    linked_drawing_ids that are owned by the same user.

    Returns: dict with `plays_deleted` (int) and `drawings_deleted` (int —
    actual cascade count, 0 when cascade_drawings is False), not
    len(linked_drawing_ids), which would count requested-not-deleted.
    """
    if not play_ids:
        return {'plays_deleted': 0, 'drawings_deleted': 0}

    # Fetch owned plays first (need linked_drawing_ids for cascade, and ownership check)
    with get_conn() as conn:
        _ensure_table(conn)
        placeholders = ','.join(['?' if get_db_type() == 'sqlite' else '%s'] * len(play_ids))
        sql = adapt_query(
            f'SELECT id, linked_drawing_ids FROM plays '
            f'WHERE id IN ({placeholders}) AND user_id = ?'
        )
        rows = conn.execute(sql, list(play_ids) + [user_id]).fetchall()

    owned = [dict(r) for r in rows]
    if not owned:
        return {'plays_deleted': 0, 'drawings_deleted': 0}

    owned_ids = [r['id'] for r in owned]

    # Cascade: delete linked drawings before removing the play rows.
    # F5: use delete_drawings_bulk for one round trip per play AND an
    # accurate count of drawings actually removed (ownership-checked).
    drawings_deleted = 0
    if cascade_drawings:
        try:
            from carrac.content_db import get_content_db
            db = get_content_db()
            if db:
                for row in owned:
                    linked = _json_loads(row.get('linked_drawing_ids')) or []
                    if not linked:
                        continue
                    try:
                        linked_ids = [int(did) for did in linked]
                    except (TypeError, ValueError):
                        logger.warning(
                            'delete_plays_bulk: skipping non-int linked_drawing_ids for play %s: %r',
                            row['id'], linked,
                        )
                        continue
                    try:
                        drawings_deleted += db.delete_drawings_bulk(linked_ids, user_id)
                    except Exception:
                        logger.warning(
                            'delete_plays_bulk: cascade bulk-delete failed for play %s',
                            row['id'],
                        )
        except Exception:
            logger.warning('delete_plays_bulk: content_db unavailable; skipping cascade')

    # Hard-delete the play rows
    with get_conn() as conn:
        _ensure_table(conn)
        ph = ','.join(['?' if get_db_type() == 'sqlite' else '%s'] * len(owned_ids))
        cur = conn.execute(
            adapt_query(f'DELETE FROM plays WHERE id IN ({ph}) AND user_id = ?'),
            owned_ids + [user_id],
        )
        return {'plays_deleted': cur.rowcount, 'drawings_deleted': drawings_deleted}


def count_active_plays(user_id):
    """Count non-terminal plays for tier limit enforcement."""
    with get_conn() as conn:
        _ensure_table(conn)
        cur = conn.execute(
            adapt_query(
                "SELECT COUNT(*) as cnt FROM plays "
                "WHERE user_id = ? AND status NOT IN ('completed', 'invalidated')"
            ),
            (user_id,),
        )
        row = cur.fetchone()
        return row['cnt'] if isinstance(row, dict) else row[0]


def link_drawing(play_id, user_id, drawing_id):
    """Atomically add drawing_id to a play's linked_drawing_ids.

    Uses a single UPDATE to avoid lost-update races when two tabs POST
    simultaneously.  PostgreSQL: native JSONB append with
    containment guard.  SQLite: read-then-CAS retry loop (sufficient for
    local dev — SQLite serialises writers at the file level).

    Verifies the drawing belongs to ``user_id`` BEFORE linking
    it — otherwise a caller could link another user's private drawing into
    their own play, then mint a public share that leaks it (see
    carrac/plays/shares.py _freeze_drawings). Mirrors the equivalent check
    in the MCP twin (carrac/mcp/tools/drawings.py link_unlink_drawing).
    Returns None (same as "play not found") when the drawing isn't owned
    by the caller.
    """
    drawing_id = int(drawing_id)

    from carrac.content_db import get_content_db
    db = get_content_db()
    if not db.get_drawing_by_id(drawing_id, user_id=user_id):
        return None

    if get_db_type() == 'postgresql':
        # Append only when the array does NOT already contain the value.
        sql = """
            UPDATE plays
            SET linked_drawing_ids = CASE
                WHEN NOT (linked_drawing_ids::jsonb @> %s::jsonb)
                THEN linked_drawing_ids::jsonb || %s::jsonb
                ELSE linked_drawing_ids::jsonb
            END,
            updated_at = %s
            WHERE id = %s AND user_id = %s
        """
        new_elem_json = json.dumps([drawing_id])
        with get_conn() as conn:
            _ensure_table(conn)
            cur = conn.execute(sql, (new_elem_json, new_elem_json, _now_iso(), play_id, user_id))
            if cur.rowcount == 0:
                return None
    else:
        # SQLite CAS retry loop (WAL + busy_timeout=30000 make mid-air collisions rare)
        for _attempt in range(5):
            existing = get_play(play_id, user_id)
            if not existing:
                return None
            linked = existing.get('linked_drawing_ids') or []
            if drawing_id not in linked:
                linked = linked + [drawing_id]
                new_json = _json_dumps(linked)
                old_json = _json_dumps(existing.get('linked_drawing_ids') or [])
                now = _now_iso()
                with get_conn() as conn:
                    _ensure_table(conn)
                    cur = conn.execute(
                        'UPDATE plays SET linked_drawing_ids = ?, updated_at = ? '
                        'WHERE id = ? AND user_id = ? AND linked_drawing_ids = ?',
                        (new_json, now, play_id, user_id, old_json),
                    )
                if cur.rowcount > 0:
                    break
                # Concurrent write changed the row — retry
            else:
                break

    return get_play(play_id, user_id)


def unlink_drawing(play_id, user_id, drawing_id):
    """Atomically remove drawing_id from a play's linked_drawing_ids.

    Same dual-path approach as link_drawing.
    """
    drawing_id = int(drawing_id)

    if get_db_type() == 'postgresql':
        # Remove all occurrences via jsonb_agg filter
        sql = """
            UPDATE plays
            SET linked_drawing_ids = COALESCE(
                (SELECT jsonb_agg(elem)
                 FROM jsonb_array_elements(linked_drawing_ids::jsonb) AS elem
                 WHERE elem <> %s::jsonb),
                '[]'::jsonb
            ),
            updated_at = %s
            WHERE id = %s AND user_id = %s
        """
        with get_conn() as conn:
            _ensure_table(conn)
            cur = conn.execute(sql, (json.dumps(drawing_id), _now_iso(), play_id, user_id))
            if cur.rowcount == 0:
                return None
    else:
        # SQLite CAS retry loop
        for _attempt in range(5):
            existing = get_play(play_id, user_id)
            if not existing:
                return None
            linked = existing.get('linked_drawing_ids') or []
            if drawing_id in linked:
                linked = [x for x in linked if x != drawing_id]
                new_json = _json_dumps(linked)
                old_json = _json_dumps(existing.get('linked_drawing_ids') or [])
                now = _now_iso()
                with get_conn() as conn:
                    _ensure_table(conn)
                    cur = conn.execute(
                        'UPDATE plays SET linked_drawing_ids = ?, updated_at = ? '
                        'WHERE id = ? AND user_id = ? AND linked_drawing_ids = ?',
                        (new_json, now, play_id, user_id, old_json),
                    )
                if cur.rowcount > 0:
                    break
                # Concurrent write changed the row — retry
            else:
                break

    return get_play(play_id, user_id)


def play_get_digest(user_id):
    """Get structured play data for morning engine / AI context block."""
    plays = get_active_plays(user_id)
    if not plays:
        return {'plays': [], 'summary': 'No active plays.'}

    summary_parts = []
    by_status = {}
    for p in plays:
        st = p['status']
        by_status.setdefault(st, []).append(p)

    for st in ('acting', 'watching', 'expired'):
        count = len(by_status.get(st, []))
        if count:
            summary_parts.append(f"{count} {st}")

    context_plays = []
    for p in plays:
        conditions = p.get('conditions') or {'rules': [], 'logic': 'all'}
        rules = conditions.get('rules', [])
        met_count = sum(1 for r in rules if r.get('met'))
        total = len(rules)

        # Build per-condition detail for AI context
        condition_details = []
        for r in rules:
            detail = {
                'type': r.get('type', '?'),
                'met': r.get('met', False),
                'evidence': r.get('evidence', ''),
            }
            # Build human-readable label from params
            params = r.get('params', r)
            label = r.get('label', '')
            if not label:
                t = r.get('type', '')
                if t == 'price_level':
                    label = f"Price {params.get('relation', '?')} ${params.get('level', '?')}"
                elif t == 'indicator_state':
                    label = f"{params.get('indicator', '?')} {params.get('state', '?')} ({params.get('timeframe', '?')})"
                elif t == 'indicator_value':
                    label = f"{params.get('indicator', '?')} ({params.get('timeframe', '?')}) {params.get('operator', '?')} {params.get('value', '?')}"
                elif t == 'consensio_threshold':
                    label = f"Consensio >= {params.get('min_value', '?')}"
                elif t == 'scanner_verdict':
                    label = f"Scanner {params.get('direction', '?')} >= {params.get('min_verdict', '?')}"
                elif t == 'cross_asset':
                    label = f"Cross-asset: {params.get('asset', '?')}"
                elif t == 'manual':
                    label = params.get('label', 'Manual check')
                elif t == 'time_limit':
                    label = f"Deadline: {params.get('expires_at', '?')}"
                elif t == 'price_change_pct':
                    label = f"Price {params.get('direction', '?')} {params.get('threshold_pct', '?')}% in {params.get('period', '?')}"
                elif t == 'engulfing_detected':
                    label = f"Engulfing {params.get('direction', '?')} ({params.get('timeframe', '?')})"
                elif t == 'bb_breakout_retest_engulfing':
                    label = f"BB breakout-retest ({params.get('direction', '?')} {params.get('timeframe', '?')}, lookback {params.get('lookback_bars', '?')}b)"
                elif t == 'bb_fade_engulfing':
                    label = f"BB fade ({params.get('direction', '?')} {params.get('timeframe', '?')})"
                elif t == 'bb_squeeze_breakout':
                    label = f"BB squeeze breakout ({params.get('direction', '?')} {params.get('timeframe', '?')})"
                else:
                    label = t.replace('_', ' ')
            detail['label'] = label
            condition_details.append(detail)

        context_plays.append({
            'id': p['id'],
            'title': p['title'],
            'thesis': p['thesis'],
            'type': p['type'],
            'approach': p['approach'],
            'direction': p['direction'],
            'primary_asset': p['primary_asset'],
            'related_assets': p.get('related_assets') or [],
            'trigger_asset': p.get('trigger_asset'),
            'status': p['status'],
            'conditions_met': f"{met_count}/{total}",
            'conditions_detail': condition_details,
            'conditions_logic': conditions.get('logic', 'all'),
            'invalidation': p.get('invalidation'),
            'action_plan': p.get('action_plan'),
            'expiry': p.get('expiry'),
            'timeframe': p.get('timeframe'),
        })

    return {
        'plays': context_plays,
        'summary': ', '.join(summary_parts) if summary_parts else 'No active plays.',
    }


def get_active_plays_for_monitoring():
    """Get all non-terminal plays across all users (for background monitor)."""
    sql = adapt_query(
        "SELECT * FROM plays WHERE status IN ('watching', 'acting', 'expired') "
        "ORDER BY updated_at DESC"
    )
    with get_conn() as conn:
        _ensure_table(conn)
        cur = conn.execute(sql)
        return [_row_to_dict(r) for r in cur.fetchall()]


def persist_condition_state(play_id, user_id, conditions, invalidation=None):
    """Update conditions and invalidation with evaluated met/met_at/evidence."""
    now = _now_iso()
    sets = ['conditions = ?', 'updated_at = ?']
    params = [_json_dumps(conditions), now]

    if invalidation is not None:
        sets.append('invalidation = ?')
        params.append(_json_dumps(invalidation))

    params.extend([play_id, user_id])
    set_clause = ', '.join(sets)
    sql = adapt_query(f'UPDATE plays SET {set_clause} WHERE id = ? AND user_id = ?')

    with get_conn() as conn:
        _ensure_table(conn)
        conn.execute(sql, params)
