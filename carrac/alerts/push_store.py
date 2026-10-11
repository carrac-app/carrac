"""Web-push subscriptions, one row per user in the app database.

A user has at most one subscription: the one their browser registered last
replaces any earlier one. The row holds what ``pywebpush`` needs to deliver,
the endpoint URL and the two encryption keys, and nothing else.

A subscription is user input that tells the server where to send an HTTP
POST, so ``clean`` checks it like any other outbound URL a user supplies:
HTTPS only, and through ``security.validate_outbound_url``, which refuses IP
literals, internal names and hostnames that resolve to private or metadata
addresses. ``upsert`` stores only what ``clean`` accepts, and the send path
runs ``clean`` again before every delivery, so a row written before these
checks existed is never posted to either.

Writes are row-level: subscribing or unsubscribing touches only that user's
row, and sending touches none. Nothing here catches a failed write: it raises
to the caller.

Subscriptions used to be one JSON map in Redis (``mcd:push:subscriptions``),
mirrored to ``DATA_DIR/push_subscriptions.json``, that every process rewrote
whole. The first use of this module in a process imports that map once, if the
table is still empty, then deletes the Redis key and renames the file to
``push_subscriptions.json.imported``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from carrac.db_pool import adapt_query, get_db_type
from security import validate_outbound_url

logger = logging.getLogger('carrac.alerts.push_store')

TABLE = 'push_subscriptions'
LEGACY_REDIS_KEY = 'mcd:push:subscriptions'
LEGACY_FILENAME = 'push_subscriptions.json'

# The keys are base64url strings: a 65-byte P-256 point and a 16-byte secret.
# The cap is generous; it only stops a client from storing a blob.
_MAX_KEY_LEN = 256

_import_lock = threading.Lock()
_import_checked = False


class InvalidSubscription(ValueError):
    """A push subscription that may not be stored or sent to."""


def clean(subscription) -> dict:
    """The deliverable part of a browser push subscription, or InvalidSubscription.

    Returns ``{'endpoint': ..., 'keys': {'p256dh': ..., 'auth': ...}}``.
    """
    if not isinstance(subscription, dict):
        raise InvalidSubscription('Invalid push subscription: expected an object')
    endpoint = subscription.get('endpoint')
    if not isinstance(endpoint, str) or not endpoint.startswith('https://'):
        raise InvalidSubscription('Invalid push subscription: endpoint must be an https:// URL')
    ok, reason = validate_outbound_url(endpoint, allowed_schemes=('https',))
    if not ok:
        raise InvalidSubscription(f'Invalid push subscription: endpoint rejected: {reason}')
    keys = subscription.get('keys')
    if not isinstance(keys, dict):
        raise InvalidSubscription('Invalid push subscription: keys.p256dh and keys.auth are required')
    cleaned = {}
    for name in ('p256dh', 'auth'):
        value = keys.get(name)
        if not isinstance(value, str) or not value or len(value) > _MAX_KEY_LEN:
            raise InvalidSubscription(
                f'Invalid push subscription: keys.{name} must be a non-empty string')
        cleaned[name] = value
    return {'endpoint': endpoint, 'keys': cleaned}


# ── helpers ─────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from user_db import user_db
    return user_db


def _redis():
    from carrac.redis_cache import get_redis
    return get_redis()


def _ready() -> None:
    """Run the one-shot legacy import the first time this process uses the store."""
    global _import_checked
    if _import_checked:
        return
    with _import_lock:
        if not _import_checked:
            import_legacy()
            _import_checked = True


def _upsert_sql() -> str:
    return adapt_query(
        f'INSERT INTO {TABLE} (user_id, subscription, created_at) VALUES (?, ?, ?) '
        'ON CONFLICT (user_id) DO UPDATE SET '
        'subscription = excluded.subscription, created_at = excluded.created_at')


# ── reads and writes ────────────────────────────────────────────────────────

def get(user_id: int) -> Optional[dict]:
    """The user's stored subscription, as stored (not re-validated), or None."""
    _ready()
    with _db().transaction() as conn:
        cur = conn.cursor()
        cur.execute(adapt_query(f'SELECT subscription FROM {TABLE} WHERE user_id = ?'),
                    (int(user_id),))
        row = cur.fetchone()
    if row is None:
        return None
    value = dict(row)['subscription']
    return json.loads(value) if isinstance(value, str) else value


def upsert(user_id: int, subscription) -> dict:
    """Validate ``subscription`` and make it the user's one subscription.

    Raises InvalidSubscription when ``clean`` refuses it. Returns what was stored.
    """
    cleaned = clean(subscription)
    _ready()
    with _db().transaction() as conn:
        conn.cursor().execute(_upsert_sql(), (int(user_id), json.dumps(cleaned), _now()))
    return cleaned


def delete(user_id: int) -> bool:
    """Remove the user's subscription; False when there was none."""
    _ready()
    with _db().transaction() as conn:
        cur = conn.cursor()
        cur.execute(adapt_query(f'DELETE FROM {TABLE} WHERE user_id = ?'), (int(user_id),))
        return cur.rowcount > 0


# ── one-shot import of the legacy Redis map / JSON file ─────────────────────

def _legacy_path() -> Path:
    return Path(os.environ.get('DATA_DIR', 'data')) / LEGACY_FILENAME


def _legacy_owner(key) -> Optional[int]:
    from push_notifications import _resolve_user_id
    return _resolve_user_id(str(key))


def _parse_created_at(value) -> datetime:
    """A legacy ``created_at`` string as an aware datetime (naive = local time)."""
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        return _now()


def _read_legacy(redis, path: Path):
    """The legacy map and where it came from. Redis was the primary copy; the
    file was its mirror, read only when Redis held nothing."""
    if redis is not None:
        raw = redis.get(LEGACY_REDIS_KEY)
        if raw:
            return json.loads(raw), 'redis'
    if path.exists():
        return json.loads(path.read_text()), str(path)
    return None, None


def import_legacy(path: Optional[Path] = None) -> Optional[int]:
    """Import the legacy subscription map into an empty table; return the count.

    Returns None when there is nothing to do: no legacy map, or the table
    already holds subscriptions (then the sources are left alone and a warning
    says so). Runs in one transaction that holds the table's write lock, so
    concurrent processes import it once. Entries are imported as stored; the
    send path validates each one before it is used.
    """
    path = Path(path) if path is not None else _legacy_path()
    redis = _redis()
    legacy, source = _read_legacy(redis, path)
    if legacy is None:
        return None

    rows, unowned = [], []
    for key, entry in legacy.items():
        owner = _legacy_owner(key)
        subscription = (entry or {}).get('subscription')
        if owner is None or not subscription:
            unowned.append(key)
            continue
        rows.append((owner, json.dumps(subscription), _parse_created_at(entry.get('created_at'))))

    with _db().transaction() as conn:
        cur = conn.cursor()
        if get_db_type() == 'postgresql':
            cur.execute(f'LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE')
        elif not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        cur.execute(f'SELECT COUNT(*) AS n FROM {TABLE}')
        if dict(cur.fetchone())['n']:
            logger.warning('A legacy push subscription map exists in %s but %s already holds '
                           'subscriptions; it was not imported', source, TABLE)
            return None
        cur.executemany(adapt_query(
            f'INSERT INTO {TABLE} (user_id, subscription, created_at) VALUES (?, ?, ?) '
            'ON CONFLICT (user_id) DO NOTHING'), rows)
        cur.execute(f'SELECT COUNT(*) AS n FROM {TABLE}')
        imported = dict(cur.fetchone())['n']

    if redis is not None:
        redis.delete(LEGACY_REDIS_KEY)
    if path.exists():
        path.rename(path.with_name(path.name + '.imported'))
    logger.info('Imported %d push subscriptions from %s into %s', imported, source, TABLE)
    if unowned:
        logger.warning('Skipped %d legacy push subscriptions whose user matches no user: %s',
                       len(unowned), unowned)
    return imported
