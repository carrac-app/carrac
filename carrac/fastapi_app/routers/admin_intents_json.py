"""Admin MCP request-intent telemetry endpoints.

GET /api/admin/intents
    Live feed (newest-first), filterable by tool / tier / date / free-text.
    Polled every 60s by templates/admin_intents.html — plain polling, not SSE.

GET /api/admin/intents/unmet
    The separate, pinned "Unmet Needs" panel — rows where unmet_need is set,
    independent of the main feed's filters (it's meant to stay visible
    regardless of what the live feed is currently filtered to).

Admin-only; mirrors the pattern established in admin_usage_json.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime as _dt, timedelta, timezone as _tz
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import require_admin
from carrac.fastapi_app.limiter import limiter

logger = logging.getLogger("carrac.fastapi.admin_intents")

router = APIRouter()

MAX_LIMIT = 500
DEFAULT_LIMIT = 100
DEFAULT_UNMET_DAYS = 30


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "ts": row["ts"],
        "tool": row["tool"],
        "tier": row["tier"],
        "user_ref": row["user_ref"],
        "user_intent": row["user_intent"],
        "unmet_need": row["unmet_need"],
        "latency_ms": row["latency_ms"],
        "ok": bool(row["ok"]),
        "result_empty": bool(row["result_empty"]),
        "error_kind": row["error_kind"],
        "prev_tool": row["prev_tool"],
    }


# ---------------------------------------------------------------------------
# GET /api/admin/intents
# ---------------------------------------------------------------------------


@router.get("/api/admin/intents")
@limiter.limit("60/minute")
async def admin_intents(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    tool: Optional[str] = None,
    tier: Optional[str] = None,
    date: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> JSONResponse:
    """Live feed of mcp_intent_log rows, newest-first.

    ``date`` is a single UTC day (``YYYY-MM-DD``); ``q`` is a free-text
    substring match over tool / user_intent / unmet_need.
    """
    from carrac.db_pool import adapt_query, get_conn
    from carrac.mcp.registry import TIER_RANK, TOOL_REGISTRY

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    where = []
    params: list = []
    if tool:
        where.append("tool = ?")
        params.append(tool)
    if tier:
        where.append("tier = ?")
        params.append(tier)
    if date:
        try:
            day = _dt.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return JSONResponse({"error": f"Invalid date {date!r}, expected YYYY-MM-DD"}, status_code=400)
        start = day.strftime("%Y-%m-%d 00:00:00")
        end = (day + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        where.append("ts >= ? AND ts < ?")
        params.extend([start, end])
    if q:
        needle = f"%{q}%"
        where.append("(tool LIKE ? OR user_intent LIKE ? OR unmet_need LIKE ?)")
        params.extend([needle, needle, needle])

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    def _load():
        with get_conn() as conn:
            rows = conn.execute(
                adapt_query(
                    f"""
                    SELECT id, ts, tool, tier, user_ref, user_intent, unmet_need,
                           latency_ms, ok, result_empty, error_kind, prev_tool
                    FROM mcp_intent_log
                    {where_sql}
                    ORDER BY ts DESC
                    LIMIT ?
                    """
                ),
                (*params, limit),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    try:
        result = await asyncio.to_thread(_load)
    except Exception as exc:
        logger.error("admin_intents query failed: %s", exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)

    payload = {
        "rows": result,
        "count": len(result),
        "tools": sorted(TOOL_REGISTRY.keys()),
        "tiers": list(TIER_RANK.keys()),
    }
    return JSONResponse(jsonable_encoder(payload))


# ---------------------------------------------------------------------------
# GET /api/admin/intents/unmet
# ---------------------------------------------------------------------------


@router.get("/api/admin/intents/unmet")
@limiter.limit("60/minute")
async def admin_intents_unmet(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    days: int = DEFAULT_UNMET_DAYS,
    limit: int = DEFAULT_LIMIT,
) -> JSONResponse:
    """The pinned "Unmet Needs" panel — rows with a non-null unmet_need,
    newest-first. Independent of the main feed's tool/tier/q filters."""
    from carrac.db_pool import adapt_query, get_conn

    days = max(1, min(int(days or DEFAULT_UNMET_DAYS), 3650))
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    cutoff = (_dt.now(_tz.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    def _load():
        with get_conn() as conn:
            rows = conn.execute(
                adapt_query(
                    """
                    SELECT id, ts, tool, tier, user_ref, user_intent, unmet_need,
                           latency_ms, ok, result_empty, error_kind, prev_tool
                    FROM mcp_intent_log
                    WHERE unmet_need IS NOT NULL AND ts >= ?
                    ORDER BY ts DESC
                    LIMIT ?
                    """
                ),
                (cutoff, limit),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    try:
        result = await asyncio.to_thread(_load)
    except Exception as exc:
        logger.error("admin_intents_unmet query failed: %s", exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse(jsonable_encoder({"rows": result, "count": len(result), "days": days}))
