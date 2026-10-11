"""Per-fact provenance from the persisted briefing."""

from carrac.grounding.provenance import (
    RETAINED_HEADER,
    check_provenance,
    index_source,
    render_retained_sources,
    source_of,
    split_retained,
)

_BRIEF = (
    "# Morning Briefing\n"
    "## Quant Engine Context\n"
    "BTC nearest confluence zones (price=62,425.48): support 61,110.20\n"
    "## Levels in Focus — REPRODUCE THESE LEVELS VERBATIM\n"
    "| Asset | Price | Nearest support | Nearest resistance |\n"
    "| GOLD | 4,122.10 | 4,105.15 (0.4% below) | 4,158.33 (0.9% above) |\n"
)


def test_report_number_maps_back_to_its_briefing_block():
    rep = "## The Story\nBTC sits above a support cluster at $62,425 this morning.\n"
    out = check_provenance(rep, _BRIEF)
    assert out["orphans"] == []
    assert out["grounded"] == out["checked"]


def test_rounding_is_tolerated_in_both_directions():
    """The briefing carries full precision; the report rounds. String matching
    would call every one of these an orphan."""
    assert source_of("62,425", _BRIEF) == ["Quant Engine Context"]
    assert source_of("4,158", _BRIEF)  # briefing says 4,158.33
    assert source_of("4,122.10", _BRIEF)


def test_a_number_from_nowhere_is_an_orphan():
    rep = "## The Story\nBTC flipped gamma at 165,694 overnight.\n"
    out = check_provenance(rep, _BRIEF)
    assert [o["value"] for o in out["orphans"]] == ["165,694"]
    assert source_of("165,694", _BRIEF) == []


def test_percentages_and_ordinals_are_not_treated_as_sourceable_facts():
    rep = "## The Story\nBTC is 2.2% above support, at the 94th percentile of range.\n"
    assert check_provenance(rep, _BRIEF)["orphans"] == []


def test_deterministic_sections_are_counted_but_not_reported():
    """Market Snapshot etc. are rendered by code after generation from live
    queries, so their numbers are not model claims."""
    rep = "## Market Snapshot\nHashrate: 936.6 EH/s · Block Height: 960,956\n"
    out = check_provenance(rep, _BRIEF)
    assert out["orphans"] == []
    assert out["by_section"]["Market Snapshot"]["orphan"] == 2


def test_retained_block_renders_and_round_trips():
    block = render_retained_sources({"GOLD": [4105.15, 4158.33], "BTC": [62425.48]})
    assert RETAINED_HEADER in block and "GOLD:" in block and "BTC:" in block
    stored = _BRIEF + "\n\n" + block
    prompt, retained = split_retained(stored)
    assert prompt == _BRIEF.rstrip()          # exactly what the model saw
    assert retained.startswith(RETAINED_HEADER)
    assert "4,105" not in prompt.split("Levels in Focus")[0]


def test_split_retained_is_a_noop_on_older_briefings():
    prompt, retained = split_retained(_BRIEF)
    assert prompt == _BRIEF and retained == ""


def test_retained_levels_give_provenance_to_a_cpr_citation():
    """The point of the block: a level the report cites that the rendered table
    never showed is checkable once the computed set is retained."""
    rep = "## Scenarios\nGOLD would need to reclaim the monthly CPR at 4,251.95.\n"
    assert [o["value"] for o in check_provenance(rep, _BRIEF)["orphans"]] == ["4,251.95"]
    stored = _BRIEF + "\n\n" + render_retained_sources({"GOLD": [4251.95]})
    assert check_provenance(rep, stored)["orphans"] == []


def test_empty_inputs_do_not_crash():
    assert render_retained_sources({}) == ""
    assert render_retained_sources({"GOLD": []}) == ""
    assert check_provenance("", "")["checked"] == 0
    assert index_source("") == {}


# --- Wiring ------------------------------------------------------
# "Built but never connected" is this codebase's most common defect, so assert
# the retained-sources block actually reaches the persisted briefing. Same
# source-inspection idiom as test_verify_wiring.py: _generate_general_report is
# network/DB-coupled and not practical to run end-to-end here.

import inspect

from morning_engine import data_assembly, engine


def test_general_report_stores_the_retained_block_not_the_prompt():
    src = inspect.getsource(engine._generate_general_report)
    assert "render_retained_sources" in src
    assert "source_briefing=stored_briefing" in src
    # The block must be appended AFTER generation and AFTER the verifier, so it
    # can never leak into what the model or Haiku sees.
    verify_pos = src.index("_verify_tripwire(_llm_narrative_for_verify")
    retained_pos = src.index("render_retained_sources")
    assert verify_pos < retained_pos
    # gen_briefing itself must stay untouched — Haiku is verified against it.
    assert "gen_briefing = gen_briefing +" not in src
    assert "stored_briefing = gen_briefing" in src


def test_focus_and_scenario_levels_both_reach_the_block():
    src = inspect.getsource(engine._generate_general_report)
    assert '_levels_focus' in src and 'scenario_levels' in src
    assert '_level_sets.update(scenario_levels or {})' in src


def test_every_general_report_call_site_passes_scenario_levels():
    """Three call sites (run_morning, run_dispatch x2). A missed one silently
    stores a briefing with no scenario levels."""
    for fn in (engine.run_morning, engine.run_dispatch):
        src = inspect.getsource(fn)
        calls = src.count("_generate_general_report(")
        passes = src.count("scenario_levels=(quant_context or {}).get('confluence_levels')")
        assert calls == passes, f"{fn.__name__}: {calls} calls but {passes} pass scenario_levels"


def test_quant_context_returns_the_computed_level_sets():
    src = inspect.getsource(data_assembly.fetch_quant_context)
    assert "confluence_levels[sym] = _all_grounded_levels(cf_data)" in src
    assert "'confluence_levels': confluence_levels" in src


def test_nested_deterministic_subsections_inherit_the_skip():
    """ta_cards renders '## Spotlight — Levels & Structure' with a '###'
    sub-heading per asset. Tracking only the last heading reported every one of
    their code-rendered numbers as a model claim."""
    rep = ("## Spotlight — Levels & Structure\n"
           "### WHEAT — $634.75\n"
           "▲ resistance 642.56 (TC+EMA30+PIVOT+BC, 4)\n")
    out = check_provenance(rep, "# Briefing\n## Quant\nnothing here\n")
    assert out["orphans"] == []
    # 3 numbers on that line: the level, the 30 inside 'EMA30', the member count.
    assert out["by_section"]["Spotlight — Levels & Structure > WHEAT — $634.75"]["orphan"] == 3
