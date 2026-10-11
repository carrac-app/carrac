"""Test that enforce_char_limit keeps Market Snapshot at a 13000 cap
but drops it at a 9000 cap.

Market Snapshot is the FIRST entry in DEFAULT_DROP_ORDER (lowest-value drop),
so any report exceeding the cap will lose it before any other section.
"""

from morning_engine.briefing import enforce_char_limit

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_section(heading: str, body_chars: int) -> str:
    """Return a ## section whose body is exactly body_chars characters."""
    body = "x" * body_chars
    return f"## {heading}\n{body}\n\n"


def _build_report() -> str:
    """Build a report whose total length is between 9000 and 13000 chars.

    Structure (approximate):
      ## Executive Summary   ~3000 chars  — PROTECTED, never dropped
      ## Market Snapshot     ~4000 chars  — first in DEFAULT_DROP_ORDER
      ## Macro Context       ~3000 chars  — later in DEFAULT_DROP_ORDER

    Total ≈ 10 000 chars  →  over 9000, under 13000.
    """
    report = ""
    report += _make_section("Executive Summary", 3000)
    report += _make_section("Market Snapshot", 4000)
    report += _make_section("Macro Context", 3000)
    return report


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_market_snapshot_dropped_at_9000():
    """At a 9000 cap, Market Snapshot (first drop target) is removed."""
    report = _build_report()
    assert len(report) > 9000, "fixture must exceed 9000 chars"
    assert "## Market Snapshot" in report

    trimmed = enforce_char_limit(report, 9000)

    assert len(trimmed) <= 9000
    assert "## Market Snapshot" not in trimmed, (
        "Market Snapshot should have been dropped to bring report under 9000"
    )
    # Executive Summary is PROTECTED — must survive
    assert "## Executive Summary" in trimmed


def test_market_snapshot_kept_at_13000():
    """At the new 13000 cap, the same report fits without dropping Market Snapshot."""
    report = _build_report()
    assert len(report) < 13000, "fixture must be under 13000 chars"
    assert "## Market Snapshot" in report

    trimmed = enforce_char_limit(report, 13000)

    assert len(trimmed) <= 13000
    assert "## Market Snapshot" in trimmed, (
        "Market Snapshot must NOT be dropped when report fits within 13000"
    )
    assert "## Executive Summary" in trimmed


def _build_long_report() -> str:
    """A full general report ~14.3K chars (mirrors a real general report): data-dense
    Market Snapshot at the end, prose sections ahead of it. Over 13000, under 15000."""
    report = ""
    report += _make_section("The Story", 4000)
    report += _make_section("What the Data Says", 3500)
    report += _make_section("Scenarios", 2800)          # PROTECTED
    report += _make_section("Levels in Focus", 1500)
    report += _make_section("Market Snapshot", 2500)     # first in DEFAULT_DROP_ORDER
    return report


def test_market_snapshot_dropped_at_13000_when_report_runs_long():
    """Pins that a ~14.3K report loses Market Snapshot at a 13000 cap."""
    report = _build_long_report()
    assert len(report) > 13000, "fixture must exceed the 13000 cap"
    trimmed = enforce_char_limit(report, 13000)
    assert "## Market Snapshot" not in trimmed


def test_market_snapshot_kept_at_15000():
    """At the 15000 cap a ~14.3K report keeps Market Snapshot intact."""
    report = _build_long_report()
    assert len(report) < 15000, "fixture must fit under the 15000 cap"
    trimmed = enforce_char_limit(report, 15000)
    assert len(trimmed) <= 15000
    assert "## Market Snapshot" in trimmed, "Market Snapshot must survive at the 15000 cap"
