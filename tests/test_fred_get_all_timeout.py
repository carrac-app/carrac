"""FredClient.get_all() must bound its own wall time.

An earlier change fixed the sentiment fan-out one layer up
(carrac/sentiment/routes.py::_compute_sentiment) with a per-task
`future.result(timeout=...)` + `executor.shutdown(wait=False)`. get_all()
itself still used a `with ThreadPoolExecutor(...)` block and iterated
`as_completed(futures)` with NO timeout at all — a single hung series (FRED
retrying through a ConnectTimeout) blocks `get_all()`, and the shared
`_FRED_REQUEST_SEMAPHORE` (capacity 2) it holds a slot in, for as long as the
underlying HTTP retry budget takes. That is the mechanism behind "Sentiment compute budget exhausted" events.

This test fails without the per-call timeout: get_all() blocks for ~the hang duration
(bounded only by the outer test's own patience), far past any per-call budget.
"""

import threading
import time

import pytest

from data_sources.fred import FredClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('FRED_API_KEY', 'test-key')
    return FredClient()


class TestGetAllIsBounded:
    def test_one_hanging_series_does_not_block_get_all_past_its_budget(self, client, monkeypatch):
        hanging = threading.Event()

        def hung_fed_funds():
            hanging.wait(30)   # far longer than any sane per-call timeout
            return {'series': 'FED_FUNDS', 'value': 5.0}

        monkeypatch.setattr(client, 'get_fed_funds', hung_fed_funds)
        monkeypatch.setattr(client, 'get_m2', lambda: {'series': 'M2', 'value': 1.0})
        monkeypatch.setattr(client, 'get_yield_spread', lambda: {'series': 'YIELD_10Y2Y', 'value': 0.1})
        monkeypatch.setattr(client, 'get_yield_10y', lambda: {'series': 'YIELD_10Y', 'value': 4.0})
        monkeypatch.setattr(client, 'get_dxy', lambda: {'series': 'DXY', 'value': 100.0})

        started = time.monotonic()
        try:
            result = client.get_all(timeout_s=1.0)
            elapsed = time.monotonic() - started
        finally:
            hanging.set()

        assert elapsed < 4.0, f'get_all() took {elapsed:.1f}s against a 1s per-call budget'
        assert result['fed_funds'] is None, 'the hung series must fall back to None, not hang the dict'
        assert result['m2'] == {'series': 'M2', 'value': 1.0}
        assert result['dxy'] == {'series': 'DXY', 'value': 100.0}

    def test_default_timeout_is_used_when_none_passed(self, client, monkeypatch):
        """Sanity: production call sites (fetch_fred() in routes.py) call
        get_all() with no arguments — the bound must apply without one."""
        for name, val in [
            ('get_m2', {'v': 1}), ('get_fed_funds', {'v': 2}),
            ('get_yield_spread', {'v': 3}), ('get_yield_10y', {'v': 4}),
            ('get_dxy', {'v': 5}),
        ]:
            monkeypatch.setattr(client, name, (lambda val=val: val))
        result = client.get_all()
        assert result['m2'] == {'v': 1}
        assert result['dxy'] == {'v': 5}
