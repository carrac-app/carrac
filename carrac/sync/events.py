"""
Sync event tracking for drawings and preferences.

On every drawing CRUD or preference save, publish_*_event() writes a timestamp
to Redis so polling clients can detect changes.

Keys:
  mcd:sync:lastchange:{user_id}       → drawing change timestamp
  mcd:sync:lastchange_prefs:{user_id} → preference change timestamp
"""

import json
import logging
import os
import time

logger = logging.getLogger('carrac.sync')

LASTCHANGE_PREFIX = 'mcd:sync:lastchange:'
PREF_LASTCHANGE_PREFIX = 'mcd:sync:lastchange_prefs:'

_pub_redis = None


def _get_redis():
    """Get Redis client. Returns None if unavailable. Retries on each call if not connected."""
    global _pub_redis
    if _pub_redis is not None:
        try:
            _pub_redis.ping()
            return _pub_redis
        except Exception:
            _pub_redis = None
    try:
        import redis
        url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
        _pub_redis = redis.from_url(url, socket_connect_timeout=2, decode_responses=True)
        _pub_redis.ping()
        return _pub_redis
    except Exception as e:
        logger.warning("Redis unavailable for sync events: %s", e)
        _pub_redis = None
        return None


def publish_drawing_event(user_id, assets, session_id):
    """
    Record a drawing change for the user.

    Sets a timestamp in Redis that polling clients compare against
    to detect remote changes.
    """
    r = _get_redis()
    if not r:
        return

    key = f'{LASTCHANGE_PREFIX}{user_id}'
    now = time.time()
    payload = json.dumps({
        'ts': now,
        'assets': assets,
        'session_id': session_id or '',
    })

    try:
        pipe = r.pipeline()
        # Store the timestamp for poll comparison
        pipe.set(key, now, ex=86400)  # Expire after 24h
        # Store the full event details for the poll response
        pipe.set(f'{key}:detail', payload, ex=86400)
        pipe.execute()
    except Exception as e:
        logger.warning("Failed to record sync event: %s", e)


def get_last_change(user_id):
    """Get the timestamp and details of the last drawing change for a user."""
    r = _get_redis()
    if not r:
        return 0, None

    try:
        key = f'{LASTCHANGE_PREFIX}{user_id}'
        pipe = r.pipeline()
        pipe.get(key)
        pipe.get(f'{key}:detail')
        ts_str, detail_str = pipe.execute()

        ts = float(ts_str) if ts_str else 0
        detail = None
        if detail_str:
            try:
                detail = json.loads(detail_str)
            except (json.JSONDecodeError, TypeError):
                pass
        return ts, detail
    except Exception:
        return 0, None


def publish_preference_event(user_id, session_id):
    """
    Record a preference change for the user.

    Sets a timestamp in Redis that polling clients compare against
    to detect remote preference changes.
    """
    r = _get_redis()
    if not r:
        return

    key = f'{PREF_LASTCHANGE_PREFIX}{user_id}'
    now = time.time()
    payload = json.dumps({
        'ts': now,
        'session_id': session_id or '',
    })

    try:
        pipe = r.pipeline()
        pipe.set(key, now, ex=86400)
        pipe.set(f'{key}:detail', payload, ex=86400)
        pipe.execute()
    except Exception as e:
        logger.warning("Failed to record preference sync event: %s", e)


def get_last_pref_change(user_id):
    """Get the timestamp and details of the last preference change for a user."""
    r = _get_redis()
    if not r:
        return 0, None

    try:
        key = f'{PREF_LASTCHANGE_PREFIX}{user_id}'
        pipe = r.pipeline()
        pipe.get(key)
        pipe.get(f'{key}:detail')
        ts_str, detail_str = pipe.execute()

        ts = float(ts_str) if ts_str else 0
        detail = None
        if detail_str:
            try:
                detail = json.loads(detail_str)
            except (json.JSONDecodeError, TypeError):
                pass
        return ts, detail
    except Exception:
        return 0, None
