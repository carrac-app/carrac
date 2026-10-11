"""The shipped prompts and steering config must not originate advice.

Three surfaces feed text straight into a model's system prompt, so a
personal market view or a "recommended action" instruction there is
advice the code itself asks for — whatever the non-advice rule a few
lines further down says:

- ``morning_engine/config/analysis_steering.yaml`` — pasted verbatim into
  every report prompt. It ships neutral: no per-asset bias, an empty house
  view, no positions.
- ``morning_engine/prompts.py`` — the report prompts.
- ``carrac/ai/features.py`` — the Copilot / MCP-prompt instructions. They
  evaluate the user's own plan or thesis; they never originate an entry,
  stop, target or size.

Each check is a plain function over text so it can be pointed at an older
revision of a file to show that it fails there.
"""

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
STEERING = ROOT / "morning_engine" / "config" / "analysis_steering.yaml"
REGIMES = ROOT / "morning_engine" / "config" / "derivatives_regimes.yaml"
PROMPTS = ROOT / "morning_engine" / "prompts.py"
FEATURES = ROOT / "carrac" / "ai" / "features.py"


# ── checkers (text in, list of problems out) ─────────────────────────────

_BIAS_KEY = re.compile(r"^\s*my_[a-z_]*\s*:\s*(?P<val>.*)$", re.M)
_THESIS_PHRASES = [
    r"\bbear market in progress\b",
    r"\bdips are opportunities\b",
    r"\bspot only\b",
    r"\bunderperformance thesis\b",
    r"\bnot allocating\b",
    r"\bopportunistic trade\b",
]


def steering_problems(text):
    problems = []
    for m in _BIAS_KEY.finditer(text):
        val = m.group("val").strip().strip("\"'")
        if val and not val.startswith("#"):
            problems.append(f"first-person bias field with a value: {m.group(0).strip()}")
    for pat in _THESIS_PHRASES:
        if re.search(pat, text, re.I):
            problems.append(f"personal thesis phrasing: {pat}")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        return problems + ["not a YAML mapping"]
    for key, val in (data.get("thesis") or {}).items():
        if val not in ("", None):
            problems.append(f"thesis.{key} ships with a value")
    if data.get("positions"):
        problems.append("positions ship with a value")
    for sym, cfg in (data.get("assets") or {}).items():
        if isinstance(cfg, dict) and any(k.startswith("my_") for k in cfg):
            problems.append(f"assets.{sym} carries a my_* field")
    return problems


_PROMPT_ADVICE = [
    r"recommended action",
    r"keep it actionable",
    r"specific level or action",
    r"prepare to enter",
    r"the reader can act on",
    r"actionable signals",
]


def prompt_problems(text):
    return [p for p in _PROMPT_ADVICE if re.search(p, text, re.I)]


_FEATURE_ADVICE = [
    r"suggested trade plan",
    r"### position sizing",
    r"go full size or reduce",
    r"full / 75%+ / half / quarter / skip",
    r"alternative setups",
    r"design a complete trade",
    r"construct a fully specified trade",
    r"suggest reduced position sizing",
    r"one actionable recommendation",
    r"take profit' framing",
    r"cutting losses",
]


def feature_problems(text):
    return [p for p in _FEATURE_ADVICE if re.search(p, text, re.I)]


_REGIME_ADVICE = [
    r"contrarian buy signal",
    r"\baction_bias\s*:",
    r"\bbegin accumulation\b",
    r"\breduce longs\b",
    r"\bposition for upside\b",
]


def regime_problems(text):
    return [p for p in _REGIME_ADVICE if re.search(p, text, re.I)]


# ── the shipped files ────────────────────────────────────────────────────

def test_steering_file_is_neutral():
    assert steering_problems(STEERING.read_text()) == []


def test_report_prompts_do_not_ask_for_actions():
    assert prompt_problems(PROMPTS.read_text()) == []


def test_ai_features_do_not_originate_trades():
    assert feature_problems(FEATURES.read_text()) == []


def test_regime_playbook_is_descriptive():
    assert regime_problems(REGIMES.read_text()) == []


def test_validate_and_design_say_they_never_originate():
    from carrac.ai.features import FEATURES as F
    for key in ("validate", "trade_design"):
        assert "never originate" in F[key]["system_instruction"].lower(), key


def test_follow_up_chips_never_ask_for_an_entry():
    from carrac.ai.features import FOLLOW_UP_SUGGESTIONS
    chips = " | ".join(s for v in FOLLOW_UP_SUGGESTIONS.values() for s in v).lower()
    for bad in ("where should i enter", "entry/sl/tp", "alternative entries", "worth opening"):
        assert bad not in chips, bad


# ── the checkers can see what they guard ─────────────────────────────────
# A checker that cannot fail is decoration: each one must flag a known-bad
# sample shaped like the content it replaced.

def test_checkers_flag_known_bad_samples():
    bad_steering = (
        "thesis:\n  macro_view: |\n    Bear market in progress.\n"
        "assets:\n  GOLD:\n    my_bias: \"bullish\"\n"
        "    watching_for: \"blow-off, but dips are opportunities\"\n"
    )
    assert len(steering_problems(bad_steering)) >= 3
    assert prompt_problems("- Recommended action: wait, prepare to enter")
    assert feature_problems("### Suggested Trade Plan\n### Position Sizing\n")
    assert regime_problems('action_bias: "Begin accumulation, sell puts"')


def test_neutral_template_passes_steering_checker():
    ok = "thesis:\n  macro_view: \"\"\nassets:\n  BTC:\n    context: \"anchor asset\"\n"
    assert steering_problems(ok) == []


# ── the loader is loud about a malformed file ────────────────────────────

def test_loader_returns_shipped_steering():
    from morning_engine.config_loader import load_steering_yaml
    text = load_steering_yaml()
    assert "Steering config not available" not in text
    assert "thesis:" in text


@pytest.mark.parametrize("text", ["global: [unclosed\n", "- a\n- b\n", "just a string\n"])
def test_loader_rejects_malformed_steering(text):
    from morning_engine.config_loader import _validate_steering
    with pytest.raises(ValueError):
        _validate_steering(text, "analysis_steering.yaml")


def test_loader_raises_through_load(monkeypatch, tmp_path):
    """The real entry point, not just the helper, refuses a broken file."""
    import morning_engine.config_loader as cl
    bad = tmp_path / "config" / "analysis_steering.yaml"
    bad.parent.mkdir()
    bad.write_text("global: [unclosed\n")
    monkeypatch.setattr(cl, "__file__", str(tmp_path / "config_loader.py"))
    with pytest.raises(ValueError):
        cl.load_steering_yaml()
