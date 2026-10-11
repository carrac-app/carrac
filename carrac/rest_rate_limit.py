"""
REST /api/* per-key sliding-window rate limiter.

Wraps the shared engine in carrac.rate_limit with REST-specific budgets
and a dedicated Redis key prefix so MCP and REST counters stay isolated.

Budgets (shared with MCP — both surfaces hit the same 2-core box):
  free:    20/min,    300/hour,   2,000/day
  member:  60/min,  1,500/hour,  15,000/day
  premium: 120/min, 5,000/hour,  50,000/day
  tester:  mirrors premium
  admin:   unlimited

Free is now open (invite-only beta); tier differentiation is per-tool
(min_tier) + these per-tier budgets, not an all-or-nothing API gate.

Wiring:
  carrac/__init__.py registers `enforce_rest_rate_limit` as a
  before_request hook that fires AFTER rate_pools.classify_request.
  Only the 'api_key' pool is rate-limited here — session and widget pools
  have their own limits via flask-limiter.
"""

import logging

from flask import g, jsonify, request

from carrac.rate_limit import (
    RateLimitExceeded,
    check_rate_limit as _shared_check,
    peek_rate_limit as _shared_peek,
)

logger = logging.getLogger('carrac.rest_rate_limit')


LIMITS_REST = {
    'free':    [(20,  60,    'minute'), (300,   3600,  'hour'), (2000,  86400, 'day')],
    'member':  [(60,  60,    'minute'), (1500,  3600,  'hour'), (15000, 86400, 'day')],
    'premium': [(120, 60,    'minute'), (5000,  3600,  'hour'), (50000, 86400, 'day')],
    'tester':  [(120, 60,    'minute'), (5000,  3600,  'hour'), (50000, 86400, 'day')],
    'admin':   [],
}

KEY_PREFIX = 'mcd:rest:rl:'


def check_rest_rate_limit(key_id, tier):
    """Record a REST call and enforce the sliding-window budget."""
    return _shared_check(key_id, tier, LIMITS_REST, KEY_PREFIX)


def peek_rest_rate_limit(key_id, tier):
    """Return current REST usage without incrementing counters."""
    return _shared_peek(key_id, tier, LIMITS_REST, KEY_PREFIX)


def _resolve_key_id():
    """Derive a stable rate-limit key for the current request.

    Prefers g.api_key_user_id (per-user cvx_k_ keys). Falls back to the
    shared admin key fingerprint already set by rate_pools.classify_request.
    """
    uid = getattr(g, 'api_key_user_id', None)
    if uid is not None:
        return f'u:{uid}'
    rate_key = getattr(g, 'rate_key', None)
    if rate_key and rate_key.startswith('api:'):
        return rate_key[4:]  # strip 'api:' prefix already baked in
    return None


def enforce_rest_rate_limit():
    """before_request hook: apply REST sliding-window limiter to /api/* API-key calls.

    Skips when:
      - path is not under /api/
      - request pool is not 'api_key' (session/widget/anon are handled elsewhere)
      - tier is 'admin' (unlimited — empty LIMITS_REST list)
      - key is flagged is_internal (never metered)

    On breach, returns a 429 JSON response with Retry-After header.
    """
    if not request.path.startswith('/api/'):
        return None

    # Don't interfere with OpenAPI docs/spec — they're intentionally public.
    if request.path in ('/api/docs', '/api/openapi.json'):
        return None

    if getattr(g, 'rate_pool', None) != 'api_key':
        return None

    if getattr(g, 'api_key_internal', False):
        return None  # Internal key — never metered, never rate-limited.

    tier = getattr(g, 'rate_tier', 'free')
    if not LIMITS_REST.get(tier):
        return None  # admin (empty list) or unknown tier

    key_id = _resolve_key_id()
    if not key_id:
        return None  # pool said api_key but no id resolved — let downstream auth reject

    try:
        stats = check_rest_rate_limit(key_id, tier)
        g.rest_rate_stats = stats
        return None
    except RateLimitExceeded as exc:
        logger.info(
            '[rate-limit] REST 429 key=%s tier=%s window=%s limit=%d retry=%ds',
            key_id, tier, exc.window_label, exc.limit, exc.retry_after,
        )
        resp = jsonify({
            'error': 'Rate limit exceeded',
            'limit': exc.limit,
            'window': exc.window_label,
            'retry_after': exc.retry_after,
        })
        resp.status_code = 429
        resp.headers['Retry-After'] = str(exc.retry_after)
        return resp


__all__ = [
    'LIMITS_REST',
    'KEY_PREFIX',
    'RateLimitExceeded',
    'check_rest_rate_limit',
    'peek_rest_rate_limit',
    'enforce_rest_rate_limit',
]
