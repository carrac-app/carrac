"""
API Rate Limiter - Tracks and enforces rate limits across all data providers.

Persists call counts to disk for container restart recovery.
Provides usage stats for monitoring and UI display.

================================================================================
HOW TO INTEGRATE A NEW API PROVIDER
================================================================================

When adding a new data provider to the dashboard, follow these steps:

1. ADD TO RATE_LIMITS (below):
   Add a new entry to the RATE_LIMITS dict with:
   - 'calls_per_window': Conservative limit (80-90% of actual to avoid bans)
   - 'window_seconds': Time window (60=minute, 3600=hour, 86400=day)
   - 'description': Human-readable name for UI display
   - 'soft_limit_pct': Warning threshold (0.7-0.8 typical)

   Example:
   'new_provider': {
       'calls_per_window': 500,      # Actual: 600/hour, using 83%
       'window_seconds': 3600,       # 1 hour window
       'description': 'NewProvider (600/hour)',
       'soft_limit_pct': 0.8,        # Warn at 80%
   },

2. ADD RATE LIMIT CHECK BEFORE API CALL:
   In your fetch function (database.py or cache_manager.py):

   from api_rate_limiter import can_call, record_call

   def fetch_new_provider_data():
       allowed, reason = can_call('new_provider')
       if not allowed:
           logger.warning(f"Rate limit hit: {reason}")
           return fallback_data_or_empty()

       # Make your API call here
       response = requests.get(...)

       # Record the call AFTER success
       record_call('new_provider', count=1)
       return response.json()

3. RESPECT PROVIDER-SPECIFIC LIMITS:
   - Read their docs carefully for rate limits
   - Some have per-minute AND per-day limits (add both!)
   - Some count multiple endpoints differently
   - Some have burst limits (use smaller windows)

4. ADD TO UI (/api/usage endpoint in carrac/usage_routes.py):
   The get_usage_stats() function automatically includes all providers
   in RATE_LIMITS, so just adding to RATE_LIMITS is enough.

5. TEST BEFORE PRODUCTION:
   - Watch logs for "Rate limit" warnings
   - Monitor /api/usage endpoint for accurate tracking
   - Test container restart recovery (check api_rate_limits.json)

================================================================================
"""

import json
import os
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple
import logging

logger = logging.getLogger('carrac.rate_limiter')

# Shared prefix so ops can see the full picture across workers.
_REDIS_KEY_PREFIX = 'mcd:rate:'


class APIRateLimiter:
    """
    Thread-safe rate limiter with persistent storage.
    Tracks calls per provider and enforces limits.
    """

    # Rate limits per provider (calls per time window)
    # CRITICAL: Use conservative limits to stay WELL under actual limits
    # All limits verified against actual API documentation as of 2025-12
    RATE_LIMITS = {

        # ========== Binance ==========
        'binance': {
            'calls_per_window': 1000,      # Actual: 1200/min, using 83%
            'window_seconds': 60,          # 1 minute
            'description': 'Binance (1200/min)',
            'soft_limit_pct': 0.8,
        },
        'binance_daily': {
            'calls_per_window': 5000,      # Spread limit for daily calls
            'window_seconds': 3600,        # 1 hour
            'description': 'Binance Daily OHLCV',
            'soft_limit_pct': 0.8,
        },
        'binance_futures': {
            'calls_per_window': 800,       # Actual: 1000/5min, using 80%
            'window_seconds': 300,         # 5 minutes
            'description': 'Binance Futures (1000/5min)',
            'soft_limit_pct': 0.8,
        },
        # ========== Hyperliquid ==========
        # KEYLESS public /info endpoint, ~1200 weight/min/IP. Used as fallback
        # for HL-only coins (HYPE, FARTCOIN, kPEPE, etc.) — see HL_COINS in
        # data_sources/hyperliquid.py.
        'hyperliquid': {
            'calls_per_window': 1000,      # Actual: ~1200 weight/min, using 83%
            'window_seconds': 60,          # 1 minute
            'description': 'Hyperliquid (1200 weight/min)',
            'soft_limit_pct': 0.8,
        },
        # ========== CCXT Exchanges ==========
        # Multi-exchange fallback via CCXT (MEXC, Gate.io, KuCoin, Bybit).
        # CCXT handles per-request pacing (enableRateLimit=True); these are
        # outer accounting for monitoring + hard-cap safety.
        'ccxt_mexc': {
            'calls_per_window': 600,       # MEXC: ~1200/min, using 50%
            'window_seconds': 60,
            'description': 'MEXC via CCXT (1200/min)',
            'soft_limit_pct': 0.8,
        },
        'ccxt_gateio': {
            'calls_per_window': 300,       # Gate.io: ~900/10s rolling, conservative
            'window_seconds': 60,
            'description': 'Gate.io via CCXT (900/10s)',
            'soft_limit_pct': 0.8,
        },
        'ccxt_kucoin': {
            'calls_per_window': 50,        # KuCoin: 100/10s unauthenticated, using 50%
            'window_seconds': 10,
            'description': 'KuCoin via CCXT (100/10s)',
            'soft_limit_pct': 0.8,
        },
        'ccxt_bybit': {
            'calls_per_window': 500,       # Bybit: ~600/5min, using 83%
            'window_seconds': 300,
            'description': 'Bybit via CCXT (120/min)',
            'soft_limit_pct': 0.8,
        },
        # ========== CoinPaprika ==========
        'coinpaprika': {
            'calls_per_window': 2000,      # Conservative
            'window_seconds': 24 * 3600,   # Daily
            'description': 'CoinPaprika (~2500/day)',
            'soft_limit_pct': 0.8,
        },
        # ========== Other Providers ==========
        'stooq': {
            'calls_per_window': 50,        # Unknown limit, very conservative
            'window_seconds': 3600,        # 1 hour
            'description': 'Stooq (unknown limit)',
            'soft_limit_pct': 0.5,
        },
        'yahoo': {
            'calls_per_window': 200,       # Yahoo v8 tolerates ~2000/day; 200/hr safe
            'window_seconds': 3600,        # 1 hour
            'description': 'Yahoo Finance (200/hr)',
            'soft_limit_pct': 0.8,
        },
        'yahoo_minute': {
            'calls_per_window': 10,        # Burst limiter: prevent hammering on cold start
            'window_seconds': 60,          # 1 minute
            'description': 'Yahoo Finance Burst (10/min)',
            'soft_limit_pct': 0.8,
        },
        'stablecoin_onchain': {
            'calls_per_window': 20,        # ~5 RPC calls/day; bucket is cosmetic
            'window_seconds': 24 * 3600,   # 1 day
            'description': 'On-chain stablecoin (ETH public RPC + TronGrid)',
            'soft_limit_pct': 0.8,
        },
        'blockchain_com': {
            'calls_per_window': 4,
            'window_seconds': 60,
            'description': 'Blockchain.com (6/min est)',
            'soft_limit_pct': 0.7,
        },
        'coinglass': {
            'calls_per_window': 70,        # Free tier: 100/month, using 70%
            'window_seconds': 30 * 24 * 3600,  # 30 days
            'description': 'CoinGlass (100/month free)',
            'soft_limit_pct': 0.7,
        },
        # CoinGlass daily sub-limit: ~3 calls/day to spread evenly
        'coinglass_daily': {
            'calls_per_window': 3,         # ~3/day for 30 days = 90 calls
            'window_seconds': 24 * 3600,   # 1 day
            'description': 'CoinGlass Daily Sub-limit',
            'soft_limit_pct': 0.8,
        },
    }

    SAVE_INTERVAL = 30  # Flush to disk at most every 30 seconds

    def __init__(self, cache_dir: Path = None):
        if cache_dir is None:
            cache_dir = Path(os.environ.get('DATA_DIR', 'data'))
        self.cache_dir = cache_dir
        self.cache_file = cache_dir / 'api_rate_limits.json'
        self._lock = threading.Lock()

        # Call tracking: {provider: [(timestamp, count), ...]}
        # In Redis mode this is only used as a fallback when Redis ops fail.
        self._calls: Dict[str, list] = {}
        self._dirty = False
        self._last_save_time = 0.0

        # Redis-backed shared state. Gunicorn runs 4 web workers + 1
        # compute worker; each was keeping its own counter, so an
        # "8000 calls per 30 days" budget was really 5 × 8000. If Redis is up
        # we mirror every record into a sorted set keyed per provider, and
        # every can_call / get_usage_stats reads from there. In-memory state
        # becomes the fallback only when Redis ops fail.
        self._redis = None
        self._use_redis = False
        self._init_redis()

        # Load persistent state (still useful for cold-start recovery even
        # when Redis is live — the in-memory dict seeds the display while
        # Redis catches up).
        self._load_state()

        # Start periodic flush thread (only emits when in-memory is the
        # source of truth — skip writes when Redis owns the data).
        self._flush_thread = threading.Thread(target=self._periodic_flush, daemon=True)
        self._flush_thread.start()

        logger.info(
            "API Rate Limiter initialized (backend=%s)",
            "redis" if self._use_redis else "in-memory",
        )

    def _init_redis(self) -> None:
        """Try to open a redis connection for cross-process counting."""
        url = os.environ.get('REDIS_URL', '').strip()
        if not url:
            return
        try:
            import redis  # lazy import — optional dependency
            client = redis.from_url(url, socket_connect_timeout=3, decode_responses=True)
            client.ping()
            self._redis = client
            self._use_redis = True
        except Exception as e:
            logger.warning("API rate limiter: Redis unavailable, using in-memory fallback (%s)", e)
            self._redis = None
            self._use_redis = False

    @staticmethod
    def _redis_key(provider: str) -> str:
        return f"{_REDIS_KEY_PREFIX}{provider}"

    def _redis_add(self, provider: str, count: int) -> bool:
        """Record `count` call(s) in the provider's sorted set. Returns True on success."""
        if not self._use_redis or self._redis is None:
            return False
        try:
            window = self.RATE_LIMITS[provider]['window_seconds']
            now = time.time()
            key = self._redis_key(provider)
            pipe = self._redis.pipeline()
            # Write `count` members so ZCARD reflects real call volume. A
            # nanosecond suffix + short uuid prevents score collisions and
            # rejects duplicates from concurrent workers.
            batch_id = uuid.uuid4().hex[:8]
            for i in range(count):
                pipe.zadd(key, {f"{now:.6f}:{batch_id}:{i}": now})
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.expire(key, window + 60)
            pipe.execute()
            return True
        except Exception as e:
            logger.warning("Redis record_call failed for %s: %s", provider, e)
            return False

    def _redis_count(self, provider: str) -> Optional[int]:
        """Return current count in window from Redis, or None if Redis unavailable."""
        if not self._use_redis or self._redis is None:
            return None
        try:
            window = self.RATE_LIMITS[provider]['window_seconds']
            now = time.time()
            key = self._redis_key(provider)
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.zcard(key)
            _, count = pipe.execute()
            return int(count)
        except Exception as e:
            logger.warning("Redis count failed for %s: %s", provider, e)
            return None

    def _redis_oldest_ts(self, provider: str) -> Optional[float]:
        """Return timestamp of oldest call in window, or None."""
        if not self._use_redis or self._redis is None:
            return None
        try:
            key = self._redis_key(provider)
            items = self._redis.zrange(key, 0, 0, withscores=True)
            if not items:
                return None
            return float(items[0][1])
        except Exception:
            return None

    def _load_state(self) -> None:
        """Load persistent state from disk."""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                    self._calls = {
                        provider: [(ts, count) for ts, count in calls]
                        for provider, calls in data.get('calls', {}).items()
                    }
                logger.info(f"Loaded rate limit state from {self.cache_file}")
        except Exception as e:
            logger.warning(f"Failed to load rate limit state: {e}")
            self._calls = {}

    def _save_state(self) -> None:
        """Save state to disk for container restart recovery."""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            data = {
                'calls': self._calls,
                'saved_at': datetime.now().isoformat()
            }
            with open(self.cache_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"Failed to save rate limit state: {e}")

    def _cleanup_old_calls(self, provider: str) -> None:
        """Remove calls outside the rate limit window."""
        if provider not in self.RATE_LIMITS:
            return

        window_seconds = self.RATE_LIMITS[provider]['window_seconds']
        cutoff = time.time() - window_seconds

        if provider in self._calls:
            self._calls[provider] = [
                (ts, count) for ts, count in self._calls[provider]
                if ts > cutoff
            ]

    def _get_call_count(self, provider: str) -> int:
        """Get total calls in the current window."""
        self._cleanup_old_calls(provider)
        if provider not in self._calls:
            return 0
        return sum(count for _, count in self._calls[provider])

    def can_call(self, provider: str) -> Tuple[bool, str]:
        """
        Check if we can make an API call to this provider.
        Returns (allowed, reason).
        """
        if provider not in self.RATE_LIMITS:
            return True, "Unknown provider - allowing"

        limit_config = self.RATE_LIMITS[provider]
        limit = limit_config['calls_per_window']
        soft_limit = int(limit * limit_config['soft_limit_pct'])

        # Redis path — shared across all workers.
        redis_count = self._redis_count(provider)
        if redis_count is not None:
            if redis_count >= limit:
                return False, f"Rate limit reached ({redis_count}/{limit})"
            if redis_count >= soft_limit:
                remaining = limit - redis_count
                logger.warning(f"{provider}: Approaching limit ({redis_count}/{limit}, {remaining} remaining)")
            return True, f"OK ({redis_count}/{limit})"

        # Fallback: in-memory counter (per-process).
        with self._lock:
            current_count = self._get_call_count(provider)
            if current_count >= limit:
                return False, f"Rate limit reached ({current_count}/{limit})"
            if current_count >= soft_limit:
                remaining = limit - current_count
                logger.warning(f"{provider}: Approaching limit ({current_count}/{limit}, {remaining} remaining)")
            return True, f"OK ({current_count}/{limit})"

    def record_call(self, provider: str, count: int = 1) -> None:
        """Record that we made API call(s) to this provider."""
        # Redis-first. Successful write = done. Fall through to
        # in-memory only if Redis is unavailable, so the two backends don't
        # both grow state during steady-state ops.
        if self._redis_add(provider, count):
            return
        with self._lock:
            if provider not in self._calls:
                self._calls[provider] = []

            self._calls[provider].append((time.time(), count))
            self._cleanup_old_calls(provider)
            self._dirty = True

    def _periodic_flush(self) -> None:
        """Background thread: flush dirty state to disk every SAVE_INTERVAL seconds."""
        while True:
            time.sleep(self.SAVE_INTERVAL)
            # Skip disk flush when Redis owns the counters — the
            # file only captures this one process's fallback state and is
            # stale within seconds. Keep the thread alive so Redis outages
            # which set `_use_redis = False` still flush.
            if self._use_redis:
                continue
            with self._lock:
                if self._dirty:
                    self._save_state()
                    self._dirty = False

    def get_usage_stats(self) -> Dict[str, dict]:
        """
        Get usage statistics for all providers.
        Returns dict with usage info for UI display.
        """
        stats = {}

        with self._lock:
            for provider, config in self.RATE_LIMITS.items():
                limit = config['calls_per_window']
                soft_limit = int(limit * config['soft_limit_pct'])
                window_seconds = config['window_seconds']

                # Prefer Redis (shared view) with in-memory fallback.
                current_count = self._redis_count(provider)
                oldest_call: Optional[float]
                if current_count is not None:
                    oldest_call = self._redis_oldest_ts(provider)
                else:
                    self._cleanup_old_calls(provider)
                    current_count = self._get_call_count(provider)
                    if provider in self._calls and self._calls[provider]:
                        oldest_call = min(ts for ts, _ in self._calls[provider])
                    else:
                        oldest_call = None

                # Calculate time until window resets
                if oldest_call is not None:
                    reset_at = oldest_call + window_seconds
                    seconds_until_reset = max(0, reset_at - time.time())
                else:
                    seconds_until_reset = 0

                # Determine status
                pct_used = (current_count / limit * 100) if limit > 0 else 0
                if current_count >= limit:
                    status = 'blocked'
                elif current_count >= soft_limit:
                    status = 'warning'
                else:
                    status = 'ok'

                stats[provider] = {
                    'description': config['description'],
                    'current': current_count,
                    'limit': limit,
                    'soft_limit': soft_limit,
                    'pct_used': round(pct_used, 1),
                    'status': status,
                    'window_seconds': window_seconds,
                    'window_human': self._format_window(window_seconds),
                    'reset_seconds': int(seconds_until_reset),
                    'reset_human': self._format_duration(seconds_until_reset),
                }

        return stats

    def _format_window(self, seconds: int) -> str:
        """Format window duration for display."""
        if seconds >= 24 * 3600:
            days = seconds // (24 * 3600)
            return f"{days}d" if days > 1 else "day"
        elif seconds >= 3600:
            hours = seconds // 3600
            return f"{hours}h" if hours > 1 else "hour"
        else:
            mins = seconds // 60
            return f"{mins}m" if mins > 1 else "min"

    def _format_duration(self, seconds: float) -> str:
        """Format duration for display."""
        if seconds <= 0:
            return "now"
        elif seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds / 60)}m"
        elif seconds < 24 * 3600:
            return f"{int(seconds / 3600)}h"
        else:
            return f"{int(seconds / (24 * 3600))}d"


# Global singleton instance
_rate_limiter: Optional[APIRateLimiter] = None
_rate_limiter_lock = threading.Lock()


def get_rate_limiter() -> APIRateLimiter:
    """Get the global rate limiter instance."""
    global _rate_limiter
    with _rate_limiter_lock:
        if _rate_limiter is None:
            _rate_limiter = APIRateLimiter()
        return _rate_limiter


def can_call(provider: str) -> Tuple[bool, str]:
    """Check if we can make an API call."""
    return get_rate_limiter().can_call(provider)


def record_call(provider: str, count: int = 1) -> None:
    """Record that we made an API call."""
    get_rate_limiter().record_call(provider, count)


def get_usage_stats() -> Dict[str, dict]:
    """Get usage statistics for all providers."""
    return get_rate_limiter().get_usage_stats()


# ============================================================================
# MULTI-TIER RATE LIMIT HELPERS
# ============================================================================
# These functions check ALL applicable rate limits for a provider to prevent
# exceeding any tier (monthly, daily, hourly, per-minute).


def can_call_yahoo() -> Tuple[bool, str]:
    """
    Check ALL Yahoo Finance rate limits before making a call.
    Checks: hourly and per-minute burst limits.
    Returns (allowed, reason).
    """
    limiter = get_rate_limiter()

    tiers = ['yahoo', 'yahoo_minute']
    for tier in tiers:
        allowed, reason = limiter.can_call(tier)
        if not allowed:
            logger.warning(f"Yahoo rate limit hit ({tier}): {reason}")
            return False, f"{tier}: {reason}"

    return True, "OK"


def record_yahoo_call(count: int = 1) -> None:
    """
    Record a Yahoo Finance API call to ALL rate limit tiers.
    Must be called after each successful API call.
    """
    limiter = get_rate_limiter()
    tiers = ['yahoo', 'yahoo_minute']
    for tier in tiers:
        limiter.record_call(tier, count)
