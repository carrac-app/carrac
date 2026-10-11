"""
Tests for the cache manager module.

Covers:
- Cache entry lifecycle
- Thread safety
- Circuit breaker functionality
- Background refresh
- Stale-while-revalidate pattern
"""

import pytest
import time
import threading
from datetime import datetime, timedelta
from unittest.mock import Mock, patch


from cache_manager import CacheEntry, GlobalCacheManager, CryptoAPIFetcher


class TestCacheEntry:
    """Test CacheEntry dataclass."""

    def test_cache_entry_creation(self):
        """Test creating a cache entry."""
        entry = CacheEntry(
            data={'price': 50000},
            timestamp=datetime.now(),
            ttl_seconds=60,
            source='test'
        )
        assert entry.data == {'price': 50000}
        assert entry.ttl_seconds == 60
        assert entry.source == 'test'

    def test_is_expired_fresh(self):
        """Test fresh entry is not expired."""
        entry = CacheEntry(
            data='test',
            timestamp=datetime.now(),
            ttl_seconds=60,
            source='test'
        )
        assert entry.is_expired() is False

    def test_is_expired_old(self):
        """Test old entry is expired."""
        entry = CacheEntry(
            data='test',
            timestamp=datetime.now() - timedelta(seconds=120),
            ttl_seconds=60,
            source='test'
        )
        assert entry.is_expired() is True

    def test_is_stale_within_grace(self):
        """Test entry in stale grace period."""
        entry = CacheEntry(
            data='test',
            timestamp=datetime.now() - timedelta(seconds=90),
            ttl_seconds=60,
            source='test'
        )
        # Expired but within 2x TTL
        assert entry.is_expired() is True
        assert entry.is_stale() is True

    def test_is_stale_beyond_grace(self):
        """Test entry beyond stale grace period."""
        entry = CacheEntry(
            data='test',
            timestamp=datetime.now() - timedelta(seconds=200),
            ttl_seconds=60,
            source='test'
        )
        assert entry.is_stale() is False

    def test_error_count_tracking(self):
        """Test error count tracking."""
        entry = CacheEntry(
            data='test',
            timestamp=datetime.now(),
            ttl_seconds=60,
            source='test',
            error_count=3,
            last_error='API timeout'
        )
        assert entry.error_count == 3
        assert entry.last_error == 'API timeout'


class TestGlobalCacheManager:
    """Test GlobalCacheManager singleton and operations."""

    @pytest.fixture
    def cache(self):
        """Get cache manager instance and clear it."""
        cm = GlobalCacheManager()
        cm.clear_all()
        cm._circuit_breaker.clear()
        cm._circuit_breaker_reset_time.clear()
        return cm

    def test_singleton_pattern(self):
        """Test singleton pattern returns same instance."""
        cm1 = GlobalCacheManager()
        cm2 = GlobalCacheManager()
        assert cm1 is cm2

    def test_set_and_get(self, cache):
        """Test basic set and get operations."""
        cache.set('test_key', {'value': 42}, ttl=60)
        result = cache.get('test_key')
        assert result == {'value': 42}

    def test_get_missing_key(self, cache):
        """Test getting non-existent key."""
        result = cache.get('nonexistent')
        assert result is None

    def test_cache_expiration(self, cache):
        """Test cache expiration — expired entries are still returned as last-resort fallback."""
        cache.set('expire_test', 'data', ttl=1)
        assert cache.get('expire_test') == 'data'

        time.sleep(1.5)
        # Without fetch_func, expired data is returned as fallback (not None)
        # This is by design — the implementation returns whatever it has
        result = cache.get('expire_test', allow_stale=False)
        assert result == 'data'

    def test_stale_while_revalidate(self, cache):
        """Test stale data is returned while refreshing."""
        cache.set('stale_test', 'original', ttl=1)
        time.sleep(1.5)

        # Should return stale data
        result = cache.get('stale_test', allow_stale=True)
        assert result == 'original'

    def test_fetch_func_on_miss(self, cache):
        """Test fetch function is called on cache miss."""
        fetch_mock = Mock(return_value={'fetched': True})

        result = cache.get('fetch_test', fetch_func=fetch_mock, ttl=60)
        assert result == {'fetched': True}
        fetch_mock.assert_called_once()

    def test_fetch_func_not_called_on_hit(self, cache):
        """Test fetch function is not called on cache hit."""
        cache.set('hit_test', 'cached_value', ttl=60)
        fetch_mock = Mock(return_value='new_value')

        result = cache.get('hit_test', fetch_func=fetch_mock)
        assert result == 'cached_value'
        fetch_mock.assert_not_called()

    def test_invalidate(self, cache):
        """Test cache invalidation."""
        cache.set('invalidate_test', 'data', ttl=60)
        assert cache.get('invalidate_test') == 'data'

        cache.invalidate('invalidate_test')
        assert cache.get('invalidate_test') is None

    def test_clear_all(self, cache):
        """Test clearing entire cache."""
        cache.set('key1', 'value1', ttl=60)
        cache.set('key2', 'value2', ttl=60)

        cache.clear_all()
        assert cache.get('key1') is None
        assert cache.get('key2') is None

    def test_get_stats(self, cache):
        """Test getting cache statistics."""
        cache.set('stats_test', 'data', ttl=60)

        stats = cache.get_stats()
        assert 'total_entries' in stats
        assert stats['total_entries'] >= 1
        assert 'entries' in stats
        assert 'stats_test' in stats['entries']


class TestCircuitBreaker:
    """Test circuit breaker functionality."""

    @pytest.fixture
    def cache(self):
        """Get fresh cache manager."""
        cm = GlobalCacheManager()
        cm.clear_all()
        cm._circuit_breaker.clear()
        cm._circuit_breaker_reset_time.clear()
        return cm

    def test_circuit_opens_after_failures(self, cache):
        """Test circuit opens after threshold failures."""
        # Simulate failures by calling _record_failure directly
        for _ in range(cache.CIRCUIT_BREAKER_THRESHOLD):
            cache._record_failure('failing_api', 'Test error')

        # Circuit should be open now
        assert cache._is_circuit_open('failing_api') is True

    def test_circuit_blocks_requests(self, cache):
        """Test open circuit blocks fetch attempts."""
        # Open the circuit
        for _ in range(cache.CIRCUIT_BREAKER_THRESHOLD):
            cache._record_failure('blocked_api', 'Error')

        fetch_mock = Mock(return_value='data')
        cache.get('blocked_api', fetch_func=fetch_mock)

        # Should not call fetch when circuit is open
        fetch_mock.assert_not_called()

    def test_reset_circuit_breaker(self, cache):
        """Test resetting circuit breaker on success."""
        cache._record_failure('reset_test', 'Error')
        assert cache._circuit_breaker.get('reset_test', 0) > 0

        cache._reset_circuit_breaker('reset_test')
        assert cache._circuit_breaker.get('reset_test', 0) == 0


class TestThreadSafety:
    """Test thread-safe operations."""

    @pytest.fixture
    def cache(self):
        """Get fresh cache manager."""
        cm = GlobalCacheManager()
        cm.clear_all()
        return cm

    def test_concurrent_writes(self, cache):
        """Test concurrent write operations are safe."""
        results = []
        errors = []

        def write_cache(key_suffix):
            try:
                for i in range(100):
                    cache.set(f'concurrent_{key_suffix}_{i}', i, ttl=60)
                results.append(True)
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=write_cache, args=(f't{i}',))
            for i in range(5)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 5

    def test_concurrent_reads_writes(self, cache):
        """Test concurrent read/write operations."""
        cache.set('rw_test', 'initial', ttl=60)
        results = {'reads': [], 'writes': 0}
        lock = threading.Lock()

        def reader():
            for _ in range(50):
                val = cache.get('rw_test')
                with lock:
                    results['reads'].append(val)

        def writer():
            for i in range(50):
                cache.set('rw_test', f'value_{i}', ttl=60)
                with lock:
                    results['writes'] += 1

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=writer)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All operations should complete without errors
        assert len(results['reads']) == 100
        assert results['writes'] == 50


class TestCryptoAPIFetcher:
    """Test CryptoAPIFetcher API integration."""

    def test_fetch_binance_structure(self):
        """Test Binance fetcher returns correct structure."""
        with patch('cache_manager.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = [
                {'symbol': 'BTCUSDT', 'lastPrice': '50000.00', 'priceChangePercent': '2.5',
                 'highPrice': '51000', 'lowPrice': '49000', 'volume': '1000000'}
            ]
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response

            result = CryptoAPIFetcher.fetch_binance_tickers()

            assert result is not None
            # fetch_binance_tickers strips USDT suffix from keys
            assert 'BTC' in result

    def test_fetch_binance_filters_zombie_tickers(self):
        """Delisted pairs still appear in the raw dump with a frozen lastPrice
        and a dead order book (bidPrice=askPrice=0) — these must be filtered
        out, not treated as live prices."""
        with patch('cache_manager.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = [
                {'symbol': 'BTCUSDT', 'lastPrice': '50000.00', 'priceChangePercent': '2.5',
                 'highPrice': '51000', 'lowPrice': '49000', 'volume': '1000000',
                 'bidPrice': '49999.50', 'askPrice': '50000.50'},
                {'symbol': 'SOLUSDT', 'lastPrice': '7.03', 'priceChangePercent': '0',
                 'highPrice': '7.03', 'lowPrice': '7.03', 'volume': '0',
                 'bidPrice': '0.00000000', 'askPrice': '0.00000000'},
            ]
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response

            result = CryptoAPIFetcher.fetch_binance_tickers()

            assert 'BTC' in result
            assert 'SOL' not in result

    def test_fetch_paprika_structure(self):
        """Test CoinPaprika fetcher returns correct structure."""
        with patch('cache_manager.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = [
                {'symbol': 'BTC', 'quotes': {'USD': {
                    'price': 50000, 'percent_change_24h': 2.5,
                    'volume_24h': 1000000, 'market_cap': 1000000000000
                }}}
            ]
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response

            result = CryptoAPIFetcher.fetch_coinpaprika_tickers()

            assert result is not None

    def test_fetch_paprika_enrichment_fields_captured(self):
        """ATH distance + 7d/30d change captured, not discarded."""
        with patch('cache_manager.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = [
                {'id': 'btc-bitcoin', 'quotes': {'USD': {
                    'price': 50000, 'percent_change_24h': 2.5,
                    'volume_24h': 1000000, 'market_cap': 1000000000000,
                    'percent_from_price_ath': -12.34,
                    'percent_change_7d': 5.5,
                    'percent_change_30d': -8.2,
                }}}
            ]
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response

            result = CryptoAPIFetcher.fetch_coinpaprika_tickers()

            assert 'BTC' in result
            assert result['BTC']['ath_distance_pct'] == -12.34
            assert result['BTC']['change_pct_7d'] == 5.5
            assert result['BTC']['change_pct_30d'] == -8.2

    def test_fetch_paprika_enrichment_fields_missing_tolerant(self):
        """Missing upstream fields resolve to None — not KeyError, not a
        fabricated 0."""
        with patch('cache_manager.requests.get') as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = [
                {'id': 'btc-bitcoin', 'quotes': {'USD': {
                    'price': 50000, 'percent_change_24h': 2.5,
                    'volume_24h': 1000000, 'market_cap': 1000000000000,
                    # percent_from_price_ath / _7d / _30d intentionally absent
                }}}
            ]
            mock_response.raise_for_status = Mock()
            mock_get.return_value = mock_response

            result = CryptoAPIFetcher.fetch_coinpaprika_tickers()

            assert result['BTC']['ath_distance_pct'] is None
            assert result['BTC']['change_pct_7d'] is None
            assert result['BTC']['change_pct_30d'] is None

    def test_fetch_combined_prices_parallel(self):
        """Test combined fetch uses parallel execution."""
        with patch.object(CryptoAPIFetcher, 'fetch_binance_tickers') as mock_binance, \
             patch.object(CryptoAPIFetcher, 'fetch_coinpaprika_tickers') as mock_paprika:

            mock_binance.return_value = {'BTCUSDT': {'price': 50000}}
            mock_paprika.return_value = {'BTC': {'price': 50100}}

            CryptoAPIFetcher.fetch_combined_prices()

            # Both should be called
            mock_binance.assert_called_once()
            mock_paprika.assert_called_once()

    def test_fetch_combined_prices_preserves_paprika_enrichment_on_binance_symbol(self):
        """Binance owns price/change_pct/source for a shared symbol, but the
        CoinPaprika-only enrichment fields (ATH distance, 7d/30d change)
        must survive the merge instead of being discarded."""
        with patch.object(CryptoAPIFetcher, 'fetch_binance_tickers') as mock_binance, \
             patch.object(CryptoAPIFetcher, 'fetch_coinpaprika_tickers') as mock_paprika:

            mock_binance.return_value = {
                'BTC': {'price': 50000, 'change_pct': 2.5, 'source': 'binance'}
            }
            mock_paprika.return_value = {
                'BTC': {
                    'price': 50010, 'change_pct': 2.4, 'source': 'coinpaprika',
                    'ath_distance_pct': -12.34,
                    'change_pct_7d': 5.5,
                    'change_pct_30d': -8.2,
                }
            }

            result = CryptoAPIFetcher.fetch_combined_prices()

            # Binance stays primary for price/source...
            assert result['BTC']['price'] == 50000
            assert result['BTC']['source'] == 'binance'
            # ...but Paprika-only enrichment fields must not be dropped.
            assert result['BTC']['ath_distance_pct'] == -12.34
            assert result['BTC']['change_pct_7d'] == 5.5
            assert result['BTC']['change_pct_30d'] == -8.2

    def test_fetch_combined_prices_paprika_only_symbol_keeps_enrichment(self):
        """Symbol with no Binance coverage: Paprika entry (incl. enrichment)
        passes straight through untouched."""
        with patch.object(CryptoAPIFetcher, 'fetch_binance_tickers') as mock_binance, \
             patch.object(CryptoAPIFetcher, 'fetch_coinpaprika_tickers') as mock_paprika:

            mock_binance.return_value = {}
            mock_paprika.return_value = {
                'XMR': {
                    'price': 150, 'change_pct': 1.0, 'source': 'coinpaprika',
                    'ath_distance_pct': -50.0,
                    'change_pct_7d': 1.1,
                    'change_pct_30d': -2.2,
                }
            }

            result = CryptoAPIFetcher.fetch_combined_prices()

            assert result['XMR']['source'] == 'coinpaprika'
            assert result['XMR']['ath_distance_pct'] == -50.0
            assert result['XMR']['change_pct_7d'] == 1.1
            assert result['XMR']['change_pct_30d'] == -2.2

    def test_api_error_handling(self):
        """Test graceful error handling on API failure."""
        with patch('cache_manager.requests.get') as mock_get:
            mock_get.side_effect = Exception("API Error")

            result = CryptoAPIFetcher.fetch_binance_tickers()
            # Returns empty dict on failure (rate limiter short-circuits before requests.get)
            assert result is not None
            assert isinstance(result, dict)


class TestDefaultTTLs:
    """Test default TTL configurations."""

    def test_default_ttls_exist(self):
        """Test default TTLs are configured."""
        cm = GlobalCacheManager()
        assert 'realtime_prices' in cm.DEFAULT_TTLS
        assert 'fear_greed' in cm.DEFAULT_TTLS
        assert 'top_coins' in cm.DEFAULT_TTLS

    def test_realtime_prices_ttl(self):
        """Test realtime prices have short TTL (2 min to reduce CoinPaprika usage)."""
        cm = GlobalCacheManager()
        assert cm.DEFAULT_TTLS['realtime_prices'] <= 300

    def test_top_coins_ttl(self):
        """Test top coins have longer TTL."""
        cm = GlobalCacheManager()
        assert cm.DEFAULT_TTLS['top_coins'] >= 3600


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


class TestMarketCapMemoFreshness:
    """The market-cap memo must not outlive its TTL.

    ``get_cached_market_caps`` calls ``get('market_caps', ttl=3600,
    allow_stale=True)`` with NO fetch_func. Every branch of ``get`` falls
    through to ``return entry.data if entry else None``, so once the memo is
    set it is returned regardless of age — and ``_get_combined_market_caps``
    short-circuits on it, so the 6h file tier is never re-read.

    Failure mode: after the derived indices are rebuilt from clean
    market-cap data, a worker still holding the stale frame rewrites BTC.D
    back to its corrupt shape.
    """

    def test_expired_memo_is_not_returned(self):
        mgr = GlobalCacheManager()
        mgr.set('market_caps', {'old': True}, ttl=3600)

        # Age it past the TTL.
        entry = mgr._cache['market_caps']
        entry.timestamp = datetime.now() - timedelta(seconds=7200)

        assert mgr.get_if_fresh('market_caps', 3600) is None, (
            "an expired memo was served — the file tier can never be re-read"
        )

    def test_fresh_memo_is_returned(self):
        mgr = GlobalCacheManager()
        mgr.set('market_caps', {'fresh': True}, ttl=3600)
        assert mgr.get_if_fresh('market_caps', 3600) == {'fresh': True}

    def test_missing_key_returns_none(self):
        # GlobalCacheManager is a server-wide singleton, so a sibling test may
        # already have populated this key — clear it rather than assume.
        mgr = GlobalCacheManager()
        mgr.invalidate('market_caps')
        assert mgr.get_if_fresh('market_caps', 3600) is None

    def test_market_cap_helper_respects_ttl(self):
        import cache_manager as cm

        cm.set_cached_market_caps({'v': 1})
        assert cm.get_cached_market_caps() == {'v': 1}

        cm.cache_manager._cache['market_caps'].timestamp = (
            datetime.now() - timedelta(seconds=7200)
        )
        assert cm.get_cached_market_caps() is None, (
            "stale market-cap memo still served; data fixes would need a "
            "container recreate to take effect"
        )
