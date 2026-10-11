"""Session kill-epoch — shared by BOTH front doors.

The kill-epoch exists because deleting a session blob is not enough,
because an in-flight request holding the session in memory re-saves it on its
way out and resurrects the "deleted" session. So validity is made independent
of blob existence — every session carries ``_created_at``, and any session
created at or before the user's kill timestamp is rejected per request.

That guard lived inside ``carrac/auth/routes.py`` as a Flask
``before_app_request`` hook, so it only ever ran on the Flask side. The
FastAPI stack reads the SAME ``mcd:session:<sid>`` blobs and never looked at
the epoch, which meant deactivating a user locked them out of the pages but
left the whole ``/api/*`` mutating surface open to them.

This module holds the mechanism with no framework imports, so both stacks can
share one implementation rather than two that drift.
"""

import logging
import os
import time

logger = logging.getLogger('carrac.auth.session_kill')

# Process-local fallback for when Redis is absent (tests, single-process dev).
# NB: carrac/auth/routes.py imports this exact object, and tests/conftest.py
# clears it between tests — it must stay one shared dict, not a copy.
_KILL_TS_FALLBACK: dict = {}

_KILL_TS_TTL = 31 * 86400      # outlives the 30-day absolute session cap
_kill_redis_client = None


def _kill_redis():
    """Cached Redis client for the kill-epoch (None when unavailable)."""
    global _kill_redis_client
    if _kill_redis_client is None:
        try:
            import redis as redis_mod
            url = os.getenv('REDIS_SESSION_URL') or os.getenv('REDIS_URL', '')
            if not url:
                return None
            _kill_redis_client = redis_mod.from_url(
                url, socket_connect_timeout=1, socket_timeout=1,
                decode_responses=True)
        except Exception:
            return None
    return _kill_redis_client


def kill_ts_key(user_id) -> str:
    return f'mcd:user:{user_id}:killed_at'


def mark_sessions_killed(user_id):
    """Record the kill-epoch for a user."""
    ts = time.time()
    _KILL_TS_FALLBACK[str(user_id)] = ts
    try:
        r = _kill_redis()
        if r is not None:
            r.setex(kill_ts_key(user_id), _KILL_TS_TTL, str(ts))
    except Exception as exc:
        # Only this process's fallback holds the epoch now; other workers
        # will keep accepting the user's old sessions.
        logger.error("Session kill: failed to store kill epoch for user %s: %s", user_id, exc)


def sessions_killed_at(user_id) -> float:
    """The user's kill epoch, or 0.0 if they have never been killed."""
    ts = _KILL_TS_FALLBACK.get(str(user_id), 0.0)
    try:
        r = _kill_redis()
        if r is not None:
            v = r.get(kill_ts_key(user_id))
            if v:
                ts = max(ts, float(v))
    except Exception:
        pass
    return ts


def session_is_killed(session_data) -> bool:
    """True when this session predates its user's kill epoch.

    Fails CLOSED on a session that carries a user_id but no ``_created_at``:
    such a blob cannot prove it was issued after the kill, and the only
    sessions without the field are pre-kill-epoch relics — exactly the
    long-lived ones a kill is meant to catch.
    """
    if not session_data:
        return False
    user_id = session_data.get('user_id')
    if not user_id:
        return False
    killed_at = sessions_killed_at(user_id)
    if not killed_at:
        return False
    return float(session_data.get('_created_at') or 0) <= killed_at
