"""Pins the free-tier report's observational-evidence section.

The daily general ("Carrac Market Pulse") report's spec must not instruct the
model to write prescriptive bull/bear trade scenarios — named "Trigger
levels", per-trigger "Conditional read" odds, "invalidation" bullets, or a "weight of evidence" directional lean. That
violates the brand rule: user-facing surfaces carry
observation/evidence framing only — no entry/trigger levels, no invalidation
levels, no if/then price conditionals, no directional lean.

The section is ``## What Would Change the Picture`` (observed level
clusters + analogue base rates + a calendar note, closing with an honest
"Pattern data, not a forecast." tagline). Base-rate stats attached to an
OBSERVED configuration remain allowed ("in N similar instances the median 30-day
move was …"); the prescriptive machinery is absent.

Deterministic prompt-copy guard — no LLM call. Scoping note: bare "trigger" is
NOT asserted absent because ``## The Story``'s calendar guard legitimately says
"scheduled catalysts and scenario triggers must come from the Upcoming Economic
Events table" — so the prescriptive form "trigger level" is what we assert gone.
"""

from morning_engine.prompts import free_tier_report_prompt


def _p():
    return free_tier_report_prompt("steer", "patterns")


def test_prompt_declares_what_would_change_the_picture_section():
    assert "## What Would Change the Picture" in _p()


def test_old_scenarios_heading_is_gone_from_free_tier():
    p = _p()
    # The literal section heading must be gone (the section-order line + the
    # section block). ``Scenarios ~1,200 chars`` in the budget line is also
    # renamed, so the word should not survive as a section label.
    assert "## Scenarios" not in p


def test_section_placed_between_data_says_and_market_snapshot():
    p = _p()
    assert (
        p.index("## What the Data Says")
        < p.index("## What Would Change the Picture")
        < p.index("## Market Snapshot")
    )


def test_prompt_carries_honest_tagline():
    assert "Pattern data, not a forecast." in _p()


def test_prescriptive_machinery_removed():
    p = _p()
    pl = p.lower()
    # Directional lean sentence — gone.
    assert "weight of evidence" not in pl
    # Per-trigger odds framing — gone.
    assert "conditional read" not in pl
    # Named entry/trigger price levels — gone (bare "scenario triggers" in the
    # calendar guard is a different, allowed construct; see module docstring).
    assert "trigger level" not in pl
    # Invalidation levels — gone (the verb "invalidate" may still appear in the
    # forbiddance clause; the prescriptive noun must not).
    assert "invalidation" not in pl
    # A/B trade-scenario labelling — gone (case-sensitive: guards the label,
    # not lowercase prose like "scenario builds").
    assert "Scenario A" not in p
    assert "Scenario B" not in p


def test_observed_configuration_base_rates_still_allowed():
    # Base-rate stats attached to an OBSERVED configuration stay in-bounds —
    # the section keeps the "in N similar instances … hit rate" framing.
    pl = _p().lower()
    assert "instances" in pl
    assert "hit rate" in pl or "hit-rate" in pl


def test_char_budget_roughly_preserved():
    # The section keeps a roughly ~1,000–1,500 char budget.
    p = _p()
    i = p.index("## What Would Change the Picture\n")
    block = p[i:i + 1600]
    assert "1,000" in block or "1,500" in block or "1,200" in _p()


def test_prompt_has_raised_char_target():
    import re
    p = _p()
    m = re.search(r"TARGET\s+([\d,]+)\s*[–-]\s*([\d,]+)\s+CHARACTERS", p)
    assert m, "LENGTH TARGET line missing from free-tier prompt"
    lo, hi = (int(x.replace(",", "")) for x in m.groups())
    assert lo >= 5000 and hi >= 7500
