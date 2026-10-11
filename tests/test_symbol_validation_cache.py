"""A dynamic-symbol chart request must not gate on a live Yahoo
call it does not need.

Two independent guarantees:

1. ``OHLCVDatabase.validate_symbol`` caches POSITIVE results in Redis, so a
   symbol validated once stays validated even while the shared
   ``yahoo_minute`` budget is exhausted (the ~140s post-restart warmup
   window). A ``None`` result is NEVER cached — a rate-limited denial is
   indistinguishable from a genuinely unknown symbol, and caching it would
   404 a valid symbol for the whole TTL.

2. ``get_chart_data`` consults the chart response cache BEFORE validating a
   dynamic symbol, so a warm cached chart serves even when every upstream
   validator would fail at that moment.
"""
from __future__ import annotations

from unittest.mock import MagicMock


import database as database_mod
from database import OHLCVDatabase


class _FakeRedis:
    """Just enough of the redis interface for the validation cache."""

    def __init__(self):
        self.store = {}

    def get(self, k):
        return self.store.get(k)

    def set(self, k, v, ex=None, nx=False):
        if nx and k in self.store:
            return None
        self.store[k] = v
        return True

    def setex(self, k, ttl, v):
        self.store[k] = v
        return True


def _stub_db(yahoo_result):
    """A stand-in ``self`` for the unbound real method: not curated, Binance
    and CCXT both miss, Yahoo answers ``yahoo_result``."""
    stub = MagicMock()
    stub.is_curated.return_value = False
    stub.binance.get_exchange_info.return_value = {}
    stub.ccxt.is_supported.return_value = False
    stub.yahoo.validate_symbol.return_value = yahoo_result
    return stub


class TestValidationResultCache:
    def test_positive_validation_served_from_cache_without_any_source(self, monkeypatch):
        r = _FakeRedis()
        monkeypatch.setattr(database_mod, "_get_cc_redis", lambda: r)
        info = {"symbol": "BTC-USD", "name": "Bitcoin USD", "type": "crypto",
                "source": "yahoo", "tier": "dynamic"}
        stub = _stub_db(info)

        first = OHLCVDatabase.validate_symbol(stub, "BTC-USD")
        assert first == info

        # Second call: every upstream is now poisoned — a cached validation
        # must not touch any of them (this is the rate-limit-window scenario).
        stub.binance.get_exchange_info.side_effect = AssertionError("binance hit on cached validation")
        stub.ccxt.is_supported.side_effect = AssertionError("ccxt hit on cached validation")
        stub.yahoo.validate_symbol.side_effect = AssertionError("yahoo hit on cached validation")

        second = OHLCVDatabase.validate_symbol(stub, "BTC-USD")
        assert second == info

    def test_negative_validation_is_not_cached(self, monkeypatch):
        r = _FakeRedis()
        monkeypatch.setattr(database_mod, "_get_cc_redis", lambda: r)
        stub = _stub_db(None)

        assert OHLCVDatabase.validate_symbol(stub, "NOPE-XX") is None
        assert OHLCVDatabase.validate_symbol(stub, "NOPE-XX") is None
        # Both calls must have gone all the way to Yahoo — a None (which may
        # just mean "rate-limited right now") must never be replayed.
        assert stub.yahoo.validate_symbol.call_count == 2

    def test_no_redis_degrades_to_live_validation(self, monkeypatch):
        monkeypatch.setattr(database_mod, "_get_cc_redis", lambda: None)
        info = {"symbol": "ETH-USD", "name": "Ethereum USD", "type": "crypto",
                "source": "yahoo", "tier": "dynamic"}
        stub = _stub_db(info)
        assert OHLCVDatabase.validate_symbol(stub, "ETH-USD") == info
        assert OHLCVDatabase.validate_symbol(stub, "ETH-USD") == info
        assert stub.yahoo.validate_symbol.call_count == 2


class TestCacheBeforeValidation:
    def test_warm_cached_chart_serves_dynamic_symbol_without_validation(
            self, auth_client, monkeypatch):
        import time as _time
        payload = {
            "symbol": "FOO-USD", "timeframe": "1D", "ohlcv": [], "volume": [],
            "rsi": [], "markers": [], "priceLines": [], "indicators": {},
            "cpr": None, "weekly_cpr": None, "daily_cpr": None,
            "tier": "dynamic", "live_ws": False, "ws_venue": None,
            "_cached_at": int(_time.time()) - 5,
        }
        mock_db = MagicMock()
        mock_db.is_curated.return_value = False
        mock_db.validate_symbol.side_effect = AssertionError(
            "validate_symbol called although a warm cached chart existed")
        monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart",
                            lambda *a, **kw: (payload, True))

        resp = auth_client.get("/api/chart/FOO-USD?days=1500&mode=htf&timeframe=1D")

        assert resp.status_code == 200, resp.data[:300]

    def test_cache_miss_still_validates_and_404s_unknown_symbols(
            self, auth_client, monkeypatch):
        mock_db = MagicMock()
        mock_db.is_curated.return_value = False
        mock_db.validate_symbol.return_value = None
        monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart",
                            lambda *a, **kw: None)

        resp = auth_client.get("/api/chart/NOPE-XX?days=1500&mode=htf&timeframe=1D")

        assert resp.status_code == 404
        assert b"Unknown symbol" in resp.data
