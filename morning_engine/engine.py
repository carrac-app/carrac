#!/usr/bin/env python3
"""Morning Engine — orchestrates data assembly and Anthropic/Grok API calls.

Usage:
    python -m morning_engine [--mode morning|afternoon|weekly|dispatch] [--dry-run]
    python -m morning_engine --mode dispatch --region europe [--dry-run]

Modes:
    morning   — Full morning report (default)
    afternoon — Delta update for paid/tester users
    weekly    — Weekly digest (Friday synthesis)
    dispatch  — Regional per-user personalized reports (requires --region)
"""

import argparse
import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from .config import (
    ANTHROPIC_MODEL_FAST,
    DASHBOARD_API_KEY, DASHBOARD_BASE_URL, DEFAULT_X_HANDLES,
    ENABLE_GENERAL_REPORT, ENABLE_MORNING_REPORT, GENERAL_REPORT_UNGATED, MAX_FREE_REPORT_TOKENS,
    MAX_MACRO_TOKENS, MAX_PERSONAL_TOKENS,
    MAX_REPORT_TOKENS, NTFY_MARKET_TOPIC, TTS_ENABLED, TTS_VOICE_ID,
)

# API key via header (not URL param) to avoid leaking in server logs
_API_HEADERS = {"X-Internal-Key": DASHBOARD_API_KEY}
from .data_assembly import (
    fetch_chart_data, fetch_context_data,
    fetch_options_data, fetch_permitted_derivatives, fetch_quant_context,
    fetch_signal_flips, fetch_yesterday_regime, fetch_yesterday_report,
    fetch_todays_general_report,
    format_since_yesterday_payload, format_since_yesterday_block,
    fetch_market_breadth, fetch_cot,
    INDICATOR_SIGNAL_MAP,
)
from carrac.llm.grok import GrokProvider
from .grok_client import GrokClient
from .anthropic_client import MorningAnthropicClient
from .prompts import (
    afternoon_delta_prompt, extract_morning_constraints, free_tier_report_prompt,
    macro_research_prompt, personal_section_prompt, standard_personal_prompt,
    shared_report_system_prompt, weekly_digest_prompt,
)

# Extracted helpers (behaviour-preserving module split)
from .briefing import (
    strip_section_header as _strip_section_header,
    extract_section as _extract_section,
    extract_exec_summary as _extract_exec_summary,
    extract_tldr_summary as _extract_tldr_summary,
    parse_asset_priority as _parse_asset_priority,
    build_shared_briefing_parts as _build_shared_briefing_parts,
    enforce_char_limit as _enforce_char_limit,
    trim_dangling_sentence as _trim_dangling_sentence,
    WEEK_AHEAD_DROP_ORDER as _WEEK_AHEAD_DROP_ORDER,
)
from .config_loader import (
    load_steering_yaml as _load_steering_yaml,
    load_patterns_yaml as _load_patterns_yaml,
    make_primary_client as _make_primary_client,
)
from .dashboard_api import (
    post_report as _post_report,
    log_engine_costs as _log_engine_costs,
    save_summary as _save_summary,
    save_metadata as _save_metadata,
    save_audio_script as _save_audio_script,
    save_personal_audio as _save_personal_audio,
    get_eligible_users as _get_eligible_users,
    get_user_analyst_sources as _get_user_analyst_sources,
    save_personal_section as _save_personal_section,
    debit_credits as _debit_credits,
)
from .notify import (
    notify as _notify,
    generate_tts as _generate_tts,
)
from carrac.grounding import escape_user_text as _escape_user_text
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("morning_engine")


# ==========================================================================
# Module-level helpers extracted to briefing.py / config_loader.py /
# dashboard_api.py / notify.py. Imports above preserve the
# underscore-prefixed aliases used by this file.
# ==========================================================================

# ==========================================================================
# SHARED BRIEFING BUILDER — see briefing.build_shared_briefing_parts
# ==========================================================================

# ==========================================================================
# LLM CALL RETRY HELPER
# ==========================================================================


def _call_with_retry(func, *args, max_attempts=3, base_wait=15,
                     rate_limit_wait=65, label="llm_call", **kwargs):
    """Retry an LLM call with exponential backoff and rate-limit-aware sleep.

    One retry policy for every LLM call (macro research, morning shared report,
    dispatch shared report, afternoon delta). Backoff: rate_limit_wait on
    429/"rate_limit" in error message, otherwise base_wait * attempt.

    Returns the function's result on success. Raises the LAST exception
    if all attempts fail — caller decides on fallback behavior.

    Args:
        func: callable to invoke (e.g. `opus.chat` or `grok.search`)
        max_attempts: total attempt count (default 3)
        base_wait: seconds to sleep on regular failure × attempt number
        rate_limit_wait: seconds to sleep when 429/rate_limit detected
        label: short string for log messages (e.g. "shared_report")
    """
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            log.warning("%s attempt %d/%d failed: %s", label, attempt, max_attempts, e)
            if attempt == max_attempts:
                break
            err_str = str(e).lower()
            wait = rate_limit_wait if ("rate_limit" in err_str or "429" in err_str) else base_wait * attempt
            log.info("%s: waiting %ds before retry...", label, wait)
            time.sleep(wait)
    raise last_err


# ==========================================================================
# QUANT SNAPSHOT FRESHNESS
# ==========================================================================

# How old a quant snapshot can be before personal sections force a re-fetch.
# 15 min covers normal report generation latency (5-10 min) with headroom.
QUANT_SNAPSHOT_MAX_AGE_SEC = 900


def _extract_afternoon_summary(content):
    """Extract a 1-2 sentence summary from afternoon report content.

    Parses the first non-empty prose line/paragraph from the Session Summary
    section. Falls back to the first substantive paragraph of the full report
    if the section is absent. No LLM call — pure text extraction.
    Returns a string ≤ 300 chars, or None if content is empty.
    """
    if not content:
        return None
    # Try to get Session Summary section body
    session_body = _extract_section(content, "Session Summary")
    source = session_body or content

    # Collect candidate lines: split on single newlines first so we can
    # skip heading/table/bullet lines individually even when they share a
    # paragraph block (common in short sections without blank-line separation).
    lines = [l.strip() for l in source.replace('\r', '').split('\n') if l.strip()]
    prose_lines = []
    for line in lines:
        if line.startswith('#') or line.startswith('|') or line.startswith('- '):
            # Flush any accumulated prose if we hit a non-prose line
            if prose_lines:
                break
            continue
        # Strip leading list markers / bold markers
        clean = line.lstrip('*').strip()
        if len(clean) < 20:
            continue
        prose_lines.append(clean)
        # Two prose lines is enough for a 1-2 sentence summary
        if len(' '.join(prose_lines)) >= 100:
            break

    if not prose_lines:
        # Fallback: try double-newline paragraph split
        paragraphs = [p.strip() for p in source.split('\n\n') if p.strip()]
        for para in paragraphs:
            if para.startswith('#') or para.startswith('|') or para.startswith('- '):
                continue
            clean = para.lstrip('*').strip()
            if len(clean) >= 20:
                prose_lines = [clean]
                break

    if not prose_lines:
        return None

    result = ' '.join(prose_lines)
    if len(result) > 300:
        cut = result.rfind('. ', 0, 300)
        result = result[:cut + 1] if cut > 50 else result[:300].rstrip() + '…'
    return result


def _ensure_fresh_quant(quant_context, max_age_sec=QUANT_SNAPSHOT_MAX_AGE_SEC):
    """Return a fresh quant_context dict if the cached one is stale.

    The shared report fetches quant context in Phase A. By the time
    per-user personal sections run (5-10 min later), regime flips can have
    invalidated it. This re-fetches if the cached snapshot is older than
    max_age_sec, otherwise returns the original.

    Always returns a dict with `_fetched_at` set.
    """
    fetched_at = quant_context.get("_fetched_at", 0)
    age = time.time() - fetched_at
    if age <= max_age_sec:
        return quant_context
    log.warning(
        "Quant snapshot %.0fs old (>%ds); re-fetching for personal section",
        age, max_age_sec,
    )
    try:
        fresh = fetch_quant_context()
        fresh["_fetched_at"] = time.time()
        return fresh
    except Exception as e:
        log.warning("Quant re-fetch failed; reusing stale (age=%.0fs): %s", age, e)
        return quant_context


# ==========================================================================
# PER-USER PERSONALIZED REPORTS
# ==========================================================================

def _user_context_sections(uid, user_tier, context):
    """Alerts + positions text for ONE user's prompt.

    The Phase-A ``context`` blob carries the OPERATOR's alerts
    (``get_user_alerts('admin')``) and a cross-user position read
    (``drawing_list(user_id=-1)``) assembled for the admin
    morning report. Non-admin users get their OWN rows, fetched in-process
    (same pattern as the per-user plays fetch); the operator keeps the
    global view (legitimate for the operator).

    Returns (alerts_text, positions_text) — same formatting as the
    Phase-A sections so downstream prompt assembly is unchanged.
    """
    if user_tier == "admin":
        return (context.get("alerts", "None"),
                context.get("positions", "None"))

    from .data_assembly import _format_alerts_summary, _format_json_compact

    alerts_text = "None"
    try:
        import push_notifications
        user_alerts = push_notifications.get_user_alerts(str(uid), active_only=False)
        alerts_text = _format_alerts_summary({"alerts": user_alerts})
    except Exception as e:
        log.debug("Per-user alerts fetch failed for %s: %s", uid, e)

    positions_text = "None"
    try:
        import json as json_mod
        from carrac.content_db import get_content_db
        db = get_content_db()
        positions = []
        for dtype in ("position", "long_position", "short_position"):
            for d in db.drawing_list(drawing_type=dtype, user_id=uid):
                meta = d.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json_mod.loads(meta)
                    except (ValueError, TypeError):
                        meta = {}
                if meta.get("archived"):
                    continue
                points = meta.get("points", [])
                if len(points) < 3:
                    continue
                entry_v = points[0].get("value", 0)
                sl_v = points[1].get("value", 0)
                tp_v = points[2].get("value", 0)
                direction = "long" if sl_v < entry_v else "short"
                risk = abs(entry_v - sl_v)
                reward = abs(tp_v - entry_v)
                rr = round(reward / risk, 2) if risk > 0 else 0
                positions.append({
                    "asset": d.get("asset", ""),
                    "timeframe": d.get("timeframe", ""),
                    "direction": direction,
                    "entry": round(entry_v, 2),
                    "stop_loss": round(sl_v, 2),
                    "take_profit": round(tp_v, 2),
                    "risk_reward": rr,
                    "label": d.get("label", ""),
                })
        positions_text = _format_json_compact(
            {"positions": positions, "count": len(positions)})
    except Exception as e:
        log.debug("Per-user positions fetch failed for %s: %s", uid, e)

    return alerts_text, positions_text


def _run_personalized_reports(report_id, shared_briefing_parts, chart_raw,
                               signal_data, context, signal_conflicts,
                               steering, patterns, sonnet, grok,
                               _record_phase, eligible_users=None,
                               quant_context=None):
    """Generate fully personalized reports for each eligible paying user.

    Replaces legacy Phase E (personal sections as appendix) with a unified
    per-user report that weaves market analysis with personal portfolio context.

    Users without configured report_assets/report_indicators receive the
    free-tier report + a CTA banner instead.

    If eligible_users is provided, uses that list instead of fetching all.
    This allows run_dispatch() to pass region-filtered users.

    quant_context: optional dict from Phase A. Checked for staleness and
    re-fetched if older than QUANT_SNAPSHOT_MAX_AGE_SEC; a "## Quant Refresh"
    note is added to each user's briefing if a refresh occurred.
    """
    from .config import (
        MAX_USER_REPORT_TOKENS, MAX_USER_REPORT_TOKENS_MEMBER,
        TTS_ENABLED,
    )
    from .prompts import personalized_report_prompt, combined_audio_prompt
    from .data_assembly import build_user_briefing

    if eligible_users is None:
        eligible_users = _get_eligible_users()
    log.info("Per-user reports: %d eligible users", len(eligible_users))

    if not eligible_users:
        return

    # Quant snapshot freshness check — if shared report took >15min the
    # cached quant data may be stale relative to current regime.
    quant_refresh_note = None
    if quant_context is not None:
        original_fetched_at = quant_context.get("_fetched_at", 0)
        refreshed = _ensure_fresh_quant(quant_context)
        if refreshed.get("_fetched_at", 0) > original_fetched_at:
            quant_refresh_note = (
                "## Quant Refresh\n"
                "Note: quant snapshot in shared briefing was stale at personal-section time; "
                "a refreshed snapshot has been included below. Defer to the refreshed values "
                "if they conflict with the shared briefing.\n\n"
                f"{refreshed.get('quant', 'Unavailable')}\n"
            )

    # Load user sources for all users
    all_user_sources = {}
    for u in eligible_users:
        all_user_sources[u["id"]] = _get_user_analyst_sources(u["id"])

    # Partition: configured vs unconfigured
    configured_users = []
    unconfigured_users = []
    for u in eligible_users:
        sources = all_user_sources[u["id"]]
        has_assets = bool(sources.get("report_assets"))
        has_indicators = bool(sources.get("report_indicators"))
        if has_assets or has_indicators:
            configured_users.append(u)
        else:
            unconfigured_users.append(u)

    log.info("Per-user: %d configured, %d unconfigured (CTA only)",
             len(configured_users), len(unconfigured_users))

    # Unconfigured users: save CTA banner (no LLM call)
    for u in unconfigured_users:
        cta = ("<!-- CUSTOMIZE_CTA -->\n"
               "## Personalize Your Report\n\n"
               "You're seeing the general market report. "
               "Head to **[Settings](/settings#morning-report)** to select "
               "your assets and indicators — your next morning brief will be "
               "built around YOUR portfolio.\n")
        try:
            _save_personal_section(report_id, u["id"], cta, cost_cents=0)
        except Exception as e:
            log.error("CTA save failed for report %s, user %s: %s", report_id, u["id"], e)

    if not configured_users:
        return

    # Build system prompt ONCE (cached across users by Anthropic)
    from carrac.grounding import grounded_system_prompt as _grounded_sp
    system_prompt = _grounded_sp(personalized_report_prompt(steering, patterns))

    # Build signal conflict text for user context
    conflict_text = ""
    if signal_conflicts:
        conflict_lines = ["Conflicts in today's signals:"]
        for c in signal_conflicts:
            conflict_lines.append(f"- {c['pair'][0]} vs {c['pair'][1]}: {c['implication']}")
        conflict_text = "\n".join(conflict_lines)

    def _generate_one_user_report(user):
        """Generate personalized report for one user (runs in thread)."""
        uid = user["id"]
        user_tier = user["tier"]
        sources = all_user_sources[uid]
        log.info("Generating personalized report for user %s (tier=%s)", uid, user_tier)

        # Credit check for non-exempt users
        if user_tier not in ("admin", "tester"):
            try:
                cr_resp = requests.get(
                    f"{DASHBOARD_BASE_URL}/api/credits/check",
                    params={"user_id": uid},
                    headers=_API_HEADERS, timeout=5)
                if cr_resp.ok and cr_resp.json().get("balance_cents", 0) < 10:
                    log.info("User %s insufficient credits, saving CTA", uid)
                    _save_personal_section(report_id, uid,
                                           "<!-- CREDITS_EXHAUSTED -->", cost_cents=0)
                    return
            except Exception as e:
                log.warning("Credit check failed for user %s, proceeding: %s", uid, e)

        # Assemble user-specific briefing
        user_report_assets = sources.get("report_assets", [])
        user_report_indicators = sources.get("report_indicators", [])
        trader_profile = sources.get("trader_profile", "")
        mr_prefs = sources.get("morning_report_prefs", {})

        # Fetch plays for this user
        plays_text = ""
        try:
            from carrac.plays.models import play_get_digest as _get_plays_ctx
            user_plays = _get_plays_ctx(uid)
            if user_plays.get("plays"):
                plines = [f"## Active Plays ({len(user_plays['plays'])})"]
                for p in user_plays["plays"]:
                    safe_title = _escape_user_text(p.get('title', ''), 'play_title', max_chars=200)
                    plines.append(
                        f"- [{p.get('status', '')}] {safe_title} "
                        f"({p.get('primary_asset', '')} {p.get('direction', '')})"
                    )
                    if p.get("thesis"):
                        plines.append(
                            f"  Thesis: {_escape_user_text(p['thesis'][:200], 'play_thesis', max_chars=200)}"
                        )
                plays_text = "\n".join(plines)
        except Exception as e:
            log.debug("Plays fetch failed for user %s: %s", uid, e)

        # Fetch user's yesterday personal report for continuity
        yesterday_personal = ""
        try:
            resp = requests.get(
                f"{DASHBOARD_BASE_URL}/api/report/morning/latest",
                params={"user_id": uid, "offset": 1},
                headers=_API_HEADERS, timeout=5)
            if resp.ok:
                prev = resp.json()
                if prev and prev.get("personal_content"):
                    yesterday_personal = prev["personal_content"][:3000]
        except Exception as e:
            # Non-fatal: continuity context is a nice-to-have. Log so the error tracker
            # can surface a pattern of failures (auth issue, schema drift, etc.)
            log.debug("Yesterday-report fetch failed for user %s: %s", uid, e)

        # Per-user alerts/positions — never the operator blob.
        user_alerts_text, user_positions_text = _user_context_sections(
            uid, user_tier, context)
        user_ctx = {
            "trader_profile": trader_profile,
            "plays_text": plays_text,
            "alerts": user_alerts_text,
            "positions": user_positions_text,
            "yesterday_personal": yesterday_personal,
        }
        if quant_refresh_note:
            # Surface the staleness so the model knows to defer to refreshed quant.
            user_ctx["quant_refresh_note"] = quant_refresh_note

        # user_briefing (trader profile, positions, plays/journal
        # text) is deliberately NEVER persisted — it goes to _save_personal_section
        # only, which has no source_briefing param. Only the shared/market
        # briefings (gen_briefing / briefing in run_morning/run_dispatch/
        # run_afternoon/run_weekly/run_week_ahead) are stored, via _post_report.
        user_briefing = build_user_briefing(
            shared_briefing_parts, chart_raw, signal_data,
            user_report_assets, user_report_indicators,
            user_ctx,
        )

        if conflict_text:
            user_briefing += f"\n{conflict_text}\n"

        # Choose max tokens by tier
        max_tok = MAX_USER_REPORT_TOKENS
        if user_tier == "member":
            max_tok = MAX_USER_REPORT_TOKENS_MEMBER

        # Generate personalized report (system prompt is CACHED after first user)
        report_content = None
        cost_cents = 0
        try:
            result = _call_with_retry(
                sonnet.chat,
                system_prompt=system_prompt,
                user_message=(
                    f"Generate a fully personalized morning report for this trader.\n\n"
                    f"{user_briefing}"
                ),
                max_tokens=max_tok,
                temperature=0.7,
                timeout=300,
                max_attempts=2,
                base_wait=10,
                label=f"personalized_report[user={uid}]",
            )
            report_content = result["content"]
            _pers_truncated = result.get("stop_reason") == "max_tokens"
            # Enforce hard char limit (member=14K, premium/admin=22K).
            _hard_limit = 22000 if user_tier in ("premium", "admin") else 14000
            report_content = _enforce_char_limit(
                report_content, _hard_limit,
                log_callback=lambda m, _u=uid: log.warning("user=%s %s", _u, m),
            )
            # If the model was cut off (hit max_tokens), drop the dangling partial
            # sentence so the personalized report never ends mid-thought.
            if _pers_truncated:
                report_content = _trim_dangling_sentence(report_content)
            cost_cents = max(1, int(result["usage"]["cost"] * 100))
            log.info("Personalized report for user %s: %d chars, $%.4f",
                     uid, len(report_content), result["usage"]["cost"])
        except Exception:
            log.error("Personalized report failed for user %s", uid, exc_info=True)
            return

        if not report_content:
            return

        # Calendar-fabrication tripwire (non-blocking) — covers ALL calendar-bearing
        # report paths via the shared helper.
        _calendar_tripwire(report_content, f"personalized report (user {uid})")
        # GATE, not a log-only tripwire, on the personal path —
        # see _advice_gate's docstring for why personal reports differ from
        # the shared/general ones.
        report_content = _advice_gate(report_content, f"personalized report (user {uid})")
        _news_tripwire(report_content, f"personalized report (user {uid})")
        _data_release_tripwire(report_content, f"personalized report (user {uid})")
        _leadership_tripwire(report_content, f"personalized report (user {uid})")

        # Haiku second-opinion fact check (non-blocking) against
        # the SAME per-user briefing the personalized report was generated
        # from. NOTE: this call site only fires when PERSONALIZED_REPORTS is
        # enabled, which stays off in production (see morning_engine/config.py) —
        # wired for completeness, not currently exercised in production.
        _verify_tripwire(report_content, user_briefing,
                         f"personalized report (user {uid})", "personalized")

        # Generate combined audio inline (if enabled)
        # Sonnet writes the script, grounded by the facts block and a regex scan.
        personal_audio_script = None
        if mr_prefs.get("voiceBriefing", True) and report_id and TTS_ENABLED:
            try:
                from carrac.grounding import (
                    facts_block, grounded_system_prompt,
                    scan as _hscan, has_high as _has_high, build_facts_dict,
                )
                # Extract key sections from the personalized report
                story = _extract_section(report_content, "The Story")
                portfolio = _extract_section(report_content, "Your Portfolio")
                actions = _extract_section(report_content, "Action Items")
                audio_input = (
                    f"=== MARKET STORY ===\n{story[:1500]}\n\n"
                    f"=== YOUR PORTFOLIO ===\n{portfolio[:2000]}\n\n"
                    f"=== ACTION ITEMS ===\n{actions[:1000]}"
                )
                facts = build_facts_dict(include_prices=True)
                fb_text = facts_block(include_prices=True, facts=facts)
                grounded = grounded_system_prompt(combined_audio_prompt(), facts_block_text=fb_text)
                pa_result = sonnet.chat(
                    system_prompt=grounded,
                    user_message=f"Generate a combined spoken briefing from the source:\n\n{audio_input}",
                    max_tokens=1200, temperature=0.6,
                )
                raw = _advice_gate(pa_result["content"],
                                   f"personalized audio script (user {uid})")
                findings = _hscan(raw, facts)
                if _has_high(findings):
                    log.error("Personal audio BLOCKED for user %s: %s",
                              uid, [f.pattern_name for f in findings if f.severity.value == "HIGH"])
                    personal_audio_script = None
                else:
                    personal_audio_script = raw
            except Exception as e:
                log.debug("Audio script failed for user %s: %s", uid, e)

        # Save personalized report as personal section
        _save_personal_section(report_id, uid, report_content,
                                cost_cents=cost_cents,
                                audio_script=personal_audio_script)

        # Deduct credits for non-exempt users — retries + alerts on exhaustion
        # so a transient API blip can't silently grant the user a free section.
        if user_tier not in ("admin", "tester"):
            _debit_credits(
                user_id=uid,
                amount_cents=cost_cents,
                feature="morning_personal",
                model="claude-sonnet-4-6",
                input_tokens=result["usage"].get("input_tokens", 0),
                output_tokens=result["usage"].get("output_tokens", 0),
            )

        # TTS generation
        if personal_audio_script and TTS_ENABLED:
            try:
                tts_result = grok.tts(personal_audio_script)
                if tts_result:
                    _save_personal_audio(report_id, uid, tts_result)
            except Exception as e:
                log.debug("TTS failed for user %s: %s", uid, e)

        log.info("Personalized report for user %s: %d chars%s",
                 uid, len(report_content),
                 " + audio" if personal_audio_script else "")

    # Run all configured users in parallel
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_generate_one_user_report, u): u
                   for u in configured_users}
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                uid = futures[f]["id"]
                log.error("Personalized report failed for user %s: %s", uid, exc, exc_info=exc)

    log.info("Personalized reports complete: %d configured + %d CTA",
             len(configured_users), len(unconfigured_users))


# _extract_section moved to briefing.py


# ==========================================================================
# MAIN FLOWS
# ==========================================================================

def _general_report_exists_today() -> bool:
    """True if a 'general' report row already exists for today's UTC date.

    Idempotency guard for the GENERAL_REPORT_UNGATED empty-region fall-through:
    once today's general report exists (produced by europe or the first empty
    region), later empty-region dispatches skip the shared pipeline instead of
    re-generating it. Fails OPEN (returns False → generate) so a transient DB
    error never suppresses the free-tier report.
    """
    try:
        from carrac.db_pool import get_conn, adapt_query
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        # db_pool.get_conn() is a RAII context manager (handles commit/release).
        with get_conn() as conn:
            row = conn.execute(
                adapt_query("SELECT 1 FROM reports WHERE report_type = ? "
                            "AND generation_date = ? LIMIT 1"),
                ('general', today),
            ).fetchone()
        return row is not None
    except Exception as e:
        log.warning("general-report exists-check failed (will generate): %s", e)
        return False


def _parse_x_posts(content: str) -> tuple[str, list[str]]:
    """Extract and validate X-post candidates from a general-report LLM response.

    The LLM is instructed to append a `## X-POSTS` tail section with lines
    prefixed `POST: <text>`. This function:
      1. Detects the section (case-insensitive, may have space variants).
      2. Strips it from the stored content.
      3. Parses POST: lines, validates them (length ≤280, no advice phrases).
      4. Returns (clean_content, validated_candidates).

    Candidates are the one piece of free prose that would go
    straight to the public internet (a tweet), so advice-phrase filtering
    runs the CANONICAL scanner (carrac.grounding.advice_scan.scan_advice —
    the same one used by _advice_tripwire for the whole report body)
    ALONGSIDE the original narrow inline word-list, rather than replacing
    it outright: the canonical scanner is high-precision by design (it
    requires advice *constructions* like "buy the dip"/"you need to buy" to
    avoid false-positiving on "buyers"/"sell-off"/"oversold"), so it does not
    strictly cover every phrasing the narrow list already caught (e.g. a bare
    "go long"/"go short" with no "now"/"here", or a bare "I would" with no
    verb). Keeping both is a pure coverage union — nothing that passed
    before now leaks through, and phrases like "buy the dip" that the narrow
    list missed are now caught too.

    A blocking hallucination-gate scan (the same one _publish_gate_ok runs
    over the whole report) is intentionally NOT run here — see
    _gate_filter_x_candidates, applied by the caller once retry/no-retry is
    resolved, so it isn't run twice on a discarded first draft.

    One retry is NOT triggered here — the validation is intentionally lenient
    (drops invalid candidates, keeps valid ones). The caller decides whether
    the surviving list is usable.
    """
    import re as _re
    from carrac.grounding.advice_scan import scan_advice

    # --- Detect and strip the X-POSTS section ---
    xpost_pattern = _re.compile(r'^##\s+X[-\s]?POSTS\s*$', _re.IGNORECASE | _re.MULTILINE)
    match = xpost_pattern.search(content)
    if not match:
        # Token truncation can leave 0 candidates; a report-row existence
        # check does not see that, only this log does. Grep-able prefix:
        # "x_posts:".
        log.warning("x_posts: X-POSTS section missing entirely from LLM response (0 candidates)")
        return content, []

    clean_content = content[:match.start()].rstrip()
    xpost_block = content[match.end():]

    # --- Parse POST: lines ---
    raw_candidates: list[str] = []
    for line in xpost_block.splitlines():
        line = line.strip()
        if line.upper().startswith('POST:'):
            text = line[5:].strip()
            if text:
                raw_candidates.append(text)

    # --- Validate candidates ---
    _ADVICE_WORDS = re.compile(
        r'\b(buy now|sell now|you should|i recommend|i would|go long|go short)\b',
        re.IGNORECASE,
    )
    validated: list[str] = []
    for candidate in raw_candidates:
        if len(candidate) > 280:
            log.info("x_posts: dropped candidate (too long: %d chars): %.60s...", len(candidate), candidate)
            continue
        narrow_hit = _ADVICE_WORDS.search(candidate)
        canonical_hits = scan_advice(candidate)
        if narrow_hit or canonical_hits:
            reason = canonical_hits or [narrow_hit.group(0)]
            log.info("x_posts: dropped candidate (advice-phrase %s): %.60s...", reason, candidate)
            continue
        validated.append(candidate)

    if not validated:
        log.warning(
            "x_posts: 0 of %d parsed candidates survived length/advice-phrase validation",
            len(raw_candidates),
        )
    else:
        log.info("x_posts: %d raw → %d validated", len(raw_candidates), len(validated))
    return clean_content, validated


def _build_levels_in_focus(spotlight_symbols, api):
    """Deterministic Levels-in-Focus block for the top-3 in-focus assets (or '').

    Wraps `api` into a (payload, status) fetch_fn expected by select_confluence_focus.
    Returns (rendered_block, focus_list) — the rendered markdown string (or '') and
    the raw focus list for use by the confluence tripwire.
    """
    from morning_engine.confluence_block import select_confluence_focus, render_confluence_focus

    def _fetch(sym):
        data = api(f'/api/confluence?symbol={sym}', timeout=8, retries=1)
        if isinstance(data, dict) and data.get('price'):
            return data, 'ok'
        return None, 'not_found'

    focus = select_confluence_focus(spotlight_symbols, _fetch)
    return render_confluence_focus(focus), focus


def _confluence_tripwire(report_content, focus_list, label):
    """Non-blocking: flag invented levels INSIDE the rendered Levels-in-Focus
    section. Scoped to that section only — the rest of the report legitimately
    cites CPR/swing/BB levels from other grounded sources."""
    try:
        if not focus_list:
            return
        import re as _re
        m = _re.search(r"#+\s*Levels in Focus\b.*?(?=\n#+\s|\Z)",
                       report_content or "", _re.IGNORECASE | _re.DOTALL)
        section = m.group(0) if m else ""
        if not section:
            return
        from carrac.grounding.hallucination_scan import scan_confluence_fabrication
        findings = scan_confluence_fabrication(section, focus_list)
        if findings:
            # Carry the grounded-set size: this scanner's failure mode is
            # grounding too narrowly (flagging real CPR pivots), and "grounded on 3 levels" vs "on 49" is the whole
            # diagnosis. Without it a future noise storm is unreadable.
            grounded_n = [(f.get("asset"), len(f.get("levels") or []))
                          for f in focus_list]
            log.error("[grounding] confluence fabrication in %s (grounded levels %s): %s",
                      label, grounded_n, findings)
            # Same _notify() path as every other tripwire.
            try:
                _notify(f"{label.title()} Confluence-Fabrication Tripwire",
                        "\n".join(str(f.get("detail") or f) for f in findings[:5]))
            except Exception as _ne:
                log.warning("Confluence tripwire notify failed (non-fatal): %s", _ne)
    except Exception as _ce:
        log.error("confluence tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _range_pctile_tripwire(report_content, grounded_pct, label):
    """Non-blocking: flag a Market Snapshot Table 1 'Range' cell that
    disagrees with the deterministic 90-day range_pctile the model was
    handed as ground truth. Defense-in-depth alongside the
    format_compact_asset_lines() source fix — that fix stops the model from
    being fed the wrong number in the first place; this catches any residual
    transcription/mis-attribution the model introduces on its own. Same
    idiom as the other tripwires: never blocks, just gives operator
    visibility so a slip can be caught and the prompt tightened."""
    try:
        if not grounded_pct:
            return
        from carrac.grounding.hallucination_scan import scan_range_pctile_mismatch
        findings = scan_range_pctile_mismatch(report_content, grounded_pct)
        if findings:
            log.error("[grounding] range_pctile mismatch in %s (grounded %s): %s",
                      label, grounded_pct, findings)
    except Exception as _ce:
        log.error("range_pctile tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _internal_arithmetic_tripwire(report_content, label):
    """Flag a stated % move that the report's OWN two printed prices refute.

    Generated text can contradict itself; the clearest case is
    "Oil 84 -> 71.90 ... -22.8%", which computes to -14.4%.
    This class is invisible to every upstream check by construction: both
    prices are real and the percentage is well-formed, they are just not the
    same claim. Only the generated text contains the contradiction.

    ⚠ This one DOES notify, unlike the six other tripwires that
    log.error into /var/log/morning-engine.log and reach nobody. Justified
    because the check is arithmetic rather than heuristic: it fires only when
    a start value, an end value and a percentage sit in one clause, so there
    is no interpretation involved and a hit is a hit. A false positive here
    would be a bug in the scanner, not a judgement call — which is exactly
    the property an alarm needs before it earns a phone.

    Applied to every report type; nothing about the failure is weekly-specific.
    """
    try:
        from carrac.grounding.hallucination_scan import scan_internal_arithmetic
        findings = scan_internal_arithmetic(report_content)
        if findings:
            log.error("[grounding] internal arithmetic mismatch in %s: %s", label, findings)
            _notify(
                f"{label.title()} ARITHMETIC MISMATCH",
                "\n".join(f["detail"] for f in findings[:5]),
            )
    except Exception as _ce:
        log.error("internal arithmetic tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _grounded_field_tripwire(report_content, claims, label):
    """Flag a stated numeric field that disagrees with the deterministic
    ground truth handed to the model for it (e.g. a BTC GEX flip
    point published as 165,694 when quant.derivatives held 69,109.33, 2.6x off).

    `claims` come from the SAME DB row the briefing renders — see
    morning_engine.data_assembly.fetch_options_data. An empty/None claims
    list (the licensing gate withheld the Deribit block, or this report
    type has no options data at all) is a silent no-op.

    Non-blocking, like the other tripwires. Reaches
    Sentry via capture_message so a human sees it — but, unlike
    _internal_arithmetic_tripwire, does NOT go through _notify (ntfy/
    Telegram): notify stays off until this scanner has been replayed
    against the stored report corpus (reports.source_briefing), the pass that
    gave the arithmetic tripwire its clause-break guard.
    """
    try:
        if not claims:
            return
        from carrac.grounding.hallucination_scan import scan_grounded_field_mismatch
        findings = scan_grounded_field_mismatch(report_content, claims)
        if findings:
            log.error("[grounding] grounded field mismatch in %s: %s", label, findings)
            import sentry_sdk
            sentry_sdk.capture_message(
                f"[grounding] {label} grounded field mismatch: "
                + "; ".join(f["detail"] for f in findings[:5]),
                level="error",
            )
    except Exception as _ce:
        log.error("grounded field tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _rotation_quadrant_tripwire(report_content, label):
    """Non-blocking: flag a Rotation Scorecard row whose Status disagrees
    with the actual RRG quadrant of an asset named in that row (e.g. a row
    grouping BTC/ETH/TOTAL called 'Lagging' when BTC and ETH are Leading:
    TOTAL's quadrant generalized onto the whole row). Fetches its own ground truth from
    the SAME cache the page's RS Rankings table and Rotation strip read —
    same idiom as _calendar_tripwire, so callers don't have to thread RS
    data through the generation path just for this check."""
    try:
        from carrac.grounding.hallucination_scan import scan_rotation_quadrant_mismatch
        from morning_engine.data_assembly import _api as _rs_api
        src = _rs_api("/api/relative-strength", timeout=10, retries=1)
        assets = (src or {}).get("assets") or []
        grounded = {
            str(a.get("symbol", "")).upper(): str(a.get("quadrant", "")).lower()
            for a in assets if a.get("symbol") and a.get("quadrant")
        }
        if not grounded:
            return
        findings = scan_rotation_quadrant_mismatch(report_content, grounded)
        if findings:
            log.error("[grounding] rotation quadrant mismatch in %s: %s", label, findings)
            # Same _notify() path as every other tripwire.
            try:
                _notify(
                    f"{label.title()} Rotation Quadrant Mismatch",
                    "\n".join(f["detail"] for f in findings[:5]),
                )
            except Exception as _ne:
                log.warning("Rotation quadrant tripwire notify failed (non-fatal): %s", _ne)
    except Exception as _ce:
        log.error("rotation quadrant tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _afternoon_grounding_tripwire(report_content, morning_content, label):
    """Non-blocking: run the spread self-consistency + breadth
    continuity checks BEFORE publish (here, before _post_report in
    run_afternoon), not only at save time.

    save_afternoon_report() (carrac/reports/routes.py) also runs
    check_afternoon_grounding() as a save-time backstop.
    Calling the SAME function here — reused, not reimplemented, so the two
    call sites cannot drift — fires the alarm before the report is public
    rather than only after. The save-time call is harmless
    defense-in-depth (a duplicate error-tracker event on a real finding, nothing
    worse) for any caller that ever posts to /api/report/afternoon directly
    without going through run_afternoon.

    Deliberately non-blocking, matching every other content-quality
    tripwire in this module (_advice_tripwire, _news_tripwire,
    _calendar_tripwire, _rotation_quadrant_tripwire, ...) and the explicit
    "notify, never withhold" posture check_afternoon_grounding itself
    documents. Only the curated fact-hallucination scan (_publish_gate_ok /
    _gate_scan) blocks — that is a narrower, higher-precision, separately
    vetted bar. These are best-effort regexes over known report shapes, not
    a parser (see afternoon_grounding.py's own docstring); dropping an
    entire day's report on a regex false positive would cost more than the
    early alarm saves. Never raises: same fail-safe shape as every tripwire
    here.
    """
    try:
        from carrac.reports.afternoon_grounding import check_afternoon_grounding
        findings = check_afternoon_grounding(report_content, morning_content)
        if findings:
            log.error("[grounding] %s spread/breadth contradiction (pre-publish): %s",
                      label, findings)
    except Exception as _ce:
        log.error("afternoon grounding tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _citation_sources_line(urls):
    """Distil Grok's url_citation list into a one-line
    'Sources consulted' note (unique domains, www-stripped) appended to the
    macro briefing — gives the writer models real outlets to attribute
    breaking claims to instead of a bare 'reportedly'. Returns '' when there
    is nothing usable. Per-claim citation gating is out of scope."""
    from urllib.parse import urlparse
    domains = []
    for u in urls or []:
        try:
            d = urlparse(u).netloc.lower()
        except Exception:
            continue
        if d.startswith("www."):
            d = d[4:]
        if d and d not in domains:
            domains.append(d)
    if not domains:
        return ""
    return ("Sources consulted (attribute breaking claims to these outlets "
            "where relevant): " + ", ".join(domains[:12]))


def _news_tripwire(report_content, label):
    """Non-blocking: flag unhedged breaking-news event claims (MEDIUM).

    log.error, not warning: the Sentry logging integration only captures
    ERROR+ as standalone error-tracker events — warning is a breadcrumb nobody
    sees. MEDIUM means "alert, not block", so it must alert.

    log.error alone lands only in the host log file, invisible to
    the error tracker/Dozzle/the phone, so a hit also _notify()s, the same path every
    other tripwire in this module uses (e.g. _internal_arithmetic_tripwire)."""
    try:
        from carrac.grounding.hallucination_scan import scan_news_fabrication
        findings = scan_news_fabrication(report_content)
        if findings:
            log.error("[grounding] unhedged news claim in %s: %s", label, findings)
            try:
                _notify(
                    f"{label.title()} Unhedged News Claim",
                    "\n".join(f["detail"] for f in findings[:5]),
                )
            except Exception as _ne:
                log.warning("News tripwire notify failed (non-fatal): %s", _ne)
    except Exception as _ce:
        log.error("news tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _leadership_tripwire(report_content, label):
    """Non-blocking: flag an unhedged central-bank/institutional leadership
    change ('incoming Fed Chair X') — the named-official fabrication class. MEDIUM: alert, never block."""
    try:
        from carrac.grounding.hallucination_scan import scan_leadership_fabrication
        findings = scan_leadership_fabrication(report_content)
        if findings:
            log.error("[grounding] unhedged leadership claim in %s: %s", label, findings)
            # Same _notify() path as every other tripwire.
            try:
                _notify(f"{label.title()} Leadership-Claim Tripwire",
                        "\n".join(str(f.get("detail") or f) for f in findings[:5]))
            except Exception as _ne:
                log.warning("Leadership tripwire notify failed (non-fatal): %s", _ne)
    except Exception as _ce:
        log.error("leadership tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _advice_tripwire(report_content, label):
    """Non-blocking: flag advice-phrase constructions ('you should', 'I
    recommend', 'I would buy/sell', ...) — backstops the prompt-level
    non-advice posture.
    Same idiom as the other tripwires: never blocks or raises, just gives
    operator visibility so a slip can be caught and the prompt tightened.

    A literal banned phrase ("hedge or reduce", "buy the dip") could reach a
    saved personal report with only a host logfile as the trace, so every hit
    also _notify()s, same path as every other tripwire. For PERSONAL reports specifically, see
    _advice_gate below — this function alone still never blocks or scrubs.
    """
    try:
        from carrac.grounding.advice_scan import scan_advice
        hits = scan_advice(report_content)
        if hits:
            log.error("[grounding] advice-phrase tripwire in %s: %s", label, hits)
            try:
                _notify(f"{label.title()} Advice-Phrase Tripwire", "\n".join(hits[:5]))
            except Exception as _ne:
                log.warning("Advice tripwire notify failed (non-fatal): %s", _ne)
    except Exception as _ce:
        log.error("advice tripwire failed (non-fatal): %s", _ce, exc_info=True)


# Sentence tokenizer mirroring carrac.compliance.deribit_gate._SENTENCE_RE's
# shape (a sentence-ender followed by any run of closing markdown marks) —
# reimplemented here rather than imported because that module's scrub is
# purpose-built for DERIBIT_PROSE_PATTERNS plus column-aware table handling
# this content doesn't need (advice phrases show up in prose/bullets, not
# calendar or snapshot tables), and this change is scoped to
# morning_engine/engine.py.
_ADVICE_SENTENCE_RE = re.compile(r'.*?[.!?][*_")\]]*(?:\s+|$)', re.DOTALL)
_ADVICE_LIST_MARKER = re.compile(r"^(\s*(?:[-*+]|\d+\.)\s+)(.*)$")


def _scrub_advice_sentences(content, patterns):
    """Remove any sentence (or whole bullet, if it never resolves to a
    clean remainder) matching an advice-phrase pattern. Mirrors
    scrub_deribit_prose's tokenize -> drop matching units -> rejoin shape."""

    def _has_advice(text):
        return any(p.search(text) for p in patterns)

    out_lines = []
    for line in content.splitlines():
        if not _has_advice(line):
            out_lines.append(line)
            continue
        marker = _ADVICE_LIST_MARKER.match(line)
        prefix, body = (marker.group(1), marker.group(2)) if marker else ("", line)
        parts = [p for p in _ADVICE_SENTENCE_RE.findall(body) if p.strip()]
        consumed = sum(len(p) for p in parts)
        if consumed < len(body) and body[consumed:].strip():
            parts.append(body[consumed:])
        kept = [p.strip() for p in parts if not _has_advice(p)]
        new_body = " ".join(kept)
        if new_body.strip():
            out_lines.append(prefix + new_body)
        # else: the whole line was advice-phrase — drop it entirely.
    return "\n".join(out_lines)


def _advice_gate(report_content, label):
    """Blocking-in-effect GATE for PERSONAL reports: a
    personal report reaches exactly one reader, so — unlike the shared/
    general reports where _advice_tripwire above stays log+notify-only
    (blocking on a false positive there would drop the report for every
    reader, the shared-report asymmetry) — the offending sentence can be
    scrubbed before save.

    Mirrors carrac.compliance.deribit_gate's alert_if_barred_prose (alarm)
    + scrub_deribit_prose (sentence-level scrub) shape: alarm first — via
    _advice_tripwire itself, so the log line and _notify() are byte-
    identical to every other call site — then scrub. Fails OPEN to the
    original, unscrubbed text on any internal error or if scrubbing would
    empty the report outright: a bug in the gate must not silently kill a
    personal report, same posture as _publish_gate_ok.
    """
    try:
        from carrac.grounding.advice_scan import scan_advice, _ADVICE_PATTERNS
        hits = scan_advice(report_content)
        if not hits:
            return report_content
        _advice_tripwire(report_content, label)
        scrubbed = _scrub_advice_sentences(report_content, _ADVICE_PATTERNS)
        return scrubbed if scrubbed.strip() else report_content
    except Exception as _ge:
        log.warning("Advice gate failed (non-fatal, fail-open): %s", _ge)
        return report_content


def _scenario_advisory_tripwire(report_content, label):
    """Non-blocking: flag prescriptive-scenario constructs ('Trigger:',
    'Invalidation', 'Scenario A/B', 'weight of evidence leans') in the general
    report's observational '## What Would Change the Picture' section — the
    brand rule allows evidence framing only.
    Same idiom as _advice_tripwire: ADVISORY, never blocks or raises — a stray
    word can never take the report offline; it just gives operator visibility so
    a slip can be caught and the prompt tightened."""
    try:
        from carrac.grounding.advice_scan import scan_prescriptive_scenario
        hits = scan_prescriptive_scenario(report_content)
        if hits:
            log.error("[grounding] prescriptive-scenario tripwire in %s: %s", label, hits)
    except Exception as _ce:
        log.error("scenario advisory tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _data_release_tripwire(report_content, label):
    """Non-blocking: flag unattributed economic-print figures (PMI/ISM/home sales/etc.)."""
    try:
        from carrac.grounding.hallucination_scan import scan_data_release_fabrication
        findings = scan_data_release_fabrication(report_content)
        if findings:
            # ERROR so it lands in the error tracker as an event (see _news_tripwire).
            log.error("[grounding] unattributed data-release figure in %s: %s", label, findings)
            try:
                _notify(
                    f"{label.title()} Unattributed Data-Release Figure",
                    "\n".join(f["detail"] for f in findings[:5]),
                )
            except Exception as _ne:
                log.warning("Data-release tripwire notify failed (non-fatal): %s", _ne)
    except Exception as _ce:
        log.error("data-release tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _calendar_tripwire(report_content, label):
    """Non-blocking calendar-fabrication tripwire for ANY calendar-bearing report.

    Flags economic events / consensus in the report not grounded in quant.events.
    Never blocks or raises — logs for operator visibility, like the other tripwires.
    """
    try:
        from datetime import timedelta as _td
        from carrac.grounding.hallucination_scan import scan_calendar_fabrication
        from morning_engine.calendar_block import CALENDAR_WINDOW_DAYS
        from morning_engine.data_assembly import _api as _ev_api
        _from = datetime.now().strftime('%Y-%m-%d')
        # Match the report's calendar TABLE window (the single source of truth in
        # calendar_block) — a narrower window here false-flags events in the
        # 8–14 day range that the table legitimately includes.
        _to = (datetime.now() + _td(days=CALENDAR_WINDOW_DAYS)).strftime('%Y-%m-%d')
        src = _ev_api(f'/api/quant/events?from={_from}&to={_to}', timeout=10, retries=1)
        if isinstance(src, list):
            findings = scan_calendar_fabrication(report_content, src)
            if findings:
                log.error("[grounding] calendar fabrication in %s: %s", label, findings)
                # Same _notify() path as every other tripwire.
                try:
                    _notify(
                        f"{label.title()} Calendar Fabrication",
                        "\n".join(f["detail"] for f in findings[:5]),
                    )
                except Exception as _ne:
                    log.warning("Calendar tripwire notify failed (non-fatal): %s", _ne)
    except Exception as _ce:
        log.error("calendar tripwire failed (non-fatal): %s", _ce, exc_info=True)


def _gate_scan(content):
    """Thin wrapper around carrac.grounding.scan() for the blocking publish
    gate (the written report must clear the same hallucination bar the audio
    path enforces).

    Fail-open: a scan crash returns [] + log.warning rather than propagating,
    so a bug in the gate itself can never take down publishing — the gate is
    meant to catch real hallucinations, not become a new outage vector.
    """
    try:
        from carrac.grounding import scan as _scan, build_facts_dict as _bfd
        return _scan(content, _bfd(include_prices=True))
    except Exception as e:
        log.warning("gate scan failed (non-fatal, fail-open): %s", e)
        return []


def _gate_filter_x_candidates(candidates: list[str]) -> list[str]:
    """Per-candidate hallucination-gate scan for X-post candidates.

    Runs the SAME canonical scan() that the whole-report _publish_gate_ok
    blocks on (via _gate_scan, which already bundles in build_facts_dict —
    no extra wiring needed), but per-candidate instead of whole-report:
    ONE fabricated element drops just that tweet rather than sinking the
    entire general report the way _publish_gate_ok does for report bodies.

    Attempted and adopted (not the "explain why not" fallback): scan()'s
    API is `scan(text, facts) -> list[Finding]` — it needs only the
    candidate's own text plus the (already-self-fetching) facts dict, no
    whole-report context like the Levels-in-Focus confluence check needs.
    That makes it a clean, no-refactor per-candidate application. The one
    piece of _publish_gate_ok's machinery deliberately NOT replicated here
    is the BLOCKING behavior (drop-the-whole-report) and the operator
    _notify() alert — a single bad tweet should never sink an otherwise
    clean report, and paging on it would be noise per-candidate; it's
    logged instead.

    Fails open per-candidate: _gate_scan already fail-opens (a scan crash
    logs a warning and returns []), so a keeper on error is the natural
    behavior here too — the tweet is kept rather than dropped, consistent
    with every other tripwire in this module never blocking on its own
    failure.
    """
    survivors: list[str] = []
    for candidate in candidates:
        findings = _gate_scan(candidate)
        high = [f for f in findings if f.severity.value == "HIGH"]
        if high:
            log.warning(
                "x_posts: dropped candidate (gate:%s): %.80s...",
                high[0].pattern_name, candidate,
            )
            continue
        # The licensing gate, which this never had. 27 of 193
        # stored candidates narrate DVOL/GEX — the report BODY was gated
        # But X-POSTS are split out at _parse_x_posts before any
        # of that runs. Dropped, not scrubbed: a 280-char claim minus its
        # barred sentence is a fragment, and a tweet is published verbatim.
        try:
            from carrac.compliance.deribit_gate import scrub_deribit_prose
            if (scrub_deribit_prose(candidate) or '').strip() != candidate.strip():
                log.warning(
                    "x_posts: dropped candidate (licensing): %.80s...", candidate,
                )
                continue
        except Exception as _lic_err:  # noqa: BLE001
            # Fail CLOSED here, unlike the hallucination scan above. That one
            # keeps a tweet on error because a scan bug should not silence the
            # account; this one is a licence boundary, where the cost of one
            # lost candidate is nothing and the cost of one published figure is
            # the licence.
            log.warning("x_posts: licensing scrub failed, dropping candidate: %s", _lic_err)
            continue
        survivors.append(candidate)
    return survivors


def _publish_gate_ok(content, label):
    """Blocking anti-hallucination gate. Returns False (and logs + notifies)
    only on a HIGH-severity finding — mirrors the audio-briefing blocking
    pattern (scan → has_high → log.error + _notify + drop), applied to the
    written report so it meets the same bar. Never raises: a bug in the gate
    itself must fail OPEN (allow publish) rather than silently killing the
    day's report — the gate exists to catch real hallucinations, not to
    become a second outage vector.
    """
    try:
        findings = _gate_scan(content)
        high = [f for f in findings if f.severity.value == "HIGH"]
        if not high:
            return True
        log.error("%s BLOCKED — hallucination findings: %s",
                  label, [f.to_dict() for f in high])
        try:
            _notify(f"{label.title()} BLOCKED",
                    f"{label} dropped by publish gate. First finding: "
                    f"{high[0].to_dict()['pattern']} — {high[0].to_dict()['match']!r}")
        except Exception as _ne:
            log.warning("Publish-gate notify failed (non-fatal): %s", _ne)
        return False
    except Exception as _ge:
        log.warning("Publish gate failed (non-fatal, fail-open): %s", _ge)
        return True


def _log_verify_cost(report_type, cost):
    """Fold carrac.grounding.verify's Haiku spend into the SAME in-process
    path report generation already uses (dashboard_api.log_engine_costs)
    — logs to ai_usage_log AND bumps the global daily-spend counter
    so the $30/day cap sees it. 
    """
    if not cost or cost <= 0:
        return
    cost_cents = max(1, int(cost * 100))
    try:
        from user_db import user_db
        from carrac.grounding.verify import _VERIFY_MODEL as _verify_model_label
        user_db.log_ai_usage(
            user_id=0,
            feature=f"engine_{report_type}_verify",
            asset=None,
            input_tokens=0,
            output_tokens=0,
            cost_cents=cost_cents,
            charged_cents=0,  # platform cost, not charged to a user
            model=_verify_model_label,
        )
    except Exception as e:
        from .dashboard_api import _ops_beacon
        _ops_beacon(f"verify cost usage-log failed ({report_type}): {e} — "
                    f"{cost_cents}c of real spend is invisible to admin "
                    f"analytics")
    try:
        from carrac.ai import billing
        billing._increment_global_spend(cost_cents)
    except Exception as e:
        from .dashboard_api import _ops_beacon
        _ops_beacon(f"verify global-spend increment failed ({report_type}): "
                    f"{e} — the daily LLM cost cap is now blind to "
                    f"{cost_cents}c of real spend")


def _verify_tripwire(generated_text, source_text, label, report_type, facts_block_text=None):
    """Non-blocking second-opinion fact check via carrac.grounding.verify's
    Haiku pass — runs AFTER the regex tripwires and (where one exists) the
    publish gate, same idiom as every other tripwire in this module: never
    blocks or raises, logs at ERROR + notifies the operator on a
    HALLUCINATION verdict, stays silent on CLEAN.

    Per the shared-report
    asymmetry: blocking a shared report leaves every user with no report,
    which is worse than shipping one flagged claim, so this NEVER drops or
    blocks the report — it is a second opinion the operator acts on, not a
    gate. verify_against_source() already respects GROUNDING_VERIFY_ENABLED=0
    internally (short-circuits to CLEAN, cost=0) — no duplicate check here.

    facts_block_text: if not supplied, fetches a fresh one — cheap, same
    pattern as the critic/audio call sites nearby that each fetch their own.
    """
    if not generated_text or not source_text:
        return
    if facts_block_text is None:
        try:
            from carrac.grounding import facts_block as _verify_fb
            facts_block_text = _verify_fb(include_prices=True)
        except Exception:
            facts_block_text = ""
    try:
        from carrac.grounding.verify import verify_against_source
        result = verify_against_source(generated_text, source_text, facts_block_text)
    except Exception as e:
        log.error("verify tripwire failed (non-fatal): %s", e, exc_info=True)
        return
    _log_verify_cost(report_type, result.cost)
    if result.verdict == "HALLUCINATION":
        log.error("[grounding] Haiku verifier HALLUCINATION in %s: %s",
                  label, result.unsupported)
        try:
            first = (result.unsupported or [{}])[0]
            _notify(f"{label.title()} Verifier HALLUCINATION",
                    f"Unsupported claim: {first.get('quote', '')!r} — "
                    f"{first.get('why', '')}. Report NOT blocked (the "
                    f"verifier is a second opinion, not a gate); review ASAP.")
        except Exception as _ne:
            log.warning("Verify tripwire notify failed (non-fatal): %s", _ne)
    elif result.verdict == "ERROR" and result.raw not in ("<disabled>", "<empty>"):
        # An ERROR means the report shipped UNCHECKED. That is not
        # a debug detail — it is the same blind spot as having no verifier at
        # all: a verifier that always errors (for example by truncating
        # every response) would pass every report unnoticed. Loud, and notify on the
        # truncation case specifically (the one that recurs silently and is
        # fixed by raising a constant rather than by waiting it out).
        log.error("[grounding] Haiku verifier ERROR in %s — report NOT checked: %s",
                  label, result.raw[:200])
        if result.raw.startswith("<truncated>"):
            try:
                _notify(f"{label.title()} Verifier BLIND",
                        "The fact verifier's verdict was truncated, so this "
                        "report shipped unchecked. Raise _MAX_VERDICT_TOKENS "
                        "in carrac/grounding/verify.py.")
            except Exception as _ne:
                log.warning("Verify tripwire notify failed (non-fatal): %s", _ne)


_GENERAL_REPORT_FOOTER = (
    "\n\n---\n_Free covers the market's hotspots — the full version focuses "
    "this same levels-and-indicators read on what you're trading._"
)


def _generate_general_report(shared_briefing_parts, spotlight_symbols,
                             compact_summary_text, steering, patterns,
                             sonnet, t_start, label="general",
                             morning_report: str | None = None,
                             continuity_block: str | None = None,
                             scenario_levels: dict | None = None,
                             range_pctile_map: dict | None = None,
                             options_claims: list | None = None):
    """Generate + save the free-tier "general" report.

    INDEPENDENCE: The general report is written from the SAME underlying
    shared_briefing_parts (macro / signals / quant / charts) that feed the
    operator morning report — NOT from the finished morning report text.
    This guarantees the general report writes independently rather than
    paraphrasing the morning report.

    The free-tier prompt instructs the model to adopt a distinct educational
    editorial identity (market-wide, ELI5-explanatory, no operator perspective).

    DEDUP: After generation, intra-report and cross-report overlap are checked
    via morning_engine.dedup. If thresholds are breached, one corrective
    Sonnet retry is attempted. A failure in the dedup/retry path is non-fatal —
    the original content is used. Metrics are logged and stored in metadata.

    X-POSTS: The response is expected to contain a `## X-POSTS` tail section.
    The engine strips it, validates candidates (≤280 chars, canonical
    advice-phrase scan, then a per-candidate hallucination-gate scan — see
    _parse_x_posts / _gate_filter_x_candidates), and stores the
    survivors in the report metadata under `x_candidates`. This section
    never appears in the stored report content.

    Generated once per cycle alongside the morning report (fixed operator cost
    regardless of user count). Idempotent per (report_type, UTC day).
    Non-fatal — a failure here never blocks the paid morning pipeline.
    """
    if not ENABLE_GENERAL_REPORT:
        return None
    try:
        from carrac.grounding import grounded_system_prompt as _gsp_gen
        from .dedup import check_intra_report, check_cross_report_overlap, build_dedup_correction_prompt

        sys_prompt = _gsp_gen(free_tier_report_prompt(steering, patterns))

        # Build the briefing from UNDERLYING data, not the finished morning report.
        # shared_briefing_parts already contains macro / signals / quant / charts.
        # We add spotlight context for the Market Snapshot tables only.
        gen_parts = list(shared_briefing_parts) + [
            f"## SPOTLIGHT ASSETS: {', '.join(spotlight_symbols)}\n"
            f"These assets can receive more narrative depth. All others appear in the Market Snapshot table.\n"
            if spotlight_symbols else "",
            f"\n## Non-Spotlight Asset Summary\n{compact_summary_text}\n" if compact_summary_text else "",
            # Daily continuity data for the general report's Since Yesterday section
            continuity_block if continuity_block else "",
        ]

        # Deterministic Levels in Focus block (top-3 spotlight assets near confluence zones).
        # Grounded data only — LLM must reproduce verbatim, never invent levels.
        from morning_engine.data_assembly import _api as _lif_api
        _levels_block, _levels_focus = _build_levels_in_focus(spotlight_symbols, _lif_api)
        if _levels_block:
            gen_parts.append(_levels_block)

        gen_briefing = "\n".join(p for p in gen_parts if p)

        # --- Initial generation ---
        result = _call_with_retry(
            sonnet.chat,
            system_prompt=sys_prompt,
            user_message=(
                "Generate the Carrac Market Pulse — the free-tier educational market "
                "overview — from the underlying data below. This is a STANDALONE "
                "PUBLICATION with its own editorial voice; write independently, "
                "do not paraphrase any other report.\n\n" + gen_briefing
            ),
            max_tokens=MAX_FREE_REPORT_TOKENS,
            temperature=0.7,
            timeout=300,
            max_attempts=2,
            label=label,
        )
        raw_content = result["content"]
        _gen_truncated = result.get("stop_reason") == "max_tokens"

        # --- Strip X-POSTS section before any further processing ---
        content, x_candidates = _parse_x_posts(raw_content)

        # --- Dedup check: intra-report ---
        intra = check_intra_report(content)
        log.info(
            "general_report dedup: dup_pairs=%d dup_facts=%d exceeds=%s",
            intra['dup_pair_count'], intra['dup_fact_count'], intra['exceeds_threshold'],
        )

        # --- Dedup check: cross-report overlap (vs morning report) ---
        cross: dict = {}
        if morning_report:
            cross = check_cross_report_overlap(morning_report, content)
            log.info(
                "general_report cross-overlap vs morning: %.1f%% (threshold: 25%%)",
                cross.get('overlap_fraction', 0) * 100,
            )

        # --- Corrective retry if thresholds exceeded ---
        needs_retry = intra['exceeds_threshold'] or (cross and cross.get('exceeds_threshold'))
        if needs_retry:
            log.warning(
                "general_report dedup thresholds exceeded — triggering corrective retry "
                "(dup_pairs=%d, dup_facts=%d, cross_overlap=%.1f%%)",
                intra['dup_pair_count'], intra['dup_fact_count'],
                cross.get('overlap_fraction', 0) * 100,
            )
            try:
                correction_prompt = build_dedup_correction_prompt(content, intra, cross or None)
                retry_result = sonnet.chat(
                    system_prompt=sys_prompt,
                    user_message=correction_prompt,
                    max_tokens=MAX_FREE_REPORT_TOKENS,
                    temperature=0.5,
                    timeout=300,
                )
                retry_raw = retry_result["content"]
                retry_content, retry_xcands = _parse_x_posts(retry_raw)
                # Prefer retry X-posts if the retry produced them
                if retry_xcands:
                    x_candidates = retry_xcands
                content = retry_content
                _gen_truncated = retry_result.get("stop_reason") == "max_tokens"
                # Re-check after retry (log only, no further retry)
                intra_post = check_intra_report(content)
                cross_post = check_cross_report_overlap(morning_report, content) if morning_report else {}
                log.info(
                    "general_report post-retry dedup: dup_pairs=%d dup_facts=%d cross=%.1f%%",
                    intra_post['dup_pair_count'], intra_post['dup_fact_count'],
                    cross_post.get('overlap_fraction', 0) * 100,
                )
            except Exception as retry_err:
                log.warning("general_report corrective retry failed (non-fatal): %s", retry_err)

        # Per-candidate hallucination-gate scan — same canonical
        # scan() the whole-report _publish_gate_ok blocks on (via _gate_scan),
        # applied here per-tweet so a single fabricated element drops just
        # that candidate instead of the whole report. Runs once the
        # corrective-retry decision above is final so a discarded first draft
        # is never scanned twice.
        _xc_before_gate = len(x_candidates)
        x_candidates = _gate_filter_x_candidates(x_candidates)
        if _xc_before_gate and not x_candidates:
            log.warning(
                "x_posts: 0 of %d candidates survived the hallucination-gate scan",
                _xc_before_gate,
            )

        # If the model was cut off (hit max_tokens), drop the dangling partial
        # sentence so the stored report never ends mid-thought (e.g. "...negative. On").
        if _gen_truncated:
            content = _trim_dangling_sentence(content)

        # 15000 safety net: at the 5500-token budget the full
        # report body (incl. the data-dense Market Snapshot + Levels in Focus
        # tables, X-POSTS already parsed out) runs ~14.3K chars.
        # A 13000 cap would drop ## Market Snapshot — the only general-report
        # section in DEFAULT_DROP_ORDER — on long runs. 15000 clears the
        # token-bounded max (~14.3K) so the snapshot survives; the 5500-token cap
        # still bounds total length, so this won't let output balloon.
        content = _enforce_char_limit(
            content, 15000,
            log_callback=lambda m: log.warning("general_report: %s", m),
        )
        # Haiku verifier wiring — capture the pure LLM-authored narrative BEFORE the
        # deterministic blocks below are appended, for the Haiku fact
        # verifier further down. Those blocks (TA cards, COT, Whale Watch,
        # BTC Network, Market Pulse, Cross-Asset Heat, Correlation Shifts)
        # are computed IN CODE from live data, never LLM-authored — running
        # an LLM fact-check over them would just add false-positive noise
        # for numbers the verifier has no way to independently source.
        _llm_narrative_for_verify = content
        # Deterministic TA cards — rendered IN CODE after char-limit enforcement,
        # before the footer, so they are stored verbatim and never fed to the LLM.
        try:
            from morning_engine.ta_cards import build_ta_cards_block
            _cards_block = build_ta_cards_block(spotlight_symbols[:3])
            if _cards_block:
                content = content.rstrip() + "\n\n" + _cards_block
        except Exception as _cards_err:
            log.warning("TA cards block failed (non-fatal): %s", _cards_err)
        # Deterministic COT positioning block — IN CODE, never fed to LLM.
        try:
            from morning_engine.report_blocks import build_cot_block
            _cot = build_cot_block()
            if _cot:
                content = content.rstrip() + "\n\n" + _cot
        except Exception as _cot_err:
            log.warning("COT block failed (non-fatal): %s", _cot_err)
        # Deterministic Whale Watch block (Hyperliquid smart-money) — IN CODE, never fed to LLM.
        try:
            from morning_engine.report_blocks import build_smart_money_block
            _whale = build_smart_money_block()
            if _whale:
                content = content.rstrip() + "\n\n" + _whale
        except Exception as _whale_err:
            log.warning("Whale Watch block failed (non-fatal): %s", _whale_err)
        # Deterministic Bitcoin Network Fundamentals block — IN CODE, never fed to LLM.
        try:
            from morning_engine.report_blocks import build_btc_network_block
            _btc_net = build_btc_network_block()
            if _btc_net:
                content = content.rstrip() + "\n\n" + _btc_net
        except Exception as _btc_net_err:
            log.warning("BTC Network block failed (non-fatal): %s", _btc_net_err)
        # Deterministic Market Pulse one-liner — IN CODE, never fed to LLM.
        try:
            from morning_engine.report_blocks import build_sentiment_vol_block
            _pulse = build_sentiment_vol_block()
            if _pulse:
                content = content.rstrip() + "\n\n" + _pulse
        except Exception as _pulse_err:
            log.warning("Market Pulse block failed (non-fatal): %s", _pulse_err)
        # Deterministic Cross-Asset Heat strip — IN CODE, never fed to LLM.
        try:
            from morning_engine.report_blocks import build_cross_asset_heat_block
            _heat = build_cross_asset_heat_block()
            if _heat:
                content = content.rstrip() + "\n\n" + _heat
        except Exception as _heat_err:
            log.warning("Cross-Asset Heat block failed (non-fatal): %s", _heat_err)
        # Deterministic Correlation Shifts block — IN CODE, never fed to LLM.
        try:
            from morning_engine.report_blocks import build_correlation_shift_block
            _corr = build_correlation_shift_block()
            if _corr:
                content = content.rstrip() + "\n\n" + _corr
        except Exception as _corr_err:
            log.warning("Correlation Shifts block failed (non-fatal): %s", _corr_err)
        content = content.rstrip() + _GENERAL_REPORT_FOOTER

        assets = list(set(p.split("/")[0] for p in from_chart_pairs()))
        general_metadata = {
            'dedup': {
                'dup_pair_count': intra.get('dup_pair_count', 0),
                'dup_fact_count': intra.get('dup_fact_count', 0),
                'intra_exceeded': intra.get('exceeds_threshold', False),
                'overlap_vs_morning': cross.get('overlap_fraction', None),
                'cross_exceeded': cross.get('exceeds_threshold', False) if cross else False,
                'corrective_retry': needs_retry,
            },
            'x_candidates': x_candidates,
        }

        _calendar_tripwire(content, "general report")
        _advice_tripwire(content, "general report")
        _scenario_advisory_tripwire(content, "general report")
        _confluence_tripwire(content, _levels_focus, "general report")
        _news_tripwire(content, "general report")
        _data_release_tripwire(content, "general report")
        _leadership_tripwire(content, "general report")
        _range_pctile_tripwire(content, range_pctile_map, "general report")
        _rotation_quadrant_tripwire(content, "general report")

        # Blocking hallucination gate — the written general report must clear the same HIGH-severity
        # bar the audio path already enforces before it is ever posted.
        if not _publish_gate_ok(content, "general report"):
            return None

        # Haiku second-opinion fact check (non-blocking — see
        # _verify_tripwire). Checks the LLM narrative against the SAME
        # underlying briefing it was generated from (gen_briefing), not the
        # finished report, so evidence for its claims is actually present
        # in the source text sent to Haiku.
        _verify_tripwire(_llm_narrative_for_verify, gen_briefing, "general report", "general")
        _internal_arithmetic_tripwire(content, "general report")
        _grounded_field_tripwire(content, options_claims, "general report")

        # Deterministic Macro Facts strip — authoritative data from
        # build_facts_dict(), prepended AFTER tripwires (no scan needed) and
        # before persisting so every consumer sees it verbatim.
        try:
            from carrac.grounding import build_facts_dict, render_macro_facts_strip
            _facts_strip = render_macro_facts_strip(build_facts_dict(include_prices=True))
            if _facts_strip:
                content = _facts_strip + "\n\n" + content
        except Exception as _fs_err:
            log.warning("macro facts strip (general) failed (non-fatal): %s", _fs_err)
        # Append the computed level sets to the STORED briefing —
        # after generation, never to the prompt. The rendered Levels-in-Focus
        # table keeps only the nearest support/resistance, so without this a
        # report's CPR/band/swing citations leave no trace and a bad one cannot
        # be diagnosed later. split_retained() gives back exactly
        # what the model saw for anyone re-running the verifier.
        stored_briefing = gen_briefing
        try:
            from carrac.grounding.provenance import render_retained_sources
            _level_sets = {f["asset"]: f.get("levels") or [] for f in (_levels_focus or [])}
            _level_sets.update(scenario_levels or {})
            _retained = render_retained_sources(_level_sets)
            if _retained:
                stored_briefing = gen_briefing + "\n\n" + _retained
        except Exception as _rs_err:
            log.error("retained sources block failed (report saved without it): %s", _rs_err)

        rid = _post_report(
            content=content,
            report_type="general",
            assets=assets,
            generation_time_ms=int((time.time() - t_start) * 1000),
            metadata=general_metadata,
            source_briefing=stored_briefing,
        )
        if rid:
            log.info(
                "General (free-tier) report saved: #%s (%d chars, %d x_candidates)",
                rid, len(content), len(x_candidates),
            )
            # Populate the summary column from the TL;DR section so the general
            # report's summary is never NULL when content exists.
            gen_summary = _extract_tldr_summary(content)
            if gen_summary:
                _save_summary(rid, gen_summary)
            # A fresh general report just landed — evict /brief's page cache
            # immediately so anonymous visitors never see yesterday's brief
            # for up to the 10-minute TTL window after publish. Non-fatal — a miss here just
            # means that entry expires on its own TTL instead of early.
            try:
                from carrac.redis_cache import get_redis
                from carrac.reports.routes import _BRIEF_CACHE_KEY
                _r = get_redis()
                if _r:
                    _r.delete(_BRIEF_CACHE_KEY)
            except Exception:
                pass
        else:
            log.error("General (free-tier) report NOT saved — post_report returned None; "
                      "%d chars dropped (check /api/report/general route)", len(content))
        return rid
    except Exception as e:
        log.warning("General report generation failed (non-fatal): %s", e)
        return None


def _write_personal_section(*, uid, user_tier, sys_prompt, max_tok, sonnet,
                            grok, report_id, shared_report, mr_prefs):
    """Generate, check and save one user's personal section of the nightly
    report, then debit credits and render its audio.

    Lives at module level (rather than inside run_morning's per-user worker)
    so the path from model output to saved row can be exercised with a
    stub model. Returns (content, audio_script), or (None, None) when the
    model call failed and nothing was saved.
    """
    personal_content = None
    personal_tokens = {}
    try:
        personal_result = _call_with_retry(
            sonnet.chat,
            system_prompt=sys_prompt,
            user_message="Generate the personal analysis sections based on the user context provided.",
            max_tokens=max_tok,
            temperature=0.7,
            timeout=120,
            max_attempts=2,
            base_wait=5,
            label=f"personal_section[user={uid}]",
        )
        personal_content = personal_result["content"]
        personal_tokens = personal_result.get("usage", {})
        # Enforce hard char limit (premium=18K, member=5K — see prompts.py).
        _hard_limit = 18000 if user_tier in ("premium", "admin") else 5000
        personal_content = _enforce_char_limit(
            personal_content, _hard_limit,
            log_callback=lambda m, _u=uid: log.warning("user=%s %s", _u, m),
        )
    except Exception as e:
        log.error("Personal section failed for user %s after 2 attempts: %s", uid, e, exc_info=True)
    if not personal_content:
        return None, None

    # Same checks as the other personal paths. The advice check is a gate
    # here, not a log-only tripwire: it runs before the audio script is
    # built and before the save, so neither carries the removed sentence.
    _label = f"personal section (user {uid})"
    _calendar_tripwire(personal_content, _label)
    personal_content = _advice_gate(personal_content, _label)
    _news_tripwire(personal_content, _label)
    _data_release_tripwire(personal_content, _label)
    _leadership_tripwire(personal_content, _label)

    # Combined audio briefing for paying members (macro + personal in one)
    # Sonnet writes the script, grounded by the facts block; a HIGH regex hit blocks it.
    personal_audio_script = None
    if (user_tier in ('member', 'premium', 'admin') and report_id
            and mr_prefs.get('voiceBriefing', True)):
        try:
            from .prompts import combined_audio_prompt
            from carrac.grounding import (
                facts_block, grounded_system_prompt,
                scan as _hscan, has_high as _has_high, build_facts_dict,
            )
            # Extract shared report context (same pattern as Phase F)
            macro_section = _extract_section(shared_report, "Macro Context") or \
                            _extract_section(shared_report, "Macro") or ""
            overview = _extract_section(shared_report, "Overview") or ""
            # Extract personal sections
            exec_summary = _extract_section(personal_content, "Executive Summary") or ""
            p_action_items = _extract_section(personal_content, "Action Items") or ""
            if exec_summary:
                pa_input = (f"=== MACRO OVERVIEW ===\n"
                            f"Macro Context:\n{macro_section[:1500]}\n\n"
                            f"Report Overview:\n{overview[:1000]}\n\n"
                            f"=== PERSONAL ANALYSIS ===\n"
                            f"Executive Summary:\n{exec_summary[:2000]}\n\n"
                            f"Action Items:\n{p_action_items[:1000]}")
                facts = build_facts_dict(include_prices=True)
                fb_text = facts_block(include_prices=True, facts=facts)
                grounded = grounded_system_prompt(combined_audio_prompt(), facts_block_text=fb_text)
                pa_result = sonnet.chat(
                    system_prompt=grounded,
                    user_message=f"Generate a combined spoken briefing from the source:\n\n{pa_input}",
                    max_tokens=1200, temperature=0.6)
                raw = _advice_gate(pa_result["content"],
                                   f"personal audio script (user {uid})")
                findings = _hscan(raw, facts)
                if _has_high(findings):
                    log.error("Combined audio BLOCKED for user %s: %s", uid,
                              [f.pattern_name for f in findings if f.severity.value == "HIGH"])
                    personal_audio_script = None
                else:
                    personal_audio_script = raw
                    if len(personal_audio_script) > 4000:
                        personal_audio_script = personal_audio_script[:4000].rsplit('.', 1)[0] + '.'
                    log.info("Combined audio script for user %s: %d chars", uid, len(personal_audio_script))
        except Exception as e:
            log.warning("Combined audio script failed for user %s: %s", uid, e)

    actual_cost_cents = 0 if user_tier in ("admin", "tester") else int(personal_tokens.get("cost", 0) * 100)

    if report_id:
        _save_personal_section(
            report_id, uid, personal_content,
            cost_cents=actual_cost_cents,
            input_tokens=personal_tokens.get("input_tokens", 0),
            output_tokens=personal_tokens.get("output_tokens", 0),
            audio_script=personal_audio_script,
        )

    # Debit credits for non-exempt users. Retries + alerts on
    # exhaustion so a transient API blip can't silently grant the user
    # a free section.
    if actual_cost_cents > 0 and user_tier not in ("admin", "tester"):
        _debit_credits(
            user_id=uid,
            amount_cents=actual_cost_cents,
            feature="morning_personal",
            model=ANTHROPIC_MODEL_FAST,
            input_tokens=personal_tokens.get("input_tokens", 0),
            output_tokens=personal_tokens.get("output_tokens", 0),
        )

    # Personal TTS generation (after save, so script is persisted even if TTS fails)
    if personal_audio_script and report_id:
        try:
            audio_data = grok.tts(personal_audio_script, voice_id=TTS_VOICE_ID)
            _save_personal_audio(report_id, uid, audio_data)
            log.info("Personal TTS for user %s: %d bytes", uid, len(audio_data))
        except Exception as e:
            log.warning("Personal TTS failed for user %s: %s", uid, e)

    return personal_content, personal_audio_script


class _PhaseLog:
    """Per-phase outcomes of one run, stored in the report metadata.

    Phase E writes to it from several worker threads, so every write goes
    through ``record`` under the lock. ``record`` has the call signature
    ``_run_personalized_reports`` expects for its phase callback.
    """

    def __init__(self):
        self.results = {}
        self.lock = threading.Lock()

    def record(self, key, name, status, started_at, duration_ms,
               retries=0, error=None, detail=None):
        with self.lock:
            self.results[key] = {
                "name": name, "status": status,
                "started_at": started_at, "duration_ms": duration_ms,
                "retries": retries, "error": error, "detail": detail,
            }


def _abort_morning(phases, reason, *, error, detail):
    """Record the failed critical gate and tell the operator the run stopped."""
    phases.record("abort", "Critical Gate", "error",
                  datetime.now().strftime("%H:%M:%S"), 0,
                  error=error, detail=detail)
    log.error("ABORT: %s", reason)
    _notify("Morning Report ABORTED", reason)


@dataclass
class _MorningInputs:
    """Everything Phase A fetched. ``data_sources`` is filled in further by
    the later phases and ends up in the report's health panel."""
    chart_data: str
    chart_raw: dict
    context: dict
    options_data: str
    options_claims: list
    permitted_deriv: str
    quant_context: dict
    continuity_block: str
    data_sources: dict


@dataclass
class _MorningSignals:
    data: dict
    summary: str
    regime: str
    conflicts: list


@dataclass
class _MorningBriefing:
    steering: dict
    patterns: dict
    system_prompt: str
    shared_parts: list
    spotlight_symbols: list
    compact_summary_text: str
    range_pctile_map: dict
    text: str


def _fetch_continuity_block(quant_context, chart_raw):
    """Day-over-day continuity (signal flips, regime delta, yesterday's
    self-grade), rendered as one prompt block. Non-fatal by construction:
    the fetchers return empty values rather than raising."""
    # 'general' is the live daily type; the retired 'morning' type returns
    # None and would leave the Since Yesterday self-grade empty.
    yesterday_report_raw = fetch_yesterday_report(report_type='general')
    signal_flips = fetch_signal_flips()
    yesterday_regime = fetch_yesterday_regime(quant_context)
    continuity_payload = format_since_yesterday_payload(
        signal_flips=signal_flips,
        yesterday_regime=yesterday_regime,
        quant_context=quant_context,
        yesterday_report=yesterday_report_raw,
        chart_raw=chart_raw,
    )
    continuity_block = format_since_yesterday_block(continuity_payload)
    log.info(
        "Continuity context: has_data=%s, flips=%d, regime_change=%s",
        continuity_payload.get('has_data'),
        len(signal_flips),
        bool(continuity_payload.get('regime_change')),
    )
    return continuity_block


def _morning_data_sources(chart_raw, context, options_data, permitted_deriv, quant_context):
    """Health of every Phase A source, for the report's health panel."""
    data_sources = {}

    chart_pair_count = len(chart_raw) if chart_raw else 0
    data_sources["chart_data"] = {"status": "ok", "detail": f"{chart_pair_count} pairs"} if chart_pair_count > 0 else {"status": "error", "detail": "No chart data"}

    avail = context.get("_availability", {})
    for src, status in avail.items():
        data_sources[src] = {"status": "ok"} if status == "ok" else {"status": "error", "detail": str(status)}

    # An empty string here is the licensing gate doing its job
    # (fetch_options_data withholds the Deribit block from non-admin callers,
    # which is every cron-run report), NOT a fetch failure. Reporting it as
    # "error" would leave a permanent red in the health panel and train us to
    # ignore that panel.
    if options_data == "":
        data_sources["options"] = {"status": "ok", "detail": "withheld (licensing gate)"}
    else:
        data_sources["options"] = {"status": "ok"} if options_data and len(options_data) > 50 else {"status": "error", "detail": "No options data"}

    # The permitted positioning block is the one the SHARED reports actually
    # publish, so its absence is a real fault — unlike the Deribit block
    # above, which is withheld by design on every cron run.
    if permitted_deriv:
        data_sources["permitted_derivatives"] = {
            "status": "ok", "detail": f"{len(permitted_deriv)} chars"}
    else:
        data_sources["permitted_derivatives"] = {
            "status": "error", "detail": "positioning block empty"}

    qe_text = quant_context.get('quant', '')
    data_sources["quant_engine"] = {"status": "ok", "detail": f"{len(qe_text)} chars"} if qe_text and 'unavailable' not in qe_text.lower() else {"status": "error", "detail": "QE data unavailable"}

    if avail:
        failed = [k for k, v in avail.items() if v != "ok"]
        if failed:
            log.warning("Unavailable context sources: %s", ", ".join(failed))
    return data_sources


def _morning_assemble_data(phases):
    """Phase A: fetch every input the report is written from."""
    log.info("Phase A: Data assembly...")
    t_data = time.time()

    chart_data, chart_raw = fetch_chart_data()
    context = fetch_context_data()
    options_data, options_claims = fetch_options_data()
    # The permitted-data positioning block rides ALONGSIDE the Deribit block
    # rather than replacing it — the two are different reads (positioning vs
    # volatility) and the Deribit one is still assembled for the operator.
    # Its grounding claims are merged into options_claims so the existing
    # _grounded_field_tripwire calls cover both without a second wiring point.
    permitted_deriv, _permitted_deriv_claims = fetch_permitted_derivatives()
    options_claims = list(options_claims or []) + _permitted_deriv_claims
    quant_context = fetch_quant_context()
    # Tag fetch time so downstream phases (personal sections, audio) can
    # detect a stale snapshot and re-fetch before consuming. See
    # _ensure_fresh_quant() — guards against mid-run regime flips.
    quant_context["_fetched_at"] = time.time()

    continuity_block = _fetch_continuity_block(quant_context, chart_raw)
    data_sources = _morning_data_sources(
        chart_raw, context, options_data, permitted_deriv, quant_context)

    data_assembly_ms = int((time.time() - t_data) * 1000)
    log.info("Data assembly: %.1fs", data_assembly_ms / 1000)
    chart_pair_count = len(chart_raw) if chart_raw else 0
    phases.record("phase_a", "Data Assembly", "ok" if chart_pair_count > 0 else "error",
                  datetime.fromtimestamp(t_data).strftime("%H:%M:%S"), data_assembly_ms,
                  detail=f"{chart_pair_count} pairs")

    return _MorningInputs(
        chart_data=chart_data, chart_raw=chart_raw, context=context,
        options_data=options_data, options_claims=options_claims,
        permitted_deriv=permitted_deriv, quant_context=quant_context,
        continuity_block=continuity_block, data_sources=data_sources,
    )


def _morning_extract_signals(chart_raw, context, data_sources, phases):
    """Phase A.5: deterministic signal extraction (no LLM). Degrades to empty
    signals rather than failing the run."""
    log.info("Phase A.5: Signal extraction...")
    t_signals = time.time()
    from .signal_extractor import extract_all_signals

    signal_data = {}
    try:
        signal_data = extract_all_signals(chart_raw, context)
        signal_summary = signal_data["summary_json"]
        market_regime = signal_data["market_regime"]
        signal_conflicts = signal_data["signal_conflicts"]

        data_sources["signal_extraction"] = {
            "status": "ok",
            "detail": f"regime={market_regime}, {len(signal_conflicts)} conflicts, {len(signal_summary)} chars",
        }

        signal_ms = int((time.time() - t_signals) * 1000)
        phases.record("phase_a5", "Signal Extraction", "ok",
                      datetime.fromtimestamp(t_signals).strftime("%H:%M:%S"), signal_ms,
                      detail=f"regime={market_regime}, {len(signal_conflicts)} conflicts")

        log.info("Signals: regime=%s, conflicts=%d, summary=%d chars",
                 market_regime, len(signal_conflicts), len(signal_summary))
    except Exception as e:
        log.error("Signal extraction failed: %s", e, exc_info=True)
        signal_summary = "{}"
        market_regime = "unknown"
        signal_conflicts = []
        data_sources["signal_extraction"] = {"status": "error", "detail": str(e)}
        signal_ms = int((time.time() - t_signals) * 1000)
        phases.record("phase_a5", "Signal Extraction", "degraded",
                      datetime.fromtimestamp(t_signals).strftime("%H:%M:%S"), signal_ms,
                      error=str(e), detail="Falling back to empty signals")

    return _MorningSignals(data=signal_data, summary=signal_summary,
                           regime=market_regime, conflicts=signal_conflicts)


def _neutralize_macro_leaks(macro_text):
    """Training-leak tripwire on Grok's web/X output BEFORE it becomes report
    "source material" (the grounding preamble otherwise treats it as trusted).
    HIGH-severity leaked phrases (e.g. a stale Fed rate range) are replaced so
    the writer cannot repeat them. Named-official / treaty fabrications are
    constrained at the macro_research_prompt level. Non-fatal."""
    try:
        from carrac.grounding import (
            build_facts_dict as _bfd, scan as _mscan, Severity as _Sev)
        _mfindings = _mscan(macro_text, _bfd(include_prices=True))
        _mhighs = [f for f in _mfindings if f.severity == _Sev.HIGH]
        if _mhighs:
            log.error("Macro research HIGH tripwire: %s",
                      [(f.pattern_name, f.match_text) for f in _mhighs[:5]])
            for f in sorted(_mhighs, key=lambda x: x.span[0], reverse=True):
                s, e = f.span
                macro_text = macro_text[:s] + "[unverified]" + macro_text[e:]
    except Exception as _ge:
        log.error("Macro grounding scan skipped (non-fatal): %s", _ge, exc_info=True)
    return macro_text


def _morning_macro_research(grok, signal_summary, data_sources):
    """Grok web + X search for the day's macro picture, five attempts with
    escalating timeouts. Returns (macro_text, retries); macro_text is "" and
    data_sources["macro_research"] is marked "error" when every attempt failed."""
    macro_query = macro_research_prompt(signal_summary_json=signal_summary)

    from datetime import timedelta
    from_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")

    macro_text = ""
    macro_tools = [{"type": "web_search"}]
    if DEFAULT_X_HANDLES:
        macro_tools.append({
            "type": "x_search",
            "allowed_x_handles": DEFAULT_X_HANDLES,
            "from_date": from_date,
            "to_date": to_date,
        })
    else:
        log.warning("X_SEARCH_HANDLES is empty - macro research runs without x_search")
    macro_retries = 0
    macro_timeouts = [120, 120, 150, 180, 240]  # escalating timeouts
    for attempt in range(1, 6):  # 5 attempts
        try:
            macro_result = grok.search(
                macro_query,
                tools=macro_tools,
                max_tokens=MAX_MACRO_TOKENS,
                timeout=macro_timeouts[attempt - 1],
            )
            # macro_result is untrusted third-party text (Grok's x_search /
            # web_search). Scrub prompt-injection patterns before it lands in
            # a system prompt.
            from security import sanitize_for_llm as _sanitize
            macro_text = _sanitize(macro_result["content"], max_length=200_000, source="grok_macro")
            macro_text = _neutralize_macro_leaks(macro_text)
            # Thread the cited outlets into the briefing text.
            _cite_urls = macro_result.get('citations') or []
            _sources_line = _citation_sources_line(_cite_urls)
            if _sources_line:
                macro_text += "\n\n" + _sources_line
            data_sources["macro_research"] = {"status": "ok", "detail": f"{len(macro_text)} chars, {len(_cite_urls)} citations"}
            macro_retries = attempt - 1
            break
        except Exception as e:
            log.warning("Macro research attempt %d/5 failed: %s", attempt, e)
            macro_retries = attempt
            if attempt == 5:
                log.error("Macro research failed after 5 attempts", exc_info=True)
                macro_text = ""
                data_sources["macro_research"] = {"status": "error", "detail": str(e)}
            else:
                time.sleep(5 * attempt)  # 5s, 10s, 15s, 20s backoff
    return macro_text, macro_retries


def _morning_top5(sonnet, signal_summary, macro_text, data_sources):
    """Top-5 market drivers plus the per-asset priority classification
    ({asset: "CRITICAL"/"NOTABLE"/"BACKGROUND"}). Two attempts; on failure
    both come back empty and the report is written without them."""
    top5_text = ""
    asset_priority = {}
    from .prompts import top5_drivers_prompt
    for t5_attempt in range(1, 3):  # 2 attempts
        try:
            top5_result = sonnet.chat(
                system_prompt="You are a concise market analyst. Follow the output format exactly.",
                user_message=top5_drivers_prompt(signal_summary, macro_text),
                max_tokens=1200,
                temperature=0.5,
                timeout=60,
            )
            raw_top5 = top5_result["content"]

            # Dual output: top-5 drivers + asset priority
            separator = "---ASSET_PRIORITY---"
            if separator in raw_top5:
                parts = raw_top5.split(separator, 1)
                top5_text = parts[0].strip()
                priority_text = parts[1].strip()
                asset_priority = _parse_asset_priority(priority_text)
            else:
                top5_text = raw_top5
                log.warning("Top-5 response missing priority separator, no asset classification")

            data_sources["top5_drivers"] = {
                "status": "ok",
                "detail": f"{len(top5_text)} chars, {len(asset_priority)} assets classified",
            }
            log.info("Top-5 drivers: %d chars, %d assets classified, $%.4f",
                     len(top5_text), len(asset_priority), top5_result["usage"]["cost"])
            break
        except Exception as e:
            if t5_attempt < 2:
                log.warning("Top-5 drivers attempt %d/2 failed: %s", t5_attempt, e)
                time.sleep(5)
            else:
                log.warning("Top-5 drivers failed after 2 attempts: %s", e)
                data_sources["top5_drivers"] = {"status": "error", "detail": str(e)}
    return top5_text, asset_priority


def _morning_research(grok, sonnet, signal_summary, data_sources, phases):
    """Phase B: macro research, then the top-5 drivers written from it.
    Returns (macro_text, top5_text, asset_priority, macro_ok)."""
    log.info("Phase B: Macro research + top-5 drivers...")
    t_macro = time.time()

    macro_text, macro_retries = _morning_macro_research(grok, signal_summary, data_sources)
    top5_text, asset_priority = _morning_top5(sonnet, signal_summary, macro_text, data_sources)

    macro_ms = int((time.time() - t_macro) * 1000)
    log.info("Macro research + top-5: %.1fs", macro_ms / 1000)

    macro_status = data_sources.get("macro_research", {}).get("status", "error")
    phases.record("phase_b", "Macro Research + Top-5", macro_status,
                  datetime.fromtimestamp(t_macro).strftime("%H:%M:%S"), macro_ms,
                  retries=macro_retries,
                  error=None if macro_status == "ok" else data_sources.get("macro_research", {}).get("detail"),
                  detail=f"macro={'OK' if macro_status == 'ok' else 'FAIL'}, top5={'OK' if top5_text else 'FAIL'}")
    return macro_text, top5_text, asset_priority, macro_status != "error"


def _fetch_past_overviews():
    """The last five general reports' summaries, for narrative continuity.

    type=general: the morning-type stream stops when ENABLE_MORNING_REPORT is
    off. General reports carry summary, not audio_script.
    """
    past_overviews_text = ""
    try:
        resp = requests.get(
            f"{DASHBOARD_BASE_URL}/api/report/morning/recent?limit=5&type=general",
            headers=_API_HEADERS, timeout=5)
        if resp.ok:
            recent = resp.json()
            overview_parts = []
            for r in recent:
                script = r.get('audio_script') or r.get('summary') or ''
                if script and len(script) > 20:
                    date_str = str(r.get('created_at', 'Unknown'))[:10]
                    overview_parts.append(f"### {date_str}\n{script}")
            if overview_parts:
                past_overviews_text = "\n\n".join(overview_parts)
                log.info("Injecting %d past overview scripts (%d chars)",
                         len(overview_parts), len(past_overviews_text))
    except Exception as e:
        log.debug("Failed to fetch recent overviews: %s", e)
    return past_overviews_text


def _fetch_admin_report_assets():
    """The operator's chosen report assets (they steer the spotlight pick), or None."""
    admin_report_assets = None
    try:
        resp = requests.get(
            f"{DASHBOARD_BASE_URL}/api/user/preferences",
            headers=_API_HEADERS, timeout=5)
        if resp.ok:
            prefs = resp.json()
            ra = prefs.get("report_assets", [])
            if ra:
                admin_report_assets = [a["asset"] if isinstance(a, dict) else a for a in ra]
    except Exception as e:
        log.debug("Failed to fetch admin report_assets: %s", e)
    return admin_report_assets


def _morning_build_briefing(today, inputs, signals, top5_text, macro_text, asset_priority):
    """Phase C, first half: the system prompt and the briefing the shared
    report is written from. The shared parts are reused unchanged by the
    free-tier general report and the per-user reports."""
    steering = _load_steering_yaml()
    patterns = _load_patterns_yaml()
    from carrac.grounding import grounded_system_prompt as _gsp_shared
    system_prompt = _gsp_shared(shared_report_system_prompt(steering, patterns))

    past_overviews_text = _fetch_past_overviews()

    shared_briefing_parts = _build_shared_briefing_parts(
        today, signals.regime, top5_text, signals.summary,
        signals.conflicts, macro_text, inputs.context, signals.data,
        inputs.quant_context, inputs.chart_data, inputs.options_data,
        past_overviews_text, asset_priority)

    # Permitted positioning block and market breadth, appended without
    # changing the shared builder's signature.
    permitted_deriv = inputs.permitted_deriv
    if permitted_deriv:
        shared_briefing_parts = list(shared_briefing_parts) + [
            f"\n{permitted_deriv}\n"
        ]
    morning_breadth = fetch_market_breadth()
    if morning_breadth:
        shared_briefing_parts = list(shared_briefing_parts) + [
            f"\n## Market Breadth\n{morning_breadth}\n"
        ]

    # Spotlight assets get narrative depth; the rest get one compact line.
    from .signal_extractor import select_spotlight_assets
    spotlight_symbols, summary_symbols = select_spotlight_assets(
        signals.data.get("per_asset", {}),
        user_report_assets=_fetch_admin_report_assets())

    # range_pctile_map is the grounded 90-day range percentile per asset —
    # threaded into _generate_general_report so its post-publish tripwire can
    # check the generated Market Snapshot Range column against it.
    from .data_assembly import format_compact_asset_lines
    compact_summary_text, range_pctile_map = format_compact_asset_lines(
        inputs.chart_raw, summary_symbols)

    context = inputs.context
    briefing_parts = list(shared_briefing_parts) + [
        f"## SPOTLIGHT ASSETS: {', '.join(spotlight_symbols)}\n"
        f"These assets get narrative depth in Asset Spotlight. All others appear in Market Snapshot table only.\n",
        f"\n## Non-Spotlight Asset Summary\n{compact_summary_text}\n" if compact_summary_text else "",
        # The shared report is written in the operator's context.
        f"## Alerts\n{context.get('alerts', 'None')}\n",
        f"## Positions\n{context.get('positions', 'None')}\n",
        f"## Yesterday\n{context.get('yesterday_report', 'No previous report')}\n",
        inputs.continuity_block if inputs.continuity_block else "",
    ]
    return _MorningBriefing(
        steering=steering, patterns=patterns, system_prompt=system_prompt,
        shared_parts=shared_briefing_parts, spotlight_symbols=spotlight_symbols,
        compact_summary_text=compact_summary_text, range_pctile_map=range_pctile_map,
        text="\n".join(p for p in briefing_parts if p),
    )


def _morning_dry_run(work_dir, briefing, signals, phases):
    """Write the briefing and signals to work_dir instead of calling the model."""
    log.info("DRY RUN — briefing size: %d chars, 0 images, regime: %s",
             len(briefing.text), signals.regime)
    with open(os.path.join(work_dir, "briefing.txt"), "w") as f:
        f.write(briefing.text)
    with open(os.path.join(work_dir, "signals.json"), "w") as f:
        json.dump(signals.data.get("per_asset", {}), f, indent=2, default=str)
    log.info("Briefing saved to %s/briefing.txt", work_dir)
    log.info("Signals saved to %s/signals.json", work_dir)
    log.info("System prompt: %d chars", len(briefing.system_prompt))
    log.info("Phase results: %s", json.dumps(phases.results, indent=2))


def _morning_write_shared_report(opus, briefing, t_report, phases):
    """Phase C, second half: the shared report itself. Returns None (after
    paging the operator) when every attempt failed."""
    try:
        report_result = _call_with_retry(
            opus.chat,
            system_prompt=briefing.system_prompt,
            user_message=f"Here is today's market data and pre-processed signals. Generate the full morning analysis report.\n\n{briefing.text}",
            images=None,
            max_tokens=MAX_REPORT_TOKENS,
            temperature=0.7,
            timeout=600,
            max_attempts=3,
            label="shared_report",
        )
        # Enforce hard char limit — prompt declares 14K, runtime backs it.
        shared_report = _enforce_char_limit(
            report_result["content"], 14000,
            log_callback=lambda m: log.warning("shared_report: %s", m),
        )
    except Exception as e:
        log.error("Report generation failed after 3 attempts", exc_info=True)
        _notify("Morning Report FAILED", f"3 attempts exhausted: {e}")
        return None

    report_ms = int((time.time() - t_report) * 1000)
    log.info("Shared report: %d chars, %.1fs", len(shared_report), report_ms / 1000)
    phases.record("phase_c", "Shared Report Generation", "ok",
                  datetime.fromtimestamp(t_report).strftime("%H:%M:%S"), report_ms,
                  detail=f"{len(shared_report)} chars")
    return shared_report


def _morning_dedup_pass(sonnet, system_prompt, shared_report):
    """Phase C.2: intra-report duplication check with one corrective retry.
    Non-fatal. Returns (report, intra_stats); intra_stats is None when the
    check itself failed."""
    _morning_intra = None
    try:
        from .dedup import check_intra_report as _check_intra, build_dedup_correction_prompt as _bdc
        _morning_intra = _check_intra(shared_report)
        log.info(
            "morning_report dedup: dup_pairs=%d dup_facts=%d exceeds=%s",
            _morning_intra['dup_pair_count'], _morning_intra['dup_fact_count'],
            _morning_intra['exceeds_threshold'],
        )
        if _morning_intra['exceeds_threshold']:
            log.warning("morning_report dedup thresholds exceeded — one corrective retry")
            try:
                _correction_msg = _bdc(shared_report, _morning_intra)
                _retry_res = sonnet.chat(
                    system_prompt=system_prompt,
                    user_message=_correction_msg,
                    max_tokens=MAX_REPORT_TOKENS,
                    temperature=0.5,
                    timeout=300,
                )
                _retry_content = _enforce_char_limit(
                    _retry_res["content"], 14000,
                    log_callback=lambda m: log.warning("morning_dedup_retry: %s", m),
                )
                _post_intra = _check_intra(_retry_content)
                log.info(
                    "morning_report post-retry dedup: dup_pairs=%d dup_facts=%d",
                    _post_intra['dup_pair_count'], _post_intra['dup_fact_count'],
                )
                shared_report = _retry_content
            except Exception as _dr:
                log.warning("morning_report dedup retry failed (non-fatal): %s", _dr)
    except Exception as _de:
        log.warning("morning_report dedup check failed (non-fatal): %s", _de)
    return shared_report, _morning_intra


def _shared_report_tripwires(shared_report):
    """The log-and-alert content checks every shared report gets."""
    _calendar_tripwire(shared_report, "shared report")
    _advice_tripwire(shared_report, "shared report")
    # The prompt leads with unscheduled events, so the news check matters
    # here as much as anywhere.
    _news_tripwire(shared_report, "shared report")
    _data_release_tripwire(shared_report, "shared report")
    _leadership_tripwire(shared_report, "shared report")


def _morning_critic(sonnet, shared_report, phases):
    """Phase C.5: optional fact-check pass over the shared report, plus the
    training-leak scan over both the critic's verdict and the report.
    Alerts on a HIGH finding but never blocks. Returns the critic's text,
    or None when the critic is off or failed."""
    from .config import ENABLE_CRITIC
    critic_result_text = None
    if not (ENABLE_CRITIC and shared_report):
        return critic_result_text
    log.info("Phase C.5: Report critic (with ground-truth cross-check)...")
    t_critic = time.time()
    try:
        from .prompts import critic_prompt
        from carrac.grounding import (
            facts_block as _fb, grounded_system_prompt as _gsp_crit,
            scan as _crit_scan, build_facts_dict as _crit_facts,
        )
        _facts_for_critic = _crit_facts(include_prices=True)
        _fb_text_crit = _fb(include_prices=True, facts=_facts_for_critic)
        critic_response = sonnet.chat(
            system_prompt=_gsp_crit(critic_prompt(), facts_block_text=_fb_text_crit),
            user_message=(
                f"FACTS BLOCK (ground truth):\n{_fb_text_crit}\n\n"
                f"REPORT UNDER REVIEW:\n\n{shared_report[:30000]}"
            ),
            max_tokens=2000,
            temperature=0.2,
        )
        critic_result_text = critic_response["content"]
        critic_ms = int((time.time() - t_critic) * 1000)
        # Scan the critic's conclusion too, in case the critic itself
        # hallucinates, and the report, for anything the critic missed.
        _findings_crit = _crit_scan(critic_result_text, _facts_for_critic)
        _findings_report = _crit_scan(shared_report, _facts_for_critic)
        _all_highs = [f for f in _findings_crit + _findings_report if f.severity.value == "HIGH"]
        log.info("Critic: %d chars, %.1fs, tripwire highs=%d", len(critic_result_text), critic_ms / 1000, len(_all_highs))
        if _all_highs:
            log.error("SHARED REPORT tripwire HIGH findings: %s",
                      [(f.pattern_name, f.match_text) for f in _all_highs[:5]])
            try:
                _notify("Shared Report HIGH tripwire hit",
                        f"Report text contains training-era leak patterns: "
                        f"{_all_highs[0].pattern_name} — {_all_highs[0].match_text!r}. "
                        f"Report NOT blocked (hard block on reports would leave users with nothing); "
                        f"review ASAP.")
            except Exception as _ne:
                log.warning("Tripwire notify failed (non-fatal): %s", _ne)
        phases.record("phase_c5", "Report Critic", "ok",
                      datetime.fromtimestamp(t_critic).strftime("%H:%M:%S"),
                      critic_ms, detail=f"{len(critic_result_text)} chars, high={len(_all_highs)}")
    except Exception as e:
        log.warning("Critic failed (non-fatal): %s", e)
        critic_ms = int((time.time() - t_critic) * 1000)
        phases.record("phase_c5", "Report Critic", "error",
                      datetime.fromtimestamp(t_critic).strftime("%H:%M:%S"),
                      critic_ms, error=str(e))
    return critic_result_text


@dataclass
class _PersonalContext:
    """What every per-user worker in the legacy Phase E reads. Shared and
    read-only across the worker threads."""
    report_id: object
    shared_report: str
    market_regime: str
    top5_text: str
    signal_summary: str
    conflict_text: str
    context: dict
    signal_data: dict
    chart_raw: dict
    user_sources: dict
    sonnet: object
    grok: object


def _personal_credits_exhausted(uid, user_tier, report_id):
    """True when a paying user cannot cover a personal section (and a marker
    row has been saved in its place). A failed balance check proceeds."""
    if user_tier in ("admin", "tester"):
        return False
    try:
        cr_resp = requests.get(
            f"{DASHBOARD_BASE_URL}/api/credits/check",
            params={"user_id": uid},
            headers=_API_HEADERS, timeout=5)
        if cr_resp.ok:
            balance = cr_resp.json().get("balance_cents", 0)
            if balance < 5:  # ~5 cents estimated personal section cost
                log.info("User %s insufficient credits (%d cents), skipping personal", uid, balance)
                if report_id:
                    _save_personal_section(report_id, uid, "<!-- CREDITS_EXHAUSTED -->")
                return True
    except Exception as e:
        log.warning("Credit check failed for user %s, proceeding: %s", uid, e)
    return False


def _personal_plays_block(uid):
    """The user's active plays as a prompt block, or '' when there are none.
    Fetched per user: plays carry private trading theses."""
    try:
        from carrac.plays.models import play_get_digest as _get_plays_ctx
        user_plays = _get_plays_ctx(uid)
        plays_text = user_plays.get('summary', '')
        if user_plays.get('plays'):
            plays_lines = [f"## Active Plays ({len(user_plays['plays'])})"]
            for p in user_plays['plays']:
                # Title is user-controlled; escape inline so the whole
                # block can later be wrapped as a single user_plays blob.
                safe_title = _escape_user_text(p.get('title', ''), 'play_title', max_chars=200)
                plays_lines.append(
                    f"- [{p.get('status', '')}] {safe_title} ({p.get('primary_asset', '')} {p.get('direction', '')})"
                )
                if p.get('thesis'):
                    plays_lines.append(
                        f"  Thesis: {_escape_user_text(p['thesis'][:200], 'play_thesis', max_chars=200)}"
                    )
            plays_text = "\n".join(plays_lines)
    except Exception as e:
        log.debug("Per-user plays fetch failed for %s: %s", uid, e)
        plays_text = ''
    if plays_text and plays_text.strip() and plays_text != 'No active plays.':
        return f"{plays_text}\n"
    return ""


def _personal_indicator_focus(user_report_indicators, signal_data):
    """The current state of the indicators this user follows, or ''."""
    if not (user_report_indicators and signal_data.get("per_asset")):
        return ""
    ind_names = [i for i in user_report_indicators if i in INDICATOR_SIGNAL_MAP]
    if not ind_names:
        return ""
    focus_lines = ["## Indicator Focus"]
    focus_lines.append(f"This user monitors: {', '.join(ind_names)}")
    focus_lines.append("Analyze positions and plays through these indicators.\n")
    focus_lines.append("Current states:")
    per_asset = signal_data["per_asset"]
    for asset_key in sorted(per_asset.keys()):
        for tf_key in sorted(per_asset[asset_key].keys()):
            sigs = per_asset[asset_key][tf_key]
            relevant = {}
            for ind_id in ind_names:
                for sig_key in INDICATOR_SIGNAL_MAP.get(ind_id, []):
                    val = sigs.get(sig_key)
                    if val is not None and val != "none" and val != "normal" and val != "neutral" and val != "flat":
                        relevant[sig_key] = val
            if relevant:
                parts = [f"{k}={v}" for k, v in relevant.items()]
                focus_lines.append(f"  {asset_key} {tf_key}: {', '.join(parts)}")
    return "\n".join(focus_lines) + "\n"


def _notable_signal_line(asset, tf, sigs):
    """'  ASSET TF: k=v, ...' for the non-neutral signals (first 15), or None."""
    notable = {k: v for k, v in sigs.items()
               if k not in ("asset", "tf", "status", "price") and v is not None
               and v != "none" and v != "normal" and v != "neutral" and v != "flat"}
    if not notable:
        return None
    parts = [f"{k}={v}" for k, v in list(notable.items())[:15]]
    return f"  {asset} {tf}: {', '.join(parts)}"


def _personal_asset_focus(uid, user_report_assets, chart_raw, signal_data):
    """Signal states for the assets this user tracks, or ''. Pairs missing
    from the shared chart data are fetched on demand (at most ten)."""
    if not user_report_assets:
        return ""
    from .data_assembly import _api
    from .signal_extractor import extract_all_signals as _extract_signals
    MAX_USER_FETCHES = 10
    fetched = 0
    asset_focus_lines = ["## Asset Focus"]
    asset_names = [item.get("asset", "") for item in user_report_assets if item.get("asset")]
    asset_focus_lines.append(f"This user specifically tracks: {', '.join(asset_names)}")
    asset_focus_lines.append("Analyze these assets in depth when discussing positions and plays.\n")
    asset_focus_lines.append("Current states:")
    for item in user_report_assets:
        asset = item.get("asset", "").upper()
        tfs = item.get("timeframes", [])
        if not asset or not tfs:
            continue
        for tf in tfs:
            if (asset, tf) in chart_raw:
                if signal_data.get("per_asset", {}).get(asset, {}).get(tf):
                    line = _notable_signal_line(asset, tf, signal_data["per_asset"][asset][tf])
                    if line:
                        asset_focus_lines.append(line)
            elif fetched < MAX_USER_FETCHES:
                try:
                    data = _api(f"/api/chart-internal/{asset}?timeframe={tf}", timeout=10)
                    if data and "ohlcv" in data and not data.get("error"):
                        fetched += 1
                        extra_signals = _extract_signals({(asset, tf): data})
                        extra_per_asset = extra_signals.get("per_asset", {}).get(asset, {}).get(tf, {})
                        line = _notable_signal_line(asset, tf, extra_per_asset)
                        if line:
                            asset_focus_lines.append(line)
                except Exception as e:
                    log.debug("Asset focus fetch failed for %s/%s: %s", asset, tf, e)
    if len(asset_focus_lines) <= 4:  # headers only, no signal data
        return ""
    log.info("Asset focus for user %s: %d assets, %d on-demand fetches",
             uid, len(user_report_assets), fetched)
    return "\n".join(asset_focus_lines) + "\n"


def _personal_user_context(uid, user_tier, user_sources, ctx):
    """The full per-user prompt context for one personal section."""
    context = ctx.context
    user_context_parts = [
        f"## Risk Posture: {ctx.market_regime.upper()}\n",
        f"## Today's Top-5 Market Drivers\n{ctx.top5_text}\n" if ctx.top5_text else "",
        f"## Signal Summary\n```json\n{ctx.signal_summary}\n```\n",
        f"## Signal Conflicts\n{ctx.conflict_text}\n" if ctx.conflict_text else "",
        f"## Shared Report Summary\n{ctx.shared_report[:3000]}\n",
    ]
    trader_profile_text = user_sources.get("trader_profile", "")
    if trader_profile_text:
        user_context_parts.append(
            f"## Trader Profile\n{_escape_user_text(trader_profile_text, 'trader_profile')}\n"
        )
    # alerts/positions are per-user (the operator keeps the global view) —
    # see _user_context_sections.
    _pa_alerts_text, _pa_positions_text = _user_context_sections(
        uid, user_tier, context)
    user_context_parts.extend([
        f"## Alerts\n{_escape_user_text(_pa_alerts_text, 'user_alerts', max_chars=8000)}\n",
        f"## Positions\n{_escape_user_text(_pa_positions_text, 'user_positions', max_chars=8000)}\n",
        f"## Signal Changes\n{context.get('signal_diff', 'None')}\n",
        f"## Yesterday\n{_escape_user_text(context.get('yesterday_report', 'No previous report'), 'previous_report', max_chars=12000)}\n",
    ])
    for block in (
        _personal_plays_block(uid),
        _personal_indicator_focus(user_sources.get("report_indicators", []), ctx.signal_data),
        _personal_asset_focus(uid, user_sources.get("report_assets", []), ctx.chart_raw, ctx.signal_data),
    ):
        if block:
            user_context_parts.append(block)
    return "\n".join(user_context_parts)


def _personal_system_prompt(user_tier, user_context, mr_prefs):
    """(grounded system prompt, max tokens) for the user's tier: members get
    the shorter standard prompt, premium/admin the full one."""
    if user_tier in ("premium", "admin"):
        da_intensity = mr_prefs.get('devilsAdvocate', 'medium')
        sys_prompt = personal_section_prompt(user_context, devils_advocate_intensity=da_intensity)
        max_tok = MAX_PERSONAL_TOKENS
    else:
        sys_prompt = standard_personal_prompt(user_context)
        max_tok = min(MAX_PERSONAL_TOKENS, 4000)
    from carrac.grounding import grounded_system_prompt as _gsp_ps
    return _gsp_ps(sys_prompt), max_tok


def _morning_personal_section(user, ctx):
    """Write one user's personal section (runs on a worker thread).

    Returns the section's executive summary when the user is the operator
    (it becomes the report's summary field), else None.
    """
    uid = user["id"]
    user_tier = user["tier"]
    log.info("Generating personal section for user %s (tier=%s)", uid, user_tier)

    if _personal_credits_exhausted(uid, user_tier, ctx.report_id):
        return None

    user_sources = ctx.user_sources[uid]
    user_context = _personal_user_context(uid, user_tier, user_sources, ctx)
    mr_prefs = user_sources.get('morning_report_prefs', {})
    sys_prompt, max_tok = _personal_system_prompt(user_tier, user_context, mr_prefs)

    personal_content, personal_audio_script = _write_personal_section(
        uid=uid, user_tier=user_tier, sys_prompt=sys_prompt, max_tok=max_tok,
        sonnet=ctx.sonnet, grok=ctx.grok, report_id=ctx.report_id,
        shared_report=ctx.shared_report, mr_prefs=mr_prefs,
    )
    if not personal_content:
        return None

    admin_summary = _extract_exec_summary(personal_content) if user_tier == "admin" else None
    log.info("Personal section for user %s: %d chars%s", uid, len(personal_content),
             " + audio" if personal_audio_script else "")
    return admin_summary




def _signal_conflict_text(signal_conflicts):
    """Today's signal conflicts as a prompt block, or '' when there are none."""
    if not signal_conflicts:
        return ""
    conflict_lines = ["Conflicts in today's signals:"]
    for c in signal_conflicts:
        conflict_lines.append(f"- {c['pair'][0]} vs {c['pair'][1]}: {c['implication']}")
    return "\n".join(conflict_lines)


def _run_personal_workers(eligible_users, ctx):
    """Personal sections for every eligible user, three at a time (API rate
    limits). A worker that raises is logged and skipped. Returns the
    operator's executive summary, or None."""
    admin_summary = None
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_morning_personal_section, u, ctx): u for u in eligible_users}
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                uid = futures[f]["id"]
                log.error("Personal section thread failed for user %s: %s", uid, exc, exc_info=exc)
            elif admin_summary is None:
                admin_summary = f.result()
    return admin_summary


def _morning_personal_phase(report_id, shared_report, inputs, signals, top5_text,
                            briefing, sonnet, grok, phases):
    """Phase E: per-user personalized reports when PERSONALIZED_REPORTS is on,
    otherwise the legacy personal sections appended to the shared report.

    Returns (number of legacy personal sections attempted, the operator's
    executive summary or None).
    """
    from .config import PERSONALIZED_REPORTS
    ran_personalized = False
    if PERSONALIZED_REPORTS and report_id:
        log.info("Phase E: Per-user personalized reports (PERSONALIZED_REPORTS=true)...")
        t_personal = time.time()
        _run_personalized_reports(
            report_id, briefing.shared_parts, inputs.chart_raw, signals.data,
            inputs.context, signals.conflicts, briefing.steering, briefing.patterns,
            sonnet, grok, phases.record,
            quant_context=inputs.quant_context,
        )
        personal_ms = int((time.time() - t_personal) * 1000)
        phases.record("phase_e", "Personalized Reports", "ok",
                      datetime.fromtimestamp(t_personal).strftime("%H:%M:%S"),
                      personal_ms)
        ran_personalized = True
    else:
        log.info("Phase E: Personal sections (legacy)...")
        t_personal = time.time()

    # After personalized reports the legacy pass still runs, over no users.
    eligible_users = [] if ran_personalized else _get_eligible_users()
    log.info("Eligible users for personal sections: %d", len(eligible_users))

    user_sources = {}
    for user in eligible_users:
        user_sources[user["id"]] = _get_user_analyst_sources(user["id"])

    ctx = _PersonalContext(
        report_id=report_id, shared_report=shared_report,
        market_regime=signals.regime, top5_text=top5_text,
        signal_summary=signals.summary,
        conflict_text=_signal_conflict_text(signals.conflicts),
        context=inputs.context, signal_data=signals.data,
        chart_raw=inputs.chart_raw, user_sources=user_sources,
        sonnet=sonnet, grok=grok,
    )
    admin_summary = _run_personal_workers(eligible_users, ctx)

    personal_ms = int((time.time() - t_personal) * 1000)
    log.info("Personal sections: %d users, %.1fs", len(eligible_users), personal_ms / 1000)
    phases.record("phase_e", "Personal Sections", "ok",
                  datetime.fromtimestamp(t_personal).strftime("%H:%M:%S"), personal_ms,
                  detail=f"{len(eligible_users)} users")
    return len(eligible_users), admin_summary


def _audio_briefing_input(shared_report):
    """The source text the public audio briefing is written from.

    Tries the narrative-redesign sections first, then the legacy section
    names, then the whole report. Raises when the report is too short to
    brief from at all.
    """
    story = _extract_section(shared_report, "The Story") or ""
    action_dashboard = _extract_section(shared_report, "Action Dashboard") or ""
    regime = _extract_section(shared_report, "Regime & Positioning") or ""
    scenarios = _extract_section(shared_report, "Scenarios") or ""

    macro_section = (_extract_section(shared_report, "Macro Context")
                     or _extract_section(shared_report, "Macro")
                     or story)
    overview = (_extract_section(shared_report, "Overview")
                or regime
                or scenarios)
    shared_action_items = (_extract_section(shared_report, "Action Items")
                           or action_dashboard)

    if not (macro_section or overview or shared_action_items):
        if not shared_report or len(shared_report) < 500:
            log.warning("Audio briefing: shared_report missing or too short (%d chars); skipping",
                        len(shared_report or ""))
            raise RuntimeError("audio_briefing_skipped_no_source")
        log.warning("Audio briefing: no known sections matched; feeding full report as single block")
        return f"Source report:\n\n{shared_report[:5000]}"
    parts = []
    if macro_section:
        parts.append(f"Macro context (prioritize this):\n{macro_section[:2000]}")
    if overview:
        parts.append(f"Report overview:\n{overview[:2000]}")
    if shared_action_items:
        parts.append(f"Action Items:\n{shared_action_items[:1000]}")
    return "\n\n".join(parts)


def _morning_audio_briefing(sonnet, grok, shared_report, report_id, phases):
    """Phase F: the public spoken briefing, written from the shared report only
    (no personal data).

    Written on Sonnet under the grounding preamble + facts block. A HIGH
    hallucination finding blocks the audio: the key points are kept as text
    when they are clean, and the operator is paged. Grok is used only for
    the TTS bytes.

    Returns (audio_url, display_script, spoken_script); each is None when
    that step did not happen.
    """
    audio_url = None
    audio_script = None
    spoken_script = None
    if not (report_id and TTS_ENABLED):
        return audio_url, audio_script, spoken_script
    log.info("Phase F: Conversational audio briefing (public)...")
    t_tts = time.time()
    try:
        from .prompts import audio_briefing_prompt
        from carrac.grounding import facts_block, grounded_system_prompt, scan, has_high, build_facts_dict

        audio_input = _audio_briefing_input(shared_report)

        # Build facts once per phase — shared by system prompt + scan
        facts = build_facts_dict(include_prices=True)
        fb_text = facts_block(include_prices=True, facts=facts)
        grounded_prompt = grounded_system_prompt(audio_briefing_prompt(), facts_block_text=fb_text)

        script_result = sonnet.chat(
            system_prompt=grounded_prompt,
            user_message=f"Generate a spoken audio briefing based on today's source report:\n\n{audio_input}",
            max_tokens=2000,
            temperature=0.6,
        )
        raw_output = script_result["content"]
        log.info("Audio raw output: %d chars, $%.4f", len(raw_output), script_result["usage"]["cost"])

        # Dual output: KEY_POINTS + SPOKEN_SCRIPT
        separator = "---SPOKEN_SCRIPT---"
        if separator in raw_output:
            parts = raw_output.split(separator, 1)
            display_script = _strip_section_header(parts[0].strip(), "display")
            spoken_script = _strip_section_header(parts[1].strip(), "spoken")
            log.info("Parsed dual output: display=%d chars, spoken=%d chars",
                     len(display_script), len(spoken_script))
        else:
            log.warning("Audio script missing separator, using full output for both")
            display_script = raw_output
            spoken_script = raw_output

        # Hallucination tripwire — HIGH severity blocks publish
        scan_target = (display_script or "") + "\n" + (spoken_script or "")
        findings = scan(scan_target, facts)
        _advice_tripwire(scan_target, "shared audio briefing")
        if has_high(findings):
            high_matches = [f.to_dict() for f in findings if f.severity.value == "HIGH"]
            log.error("Audio briefing BLOCKED — hallucination findings: %s", high_matches)
            try:
                _notify("Audio Briefing BLOCKED",
                        f"Report #{report_id} audio dropped by tripwire. "
                        f"First finding: {high_matches[0]['pattern']} — {high_matches[0]['match']!r}")
            except Exception as _ne:
                log.warning("Audio-block notify failed (non-fatal): %s", _ne)
            # Text-only fallback: keep the key points if the finding is not in
            # them; otherwise drop everything so nothing misleading is shown.
            display_only = display_script if not any(
                f.severity.value == "HIGH" and f.span[0] < len(display_script) for f in findings
            ) else ""
            if display_only:
                _save_audio_script(report_id, display_only)
                audio_script = display_only
            tts_ms = int((time.time() - t_tts) * 1000)
            phases.record("phase_f", "Audio Briefing", "blocked",
                          datetime.fromtimestamp(t_tts).strftime("%H:%M:%S"), tts_ms,
                          detail=f"tripwire_hits={len(high_matches)}")
        else:
            for f in findings:
                log.info("Audio scan %s: %s — %s", f.severity.value, f.pattern_name, f.match_text)

            # The display version (key points) goes to the DB column
            audio_script = display_script

            # Safety: ensure spoken script fits TTS limit
            if len(spoken_script) > 4000:
                spoken_script = spoken_script[:4000].rsplit('.', 1)[0] + '.'

            _save_audio_script(report_id, display_script)

            audio_url = _generate_tts(grok, spoken_script, report_id)
            tts_ms = int((time.time() - t_tts) * 1000)
            phases.record("phase_f", "Audio Briefing", "ok",
                          datetime.fromtimestamp(t_tts).strftime("%H:%M:%S"), tts_ms,
                          detail=f"script={len(audio_script)} chars")
    except Exception as e:
        log.warning("Audio briefing generation failed: %s", e)
        tts_ms = int((time.time() - t_tts) * 1000)
        phases.record("phase_f", "Audio Briefing", "error",
                      datetime.fromtimestamp(t_tts).strftime("%H:%M:%S"), tts_ms,
                      error=str(e))
    return audio_url, audio_script, spoken_script


def _morning_usage(grok, opus, sonnet, elapsed_ms, phases):
    """Total tokens and cost across the three model clients; records the
    Finalize phase with the per-client split."""
    grok_usage = grok.get_usage_summary()
    opus_usage = opus.get_usage_summary()
    sonnet_usage = sonnet.get_usage_summary()
    usage = {
        "total_input_tokens": grok_usage["total_input_tokens"] + opus_usage["total_input_tokens"] + sonnet_usage["total_input_tokens"],
        "total_output_tokens": grok_usage["total_output_tokens"] + opus_usage["total_output_tokens"] + sonnet_usage["total_output_tokens"],
        "total_cost": grok_usage["total_cost"] + opus_usage["total_cost"] + sonnet_usage["total_cost"],
    }
    primary_label = "Grok-primary" if isinstance(opus, GrokProvider) else "Opus"
    phases.record("phase_g", "Finalize", "ok",
                  datetime.now().strftime("%H:%M:%S"), 0,
                  detail=f"Total {elapsed_ms}ms, ${usage['total_cost']:.4f} ({primary_label}: ${opus_usage['total_cost']:.4f}, Sonnet: ${sonnet_usage['total_cost']:.4f}, Grok: ${grok_usage['total_cost']:.4f})")
    return usage


def _report_prices(chart_raw):
    """Last daily close per asset at report time, for the live delta header."""
    report_prices = {}
    if chart_raw:
        for (asset, tf), data in chart_raw.items():
            if tf == "1D":
                ohlcv = data.get("ohlcv", [])
                if ohlcv:
                    report_prices[asset] = ohlcv[-1].get("close")
    return report_prices


def _dedup_metadata(intra):
    if intra is None:
        return None
    return {
        "dup_pair_count": intra.get("dup_pair_count", 0) if intra else None,
        "dup_fact_count": intra.get("dup_fact_count", 0) if intra else None,
        "intra_exceeded": intra.get("exceeds_threshold", False) if intra else None,
    }


def _morning_ready_message(report_id, total_elapsed, usage, shared_report,
                           n_personal, audio_url, audio_script):
    """The operator notification body: totals plus the first key points."""
    msg = (f"Report #{report_id} in {total_elapsed:.0f}s (${usage['total_cost']:.3f})\n"
           f"{len(shared_report)} chars shared | {n_personal} personal sections")
    if audio_url:
        msg += "\nVoice briefing attached"
    if audio_script:
        bullets = [line.strip().lstrip('•·- ') for line in audio_script.split('\n')
                   if line.strip() and len(line.strip()) > 15][:3]
        if bullets:
            msg += "\n\n" + "\n".join(f"• {b}" for b in bullets)
    return msg


def run_morning(dry_run=False):
    """Run the full morning report generation.

    A: fetch inputs · A.5: extract signals · B: macro research + top-5
    drivers · C: write the shared report, dedup it, check it · D: save it and
    the free-tier general report · E: personal sections · F: public audio
    briefing · G: metadata, summary and the operator notification.

    Returns the shared report's id, or None when the run aborted, was a dry
    run, or the shared report could not be written.
    """
    t_start = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    log.info("=== Morning Report — %s ===", today)

    grok = GrokClient()          # Retained for x_search only
    # Sonnet by default; MORNING_PRIMARY_MODEL=opus|grok overrides. The name
    # `opus` is historical.
    opus = _make_primary_client()
    sonnet = MorningAnthropicClient(model=ANTHROPIC_MODEL_FAST)  # Sonnet for top-5 + personal (cost)
    work_dir = tempfile.mkdtemp(prefix=f"morning-{today}-")
    phases = _PhaseLog()

    inputs = _morning_assemble_data(phases)
    if not inputs.chart_raw:
        _abort_morning(phases, "No chart data available — cannot generate report",
                       error="No chart data", detail="chart_pair_count == 0")
        return None

    signals = _morning_extract_signals(inputs.chart_raw, inputs.context,
                                       inputs.data_sources, phases)

    macro_text, top5_text, asset_priority, macro_ok = _morning_research(
        grok, sonnet, signals.summary, inputs.data_sources, phases)
    if not macro_ok:
        # Without macro research the report would lack essential context.
        _abort_morning(phases, "Macro research failed after 5 retries — aborting to avoid degraded report",
                       error="Macro research failed", detail="5 retries exhausted")
        return None

    log.info("Phase C: Shared report generation...")
    t_report = time.time()
    briefing = _morning_build_briefing(today, inputs, signals, top5_text,
                                       macro_text, asset_priority)
    if dry_run:
        _morning_dry_run(work_dir, briefing, signals, phases)
        return None

    shared_report = _morning_write_shared_report(opus, briefing, t_report, phases)
    if shared_report is None:
        return None
    shared_report, dedup_stats = _morning_dedup_pass(sonnet, briefing.system_prompt, shared_report)
    _shared_report_tripwires(shared_report)
    critic_result_text = _morning_critic(sonnet, shared_report, phases)

    # Phase D: save the shared report, then the free-tier general report
    # (operator-neutral, served to all free accounts) from the same briefing.
    # morning_report lets its dedup check detect cross-report overlap.
    quant_context = inputs.quant_context
    report_id = _post_report(
        content=shared_report,
        report_type="morning",
        assets=list(set(p.split("/")[0] for p in from_chart_pairs())),
        generation_time_ms=int((time.time() - t_start) * 1000),
        source_briefing=briefing.text,
    )
    _generate_general_report(
        briefing.shared_parts, briefing.spotlight_symbols, briefing.compact_summary_text,
        briefing.steering, briefing.patterns, sonnet, t_start,
        morning_report=shared_report,
        continuity_block=inputs.continuity_block,
        scenario_levels=(quant_context or {}).get('confluence_levels'),
        range_pctile_map=briefing.range_pctile_map,
        options_claims=inputs.options_claims,
    )

    n_personal, admin_summary = _morning_personal_phase(
        report_id, shared_report, inputs, signals, top5_text, briefing,
        sonnet, grok, phases)

    audio_url, audio_script, spoken_script = _morning_audio_briefing(
        sonnet, grok, shared_report, report_id, phases)

    # Phase G: finalize
    elapsed_ms = int((time.time() - t_start) * 1000)
    usage = _morning_usage(grok, opus, sonnet, elapsed_ms, phases)
    metadata = {
        "model": f"opus:{opus.model}+sonnet:{sonnet.model}+grok:{grok.model}",
        "cost": usage["total_cost"],
        "input_tokens": usage["total_input_tokens"],
        "output_tokens": usage["total_output_tokens"],
        "generation_time_ms": elapsed_ms,
        "mode": "morning",
        "engine_version": "2.6",
        "asset_priority": asset_priority,
        "personal_sections": n_personal,
        "tts_generated": audio_url is not None,
        "data_sources": inputs.data_sources,
        "phase_results": phases.results,
        "critic_review": critic_result_text,
        "audio_script": spoken_script or audio_script,
        "display_script": audio_script,
        "report_prices": _report_prices(inputs.chart_raw),
        "report_timestamp": datetime.now(timezone.utc).isoformat(),
        "dedup": _dedup_metadata(dedup_stats),
    }
    if report_id:
        # Prefer the operator's personal Executive Summary; fall back to the
        # shared report's TL;DR so the summary column is never NULL when
        # content exists.
        summary_to_save = admin_summary or _extract_tldr_summary(shared_report)
        if summary_to_save:
            _save_summary(report_id, summary_to_save)
        _save_metadata(report_id, metadata)

    total_elapsed = time.time() - t_start
    log.info("=== Morning Report Complete ===")
    log.info("Total: %.1fs | Cost: $%.4f | Report: %d chars | Users: %d | ID: %s | TTS: %s",
             total_elapsed, usage["total_cost"], len(shared_report),
             n_personal, report_id, "yes" if audio_url else "no")
    _notify("Morning Report Ready",
            _morning_ready_message(report_id, total_elapsed, usage, shared_report,
                                   n_personal, audio_url, audio_script),
            audio_url=audio_url)
    _log_engine_costs("morning", opus=opus, sonnet=sonnet, grok=grok)
    return report_id


def from_chart_pairs():
    """Get asset list from config."""
    from .config import CHART_PAIRS
    return CHART_PAIRS


def run_dispatch(region, dry_run=False):
    """Run personalized report dispatch for a specific continental region.

    Called per-region by cron (5 batches/day). First batch of the day runs the
    full shared data pipeline and caches results in Redis db=1. Subsequent
    batches load from cache and only generate per-user personalized reports.

    Regions: europe, us_east, us_west, asia_pacific, asia.
    """
    VALID_REGIONS = {'europe', 'us_east', 'us_west', 'asia_pacific', 'asia'}
    if region not in VALID_REGIONS:
        log.error("Invalid region: %s (valid: %s)", region, ', '.join(sorted(VALID_REGIONS)))
        return

    t_start = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    log.info("=== Dispatch %s — %s ===", region, today)

    # Get eligible users for this region
    eligible_users = _get_eligible_users(region=region)
    log.info("Region %s: %d eligible users", region, len(eligible_users))

    if not eligible_users:
        # Normally we skip a region with no eligible users entirely. With
        # GENERAL_REPORT_UNGATED on (beta), still run the shared pipeline so the
        # operator-neutral free-tier "general" report is produced even when no
        # region has paid/eligible users — the per-user phase below then no-ops
        # on the empty list. Guarded so only the first run of the day does the
        # work; later empty regions skip once today's general report exists.
        if not GENERAL_REPORT_UNGATED:
            log.info("No eligible users for region %s, skipping", region)
            return
        if _general_report_exists_today():
            log.info("No eligible users for region %s; general report already "
                     "exists today, skipping", region)
            return
        log.info("No eligible users for region %s; GENERAL_REPORT_UNGATED on — "
                 "running shared pipeline to produce the free-tier general report "
                 "(per-user phase will no-op)", region)

    # Redis connection for shared data caching (db=1, separate from sessions).
    # Payloads are JSON-serialised (replacing the previous pickle) and wrapped
    # in an HMAC envelope so the stored value cannot be tampered with even if
    # Redis is compromised. JSON removes the pickle RCE surface entirely.
    import hashlib
    import hmac as _hmac
    import json as _json
    try:
        import redis as redis_lib
    except ImportError:
        redis_lib = None

    # Sign with Flask's SECRET_KEY; all workers share it. Fall back to a
    # process-random key if SECRET_KEY isn't set — in that case the cache
    # simply won't be usable by a different worker, but it stays correct.
    _sign_key = (os.environ.get('SECRET_KEY', '') or os.urandom(32).hex()).encode()
    _HMAC_LEN = 32  # SHA-256

    r = None
    if redis_lib:
        try:
            # Use REDIS_URL from env (the same URL the app uses), switch to db=1
            redis_url = os.environ.get('REDIS_URL', '')
            if not redis_url:
                raise ConnectionError("REDIS_URL not set")
            # Replace db number: /0 → /1 (separate from session cache)
            if redis_url.endswith('/0'):
                redis_url = redis_url[:-1] + '1'
            r = redis_lib.from_url(redis_url, socket_connect_timeout=3,
                                   decode_responses=False)
            r.ping()
            log.info("Redis connected (db=1) for dispatch cache")
        except Exception as e:
            log.warning("Redis unavailable, will run all phases fresh: %s", e)
            r = None

    CACHE_TTL = 10800  # 3 hours
    cache_prefix = f"morning:{today}"

    def _cache_get(key):
        if not r:
            return None
        try:
            raw = r.get(f"{cache_prefix}:{key}")
            if not raw or len(raw) <= _HMAC_LEN:
                return None
            stored_mac, payload = raw[:_HMAC_LEN], raw[_HMAC_LEN:]
            expected_mac = _hmac.new(_sign_key, payload, hashlib.sha256).digest()
            if not _hmac.compare_digest(stored_mac, expected_mac):
                log.warning("Cache get %s: HMAC mismatch — refusing deserialise", key)
                return None
            return _json.loads(payload.decode('utf-8'))
        except Exception as e:
            log.debug("Cache get %s: %s", key, e)
            return None

    def _cache_set(key, data):
        if not r:
            return
        try:
            payload = _json.dumps(data, default=str).encode('utf-8')
            mac = _hmac.new(_sign_key, payload, hashlib.sha256).digest()
            r.setex(f"{cache_prefix}:{key}", CACHE_TTL, mac + payload)
        except Exception as e:
            log.debug("Cache set %s: %s", key, e)

    # Phase tracking. Lock guards phase_results against concurrent writes
    # from the per-user ThreadPoolExecutor in Phase E of dispatch mode.
    phase_results = {}
    _phase_lock = threading.Lock()

    def _record_phase(key, name, status, started_at, duration_ms,
                      retries=0, error=None, detail=None):
        with _phase_lock:
            phase_results[key] = {
                "name": name, "status": status,
                "started_at": started_at, "duration_ms": duration_ms,
                "retries": retries, "error": error, "detail": detail,
            }

    # Initialize clients
    grok = GrokClient()
    opus = _make_primary_client()
    sonnet = MorningAnthropicClient(model=ANTHROPIC_MODEL_FAST)

    # ------------------------------------------------------------------
    # Try loading shared data from Redis cache
    # ------------------------------------------------------------------
    cached_report_id = _cache_get("report_id")
    cached_shared_briefing = _cache_get("shared_briefing_parts")
    cached_chart_raw = _cache_get("chart_raw")
    cached_signal_data = _cache_get("signal_data")
    cached_context = _cache_get("context")
    cached_signal_conflicts = _cache_get("signal_conflicts")

    all_cached = all([cached_report_id, cached_shared_briefing,
                      cached_chart_raw, cached_signal_data, cached_context])

    # continuity_block is computed in the first-batch path and reused across
    # subsequent cached batches (the data is the same within a run cycle).
    continuity_block = ""

    if all_cached:
        # ---- SUBSEQUENT BATCH — load from cache ----
        log.info("Shared data loaded from Redis cache — skipping shared phases")
        report_id = cached_report_id
        shared_briefing_parts = cached_shared_briefing
        chart_raw = cached_chart_raw
        signal_data = cached_signal_data
        context = cached_context
        signal_conflicts = cached_signal_conflicts or []
        steering = _load_steering_yaml()
        patterns = _load_patterns_yaml()
        # Rebuild continuity block from recent data even on cache hit — it's cheap
        # and ensures the Since Yesterday section is always present regardless of
        # whether this is the first or a subsequent dispatch batch.
        try:
            _cached_yday = fetch_yesterday_report(report_type='general')
            _cached_flips = fetch_signal_flips()
            _cached_quant = fetch_quant_context()
            _cached_yday_regime = fetch_yesterday_regime(_cached_quant)
            _cached_payload = format_since_yesterday_payload(
                signal_flips=_cached_flips,
                yesterday_regime=_cached_yday_regime,
                quant_context=_cached_quant,
                yesterday_report=_cached_yday,
                chart_raw=chart_raw,
            )
            continuity_block = format_since_yesterday_block(_cached_payload)
        except Exception as _ce:
            log.debug("Continuity block rebuild (cached path) failed: %s", _ce)
    else:
        # ---- FIRST BATCH — run full shared pipeline ----
        log.info("No cache hit — running full shared data pipeline")

        # Phase A: Data Assembly
        log.info("Phase A: Data assembly...")
        t_data = time.time()
        chart_data, chart_raw = fetch_chart_data()
        context = fetch_context_data()
        options_data, options_claims = fetch_options_data()
        # The permitted-data positioning block. Rides ALONGSIDE the
        # Deribit block rather than replacing the variable — the two are
        # different reads (positioning vs volatility) and the Deribit one is
        # still assembled for the operator. Its grounding claims are merged into
        # options_claims so the existing _grounded_field_tripwire calls cover
        # both without a second wiring point to forget.
        permitted_deriv, _permitted_deriv_claims = fetch_permitted_derivatives()
        options_claims = list(options_claims or []) + _permitted_deriv_claims
        quant_context = fetch_quant_context()
        # Tag fetch time for downstream freshness check (see _ensure_fresh_quant).
        quant_context["_fetched_at"] = time.time()

        # Daily continuity context (dispatch path)
        _dispatch_yday_report = fetch_yesterday_report(report_type='general')
        _dispatch_flips = fetch_signal_flips()
        _dispatch_yday_regime = fetch_yesterday_regime(quant_context)
        continuity_payload = format_since_yesterday_payload(
            signal_flips=_dispatch_flips,
            yesterday_regime=_dispatch_yday_regime,
            quant_context=quant_context,
            yesterday_report=_dispatch_yday_report,
            chart_raw=chart_raw,
        )
        continuity_block = format_since_yesterday_block(continuity_payload)

        chart_pair_count = len(chart_raw) if chart_raw else 0
        data_assembly_ms = int((time.time() - t_data) * 1000)
        log.info("Data assembly: %.1fs, %d pairs", data_assembly_ms / 1000, chart_pair_count)
        _record_phase("phase_a", "Data Assembly", "ok" if chart_pair_count > 0 else "error",
                      datetime.fromtimestamp(t_data).strftime("%H:%M:%S"), data_assembly_ms,
                      detail=f"{chart_pair_count} pairs")

        if chart_pair_count == 0:
            log.error("ABORT: No chart data available")
            _notify("Dispatch ABORTED", f"No chart data for {region}")
            return

        # Phase A.5: Signal Extraction
        log.info("Phase A.5: Signal extraction...")
        t_signals = time.time()
        from .signal_extractor import extract_all_signals, select_spotlight_assets

        signal_data = {}
        try:
            signal_data = extract_all_signals(chart_raw, context)
            signal_summary = signal_data["summary_json"]
            market_regime = signal_data["market_regime"]
            signal_conflicts = signal_data["signal_conflicts"]

            signal_ms = int((time.time() - t_signals) * 1000)
            _record_phase("phase_a5", "Signal Extraction", "ok",
                          datetime.fromtimestamp(t_signals).strftime("%H:%M:%S"), signal_ms,
                          detail=f"regime={market_regime}, {len(signal_conflicts)} conflicts")
            log.info("Signals: regime=%s, %d conflicts", market_regime, len(signal_conflicts))
        except Exception as e:
            log.error("Signal extraction failed: %s", e, exc_info=True)
            signal_summary = "{}"
            market_regime = "unknown"
            signal_conflicts = []
            signal_ms = int((time.time() - t_signals) * 1000)
            _record_phase("phase_a5", "Signal Extraction", "degraded",
                          datetime.fromtimestamp(t_signals).strftime("%H:%M:%S"), signal_ms,
                          error=str(e))

        # Phase B: Macro Research + Top-5 Drivers
        log.info("Phase B: Macro research + top-5...")
        t_macro = time.time()

        macro_query = macro_research_prompt(signal_summary_json=signal_summary)
        from datetime import timedelta
        from_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        to_date = datetime.now().strftime("%Y-%m-%d")

        macro_text = ""
        macro_tools = [{"type": "web_search"}]
        if DEFAULT_X_HANDLES:
            macro_tools.append({"type": "x_search", "allowed_x_handles": DEFAULT_X_HANDLES,
                                "from_date": from_date, "to_date": to_date})
        else:
            log.warning("X_SEARCH_HANDLES is empty - macro research runs without x_search")
        macro_timeouts = [120, 120, 150, 180, 240]
        for attempt in range(1, 6):
            try:
                macro_result = grok.search(
                    macro_query, tools=macro_tools,
                    max_tokens=MAX_MACRO_TOKENS,
                    timeout=macro_timeouts[attempt - 1],
                )
                from security import sanitize_for_llm as _sanitize
                macro_text = _sanitize(macro_result["content"], max_length=200_000, source="grok_macro")
                break
            except Exception as e:
                log.warning("Macro research attempt %d/5: %s", attempt, e)
                if attempt == 5:
                    macro_text = ""
                else:
                    time.sleep(5 * attempt)

        if not macro_text:
            log.error("ABORT: Macro research failed after 5 retries")
            _notify("Dispatch ABORTED", f"Macro research failed for {region}")
            return

        # Top-5 drivers
        top5_text = ""
        asset_priority = {}
        from .prompts import top5_drivers_prompt
        for t5_attempt in range(1, 3):
            try:
                top5_result = sonnet.chat(
                    system_prompt="You are a concise market analyst. Follow the output format exactly.",
                    user_message=top5_drivers_prompt(signal_summary, macro_text),
                    max_tokens=1200, temperature=0.5, timeout=60,
                )
                raw_top5 = top5_result["content"]
                separator = "---ASSET_PRIORITY---"
                if separator in raw_top5:
                    parts = raw_top5.split(separator, 1)
                    top5_text = parts[0].strip()
                    asset_priority = _parse_asset_priority(parts[1].strip())
                else:
                    top5_text = raw_top5
                break
            except Exception as e:
                if t5_attempt == 2:
                    log.warning("Top-5 drivers failed: %s", e)

        macro_ms = int((time.time() - t_macro) * 1000)
        log.info("Macro + top-5: %.1fs", macro_ms / 1000)

        # Load config
        steering = _load_steering_yaml()
        patterns = _load_patterns_yaml()

        # Fetch past overviews for narrative continuity
        # type=general: the morning-type stream is dead — see
        # the identical fix in run() and run_weekly(). General reports carry
        # summary, not audio_script.
        past_overviews_text = ""
        try:
            resp = requests.get(
                f"{DASHBOARD_BASE_URL}/api/report/morning/recent?limit=5&type=general",
                headers=_API_HEADERS, timeout=5)
            if resp.ok:
                recent = resp.json()
                overview_parts = []
                for rec in recent:
                    script = rec.get('audio_script') or rec.get('summary') or ''
                    if script and len(script) > 20:
                        date_str = str(rec.get('created_at', 'Unknown'))[:10]
                        overview_parts.append(f"### {date_str}\n{script}")
                if overview_parts:
                    past_overviews_text = "\n\n".join(overview_parts)
        except Exception as e:
            log.debug("Past-overview fetch failed (non-fatal): %s", e)

        # Build shared briefing parts
        shared_briefing_parts = _build_shared_briefing_parts(
            today, market_regime, top5_text, signal_summary,
            signal_conflicts, macro_text, context, signal_data,
            quant_context, chart_data, options_data,
            past_overviews_text, asset_priority)

        # Permitted positioning block, injected the same way the
        # market-breadth block is — no signature change to the shared builder.
        if permitted_deriv:
            shared_briefing_parts = list(shared_briefing_parts) + [
                f"\n{permitted_deriv}\n"
            ]

        # Inject market breadth block (Wire 2 — daily)
        dispatch_breadth = fetch_market_breadth()
        if dispatch_breadth:
            shared_briefing_parts = list(shared_briefing_parts) + [
                f"\n## Market Breadth\n{dispatch_breadth}\n"
            ]

        # Phase B.7: Spotlight selection for shared report
        admin_report_assets = None
        try:
            resp = requests.get(
                f"{DASHBOARD_BASE_URL}/api/user/preferences",
                headers=_API_HEADERS, timeout=5)
            if resp.ok:
                prefs = resp.json()
                ra = prefs.get("report_assets", [])
                if ra:
                    admin_report_assets = [a["asset"] if isinstance(a, dict) else a for a in ra]
        except Exception as e:
            log.debug("Admin preferences fetch failed (non-fatal): %s", e)

        all_signals = signal_data.get("per_asset", {})
        spotlight_symbols, summary_symbols = select_spotlight_assets(
            all_signals, user_report_assets=admin_report_assets)

        from .data_assembly import format_compact_asset_lines
        compact_summary_text, range_pctile_map = format_compact_asset_lines(chart_raw, summary_symbols)

        briefing_parts = list(shared_briefing_parts) + [
            f"## SPOTLIGHT ASSETS: {', '.join(spotlight_symbols)}\n"
            f"These assets get narrative depth in Asset Spotlight. All others appear in Market Snapshot table only.\n",
            f"\n## Non-Spotlight Asset Summary\n{compact_summary_text}\n" if compact_summary_text else "",
            f"## Alerts\n{context.get('alerts', 'None')}\n",
            f"## Positions\n{context.get('positions', 'None')}\n",
            f"## Yesterday\n{context.get('yesterday_report', 'No previous report')}\n",
            # Daily continuity data for Since Yesterday section
            continuity_block if continuity_block else "",
        ]
        briefing = "\n".join(p for p in briefing_parts if p)

        if dry_run:
            work_dir = tempfile.mkdtemp(prefix=f"dispatch-{region}-{today}-")
            with open(os.path.join(work_dir, "briefing.txt"), "w") as f:
                f.write(briefing)
            log.info("DRY RUN — briefing: %d chars, regime: %s",
                     len(briefing), market_regime)
            log.info("Saved to %s/briefing.txt", work_dir)
            return

        # Operator morning report disabled → generate ONLY the free general report
        # (operator-neutral). The operator is served this same general report.
        if not ENABLE_MORNING_REPORT:
            log.info("Phase C skipped (ENABLE_MORNING_REPORT=false) — general report only")
            _generate_general_report(
                shared_briefing_parts, spotlight_symbols, compact_summary_text,
                steering, patterns, sonnet, t_start,
                morning_report=None,
                continuity_block=continuity_block,
                scenario_levels=(quant_context or {}).get('confluence_levels'),
                range_pctile_map=range_pctile_map,
                options_claims=options_claims,
            )
            return

        # Phase C: Generate shared/free-tier report
        log.info("Phase C: Shared report generation...")
        t_report = time.time()
        from carrac.grounding import grounded_system_prompt as _gsp_shared_disp
        system_prompt = _gsp_shared_disp(shared_report_system_prompt(steering, patterns))

        shared_report = None
        try:
            report_result = _call_with_retry(
                opus.chat,
                system_prompt=system_prompt,
                user_message=f"Here is today's market data and pre-processed signals. Generate the full morning analysis report.\n\n{briefing}",
                max_tokens=MAX_REPORT_TOKENS,
                temperature=0.7, timeout=600,
                max_attempts=3,
                label=f"dispatch[{region}]",
            )
            shared_report = report_result["content"]
            # Enforce hard char limit (matches morning mode).
            shared_report = _enforce_char_limit(
                shared_report, 14000,
                log_callback=lambda m, _r=region: log.warning("dispatch=%s shared_report: %s", _r, m),
            )
        except Exception as e:
            log.error("Dispatch report generation failed for %s: %s", region, e, exc_info=True)
            _notify("Dispatch ABORTED", f"Report generation failed for {region}")
            return

        report_ms = int((time.time() - t_report) * 1000)
        log.info("Shared report: %d chars, %.1fs", len(shared_report), report_ms / 1000)

        # This shared_report — the paid, region-specific dispatch
        # report — was the one content stream with ZERO tripwires wired (the
        # same shape on the report type that replaced the flagship).
        # Nine of the twelve run here: the five run_morning-parity scans plus
        # the four whose grounding data this path already computes. All are
        # advisory (log.error → the error tracker via the Sentry logging integration),
        # and they run BEFORE _post_report persists anything, so the alarm
        # always precedes a bad report reaching users. Deliberately NOT wired
        # (recorded here): _confluence_tripwire and
        # _rotation_quadrant_tripwire (need _levels_focus lifted out of
        # _generate_general_report / an extra HTTP fetch), _verify_tripwire
        # (a per-run Haiku spend — a cost decision), _afternoon_grounding_tripwire
        # (afternoon-only by design).
        _tw_label = f"dispatch shared report [{region}]"
        _calendar_tripwire(shared_report, _tw_label)
        _advice_tripwire(shared_report, _tw_label)
        _news_tripwire(shared_report, _tw_label)
        _data_release_tripwire(shared_report, _tw_label)
        _leadership_tripwire(shared_report, _tw_label)
        _scenario_advisory_tripwire(shared_report, _tw_label)
        _internal_arithmetic_tripwire(shared_report, _tw_label)
        _range_pctile_tripwire(shared_report, range_pctile_map, _tw_label)
        _grounded_field_tripwire(shared_report, options_claims, _tw_label)

        # Phase C.5: Report Critic (optional) — grounded variant
        from .config import ENABLE_CRITIC
        if ENABLE_CRITIC and shared_report:
            try:
                from .prompts import critic_prompt
                from carrac.grounding import (
                    facts_block as _fb_d, grounded_system_prompt as _gsp_cd,
                    scan as _scan_d, build_facts_dict as _facts_d,
                )
                _facts_crit_d = _facts_d(include_prices=True)
                _fb_crit_d = _fb_d(include_prices=True, facts=_facts_crit_d)
                critic_response = sonnet.chat(
                    system_prompt=_gsp_cd(critic_prompt(), facts_block_text=_fb_crit_d),
                    user_message=(f"FACTS BLOCK:\n{_fb_crit_d}\n\n"
                                  f"REPORT UNDER REVIEW:\n{shared_report[:30000]}"),
                    max_tokens=2000, temperature=0.2,
                )
                log.info("Critic: %d chars", len(critic_response["content"]))
                _findings_d = _scan_d(shared_report, _facts_crit_d)
                _highs_d = [f for f in _findings_d if f.severity.value == "HIGH"]
                if _highs_d:
                    log.error("Dispatch shared report tripwire HIGH: %s",
                              [(f.pattern_name, f.match_text) for f in _highs_d[:5]])
                    try:
                        _notify("Dispatch Report HIGH tripwire",
                                f"{_highs_d[0].pattern_name} — {_highs_d[0].match_text!r}")
                    except Exception as _ne:
                        log.warning("Tripwire notify failed (non-fatal): %s", _ne)
            except Exception as e:
                log.warning("Critic failed (non-fatal): %s", e)

        # Phase D: Save shared report
        assets = list(set(p.split("/")[0] for p in from_chart_pairs()))
        report_id = _post_report(
            content=shared_report,
            report_type="morning",
            assets=assets,
            generation_time_ms=int((time.time() - t_start) * 1000),
            source_briefing=briefing,
        )

        if not report_id:
            log.error("Failed to save shared report, cannot proceed")
            return

        # Free-tier shared "general" report (operator-neutral; served to all free
        # accounts). Reuses the assembled shared briefing — one cheap Sonnet call.
        # Pass morning_report so the dedup check can detect cross-report overlap.
        _generate_general_report(
            shared_briefing_parts, spotlight_symbols, compact_summary_text,
            steering, patterns, sonnet, t_start,
            morning_report=shared_report,
            continuity_block=continuity_block,
            scenario_levels=(quant_context or {}).get('confluence_levels'),
            range_pctile_map=range_pctile_map,
            options_claims=options_claims,
        )

        # Phase F: Audio briefing (public) — dispatch variant
        # Sonnet writes the script, grounded by the facts block; a HIGH regex hit blocks it.
        if report_id and TTS_ENABLED:
            try:
                from .prompts import audio_briefing_prompt
                from carrac.grounding import (
                    facts_block, grounded_system_prompt,
                    scan as _hscan, has_high as _has_high, build_facts_dict,
                )
                story = _extract_section(shared_report, "The Story") or ""
                action_dashboard = _extract_section(shared_report, "Action Dashboard") or ""
                regime = _extract_section(shared_report, "Regime & Positioning") or ""
                scenarios = _extract_section(shared_report, "Scenarios") or ""
                macro_section = (_extract_section(shared_report, "Macro Context")
                                 or _extract_section(shared_report, "Macro")
                                 or story)
                overview = (_extract_section(shared_report, "Overview")
                            or regime
                            or scenarios)
                shared_action_items = (_extract_section(shared_report, "Action Items")
                                       or action_dashboard)
                if not (macro_section or overview or shared_action_items):
                    if not shared_report or len(shared_report) < 500:
                        log.warning("Audio briefing (dispatch): shared_report missing or too short (%d chars); skipping",
                                    len(shared_report or ""))
                        raise RuntimeError("audio_briefing_skipped_no_source")
                    log.warning("Audio briefing (dispatch): no known sections; feeding full report as single block")
                    audio_input = f"Source report:\n\n{shared_report[:5000]}"
                else:
                    parts_in = []
                    if macro_section:
                        parts_in.append(f"Macro context (prioritize this):\n{macro_section[:2000]}")
                    if overview:
                        parts_in.append(f"Report overview:\n{overview[:2000]}")
                    if shared_action_items:
                        parts_in.append(f"Action Items:\n{shared_action_items[:1000]}")
                    audio_input = "\n\n".join(parts_in)
                facts = build_facts_dict(include_prices=True)
                fb_text = facts_block(include_prices=True, facts=facts)
                grounded = grounded_system_prompt(audio_briefing_prompt(), facts_block_text=fb_text)
                script_result = sonnet.chat(
                    system_prompt=grounded,
                    user_message=f"Generate a spoken audio briefing based on today's source report:\n\n{audio_input}",
                    max_tokens=2000, temperature=0.6,
                )
                raw_output = script_result["content"]
                separator = "---SPOKEN_SCRIPT---"
                if separator in raw_output:
                    parts_audio = raw_output.split(separator, 1)
                    display_script = _strip_section_header(parts_audio[0].strip(), "display")
                    spoken_script = _strip_section_header(parts_audio[1].strip(), "spoken")
                else:
                    display_script = raw_output
                    spoken_script = raw_output
                scan_target = (display_script or "") + "\n" + (spoken_script or "")
                findings = _hscan(scan_target, facts)
                _advice_tripwire(scan_target, "dispatch audio briefing")
                if _has_high(findings):
                    log.error("Dispatch audio BLOCKED — %s",
                              [f.pattern_name for f in findings if f.severity.value == "HIGH"])
                    try:
                        _notify("Dispatch Audio BLOCKED",
                                f"Report #{report_id} dispatch audio dropped by tripwire.")
                    except Exception as _ne:
                        log.warning("Dispatch-audio-block notify failed: %s", _ne)
                else:
                    if len(spoken_script) > 4000:
                        spoken_script = spoken_script[:4000].rsplit('.', 1)[0] + '.'
                    _save_audio_script(report_id, display_script)
                    _generate_tts(grok, spoken_script, report_id)
                    log.info("Audio briefing generated")
            except Exception as e:
                log.warning("Audio briefing failed: %s", e)

        # Save metadata
        grok_usage = grok.get_usage_summary()
        opus_usage = opus.get_usage_summary()
        sonnet_usage = sonnet.get_usage_summary()
        total_cost = grok_usage["total_cost"] + opus_usage["total_cost"] + sonnet_usage["total_cost"]

        metadata = {
            "model": f"opus:{opus.model}+sonnet:{sonnet.model}+grok:{grok.model}",
            "cost": total_cost,
            "generation_time_ms": int((time.time() - t_start) * 1000),
            "mode": "dispatch",
            "dispatch_region": region,
            "engine_version": "2.7",
            "data_sources": {},
            "phase_results": phase_results,
        }
        _save_metadata(report_id, metadata)
        _log_engine_costs("dispatch", opus=opus, sonnet=sonnet, grok=grok)

        # Cache shared data to Redis for subsequent batches
        _cache_set("report_id", report_id)
        _cache_set("shared_briefing_parts", shared_briefing_parts)
        _cache_set("chart_raw", chart_raw)
        _cache_set("signal_data", signal_data)
        _cache_set("context", context)
        _cache_set("signal_conflicts", signal_conflicts)
        log.info("Shared data cached to Redis (TTL=%ds)", CACHE_TTL)
        # Per-dispatch "Dispatch Shared Report | $cost" operator ping removed —
        # it fired ~5x/day for the same shared report. The morning-report route
        # sends one deduped daily ping; cost lives in ai_usage_log.
        # Error/abort/tripwire _notify calls are untouched.
        log.info("Dispatch %s shared report #%s | $%.3f (operator ping deduped)",
                 region, report_id, total_cost)

    # ------------------------------------------------------------------
    # Phase E: Personalized reports for this region's users
    # ------------------------------------------------------------------
    if dry_run:
        log.info("DRY RUN — would generate personalized reports for %d users", len(eligible_users))
        return

    from .config import PERSONALIZED_REPORTS
    if not PERSONALIZED_REPORTS:
        log.warning("PERSONALIZED_REPORTS is disabled — skipping per-user generation")
        return

    log.info("Phase E: Personalized reports for %d users in %s...", len(eligible_users), region)
    t_personal = time.time()

    _run_personalized_reports(
        report_id, shared_briefing_parts, chart_raw, signal_data,
        context, signal_conflicts, steering, patterns,
        sonnet, grok, _record_phase,
        eligible_users=eligible_users,
        quant_context=quant_context,
    )

    personal_ms = int((time.time() - t_personal) * 1000)
    log.info("Personalized reports: %d users, %.1fs", len(eligible_users), personal_ms / 1000)

    # Send per-user notifications
    try:
        from push_notifications import notify_user_report_ready
        for u in eligible_users:
            try:
                notify_user_report_ready(u["id"], report_id)
            except Exception as e:
                log.debug("Notification failed for user %s: %s", u["id"], e)
    except ImportError:
        log.debug("push_notifications not available in engine context")

    elapsed = time.time() - t_start
    log.info("=== Dispatch %s Complete — %.1fs, %d users ===", region, elapsed, len(eligible_users))


def _flip_entry(change: dict):
    """Classify one /api/report/diff change into a signal-flip ledger row.

    The diff endpoint emits three shapes: consensio_* (signed numbers),
    rsi_14 (positive numbers), and sma_crossover (the STRINGS
    'golden'/'death') — comparing those strings with > 0 raises TypeError,
    which would silently truncate the whole ledger inside the caller's
    bare except. Returns None for shapes that don't map to a flip.
    """
    old, new = change.get('old_value'), change.get('new_value')
    if change.get('field') == 'sma_crossover':
        if old in ('golden', 'death') and new in ('golden', 'death'):
            return {
                'asset': change.get('symbol', ''),
                'from': 'bullish' if old == 'golden' else 'bearish',
                'to': 'bullish' if new == 'golden' else 'bearish',
            }
        return None
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return {
            'asset': change.get('symbol', ''),
            'from': 'bullish' if old > 0 else 'bearish' if old < 0 else 'neutral',
            'to': 'bullish' if new > 0 else 'bearish' if new < 0 else 'neutral',
        }
    return None


def run_afternoon(dry_run=False):
    """Run afternoon delta report."""
    t_start = time.time()
    log.info("=== Afternoon Delta ===")

    grok = GrokClient()          # For x_search if needed + TTS
    # Sonnet by default; MORNING_PRIMARY_MODEL=opus|grok overrides. The name
    # `opus` is historical.
    opus = _make_primary_client()
    # Sonnet handles the afternoon audio script (grounded).
    sonnet = MorningAnthropicClient(model=ANTHROPIC_MODEL_FAST)

    # Fetch fresh data
    chart_data, _ = fetch_chart_data()
    context = fetch_context_data()

    # Get morning report for comparison — prefer TODAY's actual
    # general report over the preassembly 'yesterday_report' field, which
    # reads get_latest_report(report_type='morning') with NO date filter
    # (carrac/reports/routes.py _fetch_yesterday()). With 'morning' no longer
    # generated, that field would serve a stale report as "this morning's",
    # inviting fabricated "since this morning" deltas: the model would never
    # be shown today's morning read. Falls back to the (known-stale) preassembly field only if
    # today's general report genuinely isn't available yet.
    _today_general = fetch_todays_general_report()
    if _today_general and _today_general.get('content'):
        morning_report = _today_general['content']
    else:
        log.warning("today's general report unavailable — "
                     "falling back to preassembly yesterday_report field")
        morning_report = context.get("yesterday_report", "")  # Latest = today's morning

    # Fetch derivatives data for delta comparison
    options_data, options_claims = fetch_options_data()
    # The permitted-data positioning block. Rides ALONGSIDE the
    # Deribit block rather than replacing the variable — the two are
    # different reads (positioning vs volatility) and the Deribit one is
    # still assembled for the operator. Its grounding claims are merged into
    # options_claims so the existing _grounded_field_tripwire calls cover
    # both without a second wiring point to forget.
    permitted_deriv, _permitted_deriv_claims = fetch_permitted_derivatives()
    options_claims = list(options_claims or []) + _permitted_deriv_claims

    # Fetch today's scheduled macro events — and trigger fill_event_actuals()
    # via the quant API so that FRED-sourced actuals (CPI, NFP, FOMC, etc.)
    # are populated before we build the briefing.
    # US releases print at 12:30/13:30 UTC; FRED publishes within ~1h.
    # The afternoon engine runs at ~15:00 UTC, so actuals should be available.
    from .data_assembly import _api as _da_api
    today_str = datetime.now().strftime('%Y-%m-%d')
    try:
        # Trigger the fill (POST — fire and check result)
        fill_result = _da_api(
            f'/api/quant/events/fill-actuals?date={today_str}',
            timeout=30, retries=1)
        today_events = (fill_result.get('events', [])
                        if isinstance(fill_result, dict)
                        else [])
        log.info("Afternoon actuals fill: %d filled, %d events returned",
                 fill_result.get('filled', 0) if isinstance(fill_result, dict) else 0,
                 len(today_events))
    except Exception as _ce:
        log.debug("Afternoon fill-actuals failed (non-fatal): %s", _ce)
        # Fall back to plain event list
        try:
            today_events = _da_api(
                f'/api/quant/events?from={today_str}&to={today_str}',
                timeout=8, retries=1)
            if not isinstance(today_events, list):
                today_events = []
        except Exception:
            today_events = []

    try:
        # Forward event DATES from FRED (free, real, auto-updating). There is
        # deliberately NO consensus fetch: Finnhub's /calendar/economic is
        # premium, it was deliberately not subscribed to, and the
        # fetcher was deleted rather than left failing daily. quant.events
        # .expected stays empty — prompts must never invent a consensus.
        from quant.collectors.event_calendar import fetch_fred_release_events
        fetch_fred_release_events(window_days=45)
    except Exception as e:
        log.warning("event calendar refresh failed (non-fatal): %s", e)

    if today_events:
        evt_lines = []
        has_actuals = False
        for e in today_events:
            title = e.get('title', '')
            impact = e.get('impact', 'med')
            actual = e.get('actual')
            if actual:
                evt_lines.append(f"- {title} ({impact} impact) — actual: {actual}")
                has_actuals = True
            else:
                evt_lines.append(f"- {title} ({impact} impact)")
        today_calendar_text = "Today's scheduled events:\n" + "\n".join(evt_lines)
        if not has_actuals:
            today_calendar_text += (
                "\n(Actual printed values are NOT yet available — "
                "do NOT claim any number was 'as expected' or 'beat/missed' "
                "without an actual number in hand.)"
            )
    else:
        today_calendar_text = "No high-impact scheduled events today."

    # Breadth snapshot for afternoon delta — same-day breadth shift is
    # prime afternoon-delta material (washed-out/frothy label often changes
    # intraday when the session opens).
    afternoon_breadth = fetch_market_breadth()

    # --- Structured afternoon delta ----------------------------
    # Build a deterministic "what moved" ledger from data already in scope.
    # Non-fatal: prose report still generates if this block raises.
    # Persisted via _save_metadata so the delta endpoint can serve it as JSON.
    #
    # Fields sourced from run_afternoon scope:
    #   regime      — current from /api/quant/snapshot; morning DEFAULTED to
    #                 now (no stored morning value → was==now, changed=False)
    #   fear_greed  — current from fetch_fear_greed_index(); morning DEFAULTED
    #                 to now (same reason)
    #   signal_flips — from /api/report/diff (same endpoint as fetch_signal_flips)
    #   new_prints  — from today_events already in scope (events with actuals)
    #   levels_tested — [] (no levels data in afternoon scope; honest default)
    _afternoon_delta = None
    # Defined here (not only inside the try below) so the
    # afternoon_delta_prompt() call further down always has a value to read
    # even if this whole block raises before reaching its own assignment —
    # None there is a documented no-op, not a NameError.
    _current_regime = None
    try:
        from morning_engine.afternoon_delta import build_delta as _build_delta
        from morning_engine.data_assembly import _api as _delta_api

        # Current fear/greed (cheap external API; already used throughout codebase)
        _current_fg = None
        try:
            from carrac.analysis import fetch_fear_greed_index as _ffg
            _fg_data = _ffg()
            _current_fg = _fg_data.get('value')
        except Exception:
            pass

        # Current regime from quant snapshot (cheap, cached)
        _current_regime = 'UNKNOWN'
        try:
            _snap = _delta_api('/api/quant/snapshot', timeout=8, retries=1)
            _current_regime = (_snap.get('regime') or {}).get('state') or 'UNKNOWN'
        except Exception:
            pass

        # Signal flips from the diff endpoint (structured, same source as context signal_diff)
        _signal_flips = []
        try:
            _diff_data = _delta_api('/api/report/diff', timeout=8, retries=1)
            _raw_changes = _diff_data.get('changes', []) if isinstance(_diff_data, dict) else []
            for _c in _raw_changes:
                _entry = _flip_entry(_c)
                if _entry:
                    _signal_flips.append(_entry)
        except Exception:
            pass

        # New prints from today_events (already fetched above, only those with actuals)
        _new_prints = []
        for _evt in (today_events or []):
            _actual = _evt.get('actual')
            if _actual is not None:
                _new_prints.append({
                    'event': _evt.get('title', ''),
                    'expected': _evt.get('expected') or _evt.get('forecast') or None,
                    'actual': str(_actual),
                    'ts': str(_evt.get('event_date') or _evt.get('ts') or ''),
                })

        # morning_ctx defaults was==now because no stored morning snapshot is
        # readily available in run_afternoon scope without an expensive fetch.
        _morning_ctx = {'regime': _current_regime, 'fear_greed': _current_fg}
        _current_ctx = {
            'regime': _current_regime,
            'fear_greed': _current_fg,
            'levels_tested': [],   # honest empty — no levels data in scope
            'signal_flips': _signal_flips,
            'new_prints': _new_prints,
        }
        _afternoon_delta = _build_delta(_morning_ctx, _current_ctx)
        log.info("Afternoon delta built: %d signal_flips, %d new_prints",
                 len(_signal_flips), len(_new_prints))
    except Exception:
        log.warning("afternoon delta build failed (non-fatal)", exc_info=True)
        _afternoon_delta = None
    # ----------------------------------------------------------------------

    briefing = f"""## Morning Report (for comparison)
{morning_report}

## Current Chart Data
{chart_data}

## Current Signal Changes
{context.get('signal_diff', 'None')}

## Current Sentiment
{context.get('sentiment', 'Unavailable')}

## Current Derivatives Data
{options_data}

{permitted_deriv}

## Market Breadth (current)
{afternoon_breadth if afternoon_breadth else "Breadth data unavailable."}

## Today's Macro Calendar
{today_calendar_text}
"""

    if dry_run:
        log.info("DRY RUN — afternoon briefing: %d chars", len(briefing))
        return

    try:
        from carrac.grounding import grounded_system_prompt as _gsp_aft
        # Hard constraints from TODAY's actual morning read, not
        # background prose alone — extracted from the same content just
        # fetched into morning_report above (fetch_todays_general_report).
        _morning_constraints = extract_morning_constraints(morning_report)
        result = opus.chat(
            system_prompt=_gsp_aft(afternoon_delta_prompt(
                morning_constraints=_morning_constraints,
                morning_regime=_current_regime,
            )),
            user_message=briefing,
            max_tokens=8000,
            temperature=0.7,
        )
        content = result["content"]
        # Haiku verifier wiring — capture the pure LLM output before the deterministic
        # facts-strip / actuals / anomaly blocks below are attached, for the
        # Haiku verifier further down (same reasoning as the general report).
        _afternoon_llm_content = content
    except Exception as e:
        log.error("Afternoon delta failed: %s", e)
        return

    # Deterministic Macro Facts strip (neutral wording → safe vs the
    # afternoon tripwires that scan content). Prepended before persisting.
    try:
        from carrac.grounding import build_facts_dict, render_macro_facts_strip
        _aft_facts_strip = render_macro_facts_strip(build_facts_dict(include_prices=True))
        if _aft_facts_strip:
            content = _aft_facts_strip + "\n\n" + content
    except Exception as _fs_err:
        log.warning("macro facts strip (afternoon) failed (non-fatal): %s", _fs_err)

    # G2a — Data Released Today actuals table (IN CODE, never fed to LLM).
    try:
        from morning_engine.report_blocks import build_actuals_block
        _actuals = build_actuals_block(today_events)
        if _actuals:
            content = content.rstrip() + "\n\n" + _actuals
    except Exception as _actuals_err:
        log.warning("Actuals block failed (non-fatal): %s", _actuals_err)

    # G2b — Active Extremes anomaly pulse (IN CODE, never fed to LLM).
    try:
        from morning_engine.report_blocks import build_anomaly_pulse_block
        _pulse = build_anomaly_pulse_block()
        if _pulse:
            content = content.rstrip() + "\n\n" + _pulse
    except Exception as _pulse_err:
        log.warning("Anomaly pulse block failed (non-fatal): %s", _pulse_err)

    # Pre-publish grounding gate. Runs the same spread
    # self-consistency + breadth continuity checks as the save-time backstop,
    # but here, BEFORE _post_report, so the alarm can fire before the report
    # is public. Needs the finalized `content` (the macro-facts strip just
    # above is what supplies the deterministic header spread line the check
    # reads) and `morning_report`, already fetched earlier in this function.
    _afternoon_grounding_tripwire(content, morning_report, "afternoon report")

    report_id = _post_report(content, report_type="afternoon",
                             generation_time_ms=int((time.time() - t_start) * 1000),
                             source_briefing=briefing)

    # Persist structured delta so /api/report/afternoon/latest/delta
    # can serve it without re-computing.  Stored in reports.metadata as
    # {"delta": {...}} — same column used by morning/general reports for metadata.
    # Uses the existing /api/report/morning/<id>/metadata PUT endpoint which
    # operates on any report_id regardless of report_type.
    if report_id and _afternoon_delta is not None:
        try:
            _save_metadata(report_id, {"delta": _afternoon_delta})
            log.info("Afternoon delta persisted for report #%s", report_id)
        except Exception as _delta_err:
            log.error("afternoon delta persist failed for report %s (non-fatal): %s", report_id, _delta_err)

    _advice_tripwire(content, "afternoon report")
    _news_tripwire(content, "afternoon report")
    _data_release_tripwire(content, "afternoon report")
    _leadership_tripwire(content, "afternoon report")

    # Haiku second-opinion fact check (non-blocking) against the
    # SAME briefing (morning report + current chart/sentiment/derivatives/
    # breadth/calendar data) the afternoon delta was generated from.
    _verify_tripwire(_afternoon_llm_content, briefing, "afternoon report", "afternoon")
    _internal_arithmetic_tripwire(_afternoon_llm_content, "afternoon report")
    _grounded_field_tripwire(_afternoon_llm_content, options_claims, "afternoon report")

    # Save afternoon summary — parse Session Summary first paragraph (no LLM call).
    # Morning saves via _extract_exec_summary on the admin personal section;
    # afternoon has no personal section, so we extract inline here.
    if report_id:
        aft_summary = _extract_afternoon_summary(content)
        if aft_summary:
            _save_summary(report_id, aft_summary)
            log.info("Afternoon summary saved: %d chars", len(aft_summary))

    # Afternoon audio briefing — Sonnet, grounded, with tripwire
    if report_id and TTS_ENABLED:
        try:
            from .prompts import afternoon_audio_prompt
            from carrac.grounding import (
                facts_block, grounded_system_prompt,
                scan as _hscan, has_high as _has_high, build_facts_dict,
            )
            facts = build_facts_dict(include_prices=True)
            fb_text = facts_block(include_prices=True, facts=facts)
            grounded = grounded_system_prompt(afternoon_audio_prompt(), facts_block_text=fb_text)
            script_result = sonnet.chat(
                system_prompt=grounded,
                user_message=f"Generate a brief afternoon update based on the source:\n\n{content[:3000]}",
                max_tokens=600,
                temperature=0.6,
            )
            afternoon_script = script_result["content"]
            findings = _hscan(afternoon_script, facts)
            _advice_tripwire(afternoon_script, "afternoon audio briefing")
            if _has_high(findings):
                log.error("Afternoon audio BLOCKED — %s",
                          [f.pattern_name for f in findings if f.severity.value == "HIGH"])
                try:
                    _notify("Afternoon Audio BLOCKED",
                            f"Report #{report_id} afternoon audio dropped by tripwire.")
                except Exception as _ne:
                    log.warning("Afternoon-audio-block notify failed: %s", _ne)
            else:
                if len(afternoon_script) > 2000:
                    afternoon_script = afternoon_script[:2000].rsplit('.', 1)[0] + '.'
                _save_audio_script(report_id, afternoon_script)
                _generate_tts(grok, afternoon_script, report_id)
                log.info("Afternoon audio: %d chars", len(afternoon_script))
        except Exception as e:
            log.warning("Afternoon audio failed: %s", e)

    total_cost = (grok.get_usage_summary()["total_cost"]
                  + opus.get_usage_summary()["total_cost"]
                  + sonnet.get_usage_summary()["total_cost"])
    log.info("Afternoon delta: %.1fs, $%.4f, %d chars",
             time.time() - t_start, total_cost, len(content))

    _log_engine_costs("afternoon", opus=opus, grok=grok, sonnet=sonnet)

    return report_id


def _operator_user_id():
    """The operator's user id: the account registered with ADMIN_EMAIL, or None."""
    email = os.getenv('ADMIN_EMAIL', '').strip()
    if not email:
        return None
    try:
        from user_db import user_db
        user = user_db.get_user_by_email(email)
    except Exception as e:
        log.warning("Weekly play review: operator lookup failed: %s", e)
        return None
    return user.get('id') if user else None


def _fetch_weekly_play_review_text(days=7, max_chars=3000):
    """Build a compact play + closed-trade summary for the weekly Play Review section.

    Fetches:
      - All operator active plays and recently status-changed plays (last 7 days)
        via carrac.plays.models (direct DB — no HTTP round-trip needed).
      - Closed trades for the past 7 days via carrac.content_db
        (cap at 20 trades).

    Only the operator's own plays and trades are read (the account registered
    with ADMIN_EMAIL); other users' data never enters the digest prompt. With
    no operator account the section is empty.

    Returns a formatted text block capped at max_chars.
    If nothing is found, returns "none this week" so the prompt is explicit.
    Never throws — returns "(Play review unavailable)" on any error.
    """
    lines = []
    operator_uid = _operator_user_id()
    if operator_uid is None:
        log.warning("Weekly play review skipped: no operator account (ADMIN_EMAIL unset or not registered)")
        return "none this week"
    try:
        # ── Active + recently-changed plays ──────────────────────────
        from carrac.plays.models import get_active_plays as _gap
        all_plays = _gap(user_id=operator_uid)
        active = [p for p in all_plays if p.get('status') not in ('completed', 'invalidated')]
        # Status-changed in the last 7 days (terminal plays that moved this week)
        from datetime import timedelta as _td
        week_ago = (datetime.now(timezone.utc) - _td(days=days)).isoformat()
        terminal = [
            p for p in all_plays
            if p.get('status') in ('completed', 'invalidated')
            and (p.get('status_changed_at') or '') >= week_ago
        ]

        if active:
            lines.append(f"Active plays ({len(active)} total):")
            for p in active[:8]:  # cap at 8
                title = p.get('title', 'Untitled')[:60]
                status = p.get('status', '?')
                asset = p.get('primary_asset', '?')
                direction = p.get('direction', '')
                conds = p.get('conditions') or {}
                rules = conds.get('rules', []) if isinstance(conds, dict) else []
                met = sum(1 for r in rules if r.get('met'))
                total = len(rules)
                cond_str = f" ({met}/{total} conditions met)" if total else ""
                lines.append(f"  [{status}] {title} — {asset} {direction}{cond_str}")

        if terminal:
            lines.append(f"\nStatus changes this week ({len(terminal)}):")
            for p in terminal[:5]:
                title = p.get('title', 'Untitled')[:60]
                status = p.get('status', '?')
                asset = p.get('primary_asset', '?')
                lines.append(f"  [{status}] {title} — {asset}")

    except Exception as e:
        log.debug("Weekly plays fetch failed (non-fatal): %s", e)
        lines.append("(Plays unavailable)")

    try:
        # ── Closed trades for the week ────────────────────────────────
        from carrac.content_db import get_content_db as _gdb
        cdb = _gdb()
        history = cdb.get_trade_history(operator_uid, days=days, limit=20)
        if history:
            wins = [t for t in history if (t.get('rr_actual') or 0) > 0]
            losses = [t for t in history if (t.get('rr_actual') or 0) <= 0 and t.get('rr_actual') is not None]
            lines.append(f"\nClosed trades this week: {len(history)} total "
                         f"({len(wins)} wins / {len(losses)} losses)")
            for t in history[:6]:  # cap at 6 for brevity
                asset = t.get('asset', '?')
                direction = t.get('direction', '?')
                rr = t.get('rr_actual')
                rr_str = f" R={rr:+.2f}" if rr is not None else ""
                closed = str(t.get('closed_at', '?'))[:10]
                setup = t.get('setup_type', '')
                setup_str = f" [{setup}]" if setup else ""
                lines.append(f"  {closed} {asset} {direction}{setup_str}{rr_str}")
    except Exception as e:
        log.debug("Weekly trades fetch failed (non-fatal): %s", e)

    if not lines:
        return "none this week"

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…(truncated)"
    return text


# The freshness threshold for the weekly digest's source
# reports. 'weekly' runs Fridays; 7 days comfortably covers one missed cycle
# without being so loose it would still call a month-old pipeline "fresh".
WEEKLY_SOURCE_STALE_DAYS = 7


def _fetch_weekly_source_reports(report_type="general", limit=5):
    """Fetch the report rows backing the weekly digest's narrative arc.

    'general' is the report type that keeps generating daily regardless of
    ENABLE_MORNING_REPORT (see _generate_general_report); a fixed 'morning'
    fetch would feed the same stale rows into every weekly digest and
    re-narrate old news as current.

    Returns the raw list of report dicts, newest-first (per
    content_db.get_all_reports), or [] on any fetch failure / bad response
    shape — never raises, matching every other _fetch_* helper in this file.
    """
    try:
        resp = requests.get(
            f"{DASHBOARD_BASE_URL}/api/report/morning/recent",
            params={"limit": limit, "type": report_type},
            headers=_API_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        reports = resp.json()
        return reports if isinstance(reports, list) else []
    except Exception as e:
        log.error("Failed to fetch recent %s reports for weekly digest: %s", report_type, e)
        return []


def _parse_report_created_at(raw):
    """Parse a report row's created_at into a naive UTC datetime, or None.

    get_all_reports() returns RFC-2822 strings ("Fri, 04 Sep 2026 06:06:19 GMT")
    — fromisoformat alone rejects them, which would read every source as
    stale and put a false STALE warning in the weekly digest prompt. ISO
    stays accepted for callers that pass it.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", ""))
    except (ValueError, TypeError):
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(text)
        except (ValueError, TypeError, IndexError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def _weekly_source_staleness(reports, max_age_days=WEEKLY_SOURCE_STALE_DAYS, now=None):
    """Age-check the report rows backing the weekly digest.

    ``reports`` is newest-first (get_all_reports' ORDER BY created_at DESC),
    so reports[0] is the freshest row. A missing/unparseable timestamp and an
    empty list both count as stale — mirrors carrac/mcp/tools/reports.py's
    _is_stale idiom (its FRESHNESS FALLBACK docstring): never vouch for
    freshness that cannot be verified.

    Returns (is_stale: bool, age_days: float|None, freshest_created_at: str|None).
    """
    if not reports:
        return True, None, None
    freshest = reports[0] or {}
    created_raw = freshest.get("created_at")
    if not created_raw:
        return True, None, None
    created = _parse_report_created_at(created_raw)
    if created is None:
        return True, None, created_raw
    now = now or datetime.utcnow()
    age_days = (now - created).total_seconds() / 86400.0
    return age_days > max_age_days, age_days, created_raw


def _weekly_staleness_note(age_days, freshest_created_at, max_age_days=WEEKLY_SOURCE_STALE_DAYS):
    """Human/LLM-facing staleness callout for a stale weekly digest source.

    Mirrors the MCP report tools' documented FRESHNESS FALLBACK idiom
    (carrac/mcp/tools/reports.py) — never let a dead-pipeline source pass as
    current without saying so, in terms the LLM writing the digest can act on.
    """
    age_str = f"{age_days:.1f}" if age_days is not None else "unknown"
    return (
        f"the freshest source report behind this digest is {age_str} days old "
        f"(created {freshest_created_at or 'unknown'}), older than the "
        f"{max_age_days}-day freshness threshold — its generating pipeline "
        f"may have stalled."
    )


def _build_weekly_narrative_arc(reports):
    """Build the Daily Narrative Arc text from report rows.

    Prefers each report's audio_script (the morning-report TTS "Key Points"
    display script) when present; falls back to its summary since general
    reports — the source now — carry no audio_script (no
    TTS phase runs for them, see _generate_general_report) but do get a
    TL;DR summary via _extract_tldr_summary/_save_summary. Returned in
    chronological (oldest-first) order; ``reports`` itself is newest-first,
    the API's native order.
    """
    arc_parts = []
    for r in reports:
        r = r or {}
        script = r.get("audio_script") or r.get("summary") or ""
        if script and len(script) > 20:
            date_str = str(r.get("created_at", "unknown"))[:10]
            arc_parts.append(f"### {date_str}\n{script}")
    arc_parts.reverse()  # Chronological order (API returns newest-first)
    return "\n\n".join(arc_parts)


def run_weekly(dry_run=False):
    """Run weekly digest — Sonnet with daily narrative arc."""
    t_start = time.time()
    log.info("=== Weekly Digest ===")

    grok = GrokClient()
    sonnet = MorningAnthropicClient(model=ANTHROPIC_MODEL_FAST)

    # Fetch recent reports — 'general' is the report type
    # that still generates daily even with ENABLE_MORNING_REPORT=false (see
    # _fetch_weekly_source_reports's docstring for why 'morning' was wrong).
    reports = _fetch_weekly_source_reports(report_type="general")

    if not reports:
        log.warning("No recent reports found for weekly digest")
        return

    # Staleness guard — independent of which type fed this. If the newest
    # source report is older than the freshness threshold the generating
    # pipeline has likely stalled. Logged loudly
    # AND surfaced in the digest's own header/prompt context so the LLM
    # cannot present stale material as current — mirrors the MCP report
    # tools' documented FRESHNESS FALLBACK idiom (carrac/mcp/tools/reports.py).
    is_stale, age_days, freshest_created_at = _weekly_source_staleness(reports)
    staleness_note = None
    if is_stale:
        staleness_note = _weekly_staleness_note(age_days, freshest_created_at)
        log.error("Weekly digest source reports are STALE: %s", staleness_note)

    # Fetch fresh chart data for weekly close levels
    chart_data, _ = fetch_chart_data()

    # Extract daily narrative arc from audio_scripts/summaries
    narrative_arc = _build_weekly_narrative_arc(reports)
    if narrative_arc:
        log.info("Weekly narrative arc: %d chars", len(narrative_arc))

    # Assemble reports (truncated data for reference)
    reports_text = ""
    for r in reports:
        date = r.get("created_at", "unknown")
        content = r.get("content", "")[:8000]
        reports_text += f"\n---\n## Report: {date}\n{content}\n"

    # Fetch current derivatives snapshot for weekly context
    options_data, options_claims = fetch_options_data()
    # The permitted-data positioning block. Rides ALONGSIDE the
    # Deribit block rather than replacing the variable — the two are
    # different reads (positioning vs volatility) and the Deribit one is
    # still assembled for the operator. Its grounding claims are merged into
    # options_claims so the existing _grounded_field_tripwire calls cover
    # both without a second wiring point to forget.
    permitted_deriv, _permitted_deriv_claims = fetch_permitted_derivatives()
    options_claims = list(options_claims or []) + _permitted_deriv_claims

    # Fetch this week's play and trade activity for the Play Review section.
    # Uses direct DB imports — same pattern as the morning engine personal section.
    # Cap payload at ~3K chars so it doesn't crowd out the narrative arc.
    play_review_text = _fetch_weekly_play_review_text()

    # COT (CFTC Commitment of Traders) — weekly cadence, inject for Positioning
    # and Next Week Setup sections. Stretched series surfaced first.
    weekly_cot = fetch_cot()

    # Staleness surfaced in the briefing's OWN header context too, not just
    # the system prompt — a header the model reads first, right before the
    # material it warns about, is harder to write around than an instruction
    # buried earlier in the system prompt.
    staleness_header = (
        f"## DATA FRESHNESS WARNING\n{staleness_note}\n\n" if staleness_note else ""
    )

    briefing = f"""{staleness_header}## Daily Narrative Arc (Overview Briefings)
{narrative_arc if narrative_arc else "(No daily overview scripts available this week)"}

## This Week's Morning Reports (Data)
{reports_text}

## Current Weekly Close Data
{chart_data}

## Current Derivatives Regime Snapshot
{options_data}

{permitted_deriv}

## Play & Trade Activity This Week
{play_review_text}

## Smart-Money Positioning (CFTC COT)
{weekly_cot if weekly_cot else "(COT data unavailable this week)"}
"""

    if dry_run:
        log.info("DRY RUN — weekly briefing: %d chars from %d reports, narrative=%d chars",
                 len(briefing), len(reports), len(narrative_arc))
        return

    try:
        from carrac.grounding import grounded_system_prompt as _gsp_wk
        result = sonnet.chat(
            system_prompt=_gsp_wk(weekly_digest_prompt(staleness_note=staleness_note)),
            user_message=briefing,
            max_tokens=16000,
            temperature=0.7,
            timeout=300,
        )
        content = result["content"]
    except Exception as e:
        log.error("Weekly digest failed: %s", e)
        return

    # Weekly had NO tripwires at all despite being one of the
    # longest, most synthesis-heavy reports (traces a thesis across 7 weeks,
    # grades what worked) — arguably the highest hallucination risk of the
    # set. Same non-blocking idiom as personalized/shared (calendar, advice,
    # news, data-release, leadership); no confluence/scenario tripwire here —
    # weekly has no "Levels in Focus" / scenario section those are scoped to.
    _calendar_tripwire(content, "weekly report")
    _advice_tripwire(content, "weekly report")
    _news_tripwire(content, "weekly report")
    _data_release_tripwire(content, "weekly report")
    _leadership_tripwire(content, "weekly report")

    # Haiku second-opinion fact check (non-blocking) against the
    # SAME briefing (daily narrative arc + this week's reports + fresh chart/
    # derivatives/COT data) the weekly digest was generated from.
    _verify_tripwire(content, briefing, "weekly report", "weekly")
    _internal_arithmetic_tripwire(content, "weekly report")
    _grounded_field_tripwire(content, options_claims, "weekly report")

    report_id = _post_report(content, report_type="weekly",
                             generation_time_ms=int((time.time() - t_start) * 1000),
                             source_briefing=briefing)

    # Save weekly summary — first paragraph of Week in Review (no LLM call)
    if report_id:
        wk_summary = _extract_afternoon_summary(content)
        if wk_summary:
            _save_summary(report_id, wk_summary)
            log.info("Weekly summary saved: %d chars", len(wk_summary))

    total_cost = grok.get_usage_summary()["total_cost"] + sonnet.get_usage_summary()["total_cost"]
    log.info("Weekly digest: %.1fs, $%.4f, %d chars",
             time.time() - t_start, total_cost, len(content))

    # A digest is a feed item, not an alarm — market topic, not the ops one.
    _notify("Weekly Digest Ready",
            f"Report #{report_id} | ${total_cost:.3f}",
            topic=NTFY_MARKET_TOPIC)

    _log_engine_costs("weekly", sonnet=sonnet, grok=grok)

    return report_id


# ==========================================================================
# WEEK-AHEAD — Sunday forward-looking briefing, region-batched
# ==========================================================================

def run_week_ahead(region, dry_run=False):
    """Sunday Week-Ahead briefing for one delivery region.

    Forward-looking sibling to run_weekly (which is retrospective Friday).
    Region-batched like the daily dispatch but on a coarser 3-bucket split
    (europe/americas/asia); the daily dispatch itself uses 5 regions
    (europe/us_east/us_west/asia_pacific/asia).
    Per-user opt-in via prefs.weekly_ahead_enabled.
    """
    from .data_assembly import assemble_week_ahead_context, build_week_ahead_shared_briefing
    from .prompts import week_ahead_shared_prompt, week_ahead_personal_prompt
    from .config import MAX_USER_REPORT_TOKENS, MAX_USER_REPORT_TOKENS_MEMBER

    log.info("=== Week-Ahead Briefing — region=%s ===", region)
    t_start = time.time()

    sonnet = MorningAnthropicClient(model=ANTHROPIC_MODEL_FAST)
    grok = GrokClient()  # for TTS only

    # Eligible users (opt-in + region filter)
    eligible_users = _get_eligible_users(region=region, mode="week_ahead")
    log.info("Week-ahead: %d eligible users in region=%s", len(eligible_users), region)

    if not eligible_users and not dry_run:
        log.info("No opted-in users for week-ahead in %s — skipping", region)
        return

    # Assemble shared context
    week_ctx = assemble_week_ahead_context()
    shared_briefing_parts = build_week_ahead_shared_briefing(week_ctx)
    briefing = "\n".join(shared_briefing_parts)

    # Steering text reuse. Not wrapped: a malformed steering file raises in
    # the loader, and that must stop this run as loudly as it stops the
    # daily one rather than quietly drop the framework.
    steering_text = _load_steering_yaml()[:1500]

    if dry_run:
        log.info("DRY RUN — week-ahead briefing: %d chars, %d users",
                 len(briefing), len(eligible_users))
        return

    # Generate shared report
    from carrac.grounding import grounded_system_prompt as _gsp_wa
    sys_prompt = _gsp_wa(week_ahead_shared_prompt(steering_text=steering_text))
    try:
        result = _call_with_retry(
            sonnet.chat,
            system_prompt=sys_prompt,
            user_message=f"Generate the Sunday Week-Ahead briefing.\n\n{briefing}",
            max_tokens=4000,
            temperature=0.7,
            timeout=300,
            max_attempts=3,
            label=f"week_ahead_shared[{region}]",
        )
        shared_report = result["content"]
        # 8000 matches the prompt's own declared "HARD LIMIT 8,000"
        # (week_ahead_shared_prompt, prompts.py) — the budget itself isn't
        # wrong, the model just routinely overshoots its own stated target
        # (4,000-6,000) and DEFAULT_DROP_ORDER's daily-report vocabulary never
        # matched week_ahead's real headings, so every overage fell straight
        # to blunt truncation instead of a graceful section drop. Passing
        # WEEK_AHEAD_DROP_ORDER fixes that: real headings get dropped
        # least-valuable-first before any hard truncation is needed.
        shared_report = _enforce_char_limit(
            shared_report, 8000,
            drop_order=_WEEK_AHEAD_DROP_ORDER,
            log_callback=lambda m, _r=region: log.warning("week_ahead[%s] %s", _r, m),
        )
    except Exception as e:
        log.error("Week-ahead shared generation failed for %s: %s", region, e)
        _notify("Week-Ahead ABORTED", f"Generation failed for {region}: {e}")
        return

    # week_ahead had NO tripwires at all — same non-blocking
    # idiom as the shared/personalized morning reports (calendar, advice,
    # news, data-release, leadership). No confluence tripwire: that's scoped
    # to the "Levels in Focus" section which week_ahead doesn't have (its
    # forward-looking analog is "Setup Watch", built from scanner data, not
    # the confluence-focus block _confluence_tripwire checks).
    _calendar_tripwire(shared_report, f"week_ahead shared report [{region}]")
    _advice_tripwire(shared_report, f"week_ahead shared report [{region}]")
    _news_tripwire(shared_report, f"week_ahead shared report [{region}]")
    _data_release_tripwire(shared_report, f"week_ahead shared report [{region}]")
    _leadership_tripwire(shared_report, f"week_ahead shared report [{region}]")

    # Haiku second-opinion fact check (non-blocking) against the
    # SAME assembled week-ahead briefing the shared report was generated from.
    _verify_tripwire(shared_report, briefing, f"week_ahead shared report [{region}]", "week_ahead")
    _internal_arithmetic_tripwire(shared_report, f"week_ahead shared report [{region}]")
    # No fetch_options_data() call in this path — week_ahead has no derivatives
    # snapshot to ground claims against, so this is an unconditional no-op.
    _grounded_field_tripwire(shared_report, [], f"week_ahead shared report [{region}]")

    # Save shared report
    report_id = _post_report(content=shared_report, report_type="week_ahead",
                              generation_time_ms=int((time.time() - t_start) * 1000),
                              source_briefing=briefing)
    if not report_id:
        log.error("Week-ahead: failed to save shared report")
        return

    # Save week-ahead summary — first paragraph of Week Setup section (no LLM call)
    wa_summary = _extract_afternoon_summary(shared_report)
    if wa_summary:
        _save_summary(report_id, wa_summary)
        log.info("Week-ahead summary saved: %d chars", len(wa_summary))

    log.info("Week-ahead shared report saved: #%s (%d chars)", report_id, len(shared_report))

    # Per-user personal sections (Premium only — Member gets shared only)
    for u in eligible_users:
        uid = u["id"]
        tier = u["tier"]
        if tier == "member":
            continue  # Member sees shared only — no personal
        try:
            sources = _get_user_analyst_sources(uid)
            user_ctx_text = (
                f"## Trader Profile\n"
                f"{_escape_user_text(sources.get('trader_profile', ''), 'trader_profile')}\n\n"
                f"## Shared Week-Ahead\n{shared_report[:4000]}\n"
            )
            mr_prefs = sources.get("morning_report_prefs", {})
            da_intensity = mr_prefs.get("devilsAdvocate", "medium")
            personal_sys = _gsp_wa(week_ahead_personal_prompt(user_ctx_text, da_intensity))
            max_tok = MAX_USER_REPORT_TOKENS if tier in ("premium", "admin") else MAX_USER_REPORT_TOKENS_MEMBER
            personal_result = _call_with_retry(
                sonnet.chat,
                system_prompt=personal_sys,
                user_message="Generate the personalized week-ahead briefing for this trader.",
                max_tokens=max_tok,
                temperature=0.7,
                timeout=180,
                max_attempts=2,
                label=f"week_ahead_personal[user={uid}]",
            )
            personal_content = _enforce_char_limit(
                personal_result["content"], 16000,
                log_callback=lambda m, _u=uid: log.warning("week_ahead user=%s %s", _u, m),
            )
            if personal_result.get("stop_reason") == "max_tokens":
                personal_content = _trim_dangling_sentence(personal_content)

            # Same coverage as the shared week_ahead report above.
            _calendar_tripwire(personal_content, f"week_ahead personal report (user {uid})")
            # GATE, not a log-only tripwire, on the personal path.
            personal_content = _advice_gate(personal_content, f"week_ahead personal report (user {uid})")
            _news_tripwire(personal_content, f"week_ahead personal report (user {uid})")
            _data_release_tripwire(personal_content, f"week_ahead personal report (user {uid})")
            _leadership_tripwire(personal_content, f"week_ahead personal report (user {uid})")

            # Haiku second-opinion fact check (non-blocking) —
            # source is what the personal prompt actually received (trader
            # profile + the shared week-ahead report), not the full shared
            # briefing, which this per-user call never sees.
            _verify_tripwire(personal_content, user_ctx_text,
                             f"week_ahead personal report (user {uid})", "week_ahead_personal")

            cost = max(1, int(personal_result["usage"].get("cost", 0) * 100))
            _save_personal_section(report_id, uid, personal_content, cost_cents=cost,
                                   input_tokens=personal_result["usage"].get("input_tokens", 0),
                                   output_tokens=personal_result["usage"].get("output_tokens", 0))
            if tier not in ("admin", "tester"):
                _debit_credits(
                    user_id=uid, amount_cents=cost,
                    feature="week_ahead_personal",
                    model="claude-sonnet-4-6",
                    input_tokens=personal_result["usage"].get("input_tokens", 0),
                    output_tokens=personal_result["usage"].get("output_tokens", 0),
                )
        except Exception as e:
            log.error("Week-ahead personal failed for user %s: %s", uid, e)

    elapsed = time.time() - t_start
    log.info("Week-ahead complete for %s: %.1fs", region, elapsed)
    _notify("Week-Ahead Ready", f"Region={region} report=#{report_id}")
    _log_engine_costs("week_ahead", sonnet=sonnet, grok=grok)


# ==========================================================================
# CLI
# ==========================================================================

def main():
    parser = argparse.ArgumentParser(description="Morning Engine — AI-generated market reports")
    parser.add_argument("--mode",
                        choices=["morning", "afternoon", "weekly", "dispatch", "week_ahead"],
                        default="morning", help="Report mode")
    parser.add_argument("--region",
                        choices=["europe", "us_east", "us_west", "asia_pacific", "asia", "americas"],
                        help="Region for dispatch / week_ahead mode")
    parser.add_argument("--dry-run", action="store_true",
                        help="Assemble data but don't call the LLM provider or save report")
    args = parser.parse_args()

    if args.mode == "morning":
        run_morning(dry_run=args.dry_run)
    elif args.mode == "afternoon":
        run_afternoon(dry_run=args.dry_run)
    elif args.mode == "weekly":
        run_weekly(dry_run=args.dry_run)
    elif args.mode == "dispatch":
        if not args.region:
            parser.error("--region is required for dispatch mode")
        run_dispatch(region=args.region, dry_run=args.dry_run)
    elif args.mode == "week_ahead":
        if not args.region:
            parser.error("--region is required for week_ahead mode")
        run_week_ahead(region=args.region, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
