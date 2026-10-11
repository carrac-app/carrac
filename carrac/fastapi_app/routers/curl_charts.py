"""Curl chart endpoint — ANSI charts for terminal user agents.

Reached ONLY via the asgi.py CurlChartPath dispatcher (plus exact paths
/:help and /:assets). Browsers get a 302 to the landing page.
"""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from carrac.curl_charts import content, registry
from carrac.curl_charts.grammar import GrammarError, parse_options, parse_path, snap_window
from carrac.curl_charts.render import DataUnavailable, RenderFailed, render_chart
from carrac.fastapi_app.limiter import limiter
from carrac.site_url import site_base_url, site_host

logger = logging.getLogger(__name__)
router = APIRouter()

_BROWSER_RE = re.compile(r"Mozilla|Chrome|Safari|Edg", re.IGNORECASE)


def _landing_302() -> str:
    return f"{site_base_url()}/?utm_source=terminal&utm_medium=curl&utm_campaign=curl_chart"


_TEXT = "text/plain; charset=utf-8"
_CACHE_HDR = {"Cache-Control": "public, max-age=60"}


def _is_browser(request: Request) -> bool:
    return bool(_BROWSER_RE.search(request.headers.get("user-agent", "")))


def _text(body: str, status: int = 200) -> PlainTextResponse:
    hdrs = _CACHE_HDR if status == 200 else {"Cache-Control": "no-store"}
    return PlainTextResponse(body, status_code=status, media_type=_TEXT, headers=hdrs)


@router.api_route("/:help", methods=["GET", "HEAD"], include_in_schema=False)
def curl_help(request: Request):
    if _is_browser(request):
        return RedirectResponse(_landing_302(), status_code=302)
    return _text(content.help_page() + "\n" + content.funnel_footer(plain=True))


@router.api_route("/:assets", methods=["GET", "HEAD"], include_in_schema=False)
def curl_assets(request: Request):
    if _is_browser(request):
        return RedirectResponse(_landing_302(), status_code=302)
    syms = sorted(registry.all_symbols())
    return _text(content.assets_page(syms))


@router.api_route("/:mcp", methods=["GET", "HEAD"], include_in_schema=False)
def curl_mcp(request: Request):
    if _is_browser(request):
        return RedirectResponse(_landing_302(), status_code=302)
    return _text(content.mcp_page())


@router.api_route("/reg", methods=["GET", "HEAD"], include_in_schema=False)
def curl_register(request: Request):
    # Dual-mode: terminal tools get the ANSI register page (QR + URL),
    # browsers 302 to the register form. no-store on both (it's a funnel
    # entry point, not a cacheable chart).
    no_store = {"Cache-Control": "no-store"}
    if _is_browser(request):
        return RedirectResponse(content.register_url(), status_code=302, headers=no_store)
    plain = "T" in (request.url.query or "")
    return PlainTextResponse(
        content.register_page(plain=plain), media_type=_TEXT, headers=no_store
    )


@router.api_route("/reg-tui", methods=["GET", "HEAD"], include_in_schema=False)
def curl_register_tui(request: Request):
    # TUI-attributed twin of /reg: a pure path the TUI binary can print in its
    # gate notices. Same dual-mode behavior; only the invisible browser 302
    # carries the tui utm source.
    no_store = {"Cache-Control": "no-store"}
    if _is_browser(request):
        return RedirectResponse(
            content.register_tui_url(), status_code=302, headers=no_store
        )
    plain = "T" in (request.url.query or "")
    return PlainTextResponse(
        content.register_page(plain=plain), media_type=_TEXT, headers=no_store
    )


@router.api_route("/curl-overview", methods=["GET", "HEAD"], include_in_schema=False)
@limiter.limit("30/minute")
def curl_overview(request: Request):
    # dispatcher rewrites bare curl GET / to this internal path
    plain = "T" in (request.url.query or "")
    body = content.overview_table(plain=plain)
    return _text(body + "\n" + content.funnel_footer(plain=plain))


@router.api_route("/{symbol_spec}", methods=["GET", "HEAD"], include_in_schema=False)
@limiter.limit("30/minute")
async def curl_chart(request: Request, symbol_spec: str):
    if _is_browser(request):
        return RedirectResponse(_landing_302(), status_code=302)

    raw_query = request.url.query or ""
    try:
        symbol, window = parse_path("/" + symbol_spec)
        opts = parse_options(raw_query)
        bucket = snap_window(window)
    except GrammarError as e:
        return _text(content.usage_error(str(e), plain=True), status=400)

    # registry.resolve() can trigger a blocking DB read (OHLCVDatabase) on a
    # stale/cold symbol-whitelist cache (10-min TTL per worker) — offload so
    # a cache-miss doesn't stall the event loop for all concurrent requests.
    canonical = await asyncio.to_thread(registry.resolve, symbol)
    if canonical is None:
        return _text(content.unknown_symbol(symbol, plain=True), status=404)

    try:
        chart = await render_chart(canonical, bucket, opts)
    except DataUnavailable:
        return _text(f"data warming up, try again in a minute — {site_host()}\n", status=503)
    except RenderFailed:
        logger.exception("curl chart render failed: %s %s", canonical, bucket.label)
        return _text(f"chart temporarily unavailable — {site_host()}\n", status=503)

    body = chart if opts.quiet else chart + content.funnel_footer(opts.plain)
    return _text(body)
