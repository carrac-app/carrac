"""Cost instrumentation — timing + usage counters for the facts batch.

Both functions are NON-FATAL: Redis errors are swallowed so a Redis hiccup
never breaks the precompute batch. Accept an optional ``r`` client so tests
can inject a fake; otherwise fall back to the real ``get_redis()`` accessor
from ``carrac.redis_cache``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _redis(r: Any | None):
    """Return the provided client, or the real Redis client (may be None)."""
    if r is not None:
        return r
    from carrac.redis_cache import get_redis
    return get_redis()


def track_batch_timing(name: str, seconds: float, r: Any | None = None) -> None:
    """Log + record the wall-time of a batch sub-step. Non-fatal on Redis error."""
    log.info("FACTS_TIMING %s %.3fs", name, seconds)
    client = _redis(r)
    if client is None:
        return
    try:
        client.set(f"facts:timing:{name}", seconds)
    except Exception as exc:  # noqa: BLE001
        log.debug("track_batch_timing Redis error (swallowed): %s", exc)


def bump_usage(
    endpoint: str,
    tier: str,
    r: Any | None = None,
    day: str | None = None,
) -> int | None:
    """Increment a per-endpoint/per-tier/per-UTC-day usage counter.

    Returns the new count, or None if Redis is unavailable or raises.
    Non-fatal: any Redis exception is swallowed.
    """
    day = day or datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    key = f"facts:usage:{endpoint}:{tier}:{day}"
    client = _redis(r)
    if client is None:
        return None
    try:
        n = client.incr(key)
        try:
            client.expire(key, 86400 * 7)  # 7-day retention
        except Exception:
            pass
        return n
    except Exception as exc:  # noqa: BLE001
        log.debug("bump_usage Redis error (swallowed): %s", exc)
        return None
