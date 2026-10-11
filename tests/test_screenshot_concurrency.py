"""
Hard concurrency cap + bounded queue on the Playwright
chart-screenshot render service.

Pins that screenshot_chart() must gate the actual browser-render
section (extracted into _render_screenshot_via_playwright) behind a
module-level threading.BoundedSemaphore(SCREENSHOT_MAX_CONCURRENCY), with a
bounded wait (SCREENSHOT_QUEUE_TIMEOUT_SEC) that returns a clean 503 +
Retry-After instead of piling up unboundedly. Disk-cache HITs must never
touch the semaphore/queue.

Each test swaps in a THROWAWAY BoundedSemaphore via monkeypatch (see
_install_semaphore) rather than mutating the real module-level semaphore, so
nothing here can leak state into other test files that also exercise
/api/screenshot (e.g. test_ownership_and_prompt_injection_guards.py).
"""

import os
import threading
import time
from unittest.mock import MagicMock

from carrac.reports import screenshots

_API_KEY = 'test-api-key-for-testing'  # matches conftest.py's SCREENSHOT_API_KEY override
_HEADERS = {'X-Internal-Key': _API_KEY}
_FAKE_JPEG = b'\xff\xd8\xff\xdb-fake-jpeg-bytes'


def _install_semaphore(monkeypatch, max_concurrency, queue_timeout=None):
    """Swap in a fresh BoundedSemaphore(max_concurrency) for this test only.

    monkeypatch restores the original module attributes on teardown, so the
    real semaphore used by the rest of the suite is never mutated here.
    """
    monkeypatch.setattr(screenshots, 'SCREENSHOT_MAX_CONCURRENCY', max_concurrency)
    monkeypatch.setattr(screenshots, '_render_semaphore',
                         threading.BoundedSemaphore(max_concurrency))
    monkeypatch.setattr(screenshots, '_active_renders', 0)
    if queue_timeout is not None:
        monkeypatch.setattr(screenshots, 'SCREENSHOT_QUEUE_TIMEOUT_SEC', queue_timeout)


def _cache_paths(asset, timeframe, width, height):
    cache_dir = os.path.join('data', 'screenshots')
    cache_file = os.path.join(cache_dir, f'{asset}_{timeframe}_{width}x{height}.jpg')
    return cache_dir, cache_file


def _cleanup_cache(asset, timeframe, width, height):
    _, cache_file = _cache_paths(asset, timeframe, width, height)
    if os.path.exists(cache_file):
        os.remove(cache_file)


class TestConcurrencyCap:
    """(a) no more than SCREENSHOT_MAX_CONCURRENCY renders run at once."""

    def test_max_concurrent_renders_is_capped(self, client, monkeypatch):
        N = 5
        MAX = 2
        _install_semaphore(monkeypatch, MAX, queue_timeout=5)

        max_concurrent = [0]
        currently_active = [0]
        lock = threading.Lock()
        gate = threading.Barrier(N)

        def fake_render(asset, timeframe, indicators, width, height, quality, api_key):
            with lock:
                currently_active[0] += 1
                if currently_active[0] > max_concurrent[0]:
                    max_concurrent[0] = currently_active[0]
            time.sleep(0.15)
            with lock:
                currently_active[0] -= 1
            return _FAKE_JPEG

        monkeypatch.setattr(screenshots, '_render_screenshot_via_playwright', fake_render)

        # Distinct asset per thread -> distinct cache files -> guaranteed
        # cache MISS on every request, so every request actually renders.
        responses = [None] * N

        def worker(i):
            gate.wait()  # synchronize all N threads to fire at once
            responses[i] = client.get(
                f'/api/screenshot/ZZQC{i}/1D?width=320&height=180',
                headers=_HEADERS,
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        try:
            assert all(r is not None for r in responses), "not all requests completed in time"
            assert all(r.status_code == 200 for r in responses), \
                [r.status_code for r in responses]
            assert max_concurrent[0] <= MAX, (
                f"max concurrent renders was {max_concurrent[0]}, expected <= {MAX} "
                "— the semaphore is not being honoured"
            )
        finally:
            for i in range(N):
                _cleanup_cache(f'ZZQC{i}', '1D', 320, 180)

    def test_all_requests_eventually_complete(self, client, monkeypatch):
        """Throttling must not drop requests — all must eventually complete (200)."""
        N = 4
        _install_semaphore(monkeypatch, max_concurrency=2, queue_timeout=5)

        def fake_render(asset, timeframe, indicators, width, height, quality, api_key):
            time.sleep(0.05)
            return _FAKE_JPEG

        monkeypatch.setattr(screenshots, '_render_screenshot_via_playwright', fake_render)

        gate = threading.Barrier(N)
        responses = [None] * N

        def worker(i):
            gate.wait()
            responses[i] = client.get(
                f'/api/screenshot/ZZQA{i}/1D?width=321&height=181',
                headers=_HEADERS,
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        try:
            assert all(r is not None and r.status_code == 200 for r in responses), \
                [None if r is None else r.status_code for r in responses]
        finally:
            for i in range(N):
                _cleanup_cache(f'ZZQA{i}', '1D', 321, 181)


class TestQueueTimeoutBusy:
    """(b) a render slot not acquired within the timeout returns a clean 503."""

    def test_queue_timeout_returns_503_with_retry_after(self, client, monkeypatch):
        _install_semaphore(monkeypatch, max_concurrency=1, queue_timeout=1)

        def slow_render(asset, timeframe, indicators, width, height, quality, api_key):
            # Holds the single slot well past the 1s queue timeout so the
            # second concurrent request is guaranteed to time out waiting.
            time.sleep(2.0)
            return _FAKE_JPEG

        monkeypatch.setattr(screenshots, '_render_screenshot_via_playwright', slow_render)

        gate = threading.Barrier(2)
        responses = [None, None]

        def worker(i, asset):
            gate.wait()
            responses[i] = client.get(
                f'/api/screenshot/{asset}/1D?width=322&height=182',
                headers=_HEADERS,
            )

        t1 = threading.Thread(target=worker, args=(0, 'ZZQT0'))
        t2 = threading.Thread(target=worker, args=(1, 'ZZQT1'))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        try:
            assert all(r is not None for r in responses), "not all requests completed in time"
            statuses = sorted(r.status_code for r in responses)
            assert statuses == [200, 503], (
                f"expected one 200 (won the slot) and one 503 (queue timeout), got {statuses}"
            )

            busy = next(r for r in responses if r.status_code == 503)
            assert busy.headers.get('Retry-After') == '1', busy.headers.get('Retry-After')
            body = busy.get_json()
            assert body is not None and 'busy' in body.get('error', '').lower(), body

            ok = next(r for r in responses if r.status_code == 200)
            assert ok.headers.get('X-Cache') == 'MISS'
        finally:
            _cleanup_cache('ZZQT0', '1D', 322, 182)
            _cleanup_cache('ZZQT1', '1D', 322, 182)


class TestCacheHitBypassesSlot:
    """(c) disk-cache HITs never touch the semaphore/queue."""

    def test_cache_hit_returns_even_with_semaphore_fully_held(self, client, monkeypatch):
        _install_semaphore(monkeypatch, max_concurrency=1, queue_timeout=0.3)

        # If a cache HIT incorrectly tried to acquire a render slot, this
        # mock firing would fail the test outright.
        render_mock = MagicMock(
            side_effect=AssertionError('render function must not be called for a cache HIT')
        )
        monkeypatch.setattr(screenshots, '_render_screenshot_via_playwright', render_mock)

        asset, tf, width, height = 'ZZQH0', '1D', 323, 183
        cache_dir, cache_file = _cache_paths(asset, tf, width, height)
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_file, 'wb') as f:
            f.write(_FAKE_JPEG)

        # Fully exhaust the (capacity-1) semaphore ourselves BEFORE issuing
        # the request. A cache hit must return the cached bytes immediately
        # regardless — it must never block waiting on this held slot.
        held = screenshots._render_semaphore.acquire(blocking=False)
        assert held, "test setup: could not acquire the fresh semaphore"

        try:
            start = time.time()
            resp = client.get(
                f'/api/screenshot/{asset}/{tf}?width={width}&height={height}',
                headers=_HEADERS,
            )
            elapsed = time.time() - start

            assert resp.status_code == 200
            assert resp.headers.get('X-Cache') == 'HIT'
            assert resp.data == _FAKE_JPEG
            assert elapsed < 0.2, f"cache hit took {elapsed:.2f}s — looks like it queued"
            render_mock.assert_not_called()
        finally:
            screenshots._render_semaphore.release()
            _cleanup_cache(asset, tf, width, height)
