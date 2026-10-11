"""Chart shares — mint / revoke a public frozen-chart share link.

``POST   /api/chart/share``         mint a NEW frozen-chart share
``DELETE /api/chart/share/{slug}``  revoke it (slug-scoped to the creator)

Shares of kind ``'chart'``. Unlike plays/trades there is
no subject row and no get-or-create — every post is a frozen MOMENT, so each
mint is a new slug. Quota (the shared per-user pool) bounds the volume.

Routing: both methods are flipped to FastAPI via dedicated strangler entries
(``("/api/chart/share", {POST})`` ExactPath +
``("/api/chart/share/", {DELETE})`` PrefixPath in ``carrac/asgi.py``). We do
NOT widen a ``/api/chart`` prefix — the chart GET surface is public + nginx
cached. Public reads (/s/<slug>) stay on the Flask shares blueprint.

Candles are assembled SERVER-SIDE from ``build_chart_payload`` — the client
sends only the symbol + timeframe (+ optional indicator descriptors / drawing
ids), never price data.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    auth_or_key,
    get_request_tier,
)
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.chart_share import ChartShareRequest

logger = logging.getLogger("carrac.fastapi.chart_share")

router = APIRouter()


def _resolve_user_id(auth: dict):
    if auth.get("source") == "api_key":
        return auth.get("user_id")
    user = auth.get("user") or {}
    return user.get("id")


def _chart_share_payload(request: Request, share: dict) -> dict:
    from carrac.fastapi_app.routers.plays import _public_base

    slug = share["slug"]
    base = _public_base(request)
    snap = share.get("snapshot") or {}
    return jsonable_encoder(
        {
            "slug": slug,
            "path": f"/s/{slug}",
            "url": f"{base}/s/{slug}",
            "image_url": f"{base}/s/{slug}/card.png",
            "view_count": share.get("view_count", 0),
            "created_at": share.get("created_at"),
            "snapshot_symbol": snap.get("symbol"),
            "snapshot_timeframe": snap.get("timeframe"),
            "snapshot_change_pct": snap.get("change_pct"),
        }
    )


@router.post("/api/chart/share", status_code=201)
@limiter.limit("30/minute")
async def create_chart_share_endpoint(
    request: Request,
    body: ChartShareRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Mint a new public, frozen-snapshot chart share."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.shares import (
        ChartImageError,
        count_active_shares,
        create_chart_share,
        decode_chart_capture,
        share_quota,
    )

    # Validate the client capture (option C gate) BEFORE charging quota.
    capture_png = None
    if body.image:
        try:
            capture_png = decode_chart_capture(body.image)
        except ChartImageError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid chart image: {exc}")

    # Every chart share is a new mint → always charge quota (shared pool).
    tier = get_request_tier(request)
    quota = share_quota(tier)

    def _mint():
        current = count_active_shares(user_id)
        if current >= quota:
            return {"quota_exceeded": True, "current": current, "share": None}
        share = create_chart_share(
            user_id,
            body.symbol,
            body.timeframe,
            capture_png=capture_png,
            indicators=body.indicators,
            drawing_ids=body.drawing_ids,
            theme=body.theme,
            viewport=body.viewport.model_dump() if body.viewport else None,
        )
        return {"quota_exceeded": False, "current": current, "share": share}

    result = await asyncio.to_thread(_mint)
    if result["quota_exceeded"]:
        current = result["current"]
        raise HTTPException(
            status_code=402,
            detail={
                "error": f"Share limit reached ({current}/{quota}). Upgrade for more.",
                "limit_type": "share",
                "current": current,
                "max": quota,
                "tier": tier,
            },
        )

    share = result["share"]
    if not share:
        raise HTTPException(
            status_code=404,
            detail=f"No chart data for {body.symbol} {body.timeframe}",
        )

    return JSONResponse(_chart_share_payload(request, share), status_code=201)


@router.delete("/api/chart/share/{slug}")
@limiter.limit("30/minute")
async def revoke_chart_share_endpoint(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    slug: str = Path(..., min_length=6, max_length=12),
) -> JSONResponse:
    """Revoke a chart share by slug (scoped to its creator). Public URL → 410."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.shares import revoke_share_by_slug

    revoked = await asyncio.to_thread(revoke_share_by_slug, slug, user_id)
    return JSONResponse({"ok": True, "revoked": revoked})
