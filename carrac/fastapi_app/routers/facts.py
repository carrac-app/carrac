"""Facts bundle endpoint — GET /api/facts/{symbol}.

Returns the cached per-asset facts bundle (ma_stack, stretch, levels/clusters,
streaks, decisive_close, rsi, volume, candle, weekly). Reads from the
``facts:bundle:<symbol>`` Redis key only — never recomputes on the request path.

Auth: X-API-Key or X-Internal-Key header (mirrors chart_internal.py).
      EVERYTHING IS FREE — no tier gating beyond normal auth.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from carrac.fastapi_app.limiter import limiter

router = APIRouter()


def _resolve_key(request: Request) -> tuple[bool, str | None]:
    """Verify X-Internal-Key / X-API-Key header.

    Returns (ok, tier). Mirrors the auth idiom from chart_internal.py::_verify_key
    but also surfaces the caller tier for bump_usage.
    """
    # Shared key only via X-Internal-Key.
    internal = request.headers.get("X-Internal-Key", "")
    key = internal or request.headers.get("X-API-Key", "")
    if not key:
        return False, None
    from carrac.auth.decorators import resolve_api_key
    ok, _uid, tier, _scope, _internal = resolve_api_key(
        key, allow_shared=bool(internal))
    return ok, tier


@router.get("/api/facts/{symbol}")
@limiter.limit("60/minute")
async def get_facts_endpoint(request: Request, symbol: str) -> JSONResponse:
    """Cached per-asset facts bundle. API-key auth only."""
    ok, tier = await asyncio.to_thread(_resolve_key, request)
    if not ok:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    sym = (symbol or "").upper().strip()
    from carrac.facts.wire import get_asset_facts
    bundle = await asyncio.to_thread(get_asset_facts, sym)

    try:
        from carrac.facts.cost import bump_usage
        await asyncio.to_thread(bump_usage, "api_facts", tier or "free")
    except Exception:
        pass

    if bundle is None:
        return JSONResponse({"error": "not found", "symbol": sym}, status_code=404)
    return JSONResponse(
        bundle,
        headers={"Cache-Control": "public, max-age=1800, stale-while-revalidate=300"},
    )
