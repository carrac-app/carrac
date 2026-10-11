"""
Conversation summarizer — compresses old messages to save prompt tokens.

Uses Haiku for fast, cheap summarization (~$0.003 per summary).
Strategy: keep last N messages raw, summarize older ones into a paragraph.
The summary is stored on ai_conversations.summary and injected into the
system prompt on subsequent turns.
"""

import logging

from carrac.ai.copy import with_advice_ban

logger = logging.getLogger('carrac.ai.summarizer')

SUMMARY_THRESHOLD = 10  # message_count that triggers summarization
KEEP_RECENT = 4         # number of most recent messages to keep raw
SUMMARY_MODEL = 'claude-haiku-4-5'

SUMMARY_PROMPT = with_advice_ban(
    "Summarize this trading conversation concisely. Preserve:\n"
    "- Assets discussed and their key levels mentioned\n"
    "- Conclusions reached about direction, setups, or risk\n"
    "- Open questions or areas of uncertainty\n"
    "- Any price targets, stop losses or trade plans the USER stated, attributed to the user\n"
    "Max 200 words. Use bullet points.\n\n"
    # Grounding discipline: summarization can leak training-era facts too if
    # it paraphrases macro statements. Quote the source, don't restate.
    "GROUNDING: When summarizing macro claims (Fed policy, CPI, yields, index levels), "
    "ONLY quote what the conversation itself stated — never add numbers or events from "
    "your own memory, even if you think they're correct. If the conversation did not "
    "mention a specific value, say 'value not specified' — do not supply one."
)


def should_summarize(message_count):
    """Check if conversation needs summarization."""
    return message_count is not None and message_count >= SUMMARY_THRESHOLD


def summarize_conversation(conversation_id, user_id):
    """Summarize older messages, store summary on conversation.

    Returns the summary text, or None if not needed/failed.
    Does NOT delete old messages (kept for audit/scroll-back).
    """
    from user_db import user_db

    # Check if already summarized (idempotent)
    meta = user_db.get_conversation_meta(conversation_id, user_id)
    if not meta:
        return None
    if meta.get('summary'):
        return meta['summary']  # already done
    if not should_summarize(meta.get('message_count', 0)):
        return None

    # Fetch all messages
    msgs = user_db.get_messages(conversation_id, user_id, limit=100)
    if len(msgs) < SUMMARY_THRESHOLD:
        return None

    # Take older messages (everything except the last KEEP_RECENT)
    older = msgs[:-KEEP_RECENT] if len(msgs) > KEEP_RECENT else msgs

    # Build conversation text for summarization
    # user-turn content is user-authored free text — quarantine before
    # injecting into the summarizer prompt. Assistant turns are model-generated
    # and trusted; only user turns need escaping.
    from carrac.grounding import escape_user_text as _esc

    def _fmt_msg(m):
        content = m['content'][:500]
        if m['role'] == 'user':
            content = _esc(content, 'user_message', max_chars=500)
        return f"{m['role'].upper()}: {content}"

    conv_text = '\n'.join(_fmt_msg(m) for m in older)

    # Call Haiku for cheap summarization
    try:
        from carrac.ai.anthropic_client import complete_chat

        messages = [
            {'role': 'system', 'content': SUMMARY_PROMPT},
            {'role': 'user', 'content': conv_text},
        ]
        summary_text, usage = complete_chat(
            messages, max_tokens=300, model=SUMMARY_MODEL,
        )

        if summary_text:
            user_db.update_conversation_summary(conversation_id, summary_text)
            # Log as system cost (not charged to user)
            try:
                user_db.log_ai_usage(
                    user_id, feature='system_summarize', asset='',
                    input_tokens=usage.get('prompt_tokens', 0),
                    output_tokens=usage.get('completion_tokens', 0),
                    cost_cents=0, charged_cents=0, model=SUMMARY_MODEL,
                )
            except Exception:
                pass
            logger.info(f"Summarized conversation {conversation_id} "
                        f"({len(older)} messages → {len(summary_text)} chars)")
            return summary_text

    except Exception as e:
        logger.warning(f"Summarization failed for conv {conversation_id}: {e}")

    return None


def build_history_with_summary(conversation_id, user_id, limit=50):
    """Build conversation history with summary compression.

    If a summary exists, returns recent messages with summary context
    prepended to the system prompt (via a special dict entry).
    If no summary, returns raw messages (existing behavior).

    Returns: (messages_list, summary_text_or_None)
        messages_list: [{'role': 'user'|'assistant', 'content': '...'}]
        summary_text: str to inject into system prompt, or None
    """
    from user_db import user_db

    meta = user_db.get_conversation_meta(conversation_id, user_id)
    summary = meta.get('summary') if meta else None

    if summary:
        # Only load recent messages
        all_msgs = user_db.get_messages(conversation_id, user_id, limit=limit)
        recent = all_msgs[-KEEP_RECENT:] if len(all_msgs) > KEEP_RECENT else all_msgs
        messages = []
        for msg in recent:
            role = msg.get('role', '')
            content = msg.get('content', '')
            if role in ('user', 'assistant') and content:
                messages.append({'role': role, 'content': content[:5000]})
        return messages, summary
    else:
        # No summary — return all messages (existing behavior)
        db_msgs = user_db.get_messages(conversation_id, user_id, limit=limit)
        messages = []
        for msg in db_msgs:
            role = msg.get('role', '')
            content = msg.get('content', '')
            if role in ('user', 'assistant') and content:
                messages.append({'role': role, 'content': content[:5000]})
        return messages, None
