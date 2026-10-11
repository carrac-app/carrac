"""
Redis-backed response cache with in-memory fallback.

Replaces the plain dict-based ResponseCache when Redis is available.
All workers can read from Redis; only the lock-holder writes.
"""

import json
import logging
import threading
from datetime import datetime
from typing import Optional

import numpy as np

logger = logging.getLogger('carrac.redis_cache')

# Redis connection singleton
_redis_client = None
_redis_checked = False

KEY_PREFIX = 'mcd:response:'


def _numpy_serializer(obj):
    """JSON serializer that handles numpy types."""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def get_redis():
    """Get Redis client, or None if unavailable."""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    try:
        import redis
        import os
        url = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
        if not url:
            return None
        client = redis.from_url(url, socket_connect_timeout=2, decode_responses=False)
        client.ping()
        _redis_client = client
        logger.info("Redis connected for response cache")
        return _redis_client
    except Exception as e:
        logger.info(f"Redis unavailable, using in-memory cache: {e}")
        return None


class ResponseCache:
    """
    Thread-safe cache for pre-computed API responses.

    Uses Redis when available (shared across workers), falls back to
    in-memory dict (per-worker, same as before).
    """

    def __init__(self, local_fast_ttl: float = 5):
        self._cache = {}  # In-memory fallback + fast-path store
        self._timestamps = {}  # data's own write time (TTL_SECONDS + _cache_age)
        # wall-clock time THIS worker last confirmed each mode's
        # value (via set() or a Redis read) — separate from _timestamps
        # because the two differ whenever another worker wrote the data;
        # the fast-path gate below cares about "did I check recently?", not
        # "how old is the data?".
        self._synced_at = {}
        self._lock = threading.RLock()
        self._computing = {'htf': False, 'ltf': False}
        self.TTL_SECONDS = 3600  # 60 min — safety net; REFRESH_INTERVAL controls actual freshness
        self.REFRESH_INTERVAL = 1800  # 30 min — LTF compute takes ~18 min, so 10 min was too aggressive
        # in-process fast path — skip Redis if this worker
        # confirmed the value within local_fast_ttl seconds. Collapses the
        # per-request Redis GET+json.loads for the 2.1MB `htf` blob (rewritten
        # every REFRESH_INTERVAL=1800s) while bounding cross-worker
        # staleness to local_fast_ttl. Conservative default; per-mode tuning
        # would need a constructor arg threaded through at the instantiation
        # site (carrac/precompute.py — not ours to edit here).
        self._local_fast_ttl = local_fast_ttl

    def _redis(self):
        return get_redis()

    def _store_local(self, mode: str, data: dict, real_timestamp: datetime) -> None:
        """Write/re-arm the in-process copy backing both the memory-fallback
        path and the fast path. `real_timestamp` is the data's own write time
        (drives TTL_SECONDS + `_cache_age`); `_synced_at` is stamped to
        wall-clock now() (see __init__ for why they differ)."""
        with self._lock:
            self._cache[mode] = data
            self._timestamps[mode] = real_timestamp
            self._synced_at[mode] = datetime.now()

    def get(self, mode: str) -> Optional[dict]:
        """Get cached response if available and fresh.

        Fast path: if this worker confirmed `mode` within
        local_fast_ttl seconds (via a prior set() or Redis read), return the
        in-process copy directly — no Redis round-trip.
        """
        with self._lock:
            synced = self._synced_at.get(mode)
            recently_synced = (
                synced is not None
                and (datetime.now() - synced).total_seconds() < self._local_fast_ttl
            )
        if recently_synced:
            fast = self._memory_get_within(mode, self.TTL_SECONDS)
            if fast is not None:
                return fast
        r = self._redis()
        if r:
            return self._redis_get(r, mode)
        return self._memory_get(mode)

    def set(self, mode: str, data: dict):
        """Store response in cache."""
        r = self._redis()
        if r:
            self._redis_set(r, mode, data)
        self._memory_set(mode, data)

    def is_computing(self, mode: str) -> bool:
        # Redis-backed so the flag crosses containers: if the dedicated worker
        # is mid-warmup, a gunicorn worker in the web container must see the
        # same signal and return 202 instead of kicking off a duplicate
        # compute. Falls back to in-memory when Redis is unavailable.
        r = self._redis()
        if r:
            try:
                return bool(r.get(f'{KEY_PREFIX}computing:{mode}'))
            except Exception as e:
                logger.debug(f"Redis is_computing read failed ({e}); using memory fallback")
        with self._lock:
            return self._computing.get(mode, False)

    def set_computing(self, mode: str, value: bool):
        r = self._redis()
        if r:
            try:
                key = f'{KEY_PREFIX}computing:{mode}'
                if value:
                    # 30 min TTL — longer than any observed warmup cycle; the
                    # flag self-heals if the writer crashes mid-warmup.
                    r.set(key, '1', ex=1800)
                else:
                    r.delete(key)
            except Exception as e:
                logger.debug(f"Redis set_computing write failed ({e}); updating memory fallback only")
        with self._lock:
            self._computing[mode] = value

    def get_stats(self) -> dict:
        """Get cache statistics."""
        r = self._redis()
        if r:
            return self._redis_stats(r)
        return self._memory_stats()

    # --- Redis backend ---

    def _redis_get(self, r, mode: str) -> Optional[dict]:
        try:
            raw = r.get(f'{KEY_PREFIX}{mode}')
            if not raw:
                return None
            ts_raw = r.get(f'{KEY_PREFIX}{mode}:ts')
            if not ts_raw:
                return None
            timestamp = datetime.fromisoformat(ts_raw.decode('utf-8'))
            age = (datetime.now() - timestamp).total_seconds()
            if age > self.TTL_SECONDS:
                return None
            data = json.loads(raw)
            # re-arm the in-process fast path with what Redis
            # just returned, stamped with Redis's own write time (not
            # now()) so a later _cache_age computation stays accurate.
            self._store_local(mode, data, timestamp)
            out = data.copy()
            out['_cached'] = True
            out['_cache_age'] = int(age)
            return out
        except Exception as e:
            logger.warning(f"Redis cache get error: {e}")
            return self._memory_get(mode)

    def _redis_set(self, r, mode: str, data: dict):
        try:
            raw = json.dumps(data, default=_numpy_serializer)
            pipe = r.pipeline()
            pipe.set(f'{KEY_PREFIX}{mode}', raw, ex=self.TTL_SECONDS + 60)
            pipe.set(f'{KEY_PREFIX}{mode}:ts', datetime.now().isoformat(), ex=self.TTL_SECONDS + 60)
            pipe.execute()
            logger.info(f"Response cache updated in Redis for mode={mode}")
        except Exception as e:
            logger.warning(f"Redis cache set error: {e}")

    def _redis_stats(self, r) -> dict:
        stats = {}
        for mode in ['htf', 'ltf']:
            try:
                ts_raw = r.get(f'{KEY_PREFIX}{mode}:ts')
                if ts_raw:
                    timestamp = datetime.fromisoformat(ts_raw.decode('utf-8'))
                    age = (datetime.now() - timestamp).total_seconds()
                    raw = r.get(f'{KEY_PREFIX}{mode}')
                    data = json.loads(raw) if raw else {}
                    stats[mode] = {
                        'cached': True,
                        'age_seconds': int(age),
                        'fresh': age < self.TTL_SECONDS,
                        'asset_count': len(data.get('assets', [])),
                        'backend': 'redis'
                    }
                else:
                    stats[mode] = {'cached': False, 'backend': 'redis'}
            except Exception:
                stats[mode] = {'cached': False, 'backend': 'redis'}
        return stats

    # --- Memory backend (fallback) ---

    def _memory_get_within(self, mode: str, max_age: float) -> Optional[dict]:
        """Shallow-copy read from the in-process store, fresh within
        `max_age` seconds of the data's own write time. Always returns a
        copy — carrac/assets/routes.py mutates the dict it gets back
        (`cached_data['fetch_stats'] = ...`), so handing out the same
        object across repeat fast-path reads within local_fast_ttl would
        leak that display-only field into the next reader (that file isn't
        ours to fix, so the copy lives here instead)."""
        with self._lock:
            if mode not in self._cache:
                return None
            timestamp = self._timestamps.get(mode)
            if not timestamp:
                return None
            age = (datetime.now() - timestamp).total_seconds()
            if age > max_age:
                return None
            data = self._cache[mode].copy()
            data['_cached'] = True
            data['_cache_age'] = int(age)
            return data

    def _memory_get(self, mode: str) -> Optional[dict]:
        return self._memory_get_within(mode, self.TTL_SECONDS)

    def _memory_set(self, mode: str, data: dict):
        self._store_local(mode, data, datetime.now())
        logger.info(f"Response cache updated in memory for mode={mode}")

    def _memory_stats(self) -> dict:
        with self._lock:
            stats = {}
            for mode in ['htf', 'ltf']:
                if mode in self._timestamps:
                    age = (datetime.now() - self._timestamps[mode]).total_seconds()
                    stats[mode] = {
                        'cached': True,
                        'age_seconds': int(age),
                        'fresh': age < self.TTL_SECONDS,
                        'asset_count': len(self._cache.get(mode, {}).get('assets', [])),
                        'backend': 'memory'
                    }
                else:
                    stats[mode] = {'cached': False, 'backend': 'memory'}
            return stats
