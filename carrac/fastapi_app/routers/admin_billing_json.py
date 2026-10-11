"""Admin + billing JSON endpoints.

Ports the small JSON GETs/POSTs that didn't fit into the earlier ports:

Admin (require_admin):
* ``GET    /api/security-status``
* ``GET    /api/admin/costs``
* ``GET    /api/admin/assets``
* ``POST   /api/admin/assets``
* ``DELETE /api/admin/assets/{pair:path}``

Public-with-auth-variants:
* ``POST   /api/csp-report``        — no auth, CSRF-exempt
* ``POST   /api/feedback``          — login_required JSON
* ``GET    /api/billing/topup-check`` — login_required
* ``GET    /api/billing/status``      — login_required
* ``POST   /api/billing/portal``      — login_required

The CSP-violation counter and notifier helper are imported from the Flask
admin module so both runtimes share one source of truth — there is no
Flask-only state that must be re-implemented here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime as _dt, timedelta, timezone as _tz
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    require_admin,
    require_login,
)
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.admin_billing import (
    AddAssetRequest,
    FeedbackRequest,
)

logger = logging.getLogger("carrac.fastapi.admin_billing")

router = APIRouter()


# ---------------------------------------------------------------------------
# Admin: GET /api/security-status
# ---------------------------------------------------------------------------


@router.get("/api/security-status")
@limiter.limit("60/minute")
async def security_status(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
) -> JSONResponse:
    """Admin-only security monitoring snapshot.

    """
    from security import brute_force_protection
    from carrac.db_pool import adapt_query, get_conn
    from carrac.admin import routes as _admin_module

    def _build_status() -> dict:
        status: dict = {}

        with brute_force_protection._lock:
            status["locked_accounts"] = len(brute_force_protection._lockouts)
            status["tracked_attempts"] = len(brute_force_protection._attempts)

        try:
            with get_conn() as conn:
                row = conn.execute(
                    adapt_query(
                        "SELECT id, created_at FROM reports "
                        "WHERE report_type='morning' ORDER BY id DESC LIMIT 1"
                    )
                ).fetchone()
                if row:
                    # Postgres RealDictCursor rows are dict-keyed — row[0] raises
                    # KeyError (swallowed → panel silently blank). Read by name.
                    status["last_report_id"] = row["id"]
                    status["last_report_at"] = row["created_at"]
        except Exception:
            status["last_report_id"] = None

        with _admin_module._csp_lock:
            status["csp_violations_24h"] = _admin_module._csp_violation_count

        return status

    status = await asyncio.to_thread(_build_status)
    # last_report_at is a datetime on Postgres.
    return JSONResponse(jsonable_encoder(status))


# ---------------------------------------------------------------------------
# CSP report — POST /api/csp-report (no auth, CSRF-exempt)
# ---------------------------------------------------------------------------


@router.post("/api/csp-report", status_code=204)
@limiter.limit("120/minute")
async def csp_report(request: Request, ctx: RateCtxDep) -> Response:
    """Receive and log CSP violation reports.

    Browsers send the report with content-type ``application/csp-report``
    or ``application/reports+json``. Parse the raw body manually so any
    well-formed JSON is accepted regardless of the declared content-type.
    """
    from carrac.admin import routes as _admin_module

    try:
        raw = await request.body()
        report = json.loads(raw.decode("utf-8")) if raw else None
        if report:
            violation = report.get("csp-report", report)
            logger.warning(
                "[SECURITY] CSP_VIOLATION | "
                "directive: %s | blocked: %s | page: %s",
                violation.get("violated-directive", "unknown"),
                violation.get("blocked-uri", "unknown"),
                violation.get("document-uri", "unknown"),
            )
            with _admin_module._csp_lock:
                _admin_module._csp_violation_count += 1
    except Exception:
        # Best-effort logging — never break browser CSP reporting on parse errors.
        pass

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Feedback — POST /api/feedback
# ---------------------------------------------------------------------------


@router.post("/api/feedback")
@limiter.limit("30/minute")
async def submit_feedback(
    request: Request,
    body: FeedbackRequest,
    user: Annotated[dict, Depends(require_login)],
) -> JSONResponse:
    """Submit a bug report or feature request.

    Note: ``_notify_feedback_submission`` accepts a Flask session by duck
    type — it only calls ``.get('email')`` / ``.get('user_id')`` on it,
    so we pass the FastAPI ``request.state.session`` dict (which has the
    same Flask-Session key shape).
    """
    from carrac.admin.routes import _notify_feedback_submission
    from user_db import user_db

    if body.type not in ("bug", "feature"):
        return JSONResponse({"error": "Invalid type"}, status_code=400)
    description = (body.description or "").strip()
    if not description:
        return JSONResponse({"error": "Description is required"}, status_code=400)

    sess = getattr(request.state, "session", {}) or {}

    fb_id = await asyncio.to_thread(
        user_db.create_feedback,
        user_id=user["id"],
        user_email=sess.get("email", "") or user.get("email", ""),
        fb_type=body.type,
        area=(body.area or "").strip()[:100],
        description=description.strip()[:2000],
        expected=(body.expected or "").strip()[:2000],
        actual=(body.actual or "").strip()[:2000],
        how_should_work=(body.how_should_work or "").strip()[:2000],
        extra=(body.extra or "").strip()[:2000],
    )
    if fb_id:
        logger.info(
            "Feedback #%s (%s) from %s: %s",
            fb_id,
            body.type,
            sess.get("email", "?"),
            body.area,
        )
        try:
            await asyncio.to_thread(
                _notify_feedback_submission, fb_id, body.type, body.area, description, sess
            )
        except Exception as e:
            logger.warning("Feedback notifier failed for #%s: %s", fb_id, e)
        return JSONResponse({"ok": True, "id": fb_id})
    return JSONResponse({"error": "Failed to save"}, status_code=500)


# ---------------------------------------------------------------------------
# Admin: GET /api/admin/costs
# ---------------------------------------------------------------------------


@router.get("/api/admin/costs")
@limiter.limit("30/minute")
async def admin_costs(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    period: str = "7d",
) -> JSONResponse:
    """Cost breakdown across all AI providers and features."""
    from carrac.db_pool import adapt_query, get_conn
    from user_db import user_db

    days = {"7d": 7, "30d": 30, "all": 3650}.get(period, 7)
    cutoff = (_dt.now(_tz.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def _load_costs() -> dict:
        with get_conn() as conn:
            rows = conn.execute(
                adapt_query(
                    """
                    SELECT DATE(created_at) as day, feature, model,
                           COUNT(*) as queries,
                           COALESCE(SUM(input_tokens), 0) as total_input,
                           COALESCE(SUM(output_tokens), 0) as total_output,
                           COALESCE(SUM(cost_cents), 0) as total_cost_cents,
                           COALESCE(SUM(charged_cents), 0) as total_charged_cents
                    FROM ai_usage_log
                    WHERE created_at >= ?
                    GROUP BY DATE(created_at), feature, model
                    ORDER BY day DESC, total_cost_cents DESC
                    """
                ),
                (cutoff,),
            ).fetchall()
            totals = conn.execute(
                adapt_query(
                    """
                    SELECT feature, model, COUNT(*) as queries,
                           COALESCE(SUM(cost_cents), 0) as total_cost_cents,
                           COALESCE(SUM(charged_cents), 0) as total_charged_cents
                    FROM ai_usage_log
                    WHERE created_at >= ?
                    GROUP BY feature, model
                    ORDER BY total_cost_cents DESC
                    """
                ),
                (cutoff,),
            ).fetchall()

            platform_econ = user_db.get_platform_economics(days)
            top_users = user_db.get_top_cost_users(days=days, limit=15)
            tier_econ = user_db.get_tier_economics(days=days)

            return {
                "period": period,
                "days": days,
                "daily": [dict(r) for r in rows],
                "totals": [dict(r) for r in totals],
                "platform": platform_econ,
                "top_users": top_users,
                "tier_breakdown": tier_econ,
            }

    try:
        payload = await asyncio.to_thread(_load_costs)
        # jsonable_encoder coerces date/datetime/Decimal anywhere in the
        # payload (DATE(created_at) and the econ helpers all emit dates).
        return JSONResponse(jsonable_encoder(payload))
    except Exception as e:
        logger.error("Cost query failed: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Admin: asset registry
# ---------------------------------------------------------------------------


@router.get("/api/admin/assets")
@limiter.limit("60/minute")
async def list_assets(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
) -> JSONResponse:
    """Snapshot of the active asset registry."""
    from carrac.assets.registry import categories, chart_pairs, ratio_set

    return JSONResponse(
        {
            "pairs": chart_pairs(),
            "categories": categories(),
            "ratios": sorted(ratio_set()),
        }
    )


@router.post("/api/admin/assets")
@limiter.limit("30/minute")
async def add_asset(
    request: Request,
    body: AddAssetRequest,
    user: Annotated[dict, Depends(require_admin)],
) -> JSONResponse:
    """Add a chart pair to the registry."""
    from carrac.assets.registry import add_pair

    pair = (body.pair or "").strip()
    if not pair or "/" not in pair:
        return JSONResponse(
            {"error": "pair must be 'SYMBOL/TF' format (e.g. 'SOL/1D')"},
            status_code=400,
        )
    try:
        add_pair(pair, category=body.category, ratio_eligible=body.ratio_eligible)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "pair": pair,
            "category": body.category,
            "ratio_eligible": body.ratio_eligible,
        }
    )


@router.delete("/api/admin/assets/{pair:path}")
@limiter.limit("30/minute")
async def remove_asset(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    pair: str = Path(..., description="'SYMBOL/TF' (slash allowed)"),
) -> JSONResponse:
    """Remove a chart pair from the active registry.

    The ``{pair:path}`` converter accepts the embedded slash so the URL
    shape ``/api/admin/assets/SOL/1D`` works (matches the Flask
    ``<path:pair>`` converter exactly).
    """
    from carrac.assets.registry import remove_pair

    if "/" not in pair:
        return JSONResponse(
            {"error": "pair must be 'SYMBOL/TF' format"},
            status_code=400,
        )
    remove_pair(pair)
    return JSONResponse({"ok": True, "pair": pair})


# ---------------------------------------------------------------------------
# Billing — non-webhook JSON endpoints
# ---------------------------------------------------------------------------


@router.get("/api/billing/topup-check")
@limiter.limit("60/minute")
async def topup_eligibility(
    request: Request,
    user: Annotated[dict, Depends(require_login)],
) -> JSONResponse:
    """Defense-in-depth gate: 403 if tier disallows top-ups."""
    from carrac.billing.routes import _tier_allows_topup

    allowed = await asyncio.to_thread(_tier_allows_topup, user["id"])
    if not allowed:
        return JSONResponse(
            {
                "allowed": False,
                "reason": "Your current plan does not include AI credit top-ups.",
            },
            status_code=403,
        )
    return JSONResponse({"allowed": True})


@router.get("/api/billing/status")
@limiter.limit("60/minute")
async def billing_status(
    request: Request,
    user: Annotated[dict, Depends(require_login)],
) -> JSONResponse:
    """Tier + credit balance + subscription details."""
    from carrac.config import Config
    from user_db import user_db

    user_id = user["id"]
    sess = getattr(request.state, "session", {}) or {}
    tier = "admin" if sess.get("is_admin") else sess.get("tier", "free")

    def _load_billing_status():
        _balance = user_db.get_ai_balance(user_id)
        _stats = user_db.get_ai_usage_stats(user_id)
        _sub_info = user_db.check_subscription_expiry(user_id)
        _user_row = user_db.get_user_by_id(user_id)
        _has_paddle = bool(_user_row.get("paddle_customer_id")) if _user_row else False
        return _balance, _stats, _sub_info, _has_paddle

    balance, stats, sub_info, has_paddle = await asyncio.to_thread(_load_billing_status)

    # expires_at is a datetime on Postgres.
    return JSONResponse(
        jsonable_encoder({
            "tier": tier,
            "balance_cents": balance,
            "balance": f"${balance / 100:.2f}",
            "total_deposited_cents": stats.get("total_deposited_cents", 0),
            "total_spent_cents": stats.get("total_spent_cents", 0),
            "total_queries": stats.get("total_queries", 0),
            "expires_at": sub_info.get("expires_at"),
            "days_remaining": sub_info.get("days_remaining"),
            "expired": sub_info.get("expired", False),
            "has_paddle": has_paddle,
            "paddle_configured": Config.paddle_configured(),
        })
    )


@router.post("/api/billing/portal")
@limiter.limit("10/minute")
async def billing_portal(
    request: Request,
    user: Annotated[dict, Depends(require_login)],
) -> JSONResponse:
    """Create a Paddle customer portal session. Returns ``{url}``."""
    import requests as http_requests

    from carrac.billing.routes import _paddle_api_base, _paddle_headers
    from carrac.config import Config
    from user_db import user_db

    if not Config.paddle_configured():
        return JSONResponse(
            {"error": "Payment provider not configured"}, status_code=503
        )

    def _create_portal_session() -> str:
        user_row = user_db.get_user_by_id(user["id"])
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")

        paddle_cid = user_row.get("paddle_customer_id")
        if not paddle_cid:
            raise HTTPException(status_code=404, detail="No billing account linked")

        base = _paddle_api_base()
        resp = http_requests.post(
            f"{base}/customers/{paddle_cid}/portal-sessions",
            headers=_paddle_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"]["urls"]["general"]["overview"]

    try:
        portal_url = await asyncio.to_thread(_create_portal_session)
        return JSONResponse({"url": portal_url})
    except HTTPException as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        logger.error("Paddle portal session failed: %s", e)
        return JSONResponse(
            {"error": "Failed to create billing portal session"}, status_code=502
        )
