"""
Screenshot routes: chart screenshots, Coinglass heatmap, YouTube transcript proxy.

These are API-key-protected endpoints used by the morning bot.

Playwright cold launch is 1.5-3s + ~200MB RSS per browser instance,
so re-launching on every request was both slow and an OOM risk under bursts.
We keep one browser alive per thread (Playwright sync API is thread-affine —
the launching thread must own every subsequent call), cache it across
requests, and drop browsers that have been idle for >10 minutes so a chatty
thread doesn't permanently hold ~200MB. Context-per-request keeps pages
isolated; only the browser process survives.
"""

import atexit
import logging
import os
import re
import threading
import time

import requests
from flask import Blueprint, Response, jsonify, request

from carrac.auth.decorators import verify_api_key
from carrac.extensions import csrf, limiter

logger = logging.getLogger('carrac.screenshots')

screenshots_bp = Blueprint('screenshots', __name__)

# thread-local browser cache + idle sweeper. See module docstring.
_browser_local = threading.local()
_browsers_registry: list = []
_browsers_registry_lock = threading.Lock()
_idle_sweep_started = False
_idle_sweep_lock = threading.Lock()
_BROWSER_IDLE_TIMEOUT_SEC = 600  # 10 min
_BROWSER_SWEEP_INTERVAL_SEC = 60


def _close_slot(slot: dict) -> None:
    """Best-effort teardown of a browser slot."""
    try:
        slot['browser'].close()
    except Exception:
        pass
    try:
        slot['pw'].stop()
    except Exception:
        pass


def _idle_sweep_loop() -> None:
    while True:
        time.sleep(_BROWSER_SWEEP_INTERVAL_SEC)
        now = time.time()
        with _browsers_registry_lock:
            stale = [s for s in _browsers_registry
                     if now - s['last_used'] > _BROWSER_IDLE_TIMEOUT_SEC]
            for s in stale:
                _browsers_registry.remove(s)
        for s in stale:
            _close_slot(s)
            logger.info("Playwright browser swept after %.0fs idle (thread=%s)",
                        now - s['last_used'], s.get('thread_name', '?'))


def _ensure_idle_sweep_thread() -> None:
    global _idle_sweep_started
    if _idle_sweep_started:
        return
    with _idle_sweep_lock:
        if _idle_sweep_started:
            return
        t = threading.Thread(
            target=_idle_sweep_loop, name='screenshot-browser-gc', daemon=True,
        )
        t.start()
        _idle_sweep_started = True


def _shutdown_all_browsers() -> None:
    with _browsers_registry_lock:
        slots = list(_browsers_registry)
        _browsers_registry.clear()
    for s in slots:
        _close_slot(s)


atexit.register(_shutdown_all_browsers)


def _get_persistent_browser():
    """Return a cached Chromium browser for this thread, launching if needed.

    Playwright sync API is thread-affine; the thread that called
    `chromium.launch()` must own every subsequent page/screenshot call.
    Reusing the same browser across requests saves ~2s of cold-start latency.
    """
    from playwright.sync_api import sync_playwright

    _ensure_idle_sweep_thread()
    slot = getattr(_browser_local, 'slot', None)
    if slot is not None:
        try:
            # Liveness probe — accessing .contexts raises if the browser is gone.
            _ = slot['browser'].contexts
            slot['last_used'] = time.time()
            return slot['browser']
        except Exception as e:
            logger.info("Playwright browser handle stale (%s), relaunching", e)
            with _browsers_registry_lock:
                if slot in _browsers_registry:
                    _browsers_registry.remove(slot)
            _browser_local.slot = None

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True)
    except Exception:
        # Stopping Playwright also stops the event loop it started on this
        # thread; leaving it running breaks every later request here. A
        # failure to stop must not hide the launch error being re-raised.
        try:
            pw.stop()
        except Exception:
            logger.warning("Playwright stop after a failed launch raised", exc_info=True)
        raise
    slot = {
        'pw': pw,
        'browser': browser,
        'last_used': time.time(),
        'thread_name': threading.current_thread().name,
    }
    _browser_local.slot = slot
    with _browsers_registry_lock:
        _browsers_registry.append(slot)
    logger.info("Playwright browser launched (thread=%s, total alive=%d)",
                slot['thread_name'], len(_browsers_registry))
    return browser

# Input validation patterns — prevent path traversal and header injection
_SAFE_ASSET_RE = re.compile(r'^[A-Za-z0-9_]{1,20}$')
_SAFE_TF_RE = re.compile(r'^[A-Za-z0-9]{1,5}$')
_SAFE_INDICATORS_RE = re.compile(r'^[A-Za-z0-9_,]{1,200}$')
# share_slug format mirrors carrac/shares/routes.py _SLUG_RE.
_SAFE_SHARE_SLUG_RE = re.compile(r'^[a-z0-9]{6,12}$')

# Complementary fix: the two parser-blocking <script> tags
# chart_headless.html loads (templates/chart_headless.html), fulfilled
# straight from the on-disk static/js/ directory instead of round-tripping
# through the app during a headless render — see
# _fulfill_static_script_from_disk below. Three `os.path.dirname` calls walk
# up from this file (carrac/reports/screenshots.py) to the repo root:
# carrac/reports -> carrac -> repo root, then join('static', 'js').
_STATIC_JS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'static', 'js',
)
_STATIC_SCRIPT_NAMES = ('klinecharts.min.js', 'carrac-indicator-pack.js')


def _fulfill_static_script_from_disk(route, request) -> None:
    """Playwright route handler: serve a chart-headless <script src> straight
    from static/js/ on disk instead of letting page.goto() issue a nested
    HTTP request back into this same app. Falls back to a real
    network fetch if the file can't be read (e.g. a path misconfiguration)
    rather than failing the render outright.
    """
    filename = request.url.rsplit('/', 1)[-1].split('?', 1)[0]
    file_path = os.path.join(_STATIC_JS_DIR, filename)
    try:
        with open(file_path, 'rb') as f:
            body = f.read()
    except OSError:
        logger.warning(
            "screenshot: local static fulfill miss for %s, falling back to network",
            filename,
        )
        route.continue_()
        return
    route.fulfill(status=200, content_type='application/javascript', body=body)


_DEFAULT_SCREENSHOT_INDICATORS = 'CPR,EMA_RIBBON,WEEKLY_MA,DAILY_EMA'

# Split timeout: page navigation is just an HTML template fetch, so bound it
# tight; the chart RENDER (fetch chart-internal + draw all indicators in
# headless) is the slow part and keeps the original 30s budget so renders still
# succeed. The bug was that goto used Playwright's IMPLICIT 30s default AND
# wait_for_function used 30s -> a stalled render held a worker ~60s == gunicorn
# --timeout 60 -> SIGTERM. Capping nav at 10s drops the worst case to ~40s
# (+overhead ~46s), safely under 60s, WITHOUT shrinking the render budget.
# (Combined with the serialized pre-render below, this stops the storm without
# breaking screenshot generation.)
SCREENSHOT_NAV_TIMEOUT_MS = 10000
SCREENSHOT_RENDER_TIMEOUT_MS = 30000

# Hard concurrency cap + bounded queue around the actual
# browser-render section (goto/wait_for_function/screenshot below) — disk
# cache HITS never reach this and so never queue. Running this service unbounded
# starves small hosts; even on a larger one the
# carrac web container is already CPU-limited to 3.0 cores (docker-compose.yml)
# — an unbounded pile of concurrent Playwright renders can still starve other
# requests sharing that same core budget even though the host itself survives.
#
# threading.BoundedSemaphore matches the execution model: screenshot_chart is
# a sync Flask/WSGI view running on asgiref's thread-pool bridge (see the
# teardown comment on _get_persistent_browser below), not a native async
# endpoint, so there is no event loop to hang an asyncio.Semaphore off of.
#
# Caveat: this cap is per gunicorn WORKER PROCESS (plain module state, no
# shared store). The Dockerfile runs 4 worker processes, so the true ceiling
# across the whole container is up to 4x this value. A cross-process cap
# would need a Redis-backed semaphore (see carrac/ai/chat_slot.py for that
# pattern) — out of scope for this fix; per-process bounding already turns an
# unbounded storm into a capped one.
SCREENSHOT_MAX_CONCURRENCY = int(os.getenv('SCREENSHOT_MAX_CONCURRENCY', '2'))
SCREENSHOT_QUEUE_TIMEOUT_SEC = float(os.getenv('SCREENSHOT_QUEUE_TIMEOUT', '30'))

_render_semaphore = threading.BoundedSemaphore(SCREENSHOT_MAX_CONCURRENCY)
_active_renders = 0
_active_renders_lock = threading.Lock()


class ChartRenderError(RuntimeError):
    """The headless chart page reported title=="ERROR" (a data/render-side
    failure surfaced by the chart JS itself), distinct from an infra/Playwright
    failure. Kept as its own exception so the route can still return the
    specific `#error` text instead of the generic busy/failure message."""


@screenshots_bp.route('/api/screenshot-proxy/<asset>/<timeframe>')
@limiter.limit("30 per minute")
def screenshot_proxy(asset: str, timeframe: str):
    """Session-authenticated proxy for chart screenshots.

    Used by the morning report HTML to avoid exposing the API key in the browser.
    Internally calls the keyed screenshot endpoint.
    """
    from flask import session as flask_session
    if 'user_id' not in flask_session:
        return jsonify({'error': 'Login required'}), 401

    if not _SAFE_ASSET_RE.match(asset) or not _SAFE_TF_RE.match(timeframe):
        return jsonify({'error': 'Invalid asset or timeframe'}), 400

    width = request.args.get('width', 640, type=int)
    height = request.args.get('height', 360, type=int)
    quality = request.args.get('quality', 40, type=int)

    # Serve from disk cache if available (same cache as screenshot_chart)
    cache_dir = os.path.join('data', 'screenshots')
    cache_file = os.path.join(cache_dir, f'{asset}_{timeframe}_{width}x{height}.jpg')
    try:
        if os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < 3600:
                with open(cache_file, 'rb') as f:
                    cached_bytes = f.read()
                return Response(
                    cached_bytes, mimetype='image/jpeg',
                    headers={
                        'Content-Disposition': f'inline; filename="{asset.upper()}_{timeframe.upper()}.jpg"',
                        'Cache-Control': 'public, max-age=300',
                    },
                )
    except Exception:
        pass

    # Cache miss — forward to internal keyed endpoint
    try:
        api_key = os.getenv('SCREENSHOT_API_KEY', '')
        resp = requests.get(
            f'http://localhost:5000/api/screenshot/{asset}/{timeframe}',
            params={'width': width, 'height': height, 'quality': quality},
            headers={'X-Internal-Key': api_key},
            timeout=45,
        )
        if resp.status_code == 200:
            return Response(
                resp.content, mimetype='image/jpeg',
                headers={
                    'Content-Disposition': f'inline; filename="{asset.upper()}_{timeframe.upper()}.jpg"',
                    'Cache-Control': 'public, max-age=300',
                },
            )
        return jsonify({'error': 'Screenshot unavailable'}), resp.status_code
    except Exception:
        return jsonify({'error': 'Screenshot generation failed'}), 500


@screenshots_bp.route('/api/screenshot/<asset>/<timeframe>')
@limiter.limit("10 per minute")
@csrf.exempt
def screenshot_chart(asset: str, timeframe: str):
    """Generate a chart screenshot using headless Playwright."""
    if request.args.get('key'):
        return jsonify({'error': 'API key via URL parameter is not allowed. Use X-API-Key header.'}), 400
    key = request.headers.get('X-Internal-Key', '') or request.headers.get('X-API-Key', '')
    if not verify_api_key(key):
        return jsonify({'error': 'Unauthorized'}), 401

    # Validate asset/timeframe to prevent path traversal (CWE-22)
    if not _SAFE_ASSET_RE.match(asset) or not _SAFE_TF_RE.match(timeframe):
        return jsonify({'error': 'Invalid asset or timeframe'}), 400

    indicators_param = request.args.get('indicators', '')
    if indicators_param:
        if not _SAFE_INDICATORS_RE.match(indicators_param):
            return jsonify({'error': 'Invalid indicators parameter'}), 400
        indicators = indicators_param
    else:
        indicators = _DEFAULT_SCREENSHOT_INDICATORS

    # An optional share slug routes the render through the
    # slug-scoped headless page (own frozen drawings burned into the JPEG)
    # instead of the plain asset/timeframe one.
    share_slug = request.args.get('share_slug', '').strip()
    if share_slug and not _SAFE_SHARE_SLUG_RE.match(share_slug):
        return jsonify({'error': 'Invalid share_slug parameter'}), 400

    width = min(max(request.args.get('width', 1920, type=int), 100), 3840)
    height = min(max(request.args.get('height', 1080, type=int), 100), 3840)
    quality = request.args.get('quality', 75 if width >= 1280 else 40, type=int)
    # Device-pixel ratio: render at `width`x`height` LOGICAL px but rasterise
    # at `scale`x device px. A retina render (scale=2) lets klinecharts lay
    # its axis/OHLCV/legend fonts out relative to a smaller logical canvas
    # (so they read bigger) while the output stays crisp at width*scale px.
    # Default 1 preserves the morning-bot / journal callers exactly. Capped so
    # the device-pixel output never exceeds the 3840/side raster ceiling.
    scale = min(max(request.args.get('scale', 1, type=int), 1), 4)
    if width * scale > 3840:
        scale = max(1, 3840 // width)

    # Disk cache: 1 hour TTL, keyed on asset+timeframe+size only.
    #
    # A slug-scoped render burns THAT share's own frozen
    # drawings into the JPEG, so it must NEVER read from or write to this
    # generic cache — the key has no slug component, so a later anonymous
    # request for the same asset/timeframe/size (e.g. the morning bot, or a
    # different user's share) would otherwise be served a cached image with
    # a stranger's private drawings baked in. Slug renders are already
    # cached per-share as that share's own card file
    # (render_capture_card_to_file), so skipping this cache costs nothing.
    cache_file = cache_file_png = None
    if not share_slug:
        cache_dir = os.path.join('data', 'screenshots')
        os.makedirs(cache_dir, exist_ok=True)
        # scale=1 keeps the historical filename (no orphaned caches); a retina
        # render gets its own @Nx-suffixed key so it can't collide with the 1x one.
        _suffix = '' if scale == 1 else f'@{scale}x'
        cache_file = os.path.join(cache_dir, f'{asset}_{timeframe}_{width}x{height}{_suffix}.jpg')
        cache_file_png = cache_file.replace('.jpg', '.png')
        try:
            for cf in [cache_file, cache_file_png]:
                if os.path.exists(cf):
                    age = time.time() - os.path.getmtime(cf)
                    if age < 3600:
                        mime = 'image/jpeg' if cf.endswith('.jpg') else 'image/png'
                        with open(cf, 'rb') as f:
                            cached_bytes = f.read()
                        return Response(
                            cached_bytes, mimetype=mime,
                            headers={
                                'Content-Disposition': f'inline; filename="{asset.upper()}_{timeframe.upper()}.jpg"',
                                'Cache-Control': 'public, max-age=300',
                                'X-Cache': 'HIT',
                            },
                        )
        except Exception:
            pass

    # Acquire a bounded render slot. Everything above this point
    # (auth, validation, disk-cache HIT) already returned — only an actual
    # Playwright render reaches here, so cache hits never queue.
    global _active_renders
    queue_wait_start = time.time()
    slot_acquired = _render_semaphore.acquire(timeout=SCREENSHOT_QUEUE_TIMEOUT_SEC)
    queue_wait = time.time() - queue_wait_start
    if not slot_acquired:
        logger.info(
            "screenshot: busy — queue timeout after %.1fs waiting for a render "
            "slot (asset=%s tf=%s max_concurrency=%d)",
            queue_wait, asset, timeframe, SCREENSHOT_MAX_CONCURRENCY,
        )
        resp = jsonify({'error': 'Screenshot service busy, please retry shortly'})
        resp.status_code = 503
        resp.headers['Retry-After'] = str(int(SCREENSHOT_QUEUE_TIMEOUT_SEC))
        return resp

    with _active_renders_lock:
        _active_renders += 1
        active_now = _active_renders
    logger.info(
        "screenshot: render start (asset=%s tf=%s queue_wait=%.2fs active=%d/%d)",
        asset, timeframe, queue_wait, active_now, SCREENSHOT_MAX_CONCURRENCY,
    )

    try:
        api_key = os.getenv('SCREENSHOT_API_KEY', '')
        render_kwargs = {'share_slug': share_slug} if share_slug else {}
        if scale != 1:
            render_kwargs['scale'] = scale
        screenshot_bytes = _render_screenshot_via_playwright(
            asset, timeframe, indicators, width, height, quality, api_key,
            **render_kwargs,
        )

        # Never write a slug-scoped render into the generic
        # asset+timeframe+size cache file (see the read-side comment above).
        if cache_file:
            try:
                with open(cache_file, 'wb') as f:
                    f.write(screenshot_bytes)
            except Exception:
                pass

        return Response(
            screenshot_bytes, mimetype='image/jpeg',
            headers={
                'Content-Disposition': f'inline; filename="{asset.upper()}_{timeframe.upper()}.jpg"',
                'Cache-Control': 'public, max-age=300',
                'X-Cache': 'BYPASS' if share_slug else 'MISS',
            },
        )

    except ChartRenderError as e:
        return jsonify({'error': f'Chart render failed: {e}'}), 500
    except ImportError:
        return jsonify({'error': 'Playwright not installed'}), 500
    except Exception as e:
        logger.error(f"Screenshot error for {asset}/{timeframe}: {e}")
        # Drop the cached browser on unexpected errors — if it crashed mid-op
        # the next call should get a clean one rather than compounding the
        # failure.
        _drop_cached_browser_for_this_thread()
        return jsonify({'error': 'Screenshot generation failed'}), 500
    finally:
        with _active_renders_lock:
            _active_renders -= 1
            active_now = _active_renders
        _render_semaphore.release()
        logger.info(
            "screenshot: render end (asset=%s tf=%s active=%d/%d)",
            asset, timeframe, active_now, SCREENSHOT_MAX_CONCURRENCY,
        )


def _render_screenshot_via_playwright(asset, timeframe, indicators, width, height,
                                       quality, api_key, share_slug=None, scale=1):
    """Launch/reuse the thread-local browser, navigate, and capture JPEG bytes.

    Raises ChartRenderError if the chart JS itself reports title=="ERROR", or
    lets any Playwright/infra exception (ImportError, TimeoutError, ...)
    propagate. Extracted out of screenshot_chart so the concurrency
    guard can wrap just this section, and so tests can monkeypatch a
    slow/fake render without a real headless browser.

    ``share_slug``: navigate to the slug-scoped headless page
    (``/chart-headless/share/<slug>``) instead of the plain
    ``/chart-headless/<asset>/<timeframe>`` one — that page renders the
    share's own frozen drawings server-side instead of the live
    (auth-mismatched, always-empty) ``/api/drawings`` fetch.
    """
    base_url = 'http://localhost:5000'
    # Key is sent via X-Internal-Key header on the browser context rather than
    # as a ?key= URL query param so it is never logged in uvicorn access logs.
    if share_slug:
        chart_url = f"{base_url}/chart-headless/share/{share_slug}?indicators={indicators}"
    else:
        chart_url = (
            f"{base_url}/chart-headless/{asset}/{timeframe}"
            f"?indicators={indicators}"
        )

    # The browser is torn down at the end of every request rather than cached
    # per thread. Playwright's sync API installs an asyncio event loop on the
    # calling thread and keeps it alive as long as the browser is alive; the
    # thread is later reused for unrelated requests (asgiref's WsgiToAsgi
    # bridge, or asyncio's default executor pool behind
    # fastapi_app/routers/screenshot_public.py's asyncio.to_thread front
    # door). asgiref's `sync_send` then trips its "no AsyncToSync in a running
    # loop" guard and 500s unrelated endpoints (preferences/all, drawings,
    # even static files). Cost: ~2-3s of Playwright cold-launch per request.
    # The alternative is a playwright.async_api port (deferred).
    #
    # The teardown does not care which pool owns the thread; it drops whatever
    # Playwright state it finds on `threading.current_thread()`, and it must
    # fire on every exit path (below). Pinned by tests/fastapi/
    # test_screenshot_public.py::TestThreadTeardownSurvivesAsyncioToThread,
    # which renders once, then issues a second to_thread-routed request forced
    # onto the SAME worker thread and asserts it does not fail.
    browser = _get_persistent_browser()
    context = None
    try:
        context = browser.new_context(
            viewport={'width': width, 'height': height},
            device_scale_factor=scale,
            extra_http_headers={'X-Internal-Key': api_key},
        )
        page = context.new_page()
        # Complementary fix: page.goto() below fetches the
        # chart-headless HTML PLUS its two parser-blocking <script> tags as
        # separate nested HTTP requests back into this same app. Fulfilling
        # those two scripts straight from local disk removes 2 of the 3 nested
        # requests outright, shrinking the window in which a render could still
        # collide with its own nested fetch even with the front door in place.
        for _script_name in _STATIC_SCRIPT_NAMES:
            page.route(f"**/static/js/{_script_name}", _fulfill_static_script_from_disk)
        page.goto(chart_url, timeout=SCREENSHOT_NAV_TIMEOUT_MS)
        page.wait_for_function(
            'document.title === "READY" || document.title === "ERROR"',
            timeout=SCREENSHOT_RENDER_TIMEOUT_MS,
        )
        if page.title() == 'ERROR':
            error_text = page.text_content('#error') or 'Unknown error'
            raise ChartRenderError(error_text)

        return page.screenshot(type='jpeg', quality=quality)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        # See comment above. Tears down Playwright's
        # asyncio loop so the asgiref WSGI worker thread is clean for
        # any non-screenshot request that lands on it next.
        _drop_cached_browser_for_this_thread()


def _drop_cached_browser_for_this_thread() -> None:
    slot = getattr(_browser_local, 'slot', None)
    if slot is None:
        return
    with _browsers_registry_lock:
        if slot in _browsers_registry:
            _browsers_registry.remove(slot)
    _close_slot(slot)
    _browser_local.slot = None
