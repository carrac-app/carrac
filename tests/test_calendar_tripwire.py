"""A tripwire must flag report calendar events/consensus not grounded in quant.events."""
from carrac.grounding.hallucination_scan import scan_calendar_fabrication

SOURCE = [
    {"date": "2026-06-25", "title": "Personal Income & Outlays (PCE)", "event_type": "PCE"},
    {"date": "2026-06-25", "title": "GDP Release", "event_type": "GDP"},
    {"date": "2026-07-02", "title": "Non-Farm Payrolls (Jun)", "event_type": "NFP"},
]


def test_flags_fabricated_event_and_consensus():
    report = (
        "## Upcoming catalysts\n"
        "| Date | Event | Expected |\n"
        "| June 23 | S&P Global Services PMI | 50.7 |\n"
        "| June 25 | PCE Price Index | +3.8% |\n"
    )
    findings = scan_calendar_fabrication(report, SOURCE)
    kinds = {f["type"] for f in findings}
    assert "fabricated_event" in kinds       # PMI is not in the grounded source
    assert "fabricated_consensus" in kinds   # 50.7 / +3.8% are not grounded


def test_clean_grounded_report_passes():
    report = (
        "| Date | Event | Impact | Expected |\n"
        "| 2026-06-25 | Personal Income & Outlays (PCE) | high | — |\n"
        "| 2026-06-25 | GDP Release | medium | — |\n"
        "| 2026-07-02 | Non-Farm Payrolls (Jun) | high | — |\n"
    )
    assert scan_calendar_fabrication(report, SOURCE) == []


def test_no_source_no_crash():
    report = "| 2026-06-25 | PCE | high | — |\n"
    assert isinstance(scan_calendar_fabrication(report, []), list)


# --- _calendar_tripwire window --------------------------------
# The tripwire fetches grounded events over the same CALENDAR_WINDOW_DAYS (14)
# window the report's calendar TABLE is rendered over, so a legitimately
# grounded event 8–14 days out is not flagged.
import logging
from datetime import datetime, timedelta


def _nfp_report_and_api(monkeypatch, offset_days):
    """Patch the events API to behave like the real endpoint — it returns the
    NFP event only when that date falls within the window the tripwire asks
    for — and return a report whose calendar cites that NFP date."""
    import re
    import morning_engine.data_assembly as da
    nfp_date = (datetime.now() + timedelta(days=offset_days)).strftime('%Y-%m-%d')

    def fake_api(path, **kwargs):
        m = re.search(r'to=(\d{4}-\d{2}-\d{2})', path)
        to_date = m.group(1) if m else '0000-00-00'
        ev = {"date": nfp_date, "type": "NFP",
              "title": "Non-Farm Payrolls (Jun)", "impact": "high"}
        return [ev] if nfp_date <= to_date else []

    monkeypatch.setattr(da, '_api', fake_api)
    return f"| {nfp_date} | Non-Farm Payrolls (Jun) | high | — |\n"


def test_tripwire_does_not_flag_grounded_event_in_8_to_14_day_window(monkeypatch, caplog):
    """NFP at +8 days is grounded within the 14-day table window — no fabrication error."""
    report = _nfp_report_and_api(monkeypatch, offset_days=8)
    from morning_engine.engine import _calendar_tripwire
    with caplog.at_level(logging.ERROR):
        _calendar_tripwire(report, "general")
    assert "calendar fabrication" not in caplog.text


def test_tripwire_still_flags_event_beyond_window(monkeypatch, caplog, notifications):
    """An event genuinely outside the grounded window is still flagged."""
    report = _nfp_report_and_api(monkeypatch, offset_days=40)
    from morning_engine.engine import _calendar_tripwire
    with caplog.at_level(logging.ERROR):
        _calendar_tripwire(report, "general")
    assert "calendar fabrication" in caplog.text
    # Must reach the operator, not only the logfile.
    assert notifications
