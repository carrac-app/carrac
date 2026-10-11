"""Widget token write endpoints.

Two endpoints:

* ``POST   /api/widget/tokens``         — create a new widget token
* ``DELETE /api/widget/tokens/{token_id}`` — revoke a widget token

Security note: widget token writes have NO X-API-Key bypass for CSRF.
``CarracCSRFMiddleware`` applies its API-key bypass to every path except
``/api/widget/tokens``, so a request there is always checked against the
session.

These handlers therefore:
- Use ``auth_or_key`` for identity resolution (session OR API key).
- Do NOT add a second X-API-Key skip — the middleware deliberately has none
  for this path.
- Raise 401 for unauthenticated callers (shared admin key has no user_id
  and therefore cannot create personal widget tokens).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import RateCtxDep, auth_or_key
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.widget import WidgetTokenCreateRequest

logger = logging.getLogger("carrac.fastapi.widget")

router = APIRouter()


def _require_user_id(auth: dict) -> int:
    """Resolve user_id; 401 if absent (shared admin key or unauthenticated)."""
    if auth.get("source") == "api_key":
        uid = auth.get("user_id")
    else:
        user = auth.get("user") or {}
        uid = user.get("id")
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return uid


# ---------------------------------------------------------------------------
# POST /api/widget/tokens
# ---------------------------------------------------------------------------


@router.post("/api/widget/tokens", status_code=201)
@limiter.limit("30/minute")
async def create_token(
    request: Request,
    body: WidgetTokenCreateRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a new widget token. Returns the full token once.

    Enforces ``MAX_TOKENS_PER_USER`` active-token limit.
    """
    user_id = _require_user_id(auth)

    from carrac.widget.auth import (
        MAX_TOKENS_PER_USER,
        generate_token,
        get_token_suffix,
        hash_token,
    )
    from user_db import user_db

    name = body.name  # already max_length=50 from Pydantic

    def _create_or_reject():
        # Enforce the active-token limit.
        existing = user_db.list_widget_tokens(user_id)
        active = [t for t in existing if not t.get("revoked")]
        if len(active) >= MAX_TOKENS_PER_USER:
            return None

        token = generate_token()
        token_h = hash_token(token)
        suffix = get_token_suffix(token)
        token_id = user_db.create_widget_token(user_id, token_h, suffix, name)
        return token_id, token, suffix

    result = await asyncio.to_thread(_create_or_reject)
    if result is None:
        return JSONResponse(
            {"error": f"Maximum {MAX_TOKENS_PER_USER} active tokens allowed"},
            status_code=400,
        )
    token_id, token, suffix = result
    if not token_id:
        return JSONResponse({"error": "Failed to create token"}, status_code=500)

    logger.info("Widget token created for user %s: ...%s", user_id, suffix)

    return JSONResponse(
        {
            "id": token_id,
            "token": token,  # full token — shown once, never again
            "suffix": suffix,
            "name": name,
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# DELETE /api/widget/tokens/{token_id}
# ---------------------------------------------------------------------------


@router.delete("/api/widget/tokens/{token_id}")
@limiter.limit("30/minute")
async def revoke_token(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    token_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Revoke a widget token owned by the caller.

    Ownership is enforced by ``user_db.revoke_widget_token(token_id, user_id)``.
    """
    user_id = _require_user_id(auth)

    from user_db import user_db

    success = await asyncio.to_thread(user_db.revoke_widget_token, token_id, user_id)
    if not success:
        return JSONResponse({"error": "Token not found"}, status_code=404)

    logger.info("Widget token %s revoked by user %s", token_id, user_id)
    return JSONResponse({"success": True})
