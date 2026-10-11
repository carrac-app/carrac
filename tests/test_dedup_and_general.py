"""Tests for morning_engine.dedup and _generate_general_report.

Covers:
  1. check_intra_report — synthetic sections with planted duplicates
  2. check_cross_report_overlap — known-overlap scenarios
  3. _parse_x_posts — extraction + validation
  4. _generate_general_report — independence from morning report, dedup
     integration, X-candidates in metadata, corrective retry trigger

Pure-logic tests — no live API, no LLM.
"""

from __future__ import annotations

import time

import pytest

@pytest.fixture(autouse=True)
def _grounding_verifier_off(monkeypatch):
    """Generating a report runs the Haiku grounding verifier, which
    builds its OWN Anthropic client when none is injected
    (carrac/grounding/verify.py::verify_against_source) — a real, billed API call on any box
    where ANTHROPIC_API_KEY is set. Nothing in this file is about the verifier
    (tests/test_verify_wiring.py covers it), so use the product's own kill
    switch rather than a mock: verify_against_source short-circuits to CLEAN.
    """
    monkeypatch.setenv("GROUNDING_VERIFY_ENABLED", "0")


# ---------------------------------------------------------------------------
# 1. check_intra_report
# ---------------------------------------------------------------------------

class TestCheckIntraReport:
    """Verify sentence and fact deduplication logic."""

    def _make_report(self, *sections):
        """Build a simple markdown report from (heading, body) tuples."""
        parts = []
        for heading, body in sections:
            parts.append(f"## {heading}\n{body}")
        return "\n\n".join(parts)

    def test_no_duplicates_clean_report(self):
        from morning_engine.dedup import check_intra_report
        report = self._make_report(
            ("The Story", "Bitcoin dropped 3.5% as risk sentiment soured across markets yesterday."),
            ("Market Snapshot", "Gold rose 1.2% on safe-haven demand during the session."),
            ("What the Data Says", "The regime turned unfavorable with a z-score of -1.8."),
        )
        result = check_intra_report(report)
        assert result['dup_pair_count'] == 0
        assert result['dup_fact_count'] == 0
        assert result['exceeds_threshold'] is False

    def test_duplicate_sentence_detected_across_sections(self):
        from morning_engine.dedup import check_intra_report
        # Sentence must appear verbatim (normalised) in both sections — no prefix change.
        # One duplicate pair: detected (dup_pair_count == 1) but threshold = 2 pairs.
        dup_sentence = "Bitcoin fell sharply as global risk sentiment deteriorated rapidly overnight."
        report = self._make_report(
            ("The Story", f"{dup_sentence} Oil also declined."),
            ("What the Data Says", f"{dup_sentence} Further analysis follows here."),
        )
        result = check_intra_report(report)
        assert result['dup_pair_count'] >= 1  # detected
        # 1 pair is below the default threshold of 2 — exceeds_threshold is False here
        # (threshold enforcement tested separately in test_fact_in_three_sections_flagged
        # and the retry trigger test)

    def test_duplicate_sentences_at_threshold_exceeds(self):
        from morning_engine.dedup import check_intra_report, INTRA_DUP_SENTENCE_THRESHOLD
        # Plant exactly INTRA_DUP_SENTENCE_THRESHOLD duplicate pairs
        dup1 = "Bitcoin fell sharply as global risk sentiment deteriorated rapidly overnight."
        dup2 = "Gold surged as safe-haven demand overwhelmed profit-taking across all sessions."
        report = self._make_report(
            ("The Story", f"{dup1} {dup2}"),
            ("What the Data Says", f"{dup1} {dup2} Additional context."),
        )
        result = check_intra_report(report)
        assert result['dup_pair_count'] >= INTRA_DUP_SENTENCE_THRESHOLD
        assert result['exceeds_threshold'] is True

    def test_duplicate_sentence_under_8_words_not_flagged(self):
        from morning_engine.dedup import check_intra_report
        # Short sentence — below 8-word threshold
        report = self._make_report(
            ("The Story", "BTC fell hard. Some longer context here."),
            ("Market Snapshot", "BTC fell hard. Table below."),
        )
        result = check_intra_report(report)
        # "btc fell hard" = 3 words, should not be flagged
        assert result['dup_pair_count'] == 0

    def test_fact_in_three_sections_flagged(self):
        from morning_engine.dedup import check_intra_report
        # The number 83247 appears in 3 sections
        report = self._make_report(
            ("The Story", "BTC traded near $83,247 during the overnight session pullback."),
            ("What the Data Says", "At $83,247 BTC sits just below a key resistance cluster."),
            ("Market Snapshot", "BTC | $83,247 | bearish | RSI 42 | near support"),
        )
        result = check_intra_report(report)
        assert result['dup_fact_count'] >= 1
        assert result['exceeds_threshold'] is True

    def test_fact_in_two_sections_not_flagged(self):
        from morning_engine.dedup import check_intra_report
        # Same number in only 2 sections — below the 3-section threshold
        report = self._make_report(
            ("The Story", "BTC traded near $83,247 during the session."),
            ("Market Snapshot", "BTC | $83,247 | bearish"),
        )
        result = check_intra_report(report)
        assert result['dup_fact_count'] == 0

    def test_trivially_short_numbers_ignored(self):
        from morning_engine.dedup import check_intra_report
        # Single digit numbers (0-9) should not be flagged even if repeated
        report = self._make_report(
            ("TL;DR", "3 assets fell overnight."),
            ("The Story", "Only 3 sectors were positive."),
            ("Market Snapshot", "3 ETFs closed up."),
        )
        result = check_intra_report(report)
        assert result['dup_fact_count'] == 0

    def test_returns_offending_examples(self):
        from morning_engine.dedup import check_intra_report
        dup = "Global equities declined sharply as treasury yields rose well above expectations."
        report = self._make_report(
            ("The Story", dup),
            ("TL;DR", "Markets were down. " + dup),
        )
        result = check_intra_report(report)
        assert result['dup_pair_count'] >= 1
        # First element of each pair is the duplicate sentence
        dup_sentences = [pair[0] for pair in result['duplicate_sentence_pairs']]
        assert any('equities' in s for s in dup_sentences)

    def test_empty_report_returns_clean(self):
        from morning_engine.dedup import check_intra_report
        result = check_intra_report("")
        assert result['dup_pair_count'] == 0
        assert result['exceeds_threshold'] is False

    def test_single_section_never_flagged(self):
        from morning_engine.dedup import check_intra_report
        report = "## The Story\nBitcoin rose 2.4% as liquidity improved across all risk assets today."
        result = check_intra_report(report)
        assert result['dup_pair_count'] == 0
        assert result['exceeds_threshold'] is False


# ---------------------------------------------------------------------------
# 2. check_cross_report_overlap
# ---------------------------------------------------------------------------

class TestCheckCrossReportOverlap:
    def test_zero_overlap_when_different_content(self):
        from morning_engine.dedup import check_cross_report_overlap
        morning = "## The Story\nOil slid on OPEC supply fears while equities held up."
        general = "## The Story\nBitcoin formed a consolidation base near the prior highs."
        result = check_cross_report_overlap(morning, general)
        assert result['overlap_fraction'] == 0.0
        assert result['exceeds_threshold'] is False

    def test_high_overlap_when_sentences_duplicated(self):
        from morning_engine.dedup import check_cross_report_overlap
        shared = (
            "Bitcoin slid 4.1 percent as risk sentiment deteriorated across global markets.\n"
            "Gold rose on safe-haven demand while equities remained under moderate pressure.\n"
            "The derivatives regime turned cautious as funding rates flipped negative.\n"
        )
        morning = f"## The Story\n{shared}"
        general = f"## The Story\n{shared}"  # identical — 100% overlap
        result = check_cross_report_overlap(morning, general)
        assert result['overlap_fraction'] > 0.25
        assert result['exceeds_threshold'] is True

    def test_threshold_exactly_at_boundary(self):
        from morning_engine.dedup import check_cross_report_overlap
        # 0 overlap → not exceeded
        result = check_cross_report_overlap("## S\nFoo bar baz qux.", "## S\nAbc def ghi jkl.")
        assert result['exceeds_threshold'] is False

    def test_empty_general_is_zero_overlap(self):
        from morning_engine.dedup import check_cross_report_overlap
        result = check_cross_report_overlap("## The Story\nSome morning content.", "")
        assert result['overlap_fraction'] == 0.0
        assert result['general_sentence_count'] == 0
        assert result['exceeds_threshold'] is False

    def test_overlapping_sentences_list_populated(self):
        from morning_engine.dedup import check_cross_report_overlap
        shared = "The dollar strengthened as Fed officials signalled caution about rate cuts this year."
        morning = f"## The Story\n{shared}"
        general = f"## The Story\n{shared} Additional unique context here for the general report."
        result = check_cross_report_overlap(morning, general)
        # The shared sentence should appear in overlapping_sentences
        assert any('dollar' in s for s in result['overlapping_sentences'])


# ---------------------------------------------------------------------------
# 3. _parse_x_posts
# ---------------------------------------------------------------------------

class TestParseXPosts:
    def test_extracts_valid_posts_and_strips_section(self):
        import morning_engine.engine as eng
        content = (
            "## The Story\nBTC is steady.\n\n"
            "## Market Snapshot\n| BTC | 100 |\n\n"
            "## X-POSTS\n"
            "POST: BTC fell 4.1% in 24h to $82,143 — the largest single-day drop since March.\n"
            "POST: Gold touched $2,380 while equities slipped; classic risk-off rotation underway.\n"
            "POST: ETH funding turned negative for the first time in six weeks, suggesting short bias.\n"
        )
        clean, candidates = eng._parse_x_posts(content)
        assert '## X-POSTS' not in clean
        assert '## The Story' in clean
        assert '## Market Snapshot' in clean
        assert len(candidates) == 3
        assert 'BTC fell 4.1%' in candidates[0]

    def test_drops_post_exceeding_280_chars(self):
        import morning_engine.engine as eng
        long_post = "X" * 281
        content = (
            "## The Story\nOK.\n\n"
            "## X-POSTS\n"
            f"POST: {long_post}\n"
            "POST: BTC at $82,000 — unchanged after Fed minutes.\n"
        )
        _, candidates = eng._parse_x_posts(content)
        assert len(candidates) == 1
        assert 'BTC at $82,000' in candidates[0]

    def test_drops_post_with_advice_words(self):
        import morning_engine.engine as eng
        content = (
            "## The Story\nOK.\n\n"
            "## X-POSTS\n"
            "POST: You should buy BTC here — the dip is a gift.\n"
            "POST: ETH held its 200-day MA for the third consecutive session.\n"
        )
        _, candidates = eng._parse_x_posts(content)
        assert len(candidates) == 1
        assert 'ETH held' in candidates[0]

    def test_advice_word_buy_now_dropped(self):
        import morning_engine.engine as eng
        content = (
            "## The Story\nOK.\n\n"
            "## X-POSTS\n"
            "POST: Buy now — BTC at $82k is a gift.\n"
            "POST: DXY rose 0.4% as yields climbed back above 4.5%.\n"
        )
        _, candidates = eng._parse_x_posts(content)
        assert len(candidates) == 1

    def test_returns_empty_when_no_xposts_section(self):
        import morning_engine.engine as eng
        content = "## The Story\nBTC is steady.\n\n## Market Snapshot\n| BTC | 100 |"
        clean, candidates = eng._parse_x_posts(content)
        assert clean == content
        assert candidates == []

    def test_case_insensitive_section_detection(self):
        import morning_engine.engine as eng
        content = (
            "## The Story\nBTC moved.\n\n"
            "## x-posts\n"
            "POST: BTC dropped 3.2% while gold rose 0.8% in risk-off rotation.\n"
        )
        clean, candidates = eng._parse_x_posts(content)
        assert 'x-posts' not in clean.lower()
        assert len(candidates) == 1

    def test_exactly_280_chars_is_valid(self):
        import morning_engine.engine as eng
        post_280 = "B" * 280
        content = f"## The Story\nOK.\n\n## X-POSTS\nPOST: {post_280}\n"
        _, candidates = eng._parse_x_posts(content)
        assert len(candidates) == 1
        assert len(candidates[0]) == 280

    def test_all_fail_validation_returns_empty(self):
        import morning_engine.engine as eng
        content = (
            "## The Story\nOK.\n\n"
            "## X-POSTS\n"
            "POST: I recommend buying BTC now.\n"
            "POST: You should sell before the FOMC.\n"
        )
        _, candidates = eng._parse_x_posts(content)
        assert candidates == []

    # -- canonical advice-scan coverage -----------------------

    def test_drops_buy_the_dip_via_canonical_scan(self):
        """The narrow inline word-list only caught 'buy now'/'sell now' —
        not 'buy the dip'. The canonical carrac.grounding.advice_scan
        scanner catches this construction; this is the motivating case."""
        import morning_engine.engine as eng
        content = (
            "## The Story\nOK.\n\n"
            "## X-POSTS\n"
            "POST: Buy the dip — BTC at 62k.\n"
            "POST: ETH held its 200-day MA for the third consecutive session.\n"
        )
        _, candidates = eng._parse_x_posts(content)
        assert len(candidates) == 1
        assert 'ETH held' in candidates[0]

    def test_drops_you_ought_to_sell_via_canonical_scan(self):
        import morning_engine.engine as eng
        content = (
            "## The Story\nOK.\n\n"
            "## X-POSTS\n"
            "POST: You ought to sell before the FOMC decision drops.\n"
            "POST: DXY rose 0.4% as yields climbed back above 4.5%.\n"
        )
        _, candidates = eng._parse_x_posts(content)
        assert len(candidates) == 1
        assert 'DXY rose' in candidates[0]

    def test_clean_grounded_candidates_survive_canonical_scan(self):
        """The canonical scanner is deliberately high-precision (advice_scan
        docstring) so it must NOT false-positive on ordinary market words
        like 'buyers'/'sell-off'/'oversold' that a bare buy/sell match would
        have caught."""
        import morning_engine.engine as eng
        content = (
            "## The Story\nOK.\n\n"
            "## X-POSTS\n"
            "POST: BTC saw a sharp sell-off as buyers stepped away near $82,000 support.\n"
            "POST: RSI shows ETH is oversold at 28 after the overnight leg down.\n"
        )
        _, candidates = eng._parse_x_posts(content)
        assert len(candidates) == 2

    def test_missing_xposts_section_logs_warning(self, caplog):
        import logging
        import morning_engine.engine as eng
        content = "## The Story\nBTC is steady.\n\n## Market Snapshot\n| BTC | 100 |"
        with caplog.at_level(logging.WARNING, logger="morning_engine"):
            clean, candidates = eng._parse_x_posts(content)
        assert candidates == []
        assert any("x_posts:" in r.message for r in caplog.records)

    def test_all_fail_validation_logs_warning(self, caplog):
        import logging
        import morning_engine.engine as eng
        content = (
            "## The Story\nOK.\n\n"
            "## X-POSTS\n"
            "POST: I recommend buying BTC now.\n"
            "POST: You should sell before the FOMC.\n"
        )
        with caplog.at_level(logging.WARNING, logger="morning_engine"):
            _, candidates = eng._parse_x_posts(content)
        assert candidates == []
        warnings = [r.message for r in caplog.records if "x_posts:" in r.message]
        assert any("0 of 2" in w for w in warnings)


# ---------------------------------------------------------------------------
# 3b. _gate_filter_x_candidates — per-candidate hallucination-gate scan
# ---------------------------------------------------------------------------

class TestGateFilterXCandidates:
    """The blocking hallucination gate (_gate_scan / scan()) is
    stateless — scan(text, facts) needs only the candidate's own text, so it
    can be applied per-candidate without any refactor. These tests exercise
    _gate_filter_x_candidates directly by monkeypatching _gate_scan, so no
    real facts/DB/network lookups happen."""

    def test_high_severity_finding_drops_only_that_candidate(self, monkeypatch):
        import morning_engine.engine as eng
        from carrac.grounding.hallucination_scan import Finding, Severity

        def fake_gate_scan(text):
            if "FABRICATED" in text:
                return [Finding(
                    pattern_name="fabricated_fed_event",
                    severity=Severity.HIGH,
                    match_text="FABRICATED",
                    span=(0, 10),
                    context=text,
                    reason="test",
                )]
            return []

        monkeypatch.setattr(eng, "_gate_scan", fake_gate_scan)
        candidates = [
            "BTC held its 200-day MA for the third consecutive session.",
            "FABRICATED: the Fed cut rates this morning by 50bp.",
            "Gold rose 0.8% as the dollar weakened into the close.",
        ]
        survivors = eng._gate_filter_x_candidates(candidates)
        assert len(survivors) == 2
        assert not any("FABRICATED" in s for s in survivors)

    def test_clean_candidates_all_survive(self, monkeypatch):
        import morning_engine.engine as eng
        monkeypatch.setattr(eng, "_gate_scan", lambda text: [])
        candidates = ["BTC steady near support.", "ETH steady near resistance."]
        assert eng._gate_filter_x_candidates(candidates) == candidates

    def test_medium_severity_finding_does_not_drop(self, monkeypatch):
        """Only HIGH blocks — MEDIUM/LOW are the same non-blocking bar the
        whole-report gate uses; a per-candidate MEDIUM should not silently
        shrink the surviving list."""
        import morning_engine.engine as eng
        from carrac.grounding.hallucination_scan import Finding, Severity

        def fake_gate_scan(text):
            return [Finding(
                pattern_name="rate_cut_quantum", severity=Severity.MEDIUM,
                match_text="25bp", span=(0, 4), context=text, reason="",
            )]

        monkeypatch.setattr(eng, "_gate_scan", fake_gate_scan)
        candidates = ["Markets are pricing a 25bp cut next meeting."]
        assert eng._gate_filter_x_candidates(candidates) == candidates

    def test_empty_input_returns_empty(self, monkeypatch):
        import morning_engine.engine as eng
        monkeypatch.setattr(eng, "_gate_scan", lambda text: [])
        assert eng._gate_filter_x_candidates([]) == []


# ---------------------------------------------------------------------------
# 4. _generate_general_report — integration (no LLM)
# ---------------------------------------------------------------------------

class _FakeSonnet:
    """Returns a canned general-report response with X-POSTS section."""

    def __init__(self, response_body=None, fail_after=0):
        self.calls = 0
        self.fail_after = fail_after
        self._response_body = response_body or (
            "## TL;DR\n"
            "- BTC dropped 4.1% to $82,143 in the sharpest daily move since March.\n"
            "- Gold at $2,380 signals risk-off rotation as equities retreated.\n"
            "- The macro regime turned cautious on rising dollar strength.\n\n"
            "## The Story\n"
            "Markets entered a risk-off phase as the dollar index rose sharply.\n"
            "Crypto followed equities lower, with BTC testing the $82,000 support cluster.\n\n"
            "## What the Data Says\n"
            "1. Regime: unfavorable, early-cycle — 3d elapsed vs ~22d median.\n"
            "2. BTC z-score: -1.4 — below its 90-day mean, but not extreme.\n\n"
            "## Market Snapshot\n"
            "| Asset | Price | Trend | RSI | Range | Key Signal |\n"
            "| BTC | 82143 | bearish | 38 | inside | below 200MA |\n\n"
            "## One Thing to Learn Today\n"
            "Today illustrates the funding rate concept.\n"
            "When funding turns negative, shorts pay longs — a sign sentiment is bearish.\n"
            "**Key takeaway:** Negative funding = market leans short.\n\n"
            "## X-POSTS\n"
            "POST: BTC shed 4.1% to $82,143 — the funding rate just flipped negative for the first time in weeks.\n"
            "POST: Gold at $2,380 while BTC drops — classic risk-off rotation playing out in real time.\n"
            "POST: DXY rose 0.6% today; dollar strength is the headwind for risk assets this session.\n"
        )
        self.last_user_message = None

    def chat(self, system_prompt=None, user_message=None, **kw):
        self.calls += 1
        self.last_user_message = user_message
        if self.fail_after and self.calls > self.fail_after:
            raise RuntimeError("Fake LLM failure")
        return {'content': self._response_body}


class TestGenerateGeneralReport:
    def _shared_parts(self):
        return [
            '## Macro Context\nFed on hold; DXY soft.',
            '## Pre-Processed Signals\n{"BTC": {"rsi": 42}}',
            '## Quant Engine Context\nRegime: unfavorable, 3d elapsed.',
        ]

    def test_x_candidates_in_metadata(self, monkeypatch):
        import morning_engine.engine as eng
        posted = {}

        def fake_post(content, report_type=None, assets=None,
                      generation_time_ms=0, metadata=None, **kw):
            posted['report_type'] = report_type
            posted['content'] = content
            posted['metadata'] = metadata
            return 42

        monkeypatch.setattr(eng, '_post_report', fake_post)
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)

        sonnet = _FakeSonnet()
        eng._generate_general_report(
            shared_briefing_parts=self._shared_parts(),
            spotlight_symbols=['BTC'],
            compact_summary_text='GOLD +0.8%',
            steering='', patterns='',
            sonnet=sonnet, t_start=time.time(),
        )

        assert posted['report_type'] == 'general'
        meta = posted['metadata']
        assert meta is not None
        assert 'x_candidates' in meta
        assert isinstance(meta['x_candidates'], list)
        assert len(meta['x_candidates']) >= 1
        # X-posts block must not appear in the stored content
        assert '## X-POSTS' not in posted['content']
        assert 'X-POSTS' not in posted['content']

    def test_x_posts_stripped_from_stored_content(self, monkeypatch):
        import morning_engine.engine as eng
        stored = {}
        monkeypatch.setattr(eng, '_post_report',
                            lambda content, **kw: stored.__setitem__('content', content) or 1)
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)

        eng._generate_general_report(
            shared_briefing_parts=self._shared_parts(),
            spotlight_symbols=['BTC'], compact_summary_text='',
            steering='', patterns='', sonnet=_FakeSonnet(), t_start=time.time(),
        )
        assert 'POST:' not in stored.get('content', '')
        assert '## X-POSTS' not in stored.get('content', '')

    def test_no_operator_context_in_briefing(self, monkeypatch):
        """The general report user_message must NOT contain operator-private data."""
        import morning_engine.engine as eng
        monkeypatch.setattr(eng, '_post_report', lambda **kw: 1)
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)

        sonnet = _FakeSonnet()
        eng._generate_general_report(
            shared_briefing_parts=self._shared_parts(),
            spotlight_symbols=['BTC'], compact_summary_text='',
            steering='', patterns='', sonnet=sonnet, t_start=time.time(),
        )
        um = sonnet.last_user_message or ''
        assert '## Alerts' not in um
        assert '## Positions' not in um
        assert '## Yesterday' not in um
        # Shared macro/signals are present
        assert 'Macro Context' in um or 'Pre-Processed Signals' in um

    def test_dedup_metadata_populated(self, monkeypatch):
        import morning_engine.engine as eng
        posted = {}
        monkeypatch.setattr(eng, '_post_report',
                            lambda **kw: posted.__setitem__('meta', kw.get('metadata')) or 99)
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)

        eng._generate_general_report(
            shared_briefing_parts=self._shared_parts(),
            spotlight_symbols=['BTC'], compact_summary_text='',
            steering='', patterns='', sonnet=_FakeSonnet(), t_start=time.time(),
        )
        meta = posted.get('meta') or {}
        assert 'dedup' in meta
        assert 'dup_pair_count' in meta['dedup']
        assert 'intra_exceeded' in meta['dedup']

    def test_cross_overlap_in_metadata_when_morning_provided(self, monkeypatch):
        import morning_engine.engine as eng
        posted = {}
        monkeypatch.setattr(eng, '_post_report',
                            lambda **kw: posted.__setitem__('meta', kw.get('metadata')) or 99)
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)

        morning = "## The Story\nUnique operator morning content not in general report."
        eng._generate_general_report(
            shared_briefing_parts=self._shared_parts(),
            spotlight_symbols=['BTC'], compact_summary_text='',
            steering='', patterns='', sonnet=_FakeSonnet(), t_start=time.time(),
            morning_report=morning,
        )
        meta = posted.get('meta') or {}
        assert 'dedup' in meta
        assert 'overlap_vs_morning' in meta['dedup']
        # In this case overlap should be 0 since morning content is unrelated
        assert meta['dedup']['overlap_vs_morning'] is not None
        assert meta['dedup']['overlap_vs_morning'] == 0.0

    def test_disabled_flag_short_circuits(self, monkeypatch):
        import morning_engine.engine as eng
        called = {'posted': False}
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', False)
        monkeypatch.setattr(eng, '_post_report',
                            lambda *a, **k: called.__setitem__('posted', True) or 1)

        rid = eng._generate_general_report(
            shared_briefing_parts=[], spotlight_symbols=[],
            compact_summary_text='', steering='', patterns='',
            sonnet=_FakeSonnet(), t_start=time.time(),
        )
        assert rid is None
        assert called['posted'] is False

    def test_failure_is_non_fatal(self, monkeypatch):
        import morning_engine.engine as eng
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)

        class Boom:
            def chat(self, **kw):
                raise RuntimeError("LLM exploded")

        rid = eng._generate_general_report(
            shared_briefing_parts=self._shared_parts(),
            spotlight_symbols=['BTC'], compact_summary_text='',
            steering='', patterns='', sonnet=Boom(), t_start=time.time(),
        )
        assert rid is None

    def test_corrective_retry_triggered_on_dedup_threshold(self, monkeypatch):
        """When intra-dedup threshold is exceeded, a second LLM call is made."""
        import morning_engine.engine as eng

        # Plant INTRA_DUP_SENTENCE_THRESHOLD (2) duplicate sentence PAIRS:
        # dup1 and dup2 each appear verbatim in TWO sections (no bullet prefix to avoid normalisation mismatch).
        dup1 = "Bitcoin crashed dramatically as global risk assets collapsed under macro pressure."
        dup2 = "Gold surged to record highs as safe-haven demand overwhelmed selling pressure everywhere."
        high_dup_body = (
            "## The Story\n"
            f"{dup1}\n"
            f"{dup2}\n"
            "Oil also declined on demand fears.\n\n"
            "## What the Data Says\n"
            f"{dup1}\n"
            f"{dup2}\n"
            "1. Regime: unfavorable.\n\n"
            "## Market Snapshot\n"
            "| BTC | 82000 |\n\n"
            "## One Thing to Learn Today\n"
            "Funding rates explained via today's data.\n"
            "**Key takeaway:** Negative funding = bearish lean.\n\n"
            "## X-POSTS\n"
            "POST: BTC at $82,000 — funding flipped negative for first time in weeks.\n"
        )
        # After the retry, return a clean version (no dups)
        clean_body = (
            "## TL;DR\n"
            "- BTC dropped 4.1% to $82,000 in risk-off session.\n"
            "- Gold at $2,380 as DXY rose 0.6%.\n"
            "- The derivatives regime turned cautious.\n\n"
            "## The Story\n"
            "Markets saw broad risk-off selling as the dollar strengthened sharply.\n\n"
            "## What the Data Says\n"
            "1. Regime: unfavorable, early-cycle.\n\n"
            "## Market Snapshot\n"
            "| BTC | 82000 |\n\n"
            "## One Thing to Learn Today\n"
            "Funding rates explained via today's live data.\n"
            "**Key takeaway:** Negative funding = bearish market lean.\n\n"
            "## X-POSTS\n"
            "POST: BTC at $82,000 — funding flipped negative, suggesting short-side dominance.\n"
        )

        call_count = [0]

        class TwoStepSonnet:
            def chat(self, **kw):
                call_count[0] += 1
                if call_count[0] == 1:
                    return {'content': high_dup_body}
                return {'content': clean_body}

        posted = {}
        monkeypatch.setattr(eng, '_post_report',
                            lambda **kw: posted.__setitem__('meta', kw.get('metadata')) or 55)
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)

        eng._generate_general_report(
            shared_briefing_parts=self._shared_parts(),
            spotlight_symbols=['BTC'], compact_summary_text='',
            steering='', patterns='', sonnet=TwoStepSonnet(), t_start=time.time(),
        )
        # Must have been called at least twice (initial + corrective retry)
        assert call_count[0] >= 2
        # dedup metadata should note a retry happened
        meta = posted.get('meta') or {}
        assert meta.get('dedup', {}).get('corrective_retry') is True

    # -- per-candidate gate scan wired into the real pipeline --

    def test_gate_scan_drops_fabricated_candidate_others_survive(self, monkeypatch):
        """A candidate with a fabricated element the gate would catch is
        dropped from the stored x_candidates while the other, clean
        candidates survive — the whole report is NOT blocked."""
        import morning_engine.engine as eng
        from carrac.grounding.hallucination_scan import Finding, Severity

        body = (
            "## TL;DR\n- BTC steady overnight.\n\n"
            "## The Story\nMarkets traded in a narrow range overnight.\n\n"
            "## X-POSTS\n"
            "POST: FABTAG the Fed cut rates 50bp overnight, shocking markets.\n"
            "POST: BTC held its 200-day MA for the third straight session.\n"
            "POST: Gold rose 0.6% as the dollar softened into the close.\n"
        )

        posted = {}
        monkeypatch.setattr(eng, '_post_report',
                            lambda **kw: posted.__setitem__('meta', kw.get('metadata')) or 7)
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)

        def fake_gate_scan(text):
            if "FABTAG" in text:
                return [Finding(
                    pattern_name="fabricated_fed_event", severity=Severity.HIGH,
                    match_text="FABTAG", span=(0, 6), context=text, reason="test",
                )]
            return []

        monkeypatch.setattr(eng, '_gate_scan', fake_gate_scan)

        eng._generate_general_report(
            shared_briefing_parts=self._shared_parts(),
            spotlight_symbols=['BTC'], compact_summary_text='',
            steering='', patterns='', sonnet=_FakeSonnet(response_body=body),
            t_start=time.time(),
        )
        meta = posted.get('meta') or {}
        xc = meta.get('x_candidates', [])
        assert len(xc) == 2
        assert not any("FABTAG" in c for c in xc)
        # The whole-report gate must not have blocked the report over a
        # candidate-only fabrication (the tag never appears in the stored
        # report body) — _post_report was reached and metadata was captured.
        assert meta != {}

    def test_gate_scan_zero_survivors_logs_warning(self, monkeypatch, caplog):
        """When the gate scan drops every candidate, the "x_posts:" warning
        fires with parsed-vs-dropped counts — diagnosable on a shaky day."""
        import logging
        import morning_engine.engine as eng
        from carrac.grounding.hallucination_scan import Finding, Severity

        body = (
            "## TL;DR\n- BTC steady overnight.\n\n"
            "## The Story\nMarkets traded quietly overnight.\n\n"
            "## X-POSTS\n"
            "POST: FABTAG BTC fell hard overnight on thin liquidity.\n"
            "POST: FABTAG Gold rose slightly into the close.\n"
        )

        monkeypatch.setattr(eng, '_post_report', lambda **kw: 1)
        monkeypatch.setattr(eng, 'ENABLE_GENERAL_REPORT', True)

        def fake_gate_scan(text):
            if "FABTAG" in text:
                return [Finding(
                    pattern_name="fabricated_fed_event", severity=Severity.HIGH,
                    match_text="FABTAG", span=(0, 6), context=text, reason="test",
                )]
            return []

        monkeypatch.setattr(eng, '_gate_scan', fake_gate_scan)

        with caplog.at_level(logging.WARNING, logger="morning_engine"):
            eng._generate_general_report(
                shared_briefing_parts=self._shared_parts(),
                spotlight_symbols=['BTC'], compact_summary_text='',
                steering='', patterns='', sonnet=_FakeSonnet(response_body=body),
                t_start=time.time(),
            )
        warnings = [r.message for r in caplog.records if "x_posts:" in r.message]
        assert any("survived the hallucination-gate scan" in w for w in warnings)


# ---------------------------------------------------------------------------
# 5. build_dedup_correction_prompt
# ---------------------------------------------------------------------------

class TestBuildDedupCorrectionPrompt:
    def test_includes_report_text(self):
        from morning_engine.dedup import build_dedup_correction_prompt, check_intra_report
        report = (
            "## The Story\nBitcoin declined sharply as global risk appetite deteriorated across markets.\n\n"
            "## What the Data Says\nBitcoin declined sharply as global risk appetite deteriorated across markets.\n"
        )
        intra = check_intra_report(report)
        prompt = build_dedup_correction_prompt(report, intra)
        assert 'REPORT TO REWRITE' in prompt
        assert 'bitcoin declined sharply' in prompt.lower()

    def test_includes_cross_report_note(self):
        from morning_engine.dedup import build_dedup_correction_prompt
        intra = {'duplicate_sentence_pairs': [], 'duplicate_fact_sections': [],
                 'dup_pair_count': 0, 'dup_fact_count': 0, 'exceeds_threshold': False}
        cross = {'overlap_fraction': 0.35, 'exceeds_threshold': True,
                 'overlapping_sentences': ['btc fell as risk went off globally'],
                 'general_sentence_count': 10}
        prompt = build_dedup_correction_prompt("## S\nsome report text.", intra, cross)
        assert 'standalone' in prompt.lower() or 'cross-report' in prompt.lower()
        assert '35%' in prompt or '0.35' in prompt or '35' in prompt


class TestTokenBudgetGuard:
    """A too-small MAX_FREE_REPORT_TOKENS (e.g. 2000) silently truncates the
    general report mid-table, losing the One Thing to Learn Today section and
    the X-POSTS block (0 x_candidates). Guard the floor."""

    def test_free_report_token_budget_fits_section_plan(self):
        from morning_engine.config import MAX_FREE_REPORT_TOKENS
        # 6 sections (~8K chars ≈ 2700 tokens) + X-POSTS tail + headroom
        assert MAX_FREE_REPORT_TOKENS >= 3000

    def test_chat_returns_stop_reason_key(self):
        import inspect
        from carrac.llm import morning_anthropic
        src = inspect.getsource(morning_anthropic.MorningAnthropicProvider.chat)
        assert "'stop_reason': stop_reason" in src
        assert 'max_tokens' in src
