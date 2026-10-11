"""The UI copy must hold the same non-advice posture as the README.

The README says Carrac describes markets and does not tell anyone what to
trade. The model prompts are checked for that elsewhere
(``test_no_advice_posture.py``, ``test_prompt_advice_ban.py``); this file
checks the text a person actually reads in the browser — every template and
every hand-written script under ``static/js``.

What fails here:

- an instruction to trade ("size up", "reduce exposure", "consider hedges",
  "go long", "should I scale into or reduce…");
- capital-allocation guidance ("maximum exposure", "capital deployed",
  "out of the market");
- a performance claim the app does not compute and show with its basis
  ("87% win rate in …", "+41%/yr", "backtesting shows …", "proven levels",
  "high-probability bounce zones").

What does not fail, on purpose: field and button names for the user's own
actions and records ("Take profit", "Win rate" as a journal metric, "Scale
out" as an exit-strategy label), rules that forbid advice ("not a buy or sell
signal"), and code comments. The patterns are phrase-level so those stay out
without an allowlist; the few exact exceptions below say why each is allowed.

Each check is a plain function over text so it can be pointed at an older
revision of a file to show that it fails there.
"""

import pathlib
import re
from unittest.mock import MagicMock

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
STATIC_JS = ROOT / "static" / "js"


def ui_files():
    files = sorted(TEMPLATES.rglob("*.html"))
    files += sorted(
        p for p in STATIC_JS.rglob("*.js")
        if not p.name.endswith(".min.js") and "vendor" not in p.parts
    )
    return files


# ── banned phrasing ──────────────────────────────────────────────────────

BANNED = {
    # instructions to trade
    "imperative: size up": r"\bsize up\b",
    "imperative: reduce exposure": r"(?<!not )\breduce exposure\b",
    "imperative: consider hedges": r"\bconsider hedg\w*",
    "imperative: tighten stops": r"\btighten stops\b",
    "imperative: bias towards longs/shorts": r"\bbias towards? (longs|shorts)\b",
    "imperative: go long/short": r"\bgo (long|short)\b",
    "imperative: you should trade": (
        r"\byou should (buy|sell|enter|exit|add|reduce|trim|short|hold|scale)\b"),
    "prompt asking for a trade call": (
        r"\bshould (I|you) (buy|sell|scale|add|reduce|enter|exit|trim|short)\b"),
    "claim: AI-generated trade ideas": r"\bAI[- ]generated trade ideas\b",
    # capital allocation
    "allocation: maximum exposure": r"\bmaximum (reasonable )?exposure\b",
    "allocation: full deployment": r"\bfull deployment\b",
    "allocation: capital deployed": r"\b(capital deployed|deploy(ing)? (your )?capital)\b",
    "allocation: % of capital": r"%\s*of (your )?(capital|portfolio|account)\b",
    "allocation: out of the market": r"\bout of the market\b",
    # unsupported performance claims
    "performance: N% win rate": r"\d+(\.\d+)?\s*%\s*win[- ]rate\b",
    "performance: %/yr return": r"\d+(\.\d+)?\s*%\s*/\s*(yr|year)\b",
    "performance: backtesting shows": r"\bbacktest\w*\s+(shows?|proves?|proved|demonstrates?)\b",
    "performance: proven levels/edge": (
        r"\bproven (support|resistance|levels?|edge|strateg\w*|system|results|track record)\b"),
    "performance: high-probability": r"\bhigh[- ]probability\b",
    "performance: beats the market": r"\b(beat|outperform)s? the market\b",
    # BTC closed weekly candles under its 200-week SMA in 2022.
    "history: never closed below": r"\bnever closed below\b",
}
_BANNED_RE = {name: re.compile(pat, re.I) for name, pat in BANNED.items()}

# (file relative to repo root, banned-pattern name) → why it is allowed.
# Empty on purpose: the patterns are phrase-level, so the near misses in the
# UI ("You should only trade with money you can afford to lose" in the
# disclaimer, "Take profit" / "Win rate" / "Scale out" as the user's own
# fields, "proven path" in code comments) never match. Add an entry here only
# with a reason, and the test below fails if the entry stops being needed.
ALLOWED = {}


def copy_problems(text, rel=""):
    """Return 'pattern: excerpt' for every banned phrase in ``text``."""
    problems = []
    for name, rx in _BANNED_RE.items():
        if (rel, name) in ALLOWED:
            continue
        for m in rx.finditer(text):
            start = max(0, m.start() - 40)
            excerpt = text[start:m.end() + 40].replace("\n", " ")
            problems.append(f"{name}: …{excerpt}…")
    return problems


@pytest.mark.parametrize("path", ui_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_ui_copy_carries_no_advice_or_return_claim(path):
    rel = str(path.relative_to(ROOT))
    problems = copy_problems(path.read_text(encoding="utf-8"), rel)
    assert problems == [], f"{rel}:\n" + "\n".join(problems)


def test_checker_sees_the_phrasing_it_bans():
    """A guard never seen to fail is not a guard."""
    for sample in (
        "Favorable regime: bias towards longs, size up.",
        "Unfavorable: reduce exposure, tighten stops, consider hedges",
        "BTC returns (+41%/yr)",
        "87% win rate in Healthy Trend",
        "The framework suggests maximum exposure.",
        "Backtesting shows that Consensio dampens portfolio volatility",
        "should I scale into or reduce any open positions?",
        "see how close price is to proven levels",
        "Weekly MA tests are high-probability bounce zones.",
        "200W SMA is the macro floor (BTC has never closed below it).",
    ):
        assert copy_problems(sample), sample
    for sample in (
        "Take profit", "Win rate", "Scale out", "Log trade",
        "not a buy signal, not a sell signal",
        "KPI bar: total trades, win rate, avg R",
    ):
        assert copy_problems(sample) == [], sample


def test_every_allowlist_entry_is_still_needed():
    """A stale exception hides the next real hit in the same file."""
    for (rel, name), why in ALLOWED.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert _BANNED_RE[name].search(text), f"unused allowlist entry {rel} / {name} ({why})"


# ── biography detector, signups page ───────────────────────────────

_BIOGRAPHY = [r"fortune 500", r"blown[- ]up account", r"\b\d+\s*years? (shipping|delivering)\b",
              r"\bafter years of trading\b"]


def biography_problems(text):
    return [p for p in _BIOGRAPHY if re.search(p, text, re.I)]


def test_biography_detector_flags_a_bio():
    assert biography_problems("17 years shipping software for Fortune 500 companies")


def test_signups_paused_page_does_not_claim_a_beta():
    text = (TEMPLATES / "signups_paused.html").read_text(encoding="utf-8").lower()
    for phrase in ("closed beta", "at capacity", "a spot opens"):
        assert phrase not in text, phrase


# ── asset counts ─────────────────────────────────────────────────────────

def _default_universe_size():
    from data_sources.stooq import StooqClient

    stooq = StooqClient(MagicMock(), MagicMock())
    traditional = sum(len(getattr(stooq, k)) for k in (
        "stock_map", "etf_map", "commodity_map", "index_map", "forex_map"))
    src = (ROOT / "database.py").read_text(encoding="utf-8")
    i = src.index("self.crypto_map = {")
    j = src.index("\n        }", i)
    crypto = len(set(re.findall(r"'([A-Z0-9]+)'\s*:", src[i:j])))
    return crypto + traditional


def test_asset_count_claims_match_the_default_universe():
    """Two pages said 270+ and 350+ while the code held 340."""
    size = _default_universe_size()
    rx = re.compile(r"\b(\d{3})\+?\s*(?:<[^>]+>\s*)?(?:curated\s+)?assets\b", re.I)
    wrong = []
    for path in ui_files():
        for m in rx.finditer(path.read_text(encoding="utf-8")):
            if int(m.group(1)) != size:
                wrong.append(f"{path.relative_to(ROOT)}: {m.group(0)!r} (universe is {size})")
    assert wrong == [], "\n".join(wrong)
