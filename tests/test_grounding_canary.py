"""Canary suite — frozen fixtures that pin the grounding guarantees.

Five reference dates + a sample audio briefing per date. For each, we
assert:
- Clean briefings with correct facts pass the tripwire
- Historical briefings about the Sept-2024 cut TODAY (as if it were
  current news) trip HIGH

A pattern change that breaks any of these weakens the tripwire. Update
the canary intentionally after a real Fed action, never to silence it.

Runs as part of the normal test suite, so every `make test` (and therefore every deploy gate) exercises it.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("GROUNDING_ENABLED", "1")

from carrac.grounding.hallucination_scan import scan, has_high, Severity


# ---------- Fixture set -------------------------------------------------

CANARIES = [
    # 1. Pre-cut 2024 — 5.25-5.50% was the range Jan–Sept 2024
    {
        "name": "2024-03-20 hold",
        "facts": {"today": "2024-03-20", "last_fomc": "2024-03-20",
                  "fed_funds_latest": 5.33},
        "clean_text": (
            "The Fed held at 5.25 to 5.50 percent today, signaling patience. "
            "Dot plot still implies three cuts in 2024 but the pace is slowing."
        ),
        # A briefing saying "Fed cut today" on a day the Fed held would be wrong
        "dirty_text": "Today the Fed surprised markets with a 25bp cut.",
    },
    # 2. The Sept-2024 cut itself — real event
    {
        "name": "2024-09-18 first cut",
        "facts": {"today": "2024-09-18", "last_fomc": "2024-09-18",
                  "fed_funds_latest": 4.88},
        # On the actual day, the cut IS the news — downgrade to MEDIUM
        "clean_text": (
            "Today the Fed cut 50 basis points, moving the range to "
            "4.75 to 5.00 percent. First cut of the cycle."
        ),
        "dirty_text": None,  # on the day of, the claim is real
    },
    # 3. 2025 mid-year hold
    {
        "name": "2025-06-18 hold",
        "facts": {"today": "2025-06-18", "last_fomc": "2025-06-18",
                  "fed_funds_latest": 4.38},
        "clean_text": (
            "Rates held at 4.25 to 4.50 percent today. Dot plot revised lower."
        ),
        # Fabricated cut on a hold day
        "dirty_text": "The Fed today slashed 50 basis points to 4.75-5%.",
    },
    # 4. 2026-03-18 — most recent real hold at 3.50-3.75%
    {
        "name": "2026-03-18 hold",
        "facts": {"today": "2026-03-18", "last_fomc": "2026-03-18",
                  "fed_funds_latest": 3.64},
        "clean_text": (
            "The Fed held steady at 3.50 to 3.75 percent today. One dissent "
            "from Miran preferring a quarter-point cut."
        ),
        "dirty_text": "The Fed today cut 50bp to 4.75-5%, a surprise easing.",
    },
    # 5. 2026-04-18 — between meetings (the leak case)
    {
        "name": "2026-04-18 between meetings",
        "facts": {"today": "2026-04-18", "last_fomc": "2026-03-18",
                  "next_fomc": "2026-04-29", "fed_funds_latest": 3.64},
        "clean_text": (
            "Quiet pre-FOMC tape. Fed is between meetings — next decision "
            "is April 28-29. Current range remains 3.50 to 3.75 percent."
        ),
        # Real leaked text
        "dirty_text": (
            "Fed Surprises with 50bps Cut: Federal Reserve slashed rates by "
            "50 basis points to 4.75-5%, inflation at 2.4% YoY; markets "
            "surged, S&P 500 +2.1% to 5,980."
        ),
    },
]


# ---------- Assertions --------------------------------------------------

@pytest.mark.parametrize("canary", CANARIES, ids=[c["name"] for c in CANARIES])
def test_clean_text_passes(canary):
    """Clean canary text must not trip HIGH findings."""
    findings = scan(canary["clean_text"], canary["facts"])
    highs = [f for f in findings if f.severity == Severity.HIGH]
    if canary["name"] == "2024-09-18 first cut":
        # On the real cut day, a fabricated_fed_event/rate_cut_quantum match
        # should be DOWNGRADED to MEDIUM by the facts-aware scan. No HIGH.
        assert not highs, f"Real-day cut tripped HIGH: {highs}"
    else:
        assert not highs, f"Clean text unexpectedly tripped HIGH: {highs}"


@pytest.mark.parametrize("canary", [c for c in CANARIES if c["dirty_text"]],
                          ids=[c["name"] for c in CANARIES if c["dirty_text"]])
def test_dirty_text_blocks(canary):
    """Dirty canary text must trip HIGH findings."""
    findings = scan(canary["dirty_text"], canary["facts"])
    assert has_high(findings), f"Dirty text did NOT trip HIGH: {canary['name']} / {findings}"


def test_facts_block_renders_with_real_values():
    """Sanity: facts_block produces expected output given a populated dict."""
    from carrac.grounding.facts_block import facts_block
    f = {
        "today": "2026-04-18",
        "today_weekday": "Saturday",
        "fed_funds_latest": 3.64,
        "fed_funds_as_of": "2026-04-17",
        "yield_10y_latest": 4.12,
        "next_fomc": "2026-04-29",
        "last_fomc": "2026-03-18",
        "cpi_yoy": 2.8,
        "dxy_broad": 98.03,
        "btc_usd": 77856,
        "spx": 7041,
    }
    block = facts_block(include_prices=True, facts=f)
    assert "2026-04-18" in block
    assert "3.64" in block
    assert "2026-04-29" in block
    assert "HAS NOT YET OCCURRED" in block
    assert "Bitcoin" in block
    assert "77,856" in block
    assert "CPI YoY (latest print): 2.80%" in block


# ─── SPX tripwire fact-aware downgrade ─────────────────────────

def test_spx_in_band_downgrades_to_low_when_current():
    """When live SPX is genuinely in the 5,500-5,999 band (April 2026),
    a model output mentioning that band should NOT trip HIGH."""
    text = "S&P 500 closed at 5,800 yesterday."
    facts = {"today": "2026-04-19", "spx": 5780}  # within ±3% of 5800
    findings = scan(text, facts)
    sevs = [f.severity for f in findings if f.pattern_name == "historic_spx_level"]
    # Pattern must still match (we want observability) but be downgraded.
    assert sevs, "regex should still match S&P 500 5,800"
    assert Severity.HIGH not in sevs, (
        "When current SPX is within ±3% of the matched value, the "
        "tripwire must downgrade from HIGH so audio briefings ship."
    )


def test_spx_outside_band_still_high():
    """If model claims 5,800 while live SPX is 4,800, HIGH must still fire."""
    text = "S&P 500 closed at 5,800 yesterday."
    facts = {"today": "2026-04-19", "spx": 4800}
    findings = scan(text, facts)
    highs = [f for f in findings if f.severity == Severity.HIGH and f.pattern_name == "historic_spx_level"]
    assert highs, "Hallucinated SPX (5,800 vs current 4,800) must trip HIGH"
