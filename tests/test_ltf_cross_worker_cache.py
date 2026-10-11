"""Cross-worker LTF DataFrame cache (Redis L2 behind in-process L1).

Two independent cache_manager module states sharing ONE Redis double:
  A populates → B reads from L2 (no underlying fetch called again).

Plus: Redis-down fallback → still works via compute.
"""
from __future__ import annotations

import threading


# ---------------------------------------------------------------------------
# Minimal FakeRedis: stores bytes, supports set/get/ttl, thread-safe
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Thread-safe in-memory Redis stand-in (bytes semantics)."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self._lock = threading.Lock()

    def get(self, k: str):
        with self._lock:
            return self.store.get(k)

    def set(self, k: str, v, ex: int = None):
        with self._lock:
            self.store[k] = v if isinstance(v, bytes) else v.encode()
            if ex is not None:
                self.ttls[k] = ex

    def ttl(self, k: str) -> int:
        with self._lock:
            return self.ttls.get(k, -1)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_dummy_ltf_data():
    """Build a minimal {1h, 2h, 4h} DataFrame dict."""
    import pandas as pd
    idx = pd.date_range("2024-01-01", periods=5, freq="h")
    df = pd.DataFrame(
        {"open": [1.0]*5, "high": [1.1]*5, "low": [0.9]*5,
         "close": [1.05]*5, "volume": [100.0]*5},
        index=idx,
    )
    return {"1h": df, "2h": df.copy(), "4h": df.copy()}


# ---------------------------------------------------------------------------
# Core tests
# ---------------------------------------------------------------------------

class TestLTFCrossWorkerCache:
    """Tests for L1/L2 LTF cache in cache_manager.py."""

    def test_l1_hit_skips_redis(self, monkeypatch):
        """L1 hit → Redis.get never called (fast path)."""
        import cache_manager as cm

        fake_r = _FakeRedis()
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: fake_r)

        data = _make_dummy_ltf_data()
        symbol = "BTC_L1"

        # Populate L1 directly
        cm.set_cached_ltf_data(symbol, data)

        # Track Redis calls
        original_get = fake_r.get
        get_calls = []
        def _tracked_get(k):
            get_calls.append(k)
            return original_get(k)
        fake_r.get = _tracked_get

        result = cm.get_cached_ltf_data(symbol)
        assert result is not None
        # L1 should have served it — no Redis call for the LTF key
        ltf_keys = [k for k in get_calls if "ltf:df:" in k]
        assert ltf_keys == [], f"Redis.get called for L1 hit: {ltf_keys}"

    def test_cross_worker_l2_hit(self, monkeypatch):
        """Worker B reads from L2 Redis without calling the underlying fetch."""
        import cache_manager as cm

        shared_redis = _FakeRedis()
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: shared_redis)

        data = _make_dummy_ltf_data()
        symbol = "BTC_XW"

        # Worker A: populate (sets both L1 and L2)
        cm.set_cached_ltf_data(symbol, data)

        # Simulate Worker B: clear L1 only (same module, different process state)
        with cm._ltf_cache_lock:
            cm._ltf_cache.pop(symbol.upper(), None)

        # Worker B fetch — should hit L2, not call compute

        result = cm.get_cached_ltf_data(symbol)
        assert result is not None, "L2 hit should return data, got None"

        # Verify the DataFrames survived the round-trip
        import pandas as pd
        assert isinstance(result.get("1h"), pd.DataFrame), "1h must be a DataFrame"
        assert len(result["1h"]) == 5

    def test_underlying_fetch_called_only_once(self, monkeypatch):
        """Two 'workers' sharing Redis: only the first triggers the underlying fetch."""
        import cache_manager as cm

        shared_redis = _FakeRedis()
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: shared_redis)

        data = _make_dummy_ltf_data()
        symbol = "ETH_ONCE"
        fetch_count = {"n": 0}

        def _fake_fetch(sym):
            fetch_count["n"] += 1
            return data

        # Worker A: simulate a cache miss → compute → store L1+L2
        with cm._ltf_cache_lock:
            cm._ltf_cache.pop(symbol.upper(), None)
        assert cm.get_cached_ltf_data(symbol) is None  # cold miss

        cm.set_cached_ltf_data(symbol, _fake_fetch(symbol))  # worker A computes

        # Worker B: clear L1, read from L2
        with cm._ltf_cache_lock:
            cm._ltf_cache.pop(symbol.upper(), None)

        result_b = cm.get_cached_ltf_data(symbol)
        assert result_b is not None, "Worker B must read from L2"
        # fetch_count is still 1 (Worker A's fetch) — Worker B did NOT re-fetch
        assert fetch_count["n"] == 1

    def test_redis_down_falls_back_to_compute(self, monkeypatch):
        """Redis down → get_cached_ltf_data returns None (caller recomputes), no crash."""
        import cache_manager as cm

        # Redis is unavailable
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: None)

        # Clear L1 for a clean miss
        symbol = "SOL_NOREDIS"
        with cm._ltf_cache_lock:
            cm._ltf_cache.pop(symbol.upper(), None)

        result = cm.get_cached_ltf_data(symbol)
        # With no L1 and no Redis, should return None (caller will recompute)
        assert result is None

    def test_redis_down_set_still_populates_l1(self, monkeypatch):
        """Redis unavailable during set → L1 is still populated (local worker benefits)."""
        import cache_manager as cm

        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: None)

        symbol = "ADA_L1ONLY"
        data = _make_dummy_ltf_data()
        cm.set_cached_ltf_data(symbol, data)

        # Clear L2 path irrelevant since Redis is None; L1 must have it
        result = cm.get_cached_ltf_data(symbol)
        assert result is not None, "L1 must be populated even when Redis is down"

    def test_corrupt_redis_blob_falls_back(self, monkeypatch):
        """Corrupt pickle blob in Redis → graceful miss (no crash, returns None from L2)."""
        import cache_manager as cm

        fake_r = _FakeRedis()
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: fake_r)

        symbol = "XRP_CORRUPT"
        # Write garbage bytes directly to the L2 key
        key = f"mcd:ltf:df:{symbol.upper()}"
        fake_r.set(key, b"\x00\x01\x02CORRUPT_PICKLE_DATA\xff")

        # L1 is empty
        with cm._ltf_cache_lock:
            cm._ltf_cache.pop(symbol.upper(), None)

        # Must not raise — should return None (caller recomputes)
        result = cm.get_cached_ltf_data(symbol)
        assert result is None, f"Corrupt blob must yield None, got {type(result)}"

    def test_l2_key_format(self, monkeypatch):
        """set_cached_ltf_data writes to 'mcd:ltf:df:{SYMBOL}' in Redis."""
        import cache_manager as cm

        fake_r = _FakeRedis()
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: fake_r)

        symbol = "LINK"
        cm.set_cached_ltf_data(symbol, _make_dummy_ltf_data())

        expected_key = f"mcd:ltf:df:{symbol.upper()}"
        assert expected_key in fake_r.store, (
            f"L2 key {expected_key!r} not found. Keys: {list(fake_r.store.keys())}"
        )

    def test_l2_ttl_matches_l1_ttl(self, monkeypatch):
        """L2 Redis key TTL matches the _LTF_CACHE_TTL constant."""
        import cache_manager as cm

        fake_r = _FakeRedis()
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: fake_r)

        symbol = "DOT"
        cm.set_cached_ltf_data(symbol, _make_dummy_ltf_data())

        key = f"mcd:ltf:df:{symbol.upper()}"
        stored_ttl = fake_r.ttl(key)
        assert stored_ttl == cm._LTF_CACHE_TTL, (
            f"L2 TTL {stored_ttl} != _LTF_CACHE_TTL {cm._LTF_CACHE_TTL}"
        )

    def test_l2_hit_populates_l1(self, monkeypatch):
        """Reading from L2 must also populate L1 so next call is an L1 hit."""
        import cache_manager as cm

        fake_r = _FakeRedis()
        monkeypatch.setattr("cache_manager._get_ltf_redis", lambda: fake_r)

        data = _make_dummy_ltf_data()
        symbol = "AVAX_PROPAGE"

        # Worker A stores → L1 + L2
        cm.set_cached_ltf_data(symbol, data)

        # Simulate cross-worker miss: clear L1
        with cm._ltf_cache_lock:
            cm._ltf_cache.pop(symbol.upper(), None)

        # First read: L2 hit → should re-populate L1
        r1 = cm.get_cached_ltf_data(symbol)
        assert r1 is not None

        # Track Redis calls for the second read
        get_calls = []
        original_get = fake_r.get
        fake_r.get = lambda k: get_calls.append(k) or original_get(k)

        r2 = cm.get_cached_ltf_data(symbol)
        assert r2 is not None
        ltf_gets = [k for k in get_calls if "ltf:df:" in k]
        assert ltf_gets == [], f"Second read should hit L1, not Redis: {ltf_gets}"
