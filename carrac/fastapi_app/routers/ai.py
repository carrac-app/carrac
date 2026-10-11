"""FastAPI AI chat endpoint.

Serves POST /api/ai/chat as a StreamingResponse so each connection costs one
asyncio task rather than one gunicorn gthread — capacity grows >=5× for the
same worker count.

Billing, concurrency, tier-gate, grounding and disconnect-settle logic all
live here. Sync DB/Redis calls are wrapped in asyncio.to_thread() so they
don't block the event loop.

CSRF: /api/ai is in _SKIP_PREFIXES in csrf_middleware.py — no CSRF token needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Annotated, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from carrac.fastapi_app.deps.auth import (
    RateCtxDep,
    auth_or_key,
)
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.models.ai import ChatRequest

logger = logging.getLogger("carrac.fastapi.ai")

router = APIRouter()


# ---------------------------------------------------------------------------
# Rate-limit key.
# Keys on user_id so carrier-grade NAT / shared IP users don't trip each other.
# ---------------------------------------------------------------------------


def _rate_key_by_user(request: Request) -> str:
    """Key slowapi on user_id, falling back to remote IP."""
    ctx = getattr(request.state, "rate", None)
    if ctx is not None and getattr(ctx, "key", ""):
        return ctx.key
    # read from session as secondary fallback
    sess = getattr(request.state, "session", {}) or {}
    uid = sess.get("user_id")
    if uid:
        return f"user:{uid}"
    from slowapi.util import get_remote_address
    return get_remote_address(request)


# ---------------------------------------------------------------------------
# POST /api/ai/chat
# ---------------------------------------------------------------------------


@router.post("/api/ai/chat")
@limiter.limit("50/hour", key_func=_rate_key_by_user)
async def ai_chat(
    request: Request,
    body: ChatRequest,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> StreamingResponse:
    """SSE streaming chat with credit billing and conversation history.

    Uses async generators and asyncio.to_thread() for sync DB/Redis calls.
    """
    # In-app AI chat is feature-flagged OFF for the MCP-first free beta (stops
    # per-token LLM cost — users bring their own AI via MCP). Re-enable later as
    # a paid perk by flipping the PostHog `ai_chat_enabled` flag.
    # an admin session passes regardless of the flag (ai_chat_allowed is the one
    # rule, shared with the index UI and execute-action). An API-key caller has
    # no admin session here, so the flag alone decides for it.
    from carrac.integrations import ai_chat_allowed
    _is_admin = bool((auth.get("user") or {}).get("is_admin")) if auth.get("source") == "session" else False
    if not await asyncio.to_thread(ai_chat_allowed, _is_admin):
        raise HTTPException(
            status_code=403,
            detail={"error": "In-app AI chat is currently unavailable.", "ai_chat_disabled": True},
        )

    from carrac.ai.billing import check_and_reserve, settle, check_global_budget
    from security import sanitize_for_llm
    from carrac.ai.features import build_system_prompt, get_max_tokens, get_feature_config
    from carrac.ai.routes import VALID_FEATURES, VALID_TF, ASSET_RE
    from user_db import user_db

    # Resolve user_id from session or per-user API key.
    # The shared admin key has no per-user binding; reject it here
    # because AI quota tracking requires a real user_id.
    if auth.get("source") == "api_key":
        _uid = auth.get("user_id")
        if _uid is None:
            raise HTTPException(
                status_code=401,
                detail={"error": "AI chat requires a per-user API key (cvx_k_), not the shared admin key."},
            )
        user_id = int(_uid)
    else:
        user_id = int(auth["user"]["id"])

    # Global daily-spend circuit breaker.
    ok_budget, budget_info = await asyncio.to_thread(check_global_budget)
    if not ok_budget:
        logger.warning("AI chat blocked by global daily cap: %s", budget_info)
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    "AI is paused for the rest of the day — the shared safety cap was reached. "
                    "Service resumes at 00:00 UTC."
                ),
                "global_cap": True,
                "spent_cents": budget_info["spent_cents"],
                "cap_cents": budget_info["cap_cents"],
            },
        )

    # first_copilot_message funnel event (before tier gate).
    try:
        from carrac.integrations import fire_first_event
        await asyncio.to_thread(
            fire_first_event, user_id, "first_copilot_message",
            {"feature": body.feature},
        )
    except Exception:
        pass

    # Tier & monthly cap checks.
    from carrac.auth.decorators import get_tier_limits
    tier = auth.get("tier", "free") or "free"
    limits = get_tier_limits(tier)
    monthly_cap = limits.get("ai_queries_monthly", 0)
    if monthly_cap == 0:
        try:
            from carrac.integrations import on_funnel_event
            await asyncio.to_thread(
                on_funnel_event, user_id, "ai_chat_blocked_free_user", {"tier": tier}
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=403,
            detail={
                "error": "AI queries require a Member or Premium subscription",
                "upgrade_required": True,
            },
        )
    if monthly_cap < 999:
        count = await asyncio.to_thread(user_db.get_monthly_ai_query_count, user_id)
        if count >= monthly_cap:
            try:
                from carrac.integrations import on_funnel_event
                await asyncio.to_thread(
                    on_funnel_event, user_id, "ai_monthly_limit_hit",
                    {"tier": tier, "count": count, "cap": monthly_cap},
                )
            except Exception:
                pass
            raise HTTPException(
                status_code=429,
                detail={
                    "error": f"Monthly AI query limit reached ({count}/{monthly_cap})",
                    "limit_type": "ai_monthly",
                    "current": count,
                    "max": monthly_cap,
                    "topup_hint": True,
                },
            )

    # Per-user concurrency slot.
    from carrac.ai.chat_slot import acquire_chat_slot, release_chat_slot
    slot_acquired = await asyncio.to_thread(acquire_chat_slot, user_id)
    if not slot_acquired:
        raise HTTPException(
            status_code=429,
            detail={"error": "Another AI request is in progress. Please wait."},
        )

    # Input validation.
    raw_message = body.message or ""
    if not raw_message:
        await asyncio.to_thread(release_chat_slot, user_id)
        raise HTTPException(status_code=400, detail={"error": "Message is required"})

    message = sanitize_for_llm(raw_message)
    if not message:
        await asyncio.to_thread(release_chat_slot, user_id)
        raise HTTPException(status_code=400, detail={"error": "Empty message"})

    feature = body.feature or "chat"
    if feature not in VALID_FEATURES:
        feature = "chat"

    asset = (body.asset or "").upper()
    if asset and not ASSET_RE.match(asset):
        asset = ""

    timeframe = body.timeframe or "1D"
    if timeframe not in VALID_TF:
        timeframe = "1D"

    _valid_contexts = (
        "chart", "scanner", "report", "dashboard",
        "journal", "plays", "positions", "alerts",
        "stream", "backtest", "consensio", "detail", "news",
    )
    page_context = body.page_context or "chart"
    if page_context not in _valid_contexts:
        page_context = "chart"

    conversation_id = body.conversation_id
    if conversation_id is not None:
        try:
            conversation_id = int(conversation_id)
        except (ValueError, TypeError):
            conversation_id = None

    feat_config = get_feature_config(feature)
    if feat_config.get("requires_asset") and not asset:
        await asyncio.to_thread(release_chat_slot, user_id)
        raise HTTPException(
            status_code=400,
            detail={"error": "Please select an asset for this feature"},
        )

    # Reserve credits (atomic deduction BEFORE calling LLM).
    estimate = int(os.getenv("AI_CONSERVATIVE_ESTIMATE_CENTS", "25"))
    reserved = await asyncio.to_thread(check_and_reserve, user_id, estimate)
    if not reserved:
        await asyncio.to_thread(release_chat_slot, user_id)
        raise HTTPException(
            status_code=402,
            detail={"error": "Insufficient credits", "need_credits": True},
        )

    # Build conversation history from DB (with summary compression).
    conversation_history: list = []
    conv_summary = None
    if conversation_id:
        from carrac.ai.summarizer import (
            should_summarize, summarize_conversation, build_history_with_summary,
        )
        conv_meta = await asyncio.to_thread(
            user_db.get_conversation_meta, conversation_id, user_id
        )
        if conv_meta and should_summarize(conv_meta.get("message_count", 0)):
            try:
                await asyncio.to_thread(summarize_conversation, conversation_id, user_id)
            except Exception as e:
                logger.warning("Summarization failed: %s", e)
        conversation_history, conv_summary = await asyncio.to_thread(
            build_history_with_summary, conversation_id, user_id
        )

    # Save user message to DB.
    if conversation_id:
        await asyncio.to_thread(
            user_db.add_message, conversation_id, "user", message,
            feature=feature, user_id=user_id,
        )
        if len(conversation_history) == 0:
            await asyncio.to_thread(
                user_db.auto_title_conversation, conversation_id, user_id
            )

    # Client-side drawings.
    client_drawings = list(body.client_drawings or [])
    for d in client_drawings:
        if isinstance(d, dict):
            for field_name in ("label", "drawing_type", "notes"):
                if field_name in d and isinstance(d[field_name], str):
                    d[field_name] = sanitize_for_llm(d[field_name], max_length=200)

    recipe_id = body.recipe_id
    if recipe_id is not None:
        try:
            recipe_id = int(recipe_id)
        except (ValueError, TypeError):
            recipe_id = None

    thesis_text = sanitize_for_llm(body.thesis_text or "", max_length=2000)
    news_context = sanitize_for_llm(body.news_context or "", max_length=1000)

    # 3-tier context assembly.
    from carrac.ai.context_tiers import assemble_context
    context_text, context_tokens, block_summaries = await asyncio.to_thread(
        assemble_context,
        user_id=user_id, asset=asset, timeframe=timeframe,
        feature=feature, page_context=page_context, message=message,
        recipe_id=recipe_id, client_drawings=client_drawings,
        thesis_text=thesis_text, news_context=news_context,
        user_tier=tier,
    )

    # Tool calling setup.
    from carrac.ai.copilot_tools import (
        should_include_tools, get_tools_for_focus, is_write_tool, execute_tool,
    )
    _ai_provider = (os.getenv("AI_PROVIDER", "") or "").lower()
    _ai_model = os.getenv("AI_MODEL", "") or ""
    provider = (
        "anthropic"
        if _ai_provider == "anthropic" or _ai_model.startswith("claude-")
        else "openai"
    )
    tools = None
    if should_include_tools(message):
        tools = get_tools_for_focus(page_context, provider=provider, user_tier=tier)

    is_multi = len(conversation_history) > 0
    system_prompt = build_system_prompt(
        feature, shared_context="", viewport_context=context_text,
        scenario_context="", trader_context="",
        is_multi_turn=is_multi, tools_active=bool(tools),
    )

    # Sanitize rolling summary before re-injection.
    if conv_summary:
        _safe_summary = sanitize_for_llm(conv_summary, max_length=2000, source="conv_summary")
        system_prompt += (
            f"\n\n--- EARLIER IN THIS CONVERSATION ---\n"
            f"<user_summary>\n{_safe_summary}\n</user_summary>"
        )

    # Grounding preamble.
    _facts: dict = {}
    try:
        from carrac.grounding import build_facts_dict, grounded_system_prompt, facts_block
        _facts = await asyncio.to_thread(build_facts_dict, include_prices=True)
        _fb = facts_block(include_prices=True, facts=_facts)
        system_prompt = grounded_system_prompt(system_prompt, facts_block_text=_fb)
    except Exception as _grounding_err:
        logger.warning("Grounding preamble unavailable: %s", _grounding_err)

    max_tokens_val = get_max_tokens(feature)
    model = _ai_model

    messages_list: list = [{"role": "system", "content": system_prompt}]
    messages_list.extend(conversation_history)
    messages_list.append({"role": "user", "content": message})

    # ------------------------------------------------------------------
    # Async SSE generator — all captured variables from outer scope are
    # explicit closures so there are no nonlocal write issues.
    # ------------------------------------------------------------------

    # Mutable state shared between _generate and _run_one_round.
    # Using a dict so inner async generators can mutate without nonlocal.
    _state: dict = {
        "input_tokens": 0,
        "output_tokens": 0,
        "output_tokens_so_far": 0,
        "usage_details": None,
        "full_text": "",
        "settled": False,
        "disconnected": False,
        "tool_round": 0,
        "messages_list": messages_list,
        "stream_start": 0.0,  # set before first use in _generate
    }

    async def _run_one_round() -> AsyncGenerator[bytes, None]:
        """Run one LLM tool round, yielding SSE bytes. Mutates _state."""
        from carrac.ai.llm_client import astream_chat_with_tools

        got_tool_call = False

        async for event_type, chunk, meta in astream_chat_with_tools(
            _state["messages_list"], tools=tools, max_tokens=max_tokens_val
        ):
            if time.time() - _state["stream_start"] > 180:
                logger.warning(
                    "AI stream wall-clock timeout user=%s after 180s", user_id
                )
                break

            if event_type == "text" and chunk:
                _state["full_text"] += chunk
                _state["output_tokens_so_far"] += max(1, len(chunk) // 4)
                yield f"data: {json.dumps({'t': chunk})}\n\n".encode()

            elif event_type == "tool_call" and meta:
                got_tool_call = True
                tool_name = meta.get("name", "")
                tool_args = meta.get("arguments", {})
                tool_id = meta.get("id", "")

                if is_write_tool(tool_name):
                    yield (
                        f"data: {json.dumps({'action': {'id': tool_id, 'tool': tool_name, 'arguments': tool_args}})}\n\n"
                        .encode()
                    )
                    if provider == "anthropic":
                        _state["messages_list"].append({
                            "role": "assistant",
                            "content": [{"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_args}],
                        })
                        _state["messages_list"].append({
                            "role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                         "content": "Action proposed to user for confirmation. Continue your response."}],
                        })
                    else:
                        _state["messages_list"].append({
                            "role": "assistant", "content": None,
                            "tool_calls": [{"id": tool_id, "type": "function",
                                            "function": {"name": tool_name, "arguments": json.dumps(tool_args)}}],
                        })
                        _state["messages_list"].append({
                            "role": "tool", "tool_call_id": tool_id,
                            "content": "Action proposed to user for confirmation. Continue your response.",
                        })
                else:
                    yield f"data: {json.dumps({'tool_call': {'name': tool_name}})}\n\n".encode()
                    result_text = await asyncio.to_thread(
                        execute_tool, tool_name, tool_args, user_id, user_tier=tier
                    )
                    if provider == "anthropic":
                        _state["messages_list"].append({
                            "role": "assistant",
                            "content": [{"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_args}],
                        })
                        _state["messages_list"].append({
                            "role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": result_text}],
                        })
                    else:
                        _state["messages_list"].append({
                            "role": "assistant", "content": None,
                            "tool_calls": [{"id": tool_id, "type": "function",
                                            "function": {"name": tool_name, "arguments": json.dumps(tool_args)}}],
                        })
                        _state["messages_list"].append({
                            "role": "tool", "tool_call_id": tool_id, "content": result_text,
                        })

            elif event_type == "usage" and meta:
                _state["usage_details"] = meta
                _state["input_tokens"] += meta.get("prompt_tokens", 0)
                _state["output_tokens"] += meta.get("completion_tokens", 0)

        # Signal whether a tool call occurred so the outer loop knows to continue.
        if got_tool_call:
            yield b"__GOT_TOOL_CALL__"

    async def _generate() -> AsyncGenerator[bytes, None]:
        _state["stream_start"] = time.time()

        # Context summary event.
        ctx_lines = [f"Feature: {feature}"]
        if asset:
            ctx_lines.append(f"Asset: {asset} / {timeframe}")
        for bs in block_summaries:
            tier_label = f" T{bs.get('tier', '?')}" if bs.get("tier") else ""
            status = (
                f"{bs['tokens']} tok{tier_label}"
                if bs["included"]
                else f"skipped ({bs.get('reason', '')})"
            )
            ctx_lines.append(
                f"  {'✓' if bs['included'] else '✗'} {bs['name']}: {status}"
            )
        ctx_lines.append(f"Total: {context_tokens} tokens")
        if tools:
            ctx_lines.append(f"Tools: {len(tools)} available")
        ctx_summary = "\n".join(ctx_lines)
        yield f"data: {json.dumps({'ctx': ctx_summary})}\n\n".encode()

        try:
            max_tool_rounds = 3
            while _state["tool_round"] <= max_tool_rounds:
                got_tool_call = False
                async for chunk_bytes in _run_one_round():
                    if chunk_bytes == b"__GOT_TOOL_CALL__":
                        got_tool_call = True
                    else:
                        yield chunk_bytes

                if not got_tool_call:
                    break
                _state["tool_round"] += 1

            # Fallback token estimation.
            if _state["input_tokens"] == 0:
                hist_text = "".join(m.get("content", "") or "" for m in conversation_history)
                _state["input_tokens"] = len(system_prompt + message + hist_text) // 4
            if _state["output_tokens"] == 0:
                _state["output_tokens"] = len(_state["full_text"]) // 4

            _prompt_render = system_prompt + "\n\n" + message
            actual = await asyncio.to_thread(
                settle,
                user_id, estimate, _state["input_tokens"], _state["output_tokens"],
                feature=feature, asset=asset, model=model, usage=_state["usage_details"],
                prompt_text=_prompt_render, response_text=_state["full_text"],
                conversation_id=conversation_id,
            )
            _state["settled"] = True
            balance = await asyncio.to_thread(user_db.get_ai_balance, user_id)

            # Post-stream hallucination scan.
            try:
                from carrac.grounding import scan as _hscan
                _findings = _hscan(_state["full_text"], _facts)
                if _findings:
                    _highs = [f for f in _findings if f.severity.value == "HIGH"]
                    if _highs:
                        _warning = (
                            "\n\n⚠ **Verification notice** — one or more claims "
                            "in my answer could not be verified against the "
                            "platform's live data: "
                            + "; ".join(
                                f"{f.pattern_name} (\"{f.match_text}\")"
                                for f in _highs[:3]
                            )
                            + ". Treat those specific claims as uncertain."
                        )
                        _state["full_text"] += _warning
                        yield f"data: {json.dumps({'t': _warning})}\n\n".encode()
                        yield (
                            f"data: {json.dumps({'verification': {'verdict': 'warn', 'findings': [f.to_dict() for f in _highs]}})}\n\n"
                            .encode()
                        )
                        logger.warning(
                            "Copilot verification WARN user=%s: %s",
                            user_id, [f.pattern_name for f in _highs],
                        )
                    else:
                        yield (
                            f"data: {json.dumps({'verification': {'verdict': 'ok', 'low_findings': len(_findings)}})}\n\n"
                            .encode()
                        )
            except Exception as _scan_err:
                logger.debug("Post-stream scan failed: %s", _scan_err)

            # Persist assistant message.
            if conversation_id and _state["full_text"]:
                await asyncio.to_thread(
                    user_db.add_message,
                    conversation_id, "assistant", _state["full_text"],
                    feature=feature, user_id=user_id,
                )

            # Follow-up suggestions.
            from carrac.ai.features import get_follow_up_suggestions
            suggestions = get_follow_up_suggestions(feature, asset)
            if suggestions:
                yield f"data: {json.dumps({'suggestions': suggestions})}\n\n".encode()

            yield (
                f"data: {json.dumps({'done': True, 'charged': actual, 'bal': balance, 'conversation_id': conversation_id})}\n\n"
                .encode()
            )

        except asyncio.CancelledError:
            # client closed the connection mid-stream.
            _state["disconnected"] = True
            logger.info(
                "AI SSE client disconnected user=%s tool_round=%s settled=%s "
                "output_tokens_so_far=%s",
                user_id, _state["tool_round"], _state["settled"],
                _state["output_tokens_so_far"],
            )
            if not _state["settled"]:
                try:
                    _in_tok = _state["input_tokens"]
                    if _in_tok == 0:
                        _hist_text = "".join(
                            m.get("content", "") or "" for m in conversation_history
                        )
                        _in_tok = len(system_prompt + message + _hist_text) // 4
                    _out_tok = max(_state["output_tokens"], _state["output_tokens_so_far"])
                    _prompt_render = system_prompt + "\n\n" + message
                    await asyncio.to_thread(
                        settle,
                        user_id, estimate, _in_tok, _out_tok,
                        feature=feature, asset=asset, model=model,
                        usage=_state["usage_details"],
                        prompt_text=_prompt_render, response_text="",
                        conversation_id=conversation_id,
                        disconnected=True,
                    )
                    _state["settled"] = True
                except Exception as settle_err:
                    logger.warning(
                        "AI disconnect-settle failed user=%s: %s — finally will refund",
                        user_id, settle_err,
                    )
            raise

        except Exception as e:
            logger.error("AI stream error user=%s: %s", user_id, e)
            try:
                balance = await asyncio.to_thread(user_db.get_ai_balance, user_id)
                yield (
                    f"data: {json.dumps({'error': 'AI service unavailable', 'bal': balance})}\n\n"
                    .encode()
                )
            except Exception:
                pass

        finally:
            # Refund reserved estimate when settle() didn't run.
            if not _state["settled"]:
                try:
                    await asyncio.to_thread(
                        user_db.refund_ai_credits, user_id, estimate
                    )
                    if _state["disconnected"]:
                        logger.info(
                            "AI chat refunded estimate=%d user=%s after disconnect "
                            "(settle path failed)",
                            estimate, user_id,
                        )
                except Exception as refund_err:
                    logger.warning(
                        "AI chat refund failed user=%s: %s", user_id, refund_err
                    )
            await asyncio.to_thread(release_chat_slot, user_id)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
