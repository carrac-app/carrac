"""Chart front door.

``GET /api/chart/<symbol>`` (``carrac/charts/routes.py::get_chart_data``,
``@limiter.limit("120 per minute")`` + ``@auth_or_key_required``) is the
hottest interactive endpoint in the app, but every Flask request in a
gunicorn worker serialises onto asgiref's single-thread executor
(``asgi.py``'s ``WsgiToAsgi`` wrap of the Flask app — see
``asgiref.sync.SyncToAsync.single_thread_executor``, ``max_workers=1``):
4 concurrent Flask requests SYSTEM-WIDE across the whole process. One cold
1-6s chart build (the SWR cache only helps once an entry exists)
head-of-line-blocks every other Flask request in that worker.

This module is a thin FastAPI "front door" — it does NOT reimplement the
Flask view. It:

1. Resolves the caller's identity FastAPI-side via ``auth_or_key`` — the
   EXACT session-cookie + API-key dependency the other migrated authed
   routes use (``scanner.py``, ``chart_internal.py``, ``facts.py``, ...).
   Unauthenticated behaviour matches ``auth_or_key_required`` in
   ``carrac/auth/decorators.py`` byte-for-byte in effect:
     * ``?key=`` URL param             -> 400 JSON (deprecated)
     * valid session cookie            -> pass
     * valid ``X-Internal-Key``/``X-API-Key`` -> pass
     * neither, JSON/API client        -> 401 JSON ``{"error": "Authentication required"}``
     * neither, browser client         -> 302 redirect to ``/login``
   (see ``carrac/fastapi_app/deps/auth.py::auth_or_key``/``_raise_auth_required``).
2. Applies the SAME 120/minute rate limit FastAPI-side, off the asgiref
   bottleneck, via the shared ``AsyncOffloadLimiter``
   (``carrac/fastapi_app/limiter.py`` — the Redis storage hit runs in a
   thread, not inline on the event loop).
3. Replays the UNCHANGED Flask view via ``asyncio.to_thread`` — a fresh
   ``flask_app.test_request_context(...)`` per call, with the
   already-resolved identity transplanted into it (see
   ``_replay_flask_chart`` below), then converts the Flask ``Response``
   into a FastAPI one. Every byte of the existing cache/SWR/tier-gating
   logic keeps running exactly as it does today — this router
   only gets it off the single-thread choke point so 4 chart requests can
   run concurrently across the anyio thread pool instead of serialising
   one at a time through asgiref.

Response-middleware header parity (verified, not just assumed):
``CacheHeadersMiddleware`` (``fastapi_app/cache_middleware.py``) already
skips any path starting with ``/api/chart/`` — chart handlers "manage their
own Cache-Control" (same skip Flask's own ``apply_cache_headers`` uses) — so
it never overwrites the Cache-Control / X-Carrac-Stale / X-Carrac-Age headers
this router forwards from the Flask response. ``ResponseHeadersMiddleware``
only ADDS security headers the handler hasn't already set (idempotent,
``if name not in existing``) — no conflict. ``CarracCSRFMiddleware``
skips all safe methods (GET/HEAD/OPTIONS) before it even reads
``/api/chart``'s entry in its skip-prefix list. There is no gzip (or other
body-rewriting) middleware anywhere in this ASGI stack today (verified via
grep) — nothing to double-apply.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from carrac.charts.routes import _chart_via_asgi_replay, get_chart_data
from carrac.fastapi_app.deps.auth import RateCtxDep, auth_or_key
from carrac.fastapi_app.limiter import limiter

logger = logging.getLogger("carrac.fastapi.chart_public")

router = APIRouter()

# Response headers that must NOT be forwarded verbatim — Starlette's
# ``Response`` recomputes Content-Length from the actual body we hand it
# (the Flask process may have chunked/finalised the body differently), and
# there is no gzip/Content-Encoding anywhere in this stack (see module
# docstring) so any stray Encoding/Transfer-Encoding header is stale noise
# from the Flask WSGI layer, not something a client should see twice.
_DROP_HEADERS = frozenset({"content-length", "content-encoding", "transfer-encoding", "connection"})

# Forwarded verbatim into the Flask replay so its OWN auth_or_key_required
# re-check (see module docstring — the replay calls the fully-decorated
# Flask view, not a bare function) resolves the SAME caller a per-user or
# shared API key already authenticated FastAPI-side. Session-cookie callers
# don't need this — their identity is transplanted directly into the
# replayed Flask session (see ``_replay_flask_chart``).
_FORWARD_AUTH_HEADERS = ("X-Internal-Key", "X-API-Key")
# The frontend tags background preloads so a cold build on that
# path logs as `preload`, not a blocking `miss`. The hint has to survive the
# replay or every preload would count as a user waiting.
_FORWARD_HINT_HEADERS = ("X-Carrac-Preload",)


def _replay_flask_chart(
    symbol: str,
    query_string: str,
    forwarded_headers: dict[str, str],
    identity_source: str,
    user_id: Optional[int],
    tier: str,
) -> tuple[int, bytes, dict[str, str]]:
    """Run the UNCHANGED Flask view in a fresh request context.

    Runs on a worker thread via ``asyncio.to_thread`` — never call this
    directly from the event loop (``flask_app.test_request_context`` and the
    Flask view underneath are fully synchronous, including blocking pandas/
    Binance/Redis calls on a cache miss).

    Identity transplant — read what the Flask decorator chain actually
    consults for THIS route (verified via grep, not assumed):
      * ``auth_or_key_required`` (carrac/auth/decorators.py) only checks
        ``'user_id' in session`` for the session branch, then a forwarded
        API-key header for the other.
      * ``get_request_tier()`` reads ``session.get('user_id')`` then
        ``session.get('tier', 'free')`` FIRST; only falls back to
        ``flask.g.rate_tier`` (normally set by Flask's own before_request,
        which this replay — like the existing SWR replay —
        intentionally does NOT run) or a hard-coded 'admin' guess for a
        verified header key.
      * ``get_tier_limits()`` reads ``session.get('user_id')`` to apply
        per-user tier overrides.
    So for a session-authenticated caller we set ``session['user_id']`` +
    ``session['tier']`` — sufficient for all three call sites above. For an
    API-key-authenticated caller we do NOT touch the session (production API
    key traffic never has one either — setting it would erroneously start
    applying per-user tier overrides that real API-key requests never get);
    instead we set ``flask.g.rate_tier`` directly to the SAME tier value
    Flask's own before_request ``classify_request()`` would have set,
    because both runtimes' classification is backed by the identical
    ``carrac.auth.rate_classify._classify_pure`` — the FastAPI-resolved tier
    passed in here IS what Flask would have computed on its own.
    """
    # Late import: carrac.asgi imports carrac.fastapi_app (which imports this
    # router module via fastapi_app/main.py) at module scope — importing it
    # back from here at module scope would be circular. Safe to import
    # lazily inside the call because by the time any request is actually
    # served, carrac.asgi has long finished importing.
    from carrac.asgi import flask_app

    path = f"/api/chart/{symbol}"
    if query_string:
        path = f"{path}?{query_string}"

    try:
        with flask_app.test_request_context(path, headers=forwarded_headers):
            from flask import g, session

            if identity_source == "session" and user_id is not None:
                session["user_id"] = user_id
                session["tier"] = tier
            elif identity_source == "api_key":
                g.rate_tier = tier

            _chart_via_asgi_replay.active = True
            try:
                rv = get_chart_data(symbol)
            finally:
                _chart_via_asgi_replay.active = False

            flask_response = flask_app.make_response(rv)
            status_code = flask_response.status_code
            body = flask_response.get_data()
            out_headers = {
                k: v for k, v in flask_response.headers.items()
                if k.lower() not in _DROP_HEADERS
            }
        return status_code, body, out_headers
    except Exception:  # noqa: BLE001 — mirror get_chart_data's own 500 shape
        logger.exception("chart front door replay failed for %s", symbol)
        error_body = json.dumps(
            {"error": "Failed to load chart data", "symbol": symbol.upper()}
        ).encode("utf-8")
        return 500, error_body, {"Content-Type": "application/json"}


@router.get("/api/chart/{symbol}")
@limiter.limit("120/minute")
async def get_chart_data_front_door(
    request: Request,
    symbol: str,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> Response:
    """Front door for ``GET /api/chart/<symbol>`` — see module docstring."""
    identity_source: str = auth["source"]
    if identity_source == "session":
        user_id: Optional[Any] = auth["user"]["id"]
    else:  # "api_key" — user_id is None for the shared admin/internal key
        user_id = auth.get("user_id")
    tier: str = auth["tier"]

    forwarded_headers: dict[str, str] = {}
    for header_name in _FORWARD_AUTH_HEADERS + _FORWARD_HINT_HEADERS:
        value = request.headers.get(header_name)
        if value:
            forwarded_headers[header_name] = value

    status_code, body, headers = await asyncio.to_thread(
        _replay_flask_chart,
        symbol,
        request.url.query,
        forwarded_headers,
        identity_source,
        user_id,
        tier,
    )

    return Response(content=body, status_code=status_code, headers=headers)
