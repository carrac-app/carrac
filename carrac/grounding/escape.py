"""Prompt-injection input sanitization for user-controlled text.

Quarantines user-supplied strings (trader_profile, play thesis, alert notes,
position notes, etc.) before interpolating them into LLM prompts. Pairs with
grounding output checks: grounding catches hallucinated facts in the model's
output; this module prevents the user from steering the model in the first
place.

Strategy follows Anthropic's untrusted-input guidance:
1. Wrap content in XML-style tags so the model treats it as data, not instructions.
2. Neutralize patterns that mimic system/role markers or override directives.
3. Truncate to a bounded length so a 100K-char profile can't hijack the prompt.
4. Strip the user's attempt to close our own quarantine tag.

Usage:
    from carrac.grounding import escape_user_text
    user_context_parts.append(
        f"## Trader Profile\\n{escape_user_text(trader_profile_text, 'trader_profile')}\\n"
    )

Never interpolate raw `f"...{user_field}..."`. If a field can be set by a
user (free-text via UI, JSON body, or import), it must pass through
escape_user_text() before reaching a prompt.
"""

from __future__ import annotations

import re

# Patterns that look like system/role markers, instruction overrides, or
# attempts to break out of context. Matched case-insensitively.
_INJECTION_PATTERNS = [
    # Header-style role markers at line start: "## SYSTEM:", "### USER", etc.
    re.compile(
        r'^\s*#{1,6}\s*(SYSTEM|ASSISTANT|USER|INSTRUCTION|OVERRIDE|IGNORE|REPLACE|PROMPT)\b[:\s]*',
        re.IGNORECASE | re.MULTILINE,
    ),
    # Bracketed role/instruction markers: "[SYSTEM:", "[INSTRUCTION]"
    re.compile(
        r'\[\s*(SYSTEM|INSTRUCTION|OVERRIDE|PROMPT|ASSISTANT)\s*[:\]]',
        re.IGNORECASE,
    ),
    # XML-style role tags: "<system>", "</instruction>", "<prompt>"
    re.compile(
        r'<\s*/?(system|instruction|prompt|assistant|user_content|trader_profile|user_data|untrusted)\s*>',
        re.IGNORECASE,
    ),
    # Common jailbreak phrasings
    re.compile(
        r'\b(ignore (?:all |the )?(?:previous|above|prior|earlier)\s+(?:instructions?|prompts?|rules?|messages?)|'
        r'disregard\s+(?:all |the )?(?:previous|above|prior)|'
        r'forget\s+(?:all |the )?(?:previous|above|prior)|'
        r'new\s+instructions?\s*:|'
        r'you\s+are\s+now\s+(?:a|an|the)|'
        r'act\s+as\s+(?:a|an|the)|'
        r'pretend\s+(?:to be|you are)|'
        r'roleplay\s+as)\b',
        re.IGNORECASE,
    ),
]

# Maximum chars per quarantined field. 4000 covers a long bio/thesis but caps
# attack surface. Trader profile, play thesis, alert note all fit comfortably.
DEFAULT_MAX_CHARS = 4000


def escape_user_text(text, label: str = "user_content", max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Quarantine user-controlled text before injecting into an LLM prompt.

    Wraps text in XML-style tags so the model treats it as data, not
    instructions. Neutralizes patterns that mimic system/role markers
    (e.g. "## SYSTEM:", "[INSTRUCTION]", "ignore previous instructions").
    Truncates to max_chars to bound attack surface.

    Args:
        text: User-controlled string to sanitize. None/empty returns "".
        label: Tag name for the wrapper. Use a descriptive label like
            "trader_profile" or "play_thesis" so the model knows what
            kind of user data it is.
        max_chars: Truncate input beyond this length.

    Returns:
        Sanitized string wrapped in <{label}>...</{label}> tags. Empty
        string if input was empty.

    Use for ANY field a user can set: trader_profile, play thesis, alert
    notes, position notes, scenario titles, bio fields, etc. Never
    interpolate raw f"...{user_field}..." into a system prompt.
    """
    if not text:
        return ""
    s = str(text)
    if len(s) > max_chars:
        s = s[:max_chars] + "…[truncated]"
    for pat in _INJECTION_PATTERNS:
        s = pat.sub(lambda m: f"[neutralized:{m.group(0)[:30]}]", s)
    # Block user attempts to close our quarantine wrapper.
    s = s.replace(f"</{label}>", f"&lt;/{label}&gt;")
    s = s.replace("</user_content>", "&lt;/user_content&gt;")
    return f"<{label}>\n{s}\n</{label}>"


def neutralize_inline(text, max_chars: int = 200) -> str:
    """Neutralize user-controlled text for INLINE interpolation into a prompt
    sentence.

    Same injection-pattern scrubbing and quarantine-tag stripping as
    ``escape_user_text`` but WITHOUT the block ``<label>…</label>`` wrapper,
    and with newlines/whitespace collapsed to single spaces. Use for short
    fields rendered mid-sentence (indicator names, alert labels, asset
    tickers) where a multi-line XML wrapper would corrupt the prose. For
    standalone block fields (thesis, notes, bio) use ``escape_user_text``.
    """
    if not text:
        return ""
    s = str(text)
    if len(s) > max_chars:
        s = s[:max_chars] + "…[truncated]"
    for pat in _INJECTION_PATTERNS:
        s = pat.sub(lambda m: f"[neutralized:{m.group(0)[:30]}]", s)
    # Strip any attempt to close an outer quarantine fence.
    s = s.replace("</user_content>", "&lt;/user_content&gt;")
    # Collapse newlines/control whitespace so the value stays on one line.
    return " ".join(s.split())


def escape_user_dict_field(d: dict, key: str, label: str | None = None, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Convenience: escape a single field from a dict (with safe defaults).

    Returns "" if dict is None or key missing/empty.
    """
    if not d:
        return ""
    return escape_user_text(d.get(key, ""), label=label or key, max_chars=max_chars)
