"""Cross-runtime session debug route.

``/api/v2/_whoami`` returns the current session user as JSON. It is the
shadow-login sanity check, exercised by the cross-runtime gate
test in ``tests/fastapi/test_auth.py``.

The route is env-gated by ``WHOAMI_DEBUG=1``. In production it is OFF;
``GET /api/v2/_whoami`` returns 404. A later release removes this route once
production has migrated authed endpoints that surface the same data.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from carrac.fastapi_app.deps.auth import RateCtxDep, current_user

router = APIRouter()


def _enabled() -> bool:
    return os.getenv("WHOAMI_DEBUG", "").strip() in ("1", "true", "True", "yes")


@router.get("/_whoami", include_in_schema=False)
def whoami(
    request: Request,
    ctx: RateCtxDep,
) -> dict[str, Any]:
    """Return the current session user (read-only).

    Returns 404 unless ``WHOAMI_DEBUG=1`` is set in the process env. When
    enabled, returns ``{user_id, tier, source, ...}`` describing what the
    FastAPI auth chain resolved for the current request — used by the
    Cross-runtime gate to prove the session middleware reads the
    same Redis blob Flask wrote at login.
    """
    if not _enabled():
        raise HTTPException(status_code=404, detail="Not Found")

    user: Optional[dict] = current_user(request, ctx)
    if user is not None:
        return {
            "source": "session",
            "user_id": user["id"],
            "email": user.get("email"),
            "display_name": user.get("display_name"),
            "is_admin": user.get("is_admin", False),
            "tier": user.get("tier", "free"),
            "rate_pool": ctx.pool,
        }
    if ctx.pool == "api_key":
        return {
            "source": "api_key",
            "user_id": ctx.api_key_user_id,
            "tier": ctx.tier,
            "scope": ctx.api_key_scope,
            "rate_pool": ctx.pool,
        }
    return {
        "source": "anonymous",
        "user_id": None,
        "tier": ctx.tier,
        "rate_pool": ctx.pool,
    }
