"""Prompt-copy guards for the report generator (Tasks 5 & 6).

These assert the prompt TEXT carries specific instructions; they do not call an
LLM.
"""
from morning_engine import prompts


def test_general_report_section_is_observational_not_prescriptive():
    # The general-report section was reframed from prescriptive
    # bull/bear scenarios (Trigger levels / Conditional read odds / invalidation
    # / weight-of-evidence lean) to the observational "## What Would Change the
    # Picture" (evidence framing only, a brand rule). The
    # directional-lean instruction — "name which way the weight of evidence
    # leans" — must be gone; the honest tagline must be present.
    blob = open(prompts.__file__).read()
    assert "## What Would Change the Picture" in blob
    assert "Pattern data, not a forecast." in blob
    # The lean sentence was the whole point of the removed spec; it must not
    # survive anywhere in the prompt module.
    assert "weight of evidence" not in blob.lower()


def test_death_cross_defined_as_50_200():
    # The death-cross explainer must use the conventional 50/200 pair,
    # not the non-standard 20/200.
    blob = open(prompts.__file__).read()
    assert "50-day moving average drops below the 200-day" in blob
    assert "20-day moving average drops below the 200-day" not in blob


def test_calendar_guard_demands_verbatim_reproduction():
    # The calendar guard must require reproducing the grounded table
    # verbatim and forbid completing it from training memory.
    blob = open(prompts.__file__).read()
    assert "REPRODUCE its rows EXACTLY" in blob
    assert "never recall or research economic releases" in blob.lower()
    # the soft loophole that invited a "complete the table" guess must be gone
    assert "Leave the Expected column blank when no consensus" not in blob


def test_scenarios_lock_catalysts_to_grounded_calendar():
    # Catalysts/triggers must come from the grounded table only.
    blob = open(prompts.__file__).read()
    assert "scheduled catalysts and scenario triggers must come from the" in blob.lower()


def test_death_cross_is_a_hard_50_200_rule():
    # 50/200 is a hard rule (not just an example), in all report prompts.
    blob = open(prompts.__file__).read()
    assert "a death cross or golden cross always refers to the 50-day" in blob.lower()
    # present in each of the three report prompts (shared, free-tier, personalized)
    assert blob.lower().count("a death cross or golden cross always refers to the 50-day") >= 3
