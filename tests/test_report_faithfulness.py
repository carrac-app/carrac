"""L1: faithfulness gate.

Deterministic gate (always runs in CI): the calendar-fabrication tripwire must
pass a clean report and catch a fabricated one. Opt-in LLM judge runs only when
ANTHROPIC_API_KEY is set (prod / manual), so CI stays deterministic + free.
"""
import os
import pathlib
import pytest
from carrac.grounding.hallucination_scan import scan_calendar_fabrication

FIX = pathlib.Path(__file__).parent / "fixtures"
SOURCE = [
    {"date": "2026-06-25", "title": "Personal Income & Outlays (PCE)", "event_type": "PCE"},
    {"date": "2026-06-25", "title": "GDP Release", "event_type": "GDP"},
    {"date": "2026-07-02", "title": "Non-Farm Payrolls (Jun)", "event_type": "NFP"},
]


def test_golden_clean_report_is_faithful():
    assert scan_calendar_fabrication((FIX / "report_clean.md").read_text(), SOURCE) == []


def test_golden_fabricated_report_is_caught():
    findings = scan_calendar_fabrication((FIX / "report_fabricated.md").read_text(), SOURCE)
    kinds = {f["type"] for f in findings}
    assert "fabricated_event" in kinds and "fabricated_consensus" in kinds, \
        "the faithfulness gate must catch fabricated calendar events AND consensus"


@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY")
                    or os.getenv("GROUNDING_VERIFY_ENABLED", "1") == "0",
                    reason="LLM faithfulness judge is opt-in; needs ANTHROPIC_API_KEY + verify enabled")
def test_llm_judge_flags_unsupported_calendar_numbers():
    from carrac.grounding.verify import verify_against_source
    source_text = (
        "Grounded upcoming events (the ONLY real ones): "
        "PCE 2026-06-25 (high), GDP 2026-06-25 (medium), NFP 2026-07-02 (high). "
        "No consensus/expected values are available for any of them."
    )
    draft = (FIX / "report_fabricated.md").read_text()
    result = verify_against_source(draft, source_text)
    assert result.verdict in ("HALLUCINATION", "ERROR"), \
        f"judge should not call the fabricated draft CLEAN (got {result.verdict})"
