"""Authoritative economic-calendar block for the report briefing.

The report LLM must REPRODUCE this table verbatim instead of composing one from
a prose summary — that soft summary led it to invent consensus values and swap
in plausible-but-ungrounded events. Consensus is currently always absent (no
free feed), so the Expected column is "—"; any number there is fabrication.
"""
from typing import Any, Dict, List

CALENDAR_WINDOW_DAYS = 14  # single source for the report calendar window (Story table + widget)


def _date_str(e: Dict[str, Any]) -> str:
    v = e.get("ts") or e.get("date") or ""
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    return str(v)[:10]


def render_calendar_table(events: List[Dict[str, Any]], window_days: int = CALENDAR_WINDOW_DAYS) -> str:
    """Render grounded events as a Markdown table; '—' for any missing consensus."""
    if not events:
        return ""
    header = f"## Upcoming Economic Events (next {window_days} days) — REPRODUCE THIS TABLE VERBATIM"
    rows = [header,
            "| Date | Event | Impact | Expected |",
            "|------|-------|--------|----------|"]
    for e in events:
        title = e.get("title") or e.get("event_type") or "?"
        impact = e.get("impact") or "—"
        exp = e.get("expected")
        exp = str(exp) if exp not in (None, "", "—") else "—"
        rows.append(f"| {_date_str(e)} | {title} | {impact} | {exp} |")
    return "\n".join(rows)
