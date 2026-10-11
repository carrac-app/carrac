"""week_ahead was hard-truncated every single Sunday because
enforce_char_limit was called with DEFAULT_DROP_ORDER (Market Snapshot,
Asset Spotlight, Macro, Foresight, Play Evaluations — daily-report section
names) against week_ahead's real headings (Week Setup, Catalyst Calendar,
Setup Watch, Macro Themes, Thesis Going In), which share zero vocabulary —
so the graceful drop loop never engaged and every overage fell straight to
blunt character truncation with a "Report truncated..." marker mid-thesis.

These tests build a synthetic week_ahead-shaped report and confirm
WEEK_AHEAD_DROP_ORDER actually drops real sections instead of hard-
truncating, and that the two bookend sections survive.
"""
from morning_engine.briefing import (
    enforce_char_limit,
    WEEK_AHEAD_DROP_ORDER,
    DEFAULT_DROP_ORDER,
)

TRUNCATION_MARKER = "Report truncated to fit length limit."


def _make_week_ahead_report(section_len=1800):
    """A week_ahead-shaped report whose 5 real sections are each long enough
    that, combined, the report exceeds an 8000-char budget."""
    def sec(name, filler_word):
        body = ((filler_word + " ") * (section_len // (len(filler_word) + 1))).strip()
        return f"## {name}\n{body}\n\n"

    return (
        sec("Week Setup", "regime")
        + sec("Catalyst Calendar", "catalyst")
        + sec("Setup Watch", "trigger")
        + sec("Macro Themes", "theme")
        + sec("Thesis Going In", "thesis")
    )


def test_default_drop_order_falls_through_to_hard_truncate():
    """Pins that DEFAULT_DROP_ORDER's vocabulary doesn't match
    week_ahead's real headings, so nothing gets gracefully dropped and the
    report is hard-truncated with the visible marker."""
    report = _make_week_ahead_report()
    assert len(report) > 8000
    out = enforce_char_limit(report, 8000, drop_order=DEFAULT_DROP_ORDER)
    assert TRUNCATION_MARKER in out
    # None of week_ahead's real sections were recognised/dropped.
    assert "## Week Setup" in out  # survives only because it's near the top


def test_week_ahead_drop_order_drops_real_sections_instead_of_truncating():
    report = _make_week_ahead_report()
    assert len(report) > 8000
    out = enforce_char_limit(report, 8000, drop_order=WEEK_AHEAD_DROP_ORDER)
    assert len(out) <= 8000
    # A real section was dropped by name, not chopped mid-sentence.
    assert "## Macro Themes" not in out
    assert TRUNCATION_MARKER not in out


def test_week_ahead_drop_order_protects_bookend_sections():
    """Week Setup (opening synthesis) and Thesis Going In (closing thesis)
    must never be silently dropped — same idiom as Executive Summary /
    Action Items for the daily reports."""
    report = _make_week_ahead_report(section_len=3000)  # force full drop
    out = enforce_char_limit(report, 8000, drop_order=WEEK_AHEAD_DROP_ORDER)
    assert "## Week Setup" in out
    assert "## Thesis Going In" in out
    # The three droppable sections are all gone once the budget forces it.
    assert "## Macro Themes" not in out
    assert "## Setup Watch" not in out
    assert "## Catalyst Calendar" not in out


def test_week_ahead_drop_order_is_least_valuable_first():
    """Only mildly over budget: the FIRST entry in the order (Macro Themes)
    should be the one to go, leaving the calendar/setup sections intact."""
    # Trim the report to be only slightly over budget so a single drop suffices.
    report = _make_week_ahead_report(section_len=1600)
    out = enforce_char_limit(report, 8000, drop_order=WEEK_AHEAD_DROP_ORDER)
    assert len(out) <= 8000
    assert "## Macro Themes" not in out
    assert "## Catalyst Calendar" in out or "## Setup Watch" in out
