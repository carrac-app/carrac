"""User alerts, one row per alert in the app database.

Every alert kind lives in the ``user_alerts`` table: price and trendline
alerts, indicator and formula alerts, and the quant kinds (z-score, regime,
chain strength, anomaly cluster, conditional, derivatives). The fields every
evaluator filters on are columns: owner, kind, symbol, the active and
triggered flags, and the creation time that orders a user's list. Everything
else an alert carries differs by kind and is kept verbatim in ``data``.

An alert reaches callers as the dict the rest of the code has always used:
``user_id`` is a string, a price alert has no ``alert_type`` key, and a quant
alert has no ``symbol``. Keys starting with ``_`` are per-run scratch space
(``_pending_notification``) and are never stored.

Writes are row-level. ``modify`` locks the one row it changes, so a change
from the API and a change from an evaluator can land on the same alert
without either undoing the other, and ``save_changes`` writes back only the
fields an evaluator actually changed on the alerts it touched. Nothing here
catches a failed write: it raises to the caller.

Alerts used to live in ``DATA_DIR/price_alerts.json``. The first use of this
module in a process imports that file once, if the table is still empty, and
renames it to ``price_alerts.json.imported``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from carrac.db_pool import adapt_query, get_db_type

logger = logging.getLogger('carrac.alerts.store')

TABLE = 'user_alerts'
LEGACY_FILENAME = 'price_alerts.json'

# Keys stored as columns; every other persistable key goes into `data`.
_COLUMN_KEYS = frozenset({'id', 'user_id', 'alert_type', 'symbol', 'active',
                          'triggered', 'created_at'})
_SELECT = f'SELECT id, user_id, alert_type, symbol, active, triggered, data, created_at FROM {TABLE}'
_MISSING = object()

_import_lock = threading.Lock()
_import_checked = False


def new_alert_id() -> int:
    """A random 32-bit id. Alert ids appear in URLs, so they are not sequential."""
    return int(secrets.token_hex(4), 16)


# ── row <-> alert ───────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _to_alert(row) -> dict:
    row = dict(row)
    data = row['data']
    if isinstance(data, str):
        data = json.loads(data)
    alert = dict(data or {})
    alert['id'] = row['id']
    alert['user_id'] = str(row['user_id'])
    if row['alert_type'] != 'price':
        alert['alert_type'] = row['alert_type']
    if row['symbol'] is not None:
        alert['symbol'] = row['symbol']
    alert['active'] = bool(row['active'])
    alert['triggered'] = bool(row['triggered'])
    alert['created_at'] = _iso(row['created_at'])
    return alert


def _persistable(alert: dict) -> dict:
    return copy.deepcopy({k: v for k, v in alert.items() if not str(k).startswith('_')})


def _data_json(alert: dict) -> str:
    return json.dumps({k: v for k, v in _persistable(alert).items() if k not in _COLUMN_KEYS})


def _column_values(alert: dict) -> tuple:
    return (alert.get('alert_type') or 'price', alert.get('symbol'),
            bool(alert.get('active', True)), bool(alert.get('triggered', False)),
            _data_json(alert))


def _parse_created_at(value) -> datetime:
    """A legacy ``created_at`` string as an aware datetime (naive = local time)."""
    if not value:
        return _now()
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return _now()
    return parsed.astimezone(timezone.utc)


# ── transactions ────────────────────────────────────────────────────────────

def _db():
    from user_db import user_db
    return user_db


def _lock_for_write(conn) -> None:
    """Take SQLite's write lock up front, so a read-then-write cannot interleave
    with another writer. Postgres locks rows with SELECT ... FOR UPDATE instead."""
    if get_db_type() != 'postgresql' and not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')


def _for_update() -> str:
    return ' FOR UPDATE' if get_db_type() == 'postgresql' else ''


def _ready() -> None:
    """Run the one-shot legacy import the first time this process uses the store."""
    global _import_checked
    if _import_checked:
        return
    with _import_lock:
        if not _import_checked:
            import_legacy_json()
            _import_checked = True


# ── reads ───────────────────────────────────────────────────────────────────

def list_alerts(owner: Optional[int] = None, *, active_only: bool = False,
                alert_types: Optional[Iterable[str]] = None) -> List[dict]:
    """Alerts in creation order, optionally narrowed by owner, state and kind."""
    _ready()
    where, params = [], []
    if owner is not None:
        where.append('user_id = ?')
        params.append(int(owner))
    if active_only:
        where.append('active = ?')
        params.append(True)
    if alert_types is not None:
        kinds = list(alert_types)
        where.append(f"alert_type IN ({', '.join('?' * len(kinds))})")
        params.extend(kinds)
    sql = _SELECT + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY created_at, id'
    with _db().transaction() as conn:
        cur = conn.cursor()
        cur.execute(adapt_query(sql), tuple(params))
        return [_to_alert(r) for r in cur.fetchall()]


# ── writes ──────────────────────────────────────────────────────────────────

def insert(alert: dict) -> dict:
    """Store a new alert and return it as stored. ``user_id`` must be numeric."""
    _ready()
    owner = int(alert['user_id'])
    created = _now()
    with _db().transaction() as conn:
        conn.cursor().execute(adapt_query(
            f'INSERT INTO {TABLE} (id, user_id, alert_type, symbol, active, triggered, data, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)'),
            (int(alert['id']), owner, *_column_values(alert), created))
    stored = _persistable(alert)
    stored['user_id'] = str(owner)
    stored['created_at'] = created.isoformat()
    return stored


def modify(alert_id: int, mutate: Callable[[dict], None],
           owner: Optional[int] = None) -> Optional[dict]:
    """Apply ``mutate`` to one alert under a row lock and store the result.

    Returns the updated alert, or None when the alert does not exist (or is not
    ``owner``'s). ``mutate`` edits the dict in place; changes to ``id``,
    ``user_id`` and ``created_at`` are ignored.
    """
    _ready()
    where, params = 'id = ?', [int(alert_id)]
    if owner is not None:
        where += ' AND user_id = ?'
        params.append(int(owner))
    with _db().transaction() as conn:
        _lock_for_write(conn)
        cur = conn.cursor()
        cur.execute(adapt_query(f'{_SELECT} WHERE {where}{_for_update()}'), tuple(params))
        row = cur.fetchone()
        if row is None:
            return None
        alert = _to_alert(row)
        fixed = {k: alert[k] for k in ('id', 'user_id', 'created_at')}
        mutate(alert)
        alert.update(fixed)
        cur.execute(adapt_query(
            f'UPDATE {TABLE} SET alert_type = ?, symbol = ?, active = ?, triggered = ?, data = ? '
            'WHERE id = ?'), (*_column_values(alert), int(alert_id)))
    return _persistable(alert)


def delete(alert_id: int, owner: int) -> bool:
    _ready()
    with _db().transaction() as conn:
        cur = conn.cursor()
        cur.execute(adapt_query(f'DELETE FROM {TABLE} WHERE id = ? AND user_id = ?'),
                    (int(alert_id), int(owner)))
        return cur.rowcount > 0


def delete_many(alert_ids: Iterable[int], owner: int) -> List[int]:
    """Delete those of ``alert_ids`` that ``owner`` owns; return the ids deleted."""
    _ready()
    ids = sorted({int(i) for i in alert_ids})
    if not ids:
        return []
    marks = ', '.join('?' * len(ids))
    with _db().transaction() as conn:
        _lock_for_write(conn)
        cur = conn.cursor()
        cur.execute(adapt_query(
            f'SELECT id FROM {TABLE} WHERE user_id = ? AND id IN ({marks}){_for_update()}'),
            (int(owner), *ids))
        owned = [dict(r)['id'] for r in cur.fetchall()]
        if owned:
            cur.execute(adapt_query(
                f"DELETE FROM {TABLE} WHERE id IN ({', '.join('?' * len(owned))})"), tuple(owned))
    return owned


def snapshot(alerts: Iterable[dict]) -> dict:
    """The stored state of ``alerts`` as read, for ``save_changes`` to diff against."""
    return {a['id']: _persistable(a) for a in alerts}


def save_changes(alerts: Iterable[dict], baseline: dict) -> int:
    """Write back the fields that changed on each alert since ``baseline``.

    Only alerts that changed are written, and only the keys that changed, each
    applied to the row as it is now. A field the user edited meanwhile is kept,
    and an alert deleted meanwhile stays deleted. ``baseline`` is updated to
    the new state, so a second call writes only what changed after the first.
    Returns the number of alerts written.
    """
    written = 0
    for alert in alerts:
        before = baseline.get(alert['id'])
        after = _persistable(alert)
        if before is None or after == before:
            continue
        changed = {k: v for k, v in after.items() if before.get(k, _MISSING) != v}
        removed = [k for k in before if k not in after]

        def _apply(row, changed=changed, removed=removed):
            row.update(changed)
            for key in removed:
                row.pop(key, None)

        if modify(alert['id'], _apply) is not None:
            written += 1
        baseline[alert['id']] = after
    return written


# ── one-shot import of the legacy JSON file ─────────────────────────────────

def _legacy_path() -> Path:
    return Path(os.environ.get('DATA_DIR', 'data')) / LEGACY_FILENAME


def _legacy_owner(user_id) -> Optional[int]:
    from push_notifications import _resolve_user_id
    return _resolve_user_id(str(user_id)) if user_id is not None else None


def _legacy_id(raw) -> int:
    """Ids were ints, except indicator and formula alerts created through the
    API, which got 8 hex characters. Those keep their value as a hex number."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    try:
        return int(str(raw), 16)
    except ValueError:
        return new_alert_id()


def import_legacy_json(path: Optional[Path] = None) -> Optional[int]:
    """Import ``price_alerts.json`` into an empty table; return the count imported.

    Returns None when there is nothing to do: no file, or the table already
    holds alerts (then the file is left in place and a warning says so). Runs
    in one transaction that holds the table's write lock, so concurrent
    processes import it once. Rows whose id already exists are skipped.
    """
    path = Path(path) if path is not None else _legacy_path()
    if not path.exists():
        return None
    legacy = json.loads(path.read_text())
    rows, unowned = [], []
    for alert in legacy:
        owner = _legacy_owner(alert.get('user_id'))
        if owner is None:
            unowned.append(alert.get('id'))
            continue
        alert = dict(alert, id=_legacy_id(alert.get('id')), user_id=str(owner))
        rows.append((alert['id'], owner, *_column_values(alert),
                     _parse_created_at(alert.get('created_at'))))

    with _db().transaction() as conn:
        cur = conn.cursor()
        if get_db_type() == 'postgresql':
            cur.execute(f'LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE')
        else:
            _lock_for_write(conn)
        cur.execute(f'SELECT COUNT(*) AS n FROM {TABLE}')
        if dict(cur.fetchone())['n']:
            logger.warning('%s exists but %s already holds alerts; it was not imported',
                           path, TABLE)
            return None
        cur.executemany(adapt_query(
            f'INSERT INTO {TABLE} (id, user_id, alert_type, symbol, active, triggered, data, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING'), rows)
        cur.execute(f'SELECT COUNT(*) AS n FROM {TABLE}')
        imported = dict(cur.fetchone())['n']

    path.rename(path.with_name(path.name + '.imported'))
    logger.info('Imported %d alerts from %s into %s', imported, path, TABLE)
    if unowned:
        logger.warning('Skipped %d legacy alerts whose user_id matches no user: ids %s',
                       len(unowned), unowned)
    return imported
