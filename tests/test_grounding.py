"""Unit tests for carrac.grounding — must pass before every deploy.

Canary: a known hallucinated text MUST be flagged HIGH by the
regex scan. A pattern change that fails this assertion is a regression.
"""

from __future__ import annotations

import os
from unittest import mock


# Ensure grounding on regardless of local env
os.environ.setdefault("GROUNDING_ENABLED", "1")

from carrac.grounding.hallucination_scan import scan, has_high, Severity, _PATTERNS  # noqa: E402
from carrac.grounding.facts_block import facts_block  # noqa: E402
from carrac.grounding.preamble import preamble, grounded_system_prompt  # noqa: E402


# ----- THE canary: this text MUST trip HIGH --------------------

CANARY_TEXT = (
    "Voice Briefing\n"
    "Fed Surprises with 50bps Cut: Federal Reserve slashed rates by "
    "50 basis points to 4.75-5%, signaling deeper easing amid cooling "
    "inflation at 2.4% YoY; markets surged, S&P 500 +2.1% to 5,980."
)

# Fixture facts block: the last FOMC was 2026-03-18
TODAY_FACTS = {
    "today": "2026-04-18",
    "today_weekday": "Saturday",
    "last_fomc": "2026-03-18",
    "next_fomc": "2026-04-29",
    "fed_funds_latest": 3.64,
    "fed_funds_as_of": "2026-04-17",
    "yield_10y_latest": 4.12,
    "cpi_latest": 2.8,
}


def test_canary_text_is_flagged_high():
    findings = scan(CANARY_TEXT, TODAY_FACTS)
    assert has_high(findings), f"canary failed: {findings}"
    # Specifically must catch the 50bp cut claim AND the 4.75-5 band
    names = {f.pattern_name for f in findings}
    assert "fabricated_fed_event" in names or "fed_surprise" in names
    assert "sept_2024_rate_range" in names
    assert "historic_spx_level" in names


def test_canary_text_also_flags_historic_cpi():
    findings = scan(CANARY_TEXT, TODAY_FACTS)
    names = {f.pattern_name for f in findings}
    assert "historic_cpi_2024" in names


def test_is_current_cpi_in_band_uses_only_cpi_yoy():
    """cpi_latest/cpi/CPI are
    the raw CPIAUCSL index level (~333), not a percent — comparing a percentage
    mention against them is a units mismatch that would false-positive when
    cpi_yoy is transiently missing. The band check must use ONLY cpi_yoy;
    when it's absent the mention is unverifiable (not proven wrong), so the
    check returns False rather than confirming against the mismatched legacy
    fallback."""
    from carrac.grounding.hallucination_scan import _is_current_cpi_in_band
    facts_with_yoy = {"cpi_yoy": 2.5, "cpi_latest": 333.98}
    assert _is_current_cpi_in_band(facts_with_yoy, "2.5") is True
    facts_missing_yoy = {"cpi_latest": 333.98}
    assert _is_current_cpi_in_band(facts_missing_yoy, "2.4") is False


def test_clean_current_briefing_passes():
    """A briefing stating CURRENT values should not trip HIGH."""
    clean = (
        "Today's brief. The Fed held steady at its last meeting on March 18 "
        "at 3.50 to 3.75 percent. The S&P 500 closed at 7,041 on Friday. "
        "Bitcoin trades around seventy-seven thousand. Markets are in "
        "volatility compression ahead of the April 28-29 FOMC meeting."
    )
    findings = scan(clean, TODAY_FACTS)
    assert not has_high(findings), f"False positive on clean text: {findings}"


def test_legitimate_discussion_of_past_cut_is_not_blocked():
    """Discussion of the Sept 2024 cut IN THE PAST TENSE should not trip HIGH
    recent-event patterns. It may still trip the historic rate-range pattern
    which is expected — that's a LOW/MEDIUM observability signal."""
    text = (
        "Recall that in September 2024 the Fed cut 50 basis points — that "
        "was a pivot moment. Since then we've ground down to the current "
        "3.50 to 3.75 range."
    )
    findings = scan(text, TODAY_FACTS)
    # Sept-2024 range pattern is intentionally absent from this text
    # but 'fed cut' + 'September' could trip something
    names = {f.pattern_name for f in findings}
    # The fabricated_fed_event pattern requires today/yesterday/overnight — absent here
    assert "fabricated_fed_event" not in names


def test_real_recent_fomc_downgrades_severity():
    """If a real FOMC actually happened today per facts, the scan should
    DOWNGRADE the Fed-action finding from HIGH to MEDIUM (human review)
    rather than auto-blocking a legitimate day-of recap."""
    real_meeting_facts = dict(TODAY_FACTS)
    real_meeting_facts["last_fomc"] = "2026-04-18"  # today!

    text = "The Fed today cut rates by 25 basis points, signaling its first ease this cycle."
    findings = scan(text, real_meeting_facts)
    # fabricated_fed_event should be downgraded to MEDIUM
    fab = [f for f in findings if f.pattern_name == "fabricated_fed_event"]
    if fab:
        assert fab[0].severity == Severity.MEDIUM


def test_disabled_returns_empty():
    with mock.patch.dict(os.environ, {"GROUNDING_ENABLED": "0"}):
        findings = scan(CANARY_TEXT, TODAY_FACTS)
    assert findings == []


def test_facts_block_includes_date_always():
    # Even without any data, date must appear
    with mock.patch("carrac.grounding.facts_block._fetch_macro", return_value={}), \
         mock.patch("carrac.grounding.facts_block._fetch_quant_macro", return_value={}), \
         mock.patch("carrac.grounding.facts_block._fetch_fomc", return_value={"next_fomc": None, "last_fomc": None}), \
         mock.patch("carrac.grounding.facts_block._fetch_prices", return_value={}):
        block = facts_block(include_prices=False)
    assert "Today's date:" in block
    assert "UNAVAILABLE" in block  # no Fed data => explicit UNAVAILABLE


def test_facts_block_never_labels_broad_index_as_dxy():
    # DTWEXBGS (FRED broad trade-weighted USD index, ~118) is NOT the ICE DXY
    # (~99). It must never be reported under the bare label "DXY:".
    facts = {"today": "2026-06-02", "today_weekday": "Tuesday", "dxy_broad": 118.88}
    block = facts_block(include_prices=False, facts=facts)
    assert "DXY: 118.88" not in block       # the conflation bug
    assert "118.88" in block                # value still shown...
    assert "broad" in block.lower()         # ...under a broad-index label


def test_facts_block_reports_real_dxy_not_broad_when_spot_present():
    facts = {"today": "2026-06-02", "today_weekday": "Tuesday",
             "dxy_spot": 99.11, "dxy_broad": 118.88}
    block = facts_block(include_prices=False, facts=facts)
    assert "99.11" in block
    assert "DXY: 118.88" not in block
    # The "DXY" line must carry the ICE value (99.11), never the broad one.
    dxy_lines = [ln for ln in block.splitlines() if ln.startswith("DXY")]
    assert dxy_lines and all("99.11" in ln and "118.88" not in ln for ln in dxy_lines)


def test_build_facts_dict_sources_real_dxy_from_ohlcv():
    import importlib
    import pandas as pd
    # NB: carrac.grounding.__init__ re-exports the `facts_block` *function*,
    # shadowing the submodule attribute — import the module via importlib.
    fb = importlib.import_module("carrac.grounding.facts_block")
    fb._cache.clear()

    class FakeDB:
        def get_ohlcv_data(self, sym, limit=1):
            return pd.DataFrame({"close": [99.11]}) if sym == "DXY" else None

    class FakeServices:
        db = FakeDB()

    with mock.patch("carrac.grounding.facts_block._fetch_macro", return_value={"dxy_broad": 118.88}), \
         mock.patch("carrac.grounding.facts_block._fetch_quant_macro", return_value={}), \
         mock.patch("carrac.grounding.facts_block._fetch_fomc", return_value={"next_fomc": None, "last_fomc": None}), \
         mock.patch("carrac.grounding.facts_block._fetch_prices", return_value={}), \
         mock.patch("carrac.services.get_services", return_value=FakeServices()):
        facts = fb.build_facts_dict(include_prices=True)
    assert facts.get("dxy_spot") == 99.11   # real ICE DXY sourced from OHLCV
    assert facts.get("dxy_broad") == 118.88  # broad index kept, separately keyed
    fb._cache.clear()


def test_preamble_contains_hard_rules():
    pre = preamble(facts_block_text="=== stub ===")
    assert "HARD RULES" in pre
    assert "never" in pre.lower() or "do not" in pre.lower()
    assert "=== stub ===" in pre


def test_grounded_system_prompt_prepends():
    base = "You are a helpful assistant."
    wrapped = grounded_system_prompt(base, facts_block_text="=== stub ===")
    assert wrapped.startswith("You are generating output for Carrac")
    assert wrapped.endswith(base)


def test_grounded_system_prompt_bypasses_when_disabled():
    """The kill switch drops the preamble and facts block — but not the
    non-advice ban, which is not part of grounding."""
    from carrac.ai.copy import with_advice_ban
    with mock.patch.dict(os.environ, {"GROUNDING_ENABLED": "0"}):
        base = "base prompt"
        wrapped = grounded_system_prompt(base)
    assert wrapped == with_advice_ban(base)
    assert "HARD RULES" not in wrapped


# ----- Pattern library integrity ---------------------------------------

def test_all_patterns_compile():
    for name, pat, sev, reason in _PATTERNS:
        assert pat.search("") is None  # compile check
        assert name and reason


def test_pre_cut_2024_pattern():
    # 5.25-5.50 was the pre-Sept-2024 range
    findings = scan("Fed held at 5.25-5.50% today", TODAY_FACTS)
    assert any(f.pattern_name == "pre_cut_2024_rate_range" for f in findings)
