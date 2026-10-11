"""
Per-user API usage counters.

Two layers, both Redis-backed:
  1. Sliding-window live budgets — managed by carrac.rate_limit (REST) and
     carrac.mcp.rate_limit (MCP), surfaced via peek_rate_limit(). Short-term
     (minute/hour/day).
  2. Daily rollups in Redis — one hash per (date, user_id), incremented in
     an after_request hook. 35-day TTL, one key per day.

Exposed via GET /api/usage.

There is no Postgres rollup (the unused api_usage_daily table is dropped by
alembic/versions/20260826_drop_api_usage_daily.py). Redis is the only source
of per-user call counts.
"""

from datetime import date, timedelta
import logging

from flask import g, request

logger = logging.getLogger('carrac.usage')

USAGE_KEY_PREFIX = 'mcd:usage:'   # mcd:usage:YYYY-MM-DD:u:<user_id>
USAGE_TTL_DAYS = 35


def _usage_key(day, user_id):
    return f'{USAGE_KEY_PREFIX}{day.isoformat()}:u:{user_id}'


def record_usage(user_id, surface='rest'):
    """Increment today's counter for (user_id, surface). Safe no-op if Redis is down."""
    if not user_id:
        return
    try:
        from carrac.redis_cache import get_redis
        client = get_redis()
        if not client:
            return
        key = _usage_key(date.today(), user_id)
        pipe = client.pipeline()
        pipe.hincrby(key, surface, 1)
        pipe.hincrby(key, 'total', 1)
        pipe.expire(key, USAGE_TTL_DAYS * 86400)
        pipe.execute()
    except Exception as exc:
        logger.debug('usage counter increment failed: %s', exc)


def read_history(user_id, days=30):
    """Return list of {date, rest, mcp, total} for the last N days."""
    if not user_id:
        return []
    try:
        from carrac.redis_cache import get_redis
        client = get_redis()
        if not client:
            return []
        out = []
        today = date.today()
        for i in range(days):
            d = today - timedelta(days=i)
            key = _usage_key(d, user_id)
            data = client.hgetall(key) or {}
            # Redis returns bytes or strs depending on decode_responses.
            def _coerce(v):
                if v is None:
                    return 0
                if isinstance(v, bytes):
                    v = v.decode('utf-8', 'ignore')
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
            def _key(k):
                return k.decode('utf-8', 'ignore') if isinstance(k, bytes) else k
            row = {'date': d.isoformat(), 'rest': 0, 'mcp': 0, 'total': 0}
            for k, v in data.items():
                row[_key(k)] = _coerce(v)
            out.append(row)
        return out
    except Exception as exc:
        logger.debug('usage history read failed: %s', exc)
        return []


# ── After-request hook ─────────────────────────────────────────

def track_api_request(response):
    """Record this request against the user's daily counter.

    Registered in carrac/__init__.py as an after_request hook. Only
    counts /api/* and /mcp requests authenticated as a user — skips docs,
    spec, anonymous, and non-API paths.
    """
    try:
        path = request.path
        if not (path.startswith('/api/') or path.startswith('/mcp')):
            return response
        if path in ('/api/docs', '/api/openapi.json'):
            return response
        if getattr(g, 'api_key_internal', False):
            return response  # Never counted for usage analytics.
        uid = getattr(g, 'api_key_user_id', None)
        if uid is None:
            from flask import session
            uid = session.get('user_id')
        if uid is None:
            return response
        surface = 'mcp' if path.startswith('/mcp') else 'rest'
        record_usage(uid, surface)
    except Exception:
        # Never break a request because of usage accounting.
        pass
    return response


# ── /api/usage endpoint helper ─────────────────────────────────

def build_usage_response(user_id, tier, include_history=True, history_days=30):
    """Assemble the full /api/usage payload for a given user."""
    from carrac.rest_rate_limit import peek_rest_rate_limit
    from carrac.mcp.rate_limit import peek_rate_limit as peek_mcp

    key_id = f'u:{user_id}'

    payload = {
        'user_id': user_id,
        'tier': tier,
        'rest': peek_rest_rate_limit(key_id, tier),
        'mcp': peek_mcp(key_id, tier),
    }
    if include_history:
        payload['history'] = read_history(user_id, days=history_days)
    return payload
