"""Retired symbols are refused at the single funnel.

GRAM, retired deliberately. It is NOT the crypto ticker —
it resolves to GRAM.US / "Gold Flora Corporation", a defunct OTC penny stock
quoted at 0.0 and pinned at $0.00001 with zero volume.

The check lives in database.get_ohlcv_data rather than at a caller because no
caller can be found: GRAM is absent from _tracked_symbols.json, from
db.get_all_symbols(), from crypto_map, from all 43 watchlist symbols, and from
every .py/.js/.json in the repo — yet something asked for it every ~16 minutes,
spending a Stooq call each time on a stock quoted at zero (the log reads
"Approaching limit (36/50)" then "Rate limit reached (51/50)").
"""
import pandas as pd

import database


class TestRetiredSymbols:
    def test_gram_is_retired(self):
        assert 'GRAM' in database.RETIRED_SYMBOLS

    def test_a_retired_symbol_returns_empty_without_fetching(self, monkeypatch):
        """The point is the ABSENCE of a network call, not just an empty frame."""
        db = database.OHLCVDatabase.__new__(database.OHLCVDatabase)
        called = []

        class _Boom:
            def load(self, *a, **k):
                called.append('cache.load'); raise AssertionError('touched the cache')

        db.cache = _Boom()
        monkeypatch.setattr(db, '_is_crypto_metric', lambda s: False, raising=False)
        monkeypatch.setattr(db, '_is_crypto', lambda s: False, raising=False)

        out = database.OHLCVDatabase.get_ohlcv_data(db, 'GRAM')
        assert isinstance(out, pd.DataFrame) and out.empty
        assert called == [], 'a retired symbol still reached the fetch path'

    def test_lowercase_is_also_retired(self, monkeypatch):
        db = database.OHLCVDatabase.__new__(database.OHLCVDatabase)
        db.cache = None   # any access would raise
        monkeypatch.setattr(db, '_is_crypto_metric', lambda s: False, raising=False)
        monkeypatch.setattr(db, '_is_crypto', lambda s: False, raising=False)
        assert database.OHLCVDatabase.get_ohlcv_data(db, 'gram').empty

    def test_a_live_symbol_is_not_refused(self, monkeypatch):
        """The guard must not swallow anything else."""
        db = database.OHLCVDatabase.__new__(database.OHLCVDatabase)
        sentinel = pd.DataFrame({'close': [1.0]}, index=pd.to_datetime(['2026-08-23']))
        monkeypatch.setattr(db, '_is_crypto_metric', lambda s: False, raising=False)
        monkeypatch.setattr(db, '_is_crypto', lambda s: True, raising=False)
        monkeypatch.setattr(db, '_get_crypto_ohlcv', lambda s, l: sentinel, raising=False)
        out = database.OHLCVDatabase.get_ohlcv_data(db, 'BTC')
        assert not out.empty, 'a live symbol was refused'

    def test_the_consensus_fetcher_is_gone_and_its_rule_is_not(self):
        """Deleting fill_event_forecasts must not lose the grounding rule it
        carried: quant.events.expected is never populated, and prompts must
        not invent a forward consensus."""
        import inspect
        from quant.collectors import event_calendar
        assert not hasattr(event_calendar, 'fill_event_forecasts')
        assert not hasattr(event_calendar, 'match_finnhub_estimate')
        src = inspect.getsource(event_calendar)
        assert 'MUST NOT INVENT ONE' in src, 'the no-consensus rule was lost with the fetcher'
