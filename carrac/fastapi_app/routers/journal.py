"""Journal write endpoints: trades, trade shares, sessions, playbooks,
checklists and goals.

The rest of the journal API (reads, imports, reopen, screenshots, exchange
sync) is served by ``carrac/journal/routes.py``, and the AI review endpoints
by ``journal_ai.py``.

Validation: the Pydantic body models in ``carrac.fastapi_app.models.journal``
enforce shape (``extra="forbid"``) and field limits; the handlers apply the
asset and timeframe patterns and trim strings, and the ``ContentDatabase``
helpers do the writes.

CSRF: ``/api/journal`` is one of the CSRF-exempt prefixes in
``carrac/fastapi_app/csrf_middleware.py``.

Auth: every endpoint accepts a signed-in session or a per-user API key
(``X-API-Key``); anyone else gets 401 JSON.
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
)
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.journal import (
    BulkTradeRequest,
    ChecklistRequest,
    GoalRequest,
    PlaybookCreateRequest,
    PlaybookUpdateRequest,
    SessionCreateRequest,
    SessionUpdateRequest,
    TradeCloseRequest,
    TradeCreateRequest,
    TradeShareRequest,
    TradeUpdateRequest,
)
from carrac.journal.tags import dump_json_list, with_tag_lists

logger = logging.getLogger("carrac.fastapi.journal")

router = APIRouter()


def journal_user(
    auth: Annotated[dict, Depends(auth_or_key)],
) -> dict:
    """The calling user: a signed-in session or a per-user API key.

    Always returns a dict with an ``id`` key (and an ``email`` that is empty
    for key callers). The shared internal key names no user, so it is refused
    with 401 like any other unauthenticated caller.
    """
    if auth.get("source") == "session":
        return auth["user"]
    uid = auth.get("user_id")
    if uid is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return {"id": uid, "email": ""}


def _uid(user: dict) -> int:
    """Extract the user id from a :func:`journal_user` dict."""
    return int(user["id"])


# ---------------------------------------------------------------------------
# Trades — POST /api/journal/trades
# ---------------------------------------------------------------------------


@router.post("/api/journal/trades", status_code=201)
@limiter.limit("60/minute")
async def create_trade(
    request: Request,
    body: TradeCreateRequest,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a new trade journal entry.

    Enforces the tier's monthly journal cap, then stores the trade, links it
    to today's session and captures an entry score snapshot.
    """
    user_id = _uid(user)
    from carrac.content_db import get_content_db
    from carrac.auth.decorators import get_tier_limits
    from carrac.fastapi_app.deps.auth import get_request_tier
    from carrac.validation import (
        SAFE_ASSET_RE,
        SAFE_TF_RE,
        ALLOWED_DIRECTIONS,
    )

    db = get_content_db()

    # Tier gate — journal_trades_monthly cap.
    tier = get_request_tier(request)
    limits = get_tier_limits(tier)
    monthly_cap = limits.get("journal_trades_monthly", 0)
    trades_this_month = await asyncio.to_thread(db.count_trades_this_month, user_id)
    if trades_this_month >= monthly_cap:
        raise HTTPException(
            status_code=403,
            detail={
                "error": (
                    f"Monthly journal limit reached ({trades_this_month}/{monthly_cap} "
                    "trades logged this month). Upgrade for unlimited journal trades."
                ),
                "limit_type": "journal_trade",
                "current": trades_this_month,
                "max": monthly_cap,
                "tier": tier,
            },
        )

    asset = body.asset.strip()[:20].upper()
    if not asset or not SAFE_ASSET_RE.match(asset):
        raise HTTPException(
            status_code=400,
            detail="Valid asset symbol required (e.g. BTC, ETH)",
        )

    entry_price = body.entry_price
    if entry_price <= 0:
        raise HTTPException(status_code=400, detail="Positive entry price required")

    direction = body.direction or "long"
    if direction not in ALLOWED_DIRECTIONS:
        raise HTTPException(status_code=400, detail="Direction must be long or short")

    timeframe = (body.timeframe or "1D").strip()[:5]
    if not SAFE_TF_RE.match(timeframe):
        raise HTTPException(status_code=400, detail="Invalid timeframe")

    target_price = body.target_price
    stop_price = body.stop_price

    # Whitelist indicators_at_entry keys
    indicators_at_entry = None
    if isinstance(body.indicators_at_entry, dict):
        allowed = {
            "score_total", "verdict", "criteria", "vetoes", "setup",
            "trade_plan", "source",
        }
        indicators_at_entry = {
            k: v for k, v in body.indicators_at_entry.items() if k in allowed
        }

    setup_type = (body.setup_type or "").strip()[:50] or None

    # Extended fields
    extended: dict = {}
    if body.notes and body.notes.strip():
        extended["notes"] = body.notes.strip()[:10000]
    if body.size is not None and body.size > 0:
        extended["size"] = body.size
    if body.fees is not None and body.fees >= 0:
        extended["fees"] = body.fees
    if body.confidence is not None:
        extended["confidence"] = body.confidence
    if body.mood is not None:
        extended["mood"] = body.mood
    if body.account_tag:
        extended["account_tag"] = body.account_tag.strip()[:30]
    if body.tags is not None:
        extended["tags"] = dump_json_list(body.tags)
    if body.checklist_results is not None:
        extended["checklist_results"] = dump_json_list(body.checklist_results)

    def _do_create_trade() -> int:
        entry_id = db.log_trade_open(
            user_id=user_id,
            asset=asset,
            timeframe=timeframe,
            direction=direction,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            setup_type=setup_type,
            indicators_at_entry=indicators_at_entry,
            drawing_id=body.drawing_id,
            scenario_id=body.scenario_id,
        )

        if extended:
            db.trade_update(entry_id, **extended)

        # Auto-link to today's session — best-effort
        try:
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")
            session_id = db.get_or_create_session_for_date(user_id, today)
            if session_id:
                db.trade_update(entry_id, session_id=session_id)
        except Exception as exc:
            logger.warning("Session auto-link failed for trade %s: %s", entry_id, exc)

        # Score snapshot at entry — best-effort
        try:
            from carrac.scanner.snapshot import capture_score_snapshot
            capture_score_snapshot(
                db, user_id, entry_id, asset, direction,
                snapshot_type="entry",
                trade_levels={
                    "entry_price": entry_price,
                    "stop_price": stop_price,
                    "target_price": target_price,
                },
            )
        except Exception as exc:
            logger.warning("Entry snapshot failed for trade %s: %s", entry_id, exc)

        # PostHog + EspoCRM — best-effort
        try:
            from carrac.integrations import on_trade_opened
            entry_data = db.get_journal_entry(entry_id)
            if entry_data:
                on_trade_opened(user_id, user.get("email", ""), entry_data)
        except Exception as exc:
            logger.warning("Trade open integration failed for trade %s: %s", entry_id, exc)

        return entry_id

    entry_id = await asyncio.to_thread(_do_create_trade)
    return JSONResponse({"id": entry_id}, status_code=201)


# ---------------------------------------------------------------------------
# Trades — PATCH /api/journal/trades/{trade_id}
# ---------------------------------------------------------------------------


@router.patch("/api/journal/trades/{trade_id}")
@limiter.limit("60/minute")
async def trade_update(
    request: Request,
    body: TradeUpdateRequest,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
    trade_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Update any writable field on a trade.

    Partial update semantics — only sent fields are updated.
    ``ai_review`` and ``trade_grade`` are AI-only (``extra="forbid"``
    on the body model rejects them with 422→400 via exception handler).
    """
    user_id = _uid(user)
    from carrac.content_db import get_content_db
    from carrac.validation import (
        SAFE_TF_RE,
    )

    _ALLOWED_EXIT_REASONS = frozenset({
        "manual", "tp_hit", "sl_hit", "trailing_stop", "time_based", "breakeven",
    })

    db = get_content_db()

    def _do_update_trade():
        # Ownership check
        trade = db.get_journal_entry(trade_id)
        if not trade or trade.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Not found")

        data = body.model_dump(exclude_unset=True)
        if not data:
            raise HTTPException(status_code=400, detail="No valid fields to update")

        updates: dict = {}

        # Price fields — positivity is enforced by the body model (gt=0)
        for field in ("entry_price", "target_price", "stop_price", "exit_price"):
            if field in data and data[field] is not None:
                updates[field] = data[field]

        if "size" in data and data["size"] is not None:
            updates["size"] = data["size"]
        if "fees" in data and data["fees"] is not None:
            updates["fees"] = data["fees"]
        if "pnl_currency" in data and data["pnl_currency"] is not None:
            updates["pnl_currency"] = data["pnl_currency"]

        if "direction" in data and data["direction"]:
            updates["direction"] = data["direction"]

        if "exit_reason" in data and data["exit_reason"]:
            er = data["exit_reason"]
            if er not in _ALLOWED_EXIT_REASONS:
                er = "manual"
            updates["exit_reason"] = er

        if "timeframe" in data and data["timeframe"]:
            tf = data["timeframe"].strip()[:5]
            if SAFE_TF_RE.match(tf):
                updates["timeframe"] = tf

        for field in ("setup_type",):
            if field in data:
                val = data[field]
                if val is not None:
                    updates[field] = str(val).strip()[:50]

        if "account_tag" in data:
            val = data["account_tag"]
            updates["account_tag"] = str(val).strip()[:30] if val is not None else None

        if "notes" in data:
            val = data["notes"]
            updates["notes"] = str(val).strip()[:10000] if val is not None else None

        # Cross-object FK ownership (defense-in-depth): a user must not point
        # their trade's FKs at another user's scenario/session/playbook.
        if data.get("scenario_id") is not None:
            sc = db.get_drawing_by_id(data["scenario_id"], user_id=user_id)
            if not sc or sc.get("drawing_type") != "scenario":
                raise HTTPException(status_code=400, detail="Invalid scenario_id")
            updates["scenario_id"] = data["scenario_id"]
        if data.get("session_id") is not None:
            sess = db.get_session(data["session_id"])
            if not sess or sess.get("user_id") != user_id:
                raise HTTPException(status_code=400, detail="Invalid session_id")
            updates["session_id"] = data["session_id"]
        if data.get("checklist_template_id") is not None:
            pb = db.get_playbook(data["checklist_template_id"])
            if pb and pb.get("user_id") != user_id:
                raise HTTPException(status_code=400, detail="Invalid checklist_template_id")
            updates["checklist_template_id"] = data["checklist_template_id"]

        for field in ("confidence", "mood"):
            if field in data and data[field] is not None:
                updates[field] = data[field]

        for field in ("tags", "mistake_tags", "checklist_results"):
            if field in data and data[field] is not None:
                updates[field] = dump_json_list(data[field])

        if not updates:
            raise HTTPException(status_code=400, detail="No valid fields to update")

        success = db.trade_update(trade_id, **updates)
        entry = db.get_journal_entry(trade_id)
        return success, entry

    success, entry = await asyncio.to_thread(_do_update_trade)
    # Postgres returns the timestamp columns as datetime objects.
    return JSONResponse(jsonable_encoder({"ok": success, "entry": with_tag_lists(entry)}))


# ---------------------------------------------------------------------------
# Trades — POST /api/journal/trades/{trade_id}/close
# ---------------------------------------------------------------------------


@router.post("/api/journal/trades/{trade_id}/close")
@limiter.limit("60/minute")
async def trade_close(
    request: Request,
    body: TradeCloseRequest,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
    trade_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Close a trade with exit data.

    The 3-segment path is dispatched via the ``("/api/journal/trades/",
    {"PATCH", "DELETE", "POST"})`` prefix entry in ``MIGRATED_WRITE_PREFIXES``
    and matched by this specific handler inside the FastAPI router.
    """
    user_id = _uid(user)
    from carrac.content_db import get_content_db

    _ALLOWED_EXIT_REASONS = frozenset({
        "manual", "tp_hit", "sl_hit", "trailing_stop", "time_based", "breakeven",
    })

    db = get_content_db()

    def _do_close_trade():
        trade = db.get_journal_entry(trade_id)
        if not trade or trade.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Not found")
        if trade.get("closed_at"):
            raise HTTPException(status_code=400, detail="Trade is already closed")

        exit_price = body.exit_price
        exit_reason = body.exit_reason or "manual"
        if exit_reason not in _ALLOWED_EXIT_REASONS:
            exit_reason = "manual"

        notes = body.notes.strip()[:10000] if body.notes else None

        success = db.log_trade_close(
            journal_id=trade_id,
            exit_price=exit_price,
            exit_reason=exit_reason,
            notes=notes,
        )
        if not success:
            raise HTTPException(status_code=500, detail="Failed to close trade")

        # Extended close fields
        extended: dict = {}
        if body.mistake_tags is not None:
            extended["mistake_tags"] = dump_json_list(body.mistake_tags)
        if body.tags is not None:
            extended["tags"] = dump_json_list(body.tags)
        if body.fees is not None and body.fees >= 0:
            extended["fees"] = body.fees
        if body.pnl_currency is not None:
            extended["pnl_currency"] = body.pnl_currency
        if body.size is not None and body.size > 0:
            extended["size"] = body.size

        if extended:
            db.trade_update(trade_id, **extended)

        # MFE/MAE — best-effort, but LOUD: there is no retry, so
        # a throw here is a permanent hole in this trade's analytics. ERROR
        # reaches the error tracker; the close itself still succeeds. The MCP
        # trade_close tool does the same.
        try:
            from carrac.journal.mfe_mae import calculate_and_store_mfe_mae
            calculate_and_store_mfe_mae(db, trade_id)
        except Exception as exc:
            logger.error(
                "MFE/MAE calculation failed for trade %s — never recomputed after "
                "close, analytics for this trade stay blank: %s", trade_id, exc,
                exc_info=True,
            )

        # Exit snapshot — best-effort
        try:
            from carrac.scanner.snapshot import capture_score_snapshot
            capture_score_snapshot(
                db, user_id, trade_id, trade["asset"],
                trade.get("direction", "long"),
                snapshot_type="exit",
            )
        except Exception as exc:
            logger.warning("Exit snapshot failed for trade %s: %s", trade_id, exc)

        entry = db.get_journal_entry(trade_id)

        # PostHog + EspoCRM — best-effort
        try:
            from carrac.integrations import on_trade_closed
            stats = db.get_journal_stats(user_id, days=365)
            on_trade_closed(user_id, user.get("email", ""), entry, stats)
        except Exception as exc:
            logger.warning("Trade close integration failed for trade %s: %s", trade_id, exc)

        return entry

    entry = await asyncio.to_thread(_do_close_trade)
    # The close is already committed: a serialization error here would report
    # a failure for a trade that was in fact closed.
    return JSONResponse(jsonable_encoder({"ok": True, "entry": with_tag_lists(entry)}))


# ---------------------------------------------------------------------------
# Trade shares — POST/PATCH/DELETE /api/journal/trades/{trade_id}/share
#
# kind='trade' on the shared share rail (carrac/plays/shares.py). All three
# methods ride the ("/api/journal/trades/", {PATCH, DELETE, POST}) prefix in
# MIGRATED_WRITE_PREFIXES, so toggle updates use PATCH rather than PUT.
#
# Auth: ``auth_or_key`` directly, like the play share endpoints.
# ---------------------------------------------------------------------------


def _resolve_share_user_id(auth: dict):
    if auth.get("source") == "api_key":
        return auth.get("user_id")
    user = auth.get("user") or {}
    return user.get("id")


def _trade_share_payload(request: Request, share: dict) -> dict:
    from carrac.fastapi_app.routers.plays import _public_base

    slug = share["slug"]
    base = _public_base(request)
    snap = share.get("snapshot") or {}
    return jsonable_encoder(
        {
            "slug": slug,
            "path": f"/s/{slug}",
            "url": f"{base}/s/{slug}",
            "image_url": f"{base}/s/{slug}/card.png",
            # kind-facing toggle names; they ride the include_plan /
            # include_thesis columns (see carrac/plays/shares.py).
            "include_levels": share.get("include_plan"),
            "include_note": share.get("include_thesis"),
            "view_count": share.get("view_count", 0),
            "created_at": share.get("created_at"),
            # Frozen-at values the share modal uses for its labels.
            "snapshot_asset": snap.get("asset"),
            "snapshot_pnl_pct": snap.get("pnl_pct"),
            "snapshot_rr": snap.get("rr_actual"),
        }
    )


def _owned_closed_trade_or_raise(trade_id: int, user_id) -> dict:
    from carrac.content_db import get_content_db

    trade = get_content_db().get_journal_entry(trade_id)
    if not trade or trade.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Not found")
    if not trade.get("closed_at"):
        raise HTTPException(
            status_code=400, detail="Only closed trades can be shared"
        )
    return trade


@router.post("/api/journal/trades/{trade_id}/share", status_code=201)
@limiter.limit("30/minute")
async def create_trade_share(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    trade_id: int = Path(..., ge=1),
    body: TradeShareRequest | None = None,
) -> JSONResponse:
    """Mint (or return the existing active) public share for a closed trade."""
    user_id = _resolve_share_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.fastapi_app.deps.auth import get_request_tier
    from carrac.plays.shares import (
        count_active_shares,
        create_trade_share as _create_trade_share,
        get_active_share_for_trade,
        share_quota,
    )

    def _do_create_share():
        _owned_closed_trade_or_raise(trade_id, user_id)

        # Quota only charges NEW mints (shared pool across share kinds).
        if get_active_share_for_trade(trade_id, user_id) is None:
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
        share = _create_trade_share(user_id, trade_id, **toggles)
        if not share:
            raise HTTPException(status_code=404, detail="Not found")
        return share

    share = await asyncio.to_thread(_do_create_share)
    return JSONResponse(_trade_share_payload(request, share), status_code=201)


@router.patch("/api/journal/trades/{trade_id}/share")
@limiter.limit("30/minute")
async def update_trade_share(
    request: Request,
    body: TradeShareRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    trade_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Update visibility toggles; re-freezes the snapshot + drops the cached card."""
    user_id = _resolve_share_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.shares import update_trade_share as _update_trade_share

    share = await asyncio.to_thread(
        _update_trade_share, trade_id, user_id, **body.model_dump(exclude_unset=True)
    )
    if not share:
        raise HTTPException(status_code=404, detail="No active share for this trade")

    return JSONResponse(_trade_share_payload(request, share))


@router.delete("/api/journal/trades/{trade_id}/share")
@limiter.limit("30/minute")
async def revoke_trade_share(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    trade_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Revoke the trade's active share link(s). The public URL then returns 410."""
    user_id = _resolve_share_user_id(auth)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    from carrac.plays.shares import revoke_trade_share as _revoke_trade_share

    revoked = await asyncio.to_thread(_revoke_trade_share, trade_id, user_id)
    return JSONResponse({"ok": True, "revoked": revoked})


# ---------------------------------------------------------------------------
# Trades — DELETE /api/journal/trades/{trade_id}
# ---------------------------------------------------------------------------


@router.delete("/api/journal/trades/{trade_id}")
@limiter.limit("60/minute")
async def trade_delete(
    request: Request,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
    trade_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Delete a trade entry and its associated screenshots."""
    user_id = _uid(user)
    from carrac.content_db import get_content_db

    db = get_content_db()

    def _do_delete_trade():
        trade = db.get_journal_entry(trade_id)
        if not trade or trade.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Not found")
        db.trade_delete(trade_id, user_id)

    await asyncio.to_thread(_do_delete_trade)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Trades — POST /api/journal/trades/bulk
# ---------------------------------------------------------------------------


@router.post("/api/journal/trades/bulk", status_code=200)
@limiter.limit("30/minute")
async def bulk_update_trades(
    request: Request,
    body: BulkTradeRequest,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Bulk update or delete selected trades.

    Note on dispatch ordering: ``/api/journal/trades/bulk`` is an
    exact-path entry in ``MIGRATED_WRITE_PATHS`` (POST-only). The
    strangler's ExactPath entries are evaluated BEFORE PrefixPath
    entries, so this path does NOT collide with the
    ``/api/journal/trades/`` prefix entry that serves PATCH/DELETE
    (which also includes POST for the ``/close`` subroute). ExactPath
    wins here; PrefixPath never fires for ``/api/journal/trades/bulk``.
    """
    user_id = _uid(user)
    from carrac.content_db import get_content_db
    from carrac.db_pool import get_conn as _pool_get_conn

    db = get_content_db()

    trade_ids = [int(tid) for tid in body.trade_ids]
    action = body.action

    def _do_bulk_update_trades():
        # RAII pool conn — avoids pinning a pool slot in the asyncio thread pool
        placeholders = ",".join("?" * len(trade_ids))
        with _pool_get_conn() as conn:
            rows = conn.execute(
                f"SELECT id FROM trade_journal WHERE id IN ({placeholders}) AND user_id = ?",
                trade_ids + [user_id],
            ).fetchall()
        owned_ids = [r["id"] for r in rows]

        if action == "delete":
            affected = db.delete_trades_bulk(owned_ids, user_id)
            return {"ok": True, "affected": affected}

        # Bulk update
        updates: dict = {}
        if body.setup_type is not None:
            updates["setup_type"] = str(body.setup_type).strip()[:50]
        if body.tags is not None:
            updates["tags"] = dump_json_list(body.tags)
        if body.mistake_tags is not None:
            updates["mistake_tags"] = dump_json_list(body.mistake_tags)

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        count = 0
        for tid in owned_ids:
            db.trade_update(tid, **updates)
            count += 1

        return {"ok": True, "affected": count}

    result = await asyncio.to_thread(_do_bulk_update_trades)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Sessions — POST /api/journal/sessions
# ---------------------------------------------------------------------------


@router.post("/api/journal/sessions", status_code=201)
@limiter.limit("60/minute")
async def create_session(
    request: Request,
    body: SessionCreateRequest,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a new trading session."""
    user_id = _uid(user)
    from carrac.content_db import get_content_db

    db = get_content_db()

    label = (body.session_label or "default").strip()[:30]
    pre_notes = body.pre_notes.strip()[:10000] if body.pre_notes else None

    session_id = await asyncio.to_thread(
        db.create_session, user_id, body.date, label, pre_notes, body.mood_pre
    )
    return JSONResponse({"id": session_id}, status_code=201)


# ---------------------------------------------------------------------------
# Sessions — PATCH /api/journal/sessions/{session_id}
# ---------------------------------------------------------------------------


@router.patch("/api/journal/sessions/{session_id}")
@limiter.limit("60/minute")
async def update_session(
    request: Request,
    body: SessionUpdateRequest,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
    session_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Update a trading session."""
    user_id = _uid(user)
    from carrac.content_db import get_content_db

    db = get_content_db()

    def _do_update_session():
        sess = db.get_session(session_id)
        if not sess or sess.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Not found")

        data = body.model_dump(exclude_unset=True)
        if not data:
            raise HTTPException(status_code=400, detail="No valid fields")

        updates: dict = {}

        if "post_notes" in data:
            val = data["post_notes"]
            updates["post_notes"] = str(val).strip()[:10000] if val is not None else None
        if "pre_notes" in data:
            val = data["pre_notes"]
            updates["pre_notes"] = str(val).strip()[:10000] if val is not None else None
        if "session_label" in data:
            val = data["session_label"]
            updates["session_label"] = (str(val).strip()[:30] or "default") if val is not None else "default"
        if "mood_pre" in data and data["mood_pre"] is not None:
            updates["mood_pre"] = data["mood_pre"]
        if "mood_post" in data and data["mood_post"] is not None:
            updates["mood_post"] = data["mood_post"]
        if "rules_followed" in data and data["rules_followed"] is not None:
            updates["rules_followed"] = data["rules_followed"]

        if not updates:
            raise HTTPException(status_code=400, detail="No valid fields")

        db.update_session(session_id, **updates)

    await asyncio.to_thread(_do_update_session)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Sessions — DELETE /api/journal/sessions/{session_id}
# ---------------------------------------------------------------------------


@router.delete("/api/journal/sessions/{session_id}")
@limiter.limit("60/minute")
async def delete_session(
    request: Request,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
    session_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Delete a trading session."""
    user_id = _uid(user)
    from carrac.content_db import get_content_db

    db = get_content_db()

    def _do_delete_session():
        sess = db.get_session(session_id)
        if not sess or sess.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Not found")
        db.delete_session(session_id, user_id)

    await asyncio.to_thread(_do_delete_session)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Playbooks — POST /api/journal/playbooks
# ---------------------------------------------------------------------------


@router.post("/api/journal/playbooks", status_code=201)
@limiter.limit("60/minute")
async def create_playbook(
    request: Request,
    body: PlaybookCreateRequest,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create a new playbook."""
    user_id = _uid(user)
    from carrac.content_db import get_content_db

    db = get_content_db()

    name = body.name.strip()[:50]
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    def _trunc(val: Optional[str], max_len: int) -> Optional[str]:
        return str(val).strip()[:max_len] if val is not None else None

    playbook_id = await asyncio.to_thread(
        db.create_playbook,
        user_id=user_id,
        name=name,
        setup_type=_trunc(body.setup_type, 50),
        description=_trunc(body.description, 10000),
        ideal_conditions=_trunc(body.ideal_conditions, 10000),
        entry_rules=_trunc(body.entry_rules, 10000),
        exit_rules=_trunc(body.exit_rules, 10000),
    )
    return JSONResponse({"id": playbook_id}, status_code=201)


# ---------------------------------------------------------------------------
# Playbooks — PATCH /api/journal/playbooks/{playbook_id}
# ---------------------------------------------------------------------------


@router.patch("/api/journal/playbooks/{playbook_id}")
@limiter.limit("60/minute")
async def update_playbook(
    request: Request,
    body: PlaybookUpdateRequest,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
    playbook_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Update a playbook."""
    user_id = _uid(user)
    from carrac.content_db import get_content_db

    db = get_content_db()

    def _do_update_playbook():
        pb = db.get_playbook(playbook_id)
        if not pb or pb.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Not found")

        data = body.model_dump(exclude_unset=True)
        if not data:
            raise HTTPException(status_code=400, detail="No valid fields")

        updates: dict = {}
        _SHORT = 50
        _LONG = 10000

        for field in ("name", "setup_type"):
            if field in data:
                val = data[field]
                updates[field] = str(val).strip()[:_SHORT] if val is not None else None
        for field in ("description", "ideal_conditions", "entry_rules", "exit_rules"):
            if field in data:
                val = data[field]
                updates[field] = str(val).strip()[:_LONG] if val is not None else None
        for field in ("example_trade_ids", "screenshot_ids"):
            if field in data and data[field] is not None:
                updates[field] = data[field]

        if not updates:
            raise HTTPException(status_code=400, detail="No valid fields")

        db.update_playbook(playbook_id, **updates)

    await asyncio.to_thread(_do_update_playbook)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Playbooks — DELETE /api/journal/playbooks/{playbook_id}
# ---------------------------------------------------------------------------


@router.delete("/api/journal/playbooks/{playbook_id}")
@limiter.limit("60/minute")
async def delete_playbook(
    request: Request,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
    playbook_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Delete a playbook."""
    user_id = _uid(user)
    from carrac.content_db import get_content_db

    db = get_content_db()

    def _do_delete_playbook():
        pb = db.get_playbook(playbook_id)
        if not pb or pb.get("user_id") != user_id:
            raise HTTPException(status_code=404, detail="Not found")
        db.delete_playbook(playbook_id, user_id)

    await asyncio.to_thread(_do_delete_playbook)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Checklists — POST /api/journal/checklists
# ---------------------------------------------------------------------------


@router.post("/api/journal/checklists", status_code=200)
@limiter.limit("60/minute")
async def upsert_checklist(
    request: Request,
    body: ChecklistRequest,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Create or update a checklist template for a setup type.

    Upsert semantics: 200 on both create and update.
    """
    user_id = _uid(user)
    from carrac.content_db import get_content_db

    db = get_content_db()

    setup_type = body.setup_type.strip()[:50]
    if not setup_type:
        raise HTTPException(status_code=400, detail="setup_type is required")

    items = [str(item).strip()[:200] for item in body.checklist_items if str(item).strip()]
    if not items:
        raise HTTPException(
            status_code=400,
            detail="checklist_items must contain non-empty strings",
        )
    if len(items) > 30:
        raise HTTPException(status_code=400, detail="Maximum 30 checklist items")

    name = body.name.strip()[:50] if body.name else setup_type

    checklist_id = await asyncio.to_thread(db.upsert_checklist, user_id, setup_type, name, items)
    return JSONResponse({"id": checklist_id, "ok": True})


# ---------------------------------------------------------------------------
# Checklists — DELETE /api/journal/checklists/{checklist_id}
# ---------------------------------------------------------------------------


@router.delete("/api/journal/checklists/{checklist_id}")
@limiter.limit("60/minute")
async def delete_checklist(
    request: Request,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
    checklist_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Delete a checklist template."""
    user_id = _uid(user)
    from carrac.db_pool import adapt_query, get_conn as _pool_get_conn

    def _do_delete_checklist():
        # RAII pool conn — avoids pinning a pool slot in the asyncio thread pool
        with _pool_get_conn() as conn:
            row = conn.execute(
                adapt_query(
                    "SELECT id FROM setup_checklists WHERE id = ? AND user_id = ?"
                ),
                (checklist_id, user_id),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")

        with _pool_get_conn() as conn:
            conn.execute(
                adapt_query(
                    "DELETE FROM setup_checklists WHERE id = ? AND user_id = ?"
                ),
                (checklist_id, user_id),
            )

    await asyncio.to_thread(_do_delete_checklist)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Goals — POST /api/journal/goals
# ---------------------------------------------------------------------------


@router.post("/api/journal/goals", status_code=200)
@limiter.limit("60/minute")
async def set_goals(
    request: Request,
    body: GoalRequest,
    user: Annotated[dict, Depends(journal_user)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Set goals for a month.

    Upsert semantics: 200 on both create and update.
    """
    user_id = _uid(user)
    import json as json_mod
    from carrac.content_db import get_content_db

    db = get_content_db()

    if not body.goals:
        raise HTTPException(status_code=400, detail="Goals object required")

    # upsert_goals binds ``goals`` straight into SQL as text; SQLite and
    # psycopg2 both reject a dict.
    goal_id = await asyncio.to_thread(
        db.upsert_goals, user_id, body.month, json_mod.dumps(body.goals)
    )
    return JSONResponse({"id": goal_id, "ok": True})
