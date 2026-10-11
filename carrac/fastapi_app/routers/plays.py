"""Plays endpoints — reads and writes.

``GET /api/plays/active`` is a read; this module also brings the canonical
CRUD trio (``POST /api/plays``, ``PUT /api/plays/{id}``,
``DELETE /api/plays/{id}``), the transition / link-drawing / evaluate /
backtest endpoints and the share-link endpoints.

Validation strategy: the Pydantic body models in
``carrac.fastapi_app.models.plays`` enforce shape + ``extra="forbid"``,
then ``carrac.plays.routes._validate_play_input`` performs deep semantic
checks (condition logic, asset regex, expiry parsing, metadata size).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Optional

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    auth_or_key,
    get_request_tier,
)
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.plays import (
    PlayBacktestRequest,
    PlayCreateRequest,
    PlayLinkDrawingRequest,
    PlayShareRequest,
    PlayStatusRequest,
    PlayUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _resolve_user_id(auth: dict) -> Optional[int]:
    if auth.get("source") == "api_key":
        return auth.get("user_id")
    user = auth.get("user") or {}
    return user.get("id")


# ---------------------------------------------------------------------------
# GET /api/plays/active.
# ---------------------------------------------------------------------------


@router.get("/api/plays/active")
@limiter.limit("60/minute")
async def active_plays(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Return caller's non-terminal plays (watching/acting/expired).

    The shared admin key path returns ALL users' plays (used by the
    morning engine); per-user keys and sessions return only their own.
    """
    user_id = _resolve_user_id(auth)
    if user_id is None and auth.get("source") != "api_key":
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.models import get_active_plays

    plays = await asyncio.to_thread(get_active_plays, user_id)
    return JSONResponse(content=jsonable_encoder({"plays": plays, "count": len(plays)}))


# ---------------------------------------------------------------------------
# POST/PUT/DELETE /api/plays.
# ---------------------------------------------------------------------------


@router.post("/api/plays", status_code=201)
@limiter.limit("60/minute")
async def play_create(
    request: Request,
    body: PlayCreateRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a play. Tier-gated on ``max_plays``."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Tier gate: the play cap for the caller's tier.
    from carrac.auth.decorators import get_tier_limits
    from carrac.plays.models import (
        count_active_plays,
        play_create as _create_play_db,
    )
    from carrac.plays.routes import _validate_play_input

    tier = get_request_tier(request)
    limits = get_tier_limits(tier)
    max_plays = limits.get("max_plays", 0)
    current = await asyncio.to_thread(count_active_plays, user_id)
    if current >= max_plays:
        raise HTTPException(
            status_code=403,
            detail={
                "error": f"Play limit reached ({current}/{max_plays}). Upgrade for more.",
                "limit_type": "play",
                "current": current,
                "max": max_plays,
                "tier": tier,
            },
        )

    cleaned, error = _validate_play_input(body.model_dump(exclude_unset=True))
    if error:
        raise HTTPException(status_code=400, detail=error)

    play = await asyncio.to_thread(_create_play_db, user_id, **cleaned)

    # Funnel event — first_play_created. Best-effort; never breaks
    # the request on failure.
    try:
        from carrac.integrations import fire_first_event

        fire_first_event(
            user_id,
            "first_play_created",
            {
                "asset": cleaned.get("primary_asset"),
                "type": cleaned.get("type"),
            },
        )
    except Exception:
        pass

    return JSONResponse(content=jsonable_encoder({"play": play}), status_code=201)


@router.put("/api/plays/{play_id}")
@limiter.limit("60/minute")
async def update_play(
    request: Request,
    body: PlayUpdateRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    play_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Partial update of a play owned by the caller."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.models import (
        get_play,
        update_play as _update_play_db,
    )
    from carrac.plays.routes import _validate_play_input

    existing = await asyncio.to_thread(get_play, play_id, user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Play not found")

    cleaned, error = _validate_play_input(
        body.model_dump(exclude_unset=True), is_update=True
    )
    if error:
        raise HTTPException(status_code=400, detail=error)

    if not cleaned:
        return JSONResponse(content=jsonable_encoder({"play": existing}))

    play = await asyncio.to_thread(_update_play_db, play_id, user_id, **cleaned)
    return JSONResponse(content=jsonable_encoder({"play": play}))


@router.delete("/api/plays/{play_id}")
@limiter.limit("60/minute")
async def play_delete(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    play_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Hard-delete a play owned by the caller."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.models import play_delete as _delete_play_db

    deleted = await asyncio.to_thread(_delete_play_db, play_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Play not found")
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Plays subroutes.
# ---------------------------------------------------------------------------


@router.put("/api/plays/{play_id}/status")
@limiter.limit("60/minute")
async def transition_play(
    request: Request,
    body: PlayStatusRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    play_id: int = Path(..., ge=1),
) -> JSONResponse:
    """State-machine transition. Rejects invalid current→next pairs with 400."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.models import (
        PLAY_STATUSES,
        TRANSITIONS,
        get_play,
        transition_status,
    )

    if body.status not in PLAY_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {body.status}")

    existing = await asyncio.to_thread(get_play, play_id, user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Play not found")

    current = existing["status"]
    allowed = TRANSITIONS.get(current, set())
    if body.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"Cannot transition from '{current}' to '{body.status}'",
                "current_status": current,
                "allowed_transitions": sorted(allowed),
            },
        )

    play = await asyncio.to_thread(
        transition_status, play_id, user_id, body.status, resolution=body.resolution
    )
    return JSONResponse(content=jsonable_encoder({"play": play}))


@router.post("/api/plays/{play_id}/link-drawing")
@limiter.limit("60/minute")
async def link_drawing(
    request: Request,
    body: PlayLinkDrawingRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    play_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Attach a drawing id to the play."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.models import link_drawing as _link

    play = await asyncio.to_thread(_link, play_id, user_id, body.drawing_id)
    if not play:
        raise HTTPException(status_code=404, detail="Play not found")
    return JSONResponse(content=jsonable_encoder({"play": play}))


@router.post("/api/plays/{play_id}/unlink-drawing")
@limiter.limit("60/minute")
async def unlink_drawing(
    request: Request,
    body: PlayLinkDrawingRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    play_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Detach a drawing id from the play."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.models import unlink_drawing as _unlink

    play = await asyncio.to_thread(_unlink, play_id, user_id, body.drawing_id)
    if not play:
        raise HTTPException(status_code=404, detail="Play not found")
    return JSONResponse(content=jsonable_encoder({"play": play}))


@router.post("/api/plays/{play_id}/evaluate")
@limiter.limit("60/minute")
async def play_evaluate(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    play_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Re-evaluate a play's conditions against current market data."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.models import (
        get_play,
        transition_status as play_transition,
    )

    def _do_evaluate():
        play = get_play(play_id, user_id)
        if not play:
            raise HTTPException(status_code=404, detail="Play not found")

        from carrac.plays.monitor import plays_monitor

        cached_data = plays_monitor._get_cached_data()
        if not cached_data or "assets" not in cached_data:
            raise HTTPException(
                status_code=503, detail="Market data not available yet"
            )

        all_assets = {}
        for asset in cached_data.get("assets", []):
            sym = asset.get("symbol", "").upper()
            if sym:
                all_assets[sym] = asset

        primary = (play.get("primary_asset") or "").upper()
        data_asset = (play.get("trigger_asset") or primary).upper()
        asset_data = all_assets.get(data_asset, {})
        rt = asset_data.get("realtime", {})
        price = rt.get("price", asset_data.get("latest_price", 0))

        market_data = {
            "price": price or 0,
            "asset_data": asset_data,
            "all_assets": all_assets,
        }

        result = plays_monitor.play_evaluate(play, market_data)
        plays_monitor._persist_state(play, result)

        new_status = plays_monitor._compute_transition(play, result)
        if new_status and new_status != play.get("status"):
            resolution = plays_monitor._build_resolution(new_status, result)
            play_transition(play_id, user_id, new_status, resolution=resolution)

        updated_play = get_play(play_id, user_id)
        cond = result["conditions_result"]
        return {
            "play": updated_play,
            "evaluation": {
                "met_count": cond.get("met_count", 0),
                "total_count": cond.get("total_count", 0),
                "all_met": cond.get("all_met", False),
                "invalidation_triggered": result["invalidation_triggered"],
                "expired": result["expired"],
            },
        }

    try:
        payload = await asyncio.to_thread(_do_evaluate)
        # jsonable_encoder, like every other play route. On
        # Postgres the play row carries datetime objects; the bare
        # JSONResponse(payload) could not serialise them and the except
        # below turned that into a silent 500 on every fresh clone.
        return JSONResponse(content=jsonable_encoder(payload))
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — any failure is a 500 for the caller
        logger.exception("play evaluate failed for play_id=%s", play_id)
        raise HTTPException(status_code=500, detail="Evaluation failed")


@router.post("/api/plays/{play_id}/backtest")
@limiter.limit("30/minute")  # heavier than other writes
async def backtest_play(
    request: Request,
    body: PlayBacktestRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    play_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Paid-tier custom Hold-N backtest of a play's conditions."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Tier gate: the ``backtester`` limit.
    from carrac.auth.decorators import get_tier_limits

    tier = get_request_tier(request)
    if not get_tier_limits(tier).get("backtester"):
        # 'backtester' is True from member up, so naming Premium
        # here sent free users to a higher plan than the gate requires.
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Custom backtest requires a subscription",
                "tier_gated": True,
            },
        )

    lookback = body.lookback_bars or 365  # _BACKTEST_DEFAULT_LOOKBACK
    hold = body.hold_bars or 5  # _BACKTEST_DEFAULT_HOLD

    # ``_run_play_backtest`` needs only the play and the clamped inputs, so
    # no Flask request context is involved.
    from carrac.plays.models import get_play
    from carrac.plays.routes import _run_play_backtest

    def _do_backtest():
        play = get_play(play_id, user_id)
        if not play:
            raise HTTPException(status_code=404, detail="Play not found")

        lookback_clamped = max(50, min(2000, lookback))
        hold_clamped = max(1, min(60, hold))
        return _run_play_backtest(play, lookback_clamped, hold_clamped)

    result = await asyncio.to_thread(_do_backtest)
    if "error" in result:
        status = 400 if "No backtestable" in result["error"] else 500
        return JSONResponse(content=jsonable_encoder(result), status_code=status)
    return JSONResponse(content=jsonable_encoder(result))


# ---------------------------------------------------------------------------
# Shareable Plays — mint / update / revoke a public share link.
#
# These live here because the strangler routes POST/PUT/DELETE under the
# ``/api/plays/`` prefix to FastAPI (MIGRATED_WRITE_PREFIXES); a Flask handler
# for them would never be reached. Public READS (/s/<slug>) are on Flask.
# ---------------------------------------------------------------------------


def _public_base(request: Request) -> str:
    """Canonical public origin for share URLs.

    Prefer an explicit ``PUBLIC_BASE_URL``/``SITE_URL`` (so prod links read
    ``https://your-domain/...`` regardless of the internal proxy host); fall back
    to the request's own base URL (correct in dev/tests).
    """
    import os

    base = os.getenv("PUBLIC_BASE_URL") or os.getenv("SITE_URL")
    if base:
        return base.rstrip("/")
    return str(request.base_url).rstrip("/")


def _share_public_payload(request: Request, share: dict) -> dict:
    slug = share["slug"]
    base = _public_base(request)
    snap = share.get("snapshot") or {}
    return jsonable_encoder(
        {
            "slug": slug,
            "path": f"/s/{slug}",
            "url": f"{base}/s/{slug}",
            "image_url": f"{base}/s/{slug}/card.png",
            "include_thesis": share.get("include_thesis"),
            "include_conditions": share.get("include_conditions"),
            "include_plan": share.get("include_plan"),
            "include_outcome": share.get("include_outcome", False),
            "view_count": share.get("view_count", 0),
            "created_at": share.get("created_at"),
            # Frozen-at values the modal needs for labels/staleness hints.
            "snapshot_status": snap.get("status"),
            "snapshot_title": snap.get("title"),
        }
    )


@router.post("/api/plays/{play_id}/share", status_code=201)
@limiter.limit("30/minute")
async def create_play_share(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    play_id: int = Path(..., ge=1),
    body: PlayShareRequest | None = None,
) -> JSONResponse:
    """Mint (or return the existing active) public share link for a play."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.shares import (
        count_active_shares,
        create_share,
        get_active_share_for_play,
        share_quota,
    )

    def _do_create_share():
        # Quota applies only when minting a NEW share — re-sharing an existing
        # play returns its current link and doesn't consume quota.
        if get_active_share_for_play(play_id, user_id) is None:
            tier = get_request_tier(request)
            quota = share_quota(tier)
            current = count_active_shares(user_id)
            if current >= quota:
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

        toggles = body.model_dump(exclude_unset=True) if body is not None else {}
        share = create_share(user_id, play_id, **toggles)
        if not share:
            raise HTTPException(status_code=404, detail="Play not found")
        return share

    share = await asyncio.to_thread(_do_create_share)
    return JSONResponse(_share_public_payload(request, share), status_code=201)


@router.put("/api/plays/{play_id}/share")
@limiter.limit("30/minute")
async def update_play_share(
    request: Request,
    body: PlayShareRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    play_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Update field-visibility toggles; re-freezes the snapshot + drops the cached image."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.shares import update_share

    share = await asyncio.to_thread(
        update_share, play_id, user_id, **body.model_dump(exclude_unset=True)
    )
    if not share:
        raise HTTPException(status_code=404, detail="No active share for this play")

    return JSONResponse(_share_public_payload(request, share))


@router.delete("/api/plays/{play_id}/share")
@limiter.limit("30/minute")
async def revoke_play_share(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    play_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Revoke the play's active share link(s). The public URL then returns 410."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.shares import revoke_share

    revoked = await asyncio.to_thread(revoke_share, play_id, user_id)
    return JSONResponse({"ok": True, "revoked": revoked})
