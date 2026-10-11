"""The stored archive still serves Deribit prose.

Gating GENERATION fixed tomorrow forward and did nothing for the
56 of 65 stored `general` reports written while the block was in the briefing
and the prompts asked the writer to narrate it. `/brief/<date>` serves those
publicly and unauthenticated.

Measured against the live report archive: 152 of 7319 non-blank lines carry a
barred term (2.1%); the worst single report loses 23% of its lines; no report
loses more than a quarter. That is what makes a render-time scrub viable rather
than truncating the archive to the one gate-clean report.

TWO RULES THIS ENCODES, both learned from the earlier generation-gate sweep:

1. The scrub is UNCONDITIONAL — never gated on `deribit_visible()`. That
   function answers "may this CALLER see it". A published artifact needs "may
   this ARTIFACT contain it". The report engine authenticating as admin is
   precisely how the last leak happened.
2. The prose patterns are keyed to DERIBIT_COLUMNS, and a test below fails if a
   column has no pattern. A guard that cannot see what it is guarding is
   decoration.

Every prose sample here is real text from a stored prod report.
"""
from __future__ import annotations

import pytest


# ── Real lines from stored production reports ──────────────────────────────────
MIXED_PROSE = (
    "Crypto is the odd one out. BTC at $64,407 has RSI 52, a death cross, and "
    "a bearish weekly EMA ribbon. Fear & Greed reads 27. DVOL sits at cycle "
    "lows and the futures basis at +2.91% annualized sits below the Fed funds "
    "rate."
)
WHOLLY_BARRED_BULLET = (
    "- Derivatives are coiled: BTC DVOL at 40.5 (cycle lows), basis below Fed "
    "funds at 3.44%, max-pain at $74,000 — a breakout is building, direction "
    "unknown"
)
SNAPSHOT_ROW = (
    "| ETH | 1,766 | BEAR | 55.4 | 77% | Death cross; +12.3% 5-bar; below GEX "
    "flip 1,788 |"
)
PERMITTED_PROSE = (
    "Funding is +0.011% on Binance, open interest is $38.4bn, and the "
    "long/short ratio sits at 1.24 — positioning is crowded long."
)


class TestPatternsCoverEveryGatedColumn:
    """The list that decides what is barred lives in ONE place. If a column is
    added to the gate and nobody writes a prose pattern for it, that is a
    silent hole — the same shape as the four leaks the earlier sweep found."""

    def test_every_deribit_column_has_a_prose_pattern(self):
        from carrac.compliance.deribit_gate import (
            DERIBIT_COLUMNS, DERIBIT_PROSE_PATTERNS,
        )
        missing = set(DERIBIT_COLUMNS) - set(DERIBIT_PROSE_PATTERNS)
        assert not missing, (
            f"gated columns with no prose pattern: {sorted(missing)} — "
            f"their narrated form would survive the scrub"
        )

    def test_no_pattern_for_a_column_that_is_not_gated(self):
        from carrac.compliance.deribit_gate import (
            DERIBIT_COLUMNS, DERIBIT_PROSE_PATTERNS,
        )
        extra = set(DERIBIT_PROSE_PATTERNS) - set(DERIBIT_COLUMNS)
        assert not extra, f"patterns for ungated columns: {sorted(extra)}"

    @pytest.mark.parametrize("permitted", [
        "funding_rate_avg", "oi_usd", "long_short_ratio",
    ])
    def test_permitted_columns_are_never_scrubbed(self, permitted):
        """This is a licensing boundary, not a paywall. Binance and
        Hyperliquid data must survive."""
        from carrac.compliance.deribit_gate import DERIBIT_PROSE_PATTERNS
        assert permitted not in DERIBIT_PROSE_PATTERNS


class TestSentenceLevelScrub:
    def test_only_the_offending_sentence_is_dropped(self):
        from carrac.compliance.deribit_gate import scrub_deribit_prose

        out = scrub_deribit_prose(MIXED_PROSE)
        assert "Crypto is the odd one out." in out
        assert "RSI 52" in out and "death cross" in out
        assert "Fear & Greed reads 27." in out
        assert "DVOL" not in out
        assert "2.91%" not in out, "the futures-basis sentence must go with it"

    def test_a_wholly_barred_bullet_is_removed_entirely(self):
        from carrac.compliance.deribit_gate import scrub_deribit_prose
        assert scrub_deribit_prose(WHOLLY_BARRED_BULLET).strip() == ""

    def test_permitted_derivatives_prose_is_untouched(self):
        """The half that proves the scrub is a boundary, not a delete-all."""
        from carrac.compliance.deribit_gate import scrub_deribit_prose
        assert scrub_deribit_prose(PERMITTED_PROSE) == PERMITTED_PROSE

    def test_clean_text_is_returned_byte_identical(self):
        from carrac.compliance.deribit_gate import scrub_deribit_prose
        clean = "## The Story\n\nBTC is at $64,407 with RSI 52.\n\nGold broke out.\n"
        assert scrub_deribit_prose(clean) == clean

    def test_falsy_input_passes_through(self):
        from carrac.compliance.deribit_gate import scrub_deribit_prose
        assert scrub_deribit_prose("") == ""
        assert scrub_deribit_prose(None) is None

    def test_is_idempotent(self):
        from carrac.compliance.deribit_gate import scrub_deribit_prose
        once = scrub_deribit_prose(MIXED_PROSE)
        assert scrub_deribit_prose(once) == once


class TestNoMangledRemnants:
    """Found by running the scrubber over all 65 stored production reports and
    READING the worst-affected one, rather than trusting the leak count."""

    def test_bold_close_does_not_glue_a_clean_sentence_to_a_barred_one(self):
        """Real report line. The `.**` after "panicking" blocked the
        sentence split, so a clean claim was swallowed by the DVOL sentence
        that followed it — and the scrub emitted the bare fragment `**3.`."""
        from carrac.compliance.deribit_gate import scrub_deribit_prose

        line = ("**3. Derivatives are coiled, not panicking.** DVOL at 40.5 is "
                "at cycle lows. The 25-delta skew at +10.8% means put options "
                "cost more than calls.")
        out = scrub_deribit_prose(line).strip()
        assert out != "**3.", "emitted a dangling enumerator"
        assert "Derivatives are coiled, not panicking." in out
        assert "DVOL" not in out and "skew" not in out

    def test_a_mostly_barred_paragraph_is_dropped_whole(self):
        """A paragraph that loses most of its sentences leaves the remainder
        explaining something the reader can no longer see. That reads worse
        than an omission — it reads as a non-sequitur."""
        from carrac.compliance.deribit_gate import scrub_deribit_prose

        para = ("GEX stands for Gamma Exposure. When GEX is positive dealers "
                "are long gamma. Negative GEX amplifies moves. That is why a "
                "modest BTC move can generate outsized follow-through.")
        assert scrub_deribit_prose(para).strip() == ""

    def test_a_mostly_clean_paragraph_keeps_its_survivors(self):
        """The complement — one barred sentence among several clean ones must
        NOT take the paragraph down with it."""
        from carrac.compliance.deribit_gate import scrub_deribit_prose

        para = ("Gold broke out to a new high. Equities are extended. The "
                "dollar softened again. DVOL sits at cycle lows.")
        out = scrub_deribit_prose(para)
        assert "Gold broke out to a new high." in out
        assert "The dollar softened again." in out
        assert "DVOL" not in out

    def test_removing_a_middle_sentence_leaves_no_double_space(self):
        """Cosmetic, but it ships to a public page: the tokenizer keeps each
        unit's trailing whitespace, so a naive join doubles it at the seam."""
        from carrac.compliance.deribit_gate import scrub_deribit_prose

        para = "Gold broke out. DVOL is at cycle lows. The dollar softened."
        out = scrub_deribit_prose(para)
        assert "  " not in out, f"double space at the seam: {out!r}"
        assert out.strip() == "Gold broke out. The dollar softened."


class TestTableRows:
    def test_a_row_keeps_its_permitted_cells(self):
        """Dropping the whole row would delete ETH from the snapshot table —
        losing price, trend and RSI, none of which are licensed."""
        from carrac.compliance.deribit_gate import scrub_deribit_prose

        out = scrub_deribit_prose(SNAPSHOT_ROW)
        assert "ETH" in out and "1,766" in out and "BEAR" in out
        assert "Death cross" in out
        assert "GEX" not in out and "1,788" not in out
        assert out.count("|") == SNAPSHOT_ROW.count("|"), "table shape must survive"

    def test_a_row_whose_subject_is_barred_is_dropped(self):
        from carrac.compliance.deribit_gate import scrub_deribit_prose
        row = "| BTC DVOL | 36.4 | cycle lows |"
        assert scrub_deribit_prose(row).strip() == ""


class TestEmptySectionSweep:
    """Five stored reports have a 'One Thing to Learn Today' section entirely
    about GEX. Scrubbing its lines leaves a heading over nothing."""

    LESSON = (
        "## The Story\n\nGold broke out to a new high.\n\n"
        "## One Thing to Learn Today\n\n"
        "**Today's lesson: Negative GEX — when market-makers amplify.**\n\n"
        "GEX measures the net options positioning of dealers.\n\n"
        "**Key takeaway:** Negative GEX means moves get amplified.\n\n"
        "---\n"
    )

    def test_the_emptied_section_heading_is_removed(self):
        from carrac.compliance.deribit_gate import scrub_deribit_prose
        out = scrub_deribit_prose(self.LESSON)
        assert "One Thing to Learn Today" not in out
        assert "GEX" not in out

    def test_the_surviving_section_is_intact(self):
        from carrac.compliance.deribit_gate import scrub_deribit_prose
        out = scrub_deribit_prose(self.LESSON)
        assert "## The Story" in out
        assert "Gold broke out to a new high." in out

    def test_a_section_with_surviving_body_keeps_its_heading(self):
        from carrac.compliance.deribit_gate import scrub_deribit_prose
        text = ("## What the Data Says\n\nBTC RSI is 52. DVOL is at cycle lows.\n")
        out = scrub_deribit_prose(text)
        assert "## What the Data Says" in out
        assert "BTC RSI is 52." in out
        assert "DVOL" not in out


class TestNotGatedOnCaller:
    """THE GENERATION-GATE LESSON. deribit_gate answers 'may this CALLER see it';
    a published artifact needs 'may this ARTIFACT contain it'. The report
    engine authenticates with X-Internal-Key, which resolves to admin — so a
    caller-gated scrub would have been a no-op on exactly the pipeline that
    leaked."""

    def test_scrub_ignores_admin_visibility(self, monkeypatch):
        from carrac.compliance import deribit_gate

        monkeypatch.setattr(deribit_gate, 'deribit_visible', lambda: True)
        out = deribit_gate.scrub_deribit_prose(MIXED_PROSE)
        assert "DVOL" not in out, (
            "an admin-authenticated request still publishes a PUBLIC page"
        )


class TestGenerationRegressionAlarm:
    """"If this breaks, how would I find out?"

    The render-time scrub is a backstop; it makes a leak invisible rather than
    absent. If the GENERATION gate stops working (a prompt edit, a new data
    source, a new serving path), reports would quietly carry Deribit prose and
    the scrub would keep cleaning it up silently.

    So a report SAVE that carries barred prose is loud, and non-fatal: it
    reaches a human via Sentry without ever withholding a report.
    """

    DIRTY = "## The Story\n\nBTC RSI 52. DVOL sits at cycle lows.\n"
    CLEAN = "## The Story\n\nBTC RSI 52. Funding is +0.011%.\n"

    def test_barred_prose_in_a_saved_report_reaches_sentry(self, monkeypatch):
        from carrac.compliance import deribit_gate

        captured = []
        monkeypatch.setattr(deribit_gate, '_capture', lambda msg: captured.append(msg))
        found = deribit_gate.alert_if_barred_prose(self.DIRTY, context='general')

        assert found, "the check did not spot DVOL"
        assert captured, "nothing reached Sentry — this is the silent-failure shape"
        assert 'general' in captured[0]

    def test_a_clean_report_is_silent(self, monkeypatch):
        """A guard nobody will read becomes noise, and then it trains you to
        ignore guards. Clean saves must say nothing."""
        from carrac.compliance import deribit_gate

        captured = []
        monkeypatch.setattr(deribit_gate, '_capture', lambda msg: captured.append(msg))
        assert deribit_gate.alert_if_barred_prose(self.CLEAN, context='general') == []
        assert captured == []

    def test_the_alarm_never_raises(self, monkeypatch):
        """It sits in the report write path. It must not be able to fail a save."""
        from carrac.compliance import deribit_gate

        def _boom(msg):
            raise RuntimeError("sentry down")

        monkeypatch.setattr(deribit_gate, '_capture', _boom)
        deribit_gate.alert_if_barred_prose(self.DIRTY, context='general')

    def test_it_names_the_terms_it_found(self, monkeypatch):
        from carrac.compliance import deribit_gate
        monkeypatch.setattr(deribit_gate, '_capture', lambda msg: None)
        found = deribit_gate.alert_if_barred_prose(self.DIRTY, context='general')
        assert any('dvol' in f.lower() for f in found)

    def test_save_report_is_wired_to_it(self):
        """save_report is the ONE write chokepoint for every report type — the
        reason the alarm lives there rather than in the engine."""
        import inspect
        from carrac import content_db

        src = inspect.getsource(content_db.ContentDatabase.save_report)
        assert 'alert_if_barred_prose' in src


class TestServingPathsApplyIt:
    """`/brief/<date>` is the reason this scrub exists, but it is not the only
    door: the MCP report tools serve the same stored rows. Both
    funnel through carrac.reports.sanitize, so both are checked here."""

    DIRTY = "## The Story\n\nBTC RSI is 52. DVOL sits at cycle lows.\n"

    def test_general_report_public_scrubs(self):
        from carrac.reports.sanitize import general_report_public
        out = general_report_public(self.DIRTY)
        assert "BTC RSI is 52." in out and "DVOL" not in out

    def test_operator_neutral_scrubs(self):
        """The MCP report tools and the HTTP report routes call this one."""
        from carrac.reports.sanitize import operator_neutral
        out = operator_neutral(self.DIRTY)
        assert "BTC RSI is 52." in out and "DVOL" not in out

    def test_operator_neutral_still_strips_operator_sections(self):
        """Pins that the prose scrub does not displace the operator-section filter."""
        from carrac.reports.sanitize import operator_neutral
        text = "## The Story\n\nBTC RSI 52.\n\n## Action Dashboard\n\nLong BTC entered at $60,000.\n"
        out = operator_neutral(text)
        assert "Action Dashboard" not in out and "entered at $60,000" not in out
        assert "BTC RSI 52." in out

    @pytest.mark.parametrize("module_path", [
        "carrac.reports.routes",              # /brief, the live cockpit page
        "carrac.fastapi_app.routers.reports",  # /api/report/general/latest
    ])
    def test_no_general_report_path_still_uses_the_bare_strip(self, module_path):
        """`strip_operator_sections` alone was the whole shaping for the general
        report. Every such call site had to move to `general_report_public`, or
        that door still serves Deribit prose. This is the wire-it-in-both-places
        recurring bug class here."""
        import importlib
        import inspect

        src = inspect.getsource(importlib.import_module(module_path))
        assert "general_report_public" in src or "report_text_for_viewer" in src, (
            f"{module_path} shapes a stored general report without the "
            f"licensing scrub"
        )

    def test_the_serving_policy_scrubs_the_general_report(self):
        """The routes reach ``general_report_public`` through the policy helper."""
        from carrac.reports.sanitize import report_text_for_viewer
        shown = report_text_for_viewer(
            {"content": self.DIRTY, "summary": self.DIRTY}, "general", operator=False)
        for field in ("content", "summary"):
            assert "BTC RSI is 52." in shown[field] and "DVOL" not in shown[field]

    def test_brief_archive_uses_the_scrubbing_helper(self):
        """The archive route reproduces /brief's shaping rather than importing
        it. Assert it reaches the scrub, or the leak reopens silently."""
        import inspect
        from carrac.reports import brief_archive

        src = inspect.getsource(brief_archive)
        assert 'general_report_public' in src, (
            "brief_archive must route through the scrubbing helper"
        )


# ── sibling: \bskew\b was deleting real prose ──────────────────────
#
# The pattern matched "historically a slight positive skew" in published report
# 414 and the scrub removed the whole sentence — an analog-study finding with no
# Deribit content in it. The exclusions were chosen by measuring every one of
# the 325 `skew` occurrences in the stored archive, not by inspection: the two
# obvious alternatives (require a derivatives keyword nearby; exclude
# "<direction> skew") would each have stopped scrubbing ~20 genuine options
# readings. See the comment on DERIBIT_PROSE_PATTERNS["skew_25d"].

import pytest as _pytest

from carrac.compliance.deribit_gate import _BARRED_RE as _SKEW_BARRED_RE


@_pytest.mark.parametrize("text", [
    # distribution shape of a return series (report 414)
    "The median 30-day move was +2.4% at a 68% hit rate — historically a "
    "slight positive skew even in risk-off setups.",
    # lead-lag projection range (report 169)
    "Showed -4.6% to +0.6% over 7 days and -1.4% to -6.6% over 30 days — "
    "modestly bearish skew.",
    # market breadth (report 402)
    "The lows-over-highs skew and sub-50% above the 50-day diverge from the "
    "Fear and Greed reading.",
    # ordinary verb/adjective forms, excluded by \b already
    "Positioning is skewed long but not at extremes.",
])
def test_non_options_skew_prose_survives(text):
    assert _SKEW_BARRED_RE.search(text) is None, (
        "a sentence with no Deribit content was scrubbed for the word 'skew'")


@_pytest.mark.parametrize("text", [
    "Skew at +10.8% means put options are bid.",
    "The 25-delta skew is flat at +9.3%.",
    "BTC P/C ratio 0.729 (more calls than puts — mild bullish skew).",
    "The positive skew (+19.1%) has not compressed, confirming put demand.",
    "By Friday, skew had normalized back to +8.4%.",
    "BTC derivatives skew stabilizes.",
    "Skew compression in BTC (from ~19 to ~15 in five days).",
])
def test_options_skew_is_still_barred(text):
    assert _SKEW_BARRED_RE.search(text) is not None, (
        "a Deribit-derived skew reading escaped the gate")
