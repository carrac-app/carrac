"""regime's FRED reads must be bounded like get_all() is.

`gather_regime_inputs()` must not make SEQUENTIAL, untimed FRED calls
(yield spread, M2, Fed Funds) through the shared 2-slot
`_FRED_REQUEST_SEMAPHORE`: a slow FRED would hold `regime` for the whole 12 s
request budget while `fred`'s own get_all() gives up at 5 s, firing
"Sentiment compute budget exhausted before 'regime'".

Both tests run the REAL FredClient, semaphore and get_all(); only the HTTP
layer (`_fetch_series_http`) is replaced — the first one hangs it, the second
answers instantly.
"""

import threading
import time
from datetime import date, timedelta

import pytest

import data_sources.fred as fred_mod
from data_sources.fred import FredClient


@pytest.fixture
def regime_inputs_available(monkeypatch):
    """gather_regime_inputs() returns early unless the 'htf' response cache
    holds an asset list; give it one, and keep Fear & Greed off the network."""
    from carrac import precompute
    import carrac.analysis as analysis

    class _ResponseCache:
        def get(self, key):
            return {'assets': [{'symbol': 'VIX', 'latest_price': 15.0}]}

    monkeypatch.setattr(precompute, 'response_cache', _ResponseCache())
    monkeypatch.setattr(analysis, 'fetch_fear_greed_index', lambda: {})


@pytest.fixture
def fred(monkeypatch):
    monkeypatch.setenv('FRED_API_KEY', 'test-key')
    client = FredClient()
    monkeypatch.setattr(fred_mod, 'get_fred_client', lambda: client)
    return client


def _observations(series_id):
    """Synthetic FRED observations: weekly for M2 (60 of them, so a year-ago
    point exists for the YoY calc), daily for everything else."""
    today = date(2026, 9, 4)
    if series_id == 'WM2NS':
        return [{'date': (today - timedelta(weeks=i)).isoformat(), 'value': 21000.0 - i * 10}
                for i in range(60)]
    return [{'date': (today - timedelta(days=i)).isoformat(), 'value': 1.0 + i * 0.05}
            for i in range(30)]


def test_a_hung_fred_cannot_hold_regime_past_the_get_all_budget(fred, regime_inputs_available, monkeypatch):
    fred.GET_ALL_TIMEOUT_S = 1.0
    hanging = threading.Event()

    def hung_http(series_id, limit=30, sort_order='desc'):
        hanging.wait(6)   # 3 sequential calls × 6 s would far exceed the budget
        return []

    monkeypatch.setattr(fred, '_fetch_series_http', hung_http)
    from carrac.sentiment.regime import gather_regime_inputs

    started = time.monotonic()
    try:
        inputs = gather_regime_inputs()
        elapsed = time.monotonic() - started
    finally:
        hanging.set()

    assert elapsed < 4.0, f'gather_regime_inputs() took {elapsed:.1f}s against a 1s FRED budget'
    assert inputs['m2_yoy'] is None
    assert inputs['fed_funds_trend'] is None


def test_regime_still_reads_all_three_fred_series_through_the_bounded_path(fred, regime_inputs_available, monkeypatch):
    """The bound must not cost the readings: with FRED answering, all three
    inputs land exactly as the unbounded sequential code produced them."""
    monkeypatch.setattr(fred, '_fetch_series_http',
                        lambda series_id, limit=30, sort_order='desc': _observations(series_id))
    from carrac.sentiment.regime import gather_regime_inputs

    inputs = gather_regime_inputs()

    assert inputs['yield_spread'] == pytest.approx(1.0)      # latest T10Y2Y observation
    assert inputs['m2_yoy'] is not None
    assert inputs['fed_funds_trend'] in ('rising', 'falling', 'flat')
