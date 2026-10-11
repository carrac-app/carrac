"""Watchlist write endpoints.

Five endpoints:

* ``POST /api/watchlist``        — save full watchlist (list OR dict)
* ``POST /api/watchlist/add``    — append a symbol
* ``POST /api/watchlist/remove`` — remove a symbol
* ``POST /api/watchlist/reorder``— reorder flat list
* ``POST /api/watchlist/groups`` — full-state save of grouped watchlist

Validation strategy: the Pydantic body model enforces shape and
``extra="forbid"`` where applicable.  ``POST /api/watchlist`` accepts
either a flat ``list[str]`` or a legacy grouped ``dict`` — both handled
via ``Union`` at the route level using ``Body(...)``.  No deep validator
is needed; the user_db calls handle NULL/empty gracefully.

Auth: session-only (``@login_required`` in Flask).  API key callers can
still reach the route via ``auth_or_key`` but the watchlist is user-scoped
so a ``None`` user_id (shared admin key) returns 401.

CSRF: ``assets_bp`` is NOT csrf.exempt — ``CarracCSRFMiddleware`` enforces
CSRF for session callers (watchlist is NOT in ``_SKIP_PREFIXES``).  The
X-API-Key bypass branch in the middleware handles API key callers.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import RateCtxDep, auth_or_key
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.watchlist import WatchlistSymbolRequest

router = APIRouter()


def _require_user_id(auth: dict) -> int:
    """Resolve user_id from auth dict, raising 401 if absent."""
    if auth.get("source") == "api_key":
        uid = auth.get("user_id")
    else:
        user = auth.get("user") or {}
        uid = user.get("id")
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return uid


# ---------------------------------------------------------------------------
# POST /api/watchlist
# ---------------------------------------------------------------------------


@router.post("/api/watchlist")
@limiter.limit("60/minute")
async def save_watchlist(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    body: Union[list[str], dict] = Body(...),
) -> JSONResponse:
    """Save the caller's full watchlist.

    Accepts either a flat array ``["BTC", "ETH"]`` or the legacy grouped
    dict format.
    """
    user_id = _require_user_id(auth)

    from user_db import user_db

    if isinstance(body, list):
        ok = await asyncio.to_thread(user_db.save_flat_watchlist, user_id, body)
    else:
        from security import validate_watchlist_data

        ok_shape, err = validate_watchlist_data(body)
        if not ok_shape:
            return JSONResponse({"error": err}, status_code=400)
        ok = await asyncio.to_thread(user_db.save_watchlist, user_id, body)

    if not ok:
        return JSONResponse({"error": "Failed to save watchlist"}, status_code=500)
    return JSONResponse({"success": True})


# ---------------------------------------------------------------------------
# POST /api/watchlist/add
# ---------------------------------------------------------------------------


@router.post("/api/watchlist/add")
@limiter.limit("60/minute")
async def add_to_watchlist(
    request: Request,
    body: WatchlistSymbolRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Append a symbol to the caller's watchlist (no duplicates).

    The field_validator on ``WatchlistSymbolRequest.symbol`` strips + uppercases.
    """
    user_id = _require_user_id(auth)

    from user_db import user_db

    def _do_add():
        if user_db.add_to_watchlist(user_id, body.symbol):
            return True, user_db.get_flat_watchlist(user_id)
        return False, None

    ok, symbols = await asyncio.to_thread(_do_add)
    if ok:
        return JSONResponse({"success": True, "symbols": symbols})
    return JSONResponse({"error": "Failed to add symbol"}, status_code=500)


# ---------------------------------------------------------------------------
# POST /api/watchlist/remove
# ---------------------------------------------------------------------------


@router.post("/api/watchlist/remove")
@limiter.limit("60/minute")
async def remove_from_watchlist(
    request: Request,
    body: WatchlistSymbolRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Remove a symbol from the caller's watchlist.

    """
    user_id = _require_user_id(auth)

    from user_db import user_db

    def _do_remove():
        if user_db.remove_from_watchlist(user_id, body.symbol):
            return True, user_db.get_flat_watchlist(user_id)
        return False, None

    ok, symbols = await asyncio.to_thread(_do_remove)
    if ok:
        return JSONResponse({"success": True, "symbols": symbols})
    return JSONResponse({"error": "Failed to remove symbol"}, status_code=500)


# ---------------------------------------------------------------------------
# POST /api/watchlist/reorder
# ---------------------------------------------------------------------------


@router.post("/api/watchlist/reorder")
@limiter.limit("60/minute")
async def reorder_watchlist(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    body: list[str] = Body(...),
) -> JSONResponse:
    """Persist a new symbol order for the caller's watchlist.

    Body must be a JSON array of symbol strings.
    """
    user_id = _require_user_id(auth)

    from user_db import user_db

    ok = await asyncio.to_thread(user_db.save_flat_watchlist, user_id, body)
    if ok:
        return JSONResponse({"success": True})
    return JSONResponse({"error": "Failed to reorder watchlist"}, status_code=500)


# ---------------------------------------------------------------------------
# POST /api/watchlist/groups — custom asset groups
# ---------------------------------------------------------------------------


@router.post("/api/watchlist/groups")
@limiter.limit("60/minute")
async def save_watchlist_groups(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    body: dict = Body(...),
) -> JSONResponse:
    """Full-state save of the caller's grouped watchlist.

    Body: {"groups": [{"id", "name", "symbols": [...]}, ...]} — validated by
    security.validate_watchlist_data (wired in for the first time here) plus
    an id-uniqueness check the validator doesn't cover.
    """
    user_id = _require_user_id(auth)

    from security import validate_watchlist_data

    ok_shape, err = validate_watchlist_data(body)
    if not ok_shape:
        return JSONResponse({"error": err}, status_code=400)

    groups = body.get("groups", [])
    ids = [str(g.get("id") or "") for g in groups]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        return JSONResponse({"error": "Group ids must be unique and non-empty"},
                            status_code=400)

    from user_db import user_db

    ok = await asyncio.to_thread(user_db.save_watchlist_groups, user_id, groups)
    if ok:
        return JSONResponse({"success": True})
    return JSONResponse({"error": "Failed to save watchlist groups"}, status_code=500)
