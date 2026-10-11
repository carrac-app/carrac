"""Modeled liquidation-level heatmap endpoint.

``GET /api/liq-heatmap`` — a time x price liquidation-intensity heatmap for
one symbol, computed from our own Binance-derived open-interest, price, and
long/short-ratio history (see ``carrac/liq_heatmap.py`` for the full
methodology). Mirrors the ``/api/confluence`` pattern: precomputed by a
worker cycle every 15 minutes and served straight from the quant DB — no
on-request computation, no touch of the ``/api/chart`` pipeline.

Non-advice: these are MODELED estimated liquidation clusters, not observed
order-book liquidity — the frontend labels the overlay accordingly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import RateCtxDep, auth_or_key
from carrac.fastapi_app.limiter import limiter

logger = logging.getLogger("carrac.fastapi.liq_heatmap")

router = APIRouter()


@router.get("/api/liq-heatmap")
@limiter.limit("60/minute")
async def get_liq_heatmap(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    symbol: str = Query(...),
    hours: Optional[int] = Query(default=None, ge=1, le=24 * 30),
) -> JSONResponse:
    """Return the modeled liquidation-heatmap cycle series for one symbol.

    ``hours`` optionally narrows the returned window (default: the model's
    own lookback, ``DEFAULT_LOOKBACK_DAYS`` * 24). Cycles are ascending by
    timestamp; each carries its own sparse (threshold-filtered) bands —
    weak levels are already dropped server-side (see
    ``filter_bands_by_threshold``), the frontend paints only what it gets.
    """
    from carrac.liq_heatmap import HEATMAP_SYMBOLS, get_heatmap_series

    status, payload = await asyncio.to_thread(get_heatmap_series, symbol, hours)

    if status == "not_covered":
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"{symbol.upper().strip()} not covered by the liquidation heatmap",
                "covered_symbols": sorted(HEATMAP_SYMBOLS),
            },
        )
    if status == "cold":
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Liquidation heatmap not yet computed for this symbol",
                "cycles": [],
            },
        )

    return JSONResponse(content=jsonable_encoder(payload))
