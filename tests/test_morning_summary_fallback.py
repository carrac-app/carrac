"""Tests for morning report summary fallback: extract_tldr_summary.

Covers the new briefing.extract_tldr_summary helper that is used as a
fallback when admin_summary is unavailable (no admin user, admin personal
generation failed, personal section lacked an "Executive Summary" subsection).

Pure-logic tests — no live API, no LLM, no DB.
"""
from __future__ import annotations



class TestExtractTldrSummary:
    """extract_tldr_summary parses the ## TL;DR section of the shared report."""

    def _call(self, content):
        from morning_engine.briefing import extract_tldr_summary
        return extract_tldr_summary(content)

    def test_none_on_empty_content(self):
        assert self._call("") is None
        assert self._call(None) is None

    def test_extracts_tldr_section_body(self):
        content = (
            "## TL;DR\n"
            "Risk-on session: BTC reclaimed 70k, macro tailwinds "
            "from softer-than-expected CPI data.\n\n"
            "## Market Snapshot\n"
            "| Asset | Price |\n"
        )
        result = self._call(content)
        assert result is not None
        assert "Risk-on session" in result
        # Heading itself must not leak into the result
        assert "TL;DR" not in result

    def test_falls_back_to_first_prose_paragraph_if_no_tldr_section(self):
        content = (
            "## Executive Summary\n"
            "Markets broadly higher as dollar weakens on soft inflation print.\n\n"
            "## Market Snapshot\n| BTC | 70000 |\n"
        )
        result = self._call(content)
        assert result is not None
        assert "Markets broadly higher" in result

    def test_truncates_at_300_chars(self):
        long_para = "X" * 400
        content = f"## TL;DR\n{long_para}\n"
        result = self._call(content)
        assert result is not None
        assert len(result) <= 305  # small slack for ellipsis char

    def test_skips_heading_lines(self):
        content = (
            "## TL;DR\n"
            "# Not a paragraph\n"
            "Actual summary: crypto held gains despite equity sell-off.\n"
        )
        result = self._call(content)
        assert result is not None
        assert "Actual summary" in result
        assert "Not a paragraph" not in result

    def test_skips_table_lines(self):
        content = (
            "## TL;DR\n"
            "| Asset | Move |\n"
            "|-------|------|\n"
            "| BTC   | +2%  |\n\n"
            "Broad risk-on tone as macro data improved across the board.\n"
        )
        result = self._call(content)
        assert result is not None
        assert "risk-on tone" in result

    def test_flattens_substantive_bullets(self):
        """The real general/morning TL;DR is a BULLET list. Substantive bullets
        must be flattened into clean prose (markers + bold stripped), NOT
        skipped; a bullet-only TL;DR never returns None."""
        content = (
            "## TL;DR\n\n"
            "- **Oil's 5-day crash of -11.5%** is the week's most violent move "
            "as the geopolitical premium evaporates\n"
            "- DXY hit RSI 70.3 post-FOMC, sitting at 99% of its 20-bar range\n\n"
            "---\n\n"
            "## The Story\nThe dominant narrative is a three-way collision.\n"
        )
        result = self._call(content)
        assert result is not None
        assert "Oil's 5-day crash" in result
        # no markdown leakage and no stray horizontal-rule dashes
        assert "**" not in result
        assert "- " not in result
        assert "--" not in result
        assert "The Story" not in result

    def test_short_bullets_skipped_prose_used(self):
        content = (
            "## TL;DR\n"
            "- BTC up\n"
            "- ETH up\n\n"
            "Markets rallied sharply on positive macro data this session.\n"
        )
        result = self._call(content)
        assert result is not None
        assert "rallied" in result

    def test_stops_at_next_section_heading(self):
        """Content after the next ## heading must not appear in the result."""
        content = (
            "## TL;DR\n"
            "Crypto markets gained ground across the board today on softer CPI.\n\n"
            "## Action Dashboard\n"
            "This text belongs to the next section and must not be included.\n"
        )
        result = self._call(content)
        assert result is not None
        assert "Action Dashboard" not in result
        assert "next section" not in result

    def test_admin_summary_preferred_over_tldr(self):
        """Integration: when admin_summary is set, _extract_tldr_summary is not
        used for the morning save path. Verify the OR logic independently."""
        from morning_engine.briefing import extract_tldr_summary

        admin_summary = "Admin executive summary text that is long enough."
        shared_report = (
            "## TL;DR\n"
            "TL;DR fallback text that should not be saved when admin summary exists.\n"
        )
        # Mimic the engine logic: admin_summary OR tldr_fallback
        summary_to_save = admin_summary or extract_tldr_summary(shared_report)
        assert summary_to_save == admin_summary

    def test_tldr_used_when_admin_summary_none(self):
        """When admin_summary is None the TL;DR body is used as the fallback."""
        from morning_engine.briefing import extract_tldr_summary

        admin_summary = None
        shared_report = (
            "## TL;DR\n"
            "Fallback summary from the shared report TL;DR section.\n\n"
            "## Market Snapshot\n| BTC | 70k |\n"
        )
        summary_to_save = admin_summary or extract_tldr_summary(shared_report)
        assert summary_to_save is not None
        assert "Fallback summary" in summary_to_save
