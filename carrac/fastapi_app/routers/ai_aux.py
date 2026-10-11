"""AI auxiliary endpoints (weekly-review SSE).

Adds the two non-streaming Flask /api/ai/* writes:
  - POST /api/ai/execute-action   (tool execution paired with chat actions)
  - POST /api/ai/context-preview  (deterministic recipe preview, no LLM)

Both are non-streaming JSON; sync DB/registry calls are wrapped in
``asyncio.to_thread`` so the event loop stays unblocked.

Capacity win for the weekly-review: each in-flight SSE
connection consumes one asyncio task instead of one gthread (≥5× headroom
under burst).

Flask sources:
  - ``carrac/ai/routes.py:ai_weekly_review``  (since deleted)
  - ``carrac/ai/routes.py:ai_execute_action`` (still present —
    strangler dispatches /api/ai/execute-action POST here, Flask serves
    nothing for that path)
  - ``carrac/ai/routes.py:ai_context_preview`` (same arrangement)

Strangler dispatch entries (in ``carrac/asgi.py`` ``MIGRATED_WRITE_PATHS``):
    ("/api/ai/weekly-review", frozenset({"POST"})),
    ("/api/ai/execute-action", frozenset({"POST"})),
    ("/api/ai/context-preview", frozenset({"POST"})),
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    get_request_tier,
    require_login,
)
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.ai import (
    ContextPreviewRequest,
    ExecuteActionRequest,
)
from carrac.fastapi_app.models.ai_aux import WeeklyReviewRequest

logger = logging.getLogger("carrac.fastapi.ai_aux")

router = APIRouter()


def _rate_key_by_user(request: Request) -> str:
    """Mirror the chat router's per-user slowapi key."""
    ctx = getattr(request.state, "rate", None)
    if ctx is not None and getattr(ctx, "key", ""):
        return ctx.key
    sess = getattr(request.state, "session", {}) or {}
    uid = sess.get("user_id")
    if uid:
        return f"user:{uid}"
    from slowapi.util import get_remote_address
    return get_remote_address(request)


# ---------------------------------------------------------------------------
# POST /api/ai/weekly-review  — Premium-only SSE
# ---------------------------------------------------------------------------


def _weekly_review_prompt() -> str:
    """System prompt for /api/ai/weekly-review: observations about the user's
    own recorded week, never trade instructions. Carries the non-advice ban
    itself, so it survives a grounding failure below."""
    from carrac.ai.copy import with_advice_ban
    return with_advice_ban(
        "You are describing the user's own trading week from their journal. "
        "Identify patterns in the user's recorded behaviour — what their record shows "
        "working and not working, and how closely they followed their own rules. "
        "Note any recorded emotional-trading signs (revenge trades, size changes, "
        "winners closed well before their own targets). "
        "Give 3 specific observations about the user's own process and rule adherence "
        "that are worth noticing next week — observations about the record, never "
        "instructions about what to trade, when to enter or exit, or how large to size. "
        "Be direct but encouraging."
    )


@router.post("/api/ai/weekly-review")
@limiter.limit("10/minute")
async def ai_weekly_review(
    request: Request,
    body: WeeklyReviewRequest,  # noqa: ARG001 — no body fields; validates extras
    user: Annotated[dict, Depends(require_login)],
    ctx: RateCtxDep,
) -> StreamingResponse:
    """Generate an AI-powered weekly trade review from journal data.

    In order:

    1. Tier gate — Premium only (``weekly_review`` flag in TIER_LIMITS).
    2. Global daily budget circuit-breaker.
    3. Reserve credits (``check_and_reserve``).
    4. Build prompt from last-7-days journal data.
    5. Grounding scan — anti-hallucination preamble + tripwire.
    6. Stream LLM response via ``stream_chat`` wrapped in ``asyncio.to_thread``.
    7. Post-stream HIGH-severity grounding scan + verification notice.
    8. ``settle()`` + emit ``{done: True, charged, bal, stats}``.
    9. Disconnect-settle: ``GeneratorExit`` triggers refund in
       ``finally`` block when ``settle()`` hasn't run yet.

    ``stream_chat`` is the synchronous generator from
    ``carrac.ai.llm_client``. Each iteration is wrapped with
    ``asyncio.to_thread(next, iterator)`` so the event loop stays
    unblocked while the LLM streams.  This is the standard
    pattern for sync-generator → async-generator conversion.
    """
    from carrac.ai.billing import check_and_reserve, check_global_budget, settle
    from carrac.ai.llm_client import stream_chat
    from carrac.auth.decorators import get_tier_limits
    from carrac.content_db import get_content_db
    from user_db import user_db

    user_id = int(user["id"])

    # ── Tier gate ──
    tier = get_request_tier(request)
    limits = get_tier_limits(tier)
    if not limits.get("weekly_review"):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "Weekly AI review requires Premium subscription",
                "upgrade_required": True,
            },
        )

    # ── Global daily budget circuit-breaker ──
    ok_budget, budget_info = await asyncio.to_thread(check_global_budget)
    if not ok_budget:
        logger.warning("Weekly review blocked by global daily cap: %s", budget_info)
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    "AI is paused for the rest of the day — the shared safety cap was reached."
                    " Service resumes at 00:00 UTC."
                ),
                "global_cap": True,
                "spent_cents": budget_info["spent_cents"],
                "cap_cents": budget_info["cap_cents"],
            },
        )

    # ── Journal data ──
    db = get_content_db()
    stats = await asyncio.to_thread(db.get_journal_stats, user_id, days=7)
    history = await asyncio.to_thread(db.get_trade_history, user_id, days=7)

    if not history:
        raise HTTPException(status_code=400, detail={"error": "No closed trades this week"})

    # ── Reserve credits ──
    estimate = int(os.getenv("AI_ESTIMATE_CENTS", "25"))
    ok_reserve = await asyncio.to_thread(check_and_reserve, user_id, estimate)
    if not ok_reserve:
        raise HTTPException(
            status_code=402,
            detail={"error": "Insufficient credits", "need_credits": True},
        )

    # ── Build review prompt ──
    trade_lines = []
    for t in history:
        line = f"- {t.get('asset')} {t.get('direction')}: entry ${t.get('entry_price', 0):,.2f}"
        if t.get("exit_price"):
            line += f" → exit ${t['exit_price']:,.2f}"
        if t.get("rr_actual") is not None:
            line += f" (R:R {t['rr_actual']:+.2f})"
        if t.get("exit_reason"):
            # exit_reason is user-authored free text — quarantine before prompt
            from carrac.ai.billing import sanitize_for_llm
            from carrac.grounding import escape_user_text
            _exit = sanitize_for_llm(str(t['exit_reason'])[:200], max_length=200, source="exit_reason")
            line += f" [{escape_user_text(_exit, 'exit_reason', max_chars=200)}]"
        if t.get("setup_type"):
            # setup_type is user-authored free text — quarantine before prompt
            from carrac.ai.billing import sanitize_for_llm
            from carrac.grounding import escape_user_text
            _setup = sanitize_for_llm(str(t['setup_type'])[:200], max_length=200, source="setup_type")
            line += f" setup={escape_user_text(_setup, 'setup_type', max_chars=200)}"
        trade_lines.append(line)

    context = (
        f"Weekly trading stats:\n"
        f"Trades: {stats['trades']}, Wins: {stats['wins']}, Losses: {stats['losses']}\n"
        f"Win rate: {stats['win_rate']}%, Avg R:R: {stats['avg_rr']}\n"
        f"Best: {stats['best_rr']}R, Worst: {stats['worst_rr']}R\n\n"
        f"Trades:\n" + "\n".join(trade_lines)
    )

    system = _weekly_review_prompt()

    # ── apply anti-hallucination grounding ──
    _facts: dict = {}
    try:
        from carrac.grounding import build_facts_dict, facts_block, grounded_system_prompt

        _facts = await asyncio.to_thread(build_facts_dict, include_prices=True)
        _fb = facts_block(include_prices=True, facts=_facts)
        system = grounded_system_prompt(system, facts_block_text=_fb)
    except Exception as _grounding_err:
        logger.warning("Weekly-review grounding unavailable: %s", _grounding_err)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": context},
    ]

    model = os.getenv("AI_MODEL", "") or os.getenv("TOGETHER_MODEL", "")

    async def generate() -> AsyncIterator[str]:
        """Async generator that wraps the sync ``stream_chat`` iterator.

        Each ``next()`` call on the sync generator is dispatched to a
        thread via ``asyncio.to_thread`` so the event loop stays free
        while waiting for the LLM network I/O.

        Disconnect contract: ``GeneratorExit`` is raised by
        the ASGI server when the client disconnects.  The ``finally``
        block refunds the reserved estimate if ``settle()`` hasn't run.
        """
        input_tokens = 0
        output_tokens = 0
        usage_details = None
        full_text = ""
        settled = False
        disconnected = False

        llm_gen = stream_chat(messages, max_tokens=2000)

        try:
            # Drive the sync generator one chunk at a time from a thread
            # so the event loop stays unblocked between LLM network polls.
            _sentinel = object()

            async def _next_chunk():
                return await asyncio.to_thread(next, llm_gen, _sentinel)

            while True:
                chunk = await _next_chunk()
                if chunk is _sentinel:
                    break
                chunk_text, usage = chunk
                if chunk_text:
                    full_text += chunk_text
                    yield f"data: {json.dumps({'t': chunk_text})}\n\n"
                if usage:
                    usage_details = usage
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)

        except GeneratorExit:
            disconnected = True
            logger.info(
                "Weekly-review SSE client disconnected user=%s settled=%s",
                user_id,
                settled,
            )
            raise
        except Exception as exc:
            logger.error("Weekly review stream error user=%s: %s", user_id, exc)
            try:
                balance = await asyncio.to_thread(user_db.get_ai_balance, user_id)
                yield f"data: {json.dumps({'error': 'AI service unavailable', 'bal': balance})}\n\n"
            except Exception:
                pass
            return
        finally:
            # Always close the sync generator so the upstream HTTP
            # connection to the LLM provider shuts down.
            try:
                llm_gen.close()
            except Exception:
                pass

        if input_tokens == 0:
            input_tokens = len(system + context) // 4
        if output_tokens == 0:
            output_tokens = len(full_text) // 4

        # ── post-stream HIGH-severity grounding tripwire ──
        try:
            from carrac.grounding import scan as _hscan

            _findings = _hscan(full_text, _facts)
            _highs = [f for f in _findings if f.severity.value == "HIGH"]
            if _highs:
                _warning = (
                    "\n\n⚠ **Verification notice** — one or more claims in this "
                    "review could not be verified against the platform's live data: "
                    + "; ".join(
                        f"{f.pattern_name} (\"{f.match_text}\")" for f in _highs[:3]
                    )
                    + ". Treat those specific claims as uncertain."
                )
                full_text += _warning
                yield f"data: {json.dumps({'t': _warning})}\n\n"
                yield (
                    f"data: {json.dumps({'verification': {'verdict': 'warn', 'findings': [f.to_dict() for f in _highs]}})}\n\n"
                )
                logger.warning(
                    "Weekly-review verification WARN for user=%s: %s",
                    user_id,
                    [f.pattern_name for f in _highs],
                )
        except Exception as _scan_err:
            logger.debug("Weekly-review post-stream scan failed: %s", _scan_err)

        # ── Settle billing ──
        try:
            actual = await asyncio.to_thread(
                settle,
                user_id,
                estimate,
                input_tokens,
                output_tokens,
                feature="weekly_review",
                asset=None,
                model=model,
                usage=usage_details,
                prompt_text=(system + "\n\n" + context),
                response_text=full_text,
                conversation_id=f"weekly-review-{user_id}",
            )
            settled = True
            balance = await asyncio.to_thread(user_db.get_ai_balance, user_id)
            yield f"data: {json.dumps({'done': True, 'charged': actual, 'bal': balance, 'stats': stats})}\n\n"
        except Exception as settle_err:
            logger.error("Weekly review settle failed user=%s: %s", user_id, settle_err)
        finally:
            if not settled:
                try:
                    await asyncio.to_thread(user_db.refund_ai_credits, user_id, estimate)
                    if disconnected:
                        logger.info(
                            "Weekly review refunded estimate=%d user=%s after disconnect",
                            estimate,
                            user_id,
                        )
                except Exception as refund_err:
                    logger.warning(
                        "Weekly review refund failed for user=%s: %s", user_id, refund_err
                    )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# POST /api/ai/execute-action  — non-streaming tool execution
# ---------------------------------------------------------------------------


@router.post("/api/ai/execute-action")
@limiter.limit("20/minute")
@limiter.limit("50/hour", key_func=_rate_key_by_user)
async def ai_execute_action(
    request: Request,
    body: ExecuteActionRequest,
    user: Annotated[dict, Depends(require_login)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Execute a confirmed write tool call from the copilot.

    Tier-gated identically to /api/ai/chat (free → 403). ``execute_tool`` itself enforces per-tool
    ``min_tier`` and gives back ``{error, upgrade_required}`` for
    insufficient tier — we forward that as a 400 so clients can surface
    the upgrade nudge.
    """
    from carrac.ai.copilot_tools import WRITE_TOOLS, execute_tool
    from carrac.auth.decorators import get_tier_limits

    user_id = int(user["id"])

    # In-app AI chat (and its write-action surface) is feature-flagged OFF for
    # the MCP-first free beta — parity with /api/ai/chat, including the
    # Admin pass-through (one rule: ai_chat_allowed).
    from carrac.integrations import ai_chat_allowed
    if not await asyncio.to_thread(ai_chat_allowed, user.get("is_admin")):
        raise HTTPException(
            status_code=403,
            detail={"error": "In-app AI chat is currently unavailable.", "ai_chat_disabled": True},
        )

    # Tier gate parity with /api/ai/chat — free tier cannot reach
    # the copilot at all, including its write-action surface.
    tier = get_request_tier(request)
    limits = get_tier_limits(tier)
    if limits.get("ai_queries_monthly", 0) == 0:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "AI actions require a Member or Premium subscription",
                "upgrade_required": True,
            },
        )

    if body.tool not in WRITE_TOOLS:
        raise HTTPException(
            status_code=400,
            detail={"error": "Invalid or unauthorized tool"},
        )

    result_text = await asyncio.to_thread(
        execute_tool, body.tool, body.arguments, user_id, user_tier=tier,
    )

    try:
        result = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        result = {"raw": result_text}

    if isinstance(result, dict) and result.get("error"):
        return JSONResponse(status_code=400, content={"error": result["error"]})

    return JSONResponse({"result": result, "status": "executed"})


# ---------------------------------------------------------------------------
# POST /api/ai/context-preview  — deterministic recipe preview
# ---------------------------------------------------------------------------


@router.post("/api/ai/context-preview")
async def ai_context_preview(
    request: Request,
    body: ContextPreviewRequest,
    user: Annotated[dict, Depends(require_login)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Preview the assembled prompt for a recipe without calling the LLM.

    No billing, no rate-limit. ``preview_context`` is a sync function that walks the
    context-block registry and returns ``{text, total_tokens, budget,
    blocks}`` — wrapped in ``asyncio.to_thread`` since some blocks hit
    the database.
    """
    from carrac.ai.context_composer import preview_context

    user_id = int(user["id"])
    tier = get_request_tier(request)

    result = await asyncio.to_thread(
        preview_context,
        user_id,
        recipe_id=body.recipe_id,
        asset=(body.asset or "").upper(),
        timeframe=body.timeframe or "1D",
        thesis_text=body.thesis_text or "",
        user_tier=tier,
    )
    return JSONResponse(result)
