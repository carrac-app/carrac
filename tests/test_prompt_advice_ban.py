"""Every prompt Carrac sends to a model carries the non-advice ban.

The README says: every model prompt bans buy/sell instructions and advice
phrasing, and the AI features assess a plan the user wrote rather than
proposing one. This file makes that a checked fact instead of a claim.

How it works:

1. ``PROMPT_BUILDERS`` names every prompt builder in the codebase. Each one is
   called with minimal input, and its output must contain the canonical ban
   line (``carrac.ai.copy.banned_phrases_rule_line()``).
2. ``test_every_llm_call_site_is_inventoried`` scans the source tree for code
   that obtains an LLM client. A new call site fails the test until its prompt
   is added to ``PROMPT_BUILDERS`` — a ban that a new prompt silently skips is
   exactly the gap this file exists to close.
3. The shared grounding preamble carries the ban, keeps it when the grounding
   kill switch is off, and never repeats it.
"""

import os
import pathlib
import re
from unittest import mock

import pytest

from carrac.ai.copy import banned_phrases_rule_line

ROOT = pathlib.Path(__file__).resolve().parent.parent
MARKER = banned_phrases_rule_line()


# ── 1. every prompt builder ──────────────────────────────────────────────

def _morning(name, *args, **kwargs):
    def build():
        from morning_engine import prompts
        return getattr(prompts, name)(*args, **kwargs)
    return build


def _copilot(feature):
    def build():
        from carrac.ai.features import build_system_prompt
        return build_system_prompt(
            feature, shared_context="regime: neutral", viewport_context="BTC 1D",
            scenario_context="long 60000", trader_context="swing trader",
            is_multi_turn=True, tools_active=True,
        )
    return build


def _journal(name, *args):
    def build():
        from carrac.journal import ai_review
        return getattr(ai_review, name)(*args)
    return build


def _ai_aux_weekly():
    from carrac.fastapi_app.routers import ai_aux
    return ai_aux._weekly_review_prompt()


def _summarizer():
    from carrac.ai.summarizer import SUMMARY_PROMPT
    return SUMMARY_PROMPT


def _verifier():
    from carrac.grounding.verify import _VERIFY_SYSTEM
    return _VERIFY_SYSTEM


def _mcp(name):
    def build():
        import carrac.mcp.primitives  # noqa: F401 — registers the prompts
        from carrac.mcp.registry import call_prompt
        msgs = call_prompt(name, {"asset": "BTC"})
        return "\n".join(m["content"]["text"] for m in msgs)
    return build


def _copilot_features():
    from carrac.ai.features import FEATURES
    return sorted(FEATURES)


PROMPT_BUILDERS = {
    # morning_engine/prompts.py — reports, research, critic, audio
    "morning.macro_research": _morning("macro_research_prompt", "{}"),
    "morning.shared_report": _morning("shared_report_system_prompt", "", ""),
    "morning.free_tier_report": _morning("free_tier_report_prompt", "", ""),
    "morning.personalized_report": _morning("personalized_report_prompt", "", ""),
    "morning.personal_section": _morning("personal_section_prompt", "ctx"),
    "morning.standard_personal": _morning("standard_personal_prompt", "ctx"),
    "morning.afternoon_delta": _morning("afternoon_delta_prompt"),
    "morning.weekly_digest": _morning("weekly_digest_prompt"),
    "morning.week_ahead_shared": _morning("week_ahead_shared_prompt", ""),
    "morning.week_ahead_personal": _morning("week_ahead_personal_prompt", "ctx"),
    "morning.top5_drivers": _morning("top5_drivers_prompt", "{}", "macro"),
    "morning.critic": _morning("critic_prompt"),
    "morning.audio_briefing": _morning("audio_briefing_prompt"),
    "morning.afternoon_audio": _morning("afternoon_audio_prompt"),
    "morning.combined_audio": _morning("combined_audio_prompt"),
    # carrac/journal/ai_review.py — journal AI
    "journal.trade_review": _journal("_trade_review_prompt"),
    "journal.session_summary": _journal("_session_summary_prompt"),
    "journal.weekly_review": _journal("_weekly_review_prompt", "7-day"),
    "journal.nl_sql": _journal("_nl_sql_prompt"),
    "journal.nl_interpret": _journal("_nl_interpret_prompt"),
    # carrac/fastapi_app/routers/ai_aux.py — /api/ai/weekly-review
    "ai_aux.weekly_review": _ai_aux_weekly,
    # carrac/ai — conversation summarizer
    "ai.summarizer": _summarizer,
    # carrac/grounding/verify.py — Haiku fact-check pass
    "grounding.verifier": _verifier,
    # carrac/mcp/primitives.py — prompts handed to the user's own model
    "mcp.validate-trade": _mcp("validate-trade"),
    "mcp.design-trade": _mcp("design-trade"),
    "mcp.analyze-asset": _mcp("analyze-asset"),
}
# carrac/ai/features.py — Copilot, one per feature
for _feat in ("chat", "viewport", "validate", "challenge", "checklist",
              "confluence", "trade_design", "report_followup", "news_analysis"):
    PROMPT_BUILDERS[f"copilot.{_feat}"] = _copilot(_feat)


@pytest.mark.parametrize("name", sorted(PROMPT_BUILDERS))
def test_prompt_carries_the_advice_ban(name):
    text = PROMPT_BUILDERS[name]()
    assert MARKER in text, (
        f"{name}: prompt has no non-advice ban. Wrap it with "
        f"carrac.ai.copy.with_advice_ban() or build it through "
        f"carrac.grounding.grounded_system_prompt()."
    )


def test_every_copilot_feature_is_covered():
    covered = {k.split(".", 1)[1] for k in PROMPT_BUILDERS if k.startswith("copilot.")}
    assert set(_copilot_features()) == covered


def test_every_mcp_prompt_is_covered():
    import carrac.mcp.primitives  # noqa: F401
    from carrac.mcp.registry import PROMPT_REGISTRY
    covered = {k.split(".", 1)[1] for k in PROMPT_BUILDERS if k.startswith("mcp.")}
    assert set(PROMPT_REGISTRY) == covered


# Journal + weekly-review prompts describe the user's own recorded behaviour.
# These phrasings asked the model for trade instructions; none may return.
_COACHING = [
    r"trading coach",
    r"\bactionable\b",
    r"\bimprovements\b",
    r"\baction_items\b",
    r"\bspecific action\b",
]


def coaching_problems(text):
    return [p for p in _COACHING if re.search(p, text, re.I)]


@pytest.mark.parametrize("name", [
    "journal.trade_review", "journal.session_summary", "journal.weekly_review",
    "journal.nl_interpret", "ai_aux.weekly_review",
])
def test_journal_prompts_observe_rather_than_instruct(name):
    assert coaching_problems(PROMPT_BUILDERS[name]()) == []


def test_coaching_checker_sees_the_old_wording():
    old = ("You are a trading coach reviewing a completed trade. provide specific, "
           'actionable feedback. "improvements": [...] "action_items": ["specific action 1"]')
    assert len(coaching_problems(old)) == 5


# ── 2. no LLM call site outside the inventory ────────────────────────────

# Files that obtain a model client and send it a prompt. Every prompt built in
# these files is a PROMPT_BUILDERS entry above (engine.py composes the
# morning_engine/prompts.py builders through grounded_system_prompt).
KNOWN_CALL_SITES = {
    "carrac/ai/summarizer.py",
    "carrac/fastapi_app/routers/ai.py",
    "carrac/fastapi_app/routers/ai_aux.py",
    "carrac/grounding/verify.py",
    "carrac/journal/ai_review.py",
    "morning_engine/engine.py",
}

# The client layer itself — defines the entry points, sends no prompt of its own.
_CLIENT_LAYER = {
    "carrac/ai/llm_client.py",
    "carrac/ai/anthropic_client.py",
    "carrac/ai/kimi_client.py",
    "morning_engine/anthropic_client.py",
    "morning_engine/grok_client.py",
    "morning_engine/config_loader.py",
}

_ENTRY_NAMES = (
    r"(?:a?stream_chat(?:_with_tools)?|complete_chat|MorningAnthropicClient|"
    r"GrokClient|make_primary_client|AnthropicProvider|OpenAICompatProvider|"
    r"GrokProvider|MorningAnthropicProvider)"
)
# A call or an import — a name merely mentioned in a docstring is not a site.
_LLM_ENTRY = re.compile(
    rf"\b{_ENTRY_NAMES}\s*\(|^\s*(?:from\s+\S+\s+)?import\b[^\n]*\b{_ENTRY_NAMES}\b",
    re.M,
)
_SKIP_DIRS = {".venv", "node_modules", "tests", ".git", ".claude", "docs",
              "static", "templates", "backlog", "sdk-python", "sdk-typescript"}


def llm_call_sites(root):
    found = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if set(rel.parts) & _SKIP_DIRS or rel.parts[:2] == ("carrac", "llm"):
            continue
        rel_s = rel.as_posix()
        if rel_s in _CLIENT_LAYER:
            continue
        if _LLM_ENTRY.search(path.read_text(errors="ignore")):
            found.add(rel_s)
    return found


def test_every_llm_call_site_is_inventoried():
    assert llm_call_sites(ROOT) == KNOWN_CALL_SITES, (
        "An LLM call site was added or removed. Add its prompt to "
        "PROMPT_BUILDERS (with the advice ban) and update KNOWN_CALL_SITES."
    )


def test_call_site_scan_sees_a_new_call_site(tmp_path):
    (tmp_path / "carrac").mkdir()
    (tmp_path / "carrac" / "new_feature.py").write_text(
        "from carrac.ai.llm_client import stream_chat\n"
    )
    assert llm_call_sites(tmp_path) == {"carrac/new_feature.py"}


# ── 3. the shared preamble ───────────────────────────────────────────────

def test_preamble_carries_the_ban():
    from carrac.grounding.preamble import preamble
    assert MARKER in preamble(facts_block_text="=== stub ===")


def test_preamble_does_not_claim_users_trade_on_it():
    from carrac.grounding.preamble import preamble
    text = preamble(facts_block_text="=== stub ===")
    assert "make real trading decisions" not in text
    assert "market information" in text


def test_grounded_prompt_adds_the_ban_to_a_bare_prompt():
    from carrac.grounding import grounded_system_prompt
    out = grounded_system_prompt("Describe the chart.", facts_block_text="=== stub ===")
    assert out.count(MARKER) == 1


def test_grounded_prompt_never_repeats_the_ban():
    from carrac.ai.copy import with_advice_ban
    from carrac.grounding import grounded_system_prompt
    out = grounded_system_prompt(with_advice_ban("Describe the chart."),
                                 facts_block_text="=== stub ===")
    assert out.count(MARKER) == 1


def test_ban_survives_the_grounding_kill_switch():
    from carrac.grounding import grounded_system_prompt
    with mock.patch.dict(os.environ, {"GROUNDING_ENABLED": "0"}):
        out = grounded_system_prompt("Describe the chart.")
    assert MARKER in out
    assert "=== " not in out  # facts block is still switched off


def test_ban_says_the_model_never_originates_a_trade():
    from carrac.ai.copy import advice_rephrase_block
    block = advice_rephrase_block().lower()
    assert "never originate an entry, stop, target or position size" in block


# ── 4. the renamed journal keys are the keys the renderer reads ──────────
# A prompt asking for "observations" while the renderer still reads
# "improvements" would drop the section silently; run the real code paths.

def _fake_db():
    db = mock.MagicMock()
    db.get_journal_entry.return_value = {
        "id": 1, "user_id": 7, "asset": "BTC", "direction": "long",
        "timeframe": "1D", "entry_price": 100.0, "exit_price": 110.0,
        "opened_at": "2026-10-01", "closed_at": "2026-10-02",
    }
    db.get_trade_history.return_value = []
    db.get_score_snapshots.return_value = []
    db.get_session.return_value = {"id": 3, "user_id": 7}
    db.get_session_trades.return_value = [db.get_journal_entry.return_value]
    db.get_journal_analytics.return_value = {"trades": 5, "win_rate": 60, "expectancy": 0.4}
    db.get_journal_analytics_range.return_value = None
    db.get_all_snapshots_with_outcomes.return_value = []
    return db


def _run_with_llm_json(fn, payload, *args):
    import json
    from carrac.journal import ai_review
    seen = {}

    def fake_chat(messages, max_tokens=2000):
        seen["system"] = messages[0]["content"]
        return json.dumps(payload), {}

    with mock.patch.object(ai_review, "_complete_chat", fake_chat), \
         mock.patch.object(ai_review, "check_and_reserve", return_value=True), \
         mock.patch.object(ai_review, "settle", return_value=0), \
         mock.patch.object(ai_review, "build_trader_profile_context", return_value=""), \
         mock.patch("carrac.grounding.grounded_system_prompt", side_effect=lambda p, **k: p):
        out = fn(*args)
    return out, seen["system"]


def test_trade_review_renders_observations():
    from carrac.journal import ai_review
    db = _fake_db()
    out, system = _run_with_llm_json(
        ai_review.review_trade,
        {"summary": "s", "observations": ["exit came before the user's own target"]},
        db, 1, 7,
    )
    assert MARKER in system
    assert "**Observations**" in out["review"]
    assert "exit came before the user's own target" in out["review"]


def test_session_summary_renders_new_keys():
    from carrac.journal import ai_review
    out, system = _run_with_llm_json(
        ai_review.summarize_session,
        {"summary": "s", "key_observation": "KO", "pattern_to_watch": "PW"},
        _fake_db(), 3, 7,
    )
    assert MARKER in system
    assert "**Key Observation**: KO" in out["summary"]
    assert "**Pattern to Watch**: PW" in out["summary"]


def test_weekly_review_renders_observations_next_week():
    from carrac.journal import ai_review
    out, system = _run_with_llm_json(
        ai_review.weekly_review,
        {"period_summary": "p", "observations_next_week": ["three losses came after a win"]},
        _fake_db(), 7,
    )
    assert MARKER in system
    assert "three losses came after a win" in out["review"]
    assert "Action Items" not in out["review"]
