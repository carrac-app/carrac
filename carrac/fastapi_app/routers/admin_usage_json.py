"""Admin per-user API/MCP usage dashboard endpoint.

GET /api/admin/usage
  - No ?user_id: returns cross-user ranking (top AI users, recent API keys,
    totals) for the given ?period (7d / 30d / all).
  - ?user_id=N: returns build_usage_response for that user plus their
    ai_usage_log summary and api_keys list.

Admin-only; mirrors the pattern established in admin_billing_json.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime as _dt, timedelta, timezone as _tz
from typing import Annotated

from fastapi import Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi import APIRouter

from carrac.fastapi_app.deps.auth import require_admin
from carrac.fastapi_app.limiter import limiter

logger = logging.getLogger("carrac.fastapi.admin_usage")

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/admin/usage
# ---------------------------------------------------------------------------


@router.get("/api/admin/usage")
@limiter.limit("60/minute")
async def admin_usage(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    period: str = "7d",
    user_id: int | None = None,
) -> JSONResponse:
    """Per-user API/MCP usage dashboard.

    Without ``?user_id``: cross-user ranking (top AI users + recent API keys +
    totals).  With ``?user_id=N``: drill into a single user's Redis history,
    ai_usage_log summary, and api_keys.
    """
    from carrac.db_pool import adapt_query, get_conn

    days = {"7d": 7, "30d": 30, "all": 3650}.get(period, 7)
    cutoff = (
        _dt.now(_tz.utc).replace(tzinfo=None) - timedelta(days=days)
    ).strftime("%Y-%m-%d %H:%M:%S")

    errors: list[str] = []

    # ------------------------------------------------------------------
    # Per-user drill (optional path)
    # ------------------------------------------------------------------
    if user_id is not None:
        return await _user_drill(user_id, period, days, cutoff, errors)

    # ------------------------------------------------------------------
    # Cross-user summary
    # ------------------------------------------------------------------
    def _load_cross_user_summary():
        with get_conn() as conn:
            # Top users by AI query volume (cross-user ranking)
            _top_ai_rows = conn.execute(
                adapt_query(
                    """
                    SELECT u.id, u.email, u.tier,
                           COUNT(*) AS queries,
                           COALESCE(SUM(l.cost_cents), 0) AS cost_cents,
                           COALESCE(SUM(l.input_tokens + l.output_tokens), 0) AS tokens
                    FROM ai_usage_log l
                    JOIN users u ON u.id = l.user_id
                    WHERE l.created_at >= ?
                    GROUP BY u.id, u.email, u.tier
                    ORDER BY queries DESC
                    LIMIT 25
                    """
                ),
                (cutoff,),
            ).fetchall()

            # Total AI queries and active API keys. Alias COUNT and read by
            # name — Postgres returns dict rows (positional [0] raises KeyError),
            # SQLite's Row supports name access too.
            _aq = conn.execute(
                adapt_query(
                    "SELECT COUNT(*) AS cnt FROM ai_usage_log WHERE created_at >= ?"
                ),
                (cutoff,),
            ).fetchone()
            _total_ai_queries = (_aq["cnt"] if _aq else 0) or 0

            _ak = conn.execute(
                adapt_query(
                    "SELECT COUNT(*) AS cnt FROM api_keys WHERE NOT revoked"
                )
            ).fetchone()
            _total_active_keys = (_ak["cnt"] if _ak else 0) or 0

            # Most-recently-used API keys (active only)
            _recent_key_rows = conn.execute(
                adapt_query(
                    """
                    SELECT ak.name, ak.token_suffix, ak.scope,
                           ak.last_used_at, u.email
                    FROM api_keys ak
                    LEFT JOIN users u ON u.id = ak.user_id
                    WHERE NOT ak.revoked
                    ORDER BY ak.last_used_at DESC NULLS LAST
                    LIMIT 15
                    """
                )
            ).fetchall()

            return _top_ai_rows, _total_ai_queries, _total_active_keys, _recent_key_rows

    try:
        top_ai_rows, total_ai_queries, total_active_keys, recent_key_rows = (
            await asyncio.to_thread(_load_cross_user_summary)
        )
    except Exception as exc:
        logger.error("admin_usage DB query failed: %s", exc, exc_info=True)
        return JSONResponse(
            {"error": str(exc), "period": period},
            status_code=500,
        )

    top_ai_users = [
        {
            "user_id": r["id"],
            "email": r["email"],
            "tier": r["tier"],
            "queries": r["queries"],
            "cost_usd": round((r["cost_cents"] or 0) / 100, 4),
            "tokens": r["tokens"] or 0,
        }
        for r in top_ai_rows
    ]

    recent_api_keys = [
        {
            "email": r["email"],
            "name": r["name"],
            "token_suffix": r["token_suffix"],
            "scope": r["scope"],
            "last_used_at": r["last_used_at"],
        }
        for r in recent_key_rows
    ]

    payload = {
        "period": period,
        "top_ai_users": top_ai_users,
        "recent_api_keys": recent_api_keys,
        "totals": {
            "ai_queries": total_ai_queries,
            "active_api_keys": total_active_keys,
        },
        "errors": errors,
    }
    return JSONResponse(jsonable_encoder(payload))


async def _user_drill(
    user_id: int,
    period: str,
    days: int,
    cutoff: str,
    errors: list[str],
) -> JSONResponse:
    """Build the per-user drill response."""
    from carrac.db_pool import adapt_query, get_conn
    from user_db import user_db

    # Redis usage history (graceful degradation)
    usage_payload: dict = {"user_id": user_id, "tier": "unknown"}

    def _load_usage_payload():
        user_row = user_db.get_user_by_id(user_id)
        tier = (user_row or {}).get("tier", "free") or "free"
        from carrac.usage import build_usage_response

        return build_usage_response(
            user_id, tier, include_history=True, history_days=days
        )

    try:
        usage_payload = await asyncio.to_thread(_load_usage_payload)
    except Exception as exc:
        errors.append(f"Redis history unavailable: {exc}")
        usage_payload["history"] = []

    # ai_usage_log summary for this user
    ai_summary: dict = {}

    def _load_ai_summary() -> dict:
        with get_conn() as conn:
            row = conn.execute(
                adapt_query(
                    """
                    SELECT COUNT(*) AS queries,
                           COALESCE(SUM(cost_cents), 0) AS cost_cents,
                           COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
                    FROM ai_usage_log
                    WHERE user_id = ? AND created_at >= ?
                    """
                ),
                (user_id, cutoff),
            ).fetchone()
            if row:
                return {
                    "queries": row["queries"] or 0,
                    "cost_usd": round((row["cost_cents"] or 0) / 100, 4),
                    "tokens": row["tokens"] or 0,
                    "period": period,
                }
            return {}

    try:
        ai_summary = await asyncio.to_thread(_load_ai_summary)
    except Exception as exc:
        errors.append(f"ai_usage_log query failed: {exc}")

    # API keys for this user
    api_keys: list = []
    try:
        api_keys = await asyncio.to_thread(user_db.list_api_keys, user_id)
    except Exception as exc:
        errors.append(f"api_keys query failed: {exc}")

    payload = {
        **usage_payload,
        "ai_summary": ai_summary,
        "api_keys": api_keys,
        "errors": errors,
    }
    return JSONResponse(jsonable_encoder(payload))
