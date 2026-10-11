"""The written general report must meet the same hallucination bar as the
audio path.

Also covers the CPI-fallback units mismatch: the band
check must never treat a missing fact as a confirmed hallucination.
"""
from carrac.grounding.advice_scan import scan_advice


def test_scan_advice_flags_constructions():
    assert scan_advice("You should buy the dip here.")   # "you should" + "buy the dip"
    assert scan_advice("I recommend rotating into bonds.")
    assert scan_advice("I would sell this rally.")


def test_scan_advice_ignores_market_vocabulary():
    clean = ("Sellers absorbed the move; oversold RSI, heavy sell-side flow, "
             "buyers stepped in at support. Overbought conditions persist.")
    assert scan_advice(clean) == []


def test_scan_advice_empty_input():
    assert scan_advice("") == []
    assert scan_advice(None) == []


# ── Prescriptive-scenario advisory tripwire ────────────────────
# The general report's "## What Would Change the Picture" section is
# observational only (brand rule). scan_prescriptive_scenario is an
# ADVISORY tripwire (log-only, never the blocking HIGH gate) that flags a slip
# back into prescriptive framing so the prompt can be tightened.

# The reframed example bullet from the section — clean observational text that
# must NOT trip the scanner.
_CLEAN_BULLET = (
    "BTC sits 0.7% above a four-signal support cluster at $65,808 — the "
    "previous week's high, the 60-period EMA, the month's high, and the bottom "
    "CPR line. In 195 similar instances the median 30-day move was +5.0% vs a "
    "+1.5% baseline (hit rate 59%). Pattern data, not a forecast."
)


def test_scan_prescriptive_scenario_flags_trigger_level():
    from carrac.grounding.advice_scan import scan_prescriptive_scenario
    assert scan_prescriptive_scenario("Trigger: $65,808 — a break above confirms the leg.")


def test_scan_prescriptive_scenario_flags_invalidation():
    from carrac.grounding.advice_scan import scan_prescriptive_scenario
    assert scan_prescriptive_scenario("Invalidation: a daily close below $60,000 negates it.")


def test_scan_prescriptive_scenario_flags_ab_labels():
    from carrac.grounding.advice_scan import scan_prescriptive_scenario
    assert scan_prescriptive_scenario("Scenario A (bull): price reclaims $70k.")
    assert scan_prescriptive_scenario("Scenario B (bear): price loses $60k.")


def test_scan_prescriptive_scenario_flags_weight_of_evidence_lean():
    from carrac.grounding.advice_scan import scan_prescriptive_scenario
    assert scan_prescriptive_scenario("The weight of evidence leans bullish here.")


def test_scan_prescriptive_scenario_clean_on_observational_bullet():
    from carrac.grounding.advice_scan import scan_prescriptive_scenario
    assert scan_prescriptive_scenario(_CLEAN_BULLET) == []


def test_scan_prescriptive_scenario_empty_input():
    from carrac.grounding.advice_scan import scan_prescriptive_scenario
    assert scan_prescriptive_scenario("") == []
    assert scan_prescriptive_scenario(None) == []


def test_prescriptive_scenario_words_never_block_publish():
    # Advisory only: a stray prescriptive word must NEVER take the report
    # offline via the HIGH publish gate (the gate uses hallucination_scan, not
    # advice_scan). With a clean hallucination scan, the gate lets it through.
    from morning_engine import engine
    txt = "Scenario A (bull): reclaim. Invalidation: close below X. Trigger: 100."
    assert engine._publish_gate_ok(txt, "general report") is True


def test_publish_gate_blocks_on_high(monkeypatch, notifications):
    # Stubbing _gate_scan but not _notify would push a fake "General Report
    # BLOCKED" to the live operator topic on every run while reporting green.
    # The alert is part of the gate's contract, so assert on it via the
    # `notifications` recorder rather than letting it escape.
    from morning_engine import engine

    class F:
        class severity:  # mimic Finding.severity.value == "HIGH"
            value = "HIGH"
        span = (0, 5)
        def to_dict(self): return {"pattern": "x", "match": "y"}

    monkeypatch.setattr(engine, "_gate_scan", lambda content: [F()])
    assert engine._publish_gate_ok("some content", "general report") is False

    # Dropping the public briefing must page the operator — silently swallowing
    # a blocked report would hide a failure.
    assert len(notifications) == 1, f"expected one operator alert, got {notifications}"
    assert notifications[0]["args"][0] == "General Report BLOCKED"


def test_publish_gate_passes_when_clean(monkeypatch):
    from morning_engine import engine
    monkeypatch.setattr(engine, "_gate_scan", lambda content: [])
    assert engine._publish_gate_ok("some content", "general report") is True


def test_publish_gate_never_raises_on_scan_error(monkeypatch):
    """_gate_scan itself must fail open (never crash the gate) — but as a
    belt-and-suspenders check, _publish_gate_ok must also tolerate a broken
    _gate_scan without raising, so a gate bug can never take down publishing."""
    from morning_engine import engine

    def _boom(content):
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(engine, "_gate_scan", _boom)
    # Should not raise; fail-open means publish is allowed through.
    assert engine._publish_gate_ok("some content", "general report") is True


def test_cpi_mention_with_missing_cpi_yoy_does_not_produce_high():
    """cpi_yoy absent must never be
    treated as 'proven wrong' via the units-mismatched cpi_latest/cpi/CPI
    fallback (raw CPIAUCSL index ~333, not a percent). A legitimate '2.4%'
    CPI mention with cpi_yoy missing must not produce a HIGH finding —
    unverifiable is not the same as hallucinated."""
    from carrac.grounding.hallucination_scan import scan, has_high

    facts_missing_cpi_yoy = {
        "today": "2026-07-01",
        "last_fomc": "2026-06-17",
        # No cpi_yoy — but cpi_latest present as the raw index level (~333),
        # which is a UNITS mismatch against a "%" mention.
        "cpi_latest": 333.98,
    }
    text = "CPI came in at 2.4% year-over-year, a touch cooler than expected."
    findings = scan(text, facts_missing_cpi_yoy)
    assert not has_high(findings), f"unverifiable CPI fact must not block: {findings}"
