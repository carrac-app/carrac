"""
MCP per-key rate limiter — thin adapter over the shared sliding-window
engine in carrac.rate_limit.

Limits by tier:
  free:    20 req/min,    300 req/hour,  2,000 req/day
  member:  60 req/min,  1,500 req/hour, 15,000 req/day
  premium: 120 req/min, 5,000 req/hour, 50,000 req/day
  admin:   unlimited

Free is intentionally generous per-minute: an LLM agent bursts 8-15 tool
calls per user question. All free tools are cheap cached reads / own-data
CRUD. Tune from the weekly usage review's data.

The shared engine keeps MCP and REST counters isolated via distinct Redis
key prefixes (and distinct in-memory bucket keys).
"""

import time  # noqa: F401 — exposed for test monkeypatching

from carrac.rate_limit import (
    RateLimitExceeded,
    _mem_buckets,
    check_rate_limit as _shared_check,
    peek_rate_limit as _shared_peek,
)

# Preserve the module-level _mem_check for tests that poke at it directly.
# The shared engine's _mem_check has a broader signature (prefix is first
# argument); the MCP variant fixes prefix to ''.
from carrac.rate_limit import _mem_check as _shared_mem_check


# (limit, window_seconds, label) per tier
LIMITS = {
    'free':    [(20,  60,    'minute'), (300,   3600,  'hour'), (2000,  86400, 'day')],
    'member':  [(60,  60,    'minute'), (1500,  3600,  'hour'), (15000, 86400, 'day')],
    'premium': [(120, 60,    'minute'), (5000,  3600,  'hour'), (50000, 86400, 'day')],
    'tester':  [(120, 60,    'minute'), (5000,  3600,  'hour'), (50000, 86400, 'day')],
    'admin':   [],  # unlimited
}

KEY_PREFIX = 'mcd:mcp:rl:'


def _mem_check(key_id, limit, window_s):
    """Legacy single-surface memory check (used only by test harness).

    Delegates to the shared engine with the MCP prefix.
    """
    return _shared_mem_check(KEY_PREFIX, key_id, limit, window_s)


def check_rate_limit(key_id, tier):
    """Record an MCP call and enforce the sliding-window budget.

    Raises RateLimitExceeded on breach. No-op for admin.
    """
    return _shared_check(key_id, tier, LIMITS, KEY_PREFIX)


def peek_rate_limit(key_id, tier):
    """Return current MCP usage without incrementing counters."""
    return _shared_peek(key_id, tier, LIMITS, KEY_PREFIX)


__all__ = [
    'LIMITS',
    'KEY_PREFIX',
    'RateLimitExceeded',
    'check_rate_limit',
    'peek_rate_limit',
    '_mem_buckets',
    '_mem_check',
]
