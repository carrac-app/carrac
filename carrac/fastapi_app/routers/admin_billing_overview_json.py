"""Admin billing/subscription overview JSON endpoint.

GET /api/admin/billing/overview — admin-only snapshot of subscription health,
MRR/ARR, Paddle webhook backlog, and expiring subscriptions.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime as _dt, timezone as _tz
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import require_admin
from carrac.fastapi_app.limiter import limiter

logger = logging.getLogger("carrac.fastapi.admin_billing_overview")

router = APIRouter()

# Monthly subscription prices in cents (member $34.99, premium $69.00).
# Source: carrac/tiers.py tier vocabulary.
PRICES: dict[str, int] = {
    "member": 3499,
    "premium": 6900,
}


def _col(row, idx, name):
    """Read a column from a DB row by name or index, whichever the driver gives.

    PostgreSQL returns dict-keyed rows (index access raises KeyError); SQLite
    returns tuples (name access raises) or sqlite3.Row (both work). Try name
    first, fall back to positional index.
    """
    if row is None:
        return None
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        try:
            return row[idx]
        except (KeyError, IndexError, TypeError):
            return None


# ---------------------------------------------------------------------------
# GET /api/admin/billing/overview
# ---------------------------------------------------------------------------


@router.get("/api/admin/billing/overview")
@limiter.limit("60/minute")
async def billing_overview(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
) -> JSONResponse:
    """Admin-only billing/subscription dashboard snapshot.

    Degrades gracefully: each sub-query is wrapped individually so a single
    DB error doesn't 500 the whole response — the failed section is returned
    as null/empty and the error is appended to the top-level ``errors`` list.

    NOTE on DB routing: all tables (users, paddle_webhook_inbox,
    pending_paddle_events) live in the same database — user_db's connection
    in both PostgreSQL (shared schema) and SQLite (users.db). We therefore
    use user_db._get_conn() for all queries so the router works correctly in
    both the test SQLite environment and production PostgreSQL.
    """
    from carrac.config import Config
    from carrac.db_pool import adapt_query
    from user_db import user_db

    errors: list[str] = []
    # Use an ISO-format "now" string as a query parameter so the comparison
    # works in both SQLite (text comparison) and PostgreSQL (TIMESTAMPTZ).
    now_iso = _dt.now(_tz.utc).isoformat()

    # ------------------------------------------------------------------
    # Active subscriptions + MRR/ARR
    # ------------------------------------------------------------------
    active_counts: dict[str, int] = {"member": 0, "premium": 0, "total": 0}
    mrr_usd: float = 0.0
    arr_usd: float = 0.0
    totals_section: dict[str, int] = {"paying_users": 0, "free_users": 0, "trialing": 0}

    def _load_subscription_counts():
        _active_counts = {"member": 0, "premium": 0, "total": 0}
        _totals_section = {"paying_users": 0, "free_users": 0, "trialing": 0}

        conn = user_db._get_conn()
        rows = conn.execute(
            adapt_query(
                """
                SELECT tier, COUNT(*) AS cnt
                FROM users
                WHERE tier IN ('member', 'premium')
                  AND (subscription_expires_at IS NULL
                       OR subscription_expires_at > ?)
                GROUP BY tier
                """
            ),
            (now_iso,),
        ).fetchall()
        conn.commit()
        for row in rows:
            tier_name = _col(row, 0, "tier")
            cnt = _col(row, 1, "cnt")
            if tier_name in _active_counts:
                _active_counts[tier_name] = cnt
        _active_counts["total"] = _active_counts["member"] + _active_counts["premium"]

        _mrr_cents = sum(_active_counts.get(t, 0) * p for t, p in PRICES.items())
        _mrr_usd = _mrr_cents / 100.0
        _arr_usd = _mrr_usd * 12.0

        # Total user breakdown
        user_tier_row = conn.execute(
            adapt_query(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN tier IN ('member','premium','tester') THEN 1 ELSE 0 END), 0) AS paying,
                    COALESCE(SUM(CASE WHEN tier = 'free' THEN 1 ELSE 0 END), 0) AS free_count
                FROM users
                WHERE is_active IS NOT FALSE
                """
            )
        ).fetchone()
        conn.commit()
        if user_tier_row:
            _totals_section["paying_users"] = _col(user_tier_row, 0, "paying") or 0
            _totals_section["free_users"] = _col(user_tier_row, 1, "free_count") or 0

        return _active_counts, _mrr_usd, _arr_usd, _totals_section

    try:
        active_counts, mrr_usd, arr_usd, totals_section = await asyncio.to_thread(
            _load_subscription_counts
        )
    except Exception as exc:
        logger.error("billing_overview: subscription query failed: %s", exc, exc_info=True)
        errors.append(f"subscription_counts: {exc}")

    # ------------------------------------------------------------------
    # Paddle configuration flag
    # ------------------------------------------------------------------
    paddle_configured = bool(
        getattr(Config, "PADDLE_API_KEY", "") and getattr(Config, "PADDLE_CLIENT_TOKEN", "")
    )

    # ------------------------------------------------------------------
    # Expiring soon (within 7 days)
    # ------------------------------------------------------------------
    expiring_soon: list[dict] = []

    def _load_expiring_soon() -> list[dict]:
        _expiring_soon: list[dict] = []
        raw_expiring = user_db.get_expiring_subscriptions(within_days=7)
        now = _dt.now(_tz.utc)
        # Also fetch each user's tier via the user_db connection
        conn = user_db._get_conn()
        for row in raw_expiring:
            exp_at = row.get("subscription_expires_at")
            days_left: int | None = None
            if exp_at:
                try:
                    if isinstance(exp_at, str):
                        exp_dt = _dt.fromisoformat(exp_at.replace("Z", "+00:00"))
                    else:
                        exp_dt = exp_at
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=_tz.utc)
                    days_left = max(0, (exp_dt - now).days)
                except Exception:
                    pass

            tier_val = "unknown"
            try:
                t_row = conn.execute(
                    adapt_query("SELECT tier FROM users WHERE id = ?"),
                    (row.get("id"),),
                ).fetchone()
                conn.commit()
                if t_row:
                    tier_val = _col(t_row, 0, "tier")
            except Exception:
                pass

            _expiring_soon.append(
                {
                    "email": row.get("email"),
                    "tier": tier_val,
                    "expires_at": exp_at,
                    "days_left": days_left,
                }
            )
        return _expiring_soon

    try:
        expiring_soon = await asyncio.to_thread(_load_expiring_soon)
    except Exception as exc:
        logger.error("billing_overview: expiring query failed: %s", exc, exc_info=True)
        errors.append(f"expiring_soon: {exc}")

    # ------------------------------------------------------------------
    # Webhook inbox stats
    # ------------------------------------------------------------------
    webhook_inbox: dict = {
        "backlog": 0,
        "pending_race": 0,
        "recent": [],
    }

    def _load_webhook_inbox() -> dict:
        conn = user_db._get_conn()

        backlog_row = conn.execute(
            adapt_query(
                "SELECT COUNT(*) AS cnt FROM paddle_webhook_inbox WHERE processed_at IS NULL"
            )
        ).fetchone()
        conn.commit()
        backlog = _col(backlog_row, 0, "cnt") or 0

        race_row = conn.execute(
            adapt_query(
                "SELECT COUNT(*) AS cnt FROM pending_paddle_events WHERE processed_at IS NULL"
            )
        ).fetchone()
        conn.commit()
        pending_race = _col(race_row, 0, "cnt") or 0

        recent_rows = conn.execute(
            adapt_query(
                """
                SELECT event_type, received_at, processed_at, last_error
                FROM paddle_webhook_inbox
                ORDER BY received_at DESC
                LIMIT 10
                """
            )
        ).fetchall()
        conn.commit()
        recent = [
            {
                "event_type": _col(r, 0, "event_type"),
                "received_at": _col(r, 1, "received_at"),
                "processed": _col(r, 2, "processed_at") is not None,
                "error": _col(r, 3, "last_error"),
            }
            for r in recent_rows
        ]
        return {"backlog": backlog, "pending_race": pending_race, "recent": recent}

    try:
        webhook_inbox = await asyncio.to_thread(_load_webhook_inbox)
    except Exception as exc:
        logger.error("billing_overview: webhook query failed: %s", exc, exc_info=True)
        errors.append(f"webhook_inbox: {exc}")

    # ------------------------------------------------------------------
    # Top-up revenue (30d) from platform economics
    # ------------------------------------------------------------------
    topup_revenue_30d_usd: float | None = None
    try:
        econ = await asyncio.to_thread(user_db.get_platform_economics, days=30)
        topup_cents = econ.get("total_topup_revenue_cents", 0) or 0
        topup_revenue_30d_usd = topup_cents / 100.0
    except Exception as exc:
        logger.warning("billing_overview: platform economics failed: %s", exc)
        errors.append(f"topup_revenue: {exc}")

    # ------------------------------------------------------------------
    # Assemble response
    # ------------------------------------------------------------------
    payload: dict = {
        "active_subscriptions": active_counts,
        "mrr_usd": mrr_usd,
        "arr_usd": arr_usd,
        "paddle_configured": paddle_configured,
        "expiring_soon": expiring_soon,
        "webhook_inbox": webhook_inbox,
        "topup_revenue_30d_usd": topup_revenue_30d_usd,
        "totals": totals_section,
    }
    if errors:
        payload["errors"] = errors

    return JSONResponse(jsonable_encoder(payload))
