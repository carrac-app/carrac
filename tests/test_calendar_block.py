"""The grounded calendar must be rendered as a verbatim table in code."""
from datetime import datetime, timezone
from morning_engine.calendar_block import render_calendar_table


def test_renders_grounded_events_with_dash_for_missing_consensus():
    events = [
        {"ts": datetime(2026, 6, 25, tzinfo=timezone.utc),
         "title": "Personal Income & Outlays (PCE)", "event_type": "PCE", "impact": "high"},
        {"date": "2026-07-02", "title": "Non-Farm Payrolls (Jun)",
         "event_type": "NFP", "impact": "high", "expected": None},
    ]
    out = render_calendar_table(events)
    assert "REPRODUCE THIS TABLE VERBATIM" in out
    assert "| Date | Event | Impact | Expected |" in out
    assert "| 2026-06-25 | Personal Income & Outlays (PCE) | high | — |" in out
    assert "| 2026-07-02 | Non-Farm Payrolls (Jun) | high | — |" in out


def test_keeps_real_consensus_when_present():
    events = [{"date": "2026-06-25", "title": "PCE", "impact": "high", "expected": "+2.6%"}]
    assert "| 2026-06-25 | PCE | high | +2.6% |" in render_calendar_table(events)


def test_empty_events_returns_empty_string():
    assert render_calendar_table([]) == ""


def test_header_states_window_and_includes_far_event():
    # 14-day window must include a +9d event (e.g. NFP) a 7-day window would drop.
    out = render_calendar_table(
        [{"date": "2026-07-02", "title": "Non-Farm Payrolls (Jun)", "impact": "high"}],
        window_days=14)
    assert "next 14 days" in out
    assert "Non-Farm Payrolls (Jun)" in out


def test_default_window_is_14():
    from morning_engine.calendar_block import CALENDAR_WINDOW_DAYS
    assert CALENDAR_WINDOW_DAYS == 14
    out = render_calendar_table([{"date": "2026-06-25", "title": "PCE", "impact": "high"}])
    assert "next 14 days" in out
