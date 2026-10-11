from carrac.grounding.hallucination_scan import scan_news_fabrication


def test_flags_unhedged_event_claim():
    out = scan_news_fabrication("The U.S. Treasury issued a 60-day sanctions waiver.")
    assert out and out[0]["type"] == "unhedged_event_claim"


def test_clean_when_hedged():
    assert scan_news_fabrication("The Treasury reportedly issued a sanctions waiver.") == []


def test_ignores_price_action_and_scheduled_data():
    assert scan_news_fabrication("Oil fell 5.2% over five sessions; PCE is due Thursday.") == []


def test_clean_when_no_event_noun():
    assert scan_news_fabrication("The Fed announced its rate decision is data-dependent.") == []


from carrac.grounding.hallucination_scan import scan_data_release_fabrication as sd


def test_flags_unattributed_release_figure():
    assert sd("US Manufacturing PMI printing 55.7, the highest since 2022.")        # flag
    assert sd("New home sales missed at 580k vs 632k expected.")                    # flag
    assert sd("Retail sales rose 0.5% in May.")                                     # flag


def test_clean_when_attributed_or_calendar_or_no_number():
    assert sd("Manufacturing PMI reportedly printed 55.7.") == []                   # hedged
    assert sd("PCE prints today; GDP also due.") == []                              # calendar events, no figure
    assert sd("ISM services has been soft for months.") == []                       # no print-shaped number
    assert sd("Consumer confidence weak for 3 months.") == []                       # bare small int, not a print


import logging
from morning_engine.engine import _news_tripwire, _data_release_tripwire


def test_tripwire_logs_on_unhedged_event(caplog, notifications):
    with caplog.at_level(logging.WARNING):
        _news_tripwire("The U.S. Treasury issued a 60-day sanctions waiver.", "general report")
    assert "unhedged news claim" in caplog.text
    # Must reach the operator, not only the logfile.
    assert notifications


def test_tripwire_silent_when_hedged(caplog):
    with caplog.at_level(logging.WARNING):
        _news_tripwire("The Treasury reportedly issued a sanctions waiver.", "general report")
    assert "unhedged news claim" not in caplog.text


def test_data_release_tripwire_logs_on_pmi(caplog, notifications):
    with caplog.at_level(logging.WARNING):
        _data_release_tripwire("US Manufacturing PMI printing 55.7, the highest since 2022.", "general report")
    assert "unattributed data-release figure" in caplog.text
    # Must reach the operator, not only the logfile.
    assert notifications


def test_data_release_tripwire_silent_when_hedged(caplog):
    with caplog.at_level(logging.WARNING):
        _data_release_tripwire("Manufacturing PMI reportedly printed 55.7.", "general report")
    assert "unattributed data-release figure" not in caplog.text


def test_flags_unhedged_geopolitical_event():
    out = scan_news_fabrication("Oil collapsed as the US and Iran declared a de-escalation.")
    assert out and out[0]["type"] == "unhedged_event_claim"


def test_geopolitical_event_clean_when_hedged():
    assert scan_news_fabrication("Oil fell amid a reportedly brokered ceasefire.") == []


# ── "in-effect" verb class (e.g. "reopens") ──


def test_flags_in_effect_event_claims():
    # "reopens" is an in-effect verb, not a declaration verb, and must
    # still be flagged.
    out = scan_news_fabrication("The US-Iran ceasefire MOU reopens the Strait of Hormuz.")
    assert out and out[0]["type"] == "unhedged_event_claim"
    assert scan_news_fabrication("A US-Iran truce takes effect today.")
    assert scan_news_fabrication("The ceasefire reached over the weekend holds for now.")


def test_in_effect_verbs_clean_when_hedged_or_no_event():
    assert scan_news_fabrication("The reportedly agreed ceasefire MOU reopens the Strait of Hormuz.") == []
    # In-effect verbs on plain price action (no event noun) must never trip.
    assert scan_news_fabrication("Support at 62,000 holds; BTC reopens the week above the CPR.") == []


# ── Leadership-change fabrication (the "Chair Kevin Warsh" class) ──

from carrac.grounding.hallucination_scan import scan_leadership_fabrication


def test_flags_unhedged_leadership_change():
    out = scan_leadership_fabrication("Incoming Fed Chair Kevin Warsh will take over in May.")
    assert out and out[0]["type"] == "unhedged_leadership_claim"
    assert scan_leadership_fabrication("The newly appointed Fed Chair signaled a dovish shift.")


def test_leadership_clean_when_hedged_or_no_change():
    assert scan_leadership_fabrication("Kevin Warsh is reportedly the incoming Fed Chair.") == []
    assert scan_leadership_fabrication("Powell's term as Fed Chair runs to 2026.") == []


def test_news_tripwires_log_as_error(caplog, notifications):
    # log.warning never reaches the error tracker as an event (breadcrumb
    # only) — MEDIUM findings must log at ERROR like the sibling
    # calendar/advice/confluence tripwires so the operator actually sees them.
    from morning_engine.engine import _leadership_tripwire
    with caplog.at_level(logging.ERROR):
        _news_tripwire("The U.S. Treasury issued a 60-day sanctions waiver.", "t")
        _data_release_tripwire("US Manufacturing PMI printing 55.7, the highest since 2022.", "t")
        _leadership_tripwire("Incoming Fed Chair Kevin Warsh will take over in May.", "t")
    assert "unhedged news claim" in caplog.text
    assert "unattributed data-release figure" in caplog.text
    assert "unhedged leadership claim" in caplog.text


def test_citation_sources_line_dedupes_domains():
    # Grok's citation URLs were logged as a count and thrown
    # away — the writer models never saw real outlets to attribute to.
    from morning_engine.engine import _citation_sources_line
    line = _citation_sources_line([
        "https://www.reuters.com/markets/a",
        "https://reuters.com/b",
        "https://www.ft.com/x",
        "",
    ])
    assert "reuters.com" in line and "ft.com" in line
    assert line.count("reuters.com") == 1  # deduped, www-stripped
    assert _citation_sources_line([]) == ""
    assert _citation_sources_line(None) == ""


# ── "Signal conflict" false positive ──────────
#
# Carrac's own report vocabulary uses "Signal conflict(s)" as a fixed
# product phrase (morning_engine/prompts.py) for indicator disagreement,
# not a geopolitical event. Paired with the in-effect verb "holds" on the
# same line, it must not trip scan_news_fabrication as an unhedged_event_claim
# on a benign technical-analysis line.


def test_signal_conflict_product_phrase_does_not_trip():
    line = ("**Signal conflict active:** BTC weekly MACD is bullish while "
            "daily MACD is in bearish contraction — the weekly structure "
            "holds, but short-term momentum is fading")
    assert scan_news_fabrication(line) == []


def test_genuine_bare_conflict_still_trips():
    # A real, unhedged geopolitical conflict claim (no "signal " prefix)
    # must still be flagged — the fix must not blanket-exempt the word.
    out = scan_news_fabrication(
        "The conflict was declared over by both governments.")
    assert out and out[0]["type"] == "unhedged_event_claim"


def test_shared_report_runs_news_tripwires():
    # The flagship paid report must run the news tripwires, not only calendar+advice.
    import morning_engine.engine as eng
    blob = open(eng.__file__).read()
    assert '_news_tripwire(shared_report, "shared report")' in blob
    assert '_data_release_tripwire(shared_report, "shared report")' in blob
    assert '_leadership_tripwire(shared_report, "shared report")' in blob
