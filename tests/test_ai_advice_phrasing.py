"""Pins that the AI Copilot system prompt enforces a
non-advice posture aligned with MiCA Art. 82 and national-regulator rules.

The shared list of banned phrases lives in carrac/ai/copy.py so both
the Copilot (carrac/ai/features.py) and the morning engine
(morning_engine/prompts.py) stay in sync.
"""
from carrac.ai.copy import BANNED_ADVICE_PHRASES, banned_phrases_rule_line
from carrac.ai.features import BASE_PROMPT


def test_base_prompt_contains_advice_rephrase_block():
    assert "Treat every output as information" in BASE_PROMPT
    assert "Never frame as a personal recommendation" in BASE_PROMPT


def test_base_prompt_lists_every_banned_phrase():
    for phrase in BANNED_ADVICE_PHRASES:
        assert phrase in BASE_PROMPT, (
            f"BASE_PROMPT must mention banned phrase '{phrase}' so the LLM "
            f"is told what NOT to write — see carrac/ai/copy.py."
        )


def test_morning_engine_uses_same_rule_line():
    import pathlib
    text = pathlib.Path('morning_engine/prompts.py').read_text()
    # Either the literal line lives in the file (current state) or it's
    # invoked via banned_phrases_rule_line(); both are acceptable.
    assert ('"I would"' in text) or (banned_phrases_rule_line() in text), (
        "morning_engine/prompts.py must include the banned phrase 'I would' "
        "so it stays parallel with carrac/ai/copy.py BANNED_ADVICE_PHRASES."
    )
