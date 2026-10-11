"""A tripwire whose scanner crashes must say so at ERROR.

The tripwires never block a report, so a scanner that raises on every
report is indistinguishable from one that finds nothing — unless the crash
is logged where someone looks. These tests make the scanner raise and
assert the failure surfaces as an ERROR record carrying the traceback.
"""
import logging

import pytest

import carrac.grounding as grounding
from carrac.grounding import advice_scan, hallucination_scan
from carrac.grounding import verify as grounding_verify
from carrac.reports import afternoon_grounding
from morning_engine import data_assembly
from morning_engine import engine


def _boom(*_args, **_kwargs):
    raise RuntimeError("scanner exploded")


_FOCUS = [{"asset": "BTC", "price": 62890,
           "support": {"price": 62020}, "resistance": {"price": 66008}}]
_SECTION = "## Levels in Focus\nBTC support at 62,020.\n"
_REPORT = "BTC held 62,020 overnight."


def _rotation_feed(*_a, **_k):
    return {"assets": [{"symbol": "BTC", "quadrant": "leading"}]}


def _calendar_feed(*_a, **_k):
    return []


# (module owning the scanner, scanner name, call, logged message)
_CASES = {
    "confluence": (
        hallucination_scan, "scan_confluence_fabrication",
        lambda: engine._confluence_tripwire(_SECTION, _FOCUS, "general report"),
        "confluence tripwire failed"),
    "internal_arithmetic": (
        hallucination_scan, "scan_internal_arithmetic",
        lambda: engine._internal_arithmetic_tripwire("Oil 84 -> 71.90", "weekly report"),
        "internal arithmetic tripwire failed"),
    "range_pctile": (
        hallucination_scan, "scan_range_pctile_mismatch",
        lambda: engine._range_pctile_tripwire(_REPORT, {"BTC": 40}, "general report"),
        "range_pctile tripwire failed"),
    "grounded_field": (
        hallucination_scan, "scan_grounded_field_mismatch",
        lambda: engine._grounded_field_tripwire(_REPORT, [{"field": "gex"}], "general report"),
        "grounded field tripwire failed"),
    "rotation_quadrant": (
        hallucination_scan, "scan_rotation_quadrant_mismatch",
        lambda: engine._rotation_quadrant_tripwire(_REPORT, "general report"),
        "rotation quadrant tripwire failed"),
    "afternoon_grounding": (
        afternoon_grounding, "check_afternoon_grounding",
        lambda: engine._afternoon_grounding_tripwire(_REPORT, "morning", "afternoon report"),
        "afternoon grounding tripwire failed"),
    "news": (
        hallucination_scan, "scan_news_fabrication",
        lambda: engine._news_tripwire(_REPORT, "general report"),
        "news tripwire failed"),
    "leadership": (
        hallucination_scan, "scan_leadership_fabrication",
        lambda: engine._leadership_tripwire(_REPORT, "general report"),
        "leadership tripwire failed"),
    "advice": (
        advice_scan, "scan_advice",
        lambda: engine._advice_tripwire(_REPORT, "general report"),
        "advice tripwire failed"),
    "scenario_advisory": (
        advice_scan, "scan_prescriptive_scenario",
        lambda: engine._scenario_advisory_tripwire(_REPORT, "general report"),
        "scenario advisory tripwire failed"),
    "data_release": (
        hallucination_scan, "scan_data_release_fabrication",
        lambda: engine._data_release_tripwire(_REPORT, "general report"),
        "data-release tripwire failed"),
    "calendar": (
        hallucination_scan, "scan_calendar_fabrication",
        lambda: engine._calendar_tripwire(_REPORT, "general report"),
        "calendar tripwire failed"),
    "verify": (
        grounding_verify, "verify_against_source",
        lambda: engine._verify_tripwire(_REPORT, "source", "general report",
                                        "general", facts_block_text="facts"),
        "verify tripwire failed"),
    "macro_leak_scan": (
        grounding, "scan",
        lambda: engine._neutralize_macro_leaks("The Fed funds range is 5.25-5.50."),
        "Macro grounding scan skipped"),
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_scanner_crash_is_logged_at_error(monkeypatch, caplog, name):
    module, scanner, call, message = _CASES[name]
    monkeypatch.setattr(module, scanner, _boom)
    monkeypatch.setattr(data_assembly, "_api",
                        _rotation_feed if name == "rotation_quadrant" else _calendar_feed)
    with caplog.at_level(logging.DEBUG, logger=engine.log.name):
        call()  # must stay non-fatal
    hits = [r for r in caplog.records if message in r.getMessage()]
    assert hits, f"no log record mentions {message!r}"
    assert hits[0].levelno == logging.ERROR, (
        f"{message!r} logged at {hits[0].levelname}, not ERROR")
    assert hits[0].exc_info is not None, "the traceback must be attached"
