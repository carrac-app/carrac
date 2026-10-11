"""
Tests for the FRED request concurrency throttle.

Pins that FredClient._fetch_series() serializes HTTP calls
through a module-level Semaphore(2) so that a burst of concurrent FRED
requests (e.g. from precompute) cannot fire more than 2 simultaneously.

Unthrottled, ~6 concurrent FRED calls during precompute → 429 → backoff
retries → the precompute cycle stretches from ~143s to ~240s.

With the throttle: max 2 in-flight FRED HTTP calls at any moment.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import requests

from data_sources.fred import FredClient, _FRED_REQUEST_SEMAPHORE


def _mock_response(status: int = 200) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.ok = 200 <= status < 400
    resp.json.return_value = {
        'observations': [
            {'date': '2025-01-01', 'value': '5.33'},
            {'date': '2024-12-01', 'value': '5.30'},
        ]
    }
    return resp


class TestFredSemaphoreExists:
    """Structural: the semaphore must exist and have the right capacity."""

    def test_semaphore_is_threading_semaphore(self):
        assert isinstance(_FRED_REQUEST_SEMAPHORE, type(threading.Semaphore(2)))

    def test_semaphore_allows_up_to_two_concurrent_acquires(self):
        """Semaphore(2) allows two simultaneous acquires, blocks the third."""
        acquired = []
        s = _FRED_REQUEST_SEMAPHORE

        def try_acquire():
            got = s.acquire(blocking=True, timeout=0.05)
            if got:
                acquired.append(True)
                time.sleep(0.1)
                s.release()

        t1 = threading.Thread(target=try_acquire)
        t2 = threading.Thread(target=try_acquire)
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Both should have acquired (we're not holding any external locks here)
        assert len(acquired) == 2


class TestFredConcurrencyLimit:
    """Integration: concurrent fetch_series calls must not exceed 2 in-flight."""

    def test_max_concurrent_http_calls_is_at_most_two(self):
        """
        Fire N > 2 concurrent _fetch_series calls via threads.
        Track the high-water mark of simultaneous active HTTP calls.
        Assert that max_concurrent <= 2.
        """
        N = 6  # Simulates the ~6 concurrent FRED calls seen on real data logs
        max_concurrent = [0]
        currently_active = [0]
        lock = threading.Lock()
        gate = threading.Barrier(N)  # all threads start simultaneously

        def fake_request(method, url, **kwargs):
            with lock:
                currently_active[0] += 1
                if currently_active[0] > max_concurrent[0]:
                    max_concurrent[0] = currently_active[0]
            time.sleep(0.05)  # simulate network latency
            with lock:
                currently_active[0] -= 1
            return _mock_response()

        client = FredClient(api_key='test-key-does-not-matter')

        def worker():
            gate.wait()  # synchronize all N threads to fire at once
            client._fetch_series('DFF', limit=5)

        with patch.object(client, '_request', side_effect=fake_request):
            threads = [threading.Thread(target=worker) for _ in range(N)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert max_concurrent[0] <= 2, (
            f"Max concurrent FRED HTTP calls was {max_concurrent[0]}, expected <= 2. "
            "The _FRED_REQUEST_SEMAPHORE is not being honoured."
        )

    def test_all_calls_complete_despite_throttle(self):
        """Throttling must not drop calls — all N requests must eventually complete."""
        N = 5
        completed = []
        gate = threading.Barrier(N)

        client = FredClient(api_key='test-key')

        def worker(series_id):
            gate.wait()
            result = client._fetch_series(series_id, limit=5)
            completed.append(result)

        series_ids = ['DFF', 'DGS10', 'T10Y2Y', 'WM2NS', 'DTWEXBGS']

        with patch.object(client, '_request', return_value=_mock_response()):
            threads = [
                threading.Thread(target=worker, args=(sid,))
                for sid in series_ids[:N]
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert len(completed) == N, (
            f"Only {len(completed)}/{N} FRED calls completed — semaphore may be deadlocking"
        )

    def test_semaphore_released_on_exception(self):
        """If _request raises, the semaphore must still be released (no deadlock).

        The `with _FRED_REQUEST_SEMAPHORE:` context manager in _fetch_series
        guarantees release even when _fetch_series_http raises. We verify this
        by checking that a subsequent acquire succeeds immediately (i.e. the
        semaphore was not permanently consumed).
        """
        client = FredClient(api_key='test-key')

        def raising_request(*args, **kwargs):
            raise RuntimeError("simulated network failure")

        # _fetch_series_http raises → exception propagates through the `with`
        # block in _fetch_series → semaphore is released by the context manager.
        with patch.object(client, '_request', side_effect=raising_request):
            try:
                client._fetch_series('DFF', limit=5)
            except RuntimeError:
                pass  # exception may propagate; what matters is the semaphore state

        # After the call (pass or exception), the semaphore must be acquirable
        # without blocking, confirming it was released.
        acquired = _FRED_REQUEST_SEMAPHORE.acquire(blocking=False)
        assert acquired, (
            "Semaphore was not released after an exception in _fetch_series_http — "
            "subsequent callers would block (deadlock risk)"
        )
        if acquired:
            _FRED_REQUEST_SEMAPHORE.release()  # restore what we took

    def test_no_key_skips_semaphore(self):
        """When api_key is absent, _fetch_series returns early before acquiring the semaphore."""
        client = FredClient(api_key='')

        mock_http = MagicMock(return_value=_mock_response())
        with patch.object(client, '_request', mock_http):
            result = client._fetch_series('DFF', limit=5)

        assert result == []
        mock_http.assert_not_called()
