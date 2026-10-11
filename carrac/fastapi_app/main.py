"""FastAPI application instance for the Carrac strangler-fig migration."""

import logging
import time as _time_module
from contextlib import asynccontextmanager

import anyio
import carrac as _carrac_pkg
from fastapi import FastAPI
from slowapi.middleware import SlowAPIMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from changelog import APP_VERSION
from carrac.fastapi_app import exception_handlers
from carrac.fastapi_app.cache_middleware import CacheHeadersMiddleware
from carrac.fastapi_app.csrf_middleware import CarracCSRFMiddleware
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.middleware import ResponseHeadersMiddleware
from carrac.fastapi_app.routers import (
    admin_billing_json,
    admin_billing_overview_json,
    admin_intents_json,
    admin_usage_json,
    admin_waitlist_json,
    ai,
    ai_aux,
    alerts,
    billing_paddle,
    chart_internal,
    chart_public,
    chart_share,
    composite,
    curl_charts,
    dashboard_status,
    drawings,
    facts,
    journal,
    journal_ai,
    liq_heatmap,
    mcp as mcp_router,
    migrated,
    plays,
    portfolio,
    preferences,
    public,
    reports,
    scanner,
    screenshot_public,
    watchlist,
    widget,
    whoami,
)
from carrac.fastapi_app.session_middleware import CarracSessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


_fa_logger = logging.getLogger("carrac.fastapi_app.main")


class SlowRequestMiddleware:
    """Emit a WARNING for requests that exceed SLOW_REQUEST_THRESHOLD_S.

    Raw ASGI middleware (not BaseHTTPMiddleware) consistent with the other
    raw middlewares in this module (ResponseHeadersMiddleware,
    CacheHeadersMiddleware). Reads SLOW_REQUEST_THRESHOLD_S from the
    ``carrac`` package so both stacks share one env-tunable constant.
    Never raises — exceptions are swallowed so a logging failure cannot
    break a response. Reads ``_carrac_pkg.SLOW_REQUEST_THRESHOLD_S`` at
    call time so monkeypatch and env changes take effect immediately.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        t0 = _time_module.monotonic()
        status_code: int = 0

        async def send_capturing_status(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_capturing_status)
        finally:
            try:
                elapsed = _time_module.monotonic() - t0
                if elapsed > _carrac_pkg.SLOW_REQUEST_THRESHOLD_S:
                    method = scope.get("method", "")
                    path = scope.get("path", "")
                    _fa_logger.warning(
                        "SLOW %s %s %.2fs status=%s",
                        method,
                        path,
                        elapsed,
                        status_code,
                    )
            except Exception:
                pass


# Cap the anyio default thread-pool limiter at startup.
#
# FastAPI runs sync dependencies and sync route helpers via
# anyio.to_thread.run_sync(), which draws from this limiter.
# The default is 40 threads per UvicornWorker process.  With 4 workers
# and a 25-slot PostgreSQL pool, 40 × 4 = 160 potential simultaneous
# thread-pool threads > 25 slots — the root cause of pool exhaustion
# under bursty composite/MCD traffic.
#
# We cap at 10 per worker (4 workers × 10 = 40, well above the 25-slot
# pool but not all threads hold DB connections simultaneously — many hit
# only Redis or in-process caches).  The composite endpoint spawns up to
# 8 parallel to_thread tasks, so a cap below 8 would force some queuing
# there.  10 gives headroom for composite parallelism while still
# preventing the 160-thread worst-case that caused pool exhaustion.
# Raise this cap only if profiling shows it is the bottleneck, and
# raise the pool size in lock-step if concurrent DB use also grows.
_THREAD_LIMITER_TOKENS = 10


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Set per-worker anyio thread-pool cap on startup."""
    limiter_obj = anyio.to_thread.current_default_thread_limiter()
    limiter_obj.total_tokens = _THREAD_LIMITER_TOKENS
    import logging
    logging.getLogger("carrac.fastapi_app.main").info(
        f"anyio thread limiter set to {_THREAD_LIMITER_TOKENS} tokens/worker"
    )
    yield


fastapi_app = FastAPI(
    title="Carrac API (FastAPI)",
    version=APP_VERSION,
    # The published API contract is the curated Flask-built spec at
    # /api/openapi.json (+ Swagger UI at /api/docs). FastAPI's generated
    # spec lists every route, admin and internal ones included, so it is
    # not served.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)

# slowapi reads the limiter from app.state.
fastapi_app.state.limiter = limiter

# Strangler-routed paths get the same security-header set Flask emits
# via its after_request hook, plus Cache-Control via the
# middleware (which mirrors the Flask cache_headers.apply_cache_headers
# policy table).
#
# Middleware ordering note: ``add_middleware`` is LIFO at request time, so
# the LAST registration becomes the OUTERMOST layer.
#
# At REQUEST time (outer→inner): cache → session → response-headers → handler.
# At RESPONSE time (handler→outer): handler → response-headers → session → cache.
#
# CacheHeadersMiddleware is added LAST so it sees the response headers AFTER
# inner layers (and the handler) have set theirs — letting the
# ``if b"cache-control" not in existing`` idempotency guard skip handlers
# that set their own headers.
#
# ``CarracCSRFMiddleware`` is inserted BETWEEN ResponseHeaders
# and Session. This is correct under LIFO: Session must populate
# ``state.session`` before CSRF reads it, which means Session must wrap
# CSRF (added later than CSRF). At request time the order becomes
# ``Cache → Session → CSRF → ResponseHeaders → handler``.
fastapi_app.add_middleware(ResponseHeadersMiddleware)
fastapi_app.add_middleware(CarracCSRFMiddleware)
fastapi_app.add_middleware(CarracSessionMiddleware)
fastapi_app.add_middleware(SlowAPIMiddleware)
fastapi_app.add_middleware(CacheHeadersMiddleware)
# Pin to the nginx hop (127.0.0.1) for XFF-spoofing defense-in-depth.
# nginx binds to 127.0.0.1:5002 and proxies here; uvicorn sees the peer as
# 127.0.0.1. With a single trusted hop, ProxyHeadersMiddleware walks XFF
# right-to-left and stops at the first untrusted entry, yielding the real
# client IP (nginx's $remote_addr, already CF-resolved) rather than any
# attacker-controlled prefix. Do not widen to "*".
fastapi_app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="127.0.0.1")
# Slow-request logger. Added before ProxyHeadersMiddleware (LIFO →
# outermost after ProxyHeaders) so it times the full application stack
# (session decode, handler, response encoding) with the real client IP
# already rewritten by ProxyHeadersMiddleware.
fastapi_app.add_middleware(SlowRequestMiddleware)
# ``SlowAPIMiddleware`` is added so per-route
# ``@limiter.limit`` decorators on the new write handlers (which return
# explicit ``JSONResponse`` instances) fire correctly. The read
# routes still return dicts and therefore stay un-decorated for now —
# their pool-level limits in ``carrac/rest_rate_limit.py::LIMITS_REST``
# remain the ceiling. Adding response models to those routes (and
# decorating them) is a follow-up.

exception_handlers.install(fastapi_app)

fastapi_app.include_router(public.router)
fastapi_app.include_router(migrated.router)
fastapi_app.include_router(whoami.router)
# Read-mostly authed endpoints.
fastapi_app.include_router(composite.router)
fastapi_app.include_router(plays.router)
fastapi_app.include_router(scanner.router)
fastapi_app.include_router(liq_heatmap.router)
fastapi_app.include_router(reports.router)
fastapi_app.include_router(dashboard_status.router)
# Journal writes (non-AI subset).
fastapi_app.include_router(journal.router)
# Portfolio page read endpoints.
fastapi_app.include_router(portfolio.router)
# Watchlist, widget token, and preferences writes.
fastapi_app.include_router(watchlist.router)
fastapi_app.include_router(widget.router)
fastapi_app.include_router(preferences.router)
# Alerts and drawings writes.
fastapi_app.include_router(alerts.router)
fastapi_app.include_router(drawings.router)
# SSE + AI write paths. /api/ai/chat is the capacity win (asyncio
# task per stream vs. gthread thread per stream); the journal AI sidekicks
# port together so Flask serves zero LLM-routed traffic.
fastapi_app.include_router(ai.router)
fastapi_app.include_router(ai_aux.router)
fastapi_app.include_router(journal_ai.router)
# MCP JSON-RPC 2.0 endpoint. Same surface as the Flask blueprint
# (POST /mcp); per-call dispatch wrapped in asyncio.to_thread so sync tool
# handlers don't block the loop.
fastapi_app.include_router(mcp_router.router)
# Paddle webhook (HMAC signed) + admin/billing
# JSON cleanup (security-status, admin costs/assets, feedback, csp-report,
# billing topup-check/status/portal) + chart-internal (API-key auth,
# heavy logic extracted to carrac/charts/service.py).
fastapi_app.include_router(billing_paddle.router)
fastapi_app.include_router(admin_billing_json.router)
# Admin panel dashboards (billing overview, waitlist mgmt, API/MCP usage).
fastapi_app.include_router(admin_billing_overview_json.router)
fastapi_app.include_router(admin_waitlist_json.router)
fastapi_app.include_router(admin_usage_json.router)
# Admin MCP request-intent telemetry (live feed + unmet needs).
fastapi_app.include_router(admin_intents_json.router)
fastapi_app.include_router(chart_internal.router)
fastapi_app.include_router(facts.router)
# GET /api/chart/{symbol} front door (session/API-key auth,
# identity + rate-limit resolved here, then the UNCHANGED Flask view is
# replayed via asyncio.to_thread off the asgiref single-thread bottleneck).
# No method overlap with chart_share below (GET vs POST/DELETE on the
# "share"/"share/{slug}" literal segments), so registration order relative
# to it doesn't matter for correctness — see chart_public.py's docstring.
fastapi_app.include_router(chart_public.router)
# Chart shares — POST /api/chart/share +
# DELETE /api/chart/share/<slug>. Public reads stay on the Flask shares bp.
fastapi_app.include_router(chart_share.router)
# Screenshot + chart-headless front doors (see
# screenshot_public.py's module docstring for why they live here).
# The /chart-headless/share/{slug} route is declared before
# /chart-headless/{asset}/{timeframe} INSIDE that module, which is what
# actually determines match order (Starlette resolves ambiguous
# path-param routes by registration order); include_router() here just
# adds both to the app in that same order.
fastapi_app.include_router(screenshot_public.router)
# Curl chart endpoint — ANSI charts for terminal users. Registered LAST so
# the root-level /{symbol_spec} catch-all cannot shadow any other router's routes.
fastapi_app.include_router(curl_charts.router)
