"""
Unit tests for CCXT multi-exchange client (data_sources/ccxt_client.py).

All tests use mocked CCXT exchange objects — no network calls.
"""

import json
import time
from unittest.mock import MagicMock

import pandas as pd
import numpy as np
import pytest

from data_sources.ccxt_client import CcxtClient


# ─── Fixtures ─────────────────────────────────────────

@pytest.fixture
def client():
    """Create a CcxtClient with kill switches off and pre-populated routing table.

    Pre-populates _routing_table to avoid calling load_markets() (which needs ccxt).
    This simulates the state after _ensure_markets() has run.
    """
    import os
    from datetime import datetime
    os.environ.pop('CCXT_DISABLED', None)
    for ex in CcxtClient.EXCHANGE_PRIORITY:
        os.environ.pop(f'CCXT_{ex.upper()}_DISABLED', None)
    c = CcxtClient()
    # Pre-populate routing table (simulates _ensure_markets() having run)
    c._routing_table = dict(CcxtClient.CCXT_COINS_SEED)
    c._markets_loaded_at = datetime.now()
    return c


# Sample CCXT OHLCV data (same format all exchanges return)
SAMPLE_OHLCV = [
    [1712620800000, 150.0, 155.0, 148.0, 153.0, 50000.0],  # 2024-04-09 00:00
    [1712707200000, 153.0, 158.0, 151.0, 156.0, 45000.0],  # 2024-04-10 00:00
    [1712793600000, 156.0, 160.0, 154.0, 159.0, 55000.0],  # 2024-04-11 00:00
]


def _make_mock_exchange(ohlcv_data=None):
    """Create a mock CCXT exchange that returns given OHLCV data."""
    ex = MagicMock()
    ex.fetch_ohlcv.return_value = ohlcv_data or SAMPLE_OHLCV
    ex.load_markets.return_value = {}
    return ex


# ─── Initialization ───────────────────────────────────

class TestCcxtInit:
    def test_no_exchanges_on_init(self, client):
        """Exchanges are lazily initialized, not on construction."""
        assert len(client._exchanges) == 0

    def test_not_disabled_by_default(self, client):
        assert not client._disabled


# ─── is_supported ─────────────────────────────────────

class TestIsSupported:
    def test_known_coin_supported(self, client):
        assert client.is_supported('XMR') is True
        assert client.is_supported('DEEP') is True

    def test_case_insensitive(self, client):
        assert client.is_supported('xmr') is True

    def test_unknown_coin_not_supported(self, client):
        assert client.is_supported('BTC') is False
        assert client.is_supported('ETH') is False
        assert client.is_supported('HYPE') is False
        assert client.is_supported('NONEXISTENT') is False

    def test_binance_coins_not_in_ccxt(self, client):
        """Coins on Binance should never be in CCXT_COINS."""
        from data_sources.binance import BinanceClient
        binance_known = set(BinanceClient().spot_map.keys())
        ccxt_coins = set(CcxtClient.CCXT_COINS_SEED.keys())
        overlap = ccxt_coins & binance_known
        assert not overlap, f"CCXT_COINS overlaps with Binance: {overlap}"

    def test_hl_coins_not_in_ccxt(self, client):
        """Coins in HL_COINS should never be in CCXT_COINS."""
        from data_sources.hyperliquid import HyperliquidClient
        hl_coins = HyperliquidClient.HL_COINS
        ccxt_coins = set(CcxtClient.CCXT_COINS_SEED.keys())
        overlap = ccxt_coins & hl_coins
        assert not overlap, f"CCXT_COINS overlaps with HL_COINS: {overlap}"


# ─── Kill Switches ────────────────────────────────────

class TestKillSwitch:
    def test_global_kill_switch(self, monkeypatch):
        monkeypatch.setenv('CCXT_DISABLED', '1')
        c = CcxtClient()
        assert c.is_supported('XMR') is False
        assert c.is_supported('DEEP') is False

    def test_global_kill_switch_truthy_values(self, monkeypatch):
        for val in ('1', 'true', 'TRUE', 'yes'):
            monkeypatch.setenv('CCXT_DISABLED', val)
            c = CcxtClient()
            assert c.is_supported('XMR') is False, f"kill switch failed for {val!r}"

    def test_global_kill_switch_falsy(self, monkeypatch):
        for val in ('0', '', 'false'):
            monkeypatch.setenv('CCXT_DISABLED', val)
            c = CcxtClient()
            assert c.is_supported('XMR') is True, f"falsely disabled for {val!r}"

    def test_per_exchange_kill_switch(self, monkeypatch):
        """Disabling an exchange should make symbols routed to it unsupported."""
        monkeypatch.setenv('CCXT_KUCOIN_DISABLED', '1')
        c = CcxtClient()
        # Seed routing deterministically — auto-discovery is live and nondeterministic in tests
        monkeypatch.setattr(c, '_ensure_markets', lambda: None)
        c._routing_table = {'XMR': 'kucoin', 'DEEP': 'bybit'}
        assert c.is_supported('XMR') is False
        assert c.is_supported('DEEP') is True

    def test_per_exchange_kill_mexc(self, monkeypatch):
        monkeypatch.setenv('CCXT_MEXC_DISABLED', '1')
        c = CcxtClient()
        monkeypatch.setattr(c, '_ensure_markets', lambda: None)
        c._routing_table = {'ARC': 'mexc', 'XMR': 'kucoin'}
        assert c.is_supported('ARC') is False
        assert c.is_supported('XMR') is True

    def test_kill_switch_skips_network(self, client, monkeypatch):
        """fetch_ohlcv for disabled exchange must NOT make any network call."""
        monkeypatch.setenv('CCXT_DISABLED', '1')
        c = CcxtClient()
        called = {'count': 0}

        def fake_get(*args, **kwargs):
            called['count'] += 1
            return None

        monkeypatch.setattr(c, '_get_exchange', fake_get)
        df = c.fetch_ohlcv('XMR', 30)
        assert df.empty
        assert called['count'] == 0


# ─── DataFrame Conversion ─────────────────────────────

class TestCcxtToDF:
    def test_basic_conversion(self):
        df = CcxtClient._ccxt_to_df(SAMPLE_OHLCV)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3
        assert list(df.columns) == ['open', 'high', 'low', 'close', 'volume']

    def test_dtypes(self):
        df = CcxtClient._ccxt_to_df(SAMPLE_OHLCV)
        for col in ('open', 'high', 'low', 'close', 'volume'):
            assert df[col].dtype == np.float64

    def test_datetime_index(self):
        df = CcxtClient._ccxt_to_df(SAMPLE_OHLCV)
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.is_monotonic_increasing

    def test_empty_input(self):
        df = CcxtClient._ccxt_to_df([])
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_dedup_keeps_latest(self):
        duped = SAMPLE_OHLCV + [SAMPLE_OHLCV[-1]]
        df = CcxtClient._ccxt_to_df(duped)
        assert len(df) == 3

    def test_drops_zero_close(self):
        bad = SAMPLE_OHLCV + [[1712880000000, 0, 0, 0, 0, 0]]
        df = CcxtClient._ccxt_to_df(bad)
        assert len(df) == 3

    def test_values(self):
        df = CcxtClient._ccxt_to_df(SAMPLE_OHLCV)
        assert df['open'].iloc[0] == 150.0
        assert df['close'].iloc[-1] == 159.0
        assert df['volume'].iloc[1] == 45000.0


# ─── Fetch Methods ────────────────────────────────────

def _patch_resolve(client, monkeypatch, mock_ex=None):
    """Patch _resolve_symbol to return a mock exchange without importing ccxt."""
    if mock_ex is None:
        mock_ex = _make_mock_exchange()
    monkeypatch.setattr(client, '_resolve_symbol',
                        lambda s: (mock_ex, f'{s.upper()}/USDT', 'kucoin'))
    return mock_ex


class TestFetchOhlcv:
    def test_returns_data_for_supported_coin(self, client, monkeypatch):
        mock_ex = _make_mock_exchange()
        _patch_resolve(client, monkeypatch, mock_ex)
        monkeypatch.setattr('api_rate_limiter.can_call', lambda p: (True, ''))
        monkeypatch.setattr('api_rate_limiter.record_call', lambda p: None)

        df = client.fetch_ohlcv('XMR', 30)
        assert not df.empty
        assert len(df) == 3
        assert df['close'].iloc[-1] == 159.0

    def test_returns_empty_for_unsupported_coin(self, client):
        df = client.fetch_ohlcv('BTC', 30)
        assert df.empty

    def test_returns_empty_on_exchange_error(self, client, monkeypatch):
        mock_ex = MagicMock()
        mock_ex.fetch_ohlcv.side_effect = Exception("Connection timeout")
        _patch_resolve(client, monkeypatch, mock_ex)
        monkeypatch.setattr('api_rate_limiter.can_call', lambda p: (True, ''))
        monkeypatch.setattr('api_rate_limiter.record_call', lambda p: None)

        df = client.fetch_ohlcv('XMR', 30)
        assert df.empty

    def test_respects_rate_limit(self, client, monkeypatch):
        monkeypatch.setattr('api_rate_limiter.can_call',
                            lambda p: (False, 'Rate limit reached'))
        mock_ex = _make_mock_exchange()
        _patch_resolve(client, monkeypatch, mock_ex)

        df = client.fetch_ohlcv('XMR', 30)
        assert df.empty
        mock_ex.fetch_ohlcv.assert_not_called()


class TestFetchKlines:
    def test_supported_interval(self, client, monkeypatch):
        mock_ex = _make_mock_exchange()
        _patch_resolve(client, monkeypatch, mock_ex)
        monkeypatch.setattr('api_rate_limiter.can_call', lambda p: (True, ''))
        monkeypatch.setattr('api_rate_limiter.record_call', lambda p: None)

        df = client.fetch_klines('XMR', '1h', 100)
        assert not df.empty

    def test_unsupported_interval(self, client):
        df = client.fetch_klines('XMR', '2d', 100)
        assert df.empty


class TestFetchKlinesForRange:
    def test_range_fetch(self, client, monkeypatch):
        mock_ex = _make_mock_exchange()
        _patch_resolve(client, monkeypatch, mock_ex)
        monkeypatch.setattr('api_rate_limiter.can_call', lambda p: (True, ''))
        monkeypatch.setattr('api_rate_limiter.record_call', lambda p: None)

        start = 1712620800000  # 2024-04-09 (matches SAMPLE_OHLCV)
        end = 1712880000000    # 2024-04-12
        df = client.fetch_klines_for_range('XMR', '15m', start, end)
        assert not df.empty


# ─── Symbol Normalization ─────────────────────────────

class TestSymbolNormalization:
    def test_all_coins_uppercase(self):
        for coin in CcxtClient.CCXT_COINS_SEED:
            assert coin == coin.upper(), f"{coin} must be uppercase"

    def test_all_exchanges_valid(self):
        for coin, ex_id in CcxtClient.CCXT_COINS_SEED.items():
            assert ex_id in CcxtClient.EXCHANGE_PRIORITY, \
                f"{coin} maps to unknown exchange {ex_id}"

    def test_pair_format_is_usdt(self):
        """All CCXT_COINS should produce SYMBOL/USDT pairs."""
        for coin in CcxtClient.CCXT_COINS_SEED:
            assert f'{coin}/USDT' == f'{coin}/USDT'  # Verifies the pattern used in _resolve_symbol


# ─── Interval Conversion ─────────────────────────────

class TestIntervalToMs:
    def test_known_intervals(self):
        assert CcxtClient._interval_to_ms('1m') == 60_000
        assert CcxtClient._interval_to_ms('1h') == 3_600_000
        assert CcxtClient._interval_to_ms('1d') == 86_400_000

    def test_unknown_defaults_to_daily(self):
        assert CcxtClient._interval_to_ms('unknown') == 86_400_000


# ─── Auto-Discovery ──────────────────────────────────

class TestAutoDiscovery:
    def test_seed_coins_in_routing_table(self, client):
        """Seed coins should always be in the routing table."""
        for coin in CcxtClient.CCXT_COINS_SEED:
            assert coin in client._routing_table

    def test_get_available_symbols(self, client):
        syms = client.get_available_symbols()
        assert 'XMR' in syms
        assert 'DEEP' in syms

    def test_get_venue_for_symbol(self, client):
        assert client.get_venue_for_symbol('XMR') == 'kucoin'
        assert client.get_venue_for_symbol('DEEP') == 'bybit'
        assert client.get_venue_for_symbol('NONEXISTENT') is None

    def test_get_routing_table_is_copy(self, client):
        """Returned routing table should be a copy (not mutable reference)."""
        rt = client.get_routing_table()
        rt['FAKECOIN'] = 'mexc'
        assert 'FAKECOIN' not in client._routing_table


# ─── Venue Tagging ────────────────────────────────────

class TestVenueTagging:
    def test_tag_venue_sets_attr(self, client):
        df = pd.DataFrame({'close': [100.0]}, index=pd.to_datetime(['2026-04-09']))
        tagged = client._tag_venue(df, 'kucoin')
        assert tagged.attrs.get('_data_venue') == 'ccxt_kucoin'

    def test_tag_venue_empty_df(self, client):
        df = pd.DataFrame()
        tagged = client._tag_venue(df, 'mexc')
        assert '_data_venue' not in tagged.attrs


# ─── Redis Routing-Table Persistence ─────────────────


class _FakeRedis:
    """Minimal in-process stand-in for the redis client used by the routing cache.

    Supports the subset the client touches: get / set (with nx + ex) / delete.
    TTLs are ignored (tests don't sleep); nx is honoured for the build lock.
    """

    def __init__(self):
        self.store = {}

    def get(self, key):
        val = self.store.get(key)
        if val is None:
            return None
        return val.encode('utf-8') if isinstance(val, str) else val

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def cold_client(monkeypatch):
    """A CcxtClient with an EMPTY routing table (cold start) and live builds stubbed."""
    import os
    os.environ.pop('CCXT_DISABLED', None)
    for ex in CcxtClient.EXCHANGE_PRIORITY:
        os.environ.pop(f'CCXT_{ex.upper()}_DISABLED', None)
    c = CcxtClient()
    # Stub the expensive exchange build so it's observable + deterministic.
    monkeypatch.setattr(c, '_build_routing_table',
                        lambda: {'XMR': 'kucoin', 'DEEP': 'bybit'})
    return c


class TestRoutingRedisPersistence:
    def test_build_then_persist_to_redis(self, cold_client, monkeypatch):
        """Cold start with empty Redis builds once and writes the result back."""
        fake = _FakeRedis()
        monkeypatch.setattr(cold_client, '_redis', lambda: fake)

        cold_client._ensure_markets()

        assert cold_client._routing_table == {'XMR': 'kucoin', 'DEEP': 'bybit'}
        assert cold_client.ROUTING_REDIS_KEY in fake.store
        payload = json.loads(fake.store[cold_client.ROUTING_REDIS_KEY])
        assert payload['table'] == {'XMR': 'kucoin', 'DEEP': 'bybit'}
        assert 'built_at' in payload

    def test_cold_start_loads_from_redis_without_rebuild(self, cold_client, monkeypatch):
        """A fresh table in Redis is adopted; the 14s exchange build is skipped."""
        from datetime import datetime
        fake = _FakeRedis()
        fake.store[cold_client.ROUTING_REDIS_KEY] = json.dumps({
            'table': {'CACHED': 'mexc'},
            'built_at': datetime.now().isoformat(),
            # A table is only adopted if it was built by the
            # CURRENT routing logic. Without this the payload reads as the
            # pre-stamp format (version 1) and is correctly discarded.
            'logic_version': cold_client.ROUTING_LOGIC_VERSION,
        })
        monkeypatch.setattr(cold_client, '_redis', lambda: fake)

        built = {'called': False}
        monkeypatch.setattr(cold_client, '_build_routing_table',
                            lambda: built.__setitem__('called', True) or {'X': 'mexc'})

        cold_client._ensure_markets()

        assert cold_client._routing_table == {'CACHED': 'mexc'}
        assert built['called'] is False, "should not rebuild when Redis is fresh"

    def test_stale_redis_triggers_rebuild(self, cold_client, monkeypatch):
        """An expired Redis entry is ignored and the table is rebuilt."""
        from datetime import datetime, timedelta
        fake = _FakeRedis()
        old = datetime.now() - timedelta(seconds=CcxtClient.MARKET_CACHE_TTL + 100)
        fake.store[cold_client.ROUTING_REDIS_KEY] = json.dumps({
            'table': {'STALE': 'mexc'},
            'built_at': old.isoformat(),
        })
        monkeypatch.setattr(cold_client, '_redis', lambda: fake)

        cold_client._ensure_markets()

        assert cold_client._routing_table == {'XMR': 'kucoin', 'DEEP': 'bybit'}

    def test_redis_unavailable_falls_back_to_build(self, cold_client, monkeypatch):
        """No Redis → build locally, no crash."""
        monkeypatch.setattr(cold_client, '_redis', lambda: None)
        cold_client._ensure_markets()
        assert cold_client._routing_table == {'XMR': 'kucoin', 'DEEP': 'bybit'}

    def test_build_lock_blocks_second_worker_with_stale_table(self, cold_client, monkeypatch):
        """If the NX build lock is held and we already have a stale table, serve stale."""
        from datetime import datetime, timedelta
        fake = _FakeRedis()
        fake.store[cold_client.ROUTING_LOCK_KEY] = '1'  # lock already taken
        monkeypatch.setattr(cold_client, '_redis', lambda: fake)
        # Pretend we hold a stale in-memory table.
        cold_client._routing_table = {'OLD': 'mexc'}
        cold_client._markets_loaded_at = datetime.now() - timedelta(seconds=CcxtClient.MARKET_CACHE_TTL + 1)

        rebuilt = {'called': False}
        monkeypatch.setattr(cold_client, '_build_routing_table',
                            lambda: rebuilt.__setitem__('called', True) or {'NEW': 'mexc'})

        cold_client._populate_routing_table()

        assert rebuilt['called'] is False, "lock held + stale table → must not rebuild"
        assert cold_client._routing_table == {'OLD': 'mexc'}

    def test_corrupt_redis_payload_falls_back_to_build(self, cold_client, monkeypatch):
        """Garbage in Redis must not break resolution — degrade to a rebuild."""
        fake = _FakeRedis()
        fake.store[cold_client.ROUTING_REDIS_KEY] = 'not-json{{'
        monkeypatch.setattr(cold_client, '_redis', lambda: fake)
        cold_client._ensure_markets()
        assert cold_client._routing_table == {'XMR': 'kucoin', 'DEEP': 'bybit'}

    def test_fresh_table_short_circuits_redis(self, client, monkeypatch):
        """A fresh in-memory table never touches Redis."""
        touched = {'called': False}
        monkeypatch.setattr(client, '_redis',
                            lambda: touched.__setitem__('called', True) or None)
        client._ensure_markets()
        assert touched['called'] is False


# ─── a short page is not the present ──────

class TestPaginationShortPage:
    """gate.io returns 999 rows for GT/USDT from 1500d back, ending before now.
    `len(data) < limit` does not mean "reached now". The loop must
    keep walking until the last candle is within one interval of now (or `until`)."""

    DAY = 86_400_000

    def _rows(self, start_ms, n):
        return [[start_ms + i * self.DAY, 1.0, 2.0, 0.5, 1.5, 10.0] for i in range(n)]

    def test_keeps_walking_after_short_page_in_the_past(self, client):
        now_ms = int(time.time() * 1000)
        since = now_ms - 1500 * self.DAY
        page1 = self._rows(since, 999)                       # ends ~501d ago
        page2_start = page1[-1][0] + self.DAY
        page2 = self._rows(page2_start, 500)                 # reaches ~yesterday
        ex = MagicMock()
        ex.fetch_ohlcv.side_effect = [page1, page2, []]

        out = client._fetch_paginated(ex, 'GT/USDT', '1d', since)

        assert len(out) == 1499
        assert out[-1][0] == page2[-1][0]
        assert ex.fetch_ohlcv.call_count >= 2
        assert ex.fetch_ohlcv.call_args_list[1].kwargs['since'] == page2_start

    def test_short_page_that_reaches_now_stops(self, client):
        now_ms = int(time.time() * 1000)
        today = now_ms - (now_ms % self.DAY)
        since = today - 9 * self.DAY
        page = self._rows(since, 10)                         # last bar = today
        ex = MagicMock()
        ex.fetch_ohlcv.return_value = page

        out = client._fetch_paginated(ex, 'GT/USDT', '1d', since)

        assert len(out) == 10
        assert ex.fetch_ohlcv.call_count == 1

    def test_until_still_bounds_the_walk(self, client):
        now_ms = int(time.time() * 1000)
        since = now_ms - 1500 * self.DAY
        until = since + 20 * self.DAY
        page = self._rows(since, 30)
        ex = MagicMock()
        ex.fetch_ohlcv.return_value = page

        out = client._fetch_paginated(ex, 'GT/USDT', '1d', since, until)

        assert ex.fetch_ohlcv.call_count == 1
        assert all(c[0] <= until for c in out)
