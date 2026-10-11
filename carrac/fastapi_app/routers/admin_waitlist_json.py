"""Admin waitlist management JSON endpoints.

Admin-only:
* ``GET  /api/admin/waitlist``         — counts by status + full list (newest-first, limit 200).
* ``POST /api/admin/waitlist/release`` — release a wave of N invites.
                                         Default dry_run=True — no emails sent unless
                                         dry_run is explicitly False.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from carrac.fastapi_app.deps.auth import require_admin
from carrac.fastapi_app.limiter import limiter

logger = logging.getLogger("carrac.fastapi.admin_waitlist")

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic body
# ---------------------------------------------------------------------------


class WaitlistReleaseRequest(BaseModel):
    count: int = Field(..., ge=1, le=100, description="How many waiting rows to invite (1-100)")
    cohort: Optional[str] = Field(None, description="Optional cohort tag stored on each row")
    dry_run: bool = Field(True, description="Default True — no emails/marks until explicitly False")

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# GET /api/admin/waitlist
# ---------------------------------------------------------------------------


@router.get("/api/admin/waitlist")
@limiter.limit("30/minute")
async def admin_waitlist_get(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
) -> JSONResponse:
    """Return waitlist counts by status plus the entry list (newest-first, limit 200)."""
    from user_db import user_db

    def _load_waitlist() -> dict:
        waiting = user_db.count_waitlist("waiting")
        invited = user_db.count_waitlist("invited")
        registered = user_db.count_waitlist("registered")
        total = user_db.count_waitlist()

        entries_raw = user_db.get_waitlist(status=None, limit=200)
        entries = [
            {
                "email": r.get("email"),
                "status": r.get("status"),
                "source": r.get("source"),
                "cohort": r.get("cohort"),
                "created_at": r.get("created_at"),
                "invited_at": r.get("invited_at"),
            }
            for r in entries_raw
        ]

        return {
            "counts": {
                "waiting": waiting,
                "invited": invited,
                "registered": registered,
                "total": total,
            },
            "entries": entries,
        }

    try:
        payload = await asyncio.to_thread(_load_waitlist)
        return JSONResponse(jsonable_encoder(payload))
    except Exception as e:
        logger.error("Waitlist GET failed: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# POST /api/admin/waitlist/release
# ---------------------------------------------------------------------------


@router.post("/api/admin/waitlist/release")
@limiter.limit("10/minute")
async def admin_waitlist_release(
    request: Request,
    body: WaitlistReleaseRequest,
    user: Annotated[dict, Depends(require_admin)],
) -> JSONResponse:
    """Release a wave of N invites.

    When ``dry_run`` is True (the default) the endpoint returns the would-be
    picks without sending any emails or mutating any rows.  Only when
    ``dry_run`` is explicitly ``false`` does it actually issue codes, send
    emails, and mark rows as invited.
    """

    count = body.count
    cohort = body.cohort
    dry_run = body.dry_run
    admin_user_id = user.get("id")

    logger.info(
        "Waitlist release request: count=%d cohort=%s dry_run=%s by admin=%s",
        count,
        cohort,
        dry_run,
        admin_user_id,
    )

    if dry_run:
        # Preview only — just pick without touching anything.
        from user_db import user_db

        try:
            picks = await asyncio.to_thread(user_db.pick_next_waitlist, count)
        except Exception as e:
            logger.error("pick_next_waitlist failed: %s", e, exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=500)

        return JSONResponse(
            jsonable_encoder(
                {
                    "dry_run": True,
                    "requested": count,
                    "released": [{"email": r["email"]} for r in picks],
                    "skipped": [],
                    "errors": [],
                }
            )
        )

    # Real release — delegate to waitlist_release.run().
    # collect per-address results by monkey-capturing the released/failed counts
    # from the run() return value; for structured per-email results we run the
    # loop ourselves so we can build the response payload.
    from user_db import user_db
    from carrac.email import send_invite_email

    try:
        picks = await asyncio.to_thread(user_db.pick_next_waitlist, count)
    except Exception as e:
        logger.error("pick_next_waitlist failed: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

    if not picks:
        return JSONResponse(
            jsonable_encoder(
                {
                    "dry_run": False,
                    "requested": count,
                    "released": [],
                    "skipped": [],
                    "errors": ["No waiting rows available"],
                }
            )
        )

    def _do_release():
        _released = []
        _skipped: list[dict] = []
        _errors: list[str] = []
        expires_days = 14

        for row in picks:
            email = row["email"]
            try:
                code = user_db.generate_invite_code(admin_user_id, expires_days=expires_days)
                if not code:
                    _errors.append(f"{email}: could not generate invite code")
                    continue
                if send_invite_email(email, code, expires_days=expires_days):
                    user_db.mark_waitlisted_invited(email, code, cohort=cohort)
                    try:
                        from carrac.integrations import on_funnel_event
                        on_funnel_event(None, "waitlist_invited", {"cohort": cohort or ""})
                    except Exception:
                        pass
                    try:
                        from carrac.waitlist_release import _espo_mark_invited
                        _espo_mark_invited(email, cohort)
                    except Exception:
                        pass
                    _released.append({"email": email, "invite_code": code})
                    logger.info("Waitlist: invited %s (code %s cohort=%s)", email, code, cohort)
                else:
                    _errors.append(f"{email}: email send failed (left waiting; code {code} orphaned)")
                    logger.warning("Waitlist: email send failed for %s", email)
            except Exception as exc:
                _errors.append(f"{email}: {exc}")
                logger.error("Waitlist release error for %s: %s", email, exc, exc_info=True)

        return _released, _skipped, _errors

    released, skipped, errors = await asyncio.to_thread(_do_release)

    return JSONResponse(
        jsonable_encoder(
            {
                "dry_run": False,
                "requested": count,
                "released": released,
                "skipped": skipped,
                "errors": errors,
            }
        )
    )
