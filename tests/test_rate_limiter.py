"""
Unit tests for api_rate_limiter.py.

Covers: can_call/record_call, persistence, multi-tier providers,
usage stats, soft limits, reset, thread safety.
"""

import pytest
import threading


@pytest.fixture
def limiter(tmp_path, monkeypatch):
    """Create a fresh APIRateLimiter with temp storage."""
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    # Reset module-level singleton
    import api_rate_limiter as rl
    rl._instance = None
    limiter = rl.APIRateLimiter(cache_dir=tmp_path)
    yield limiter
    # Stop background flush thread
    limiter._running = False


class TestCanCall:
    def test_unknown_provider_allowed(self, limiter):
        allowed, msg = limiter.can_call('nonexistent_provider')
        assert allowed is True
        assert 'Unknown' in msg or 'allow' in msg.lower()

    def test_fresh_provider_allowed(self, limiter):
        allowed, msg = limiter.can_call('binance')
        assert allowed is True

    def test_blocked_after_limit(self, limiter):
        # Binance has a per-minute limit — fill it up
        from api_rate_limiter import APIRateLimiter
        RL = APIRateLimiter.RATE_LIMITS
        binance_limits = {k: v for k, v in RL.items() if k.startswith('binance')}
        # Find the smallest window limit
        min_key = min(binance_limits, key=lambda k: binance_limits[k]['calls_per_window'])
        limit = RL[min_key]['calls_per_window']

        for _ in range(limit):
            limiter.record_call(min_key)

        allowed, msg = limiter.can_call(min_key)
        assert allowed is False
        assert 'limit' in msg.lower() or str(limit) in msg

    def test_record_increments_count(self, limiter):
        limiter.record_call('stooq')
        limiter.record_call('stooq')
        limiter.record_call('stooq')

        stats = limiter.get_usage_stats()
        assert stats['stooq']['current'] == 3


class TestPersistence:
    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        import api_rate_limiter as rl
        rl._instance = None

        # Create, record calls, save
        limiter1 = rl.APIRateLimiter(cache_dir=tmp_path)
        limiter1.record_call('stooq', count=5)
        limiter1._save_state()

        # Create new instance — should load from disk
        limiter2 = rl.APIRateLimiter(cache_dir=tmp_path)
        stats = limiter2.get_usage_stats()
        assert stats['stooq']['current'] == 5

        limiter1._running = False
        limiter2._running = False

    def test_persistence_file_exists(self, limiter, tmp_path):
        limiter.record_call('binance')
        limiter._save_state()

        assert (tmp_path / 'api_rate_limits.json').exists()

    def test_corrupt_file_handled(self, tmp_path, monkeypatch):
        """Corrupt persistence file should not crash init."""
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        (tmp_path / 'api_rate_limits.json').write_text('NOT JSON{{{')

        import api_rate_limiter as rl
        rl._instance = None
        limiter = rl.APIRateLimiter(cache_dir=tmp_path)
        # Should start fresh, not crash
        allowed, _ = limiter.can_call('binance')
        assert allowed is True
        limiter._running = False


class TestUsageStats:
    def test_all_providers_present(self, limiter):
        stats = limiter.get_usage_stats()
        # Should have at least the main providers
        for provider in ('binance', 'stooq', 'coinpaprika', 'yahoo'):
            assert provider in stats, f"Missing provider: {provider}"

    def test_stats_structure(self, limiter):
        limiter.record_call('binance', count=10)
        stats = limiter.get_usage_stats()
        s = stats['binance']

        assert 'current' in s
        assert 'limit' in s
        assert 'pct_used' in s
        assert 'status' in s
        assert s['current'] == 10
        assert s['status'] in ('ok', 'warning', 'blocked')

    def test_pct_used_calculation(self, limiter):
        from api_rate_limiter import APIRateLimiter
        limit = APIRateLimiter.RATE_LIMITS['stooq']['calls_per_window']
        limiter.record_call('stooq', count=limit // 2)

        stats = limiter.get_usage_stats()
        pct = stats['stooq']['pct_used']
        assert 45 <= pct <= 55  # ~50%


class TestThreadSafety:
    def test_concurrent_record_calls(self, limiter):
        """Concurrent calls should not lose counts."""
        n_threads = 10
        calls_per_thread = 50
        errors = []

        def worker():
            try:
                for _ in range(calls_per_thread):
                    limiter.record_call('stooq')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        stats = limiter.get_usage_stats()
        assert stats['stooq']['current'] == n_threads * calls_per_thread
