"""A crypto ticker must never be resolved against a US equity.

Two independent defects produced "Carrac charts Macy's under the ticker M":

1. ``CryptoMetricsClient.get_top_coins`` registered the discovered symbols
   into ``crypto_map`` on its cache-MISS branches only. The list has a 1h TTL,
   so on any ordinary call the function returned at the cache-hit early return
   and the registration never ran — code that existed and was wired to nothing
   for ~95% of calls.

2. ``<TICKER>.US`` was the DEFAULT for any symbol ``get_ohlcv_data`` did not
   recognise, so an unregistered crypto ticker resolved against a US equity
   universe and came back with real, current, well-formed data for a different
   company.

Fixing (1) alone would leave (2) armed for the next symbol that slips the
registration, so both are tested here.
"""

import pandas as pd
import pytest

import database
from database import OHLCVDatabase
from data_sources.circuit_breaker import CircuitBreaker
from data_sources.coinpaprika import CryptoMetricsClient
from data_sources.stooq import StooqClient


TOP_COINS = [
    {'symbol': 'M', 'name': 'MemeCore', 'market_cap': 1e9, 'price': 1.10},
    {'symbol': 'PI', 'name': 'Pi Network', 'market_cap': 1e9, 'price': 0.089},
    {'symbol': 'SKY', 'name': 'Sky', 'market_cap': 1e9, 'price': 0.065},
]


def _real_stooq_maps():
    client = StooqClient(CircuitBreaker('stooq_test_maps'), yahoo_client=None)
    return {n: getattr(client, n) for n in
            ('stock_map', 'etf_map', 'commodity_map', 'index_map', 'forex_map')}


# ── 1. Registration must happen on the cache-HIT path ─────────────────────────

def test_top_coins_register_crypto_provenance_on_a_cache_hit(monkeypatch):
    """The branch the function actually takes almost every time.

    The cached path must still register crypto provenance in crypto_map;
    otherwise tickers like M/PI/SKY are routed down the equity path.
    """
    monkeypatch.setattr('data_sources.coinpaprika.get_cached_top_coins',
                        lambda: TOP_COINS)
    client = CryptoMetricsClient.__new__(CryptoMetricsClient)
    crypto_map = {'BTC': 'BTC'}

    out = client.get_top_coins(limit=10, crypto_map=crypto_map)

    assert [c['symbol'] for c in out] == ['M', 'PI', 'SKY']
    for sym in ('M', 'PI', 'SKY'):
        assert sym in crypto_map, (
            f"{sym} came from a crypto market-cap list but was not registered "
            f"as crypto — it will be resolved as {sym}.US"
        )


def test_top_coins_register_crypto_provenance_on_a_cache_miss(monkeypatch):
    """The branch that already worked — kept so a refactor cannot trade one
    path for the other."""
    monkeypatch.setattr('data_sources.coinpaprika.get_cached_top_coins',
                        lambda: None)
    monkeypatch.setattr('data_sources.coinpaprika.get_coinpaprika_top_coins',
                        lambda limit: list(TOP_COINS) + [
                            {'symbol': 'USDT', 'name': 'Tether',
                             'market_cap': 1e11, 'price': 1.0}])
    monkeypatch.setattr('data_sources.coinpaprika.set_cached_top_coins',
                        lambda coins, source=None: None)
    client = CryptoMetricsClient.__new__(CryptoMetricsClient)
    client.CRYPTO_BLACKLIST = CryptoMetricsClient.CRYPTO_BLACKLIST
    crypto_map = {}

    # limit=6 so the branch's own `len(coins) >= limit // 2` sanity check passes
    # for a 4-coin fixture — that check is the real code, not a test artefact.
    client.get_top_coins(limit=6, crypto_map=crypto_map)

    for sym in ('M', 'PI', 'SKY'):
        assert sym in crypto_map


# ── 2. The equity path must require a reason, not a default ───────────────────

def _bare_db():
    db = OHLCVDatabase.__new__(OHLCVDatabase)
    for name, mapping in _real_stooq_maps().items():
        setattr(db, name, mapping)
    return db


@pytest.mark.parametrize('symbol', ['M', 'PI', 'SKY', 'LEO', 'BGB', 'RAIN'])
def test_unknown_symbol_is_not_assumed_to_be_a_us_stock(symbol):
    ok, reason = _bare_db()._equity_path_eligible(symbol)
    assert ok is False and reason == 'unknown-symbol'


@pytest.mark.parametrize('symbol,reason', [
    ('AAPL', 'stock_map'), ('QQQ', 'etf_map'), ('GOLD', 'commodity_map'),
    ('SPX', 'index_map'), ('DXY', 'forex_map'),
    ('005930.KS', 'provider-format'),   # in a live watchlist today
    ('^GSPC', 'provider-format'), ('EURUSD=X', 'provider-format'),
])
def test_symbols_with_a_reason_keep_the_equity_path(symbol, reason):
    ok, got = _bare_db()._equity_path_eligible(symbol)
    assert ok is True and got == reason


def test_get_ohlcv_data_returns_empty_and_beacons_for_an_unknown_symbol(monkeypatch, caplog):
    """End to end at the funnel: no cache read, no upstream call, no wrong
    instrument — and a human-reaching record that it happened."""
    db = _bare_db()
    monkeypatch.setattr(database, '_get_cc_redis', lambda: None)
    monkeypatch.setattr(OHLCVDatabase, '_is_crypto_metric', lambda self, s: False, raising=False)
    monkeypatch.setattr(OHLCVDatabase, '_is_crypto', lambda self, s: False, raising=False)

    def _must_not_run(*a, **kw):
        raise AssertionError("the equity path was entered for an unknown symbol")

    class _Boom:
        load = staticmethod(_must_not_run)
    db.cache = _Boom()
    db.stooq = type('S', (), {'fetch': staticmethod(_must_not_run)})()

    with caplog.at_level('ERROR'):
        out = db.get_ohlcv_data('M')

    assert isinstance(out, pd.DataFrame) and out.empty
    assert any('equity-path refused' in r.message for r in caplog.records), \
        "a refusal nobody can see is a decision not to know"


@pytest.mark.parametrize('provenance,expect_level,expect_text', [
    (True, 'INFO', 'Expected; not an alarm'),
    (False, 'ERROR', 'cannot account for this symbol at all'),
])
def test_only_an_UNACCOUNTABLE_symbol_pages_a_human(monkeypatch, caplog,
                                                    provenance, expect_level,
                                                    expect_text):
    """A refusal is only worth an alarm when nobody can account for the symbol.

    Every fresh process refuses a handful of crypto tickers before its first
    `_get_top_coins` call — that is the gate working, and at ERROR it would be
    20 error-tracker issues every 12 hours forever. A fresh process
    typically sees ~20 such refusals, all crypto, all expected.
    """
    monkeypatch.setattr(database, '_get_cc_redis', lambda: None)
    monkeypatch.setattr(database, 'get_cached_top_coins',
                        lambda: ([{'symbol': 'PI'}] if provenance else []))

    with caplog.at_level('INFO'):
        database._beacon_equity_path_refusal('PI', 'unknown-symbol')

    rec = [r for r in caplog.records if 'equity-path refused' in r.message]
    assert len(rec) == 1
    assert rec[0].levelname == expect_level
    assert expect_text in rec[0].getMessage()


def test_a_registered_crypto_symbol_never_reaches_the_gate(monkeypatch):
    """With registration working, M takes the crypto route and the gate is
    never consulted — the two fixes compose rather than fight."""
    db = _bare_db()
    monkeypatch.setattr(OHLCVDatabase, '_is_crypto_metric', lambda self, s: False, raising=False)
    db.crypto_map = {'M': 'M'}
    monkeypatch.setattr(OHLCVDatabase, '_get_crypto_ohlcv',
                        lambda self, s, limit=None: pd.DataFrame({'close': [1.1]}))

    out = db.get_ohlcv_data('M')
    assert not out.empty and out['close'].iloc[0] == 1.1
