"""Shared LLM-output guard rails.

Both the AI Copilot (carrac/ai/features.py) and the morning engine
(morning_engine/prompts.py) need the same set of banned phrases — under
MiCA Art. 82 and national-regulator definitions of "regulated
investment advice", emitting personalised "buy this" recommendations is the line
between commentary and a regulated act. Centralising the list means
both surfaces evolve together.

Disclaimer: "not financial advice" was dropped from the banned list
because we now show that phrase in app chrome (Copilot drawer header,
morning report header, audio intro), so the model no longer needs to
self-disclaim every paragraph.
"""

# Phrases the model must NEVER write. Single source of truth.
BANNED_ADVICE_PHRASES = [
    'buy',
    'sell',
    'you should',
    'I recommend',
    'I would',
]

# Permitted alternatives — used in BASE_PROMPT to teach the model how to
# phrase observations as analysis, not advice.
PERMITTED_PHRASING_HINTS = [
    "the setup shows…",
    "if you're looking at…",
    "the level the data puts in focus is…",
    "the data suggests…",
    "one path worth weighing is…",
]


def banned_phrases_rule_line() -> str:
    """The line we paste into prompts: 'Never use: "buy", "sell", ...'."""
    quoted = ', '.join(f'"{p}"' for p in BANNED_ADVICE_PHRASES)
    return f"Never use: {quoted}"


def advice_rephrase_block() -> str:
    """Prompt fragment that explains how to rephrase recommendations as
    information. The one copy of the non-advice ban: the grounding preamble,
    Copilot BASE_PROMPT, the morning prompts and every other prompt carry it
    via :func:`with_advice_ban` or the preamble."""
    permitted = ' / '.join(PERMITTED_PHRASING_HINTS)
    return (
        "Treat every output as information the user will evaluate. "
        "Never frame as a personal recommendation tailored to their "
        "circumstances. " + banned_phrases_rule_line() + ". "
        "Never originate an entry, stop, target or position size: describe "
        "market levels as levels, and assess a trade plan only when the user "
        "wrote it. "
        "Use phrasings like " + permitted
    )


def advice_ban_block() -> str:
    """The ban as a stand-alone prompt section."""
    return "NON-ADVICE RULE: " + advice_rephrase_block() + "."


# For prompts whose job is to quote text back (fact-checkers, critics): the
# ban must not stop them quoting the text under review.
AUDITOR_QUOTE_NOTE = (
    "Quoting the text under review verbatim, to flag it, is not advice and "
    "is allowed."
)


def with_advice_ban(prompt: str, *, auditor: bool = False) -> str:
    """Append the non-advice ban to *prompt* unless it already carries it.

    Idempotent, so a prompt that passes through here and then through
    ``carrac.grounding.grounded_system_prompt`` still states the ban once.
    """
    if banned_phrases_rule_line() in prompt:
        return prompt
    block = advice_ban_block()
    if auditor:
        block += " " + AUDITOR_QUOTE_NOTE
    return f"{prompt.rstrip()}\n\n{block}"
