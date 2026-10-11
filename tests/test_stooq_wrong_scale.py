"""Stooq must never answer with a different instrument.

Stooq carries no spot commodity or FX series, so its symbol maps point at ETF
proxies: OIL→USO, DXY→UUP (~$28 against a real DXY of ~104), EURUSD→FXE (~$100
against a rate of ~1.08). A *successful* Stooq fetch therefore returned the
wrong instrument, correctly parsed, and cached it under the spot symbol.

Note the direction: this fires on Stooq SUCCESS. The Yahoo fallback is the safe
path, because YahooClient.SYMBOL_MAP has the real instrument for all of these.
The original report had it inverted.

Dormant at the time of the fix only because Stooq is bot-blocked and every
request already falls through to Yahoo. It re-arms the moment Stooq answers CSV
again, which is exactly the kind of latent corruption worth a test.
"""

import pandas as pd

from data_sources.stooq import WRONG_SCALE_ON_STOOQ, StooqClient
from data_sources.yahoo import YahooClient


class TestWrongScaleSymbolsNeverHitStooq:
    def test_every_wrong_scale_symbol_routes_to_yahoo_not_stooq(self, monkeypatch):
        """The load-bearing assertion: Stooq is never even requested."""
        client = StooqClient.__new__(StooqClient)

        requested_urls = []

        def _no_stooq(*a, **k):
            requested_urls.append(a)
            raise AssertionError("Stooq was requested for a wrong-scale symbol")

        fell_back = []
        monkeypatch.setattr(client, "_request", _no_stooq, raising=False)
        monkeypatch.setattr(
            client, "_yahoo_fallback",
            lambda sym, sd: fell_back.append(sym) or pd.DataFrame(),
            raising=False,
        )

        for symbol in sorted(WRONG_SCALE_ON_STOOQ):
            client.fetch(symbol)

        assert requested_urls == []
        assert sorted(fell_back) == sorted(WRONG_SCALE_ON_STOOQ)

    def test_a_normal_equity_still_uses_stooq(self, monkeypatch):
        """The reroute must be narrow — Stooq is still the primary elsewhere."""
        client = StooqClient.__new__(StooqClient)

        # Build just enough state for get_stooq_symbol + fetch's early path.
        client.stock_map = {"AAPL": "AAPL.US"}
        client.etf_map = {}
        client.commodity_map = {}
        client.index_map = {}
        client.forex_map = {}

        called = []
        monkeypatch.setattr(
            client, "_request",
            lambda *a, **k: called.append(a) or None,
            raising=False,
        )
        monkeypatch.setattr(
            client, "_yahoo_fallback",
            lambda sym, sd: pd.DataFrame(),
            raising=False,
        )

        client.fetch("AAPL")

        assert called, "AAPL should still be fetched from Stooq"
        assert "AAPL.US" in called[0][1]

    def test_yahoo_has_a_real_instrument_for_every_rerouted_symbol(self):
        """The reroute is only safe if Yahoo actually covers all of them.

        Without this, rerouting would trade wrong data for no data.
        """
        missing = [s for s in WRONG_SCALE_ON_STOOQ if s not in YahooClient.SYMBOL_MAP]
        assert missing == [], f"rerouted with no Yahoo mapping: {missing}"

    def test_rerouted_yahoo_tickers_are_not_etf_proxies(self):
        """Guards the actual defect: the replacement must be the real thing.

        Futures carry '=F', FX carries '=X', DXY is the ICE index. A bare
        alphabetic ticker here would mean we swapped one ETF for another.
        """
        for symbol in sorted(WRONG_SCALE_ON_STOOQ):
            ticker = YahooClient.SYMBOL_MAP[symbol]
            assert ("=F" in ticker or "=X" in ticker or ticker.startswith("^")
                    or ticker == "DX-Y.NYB"), f"{symbol} -> {ticker} looks like a proxy"


class TestBackfillRefusesTheSameSymbols:
    def test_backfill_skips_wrong_scale_symbols(self, monkeypatch):
        """The backfill script parses Stooq CSV directly, so it does not
        inherit fetch()'s reroute and needs its own refusal."""
        from scripts.backfill_daily_history import fetch_stooq_raw

        client = StooqClient.__new__(StooqClient)
        called = []
        monkeypatch.setattr(
            client, "_request", lambda *a, **k: called.append(a) or None,
            raising=False,
        )

        for symbol in ("OIL", "DXY", "EURUSD", "GOLD"):
            out = fetch_stooq_raw(client, symbol)
            assert out.empty

        assert called == [], "backfill still requested Stooq for a proxy symbol"
