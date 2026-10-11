"""Drawings endpoints.

Migrates the following Flask ``drawings_bp`` handlers to FastAPI:

  POST   /api/drawings            — create drawing
  PUT    /api/drawings/{id}       — update drawing (optimistic concurrency)
  DELETE /api/drawings/{id}       — delete drawing (optimistic concurrency)
  POST   /api/drawings/upsert     — create-or-update by local_id
  POST   /api/drawings/bulk       — bulk create
  DELETE /api/drawings/by-local-id

CSRF policy: ``drawings_bp`` is csrf.exempt in Flask, and ``/api/drawings``
is in ``_SKIP_PREFIXES`` in ``CarracCSRFMiddleware``. No CSRF is enforced
for drawings — this matches Flask behaviour. Do NOT add CSRF here.

Validation strategy: ``DrawingCreateRequest`` / ``DrawingUpdateRequest``
(``extra="forbid"``) enforce shape. The existing Flask
``validate_drawing_data`` helper performs deep type / range / whitelist
checks — the FastAPI handlers delegate to it so both runtimes share one
validation source.

⚠️ HIGH RISK: ``PUT /api/drawings/{id}`` passes the validated dict via
``**kwargs`` to ``db.drawing_update()``. ``extra="forbid"`` on
``DrawingUpdateRequest`` is the only guard against arbitrary field injection
at the Pydantic boundary. Do NOT relax it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    auth_or_key,
    get_request_tier,
)
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.drawings import (
    DrawingBulkRequest,
    DrawingCreateRequest,
    DrawingDeleteByLocalIdRequest,
    DrawingUpdateRequest,
    DrawingUpsertRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _resolve_user_id(auth: dict) -> Optional[int]:
    """Resolve integer user_id for drawing ownership.

    Mirrors ``carrac/drawings/routes.py::get_request_user_id``:
    - Session user → ``session['user_id']`` (int).
    - Per-user API key → the bound int ``api_key_user_id``.
    - Shared admin key (user_id=None) → None; caller emits 401.
    """
    if auth.get("source") == "api_key":
        return auth.get("user_id")  # None for shared admin key
    user = auth.get("user") or {}
    uid = user.get("id")
    return int(uid) if uid is not None else None


def _sync_session_id(request: Request) -> str:
    """Return ``X-Sync-Session`` header or empty string."""
    return request.headers.get("X-Sync-Session", "")


def _publish_sync(user_id: int, assets, request: Request) -> None:
    """Publish drawing sync event. Best-effort — never fails the request.

    The event tells the user's other tabs to refetch, so a failure leaves
    them stale until their next poll; it is logged, not raised.
    """
    if isinstance(assets, str):
        assets = [assets]
    try:
        from carrac.sync.events import publish_drawing_event

        publish_drawing_event(user_id, assets, _sync_session_id(request))
    except Exception:
        logger.warning(
            "sync event not published: user_id=%s kind=drawing assets=%s",
            user_id, assets, exc_info=True,
        )


# ---------------------------------------------------------------------------
# POST /api/drawings — create drawing
# ---------------------------------------------------------------------------


@router.post("/api/drawings", status_code=201)
@limiter.limit("60/minute")
async def drawing_create(
    request: Request,
    body: DrawingCreateRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a new drawing. Requires per-user auth."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Per-user authentication required (session or per-user API key)",
        )

    from carrac.drawings.routes import validate_drawing_data
    from carrac.content_db import get_content_db

    data = body.model_dump(exclude_unset=False)  # need defaults for validation

    is_valid, error_msg = validate_drawing_data(data)
    if not is_valid:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": error_msg}
        )

    db = get_content_db()

    from carrac.drawings.routes import MAX_DRAWINGS_PER_ASSET_TIMEFRAME

    # Tier-limit lookup is in-memory / session-bound (matches alerts.py
    # precedent for get_tier_limits — not offloaded); only ContentDatabase
    # calls below go through the thread pool.
    scenario_tier: Optional[str] = None
    scenario_limits: Optional[dict] = None
    if body.drawing_type == "scenario" and body.scenario_id is None:
        from carrac.auth.decorators import get_tier_limits

        scenario_tier = get_request_tier(request)
        scenario_limits = get_tier_limits(scenario_tier)

    def _create_drawing() -> int:
        existing = db.drawing_list(
            asset=body.asset, timeframe=body.timeframe, user_id=user_id
        )
        if len(existing) >= MAX_DRAWINGS_PER_ASSET_TIMEFRAME:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "error": (
                        f"Maximum drawings ({MAX_DRAWINGS_PER_ASSET_TIMEFRAME})"
                        " reached for this asset/timeframe"
                    ),
                },
            )

        # Enforce scenario limit for top-level scenarios.
        if scenario_limits is not None:
            current_count = db.get_active_scenario_count(user_id)
            if current_count >= scenario_limits["max_scenarios"]:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "success": False,
                        "error": (
                            f"Scenario limit reached ({current_count}/{scenario_limits['max_scenarios']})."
                            " Upgrade for more."
                        ),
                        "limit_type": "scenario",
                        "current": current_count,
                        "max": scenario_limits["max_scenarios"],
                        "tier": scenario_tier,
                    },
                )

        # Validate scenario_id if provided.
        scenario_id = body.scenario_id
        if scenario_id is not None:
            parent = db.get_drawing_by_id(scenario_id)
            if (
                not parent
                or parent.get("drawing_type") != "scenario"
                or parent.get("user_id") != user_id
            ):
                # Parent must exist, be a scenario, AND belong to the caller —
                # otherwise a user could parent a drawing under another user's
                # scenario (cross-user injection). Mirrors the MCP tool check.
                raise HTTPException(
                    status_code=400, detail={"success": False, "error": "Invalid scenario_id"}
                )

        return db.save_drawing(
            asset=body.asset,
            timeframe=body.timeframe,
            drawing_type=body.drawing_type,
            price_level=body.price_level,
            price_level_upper=body.price_level_upper,
            label=body.label,
            color=body.color or "#ff0000",
            line_style=body.line_style,
            line_width=body.line_width,
            metadata=body.metadata,
            scenario_id=scenario_id,
            user_id=user_id,
        )

    drawing_id = await asyncio.to_thread(_create_drawing)

    # first_drawing_created funnel event.
    try:
        from carrac.integrations import fire_first_event

        fire_first_event(
            user_id,
            "first_drawing_created",
            {"asset": body.asset, "drawing_type": body.drawing_type},
        )
    except Exception:
        pass

    return JSONResponse(
        {
            "success": True,
            "drawing_id": drawing_id,
            "message": f"Drawing created for {body.asset} {body.timeframe}",
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# PUT /api/drawings/{drawing_id} — update drawing
# ---------------------------------------------------------------------------


@router.put("/api/drawings/{drawing_id}")
@limiter.limit("60/minute")
async def drawing_update(
    request: Request,
    body: DrawingUpdateRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    drawing_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Update a drawing. Optimistic concurrency via ``If-Match`` or ``if_version``."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(
            status_code=401, detail={"success": False, "error": "Per-user authentication required"}
        )

    from carrac.drawings.routes import validate_drawing_data
    from carrac.content_db import get_content_db

    data = body.model_dump(exclude_unset=True)

    is_valid, error_msg = validate_drawing_data(data, is_update=True)
    if not is_valid:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": error_msg}
        )

    db = get_content_db()

    # Optimistic concurrency: ``If-Match`` header takes precedence over body.
    # Header/dict parsing is in-memory — independent of the DB calls below,
    # so it can run on the event-loop thread without changing behaviour.
    expected_version: Optional[int] = None
    if_match = request.headers.get("If-Match")
    if if_match:
        try:
            expected_version = int(if_match.strip('"'))
        except ValueError:
            expected_version = None
    # Pop if_version from data before passing to DB.
    if expected_version is None and "if_version" in data:
        try:
            expected_version = int(data.pop("if_version"))
        except (ValueError, TypeError):
            data.pop("if_version", None)
    else:
        data.pop("if_version", None)

    def _update_drawing():
        existing = db.get_drawing_by_id(drawing_id)
        if existing is None:
            raise HTTPException(
                status_code=404, detail={"success": False, "error": "Drawing not found"}
            )
        if existing.get("user_id", 0) != user_id:
            raise HTTPException(
                status_code=403, detail={"success": False, "error": "Not your drawing"}
            )

        status, new_version = db.drawing_update(
            drawing_id,
            user_id=user_id,
            expected_version=expected_version,
            **data,
        )

        if status == db.UPDATE_OK:
            updated = db.get_drawing_by_id(drawing_id)
            return existing, new_version, updated

        if status == db.UPDATE_CONFLICT:
            raise HTTPException(
                status_code=409,
                detail={
                    "success": False,
                    "error": "version_conflict",
                    "message": "Drawing was modified by another client. Refresh and retry.",
                    "current_version": new_version,
                },
            )

        # UPDATE_NOT_FOUND
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "No valid fields to update or drawing not found"},
        )

    existing, new_version, updated = await asyncio.to_thread(_update_drawing)

    _publish_sync(user_id, existing.get("asset", ""), request)
    # The re-read row's timestamps are datetime objects on Postgres.
    response = JSONResponse(
        jsonable_encoder({
            "success": True,
            "message": "Drawing updated",
            "drawing": updated,
            "version": new_version,
        })
    )
    response.headers["ETag"] = f'"{new_version}"'
    return response


# ---------------------------------------------------------------------------
# DELETE /api/drawings/by-local-id
# ---------------------------------------------------------------------------


@router.delete("/api/drawings/by-local-id")
@limiter.limit("60/minute")
async def delete_drawing_by_local_id(
    request: Request,
    body: DrawingDeleteByLocalIdRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Delete a drawing by its local_id (stored in metadata)."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(
            status_code=401, detail={"success": False, "error": "Per-user authentication required"}
        )

    from carrac.content_db import get_content_db

    db = get_content_db()
    success = await asyncio.to_thread(
        db.delete_drawing_by_local_id, body.asset, body.local_id, user_id=user_id
    )

    if success:
        _publish_sync(user_id, body.asset, request)

    return JSONResponse(
        {
            "success": success,
            "message": "Drawing deleted" if success else "Drawing not found on server",
        }
    )


# ---------------------------------------------------------------------------
# DELETE /api/drawings/{drawing_id}
# ---------------------------------------------------------------------------


@router.delete("/api/drawings/{drawing_id}")
@limiter.limit("60/minute")
async def drawing_delete(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    drawing_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Delete a drawing. Cascade-deletes scenario children."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(
            status_code=401, detail={"success": False, "error": "Per-user authentication required"}
        )

    from carrac.content_db import get_content_db

    db = get_content_db()

    def _fetch_existing():
        existing = db.get_drawing_by_id(drawing_id)
        if existing is None:
            raise HTTPException(
                status_code=404, detail={"success": False, "error": "Drawing not found"}
            )
        if existing.get("user_id", 0) != user_id:
            raise HTTPException(
                status_code=403, detail={"success": False, "error": "Not your drawing"}
            )
        return existing

    existing = await asyncio.to_thread(_fetch_existing)

    # Optimistic concurrency: If-Match header or body.if_version.
    # ``request.json()`` is itself async (reads off the ASGI receive channel,
    # not a blocking call) so it stays on the event loop, same as before.
    expected_version: Optional[int] = None
    if_match = request.headers.get("If-Match")
    if if_match:
        try:
            expected_version = int(if_match.strip('"'))
        except ValueError:
            expected_version = None
    if expected_version is None:
        # Try to read body (optional on DELETE).
        try:
            raw_body = await request.json()
            if isinstance(raw_body, dict) and "if_version" in raw_body:
                expected_version = int(raw_body["if_version"])
        except Exception:
            pass

    def _delete_and_cascade():
        status = db.drawing_delete(
            drawing_id, user_id=user_id, expected_version=expected_version
        )

        if status == db.UPDATE_CONFLICT:
            fresh = db.get_drawing_by_id(drawing_id) or {}
            raise HTTPException(
                status_code=409,
                detail={
                    "success": False,
                    "error": "version_conflict",
                    "message": "Drawing was modified by another client. Refresh and retry.",
                    "current_version": fresh.get("version"),
                },
            )
        if status == db.UPDATE_NOT_FOUND:
            raise HTTPException(
                status_code=404, detail={"success": False, "message": "Drawing not found"}
            )

        # UPDATE_OK — cascade children (scoped to the owner, defense-in-depth).
        children_deleted = 0
        if existing.get("drawing_type") == "scenario":
            children_deleted = db.delete_scenario_children(drawing_id, user_id=user_id)
        return children_deleted

    children_deleted = await asyncio.to_thread(_delete_and_cascade)

    _publish_sync(user_id, existing.get("asset", ""), request)

    msg = "Drawing deleted"
    if children_deleted:
        msg += f" (+ {children_deleted} child drawings)"

    return JSONResponse({"success": True, "message": msg})


# ---------------------------------------------------------------------------
# POST /api/drawings/upsert — create-or-update by local_id
# ---------------------------------------------------------------------------


@router.post("/api/drawings/upsert", status_code=201)
@limiter.limit("60/minute")
async def upsert_drawing(
    request: Request,
    body: DrawingUpsertRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create or update a drawing by local_id (per-drawing sync)."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Per-user authentication required (session or per-user API key)",
        )

    from carrac.drawings.routes import (
        MAX_DRAWINGS_PER_ASSET_TIMEFRAME,
        validate_drawing_data,
    )
    from carrac.content_db import get_content_db

    data = body.model_dump(exclude_unset=False)

    is_valid, error_msg = validate_drawing_data(data)
    if not is_valid:
        raise HTTPException(
            status_code=400, detail={"success": False, "error": error_msg}
        )

    meta = body.metadata or {}
    local_id = meta.get("local_id")
    if not local_id:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": "metadata.local_id required"},
        )

    db = get_content_db()

    def _upsert_drawing():
        existing = db.find_drawing_by_local_id(body.asset, local_id, user_id=user_id)

        if existing:
            db.drawing_update(
                existing["id"],
                user_id=user_id,
                metadata=meta,
                price_level=body.price_level,
                price_level_upper=body.price_level_upper,
                label=body.label,
                color=body.color,
                line_width=body.line_width,
            )
            return "updated", existing["id"]

        existing_count = db.drawing_list(
            asset=body.asset, timeframe=body.timeframe, user_id=user_id
        )
        if len(existing_count) >= MAX_DRAWINGS_PER_ASSET_TIMEFRAME:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "error": f"Maximum drawings ({MAX_DRAWINGS_PER_ASSET_TIMEFRAME}) reached",
                },
            )

        new_id = db.save_drawing(
            asset=body.asset,
            timeframe=body.timeframe,
            drawing_type=body.drawing_type,
            price_level=body.price_level,
            price_level_upper=body.price_level_upper,
            label=body.label,
            color=body.color or "#ff0000",
            line_style=body.line_style,
            line_width=body.line_width,
            metadata=meta,
            user_id=user_id,
        )
        return "created", new_id

    action, drawing_id = await asyncio.to_thread(_upsert_drawing)
    _publish_sync(user_id, body.asset, request)

    if action == "updated":
        return JSONResponse(
            {"success": True, "drawing_id": drawing_id, "action": "updated"}
        )

    return JSONResponse(
        {"success": True, "drawing_id": drawing_id, "action": "created"},
        status_code=201,
    )


# ---------------------------------------------------------------------------
# POST /api/drawings/bulk — bulk create
# ---------------------------------------------------------------------------


@router.post("/api/drawings/bulk", status_code=201)
@limiter.limit("30/minute")
async def create_bulk_drawings(
    request: Request,
    body: DrawingBulkRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create multiple drawings at once (max 50). Optional replace mode."""
    user_id = _resolve_user_id(auth)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Per-user authentication required for bulk drawing operations",
        )

    from carrac.drawings.routes import validate_drawing_data
    from carrac.content_db import get_content_db

    db = get_content_db()

    def _bulk_create():
        # Replace mode: delete existing drawings for each asset/timeframe.
        if body.replace:
            seen_pairs: set = set()
            for d in body.drawings:
                pair = (d.asset.upper(), d.timeframe.upper())
                if pair[0] and pair[1] and pair not in seen_pairs:
                    seen_pairs.add(pair)
                    db.delete_drawings_for_asset_timeframe(
                        pair[0], pair[1], user_id=user_id
                    )

        created_ids = []
        errors = []

        for idx, drawing_item in enumerate(body.drawings):
            drawing_data = drawing_item.model_dump()
            is_valid, error_msg = validate_drawing_data(drawing_data)
            if not is_valid:
                errors.append({"index": idx, "error": error_msg})
                continue

            try:
                drawing_id = db.save_drawing(
                    asset=drawing_item.asset,
                    timeframe=drawing_item.timeframe,
                    drawing_type=drawing_item.drawing_type,
                    price_level=drawing_item.price_level,
                    price_level_upper=drawing_item.price_level_upper,
                    label=drawing_item.label,
                    color=drawing_item.color or "#ff0000",
                    line_style=drawing_item.line_style,
                    line_width=drawing_item.line_width,
                    metadata=drawing_item.metadata,
                    user_id=user_id,
                )
                created_ids.append(drawing_id)
            except Exception as exc:
                errors.append({"index": idx, "error": str(exc)})

        return created_ids, errors

    created_ids, errors = await asyncio.to_thread(_bulk_create)

    if created_ids:
        affected_assets = list(
            {d.asset.upper() for d in body.drawings if d.asset}
        )
        _publish_sync(user_id, affected_assets, request)

    status_code = 201 if not errors else 207  # 207 = Multi-Status
    return JSONResponse(
        {
            "success": len(errors) == 0,
            "created_count": len(created_ids),
            "created_ids": created_ids,
            "errors": errors if errors else None,
        },
        status_code=status_code,
    )
