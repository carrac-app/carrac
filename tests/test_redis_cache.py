"""
Tests for the redis_cache module.

Covers:
- ResponseCache in-memory fallback (no Redis)
- ResponseCache TTL / expiration
- ResponseCache thread safety
- ResponseCache computing flag
- numpy serialization helper
"""

import pytest
import time
import threading
import json
from datetime import datetime, timedelta

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force no Redis for unit tests
import carrac.redis_cache as redis_cache_mod
redis_cache_mod._redis_checked = True
redis_cache_mod._redis_client = None

from carrac.redis_cache import ResponseCache, _numpy_serializer


class TestResponseCacheMemory:
    """Test ResponseCache with in-memory backend (no Redis)."""

    @pytest.fixture
    def cache(self):
        """Fresh ResponseCache instance."""
        c = ResponseCache()
        c._cache.clear()
        c._timestamps.clear()
        c._computing = {'htf': False, 'ltf': False}
        return c

    def test_set_and_get(self, cache):
        """Test basic set/get cycle."""
        cache.set('htf', {'assets': ['BTC'], 'count': 1})
        result = cache.get('htf')
        assert result is not None
        assert result['assets'] == ['BTC']
        assert result['count'] == 1

    def test_get_returns_none_on_miss(self, cache):
        """Test get returns None for missing key."""
        result = cache.get('htf')
        assert result is None

    def test_cached_metadata_added(self, cache):
        """Test _cached and _cache_age are added to responses."""
        cache.set('htf', {'data': True})
        result = cache.get('htf')
        assert result['_cached'] is True
        assert '_cache_age' in result
        assert result['_cache_age'] >= 0

    def test_ttl_expiration(self, cache):
        """Test entries expire after TTL."""
        cache.TTL_SECONDS = 1
        cache.set('htf', {'data': True})
        assert cache.get('htf') is not None

        time.sleep(1.5)
        assert cache.get('htf') is None

    def test_set_overwrites(self, cache):
        """Test setting same key overwrites previous value."""
        cache.set('htf', {'version': 1})
        cache.set('htf', {'version': 2})
        result = cache.get('htf')
        assert result['version'] == 2

    def test_independent_modes(self, cache):
        """Test htf and ltf are independent."""
        cache.set('htf', {'mode': 'htf'})
        cache.set('ltf', {'mode': 'ltf'})
        assert cache.get('htf')['mode'] == 'htf'
        assert cache.get('ltf')['mode'] == 'ltf'

    def test_get_returns_shallow_copy(self, cache):
        """Test get returns a shallow copy (top-level keys independent)."""
        cache.set('htf', {'count': 1})
        result = cache.get('htf')
        result['count'] = 999
        result2 = cache.get('htf')
        assert result2['count'] == 1  # Top-level keys are independent


class TestResponseCacheComputing:
    """Test computing flag management."""

    @pytest.fixture
    def cache(self):
        c = ResponseCache()
        c._computing = {'htf': False, 'ltf': False}
        return c

    def test_is_computing_default_false(self, cache):
        assert cache.is_computing('htf') is False
        assert cache.is_computing('ltf') is False

    def test_set_computing(self, cache):
        cache.set_computing('htf', True)
        assert cache.is_computing('htf') is True
        assert cache.is_computing('ltf') is False

    def test_set_computing_false(self, cache):
        cache.set_computing('htf', True)
        cache.set_computing('htf', False)
        assert cache.is_computing('htf') is False

    def test_is_computing_unknown_mode(self, cache):
        """Unknown mode returns False (dict.get default)."""
        assert cache.is_computing('unknown') is False


class TestResponseCacheStats:
    """Test cache statistics."""

    @pytest.fixture
    def cache(self):
        c = ResponseCache()
        c._cache.clear()
        c._timestamps.clear()
        return c

    def test_stats_empty(self, cache):
        stats = cache.get_stats()
        assert stats['htf']['cached'] is False
        assert stats['ltf']['cached'] is False
        assert stats['htf']['backend'] == 'memory'

    def test_stats_with_data(self, cache):
        cache.set('htf', {'assets': ['BTC', 'ETH']})
        stats = cache.get_stats()
        assert stats['htf']['cached'] is True
        assert stats['htf']['asset_count'] == 2
        assert stats['htf']['fresh'] is True
        assert stats['htf']['backend'] == 'memory'

    def test_stats_expired(self, cache):
        cache.TTL_SECONDS = 1
        cache.set('htf', {'assets': []})
        time.sleep(1.5)
        stats = cache.get_stats()
        assert stats['htf']['cached'] is True  # Still in stats
        assert stats['htf']['fresh'] is False


class TestResponseCacheThreadSafety:
    """Test thread-safe operations."""

    @pytest.fixture
    def cache(self):
        c = ResponseCache()
        c._cache.clear()
        c._timestamps.clear()
        return c

    def test_concurrent_set_get(self, cache):
        """Test concurrent set/get doesn't crash."""
        errors = []

        def writer():
            try:
                for i in range(100):
                    cache.set('htf', {'i': i})
            except Exception as e:
                errors.append(str(e))

        def reader():
            try:
                for _ in range(100):
                    cache.get('htf')
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class _FakeRedisResponseSpy:
    """Minimal redis client stand-in supporting the get/set/pipeline calls
    ResponseCache._redis_get / _redis_set actually use, counting .get()
    calls so tests can assert the in-process fast path skips Redis once
    armed."""

    def __init__(self):
        self.store = {}
        self.get_calls = 0

    def get(self, key):
        self.get_calls += 1
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value.encode('utf-8') if isinstance(value, str) else value

    def pipeline(self):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, parent):
        self._parent = parent
        self._ops = []

    def set(self, key, value, ex=None):
        self._ops.append((key, value))

    def execute(self):
        for key, value in self._ops:
            self._parent.set(key, value)


class TestResponseCacheFastPath:
    """In-process fast path in front of the Redis round-trip
    (the 2.1MB `htf` blob, rewritten every REFRESH_INTERVAL=1800s)."""

    @pytest.fixture
    def cache(self):
        c = ResponseCache()
        c._cache.clear()
        c._timestamps.clear()
        c._synced_at.clear()
        return c

    def test_fast_path_serves_without_redis_call(self, cache, monkeypatch):
        fake = _FakeRedisResponseSpy()
        cache._redis_set(fake, 'htf', {'assets': ['BTC']})
        monkeypatch.setattr(cache, '_redis', lambda: fake)

        first = cache.get('htf')
        assert first['assets'] == ['BTC']
        calls_after_first = fake.get_calls
        assert calls_after_first > 0  # first call had to consult Redis

        second = cache.get('htf')
        assert second['assets'] == ['BTC']
        assert fake.get_calls == calls_after_first  # no NEW Redis call

    def test_expiry_falls_through_to_redis_and_rearms(self, cache, monkeypatch):
        cache._local_fast_ttl = 0.2  # tight window for a real-clock test
        fake = _FakeRedisResponseSpy()
        cache._redis_set(fake, 'htf', {'v': 1})
        monkeypatch.setattr(cache, '_redis', lambda: fake)

        assert cache.get('htf')['v'] == 1
        calls = fake.get_calls
        assert calls > 0

        time.sleep(0.35)  # past local_fast_ttl
        cache._redis_set(fake, 'htf', {'v': 2})  # another worker wrote a newer value
        result = cache.get('htf')
        assert result['v'] == 2
        assert fake.get_calls > calls  # Redis was consulted again

        # Re-armed by the read above -> the immediate next call skips Redis.
        calls2 = fake.get_calls
        assert cache.get('htf')['v'] == 2
        assert fake.get_calls == calls2

    def test_redis_error_falls_back_to_local(self, cache, monkeypatch):
        """A prior local write survives a Redis outage on the next get()."""
        cache.set('htf', {'v': 'local-value'})  # no Redis in this test module -> memory-only

        class _BoomRedis:
            def get(self, key):
                raise ConnectionError('redis down')

        # Force past the fast-path window so get() actually reaches Redis.
        cache._synced_at['htf'] = datetime.now() - timedelta(seconds=cache._local_fast_ttl + 1)
        monkeypatch.setattr(cache, '_redis', lambda: _BoomRedis())

        result = cache.get('htf')
        assert result['v'] == 'local-value'

    def test_write_back_rearms_from_redis(self, cache, monkeypatch):
        fake = _FakeRedisResponseSpy()
        cache._redis_set(fake, 'htf', {'v': 'from-redis'})
        monkeypatch.setattr(cache, '_redis', lambda: fake)

        assert 'htf' not in cache._cache
        cache.get('htf')
        assert cache._cache['htf'] == {'v': 'from-redis'}
        assert 'htf' in cache._synced_at

    def test_fast_path_returns_copy_not_shared_object(self, cache, monkeypatch):
        """carrac/assets/routes.py mutates the dict get() returns in place
        (`cached_data['fetch_stats'] = ...`) — a file we don't own. The
        fast path must never hand out the object backing self._cache, or
        that mutation would leak into the next reader for up to
        local_fast_ttl seconds."""
        fake = _FakeRedisResponseSpy()
        cache._redis_set(fake, 'htf', {'assets': ['BTC']})
        monkeypatch.setattr(cache, '_redis', lambda: fake)

        first = cache.get('htf')
        first['fetch_stats'] = 'poisoned'  # simulate assets/routes.py mutation

        second = cache.get('htf')  # served by the fast path (no new Redis call)
        assert 'fetch_stats' not in second


class TestNumpySerializer:
    """Test the numpy JSON serializer."""

    def test_numpy_int(self):
        import numpy as np
        result = _numpy_serializer(np.int64(42))
        assert result == 42
        assert isinstance(result, int)

    def test_numpy_float(self):
        import numpy as np
        result = _numpy_serializer(np.float64(3.14))
        assert abs(result - 3.14) < 0.001
        assert isinstance(result, float)

    def test_numpy_bool(self):
        import numpy as np
        result = _numpy_serializer(np.bool_(True))
        assert result is True

    def test_numpy_array(self):
        import numpy as np
        result = _numpy_serializer(np.array([1, 2, 3]))
        assert result == [1, 2, 3]
        assert isinstance(result, list)

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            _numpy_serializer(set([1, 2]))

    def test_json_dumps_with_serializer(self):
        """Test the serializer works with json.dumps."""
        import numpy as np
        data = {'price': np.float64(50000.0), 'count': np.int64(10)}
        result = json.dumps(data, default=_numpy_serializer)
        parsed = json.loads(result)
        assert parsed['price'] == 50000.0
        assert parsed['count'] == 10


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
