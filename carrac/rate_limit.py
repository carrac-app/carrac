"""
Shared sliding-window rate limiter for per-key surfaces.

Used by:
  - carrac.mcp.rate_limit  (MCP JSON-RPC endpoint)
  - carrac.rest_rate_limit    (REST /api/* endpoints)

Each caller supplies its own LIMITS_BY_TIER and Redis key prefix, so MCP and
REST counters are independent even when the same key_id hits both surfaces.

Storage:
  Redis sorted set per (key_id, window) — score = epoch_ms.
  ZREMRANGEBYSCORE prunes old entries; ZCARD counts current usage.
  Falls back to thread-safe in-memory deques per process if Redis is down.
"""

import logging
import threading
import time
from collections import defaultdict, deque

logger = logging.getLogger('carrac.rate_limit')


class RateLimitExceeded(Exception):
    def __init__(self, limit, window_label, retry_after):
        self.limit = limit
        self.window_label = window_label
        self.retry_after = retry_after
        super().__init__(
            f'Rate limit exceeded: {limit}/{window_label}. Retry in {retry_after}s.'
        )


# ── In-memory fallback (process-local) ─────────────────────────
#
# Bucket key is (prefix, key_id, window_s) so MCP and REST stay isolated
# even when the process is the single source of truth (no Redis).

_mem_lock = threading.RLock()
_mem_buckets = defaultdict(deque)  # {(prefix, key_id, window_s): deque[epoch_s]}


def _mem_check(prefix, key_id, limit, window_s, consume=True):
    """Sliding window check using in-process deque.

    Returns (allowed, current_count, retry_after). If consume=False, does
    not record the call — used by peek().
    """
    now = time.time()
    cutoff = now - window_s
    with _mem_lock:
        dq = _mem_buckets[(prefix, key_id, window_s)]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            retry = max(1, int(dq[0] + window_s - now)) if dq else window_s
            return False, len(dq), retry
        if consume:
            dq.append(now)
            return True, len(dq), 0
        return True, len(dq), 0


def _redis_check(client, prefix, key_id, limit, window_s, consume=True):
    """Sliding window check using Redis sorted set. Returns (allowed, current_count, retry_after)."""
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - window_s * 1000
    redis_key = f'{prefix}{key_id}:{window_s}'
    try:
        pipe = client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, cutoff_ms)
        pipe.zcard(redis_key)
        _, count = pipe.execute()
        if count >= limit:
            oldest = client.zrange(redis_key, 0, 0, withscores=True)
            if oldest:
                retry = max(1, int((oldest[0][1] + window_s * 1000 - now_ms) / 1000))
            else:
                retry = window_s
            return False, count, retry
        if consume:
            pipe = client.pipeline()
            pipe.zadd(redis_key, {f'{now_ms}:{id(threading.current_thread())}': now_ms})
            pipe.expire(redis_key, window_s + 5)
            pipe.execute()
            return True, count + 1, 0
        return True, count, 0
    except Exception as e:
        logger.warning(f'Redis rate limit check failed, falling back to memory: {e}')
        return _mem_check(prefix, key_id, limit, window_s, consume=consume)


def _run_windows(key_id, tier, limits_by_tier, prefix, consume):
    """Run every window for the tier; raise on first breach (when consume=True).

    When consume=False, returns stats without raising — caller inspects `remaining`.
    """
    if not key_id:
        raise RateLimitExceeded(0, 'minute', 60)

    limits = limits_by_tier.get(tier or 'free', limits_by_tier.get('free', []))
    if not limits:
        return {'tier': tier, 'unlimited': True, 'usage': []}

    from carrac.redis_cache import get_redis

    client = get_redis()
    stats = {'tier': tier, 'unlimited': False, 'usage': []}

    for limit, window_s, label in limits:
        if client:
            allowed, count, retry = _redis_check(client, prefix, key_id, limit, window_s, consume=consume)
        else:
            allowed, count, retry = _mem_check(prefix, key_id, limit, window_s, consume=consume)
        stats['usage'].append({
            'window': label,
            'window_seconds': window_s,
            'used': count,
            'limit': limit,
            'remaining': max(0, limit - count),
            'retry_after': 0 if allowed else retry,
        })
        if consume and not allowed:
            raise RateLimitExceeded(limit, label, retry)

    return stats


def check_rate_limit(key_id, tier, limits_by_tier, key_prefix):
    """Record a call and enforce the sliding-window budget.

    Raises RateLimitExceeded if any window is over budget. No-op for tiers
    with empty limits (e.g. 'admin').

    Args:
        key_id: stable identifier (user_id or key fingerprint). Required.
        tier:   'free' | 'member' | 'tester' | 'premium' | 'admin'.
        limits_by_tier: {tier: [(limit, window_seconds, label), ...]}.
        key_prefix: Redis key namespace, e.g. 'mcd:mcp:rl:' or 'mcd:rest:rl:'.

    Returns:
        dict with per-window usage stats.
    """
    return _run_windows(key_id, tier, limits_by_tier, key_prefix, consume=True)


def peek_rate_limit(key_id, tier, limits_by_tier, key_prefix):
    """Return current usage without incrementing counters.

    Used by /api/usage and similar introspection endpoints. Never raises —
    returns stats with remaining=0 if the caller is already over budget.
    """
    return _run_windows(key_id, tier, limits_by_tier, key_prefix, consume=False)
