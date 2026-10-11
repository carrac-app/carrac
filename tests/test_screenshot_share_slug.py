"""The screenshot service's slug-scoped render path.

``/api/screenshot/<asset>/<timeframe>`` gained an optional ``share_slug``
query param: when a chart share has FROZEN drawings, ``create_chart_share``
(carrac/plays/shares.py) passes its slug through so the Playwright render
goes to ``/chart-headless/share/<slug>`` (which burns that share's own
drawings into the JPEG) instead of the plain ``/chart-headless/<asset>/
<timeframe>`` page.

The disk cache at ``data/screenshots/<asset>_<timeframe>_<w>x<h>.jpg`` keys
ONLY on asset+timeframe+size — a slug-scoped render must never read from or
write to it, or a later anonymous request for the same asset/timeframe/size
(a different share, or the morning bot) would be served a JPEG with a
stranger's private drawings baked in. Covers that cache-key isolation, the
share_slug format gate, that the render still goes through the
concurrency semaphore, and that the URL Playwright navigates to differs by
mode. Renders are monkeypatched throughout — no real headless browser.
"""

import os
import threading

from carrac.reports import screenshots

_API_KEY = 'test-api-key-for-testing'  # matches conftest.py's SCREENSHOT_API_KEY override
_HEADERS = {'X-Internal-Key': _API_KEY}


def _cache_paths(asset, timeframe, width, height):
    cache_dir = os.path.join('data', 'screenshots')
    return cache_dir, os.path.join(cache_dir, f'{asset}_{timeframe}_{width}x{height}.jpg')


def _cleanup_cache(asset, timeframe, width, height):
    _, cache_file = _cache_paths(asset, timeframe, width, height)
    if os.path.exists(cache_file):
        os.remove(cache_file)


class TestShareSlugValidation:
    def test_invalid_share_slug_rejected(self, client):
        resp = client.get('/api/screenshot/BTC/1D?share_slug=AB', headers=_HEADERS)
        assert resp.status_code == 400

    def test_valid_share_slug_accepted(self, client, monkeypatch):
        monkeypatch.setattr(
            screenshots, '_render_screenshot_via_playwright',
            lambda *a, **k: b'\xff\xd8\xff\xdb-fake')
        asset, tf = 'ZZSA0', '1D'
        try:
            resp = client.get(
                f'/api/screenshot/{asset}/{tf}?share_slug=abcd1234&width=300&height=150',
                headers=_HEADERS)
            assert resp.status_code == 200
        finally:
            _cleanup_cache(asset, tf, 300, 150)


class TestGenericCacheIsolation:
    """A slug-scoped render must not poison (read from
    or write to) the generic asset+timeframe+size cache."""

    def test_share_slug_bypasses_cache_read_even_when_a_generic_entry_exists(
            self, client, monkeypatch):
        asset, tf, width, height = 'ZZSB0', '1D', 400, 200
        cache_dir, cache_file = _cache_paths(asset, tf, width, height)
        os.makedirs(cache_dir, exist_ok=True)
        poisoned = b'GENERIC-CACHED-BYTES-FROM-SOMEONE-ELSE'
        with open(cache_file, 'wb') as f:
            f.write(poisoned)

        render_calls = []

        def fake_render(asset_, timeframe_, indicators_, width_, height_,
                        quality_, api_key_, share_slug=None):
            render_calls.append(share_slug)
            return b'SHARE-SPECIFIC-BYTES'

        monkeypatch.setattr(screenshots, '_render_screenshot_via_playwright', fake_render)

        try:
            resp = client.get(
                f'/api/screenshot/{asset}/{tf}?width={width}&height={height}'
                '&share_slug=abcd1234',
                headers=_HEADERS)
            assert resp.status_code == 200
            # Got the FRESH render, not the poisoned generic-cache bytes.
            assert resp.data == b'SHARE-SPECIFIC-BYTES'
            assert resp.headers.get('X-Cache') == 'BYPASS'
            assert render_calls == ['abcd1234']
        finally:
            _cleanup_cache(asset, tf, width, height)

    def test_share_slug_render_does_not_write_generic_cache(self, client, monkeypatch):
        asset, tf, width, height = 'ZZSB1', '1D', 401, 201
        _, cache_file = _cache_paths(asset, tf, width, height)
        assert not os.path.exists(cache_file)

        monkeypatch.setattr(
            screenshots, '_render_screenshot_via_playwright',
            lambda *a, **k: b'SHARE-BYTES')

        try:
            resp = client.get(
                f'/api/screenshot/{asset}/{tf}?width={width}&height={height}'
                '&share_slug=abcd1234',
                headers=_HEADERS)
            assert resp.status_code == 200
            # No generic cache file was created by the slug-scoped render.
            assert not os.path.exists(cache_file)
        finally:
            _cleanup_cache(asset, tf, width, height)

    def test_no_share_slug_generic_cache_still_written_and_read(self, client, monkeypatch):
        """Pins that the plain (no-slug) path is unchanged — still writes
        and later reads the generic cache."""
        asset, tf, width, height = 'ZZSB2', '1D', 402, 202
        calls = []

        def fake_render_no_kwargs(asset_, timeframe_, indicators_, width_,
                                  height_, quality_, api_key_):
            calls.append(1)
            return b'GENERIC-BYTES'

        monkeypatch.setattr(screenshots, '_render_screenshot_via_playwright',
                            fake_render_no_kwargs)

        try:
            first = client.get(
                f'/api/screenshot/{asset}/{tf}?width={width}&height={height}',
                headers=_HEADERS)
            assert first.status_code == 200
            assert first.headers.get('X-Cache') == 'MISS'
            assert len(calls) == 1

            second = client.get(
                f'/api/screenshot/{asset}/{tf}?width={width}&height={height}',
                headers=_HEADERS)
            assert second.status_code == 200
            assert second.headers.get('X-Cache') == 'HIT'
            assert len(calls) == 1  # no second render — served from disk cache
        finally:
            _cleanup_cache(asset, tf, width, height)


class TestShareSlugRespectsSemaphore:
    """No new render path outside the bounded
    concurrency guard — a slug-scoped request must queue/503 exactly like a
    plain one when the semaphore is exhausted."""

    def test_share_slug_render_queues_behind_a_held_semaphore(self, client, monkeypatch):
        monkeypatch.setattr(screenshots, 'SCREENSHOT_MAX_CONCURRENCY', 1)
        monkeypatch.setattr(screenshots, '_render_semaphore', threading.BoundedSemaphore(1))
        monkeypatch.setattr(screenshots, '_active_renders', 0)
        monkeypatch.setattr(screenshots, 'SCREENSHOT_QUEUE_TIMEOUT_SEC', 0.3)

        held = screenshots._render_semaphore.acquire(blocking=False)
        assert held, 'test setup: could not acquire the fresh semaphore'
        try:
            resp = client.get(
                '/api/screenshot/ZZSC0/1D?share_slug=abcdef12', headers=_HEADERS)
            assert resp.status_code == 503
        finally:
            screenshots._render_semaphore.release()


class TestRenderViaPlaywrightUrlSelection:
    """Exercises the REAL _render_screenshot_via_playwright (not
    monkeypatched away) with a fake browser/context/page, so the URL
    Playwright navigates to is checked directly rather than assumed."""

    class _FakePage:
        def __init__(self, urls):
            self._urls = urls

        def route(self, pattern, handler):
            # _render_screenshot_via_playwright now registers
            # page.route() handlers for the two static chart-headless
            # scripts before navigating. This fake doesn't simulate Playwright's
            # network layer at all (goto() below just records the URL), so a
            # no-op is behavior-preserving for this test's own assertions —
            # only the URL Playwright would navigate to is under test here.
            pass

        def goto(self, url, timeout=None):
            self._urls.append(url)

        def wait_for_function(self, *a, **k):
            pass

        def title(self):
            return 'READY'

        def text_content(self, *a, **k):
            return ''

        def screenshot(self, **k):
            return b'\xff\xd8\xff\xdb-fake-jpeg'

    class _FakeContext:
        def __init__(self, urls):
            self._urls = urls

        def new_page(self):
            return TestRenderViaPlaywrightUrlSelection._FakePage(self._urls)

        def close(self):
            pass

    class _FakeBrowser:
        def __init__(self, urls):
            self._urls = urls

        def new_context(self, **kwargs):
            return TestRenderViaPlaywrightUrlSelection._FakeContext(self._urls)

    def test_uses_slug_scoped_url_when_share_slug_given(self, monkeypatch):
        urls = []
        monkeypatch.setattr(
            screenshots, '_get_persistent_browser',
            lambda: TestRenderViaPlaywrightUrlSelection._FakeBrowser(urls))
        monkeypatch.setattr(screenshots, '_drop_cached_browser_for_this_thread', lambda: None)

        out = screenshots._render_screenshot_via_playwright(
            'BTC', '1D', 'CPR,EMA_RIBBON', 800, 400, 75, 'test-key',
            share_slug='abcd1234')
        assert out == b'\xff\xd8\xff\xdb-fake-jpeg'
        assert urls == ['http://localhost:5000/chart-headless/share/abcd1234?indicators=CPR,EMA_RIBBON']

    def test_uses_plain_url_when_no_share_slug(self, monkeypatch):
        urls = []
        monkeypatch.setattr(
            screenshots, '_get_persistent_browser',
            lambda: TestRenderViaPlaywrightUrlSelection._FakeBrowser(urls))
        monkeypatch.setattr(screenshots, '_drop_cached_browser_for_this_thread', lambda: None)

        screenshots._render_screenshot_via_playwright(
            'BTC', '1D', 'CPR,EMA_RIBBON', 800, 400, 75, 'test-key')
        assert urls == ['http://localhost:5000/chart-headless/BTC/1D?indicators=CPR,EMA_RIBBON']
