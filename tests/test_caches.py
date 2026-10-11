"""
Tests for carrac.caches — shared TTL cache module.
"""

import time
import threading
from unittest.mock import patch

import pytest

from carrac.caches import (
    get_cached_derivatives, set_cached_derivatives,
    get_cached_vp, set_cached_vp,
    get_cached_chart, set_cached_chart,
    get_cached_subhourly, set_cached_subhourly,
    get_cached_scanner, set_cached_scanner,
    SharedTTLCache, _mark_scanner_stale, _DEFAULT_LOCAL_FAST_TTL,
    _derivatives, _vp, _sentiment, _rs, _regime, _scanner, _chart,
    set_cached_scanner_by_symbol, get_scanner_by_symbol_hash,
    set_htf_by_symbol_hash, get_htf_by_symbol_hash,
    _SCANNER_BY_SYM_KEY, _HTF_BY_SYM_KEY,
)


@pytest.fixture(autouse=True)
def _isolate_from_redis(monkeypatch):
    """Force all SharedTTLCache operations to use the in-memory path.

    A live Redis on this box would intercept get/set before the mocked
    time.time is consulted, making TTL/eviction tests non-hermetic.
    Patching _get_redis to return None forces the fallback local-dict path
    that the tests were written for, regardless of Redis reachability.
    """
    monkeypatch.setattr('carrac.caches._get_redis', lambda: None)


class TestDerivativesCache:
    def test_set_and_get(self):
        set_cached_derivatives('test_key', {'oi': 100})
        result = get_cached_derivatives('test_key')
        assert result == {'oi': 100}

    def test_miss_returns_none(self):
        assert get_cached_derivatives('nonexistent_key') is None

    @patch('carrac.caches.time.time')
    def test_ttl_expiry(self, mock_time):
        mock_time.return_value = 1000.0
        set_cached_derivatives('ttl_test', {'data': 1})

        # Still fresh
        mock_time.return_value = 1200.0  # 200s < 300s TTL
        assert get_cached_derivatives('ttl_test') is not None

        # Expired
        mock_time.return_value = 1400.0  # 400s > 300s TTL
        assert get_cached_derivatives('ttl_test') is None


class TestVPCache:
    def test_set_and_get(self):
        set_cached_vp('BTC:1D', [1, 2, 3])
        assert get_cached_vp('BTC:1D') == [1, 2, 3]

    def test_eviction_on_overflow(self):
        # Fill cache beyond 50 entries with stale data
        with patch('carrac.caches.time.time') as mock_time:
            mock_time.return_value = 1000.0
            for i in range(55):
                set_cached_vp(f'overflow_{i}', i)

            # Now advance time past TTL and add one more to trigger eviction
            mock_time.return_value = 1400.0  # 400s > 300s TTL
            set_cached_vp('trigger_eviction', 'new')

            # Stale entries should be evicted (old ones return None)
            assert get_cached_vp('overflow_0') is None


class TestChartCache:
    """get_cached_chart now returns (payload, is_fresh) or None —
    a TTL-expired entry is a STALE HIT (serve-while-revalidate), not a miss.
    Payloads must be dicts (set_cached_chart stamps `_cached_at` into them)."""

    def test_set_and_get(self):
        set_cached_chart('BTC', '1D', False, {'candles': []})
        payload, is_fresh = get_cached_chart('BTC', '1D', False)
        assert payload['candles'] == []
        assert is_fresh is True

    def test_lite_vs_full_separate(self):
        set_cached_chart('ETH', '4h', True, {'v': 'lite_data'})
        set_cached_chart('ETH', '4h', False, {'v': 'full_data'})
        assert get_cached_chart('ETH', '4h', True)[0]['v'] == 'lite_data'
        assert get_cached_chart('ETH', '4h', False)[0]['v'] == 'full_data'

    @patch('carrac.caches.time.time')
    def test_tf_aware_ttl(self, mock_time):
        # 1D has 180s TTL
        mock_time.return_value = 1000.0
        set_cached_chart('SOL', '1D', False, {'v': 'data'})

        mock_time.return_value = 1170.0  # 170s < 180s
        payload, is_fresh = get_cached_chart('SOL', '1D', False)
        assert payload is not None and is_fresh is True

        mock_time.return_value = 1190.0  # 190s > 180s — stale hit, not a miss
        payload, is_fresh = get_cached_chart('SOL', '1D', False)
        assert payload is not None and is_fresh is False

        # 4h has 120s TTL (120 rather than 90 for warm-cadence headroom)
        mock_time.return_value = 2000.0
        set_cached_chart('BTC', '4h', False, {'v': 'fast_data'})

        mock_time.return_value = 2110.0  # 110s < 120s
        payload, is_fresh = get_cached_chart('BTC', '4h', False)
        assert payload is not None and is_fresh is True

        mock_time.return_value = 2130.0  # 130s > 120s — stale hit
        payload, is_fresh = get_cached_chart('BTC', '4h', False)
        assert payload is not None and is_fresh is False

    @patch('carrac.caches.time.time')
    def test_expired_beyond_retention_returns_none(self, mock_time):
        """Past CHART_SWR_RETENTION_S (1h) even a stale hit gives out — the
        local-dict fallback mirrors Redis's own `ex` expiry so a Redis-down
        worker doesn't serve arbitrarily-ancient data forever."""
        from carrac.caches import CHART_SWR_RETENTION_S

        mock_time.return_value = 1000.0
        set_cached_chart('XRP', '1D', False, {'v': 'old'})

        mock_time.return_value = 1000.0 + CHART_SWR_RETENTION_S + 1
        assert get_cached_chart('XRP', '1D', False) is None

    @patch('carrac.caches.time.time')
    def test_legacy_payload_without_cached_at_is_stale(self, mock_time):
        """A pre-SWR Redis entry (no `_cached_at`) must be treated as stale,
        never fresh — get_cached_chart can't know its true age."""
        import json

        class _FakeRedis:
            def __init__(self):
                self.store = {}

            def get(self, key):
                return self.store.get(key)

            def set(self, key, value, ex=None, nx=None):
                if nx and key in self.store:
                    return None
                self.store[key] = value
                return True

            def delete(self, key):
                self.store.pop(key, None)

        from carrac.caches import _chart, _chart_cache_key

        fake = _FakeRedis()
        mock_time.return_value = 1000.0
        key = _chart._prefix + _chart_cache_key('LEGACYSYM', '1D', False)
        fake.store[key] = json.dumps({'v': 'pre-swr'})

        with patch('carrac.caches._get_redis', lambda: fake):
            payload, is_fresh = get_cached_chart('LEGACYSYM', '1D', False)
        assert payload == {'v': 'pre-swr'}
        assert is_fresh is False

    @patch('carrac.caches.time.time')
    def test_bucketed_write_creates_alias_for_next_bucket(self, mock_time):
        """Intraday keys carry a `:b<epoch>` suffix. The next candle period's
        lookup (a different bucket number, exact key never written) must
        still find the previous payload via the bucket-stripped alias,
        served STALE (not fresh — it may lack the new forming candle)."""
        mock_time.return_value = 5000.0
        set_cached_chart('BTC', '5m:b100', False, {'v': 'bucket100'})

        # Exact bucket: fresh hit.
        payload, is_fresh = get_cached_chart('BTC', '5m:b100', False)
        assert payload['v'] == 'bucket100' and is_fresh is True

        # Next bucket: exact key was never written -> alias fallback, stale.
        payload, is_fresh = get_cached_chart('BTC', '5m:b101', False)
        assert payload['v'] == 'bucket100'
        assert is_fresh is False

    def test_quant_suffix_isolates_variants(self):
        """Quant series must be part of the cache key — a
        quant-augmented payload must never leak into a plain (or
        differently-quant'd) lookup and vice versa."""
        set_cached_chart('BTC', '1D:qA,B', False, {'v': 'quantAB'})
        set_cached_chart('BTC', '1D:qC', False, {'v': 'quantC'})
        set_cached_chart('BTC', '1D', False, {'v': 'noquant'})

        assert get_cached_chart('BTC', '1D:qA,B', False)[0]['v'] == 'quantAB'
        assert get_cached_chart('BTC', '1D:qC', False)[0]['v'] == 'quantC'
        assert get_cached_chart('BTC', '1D', False)[0]['v'] == 'noquant'


class TestSubhourlyCache:
    def test_set_and_get(self):
        import pandas as pd
        df = pd.DataFrame({'close': [100, 101]})
        set_cached_subhourly('BTC', '5m', df)
        result = get_cached_subhourly('BTC', '5m')
        assert result is not None
        assert len(result) == 2

    @patch('carrac.caches.time.time')
    def test_ttl_60s(self, mock_time):
        mock_time.return_value = 1000.0
        set_cached_subhourly('BTC', '1m', 'df_data')

        mock_time.return_value = 1050.0  # 50s < 60s
        assert get_cached_subhourly('BTC', '1m') is not None

        mock_time.return_value = 1070.0  # 70s > 60s
        assert get_cached_subhourly('BTC', '1m') is None


class TestThreadSafety:
    def test_concurrent_writes(self):
        errors = []

        def writer(key_prefix, count):
            try:
                for i in range(count):
                    set_cached_derivatives(f'{key_prefix}_{i}', i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f't{t}', 50)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class _FakeRedisSpy:
    """Minimal redis client stand-in that counts .get() calls and serves
    from an internal dict, so tests can assert the in-process fast path
    avoids Redis entirely once armed."""

    def __init__(self):
        self.store = {}
        self.get_calls = 0

    def get(self, key):
        self.get_calls += 1
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value


class TestSharedTTLCacheFastPath:
    """In-process fast path in front of the Redis round-trip.

    Uses a private SharedTTLCache instance (not one of the module
    singletons) so ttl/local_fast_ttl are fully controlled per test. The
    class-level `_isolate_from_redis` fixture is overridden per-test via a
    fresh monkeypatch of `_get_redis` where a live (fake) Redis is needed.
    """

    def test_fast_path_serves_without_redis_call(self, monkeypatch):
        import json as _json

        fake = _FakeRedisSpy()
        fake.store['mcd:c:t1:k'] = _json.dumps({'v': 1})
        cache = SharedTTLCache('t1', ttl=300, local_fast_ttl=5)
        monkeypatch.setattr('carrac.caches._get_redis', lambda: fake)

        first = cache.get('k')
        assert first == {'v': 1}
        calls_after_first = fake.get_calls
        assert calls_after_first > 0  # first call had to consult Redis

        second = cache.get('k')
        assert second == {'v': 1}
        assert fake.get_calls == calls_after_first  # no NEW Redis call

    @patch('carrac.caches.time.time')
    def test_expiry_falls_through_to_redis_and_rearms(self, mock_time, monkeypatch):
        import json as _json

        fake = _FakeRedisSpy()
        fake.store['mcd:c:t2:k'] = _json.dumps({'v': 'redis-value'})
        cache = SharedTTLCache('t2', ttl=300, local_fast_ttl=5)
        monkeypatch.setattr('carrac.caches._get_redis', lambda: fake)

        mock_time.return_value = 1000.0
        assert cache.get('k') == {'v': 'redis-value'}
        calls = fake.get_calls

        # Still inside the 5s fast-path window -> no new Redis call.
        mock_time.return_value = 1003.0
        assert cache.get('k') == {'v': 'redis-value'}
        assert fake.get_calls == calls

        # Past local_fast_ttl -> falls through to Redis; another worker may
        # have written a newer value in the meantime.
        mock_time.return_value = 1006.0
        fake.store['mcd:c:t2:k'] = _json.dumps({'v': 'redis-value-2'})
        assert cache.get('k') == {'v': 'redis-value-2'}
        assert fake.get_calls > calls

        # Re-armed by the read above -> the immediate next call skips Redis.
        calls2 = fake.get_calls
        mock_time.return_value = 1006.5
        assert cache.get('k') == {'v': 'redis-value-2'}
        assert fake.get_calls == calls2

    def test_redis_error_falls_back_to_local(self, monkeypatch):
        """Redis raising mid-call still serves the (slower self._ttl-bound)
        local copy — the pre-fast-path fallback, unchanged."""

        class _BoomRedis:
            def get(self, key):
                raise ConnectionError('redis down')

        cache = SharedTTLCache('t3', ttl=300, local_fast_ttl=5)
        # Seed the local dict as if written well outside the fast-path
        # window, so get() is forced to actually try Redis.
        cache._local['k'] = (time.time() - 10, {'v': 'stale-but-local'})
        monkeypatch.setattr('carrac.caches._get_redis', lambda: _BoomRedis())

        assert cache.get('k') == {'v': 'stale-but-local'}

    @patch('carrac.caches.time.time')
    def test_write_back_rearms_local_from_redis(self, mock_time, monkeypatch):
        import json as _json

        fake = _FakeRedisSpy()
        fake.store['mcd:c:t4:k'] = _json.dumps({'v': 'from-redis'})
        cache = SharedTTLCache('t4', ttl=300, local_fast_ttl=5)
        monkeypatch.setattr('carrac.caches._get_redis', lambda: fake)

        mock_time.return_value = 3000.0
        assert 'k' not in cache._local
        result = cache.get('k')
        assert result == {'v': 'from-redis'}
        assert cache._local['k'] == (3000.0, {'v': 'from-redis'})


class TestPerInstanceFastTTL:
    """Per-instance local_fast_ttl tuning table."""

    def test_default_is_5s(self):
        assert _DEFAULT_LOCAL_FAST_TTL == 5

    def test_scanner_overridden_to_10s(self):
        """The 10.8MB blob rewritten ~every 30min gets a wider window —
        cross-worker staleness of 10s is negligible against that cadence."""
        assert _scanner._local_fast_ttl == 10

    def test_other_instances_keep_the_default(self):
        for cache in (_derivatives, _vp, _sentiment, _rs, _regime, _chart):
            assert cache._local_fast_ttl == _DEFAULT_LOCAL_FAST_TTL


class TestScannerCacheMutationSafety:
    """carrac/scanner/routes.py's
    `direction_filter` branch (GET /api/scanner) temporarily sets
    `_filter_score`/`_filter_verdict` on each result dict, sorts, then pops
    them before building the response. Before the fast path every get_cached_scanner()
    call re-parsed the payload fresh from Redis, so that mutate-then-pop was
    always private to one request. With the fast path able to hand the SAME
    object graph to concurrent/sequential requests on a worker, that
    consumer (a file we don't own) must never see its mutation observed by
    -- or leak from -- another reader. get_cached_scanner() and
    _mark_scanner_stale() both return defensive copies to guarantee this.
    """

    def test_get_cached_scanner_returns_independent_copy(self):
        set_cached_scanner({
            'results': [{'symbol': 'BTC', 'price': 100}],
            'macro': {'x': 1},
            'computed_at': 't0',
        })

        first = get_cached_scanner()
        # Simulate scanner/routes.py's in-place mutation.
        first['results'][0]['_filter_score'] = 999
        first['extra_top_level'] = True

        second = get_cached_scanner()
        assert '_filter_score' not in second['results'][0]
        assert 'extra_top_level' not in second
        assert second['results'][0]['symbol'] == 'BTC'

    def test_mark_scanner_stale_copies_results(self):
        data = {'results': [{'symbol': 'SOL'}], 'macro': {}}
        out = _mark_scanner_stale(data, time.time())

        out['results'][0]['poison'] = True

        assert 'poison' not in data['results'][0]
        assert out['_stale'] is True


class _FakeRedisHashPipeline:
    """Queues delete/hset/expire ops and applies them to the parent's
    `hashes` store on execute() — enough to exercise
    `_set_by_symbol_hash`'s DELETE+HSET+EXPIRE pipeline."""

    def __init__(self, parent):
        self._parent = parent
        self._ops = []

    def delete(self, key):
        self._ops.append(('delete', key))
        return self

    def hset(self, key, mapping=None):
        self._ops.append(('hset', key, dict(mapping or {})))
        return self

    def expire(self, key, ttl):
        self._ops.append(('expire', key, ttl))
        return self

    def exists(self, key):
        # The reader pipelines EXISTS+HMGET to tell "hash missing"
        # (fallback) from "hash exists, symbols absent" (trust it).
        self._ops.append(('exists', key))
        return self

    def hmget(self, key, fields):
        self._ops.append(('hmget', key, list(fields)))
        return self

    def execute(self):
        results = []
        for op in self._ops:
            if op[0] == 'delete':
                self._parent.hashes.pop(op[1], None)
                results.append(1)
            elif op[0] == 'hset':
                key, mapping = op[1], op[2]
                h = self._parent.hashes.setdefault(key, {})
                for k, v in mapping.items():
                    kb = k.encode() if isinstance(k, str) else k
                    vb = v.encode() if isinstance(v, str) else v
                    h[kb] = vb
                results.append(len(mapping))
            elif op[0] == 'exists':
                results.append(1 if op[1] in self._parent.hashes else 0)
            elif op[0] == 'hmget':
                results.append(self._parent.hmget(op[1], op[2]))
            else:
                results.append(True)  # 'expire' — TTL isn't modeled
        self._ops = []
        return results


class _FakeRedisHash:
    """Minimal redis client stand-in for the per-symbol hash
    mirror: supports the pipeline (DELETE+HSET+EXPIRE) write path and
    HMGET reads. `decode_responses=False` in the real client means stored
    values come back as bytes — this fake matches that."""

    def __init__(self):
        self.hashes = {}  # hash_key -> {field_bytes: value_bytes}
        self.hmget_calls = 0

    def pipeline(self):
        return _FakeRedisHashPipeline(self)

    def hmget(self, key, fields):
        self.hmget_calls += 1
        h = self.hashes.get(key, {})
        out = []
        for f in fields:
            fb = f.encode() if isinstance(f, str) else f
            out.append(h.get(fb))
        return out


class TestScannerBySymbolHash:
    """Writer (set_cached_scanner_by_symbol / set_htf_by_symbol_hash)
    and reader (get_scanner_by_symbol_hash / get_htf_by_symbol_hash) for the
    per-symbol Redis hash mirrors."""

    def test_set_cached_scanner_writes_by_symbol_hash(self, monkeypatch):
        fake = _FakeRedisHash()
        monkeypatch.setattr('carrac.caches._get_redis', lambda: fake)

        set_cached_scanner_by_symbol({
            'results': [
                {'symbol': 'BTC', 'long': {'total': 8}},
                {'symbol': 'ETH', 'long': {'total': 5}},
            ],
        })

        result = get_scanner_by_symbol_hash(['BTC', 'ETH'])
        assert result == {
            'BTC': {'symbol': 'BTC', 'long': {'total': 8}},
            'ETH': {'symbol': 'ETH', 'long': {'total': 5}},
        }

    def test_set_cached_scanner_calls_by_symbol_write(self, monkeypatch):
        """set_cached_scanner (the real public writer) mirrors into the hash
        in the SAME call — writer + hash can never diverge for long."""
        fake = _FakeRedisHash()
        monkeypatch.setattr('carrac.caches._get_redis', lambda: fake)

        set_cached_scanner({'results': [{'symbol': 'SOL', 'best_verdict': 'STRONG'}]})

        assert get_scanner_by_symbol_hash(['SOL']) == {
            'SOL': {'symbol': 'SOL', 'best_verdict': 'STRONG'},
        }

    def test_set_htf_by_symbol_hash_writes_and_reads(self, monkeypatch):
        fake = _FakeRedisHash()
        monkeypatch.setattr('carrac.caches._get_redis', lambda: fake)

        set_htf_by_symbol_hash({
            'assets': [
                {'symbol': 'BTC', 'latest_price': 80000.0},
                {'symbol': 'ETH', 'latest_price': 4000.0},
            ],
        })

        result = get_htf_by_symbol_hash(['BTC', 'ETH', 'XRP'])
        assert result == {
            'BTC': {'symbol': 'BTC', 'latest_price': 80000.0},
            'ETH': {'symbol': 'ETH', 'latest_price': 4000.0},
        }
        # XRP was never written — HMGET returns None for it, so it's simply
        # absent from the result (not an error, not a zero-value entry).
        assert 'XRP' not in result

    def test_shrinking_universe_drops_stale_fields(self, monkeypatch):
        """A symbol dropped from one pass to the next must not linger in
        the hash — _set_by_symbol_hash DELETEs before HSET-ing the new set."""
        fake = _FakeRedisHash()
        monkeypatch.setattr('carrac.caches._get_redis', lambda: fake)

        set_cached_scanner_by_symbol({'results': [
            {'symbol': 'BTC'}, {'symbol': 'DELISTED'},
        ]})
        assert get_scanner_by_symbol_hash(['DELISTED']) == {'DELISTED': {'symbol': 'DELISTED'}}

        set_cached_scanner_by_symbol({'results': [{'symbol': 'BTC'}]})
        assert get_scanner_by_symbol_hash(['DELISTED']) == {}

    def test_get_returns_empty_when_hash_absent(self, monkeypatch):
        """No writer has run yet (or Redis is down): HMGET-equivalent finds
        nothing -> callers get None (hash unavailable) and fall back to the
        blob. An EXISTING hash with absent symbols returns {} and
        is trusted instead."""
        fake = _FakeRedisHash()
        monkeypatch.setattr('carrac.caches._get_redis', lambda: fake)

        assert get_scanner_by_symbol_hash(['BTC']) is None
        # populated hash, symbol legitimately absent -> {} (trusted, no fallback)
        fake.hashes['mcd:c:scanner:by_sym'] = {b'ETH': b'{}'}
        fake.hashes['mcd:response:htf:by_sym'] = {b'ETH': b'{}'}
        assert get_scanner_by_symbol_hash(['BTC']) == {}
        assert get_htf_by_symbol_hash(['BTC']) == {}

    def test_no_redis_is_a_noop_not_an_error(self, monkeypatch):
        """_isolate_from_redis (autouse) already forces _get_redis -> None;
        writers/readers must degrade silently, never raise."""
        set_cached_scanner_by_symbol({'results': [{'symbol': 'BTC'}]})
        set_htf_by_symbol_hash({'assets': [{'symbol': 'BTC'}]})
        # no Redis -> hash unavailable -> None (fallback signal), never a raise
        assert get_scanner_by_symbol_hash(['BTC']) is None
        assert get_htf_by_symbol_hash(['BTC']) is None

    def test_hash_keys_match_documented_names(self):
        """The Redis key names are part of the per-symbol contract (docs
        and reports reference these literal strings)."""
        assert _SCANNER_BY_SYM_KEY == 'mcd:c:scanner:by_sym'
        assert _HTF_BY_SYM_KEY == 'mcd:response:htf:by_sym'
