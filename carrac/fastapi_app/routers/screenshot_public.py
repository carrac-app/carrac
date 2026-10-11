"""Screenshot / chart-headless front door.

CONFIRMED ROOT CAUSE: asgiref's ``WsgiToAsgi`` wraps the WHOLE Flask app
behind a single shared per-worker thread
(``asgiref.sync.SyncToAsync.single_thread_executor``, ``max_workers=1`` —
see ``chart_public.py``'s module docstring for the full mechanics: 4
gunicorn workers = 4 concurrent Flask requests SYSTEM-WIDE). The screenshot
render (``GET /api/screenshot/<asset>/<timeframe>``,
``carrac/reports/screenshots.py::screenshot_chart``) is itself a sync Flask
view that HOLDS that one shared thread for its entire multi-second duration
while Playwright's ``page.goto()`` issues NESTED HTTP requests back into the
SAME app — the headless chart page (``/chart-headless/<asset>/<timeframe>``,
or ``/chart-headless/share/<slug>`` for a share render) plus its
two render-blocking static ``<script>`` fetches. Any nested request that
lands on the already-wedged worker thread self-starves: ``page.goto()``
times out waiting for a response that can never arrive, because the ONE
thread able to serve it is the very thread blocked waiting on it. asgiref
serialises Flask onto one thread per worker, so this is fatal under real
screenshot traffic.

The remedy (mirrors the chart front door, ``chart_public.py``): give all
THREE routes their own FastAPI entry point that replays the UNCHANGED Flask
view via ``asyncio.to_thread`` — each replay runs on its own OS thread from
asyncio's default executor, never on asgiref's single shared thread, so a
render in flight can no longer block the very nested request it depends on.
This module does NOT reimplement any of the three Flask views — every byte
of existing logic (the API-key checks, the disk cache, the
concurrency semaphore + 503/Retry-After busy response, the
share-slug handling and its 404/410 semantics, the thread-local
Playwright-browser teardown) keeps running exactly as it does today.

Unlike ``chart_public.py``, none of these three routes has session-cookie
auth to transplant — ``screenshot_chart``, ``chart_headless``, and
``chart_headless_share`` are ALL API-key-only (each does its own inline
``verify_api_key(...)`` check; no ``auth_or_key_required`` session branch
anywhere in this trio). So there is no FastAPI-side identity-resolution
step here: we just forward the caller's auth headers into a fresh
``flask_app.test_request_context(...)`` and call the already-decorated
Flask view function directly — the SAME replay idiom the chart front door uses, minus
the session-transplant machinery this trio doesn't need.

Auth-header parity — verified per view, not assumed:
  * ``screenshot_chart`` reads ``X-Internal-Key`` OR ``X-API-Key`` (see
    ``carrac/reports/screenshots.py`` — widened it to accept
    ``X-Internal-Key`` first, same fallback to ``X-API-Key``) — and still
    explicitly 400s a ``?key=`` URL param.
  * ``chart_headless`` / ``chart_headless_share`` read
    ``X-Internal-Key`` OR ``X-API-Key`` (``chart_headless`` also accepts a
    deprecated ``?key=`` fallback with a WARN log, unlike ``screenshot_chart``).
Both header names are always forwarded here regardless of which view is
being replayed — each view's own inline check decides which one it
actually honours, so this front door cannot silently widen or narrow any
view's real auth surface.

Rate-limit parity — ``screenshot_chart`` carries its own
``@limiter.limit("10 per minute")`` (Flask-Limiter, ``carrac.extensions
.limiter``); ``chart_headless``/``chart_headless_share`` carry none.
Verified (not assumed) how this behaves under a direct call rather than
Flask's normal request cycle: ``flask_limiter``'s ``LimitDecorator.__call__``
wraps the view function ITSELF in a closure that calls
``self.limiter._check_request_limit(...)`` before invoking the real view —
this is separate from (and in addition to) the ``app.before_request`` hook
the same library also registers, so calling ``screenshot_chart(asset,
timeframe)`` directly here, exactly as this module does, DOES enforce the
limit — confirmed with an isolated probe app (Flask-Limiter's own
``before_request``/``after_request`` hooks do NOT run either way, since
``test_request_context`` never triggers Flask's full dispatch cycle, but the
inline wrapper check fires regardless). Two consequences follow:
  1. ``carrac.extensions.limiter``'s key function falls back to
     ``get_remote_address()`` (``request.remote_addr``), and
     ``test_request_context`` without an explicit override defaults
     ``REMOTE_ADDR`` to a synthetic constant — which would silently collapse
     EVERY distinct real caller replayed through this front door onto one
     shared 10/minute bucket. ``_replay_flask_screenshot`` threads the
     FastAPI-resolved real client IP into ``environ_overrides`` so the
     replayed check keys correctly (verified with an isolated probe: two
     different ``REMOTE_ADDR`` overrides get independent buckets).
  2. When the limit IS hit, the inline wrapper raises ``RateLimitExceeded``
     (a ``werkzeug.exceptions.HTTPException`` / 429) directly — normally
     Flask's ``full_dispatch_request`` converts that into a response, but
     this replay never calls that, so ``_replay_flask_screenshot`` catches
     ``HTTPException`` explicitly and renders it via its own
     ``get_response()`` (verified byte-for-byte identical to what full Flask
     dispatch produces for the same limit — this app registers no custom 429
     handler and leaves ``RATELIMIT_HEADERS_ENABLED`` unset, so there is
     nothing extra to lose either way) rather than letting it fall through
     to the generic exception handler below, which would otherwise turn a
     legitimate 429 into a misleading 500.
No new FastAPI-side rate limiter is added here — the existing Flask-side one
already runs correctly once threaded the real client IP, and doubling it up
would need the same exempt_when-style double-counting guard the chart front door added
for ``/api/chart``, for no benefit: unlike ``/api/chart``'s Redis-backed
slowapi check (a genuine blocking-the-event-loop concern for
``async def`` routes), this whole call is already off the event loop by
construction (``asyncio.to_thread``).

Complementary fix (see ``carrac/reports/screenshots.py``):
``_render_screenshot_via_playwright`` now fulfils its own two static
``<script>`` requests (``klinecharts.min.js``, ``carrac-indicator-pack.js``)
directly from local disk via Playwright's ``page.route()``, removing 2 of
the 3 nested requests outright. Only the ``/chart-headless...`` HTML
request itself still round-trips through this front door.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import Response
from werkzeug.exceptions import HTTPException

logger = logging.getLogger("carrac.fastapi.screenshot_public")

router = APIRouter()

# See chart_public.py's identical constant/rationale — Starlette's Response
# recomputes Content-Length from the body we hand it, and there is no
# gzip/Content-Encoding anywhere in this ASGI stack, so a stray Encoding
# header forwarded from the Flask WSGI layer would be stale noise.
_DROP_HEADERS = frozenset({"content-length", "content-encoding", "transfer-encoding", "connection"})

# Forwarded verbatim into every replay below. Which header (if either) a
# given view actually honours is entirely that view's own inline check —
# see the auth-header-parity note in the module docstring.
_FORWARD_AUTH_HEADERS = ("X-Internal-Key", "X-API-Key")


def _forwarded_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name in _FORWARD_AUTH_HEADERS:
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


def _client_ip(request: Request) -> str:
    """Best-effort real caller IP for the replayed Flask-Limiter check.

    ``ProxyHeadersMiddleware(trusted_hosts="127.0.0.1")`` (fastapi_app/
    main.py) already rewrites ``request.client`` from a trusted single-hop
    X-Forwarded-For before this router ever sees the request — same
    resolution every other migrated authed route relies on. Falls back to
    the loopback address (test_request_context's own synthetic default)
    if, for any reason, no client address is present on the ASGI scope —
    matching, not worsening, the pre-front-door behaviour in that edge case.
    """
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def _flask_response_parts(flask_response) -> tuple[int, bytes, dict[str, str]]:
    status_code = flask_response.status_code
    body = flask_response.get_data()
    out_headers = {
        k: v for k, v in flask_response.headers.items()
        if k.lower() not in _DROP_HEADERS
    }
    return status_code, body, out_headers


# ---------------------------------------------------------------------------
# GET /chart-headless/share/{slug} — slug-scoped render. Declared
# (and therefore matched) BEFORE the plain /chart-headless/{asset}/{timeframe}
# route below: both are two-segment paths under /chart-headless/, and
# Starlette resolves ambiguous path-param routes by REGISTRATION ORDER, not
# by static-vs-dynamic specificity the way Werkzeug's Flask url_map does.
# Registering the more specific "share/{slug}" shape first is what makes
# GET /chart-headless/share/abc123 resolve here instead of being
# mis-captured as asset="share", timeframe="abc123" by the route below.
# ---------------------------------------------------------------------------


def _replay_flask_chart_headless_share(
    slug: str,
    forwarded_headers: dict[str, str],
) -> tuple[int, bytes, dict[str, str]]:
    """Run the UNCHANGED chart_headless_share Flask view in a fresh request
    context. Runs on a worker thread via asyncio.to_thread — never call
    directly from the event loop."""
    from carrac.asgi import flask_app
    from carrac.charts.routes import chart_headless_share

    path = f"/chart-headless/share/{slug}"
    try:
        with flask_app.test_request_context(path, headers=forwarded_headers):
            rv = chart_headless_share(slug)
            flask_response = flask_app.make_response(rv)
            return _flask_response_parts(flask_response)
    except Exception:  # noqa: BLE001
        logger.exception("chart-headless-share front door replay failed for slug=%s", slug)
        return 500, b"Internal Server Error", {"Content-Type": "text/plain; charset=utf-8"}


@router.get("/chart-headless/share/{slug}")
async def chart_headless_share_front_door(request: Request, slug: str) -> Response:
    """Front door for GET /chart-headless/share/<slug> — see module docstring."""
    status_code, body, headers = await asyncio.to_thread(
        _replay_flask_chart_headless_share, slug, _forwarded_headers(request),
    )
    return Response(content=body, status_code=status_code, headers=headers)


# ---------------------------------------------------------------------------
# GET /chart-headless/{asset}/{timeframe} — plain (non-share) headless page.
# ---------------------------------------------------------------------------


def _replay_flask_chart_headless(
    asset: str,
    timeframe: str,
    query_string: str,
    forwarded_headers: dict[str, str],
) -> tuple[int, bytes, dict[str, str]]:
    """Run the UNCHANGED chart_headless Flask view in a fresh request
    context. Runs on a worker thread via asyncio.to_thread — never call
    directly from the event loop."""
    from carrac.asgi import flask_app
    from carrac.charts.routes import chart_headless

    path = f"/chart-headless/{asset}/{timeframe}"
    if query_string:
        path = f"{path}?{query_string}"
    try:
        with flask_app.test_request_context(path, headers=forwarded_headers):
            rv = chart_headless(asset, timeframe)
            flask_response = flask_app.make_response(rv)
            return _flask_response_parts(flask_response)
    except Exception:  # noqa: BLE001
        logger.exception("chart-headless front door replay failed for %s/%s", asset, timeframe)
        return 500, b"Internal Server Error", {"Content-Type": "text/plain; charset=utf-8"}


@router.get("/chart-headless/{asset}/{timeframe}")
async def chart_headless_front_door(request: Request, asset: str, timeframe: str) -> Response:
    """Front door for GET /chart-headless/<asset>/<timeframe> — see module docstring."""
    status_code, body, headers = await asyncio.to_thread(
        _replay_flask_chart_headless,
        asset, timeframe, request.url.query, _forwarded_headers(request),
    )
    return Response(content=body, status_code=status_code, headers=headers)


# ---------------------------------------------------------------------------
# GET /api/screenshot/{asset}/{timeframe} — the actual Playwright render.
# ---------------------------------------------------------------------------


def _replay_flask_screenshot(
    asset: str,
    timeframe: str,
    query_string: str,
    forwarded_headers: dict[str, str],
    remote_addr: str,
) -> tuple[int, bytes, dict[str, str]]:
    """Run the UNCHANGED screenshot_chart Flask view in a fresh request
    context — this IS the long-running Playwright render (the
    semaphore/queue lives inside screenshot_chart, unchanged), so it must
    run via asyncio.to_thread and never be awaited inline.

    ``remote_addr`` is threaded into the replay's WSGI environ so
    screenshot_chart's own ``@limiter.limit("10 per minute")`` keys off the
    REAL caller — see the rate-limit-parity note in the module docstring.
    """
    from carrac.asgi import flask_app
    from carrac.reports.screenshots import screenshot_chart

    path = f"/api/screenshot/{asset}/{timeframe}"
    if query_string:
        path = f"{path}?{query_string}"

    try:
        with flask_app.test_request_context(
            path,
            headers=forwarded_headers,
            environ_overrides={"REMOTE_ADDR": remote_addr},
        ):
            rv = screenshot_chart(asset, timeframe)
            flask_response = flask_app.make_response(rv)
            return _flask_response_parts(flask_response)
    except HTTPException as exc:
        # screenshot_chart's own @limiter.limit("10 per minute") decorator
        # wraps the view in a call that raises RateLimitExceeded (a werkzeug
        # HTTPException — TooManyRequests/429) DIRECTLY when the limit is
        # hit, regardless of whether Flask's before_request/after_request
        # dispatch cycle ever runs (verified: flask_limiter's LimitDecorator
        # .__call__ wraps the view itself, not just a before_request hook —
        # so calling screenshot_chart(...) here, exactly like calling it via
        # normal Flask dispatch, raises it). Werkzeug HTTPExceptions are
        # self-rendering (get_response()) — verified byte-for-byte identical
        # to what full Flask dispatch produces for the SAME limit here (no
        # custom 429 error handler is registered on this app, and
        # RATELIMIT_HEADERS_ENABLED is unset, so there are no extra
        # X-RateLimit-*/Retry-After headers to lose either way). Converting
        # here avoids the generic except-Exception handler below turning a
        # legitimate 429 into a misleading 500 "Screenshot generation failed".
        werkzeug_response = exc.get_response()
        body = werkzeug_response.get_data()
        out_headers = {
            k: v for k, v in werkzeug_response.headers.items()
            if k.lower() not in _DROP_HEADERS
        }
        return werkzeug_response.status_code, body, out_headers
    except Exception:  # noqa: BLE001 — mirror screenshot_chart's own 500 shape
        logger.exception("screenshot front door replay failed for %s/%s", asset, timeframe)
        error_body = json.dumps({"error": "Screenshot generation failed"}).encode("utf-8")
        return 500, error_body, {"Content-Type": "application/json"}


@router.get("/api/screenshot/{asset}/{timeframe}")
async def screenshot_chart_front_door(request: Request, asset: str, timeframe: str) -> Response:
    """Front door for GET /api/screenshot/<asset>/<timeframe> — see module docstring."""
    status_code, body, headers = await asyncio.to_thread(
        _replay_flask_screenshot,
        asset, timeframe, request.url.query,
        _forwarded_headers(request), _client_ip(request),
    )
    return Response(content=body, status_code=status_code, headers=headers)
