"""Tests for carrac.facts.cost — track_batch_timing + bump_usage."""
from __future__ import annotations

import pytest

from carrac.facts.cost import bump_usage, track_batch_timing


# ---------------------------------------------------------------------------
# Fake Redis stub
# ---------------------------------------------------------------------------

class FakeRedis:
    """Minimal dict-backed Redis stub for testing."""

    def __init__(self):
        self._store: dict[str, object] = {}
        self._ttls: dict[str, int] = {}

    def set(self, key: str, value) -> None:
        self._store[key] = value

    def incr(self, key: str) -> int:
        current = int(self._store.get(key, 0))
        self._store[key] = current + 1
        return current + 1

    def expire(self, key: str, seconds: int) -> None:
        self._ttls[key] = seconds

    def get_val(self, key: str):
        return self._store.get(key)

    def get_ttl(self, key: str) -> int | None:
        return self._ttls.get(key)


class ErrorRedis:
    """Redis stub whose incr always raises."""

    def incr(self, key: str) -> None:
        raise ConnectionError("Redis is down")

    def set(self, key: str, value) -> None:
        raise ConnectionError("Redis is down")

    def expire(self, key: str, seconds: int) -> None:
        raise ConnectionError("Redis is down")


# ---------------------------------------------------------------------------
# Tests — bump_usage
# ---------------------------------------------------------------------------

class TestBumpUsage:

    def test_first_increment_returns_1(self):
        fake = FakeRedis()
        result = bump_usage("market_get_detail", "free", r=fake, day="20260626")
        assert result == 1

    def test_second_increment_returns_2(self):
        fake = FakeRedis()
        bump_usage("market_get_detail", "free", r=fake, day="20260626")
        result = bump_usage("market_get_detail", "free", r=fake, day="20260626")
        assert result == 2

    def test_counter_key_holds_final_value(self):
        fake = FakeRedis()
        bump_usage("market_get_detail", "free", r=fake, day="20260626")
        bump_usage("market_get_detail", "free", r=fake, day="20260626")
        assert fake.get_val("facts:usage:market_get_detail:free:20260626") == 2

    def test_different_days_are_independent(self):
        fake = FakeRedis()
        bump_usage("market_get_detail", "free", r=fake, day="20260625")
        bump_usage("market_get_detail", "free", r=fake, day="20260626")
        assert fake.get_val("facts:usage:market_get_detail:free:20260625") == 1
        assert fake.get_val("facts:usage:market_get_detail:free:20260626") == 1

    def test_redis_error_returns_none(self):
        bad = ErrorRedis()
        result = bump_usage("market_get_detail", "free", r=bad, day="20260626")
        assert result is None

    def test_redis_error_does_not_raise(self):
        bad = ErrorRedis()
        # Should silently return None, not propagate the exception
        try:
            bump_usage("market_get_detail", "free", r=bad, day="20260626")
        except Exception as exc:
            pytest.fail(f"bump_usage raised unexpectedly: {exc}")

    def test_expire_called_with_7day_ttl(self):
        fake = FakeRedis()
        bump_usage("api_facts", "free", r=fake, day="20260626")
        key = "facts:usage:api_facts:free:20260626"
        assert fake.get_ttl(key) == 86400 * 7

    def test_expire_called_on_second_increment(self):
        fake = FakeRedis()
        bump_usage("api_facts", "free", r=fake, day="20260626")
        bump_usage("api_facts", "free", r=fake, day="20260626")
        key = "facts:usage:api_facts:free:20260626"
        assert fake.get_ttl(key) == 86400 * 7

    def test_expire_error_does_not_raise(self):
        class ExpireFailRedis(FakeRedis):
            def expire(self, key, seconds):
                raise ConnectionError("expire failed")
        fake = ExpireFailRedis()
        try:
            result = bump_usage("api_facts", "free", r=fake, day="20260626")
        except Exception as exc:
            pytest.fail(f"bump_usage raised on expire error: {exc}")
        assert result == 1  # incr still succeeded


# ---------------------------------------------------------------------------
# Tests — track_batch_timing
# ---------------------------------------------------------------------------

class TestTrackBatchTiming:

    def test_sets_timing_key(self):
        fake = FakeRedis()
        track_batch_timing("facts", 0.42, r=fake)
        assert fake.get_val("facts:timing:facts") == 0.42

    def test_redis_error_does_not_raise(self):
        bad = ErrorRedis()
        try:
            track_batch_timing("facts", 0.5, r=bad)
        except Exception as exc:
            pytest.fail(f"track_batch_timing raised on Redis error: {exc}")

    def test_multiple_timings_overwrite(self):
        fake = FakeRedis()
        track_batch_timing("step1", 1.0, r=fake)
        track_batch_timing("step1", 2.5, r=fake)
        assert fake.get_val("facts:timing:step1") == 2.5
