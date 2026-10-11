"""Journal AI endpoints (3 non-SSE + 1 SSE).

Ports 4 journal AI subroutes from Flask to FastAPI:

1. ``POST /api/journal/trades/{trade_id}/ai-review``  — non-SSE, sync
2. ``POST /api/journal/sessions/{session_id}/ai-summary`` — non-SSE, sync
3. ``POST /api/journal/ai-review/weekly`` — non-SSE, sync
4. ``POST /api/journal/ai-query`` — SSE (streaming NL query)

Flask source: the AI endpoints in ``carrac/journal/routes.py``.

All 4 endpoints delegate to helpers in ``carrac.journal.ai_review``.
Sync calls are wrapped in ``asyncio.to_thread(...)`` so the event loop
stays unblocked.

The ``nl_query_stream`` helper is a synchronous generator that yields
pre-formatted ``data: ...\n\n`` SSE events.  To convert it to an async
generator each ``next()`` call is dispatched via ``asyncio.to_thread``.

Strangler dispatch entries (added to ``carrac/asgi.py`` by main session):
  MIGRATED_WRITE_PATHS:
    ("/api/journal/ai-query",           frozenset({"POST"})),
    ("/api/journal/ai-review/weekly",   frozenset({"POST"})),

  MIGRATED_WRITE_PREFIXES:
    ("/api/journal/trades/",            frozenset({"PATCH", "DELETE", "POST"}))
      — already present; covers the /<id>/ai-review 3-segment subroute.
    ("/api/journal/sessions/",          frozenset({"PATCH", "DELETE"}))
      — already present; covers /<id>/ai-summary via POST
      — NOTE: POST not yet in methods set for sessions/; the main
        session must extend it to frozenset({"PATCH", "DELETE", "POST"}).

  fastapi_app.include_router (in ``carrac/fastapi_app/main.py``):
    from carrac.fastapi_app.routers import journal_ai
    fastapi_app.include_router(journal_ai.router)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse, StreamingResponse

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    get_request_tier,
    require_login,
)
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.journal_ai import (
    JournalAiQueryRequest,
    JournalAiReviewBody,
    JournalWeeklyReviewRequest,
)

logger = logging.getLogger("carrac.fastapi.journal_ai")

router = APIRouter()


def _uid(user: dict) -> int:
    """Extract integer user_id from a ``require_login`` user dict."""
    return int(user["id"])


def _require_ai_tier(request: Request) -> None:
    """Block free-tier accounts from the token-costing journal-AI endpoints.

    Free tier has ``ai_queries_monthly == 0``; paid tiers (member/premium/
    admin) carry a non-zero budget. Mirrors the gate already on
    ``/api/ai/chat`` and ``/api/ai/execute-action`` so no free account can
    trigger an LLM-billed journal review/summary/query. (General/shared
    reports stay free — they are gated separately by the ``personal_report``
    flag in reports.py.)
    """
    from carrac.auth.decorators import get_tier_limits

    tier = get_request_tier(request)
    if get_tier_limits(tier).get("ai_queries_monthly", 0) == 0:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "AI journal review requires a paid plan.",
                "upgrade_required": True,
            },
        )


# ---------------------------------------------------------------------------
# POST /api/journal/trades/{trade_id}/ai-review  — non-SSE JSON
# ---------------------------------------------------------------------------


@router.post("/api/journal/trades/{trade_id}/ai-review")
@limiter.limit("10/minute")
async def ai_review_trade(
    request: Request,
    body: JournalAiReviewBody,  # noqa: ARG001 — empty body; validates extras
    user: Annotated[dict, Depends(require_login)],
    ctx: RateCtxDep,
    trade_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Generate AI review for a closed trade.

    Delegates to ``carrac.journal.ai_review.review_trade``.

    Error mapping (mirrors Flask):
    - Trade not found / not owned by caller → 404
    - Trade not closed → 400
    - Insufficient credits / AI unavailable → 400 or 503 per error text
    """
    from carrac.journal.ai_review import review_trade
    from carrac.content_db import get_content_db

    user_id = _uid(user)
    _require_ai_tier(request)
    db = get_content_db()

    trade = await asyncio.to_thread(db.get_journal_entry, trade_id)
    if not trade or trade.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail={"error": "Not found"})
    if not trade.get("closed_at"):
        raise HTTPException(
            status_code=400,
            detail={"error": "Trade must be closed"},
        )

    result = await asyncio.to_thread(review_trade, db, trade_id, user_id)

    if "error" in result:
        status = (
            503
            if result["error"] == "AI service temporarily unavailable"
            else 400
        )
        raise HTTPException(status_code=status, detail=result)

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# POST /api/journal/sessions/{session_id}/ai-summary  — non-SSE JSON
# ---------------------------------------------------------------------------


@router.post("/api/journal/sessions/{session_id}/ai-summary")
@limiter.limit("10/minute")
async def ai_session_summary(
    request: Request,
    body: JournalAiReviewBody,  # noqa: ARG001
    user: Annotated[dict, Depends(require_login)],
    ctx: RateCtxDep,
    session_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Generate AI summary for a trading session.

    Delegates to ``carrac.journal.ai_review.summarize_session``.
    """
    from carrac.journal.ai_review import summarize_session
    from carrac.content_db import get_content_db

    user_id = _uid(user)
    _require_ai_tier(request)
    db = get_content_db()

    sess = await asyncio.to_thread(db.get_session, session_id)
    if not sess or sess.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail={"error": "Not found"})

    result = await asyncio.to_thread(summarize_session, db, session_id, user_id)

    if "error" in result:
        status = (
            503
            if result["error"] == "AI service temporarily unavailable"
            else 400
        )
        raise HTTPException(status_code=status, detail=result)

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# POST /api/journal/ai-review/weekly  — non-SSE JSON
# ---------------------------------------------------------------------------


@router.post("/api/journal/ai-review/weekly")
@limiter.limit("10/minute")
async def ai_weekly_journal_review(
    request: Request,
    body: JournalWeeklyReviewRequest,
    user: Annotated[dict, Depends(require_login)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Generate AI weekly or monthly performance review.

    Body: ``{ "days": int (7-90, default 7) }``.
    Delegates to ``carrac.journal.ai_review.weekly_review``.
    """
    from carrac.journal.ai_review import weekly_review
    from carrac.content_db import get_content_db

    user_id = _uid(user)
    _require_ai_tier(request)
    db = get_content_db()
    days = body.days or 7  # Pydantic enforces ge=7,le=90 if set

    result = await asyncio.to_thread(weekly_review, db, user_id, days=days)

    if "error" in result:
        status = (
            503
            if result["error"] == "AI service temporarily unavailable"
            else 400
        )
        raise HTTPException(status_code=status, detail=result)

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# POST /api/journal/ai-query  — SSE streaming NL query
# ---------------------------------------------------------------------------


@router.post("/api/journal/ai-query")
@limiter.limit("10/minute")
async def ai_nl_query(
    request: Request,
    body: JournalAiQueryRequest,
    user: Annotated[dict, Depends(require_login)],
    ctx: RateCtxDep,
) -> StreamingResponse:
    """Natural language query — stream SQL results interpretation.

    ``carrac.journal.ai_review.nl_query_stream`` is a synchronous
    generator that yields pre-formatted ``data: ...\n\n`` SSE strings.
    We convert it to an async generator by dispatching each ``next()``
    call to a thread via ``asyncio.to_thread`` so the event loop stays
    unblocked between LLM network polls.

    Design decision: we use the synchronous ``nl_query_stream`` helper
    rather than waiting for Agent G's async dispatcher
    (``astream_chat`` / ``astream_chat_with_tools``) because
    ``nl_query_stream`` is an opaque sync generator that already handles
    its own billing + error logic — there is no clean seam to inject
    an async adapter without duplicating that logic.  The thread-based
    bridge achieves equivalent non-blocking behaviour at the asyncio
    layer while keeping the sync implementation as the single source
    of truth.
    """
    from carrac.journal.ai_review import nl_query_stream
    from carrac.content_db import get_content_db

    user_id = _uid(user)
    _require_ai_tier(request)
    question = body.question  # already validated (min_length=1, max_length=500)
    db = get_content_db()

    async def generate() -> AsyncIterator[str]:
        """Drive the sync ``nl_query_stream`` generator from a thread pool."""
        gen = nl_query_stream(db, user_id, question)
        _sentinel = object()
        while True:
            event = await asyncio.to_thread(next, gen, _sentinel)
            if event is _sentinel:
                break
            yield event

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
