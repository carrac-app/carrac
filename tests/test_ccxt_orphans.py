""""listed on Binance" is not "served by Binance".

``CcxtClient._get_excluded_symbols`` skipped every symbol in Binance's
``spot_map`` so CCXT would not duplicate a source Binance already covers. But
``BinanceClient.fetch_ohlcv`` REFUSES everything in ``UNAVAILABLE`` — the
delisting batches and friends, tickers whose order book is dead
(``bidPrice == askPrice == 0``) while the pair still appears in the exchange
dump.

A symbol in both sets was excluded from CCXT for being Binance's, then
rejected by Binance, and fell through to a CryptoCompare fallback that returns
zero rows for most of them. Eight symbols are in
that gap: BEAM, EOS, FTM, MKR, NTRN, OCEAN, RDNT, TON. TON's chart sat
frozen with nothing to say so.

With the routing fix: BEAM and RDNT both route to gateio and
return current bars. RDNT additionally moves OFF CryptoCompare (a barred
source) onto a licensed one. The other six are on no configured
venue — a retirement question, not a routing one.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest


@pytest.fixture
def client():
    from data_sources.ccxt_client import CcxtClient

    return CcxtClient()


class TestBinanceUnavailableIsNotExcluded:
    def test_the_eight_orphans_are_eligible_for_ccxt(self, client):
        """Pins the orphan case. Each of these was excluded from CCXT AND refused by
        Binance, so nothing fetched them."""
        from data_sources.binance import BinanceClient
        from data_sources.hyperliquid import HyperliquidClient

        excluded = client._get_excluded_symbols()
        b = BinanceClient()
        orphans = set(b.spot_map) & set(BinanceClient.UNAVAILABLE)
        assert orphans, 'fixture drift: no symbol is in both lists any more'

        # Hyperliquid legitimately claims some coins; those stay excluded.
        still_excluded = {
            s for s in orphans & excluded if s not in set(HyperliquidClient.HL_COINS)
        }
        assert not still_excluded, (
            f'Binance refuses these but CCXT is still barred from them: '
            f'{sorted(still_excluded)}'
        )

    def test_symbols_binance_actually_serves_stay_excluded(self, client):
        """The fix must not let CCXT duplicate a live Binance source —
        otherwise the change just trades one fault for another."""
        from data_sources.binance import BinanceClient

        excluded = client._get_excluded_symbols()
        b = BinanceClient()
        served = set(b.spot_map) - set(BinanceClient.UNAVAILABLE)
        missing = {s for s in ('BTC', 'ETH', 'SOL') if s in served and s not in excluded}
        assert not missing, f'CCXT would duplicate live Binance symbols: {missing}'

    def test_hyperliquid_coins_stay_excluded(self, client):
        from data_sources.hyperliquid import HyperliquidClient

        excluded = client._get_excluded_symbols()
        hl = set(HyperliquidClient.HL_COINS)
        assert hl <= excluded or not hl, 'Hyperliquid coins must not be duplicated'


class TestRoutingCacheIsInvalidatedByLogicChanges:
    """A routing-logic change must not sit dormant for a full
    MARKET_CACHE_TTL after deploy, because a table built by the previous logic
    is still inside its TTL and gets loaded verbatim.
    """

    class _FakeRedis:
        def __init__(self, raw=None):
            self.raw = raw

        def get(self, _key):
            return self.raw

        def set(self, _key, value, ex=None):
            self.raw = value

    def _with_redis(self, client, monkeypatch, raw):
        fake = self._FakeRedis(raw)
        monkeypatch.setattr(client, '_redis', lambda: fake)
        return fake

    def test_a_table_from_older_logic_is_discarded(self, client, monkeypatch):
        stale = json.dumps({
            'table': {'FAKE': 'gateio'},
            'built_at': datetime.now().isoformat(),
            'logic_version': client.ROUTING_LOGIC_VERSION - 1,
        })
        self._with_redis(client, monkeypatch, stale)
        table, built_at = client._load_routing_from_redis()
        assert table is None and built_at is None

    def test_a_pre_stamp_payload_is_discarded(self, client, monkeypatch):
        """What is actually in production Redis right now: no logic_version key at
        all. It must read as version 1, not as 'current'."""
        legacy = json.dumps({
            'table': {'FAKE': 'gateio'},
            'built_at': datetime.now().isoformat(),
        })
        self._with_redis(client, monkeypatch, legacy)
        table, _ = client._load_routing_from_redis()
        assert table is None

    def test_a_current_table_is_still_served(self, client, monkeypatch):
        """Otherwise the cache is useless and every process rebuilds."""
        fresh = json.dumps({
            'table': {'FAKE': 'gateio'},
            'built_at': datetime.now().isoformat(),
            'logic_version': client.ROUTING_LOGIC_VERSION,
        })
        self._with_redis(client, monkeypatch, fresh)
        table, built_at = client._load_routing_from_redis()
        assert table == {'FAKE': 'gateio'}
        assert built_at is not None

    def test_a_saved_table_round_trips(self, client, monkeypatch):
        fake = self._with_redis(client, monkeypatch, None)
        client._save_routing_to_redis({'RDNT': 'gateio'}, datetime.now())
        assert json.loads(fake.raw)['logic_version'] == client.ROUTING_LOGIC_VERSION
        table, _ = client._load_routing_from_redis()
        assert table == {'RDNT': 'gateio'}, 'a table we just wrote must load back'
