"""The Alt Season loop must not eat the request budget at midnight.

`calculate_alt_season_index` walks ~51 symbols sequentially through
`_get_90d_performance`; every daily crypto cache goes stale at UTC 00:00
at once (`days_old < 1` in database.py), so the first compute after
midnight did ~51 live Binance delta downloads on the request thread —
the 00:02 / 00:12 "Sentiment compute budget exhausted before 'alt_season'"
events in the first weekly error-tracker review.

Two fixes, two tests each:
  * the per-symbol loop is bounded by a wall clock and says so;
  * a 90-day return does not need today's bar, so a cache that is one day
    old is read as-is instead of refetched.
"""

import logging
import time
from datetime import datetime, timedelta

import pandas as pd
import pytest

import data_sources.coinpaprika as cp_mod
from csv_cache import CSVCache


def _universe(n=70):
    return [{'symbol': 'BTC'}] + [{'symbol': f'ALT{i:02d}'} for i in range(n)]


@pytest.fixture
def alt_client(monkeypatch):
    monkeypatch.setattr(cp_mod, 'get_cached_top_coins', _universe)
    client = cp_mod.CryptoMetricsClient.__new__(cp_mod.CryptoMetricsClient)
    client._alt_season_cache = None
    client._alt_season_cache_time = None
    client.ALT_SEASON_CACHE_TTL = 3600
    return client


class TestLoopIsBounded:
    def test_a_slow_universe_stops_at_the_budget_and_says_so(self, alt_client, caplog):
        caplog.set_level(logging.WARNING, logger='carrac.data_sources.coinpaprika')
        alt_client.ALT_SEASON_COMPUTE_BUDGET_S = 0.3
        calls = []

        def slow_perf(symbol):
            calls.append(symbol)
            time.sleep(0.05)
            return 10.0 if symbol == 'BTC' else 20.0

        started = time.monotonic()
        result = alt_client.calculate_alt_season_index(slow_perf)
        elapsed = time.monotonic() - started

        assert elapsed < 1.5, f'loop ran {elapsed:.1f}s against a 0.3s budget'
        assert len(calls) < 51
        assert result['index'] is None, 'fewer than ALT_SEASON_MIN_SAMPLE coins were counted'
        assert 'budget' in result['error']
        assert any('budget' in r.getMessage() for r in caplog.records)

    def test_a_partial_sample_above_the_minimum_is_still_a_reading(self, alt_client):
        alt_client.ALT_SEASON_COMPUTE_BUDGET_S = 0.9
        calls = []

        def slow_perf(symbol):
            calls.append(symbol)
            time.sleep(0.03)
            return 10.0 if symbol == 'BTC' else 20.0

        result = alt_client.calculate_alt_season_index(slow_perf)

        assert result['index'] == 100.0
        assert alt_client.ALT_SEASON_MIN_SAMPLE <= result['total_counted'] < 50
        assert result['total_analyzed'] == result['total_counted'], 'sample size stays auditable'


def _bars(idx):
    close = pd.Series(range(100, 100 + len(idx)), index=idx, dtype=float)
    return pd.DataFrame({'open': close, 'high': close + 1, 'low': close - 1,
                         'close': close, 'volume': 1.0}, index=idx)


@pytest.fixture
def db(tmp_path):
    import database
    inst = database.OHLCVDatabase.__new__(database.OHLCVDatabase)
    inst.cache = CSVCache(str(tmp_path))
    inst._network_reads = []
    inst.get_multi_timeframe_data = lambda symbol: (inst._network_reads.append(symbol)
                                                    or {'1D': pd.DataFrame()})
    return inst


class Test90dPerformanceTolerance:
    def test_a_one_day_old_cache_is_read_without_a_refetch(self, db):
        idx = pd.date_range(end=pd.Timestamp(datetime.now().date()) - timedelta(days=1), periods=120, freq='D')
        db.cache.save('ETH', _bars(idx))

        perf = db._get_90d_performance('ETH')

        assert db._network_reads == [], 'a one-day-old cache was sent back to the network'
        assert perf == pytest.approx((219 - 130) / 130 * 100)

    def test_an_older_cache_still_takes_the_network_path(self, db):
        idx = pd.date_range(end=pd.Timestamp(datetime.now().date()) - timedelta(days=3), periods=120, freq='D')
        db.cache.save('ETH', _bars(idx))

        db._get_90d_performance('ETH')

        assert db._network_reads == ['ETH']
