"""Tests for MCP per-key rate limiting."""

import pytest

from carrac.mcp.rate_limit import (
    RateLimitExceeded,
    check_rate_limit,
    _mem_buckets,
)


@pytest.fixture(autouse=True)
def _clear_mem_buckets(monkeypatch):
    # _run_windows prefers Redis when get_redis() returns a client. Force
    # the memory backend so these unit tests are hermetic against any
    # locally-running Redis (the autouse clear() below wouldn't reach
    # Redis state, leading to state-leak failures across runs).
    monkeypatch.setattr("carrac.redis_cache.get_redis", lambda: None)
    _mem_buckets.clear()
    yield
    _mem_buckets.clear()


class TestRateLimitMemory:
    def test_admin_unlimited(self):
        # 10x past the highest premium minute limit must still pass.
        for _ in range(700):
            check_rate_limit('admin-key', 'admin')

    def test_member_minute_limit(self):
        # member: 60/min — first 60 succeed, 61st raises.
        for _ in range(60):
            check_rate_limit('member-key-1', 'member')
        with pytest.raises(RateLimitExceeded) as exc:
            check_rate_limit('member-key-1', 'member')
        assert exc.value.limit == 60
        assert exc.value.window_label == 'minute'
        assert exc.value.retry_after >= 1

    def test_premium_minute_limit(self):
        # premium: 120/min.
        for _ in range(120):
            check_rate_limit('premium-key-1', 'premium')
        with pytest.raises(RateLimitExceeded):
            check_rate_limit('premium-key-1', 'premium')

    def test_per_key_isolation(self):
        # Two different keys don't share counters.
        for _ in range(30):
            check_rate_limit('key-A', 'member')
        # key-B should still have full budget.
        check_rate_limit('key-B', 'member')

    def test_free_tier_has_budget(self):
        # free is now open (invite beta): 20/min — first 20 succeed, 21st raises.
        for _ in range(20):
            check_rate_limit('free-key', 'free')
        with pytest.raises(RateLimitExceeded) as exc:
            check_rate_limit('free-key', 'free')
        assert exc.value.limit == 20

    def test_unknown_tier_treated_as_free(self):
        # Forged/unknown tier degrades to the free budget (least privilege).
        for _ in range(20):
            check_rate_limit('weird-key', 'banana')
        with pytest.raises(RateLimitExceeded) as exc:
            check_rate_limit('weird-key', 'banana')
        assert exc.value.limit == 20

    def test_empty_key_id_rejected(self):
        with pytest.raises(RateLimitExceeded):
            check_rate_limit('', 'premium')

    def test_returns_usage_stats(self):
        stats = check_rate_limit('stats-key', 'member')
        assert stats['tier'] == 'member'
        assert any(u['window'] == 'minute' for u in stats['usage'])

    def test_retry_after_decreases_over_time(self, monkeypatch):
        import carrac.mcp.rate_limit as rl
        # Freeze time so we can advance manually.
        t = [1000.0]
        monkeypatch.setattr(rl.time, 'time', lambda: t[0])
        for _ in range(60):
            check_rate_limit('time-key', 'member')
        # 61st call denied.
        with pytest.raises(RateLimitExceeded) as exc:
            check_rate_limit('time-key', 'member')
        first_retry = exc.value.retry_after
        # Advance 30s → retry_after should drop.
        t[0] += 30
        with pytest.raises(RateLimitExceeded) as exc2:
            check_rate_limit('time-key', 'member')
        assert exc2.value.retry_after < first_retry

    def test_window_slides_in(self, monkeypatch):
        import carrac.mcp.rate_limit as rl
        t = [2000.0]
        monkeypatch.setattr(rl.time, 'time', lambda: t[0])
        for _ in range(30):
            check_rate_limit('slide-key', 'member')
        # Advance 61s → all entries expire from the minute window.
        t[0] += 61
        check_rate_limit('slide-key', 'member')


# HTTP-level burst coverage moved to
# tests/fastapi/test_mcp.py::test_mcp_429_after_member_burst
# (there is no Flask /mcp blueprint). Per-key rate-limit
# module unit tests live above in TestRateLimitMemory.
