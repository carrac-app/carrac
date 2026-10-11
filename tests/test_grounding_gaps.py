"""Three gates found leaking by a grounding sweep.

All three were found by RUNNING the gate over the real stored corpus, not by
reading it. Every string below is real text from a stored prod report or a
stored x_candidate.

  339  27 of 193 stored tweet candidates carry Deribit prose. A scrub
       report CONTENT; x_candidates live in report METADATA and were untouched.
  340  scan_advice caught "Reduce or hedge" twice out of four live instances.
       The pattern needs an object noun within 40 chars AFTER the verb, so
       "Reduce or hedge rather than chase" (no object) and "Altcoin exposure —
       Reduce or hedge" (object before the verb) both walked through.
  341  _POSITION_LINE_RE matches `entr(y|ies) \\$` — the dollar sign immediately
       after — so "Entry at $2,251" survives while "entered at $2,251" is cut.
"""
from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────────
# The advice tripwire's blind spots
# ─────────────────────────────────────────────────────────────────────────
class TestAdviceScannerCatchesTheRealIdiom:
    """The four live instances, verbatim from stored afternoon reports."""

    CAUGHT_ALREADY = [
        "opens a path toward $1,767 (CPR BC) and ultimately the $1,617 S1. "
        "Reduce or hedge ETH longs if this level fails on a daily close.",
    ]
    WAS_BLIND = [
        "- **Altcoin exposure — Reduce or hedge:** The breadth of RSI oversold "
        "entries across 40+ names is a mean-reversion setup.",
        "Weekly RSI overbought suggests ETH outperformance may be near-term "
        "exhausted. Reduce or hedge rather than chase.",
    ]

    @pytest.mark.parametrize("text", CAUGHT_ALREADY)
    def test_the_instances_it_already_caught_still_fire(self, text):
        from carrac.grounding.advice_scan import scan_advice
        assert scan_advice(text), "this phrasing must be flagged as advice"

    @pytest.mark.parametrize("text", WAS_BLIND)
    def test_the_instances_it_missed_now_fire(self, text):
        from carrac.grounding.advice_scan import scan_advice
        assert scan_advice(text), "shipped to readers with no gate firing"

    @pytest.mark.parametrize("text", [
        "Now is the time to rotate out of tech.",
        "Hedge or reduce crypto exposure into the print.",
    ])
    def test_unambiguous_advice_framing_fires(self, text):
        from carrac.grounding.advice_scan import scan_advice
        assert scan_advice(text)

    @pytest.mark.parametrize("text", [
        # The other side. Over-matching turns the tripwire into noise, and a
        # guard you learn to ignore is worse than no guard.
        "Copper is at 97% of its 3-month range with a golden cross.",
        "The Fed reduced the target range by 25bp in June.",
        "Implied volatility reduced sharply after the print.",
        "Dealers hedge their gamma exposure mechanically.",
        "Watch for a close below $66,920.",          # deliberately permitted
        "Monitor the BC CPR level into the close.",  # deliberately permitted
        "Breadth reduced to 34% of names above their 50-day.",
    ])
    def test_observational_prose_stays_clean(self, text):
        from carrac.grounding.advice_scan import scan_advice
        assert scan_advice(text) == [], f"over-matched: {scan_advice(text)}"


# ─────────────────────────────────────────────────────────────────────────
# The operator-position backstop
# ─────────────────────────────────────────────────────────────────────────
class TestPositionScrubCatchesBothPhrasings:
    def test_the_phrasing_it_already_caught(self):
        from carrac.reports.sanitize import scrub_position_lines
        line = "The ETH long entered at $2,251 with a stop at $2,009 was invalidated."
        assert scrub_position_lines(line).strip() == ""

    def test_the_phrasing_it_missed(self):
        """Real line from a morning report."""
        from carrac.reports.sanitize import scrub_position_lines
        line = "**ETH Long Position Deep Underwater**: Entry at $2,251, current price $1,630."
        assert scrub_position_lines(line).strip() == ""

    @pytest.mark.parametrize("line", [
        "Entries at $2,251 were structurally breached.",
        "Entry: $2,251 — down 27.6% from here.",
    ])
    def test_sibling_phrasings(self, line):
        from carrac.reports.sanitize import scrub_position_lines
        assert scrub_position_lines(line).strip() == ""

    @pytest.mark.parametrize("line", [
        "SPX is at $7,437 with support 0.7% below.",
        "The entry level for the pattern sits near the 50-day.",
        "Gold broke out to a new high at $4,120.",
    ])
    def test_market_commentary_survives(self, line):
        """scrub_position_lines drops WHOLE lines — over-matching here deletes
        legitimate commentary from every non-operator report."""
        from carrac.reports.sanitize import scrub_position_lines
        assert scrub_position_lines(line).strip() == line.strip()


# ─────────────────────────────────────────────────────────────────────────
# Deribit prose in tweet candidates
# ─────────────────────────────────────────────────────────────────────────
class TestTweetCandidatesAreLicenceScrubbed:
    """Real stored candidates. A tweet is 280 chars, so scrubbing usually
    empties it — dropping the candidate is the correct outcome, not publishing
    a mutilated one."""

    DIRTY = ("BTC DVOL at 34.4 is at cycle lows — maximum vol compression, with "
             "spot sitting below the GEX flip at $66,372.")
    CLEAN = ("Copper is at 97% of its 3-month range with a golden cross and "
             "RS score 94.3, leading the commodity complex.")

    def test_a_candidate_narrating_deribit_data_is_dropped(self):
        from morning_engine.engine import _gate_filter_x_candidates
        assert _gate_filter_x_candidates([self.DIRTY]) == []

    def test_a_clean_candidate_survives_untouched(self):
        from morning_engine.engine import _gate_filter_x_candidates
        assert _gate_filter_x_candidates([self.CLEAN]) == [self.CLEAN]

    def test_mixed_batch_keeps_only_the_clean_one(self):
        from morning_engine.engine import _gate_filter_x_candidates
        assert _gate_filter_x_candidates([self.DIRTY, self.CLEAN]) == [self.CLEAN]

    def test_the_serving_path_scrubs_stored_candidates_too(self):
        """Generation-side is the primary fix, but 27 candidates are ALREADY
        stored — the API must not serve them while they sit in the DB."""
        from carrac.reports.sanitize import public_x_candidates
        assert public_x_candidates([self.DIRTY, self.CLEAN]) == [self.CLEAN]

    def test_serving_path_tolerates_junk(self):
        from carrac.reports.sanitize import public_x_candidates
        assert public_x_candidates(None) == []
        assert public_x_candidates([]) == []
        assert public_x_candidates(["", None, 42]) == []

    def test_the_social_pipeline_cannot_pick_a_barred_caption(self):
        """daily_card takes candidates[0] verbatim as the tweet caption. If the
        first stored candidate is barred, it must not become a tweet."""
        import carrac.social.daily_card as DC

        class _DB:
            def get_latest_report(self, report_type=None):
                import json
                return {'id': 1, 'metadata': json.dumps(
                    {'x_candidates': [TestTweetCandidatesAreLicenceScrubbed.DIRTY,
                                      TestTweetCandidatesAreLicenceScrubbed.CLEAN]})}

        import carrac.content_db as CDB
        _orig = CDB.get_content_db
        CDB.get_content_db = lambda: _DB()
        try:
            assert DC.fetch_first_x_candidate() == TestTweetCandidatesAreLicenceScrubbed.CLEAN
        finally:
            CDB.get_content_db = _orig


# ─────────────────────────────────────────────────────────────────────────
# Silent asset substitution
# ─────────────────────────────────────────────────────────────────────────
class TestQuantDataRefusesUnknownAssets:
    """`build_snapshot` whitelists against TARGET_ASSETS for SQL safety and
    then silently falls back to BTC. On the HTTP route a default is defensible;
    on the agent surface it means an LLM that asked for PEPE gets BTC's
    correlations and lead-lag back and may narrate them as PEPE."""

    def test_an_uncovered_asset_is_refused_not_substituted(self):
        from carrac.mcp.tools.analysis import quant_get_data
        from carrac.mcp.protocol import ToolError

        with pytest.raises(ToolError) as e:
            quant_get_data({'asset': 'ZZZFAKE123', 'query_type': 'snapshot'})
        msg = str(e.value)
        assert 'ZZZFAKE123' in msg, "say what was asked for"
        assert 'BTC' in msg, "and name what IS covered"

    def test_the_error_names_the_covered_universe(self):
        from carrac.mcp.tools.analysis import quant_get_data
        from carrac.mcp.protocol import ToolError
        from quant.engine.config import TARGET_ASSETS

        with pytest.raises(ToolError) as e:
            quant_get_data({'asset': 'PEPE', 'query_type': 'snapshot'})
        msg = str(e.value)
        assert sum(1 for a in TARGET_ASSETS if a in msg) >= 2

    def test_an_injection_string_is_refused(self):
        from carrac.mcp.tools.analysis import quant_get_data
        from carrac.mcp.protocol import ToolError
        with pytest.raises(ToolError):
            quant_get_data({'asset': '../../etc/passwd', 'query_type': 'snapshot'})

    def test_omitting_the_asset_still_defaults_to_btc(self):
        """The default is documented and fine — only an EXPLICIT unknown asset
        is an error. Breaking the default would break every existing caller."""
        from carrac.mcp.tools.analysis import quant_get_data
        from carrac.mcp.protocol import ToolError
        try:
            quant_get_data({'query_type': 'snapshot'})
        except ToolError as e:
            assert 'not covered' not in str(e), f"default path rejected: {e}"
        except Exception:
            pass  # DB unavailable in unit context — the asset check is what matters
