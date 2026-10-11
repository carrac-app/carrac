"""
AI-powered journal review — trade review, session summary, weekly review, NL queries.

Uses the same OpenAI-compatible LLM as the chat feature.
Credit billing follows reserve/settle pattern.
"""

import json
import logging
import os
import re
from datetime import datetime

from carrac.ai.billing import (
    check_and_reserve,
    sanitize_for_llm,
    settle,
)
from carrac.ai.context_builder import build_trader_profile_context
from carrac.ai.copy import with_advice_ban
from carrac.ai.llm_client import stream_chat

logger = logging.getLogger('carrac.journal.ai_review')


def _complete_chat(messages, max_tokens=2000):
    """Non-streaming chat completion. Returns (text, usage_dict)."""
    text_parts = []
    usage = {}
    for chunk, usage_data in stream_chat(messages, max_tokens):
        if chunk:
            text_parts.append(chunk)
        if usage_data:
            usage = usage_data
    return ''.join(text_parts), usage


def _parse_json_response(text):
    """Extract JSON from LLM response, handling markdown code blocks."""
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if match:
        text = match.group(1)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        for start, end in [('{', '}'), ('[', ']')]:
            idx_start = text.find(start)
            idx_end = text.rfind(end)
            if idx_start >= 0 and idx_end > idx_start:
                try:
                    return json.loads(text[idx_start:idx_end + 1])
                except json.JSONDecodeError:
                    continue
        return None


# ── Prompt builders ──
#
# Every journal prompt describes the user's OWN recorded behaviour — process,
# patterns, rule adherence — and never tells them what to trade. Each carries
# the non-advice ban (with_advice_ban); tests/test_prompt_advice_ban.py checks
# them all. The JSON keys below are only rendered to markdown here; nothing
# reads them back from the database.

_OBSERVE_NOT_INSTRUCT = (
    "Everything you write is an observation about the user's own recorded "
    "trading — what the journal shows — never an instruction about what to "
    "trade, when to enter or exit, or how large to size."
)


def _trade_review_prompt():
    return with_advice_ban(
        "You are reviewing a closed trade from the user's own trading journal. "
        "Describe what the record shows about the user's own process: how the trade "
        "followed (or departed from) the setup, plan and rules they recorded, how the "
        "execution and exit compare with their own stop, target and the excursion data "
        "(MFE/MAE), and which of their recorded behaviours stand out. Be honest — note "
        "what went to plan as well as where the trade departed from it. "
        "Reference the market context data when describing setup quality and how "
        "conditions changed between entry and exit. "
        + _OBSERVE_NOT_INSTRUCT + "\n\n"
        "RESPOND WITH VALID JSON ONLY (no text before or after):\n"
        "{\n"
        '  "summary": "2-3 sentence narrative of what happened in this trade",\n'
        '  "exit_critique": "How the exit compares with the user\'s own stop/target and with MFE/MAE",\n'
        '  "grade": {"setup": <1-10>, "execution": <1-10>, "exit": <1-10>, "discipline": <1-10>},\n'
        '  "suggested_mistakes": ["mistake_tag1"],\n'
        '  "observations": ["observation about the user\'s own process or rule adherence", "observation 2", "observation 3"]\n'
        "}\n\n"
        "VALID MISTAKE TAGS: fomo, revenge, chasing, moved_stop, oversized, early_entry, "
        "late_entry, early_exit, against_trend, no_setup, boredom, news_trade, overtraded\n"
        "Only suggest mistakes clearly evident from the data. Empty array if trade was clean."
    )


def _session_summary_prompt():
    return with_advice_ban(
        "You are reviewing a trading session (a day's trades) from the user's own journal. "
        "Describe the session holistically — the user's own behavioural patterns and rule "
        "adherence, not individual trade calls. "
        "Use the market context scores to describe how the trades lined up with market conditions. "
        + _OBSERVE_NOT_INSTRUCT + "\n\n"
        "RESPOND WITH VALID JSON ONLY:\n"
        "{\n"
        '  "summary": "2-3 sentence overview of the session",\n'
        '  "themes": ["behavioral theme 1", "theme 2"],\n'
        '  "rule_adherence_pct": <0-100>,\n'
        '  "key_observation": "The single most important thing the session record shows",\n'
        '  "pattern_to_watch": "The recurring behaviour from this session most worth noticing next time"\n'
        "}"
    )


def _weekly_review_prompt(period_label):
    return with_advice_ban(
        f"You are describing the user's own {period_label} trading record from their journal. "
        "Be direct and data-driven. Describe patterns, not individual trades. "
        "Use the market context correlation data to describe which conditions the user's "
        "own results were strongest and weakest in. "
        + _OBSERVE_NOT_INSTRUCT + "\n\n"
        "RESPOND WITH VALID JSON ONLY:\n"
        "{\n"
        '  "period_summary": "2-3 sentences with key stats",\n'
        '  "best_performing": "What the record shows worked best, and the evidence",\n'
        '  "worst_pattern": "Most costly recorded pattern/mistake and its R impact",\n'
        '  "behavioral_trend": "Is rule adherence improving, stable, or declining? Evidence?",\n'
        '  "observations_next_week": ["recorded pattern worth noticing 1", "pattern 2", "pattern 3"]\n'
        "}"
    )


def _nl_interpret_prompt():
    return with_advice_ban(
        "You are describing results from the user's own trading journal. The user asked a "
        "question about their journal, and a query returned the results below. Interpret "
        "them clearly and concisely (2-5 sentences). Include specific numbers. Describe the "
        "patterns the data shows. " + _OBSERVE_NOT_INSTRUCT
    )


def review_trade(db, trade_id, user_id, is_internal: bool = False):
    """Generate AI review for a closed trade.

    is_internal: passed through to settle() so an
    internal-flagged API key's review calls don't attribute spend in
    the weekly usage review. Only trade_get_ai_review (MCP) ever passes True;
    the browser route always uses the default False.

    Returns dict with review results or error.
    """
    trade = db.get_journal_entry(trade_id)
    if not trade or trade.get('user_id') != user_id:
        return {'error': 'Trade not found'}
    if not trade.get('closed_at'):
        return {'error': 'Trade must be closed for review'}

    estimate = int(os.getenv('AI_JOURNAL_REVIEW_ESTIMATE', '25'))
    if not check_and_reserve(user_id, estimate):
        return {'error': 'Insufficient AI credits'}

    try:
        trader_context = build_trader_profile_context(user_id)
        recent = db.get_trade_history(user_id, days=30, limit=10)
        recent_context = _format_recent_trades(recent)
        snapshots = db.get_score_snapshots(trade_id)
        score_context = _format_score_context(snapshots)
        trade_context = _format_trade_for_review(trade)

        system_prompt = _trade_review_prompt()

        # Grounding preamble so macro claims in the review can't leak
        # training-era Fed/CPI/index data. The non-advice ban is already in
        # the base prompt, so a grounding failure never drops it.
        try:
            from carrac.grounding import grounded_system_prompt
            system_prompt = grounded_system_prompt(system_prompt)
        except Exception as _e:
            logger.debug("Grounding preamble unavailable for trade review: %s", _e)

        if trader_context:
            system_prompt += f"\n\n--- TRADER PROFILE ---\n{trader_context}"

        user_content = trade_context
        if score_context:
            user_content += f"\n\n--- MARKET CONTEXT (12-point scorer) ---\n{score_context}"
        if recent_context:
            user_content += f"\n\n--- RECENT TRADES (context) ---\n{recent_context}"

        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_content},
        ]

        response_text, usage = _complete_chat(messages, max_tokens=1500)

        parsed = _parse_json_response(response_text)
        input_tokens = usage.get('prompt_tokens', 0)
        output_tokens = usage.get('completion_tokens', 0)

        if not parsed:
            actual_cost = settle(
                user_id, estimate, input_tokens, output_tokens,
                feature='journal_review', asset=trade.get('asset'), usage=usage,
                is_internal=is_internal,
            )
            db.trade_update(trade_id, ai_review=response_text)
            return {'review': response_text, 'charged': actual_cost, 'parse_error': True}

        # Build markdown from parsed JSON
        md_parts = []
        if parsed.get('summary'):
            md_parts.append(f"**Summary**: {parsed['summary']}")
        if parsed.get('exit_critique'):
            md_parts.append(f"\n**Exit Analysis**: {parsed['exit_critique']}")
        if parsed.get('observations'):
            md_parts.append("\n**Observations**:")
            for obs in parsed['observations']:
                md_parts.append(f"- {obs}")

        review_md = '\n'.join(md_parts)

        grade = parsed.get('grade', {})
        grade_json = json.dumps(grade) if grade else None

        updates = {'ai_review': review_md}
        if grade_json:
            updates['trade_grade'] = grade_json
        db.trade_update(trade_id, **updates)

        actual_cost = settle(
            user_id, estimate, input_tokens, output_tokens,
            feature='journal_review', asset=trade.get('asset'), usage=usage,
            is_internal=is_internal,
        )

        return {
            'review': review_md,
            'grade': grade,
            'suggested_mistakes': parsed.get('suggested_mistakes', []),
            'charged': actual_cost,
        }

    except RuntimeError:
        from user_db import user_db
        user_db.refund_ai_credits(user_id, estimate)
        return {'error': 'AI service temporarily unavailable'}
    except Exception as e:
        from user_db import user_db
        user_db.refund_ai_credits(user_id, estimate)
        logger.error(f"AI review error for trade {trade_id}: {e}")
        return {'error': 'Review generation failed'}


def summarize_session(db, session_id, user_id):
    """Generate AI summary for a trading session."""
    sess = db.get_session(session_id)
    if not sess or sess.get('user_id') != user_id:
        return {'error': 'Session not found'}

    trades = db.get_session_trades(session_id)
    if not trades:
        return {'error': 'No trades in this session'}

    estimate = int(os.getenv('AI_SESSION_SUMMARY_ESTIMATE', '25'))
    if not check_and_reserve(user_id, estimate):
        return {'error': 'Insufficient AI credits'}

    try:
        trader_context = build_trader_profile_context(user_id)
        trade_snapshots = {}
        for t in trades:
            tid = t.get('id')
            if tid:
                snaps = db.get_score_snapshots(tid)
                if snaps:
                    trade_snapshots[tid] = snaps
        session_context = _format_session_for_summary(sess, trades, trade_snapshots)

        system_prompt = _session_summary_prompt()

        if trader_context:
            system_prompt += f"\n\n--- TRADER PROFILE ---\n{trader_context}"

        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': session_context},
        ]

        response_text, usage = _complete_chat(messages, max_tokens=1000)
        parsed = _parse_json_response(response_text)

        if parsed:
            md_parts = []
            if parsed.get('summary'):
                md_parts.append(parsed['summary'])
            if parsed.get('themes'):
                md_parts.append("\n**Themes**: " + ', '.join(parsed['themes']))
            if parsed.get('rule_adherence_pct') is not None:
                md_parts.append(f"\n**Rule Adherence**: {parsed['rule_adherence_pct']}%")
            if parsed.get('key_observation'):
                md_parts.append(f"\n**Key Observation**: {parsed['key_observation']}")
            if parsed.get('pattern_to_watch'):
                md_parts.append(f"\n**Pattern to Watch**: {parsed['pattern_to_watch']}")
            summary_md = '\n'.join(md_parts)
        else:
            summary_md = response_text

        db.update_session(session_id, ai_summary=summary_md)

        actual_cost = settle(
            user_id, estimate,
            usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0),
            feature='session_summary', usage=usage,
        )

        return {'summary': summary_md, 'parsed': parsed, 'charged': actual_cost}

    except RuntimeError:
        from user_db import user_db
        user_db.refund_ai_credits(user_id, estimate)
        return {'error': 'AI service temporarily unavailable'}
    except Exception as e:
        from user_db import user_db
        user_db.refund_ai_credits(user_id, estimate)
        logger.error(f"Session summary error for {session_id}: {e}")
        return {'error': 'Summary generation failed'}


def weekly_review(db, user_id, days=7):
    """Generate AI weekly/monthly review."""
    analytics = db.get_journal_analytics(user_id, days=days)
    if not analytics or analytics.get('trades', 0) < 3:
        return {'error': 'Need at least 3 trades for a review'}

    estimate = int(os.getenv('AI_WEEKLY_REVIEW_ESTIMATE', '35'))
    if not check_and_reserve(user_id, estimate):
        return {'error': 'Insufficient AI credits'}

    try:
        trader_context = build_trader_profile_context(user_id)
        period_label = f"{days}-day"
        analytics_context = _format_analytics_for_review(analytics, period_label)

        # Get previous period for comparison (offset window, not overlapping)
        prev_analytics = db.get_journal_analytics_range(user_id, days_ago_start=days * 2, days_ago_end=days)
        if prev_analytics and prev_analytics.get('trades', 0) >= 3:
            analytics_context += f"\n\nPrevious {period_label}: {prev_analytics['trades']} trades, "
            analytics_context += f"WR: {prev_analytics.get('win_rate', 0)}%, "
            analytics_context += f"Expectancy: {prev_analytics.get('expectancy', 0):.2f}R"

        # Add market context correlation
        from datetime import timedelta
        min_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        snapshots_with_outcomes = db.get_all_snapshots_with_outcomes(user_id, min_date=min_date)
        regime_context = _format_regime_performance(snapshots_with_outcomes)
        if regime_context:
            analytics_context += f"\n\n--- MARKET CONTEXT CORRELATION ---\n{regime_context}"

        system_prompt = _weekly_review_prompt(period_label)

        # Grounding preamble (the ban is in the base prompt already).
        try:
            from carrac.grounding import grounded_system_prompt
            system_prompt = grounded_system_prompt(system_prompt)
        except Exception as _e:
            logger.debug("Grounding preamble unavailable for weekly review: %s", _e)

        if trader_context:
            system_prompt += f"\n\n--- TRADER PROFILE ---\n{trader_context}"

        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': analytics_context},
        ]

        response_text, usage = _complete_chat(messages, max_tokens=1500)
        parsed = _parse_json_response(response_text)

        if parsed:
            md_parts = []
            if parsed.get('period_summary'):
                md_parts.append(f"## {period_label.title()} Review\n\n{parsed['period_summary']}")
            if parsed.get('best_performing'):
                md_parts.append(f"\n**What Worked**: {parsed['best_performing']}")
            if parsed.get('worst_pattern'):
                md_parts.append(f"\n**What Didn't**: {parsed['worst_pattern']}")
            if parsed.get('behavioral_trend'):
                md_parts.append(f"\n**Behavioral Trend**: {parsed['behavioral_trend']}")
            if parsed.get('observations_next_week'):
                md_parts.append("\n**Patterns to Watch Next Week**:")
                for item in parsed['observations_next_week']:
                    md_parts.append(f"- {item}")
            review_md = '\n'.join(md_parts)
        else:
            review_md = response_text

        actual_cost = settle(
            user_id, estimate,
            usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0),
            feature='weekly_review', usage=usage,
        )

        return {
            'review': review_md,
            'parsed': parsed,
            'charged': actual_cost,
            'period_days': days,
        }

    except RuntimeError:
        from user_db import user_db
        user_db.refund_ai_credits(user_id, estimate)
        return {'error': 'AI service temporarily unavailable'}
    except Exception as e:
        from user_db import user_db
        user_db.refund_ai_credits(user_id, estimate)
        logger.error(f"Weekly review error for user {user_id}: {e}")
        return {'error': 'Review generation failed'}


def _nl_sql_prompt():
    """Dialect-aware system prompt for the NL→SQL step.

    The generated SQL is executed against the LIVE backend (SQLite in tests,
    Postgres in production — see ``carrac.db_pool.get_conn``). The prompt MUST steer
    the model to that dialect's parameter marker and date/JSON functions:
    ``:user_id`` is a Postgres *syntax error* and ``strftime``/``json_extract``
    don't exist there, so a SQLite-only prompt makes every query 500 in production
    (swallowed as "Query execution failed" while still charging the user).
    Pins the SQLite/Postgres dialect class: prompts must match the live DB dialect.
    """
    from carrac.db_pool import get_db_type

    pg = get_db_type() == 'postgresql'
    dialect = 'PostgreSQL' if pg else 'SQLite'
    param = '%(user_id)s' if pg else ':user_id'
    if pg:
        date_json = (
            "- Use PostgreSQL syntax: date_trunc()/to_char() for dates. "
            "The tags, mistake_tags and trade_grade columns are TEXT holding "
            "JSON — cast with (column)::jsonb then read via -> / ->>\n"
        )
    else:
        date_json = "- Use SQLite syntax (strftime for dates, json_extract for JSON)\n"
    return with_advice_ban((
        "You are a SQL generator for a trading journal database. "
        f"Generate a {dialect} SELECT query to answer the user's question.\n\n"
        "TABLES:\n"
        "- trade_journal: id, user_id, asset, timeframe, direction (long/short), "
        "entry_price, exit_price, stop_price, target_price, rr_planned, rr_actual, "
        "setup_type, notes, opened_at, closed_at, mfe, mae, exit_efficiency, "
        "mistake_tags (JSON array), tags (JSON array), confidence (1-5), mood (1-5), "
        "session_id, size, fees, pnl_currency, ai_review, trade_grade (JSON)\n"
        "- journal_sessions: id, user_id, date, session_label, pre_notes, post_notes, "
        "mood_pre (1-5), mood_post (1-5), ai_summary\n"
        "- score_snapshots: journal_id (FK to trade_journal.id), user_id, "
        "snapshot_type ('entry'/'exit'), asset, direction, captured_at, "
        "score_trend (0-2), score_momentum (0-2), score_rsi (0-2), "
        "score_structure (0-2), score_level (0-2), score_macro (0-2), "
        "score_total (0-12), verdict ('STRONG'/'GOOD'/'NEUTRAL'/'WEAK'/'AVOID'), "
        "regime, regime_score, fear_greed (0-100), weekly_rsi, funding_rate, "
        "setup_type, setup_confidence\n"
        "  JOIN pattern: score_snapshots.journal_id = trade_journal.id\n\n"
        "RULES:\n"
        f"- Always filter by user_id = {param}\n"
        "- Only SELECT queries, never INSERT/UPDATE/DELETE\n"
        f"- Use {param} as the parameter placeholder\n"
        "- LIMIT results to 100 rows max\n"
        + date_json + "\n"
        "RESPOND WITH VALID JSON ONLY:\n"
        '{"sql": "SELECT ...", "description": "brief description"}'
    ))


def _normalize_nl_sql(sql):
    """Defensive: coerce a stray SQLite ``:user_id`` marker to the live dialect.

    The prompt already asks for the right marker, but if the model emits the
    SQLite ``:user_id`` on Postgres we translate it so the query still runs
    (psycopg2 needs ``%(user_id)s``; ``:user_id`` is a Postgres syntax error).
    """
    from carrac.db_pool import get_db_type

    if get_db_type() == 'postgresql':
        return sql.replace(':user_id', '%(user_id)s')
    return sql


def nl_query_stream(db, user_id, question):
    """Natural language query — generates SQL, executes, interprets results.

    Yields SSE events: {"t": "chunk"} for tokens, {"done": True, ...} at end.
    """
    question = sanitize_for_llm(question, max_length=500)
    if not question:
        yield _sse({'error': 'Question is required'})
        return

    estimate = int(os.getenv('AI_NL_QUERY_ESTIMATE', '20'))
    if not check_and_reserve(user_id, estimate):
        yield _sse({'error': 'Insufficient AI credits'})
        return

    total_input = 0
    total_output = 0

    try:
        # Step 1: Generate SQL (dialect-aware — runs against the live backend)
        sql_prompt = _nl_sql_prompt()

        sql_messages = [
            {'role': 'system', 'content': sql_prompt},
            {'role': 'user', 'content': question},
        ]

        sql_text, sql_usage = _complete_chat(sql_messages, max_tokens=500)
        total_input += sql_usage.get('prompt_tokens', 0)
        total_output += sql_usage.get('completion_tokens', 0)

        sql_parsed = _parse_json_response(sql_text)
        if not sql_parsed or not sql_parsed.get('sql'):
            actual_cost = settle(user_id, estimate, total_input, total_output, feature='nl_query')
            yield _sse({'error': 'Could not generate query', 'charged': actual_cost})
            return

        sql = sql_parsed['sql']

        # Safety: only SELECT, must reference user_id, only allowed tables
        sql_upper = sql.upper().strip()
        if not sql_upper.startswith('SELECT'):
            actual_cost = settle(user_id, estimate, total_input, total_output, feature='nl_query')
            yield _sse({'error': 'Invalid query generated', 'charged': actual_cost})
            return

        dangerous = {
            'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE',
            'ATTACH', 'DETACH', 'PRAGMA', 'VACUUM', 'REINDEX', 'REPLACE',
        }
        # Use word-boundary matching to avoid false positives on substrings
        for kw in dangerous:
            if re.search(r'\b' + kw + r'\b', sql_upper):
                actual_cost = settle(user_id, estimate, total_input, total_output, feature='nl_query')
                yield _sse({'error': 'Unsafe query rejected', 'charged': actual_cost})
                return

        if ':user_id' not in sql and 'user_id' not in sql:
            actual_cost = settle(user_id, estimate, total_input, total_output, feature='nl_query')
            yield _sse({'error': 'Query must filter by user', 'charged': actual_cost})
            return

        # Table whitelist — only allow queries against journal tables
        _ALLOWED_TABLES = {'trade_journal', 'journal_sessions', 'score_snapshots'}
        # Extract table references (FROM/JOIN clauses)
        table_refs = set(re.findall(r'\bFROM\s+(\w+)|\bJOIN\s+(\w+)', sql_upper))
        referenced = {t.lower() for pair in table_refs for t in pair if t}
        if referenced - _ALLOWED_TABLES:
            actual_cost = settle(user_id, estimate, total_input, total_output, feature='nl_query')
            yield _sse({'error': 'Query references unauthorized tables', 'charged': actual_cost})
            return

        # Execute (RAII pool conn avoids pinning a pool slot)
        # Coerce a stray SQLite ':user_id' marker to the live dialect first.
        sql = _normalize_nl_sql(sql)
        from carrac.db_pool import get_conn as _pool_get_conn
        try:
            with _pool_get_conn() as _conn:
                rows = _conn.execute(sql, {'user_id': user_id}).fetchall()
            results = [dict(r) for r in rows[:100]]
        except Exception:
            actual_cost = settle(user_id, estimate, total_input, total_output, feature='nl_query')
            yield _sse({'error': 'Query execution failed', 'charged': actual_cost})
            return

        # Step 2: Interpret (streaming)
        results_text = json.dumps(results[:50], default=str)
        if len(results_text) > 5000:
            results_text = results_text[:5000] + '... (truncated)'

        interpret_prompt = _nl_interpret_prompt()

        interpret_messages = [
            {'role': 'system', 'content': interpret_prompt},
            {'role': 'user', 'content': f"Question: {question}\n\nResults ({len(results)} rows):\n{results_text}"},
        ]

        for chunk, usage_data in stream_chat(interpret_messages, max_tokens=800):
            if chunk:
                yield _sse({'t': chunk})
            if usage_data:
                total_input += usage_data.get('prompt_tokens', 0)
                total_output += usage_data.get('completion_tokens', 0)

        actual_cost = settle(user_id, estimate, total_input, total_output, feature='nl_query')
        yield _sse({
            'done': True,
            'charged': actual_cost,
            'query_description': sql_parsed.get('description', ''),
            'row_count': len(results),
        })

    except RuntimeError:
        from user_db import user_db
        user_db.refund_ai_credits(user_id, estimate)
        yield _sse({'error': 'AI service temporarily unavailable'})
    except Exception as e:
        from user_db import user_db
        user_db.refund_ai_credits(user_id, estimate)
        logger.error(f"NL query error: {e}")
        yield _sse({'error': 'Query failed'})


def _sse(data):
    """Format data as SSE event."""
    return f"data: {json.dumps(data)}\n\n"


# ── Formatting helpers ──

def _format_trade_for_review(trade):
    """Format trade data for AI review prompt."""
    lines = []
    lines.append(f"=== TRADE: {trade.get('asset', '?')} {trade.get('direction', '?').upper()} ===")
    lines.append(f"Timeframe: {trade.get('timeframe', '?')}")
    lines.append(f"Entry: ${trade.get('entry_price', 0):,.8g} @ {trade.get('opened_at', '?')}")
    lines.append(f"Exit: ${trade.get('exit_price', 0):,.8g} @ {trade.get('closed_at', '?')}")

    if trade.get('stop_price'):
        lines.append(f"Stop: ${trade['stop_price']:,.8g}")
    if trade.get('target_price'):
        lines.append(f"Target: ${trade['target_price']:,.8g}")
    if trade.get('rr_planned'):
        lines.append(f"Planned R:R: {trade['rr_planned']:.2f}")
    if trade.get('rr_actual') is not None:
        lines.append(f"Actual R: {trade['rr_actual']:+.2f}R ({'WIN' if trade['rr_actual'] > 0 else 'LOSS'})")

    if trade.get('setup_type'):
        # setup_type is user-authored free text — quarantine before prompt
        from carrac.grounding import escape_user_text
        _setup = sanitize_for_llm(str(trade['setup_type'])[:200], max_length=200, source="setup_type")
        lines.append(f"Setup: {escape_user_text(_setup, 'setup_type', max_chars=200)}")
    if trade.get('exit_reason'):
        # exit_reason is user-authored free text — quarantine before prompt
        from carrac.grounding import escape_user_text
        _exit = sanitize_for_llm(str(trade['exit_reason'])[:200], max_length=200, source="exit_reason")
        lines.append(f"Exit reason: {escape_user_text(_exit, 'exit_reason', max_chars=200)}")

    if trade.get('mfe') is not None:
        lines.append(f"MFE: {trade['mfe']:.2f}R (max favorable)")
    if trade.get('mae') is not None:
        lines.append(f"MAE: {trade['mae']:.2f}R (max adverse)")
    if trade.get('exit_efficiency') is not None:
        lines.append(f"Exit efficiency: {trade['exit_efficiency'] * 100:.0f}%")

    if trade.get('checklist_results'):
        try:
            cl = json.loads(trade['checklist_results']) if isinstance(trade['checklist_results'], str) else trade['checklist_results']
            if isinstance(cl, dict):
                total = len(cl)
                passed = sum(1 for v in cl.values() if v)
                lines.append(f"Checklist: {passed}/{total} items passed")
        except (json.JSONDecodeError, TypeError):
            pass

    if trade.get('mistake_tags'):
        try:
            mistakes = json.loads(trade['mistake_tags']) if isinstance(trade['mistake_tags'], str) else trade['mistake_tags']
            if mistakes:
                lines.append(f"Self-reported mistakes: {', '.join(mistakes)}")
        except (json.JSONDecodeError, TypeError):
            pass

    if trade.get('confidence'):
        lines.append(f"Confidence: {trade['confidence']}/5")
    if trade.get('mood'):
        lines.append(f"Mood: {trade['mood']}/5")
    if trade.get('notes'):
        # Sanitize + XML-quarantine user notes before injecting into prompt
        from carrac.grounding import escape_user_text
        _notes = sanitize_for_llm(str(trade['notes'])[:500], max_length=500, source="trade_notes")
        lines.append(f"Notes: {escape_user_text(_notes, 'journal_note', max_chars=500)}")

    if trade.get('opened_at') and trade.get('closed_at'):
        try:
            opened = datetime.fromisoformat(str(trade['opened_at']).replace('Z', '').split('+')[0].split('.')[0])
            closed = datetime.fromisoformat(str(trade['closed_at']).replace('Z', '').split('+')[0].split('.')[0])
            hours = (closed - opened).total_seconds() / 3600
            if hours < 1:
                lines.append(f"Duration: {hours * 60:.0f} minutes")
            elif hours < 24:
                lines.append(f"Duration: {hours:.1f} hours")
            else:
                lines.append(f"Duration: {hours / 24:.1f} days")
        except (ValueError, TypeError):
            pass

    return '\n'.join(lines)


def _format_recent_trades(trades):
    """Format recent trades for context (brief)."""
    if not trades:
        return ""
    lines = [f"Last {len(trades)} trades:"]
    for t in trades[:10]:
        rr = t.get('rr_actual')
        result = f"{rr:+.2f}R" if rr is not None else "open"
        lines.append(f"- {t.get('asset', '?')} {t.get('direction', '?')} {result} ({t.get('setup_type', 'no setup')})")
    return '\n'.join(lines)


def _format_score_context(snapshots):
    """Format entry/exit score snapshots for AI review prompt."""
    if not snapshots:
        return ""

    parts = []
    entry_snap = None
    exit_snap = None

    for s in snapshots:
        if s.get('snapshot_type') == 'entry':
            entry_snap = s
        elif s.get('snapshot_type') == 'exit':
            exit_snap = s

    for label, snap in [('AT ENTRY', entry_snap), ('AT EXIT', exit_snap)]:
        if not snap:
            continue

        ts = snap.get('captured_at', '?')
        total = snap.get('score_total', '?')
        verdict = snap.get('verdict', '?')
        direction = snap.get('direction', '?')

        lines = [f"{label} ({ts}):"]
        lines.append(f"Score: {total}/12 {verdict} | Direction: {direction}")

        # 6 criteria with evidence
        criteria = [
            ('Trend', 'score_trend', _trend_evidence(snap)),
            ('Momentum', 'score_momentum', _momentum_evidence(snap)),
            ('RSI', 'score_rsi', _rsi_evidence(snap)),
            ('Structure', 'score_structure', _structure_evidence(snap)),
            ('Level', 'score_level', _level_evidence(snap)),
            ('Macro', 'score_macro', _macro_evidence(snap)),
        ]
        for name, key, evidence in criteria:
            score = snap.get(key, '?')
            lines.append(f"  {name}: {score}/2 — {evidence}")

        vetoes = snap.get('vetoes')
        if vetoes:
            try:
                v = json.loads(vetoes) if isinstance(vetoes, str) else vetoes
                if v:
                    lines.append(f"  Vetoes: {'; '.join(v)}")
            except (json.JSONDecodeError, TypeError):
                pass

        if snap.get('funding_rate') is not None:
            lines.append(f"  Funding: {snap['funding_rate']:.4f}%")

        setup = snap.get('setup_type')
        if setup:
            readiness = snap.get('setup_readiness', '')
            conf = snap.get('setup_confidence')
            setup_parts = [setup]
            if readiness:
                setup_parts.append(readiness)
            if conf is not None:
                setup_parts.append(f"conf={conf:.1f}")
            lines.append(f"  Setup: {', '.join(setup_parts)}")

        parts.append('\n'.join(lines))

    # Add delta if both snapshots exist
    if entry_snap and exit_snap:
        e_total = entry_snap.get('score_total', 0) or 0
        x_total = exit_snap.get('score_total', 0) or 0
        e_verdict = entry_snap.get('verdict', '?')
        x_verdict = exit_snap.get('verdict', '?')
        delta = x_total - e_total
        parts.append(f"DELTA: {delta:+d} points ({e_verdict} -> {x_verdict})")

    return '\n\n'.join(parts)


def _trend_evidence(snap):
    parts = []
    for tf in ('1d', '3d', '1w'):
        sig = snap.get(f'consensio_{tf}_signal')
        if sig is not None:
            parts.append(f"{tf.upper()}={sig:+d}")
    return f"Consensio {', '.join(parts)}" if parts else "N/A"


def _momentum_evidence(snap):
    parts = []
    ema = snap.get('ema_ribbon_trend')
    if ema:
        parts.append(f"EMAs {ema}")
    macd = snap.get('macd_histogram')
    if macd is not None:
        parts.append(f"MACD {macd:+.1f}")
    return ', '.join(parts) if parts else "N/A"


def _rsi_evidence(snap):
    wrsi = snap.get('weekly_rsi')
    parts = []
    if wrsi is not None:
        parts.append(f"wRSI {wrsi:.1f}")
    for tf, key in [('daily', 'rsi_div_daily'), ('weekly', 'rsi_div_weekly')]:
        div = snap.get(key)
        if div:
            parts.append(f"{tf} {div} div")
    return ', '.join(parts) if parts else "N/A"


def _structure_evidence(snap):
    trend = snap.get('structure_trend', '')
    high = snap.get('last_high_type', '')
    low = snap.get('last_low_type', '')
    if trend or high or low:
        return f"{trend} ({high}/{low})"
    return "N/A"


def _level_evidence(snap):
    parts = []
    for name, key in [('Pivot', 'cpr_pivot'), ('BB_M', 'bb_middle'),
                       ('BB_U', 'bb_upper'), ('BB_L', 'bb_lower')]:
        val = snap.get(key)
        if val is not None:
            parts.append(f"{name} {val:,.0f}")
    return ', '.join(parts[:3]) if parts else "N/A"


def _macro_evidence(snap):
    parts = []
    regime = snap.get('regime')
    if regime:
        rs = snap.get('regime_score')
        parts.append(f"{regime} ({rs:+d})" if rs is not None else regime)
    fg = snap.get('fear_greed')
    if fg is not None:
        parts.append(f"F&G {fg}")
    return ', '.join(parts) if parts else "N/A"


def _format_session_for_summary(sess, trades, trade_snapshots=None):
    """Format session + trades for AI summary."""
    trade_snapshots = trade_snapshots or {}
    lines = [f"=== SESSION: {sess.get('date', '?')} ({sess.get('session_label', 'default')}) ==="]

    if sess.get('pre_notes'):
        # Sanitize + XML-quarantine user notes before injecting into prompt
        from carrac.grounding import escape_user_text
        _pre = sanitize_for_llm(str(sess['pre_notes'])[:500], max_length=500, source="pre_notes")
        lines.append(f"Pre-session plan: {escape_user_text(_pre, 'journal_note', max_chars=500)}")
    if sess.get('mood_pre'):
        lines.append(f"Pre-session mood: {sess['mood_pre']}/5")

    total_r = 0
    wins = 0
    losses = 0
    entry_scores = []
    regimes = []

    lines.append(f"\nTrades ({len(trades)}):")
    for t in trades:
        rr = t.get('rr_actual')
        if rr is not None:
            total_r += rr
            if rr > 0:
                wins += 1
            else:
                losses += 1

        parts = [f"- {t.get('asset', '?')} {t.get('direction', '?')}"]
        if rr is not None:
            parts.append(f"{rr:+.2f}R")
        if t.get('setup_type'):
            # setup_type is user-authored free text — quarantine before prompt
            from carrac.grounding import escape_user_text
            _setup = sanitize_for_llm(str(t['setup_type'])[:200], max_length=200, source="setup_type")
            parts.append(f"setup={escape_user_text(_setup, 'setup_type', max_chars=200)}")
        if t.get('exit_reason'):
            # exit_reason is user-authored free text — quarantine before prompt
            from carrac.grounding import escape_user_text
            _exit = sanitize_for_llm(str(t['exit_reason'])[:200], max_length=200, source="exit_reason")
            parts.append(f"exit={escape_user_text(_exit, 'exit_reason', max_chars=200)}")
        if t.get('mistake_tags'):
            try:
                mistakes = json.loads(t['mistake_tags']) if isinstance(t['mistake_tags'], str) else t['mistake_tags']
                if mistakes:
                    parts.append(f"mistakes=[{', '.join(mistakes)}]")
            except (json.JSONDecodeError, TypeError):
                pass

        # Append entry score context
        tid = t.get('id')
        snaps = trade_snapshots.get(tid, [])
        entry_snap = next((s for s in snaps if s.get('snapshot_type') == 'entry'), None)
        if entry_snap:
            score = entry_snap.get('score_total', '?')
            verdict = entry_snap.get('verdict', '?')
            regime = entry_snap.get('regime', '')
            fg = entry_snap.get('fear_greed')
            ctx = f"| Entry: {score}/12 {verdict}"
            if regime:
                ctx += f" ({regime}"
                if fg is not None:
                    ctx += f", F&G {fg}"
                ctx += ")"
            parts.append(ctx)
            if isinstance(score, int):
                entry_scores.append(score)
            if regime:
                regimes.append(regime)

        lines.append(' '.join(parts))

    lines.append(f"\nSession totals: {wins}W / {losses}L, net {total_r:+.2f}R")

    # Session-level market context aggregate
    if entry_scores:
        avg_score = sum(entry_scores) / len(entry_scores)
        lines.append(f"Avg entry score: {avg_score:.1f}/12 (range: {min(entry_scores)}-{max(entry_scores)})")
    if regimes:
        from collections import Counter
        regime_counts = Counter(regimes)
        dominant = regime_counts.most_common(1)[0]
        lines.append(f"Dominant regime: {dominant[0]} ({dominant[1]}/{len(regimes)} trades)")

    if sess.get('post_notes'):
        # Sanitize + XML-quarantine user notes before injecting into prompt
        from carrac.grounding import escape_user_text
        _post = sanitize_for_llm(str(sess['post_notes'])[:500], max_length=500, source="post_notes")
        lines.append(f"Post-session notes: {escape_user_text(_post, 'journal_note', max_chars=500)}")
    if sess.get('mood_post'):
        lines.append(f"Post-session mood: {sess['mood_post']}/5")

    return '\n'.join(lines)


def _format_regime_performance(snapshots_with_outcomes):
    """Format performance-by-regime and performance-by-verdict for weekly review."""
    if not snapshots_with_outcomes:
        return ""

    lines = []

    # By regime
    by_regime = {}
    for s in snapshots_with_outcomes:
        regime = s.get('regime') or 'Unknown'
        if regime not in by_regime:
            by_regime[regime] = {'trades': 0, 'wins': 0, 'total_r': 0}
        by_regime[regime]['trades'] += 1
        if s.get('win'):
            by_regime[regime]['wins'] += 1
        rr = s.get('rr_actual')
        if rr is not None:
            by_regime[regime]['total_r'] += rr

    if by_regime and len(by_regime) > 1 or next(iter(by_regime), '') != 'Unknown':
        lines.append("By market regime:")
        for regime, stats in sorted(by_regime.items(), key=lambda x: -x[1]['trades']):
            wr = round(stats['wins'] / stats['trades'] * 100) if stats['trades'] else 0
            avg_r = stats['total_r'] / stats['trades'] if stats['trades'] else 0
            lines.append(f"  {regime}: {stats['trades']} trades, {wr}% WR, {avg_r:+.2f}R avg")

    # By entry verdict
    by_verdict = {}
    for s in snapshots_with_outcomes:
        verdict = s.get('verdict') or 'Unknown'
        if verdict not in by_verdict:
            by_verdict[verdict] = {'trades': 0, 'wins': 0, 'total_r': 0}
        by_verdict[verdict]['trades'] += 1
        if s.get('win'):
            by_verdict[verdict]['wins'] += 1
        rr = s.get('rr_actual')
        if rr is not None:
            by_verdict[verdict]['total_r'] += rr

    verdict_order = ['STRONG', 'GOOD', 'NEUTRAL', 'WEAK', 'AVOID', 'Unknown']
    if by_verdict:
        lines.append("\nBy entry score verdict:")
        for v in verdict_order:
            if v not in by_verdict:
                continue
            stats = by_verdict[v]
            wr = round(stats['wins'] / stats['trades'] * 100) if stats['trades'] else 0
            avg_r = stats['total_r'] / stats['trades'] if stats['trades'] else 0
            lines.append(f"  {v}: {stats['trades']} trades, {wr}% WR, {avg_r:+.2f}R avg")

    # Wins vs losses avg entry score
    win_scores = [s['score_total'] for s in snapshots_with_outcomes
                  if s.get('win') and s.get('score_total') is not None]
    loss_scores = [s['score_total'] for s in snapshots_with_outcomes
                   if not s.get('win') and s.get('score_total') is not None]
    if win_scores and loss_scores:
        lines.append(f"\nAvg entry score: wins={sum(win_scores)/len(win_scores):.1f}/12, "
                     f"losses={sum(loss_scores)/len(loss_scores):.1f}/12")

    return '\n'.join(lines)


def _format_analytics_for_review(analytics, period_label):
    """Format analytics data for weekly/monthly review."""
    lines = [f"=== {period_label.upper()} REVIEW DATA ==="]

    lines.append(f"Trades: {analytics.get('trades', 0)}")
    lines.append(f"Win rate: {analytics.get('win_rate', 0)}%")
    lines.append(f"Expectancy: {analytics.get('expectancy', 0):.2f}R")
    lines.append(f"Profit factor: {analytics.get('profit_factor', 'N/A')}")
    lines.append(f"Max drawdown: {analytics.get('max_drawdown', 0):.2f}R")

    if analytics.get('avg_win'):
        lines.append(f"Average win: +{analytics['avg_win']:.2f}R")
    if analytics.get('avg_loss'):
        lines.append(f"Average loss: {analytics['avg_loss']:.2f}R")

    streak = analytics.get('current_streak', {})
    if streak.get('count', 0) > 0:
        lines.append(f"Current streak: {streak['count']} {streak.get('type', '?')}")
    if analytics.get('max_win_streak'):
        lines.append(f"Max win streak: {analytics['max_win_streak']}")
    if analytics.get('max_loss_streak'):
        lines.append(f"Max loss streak: {analytics['max_loss_streak']}")

    by_setup = analytics.get('by_setup', {})
    if by_setup:
        lines.append("\nBy setup:")
        for setup, stats in sorted(by_setup.items(), key=lambda x: x[1].get('trades', 0), reverse=True):
            lines.append(f"  {setup}: {stats.get('trades', 0)} trades, "
                         f"{stats.get('win_rate', 0)}% WR, "
                         f"{stats.get('net_rr', 0):+.2f}R net, "
                         f"expectancy {stats.get('expectancy', 0):+.2f}R")

    by_dir = analytics.get('by_direction', {})
    if by_dir:
        lines.append("\nBy direction:")
        for d, stats in by_dir.items():
            lines.append(f"  {d}: {stats.get('trades', 0)} trades, "
                         f"{stats.get('win_rate', 0)}% WR, "
                         f"{stats.get('net_rr', 0):+.2f}R")

    adherence = analytics.get('rule_adherence', {})
    if adherence:
        lines.append("\nRule adherence:")
        for level, stats in adherence.items():
            lines.append(f"  {level}: {stats.get('trades', 0)} trades, "
                         f"{stats.get('win_rate', 0)}% WR, "
                         f"expectancy {stats.get('expectancy', 0):+.2f}R")

    avg_eff = analytics.get('avg_exit_efficiency')
    if avg_eff is not None:
        lines.append(f"\nAverage exit efficiency: {avg_eff * 100:.0f}%")

    return '\n'.join(lines)
