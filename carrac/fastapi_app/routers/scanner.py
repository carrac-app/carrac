"""Scanner + relative-strength endpoints.

* ``GET /api/scanner``           — ranked asset scores.
* ``GET /api/relative-strength`` — RRG quadrants for tracked assets.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    auth_or_key,
    get_request_tier,
)
from carrac.fastapi_app.limiter import limiter

logger = logging.getLogger("carrac.fastapi.scanner")

router = APIRouter()

FREE_SCANNER_RESULT_LIMIT = 5


def _fast_json_response(payload) -> Response:
    """Serialize an already-JSON-native payload straight to bytes, skipping
    ``jsonable_encoder``.

    The scanner blob is ~12 MB and comes from ``get_cached_scanner``
    (Redis ``json.loads``) + plain-Python filtering — i.e. it is already pure
    JSON types (str/int/float/bool/None/list/dict). ``jsonable_encoder`` walked
    every node of it recursively (~9 s of pure overhead) before ``JSONResponse``
    serialized it a second time. Serializing once with stdlib ``json`` is
    equivalent output and ~an order of magnitude faster. ``default=str`` is
    cheap insurance against a stray non-native value (there are none today).
    """
    return Response(
        content=json.dumps(payload, separators=(",", ":"), default=str),
        media_type="application/json",
    )


@router.get("/api/scanner")
@limiter.limit("24/minute")
async def scanner_get_screen(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    verdict: Optional[str] = Query(default=None),
    direction: str = Query(default="both"),
    format: Optional[str] = Query(default=None),
) -> Response:
    """Return ranked asset scores."""
    from carrac.caches import get_cached_scanner

    # Serve the last-known-good snapshot (stale-marked) on cold
    # start so the Scanner is never a 503; only true first-boot 503s.
    data = await asyncio.to_thread(get_cached_scanner, allow_stale=True)
    if not data:
        raise HTTPException(
            status_code=503,
            detail={"error": "Scanner data not yet available", "results": []},
        )

    results = list(data.get("results", []))

    allowed: Optional[set[str]] = None
    if verdict:
        allowed = {v.strip().upper() for v in verdict.split(",") if v.strip()}
        results = [r for r in results if r.get("best_verdict") in allowed]

    if direction in ("long", "short"):
        for r in results:
            dir_data = r.get(direction, {})
            r["_filter_score"] = dir_data.get("total", 0)
            r["_filter_verdict"] = dir_data.get("verdict", "AVOID")
        results.sort(key=lambda r: r["_filter_score"], reverse=True)
        if allowed is not None:
            results = [r for r in results if r["_filter_verdict"] in allowed]
        for r in results:
            r.pop("_filter_score", None)
            r.pop("_filter_verdict", None)

    total_available = len(results)
    tier = get_request_tier(request)
    truncated = False
    if tier == "free" and total_available > FREE_SCANNER_RESULT_LIMIT:
        results = results[:FREE_SCANNER_RESULT_LIMIT]
        truncated = True
        try:
            from carrac.integrations import on_funnel_event

            sess = getattr(request.state, "session", {}) or {}
            on_funnel_event(
                sess.get("user_id"),
                "scanner_limit_hit",
                {
                    "tier": tier,
                    "total_available": total_available,
                    "returned": FREE_SCANNER_RESULT_LIMIT,
                },
            )
        except Exception:
            pass

    from carrac.formatters import compact_scanner, maybe_compact

    payload: dict[str, Any] = {
        "results": results,
        "macro": data.get("macro", {}),
        "computed_at": data.get("computed_at", ""),
        "event_risk": data.get("event_risk"),
        "stale": bool(data.get("_stale")),
        "stale_age_sec": data.get("_stale_age_sec"),
    }
    if truncated:
        payload["total_available"] = total_available
        payload["returned"] = len(results)
        payload["upgrade_to_see_all"] = True
    # Skip jsonable_encoder — the payload is already JSON-native and
    # ~12 MB; the redundant recursive walk was ~9 s. Serialize once, fast.
    return _fast_json_response(maybe_compact(payload, compact_scanner, format_param=format))


@router.get("/api/confluence")
@limiter.limit("60/minute")
async def get_confluence(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    symbol: str = Query(...),
) -> JSONResponse:
    """Return confluence zones for a single symbol from the scanner cache.

    No ``tf`` param: the scanner cache holds one fixed confluence snapshot per
    asset (daily-resolution levels; week/month highs/lows folded in), not one
    per chart timeframe, so a ``tf`` filter would be cosmetic.
    """
    from carrac.scanner.confluence_query import get_symbol_confluence

    payload, status = await asyncio.to_thread(get_symbol_confluence, symbol)

    if status == "cold":
        raise HTTPException(
            status_code=503,
            detail={"error": "Scanner data not yet available", "zones": []},
        )
    if status == "not_found":
        raise HTTPException(
            status_code=404,
            detail={"error": f"{symbol.upper().strip()} not in scanner universe"},
        )

    return JSONResponse(
        content=jsonable_encoder(
            {
                "symbol": payload["symbol"],
                "price": payload["price"],
                "computed_at": payload["computed_at"],
                "zones": payload["all"],
                "supports": payload["supports"],
                "resistances": payload["resistances"],
            }
        )
    )


@router.get("/api/relative-strength")
@limiter.limit("24/minute")
async def relative_strength(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Ranked relative strength with RRG quadrants for all tracked assets."""
    # The ranking helpers are framework-neutral, so they are shared with the
    # chart module.
    from carrac.charts.routes import _compute_rs_rankings, _get_cached_rs, _set_cached_rs

    cached = _get_cached_rs()
    if cached is not None:
        return JSONResponse(content=jsonable_encoder({"status": "ok", "assets": cached}))

    def _compute_and_cache_rs():
        rankings = _compute_rs_rankings()
        _set_cached_rs(rankings)
        return rankings

    try:
        rankings = await asyncio.to_thread(_compute_and_cache_rs)
        return JSONResponse(content=jsonable_encoder({"status": "ok", "assets": rankings}))
    except Exception as e:  # noqa: BLE001
        logger.error("Error computing relative strength: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "error": str(e)},
        )
