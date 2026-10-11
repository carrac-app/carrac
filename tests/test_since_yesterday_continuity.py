"""Tests for daily-continuity feature: since-yesterday data assembly.

Covers:
  1. fetch_signal_flips — formatting of /api/report/diff changes
  2. fetch_yesterday_report — date filtering against recent reports list
  3. format_since_yesterday_payload — signal flips, regime delta, self-grade
     with and without yesterday's report
  4. format_since_yesterday_block — rendered block presence/absence
  5. fetch_quant_context — yesterday's events actuals injection
  6. parseReportSections (JS) — 'Since Yesterday' matcher (Python regex test)

Pure-logic tests — no live API, no LLM, no DB.
"""

from __future__ import annotations

import datetime



# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _make_diff_response(changes):
    return {
        "status": "ok",
        "changes": changes,
        "high_count": sum(1 for c in changes if c.get("significance") == "high"),
        "medium_count": sum(1 for c in changes if c.get("significance") == "medium"),
    }


def _make_report(content, days_ago=1):
    """Build a fake report dict as returned by /api/report/morning/recent."""
    date = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).strftime('%Y-%m-%d')
    return {
        "id": 42,
        "content": content,
        "created_at": f"{date}T07:00:00",
        "audio_script": None,
    }


# ---------------------------------------------------------------------------
# 1. fetch_signal_flips
# ---------------------------------------------------------------------------

class TestFetchSignalFlips:

    def test_returns_empty_on_empty_changes(self, monkeypatch):
        from morning_engine import data_assembly as da
        monkeypatch.setattr(da, '_api', lambda *a, **kw: _make_diff_response([]))
        result = da.fetch_signal_flips()
        assert result == []

    def test_formats_high_flip_correctly(self, monkeypatch):
        from morning_engine import data_assembly as da
        changes = [
            {"symbol": "BTC", "field": "signal_1d",
             "change_type": "1D signal flip (bullish)", "significance": "high",
             "old_value": "bearish", "new_value": "bullish"},
        ]
        monkeypatch.setattr(da, '_api', lambda *a, **kw: _make_diff_response(changes))
        result = da.fetch_signal_flips()
        assert len(result) == 1
        assert "[HIGH]" in result[0]
        assert "BTC" in result[0]
        assert "bullish" in result[0].lower()

    def test_formats_multiple_flips(self, monkeypatch):
        from morning_engine import data_assembly as da
        changes = [
            {"symbol": "BTC", "change_type": "Golden Cross (SMA20 > SMA200)", "significance": "high"},
            {"symbol": "ETH", "change_type": "RSI entered oversold (<30)", "significance": "medium"},
            {"symbol": "SPX", "change_type": "1D signal flip (bearish)", "significance": "low"},
        ]
        monkeypatch.setattr(da, '_api', lambda *a, **kw: _make_diff_response(changes))
        result = da.fetch_signal_flips()
        assert len(result) == 3
        symbols = [r.split("]")[1].strip().split(":")[0] for r in result]
        assert "BTC" in symbols
        assert "ETH" in symbols

    def test_returns_empty_on_api_failure(self, monkeypatch):
        from morning_engine import data_assembly as da
        def _fail(*a, **kw):
            raise ConnectionError("network error")
        monkeypatch.setattr(da, '_api', _fail)
        result = da.fetch_signal_flips()
        assert result == []

    def test_returns_empty_when_api_returns_unexpected_shape(self, monkeypatch):
        from morning_engine import data_assembly as da
        monkeypatch.setattr(da, '_api', lambda *a, **kw: "not a dict")
        result = da.fetch_signal_flips()
        assert result == []

    def test_all_flips_returned_cap_is_in_block_renderer(self, monkeypatch):
        """fetch_signal_flips returns all flips; the :8 cap lives in format_since_yesterday_block."""
        from morning_engine import data_assembly as da
        changes = [
            {"symbol": f"A{i}", "change_type": "flip", "significance": "medium"}
            for i in range(12)
        ]
        monkeypatch.setattr(da, '_api', lambda *a, **kw: _make_diff_response(changes))
        result = da.fetch_signal_flips()
        # All 12 returned from the function
        assert len(result) == 12
        # Block renderer caps at 8
        payload = da.format_since_yesterday_payload(
            signal_flips=result,
            yesterday_regime=None,
            quant_context={"quant": "Regime: NEUTRAL (55% confidence)"},
        )
        block_flips = payload.get('flips_text', '')
        assert block_flips.count('[MEDIUM]') == 8


# ---------------------------------------------------------------------------
# 2. fetch_yesterday_report
# ---------------------------------------------------------------------------

class TestFetchYesterdayReport:

    def test_returns_yesterday_report_when_present(self, monkeypatch):
        from morning_engine import data_assembly as da
        yesterday_report = _make_report("## The Story\nBTC fell.\n\n## Scenarios\nBullish 60%.", days_ago=1)
        today_report = _make_report("## The Story\nBTC steady.", days_ago=0)
        monkeypatch.setattr(da, '_api', lambda *a, **kw: [today_report, yesterday_report])
        result = da.fetch_yesterday_report(report_type='morning')
        assert result is not None
        assert "BTC fell" in result.get("content", "")

    def test_returns_none_when_no_yesterday_in_list(self, monkeypatch):
        from morning_engine import data_assembly as da
        # Only today's report — no yesterday
        today_report = _make_report("## The Story\nBTC steady.", days_ago=0)
        monkeypatch.setattr(da, '_api', lambda *a, **kw: [today_report])
        result = da.fetch_yesterday_report(report_type='morning')
        assert result is None

    def test_returns_none_on_empty_list(self, monkeypatch):
        from morning_engine import data_assembly as da
        monkeypatch.setattr(da, '_api', lambda *a, **kw: [])
        result = da.fetch_yesterday_report(report_type='morning')
        assert result is None

    def test_returns_none_on_api_failure(self, monkeypatch):
        from morning_engine import data_assembly as da
        def _fail(*a, **kw):
            raise ConnectionError("down")
        monkeypatch.setattr(da, '_api', _fail)
        result = da.fetch_yesterday_report(report_type='morning')
        assert result is None

    def test_returns_none_on_non_list_response(self, monkeypatch):
        from morning_engine import data_assembly as da
        monkeypatch.setattr(da, '_api', lambda *a, **kw: {"error": "not a list"})
        result = da.fetch_yesterday_report(report_type='morning')
        assert result is None


# ---------------------------------------------------------------------------
# 3. format_since_yesterday_payload — with yesterday report
# ---------------------------------------------------------------------------

class TestFormatSinceYesterdayPayloadWithReport:

    def _make_quant_context(self, state="UNFAVORABLE", prob=72):
        return {"quant": f"Regime: {state} ({prob}% confidence)\nSome other line."}

    def test_has_data_true_when_flips_present(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        payload = format_since_yesterday_payload(
            signal_flips=["[HIGH] BTC: 1D signal flip (bullish)"],
            yesterday_regime=None,
            quant_context=self._make_quant_context(),
        )
        assert payload['has_data'] is True

    def test_flips_text_contains_all_flips(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        flips = [
            "[HIGH] BTC: 1D signal flip (bullish)",
            "[MEDIUM] ETH: RSI entered oversold (<30)",
        ]
        payload = format_since_yesterday_payload(
            signal_flips=flips,
            yesterday_regime=None,
            quant_context=self._make_quant_context(),
        )
        assert "BTC" in payload['flips_text']
        assert "ETH" in payload['flips_text']

    def test_regime_flip_detected(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        payload = format_since_yesterday_payload(
            signal_flips=[],
            yesterday_regime={"state": "FAVORABLE", "probability": 65},
            quant_context=self._make_quant_context(state="UNFAVORABLE", prob=72),
        )
        assert "REGIME FLIP" in payload.get('regime_change', '')
        assert "FAVORABLE" in payload['regime_change']
        assert "UNFAVORABLE" in payload['regime_change']
        assert payload['has_data'] is True

    def test_regime_unchanged_text(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        payload = format_since_yesterday_payload(
            signal_flips=[],
            yesterday_regime={"state": "UNFAVORABLE", "probability": 70},
            quant_context=self._make_quant_context(state="UNFAVORABLE", prob=72),
        )
        assert "unchanged" in payload.get('regime_change', '').lower()

    def test_self_grade_extracts_scenarios_section(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        report_content = (
            "## The Story\nBTC was choppy.\n\n"
            "## Scenarios\nBase case: range-bound 55% (BTC holds $65k).\n"
            "Bull case: breakout to $72k.\n"
            "Bear case: flush to $60k.\n\n"
            "## Market Snapshot\n| BTC | $65,000 |\n"
        )
        report = {"content": report_content, "created_at": "2026-06-10T07:00:00", "id": 1}
        payload = format_since_yesterday_payload(
            signal_flips=[],
            yesterday_regime=None,
            quant_context=self._make_quant_context(),
            yesterday_report=report,
        )
        assert "Scenarios" not in payload.get('yesterday_excerpt', '')  # heading stripped
        assert "Base case" in payload.get('yesterday_excerpt', '')
        assert payload['has_data'] is True

    def test_self_grade_falls_back_to_tldr_if_no_scenarios(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        report_content = (
            "## TL;DR\n- BTC fell 3.2%.\n- Gold up 1.1%.\n\n"
            "## The Story\nMarkets were risk-off.\n"
        )
        report = {"content": report_content, "created_at": "2026-06-10T07:00:00", "id": 2}
        payload = format_since_yesterday_payload(
            signal_flips=[],
            yesterday_regime=None,
            quant_context=self._make_quant_context(),
            yesterday_report=report,
        )
        assert "BTC fell 3.2%" in payload.get('yesterday_excerpt', '')

    def test_excerpt_truncated_to_600_chars(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        long_body = "A " * 400  # 800 chars
        report_content = f"## Scenarios\n{long_body}\n\n## Market Snapshot\n| x |\n"
        report = {"content": report_content, "created_at": "2026-06-10T07:00:00", "id": 3}
        payload = format_since_yesterday_payload(
            signal_flips=[],
            yesterday_regime=None,
            quant_context=self._make_quant_context(),
            yesterday_report=report,
        )
        assert len(payload.get('yesterday_excerpt', '')) <= 600

    def test_today_prices_extracted_from_chart_raw(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        chart_raw = {
            ("BTC", "1D"): {"ohlcv": [{"close": 65432.10}]},
            ("ETH", "1D"): {"ohlcv": [{"close": 3210.50}]},
            ("BTC", "4H"): {"ohlcv": [{"close": 65000.00}]},  # 4H ignored
        }
        payload = format_since_yesterday_payload(
            signal_flips=[],
            yesterday_regime=None,
            quant_context=self._make_quant_context(),
            chart_raw=chart_raw,
        )
        prices = payload.get('today_prices', {})
        assert "BTC" in prices
        assert abs(prices["BTC"] - 65432.10) < 0.01
        assert "ETH" in prices
        # 4H should not duplicate BTC
        assert len(prices) == 2


# ---------------------------------------------------------------------------
# 3b. format_since_yesterday_payload — WITHOUT yesterday report (first-run)
# ---------------------------------------------------------------------------

class TestFormatSinceYesterdayPayloadNoReport:

    def test_has_data_false_when_no_flips_no_report_no_regime(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        payload = format_since_yesterday_payload(
            signal_flips=[],
            yesterday_regime=None,
            quant_context={"quant": "Quant Engine: no data available."},
        )
        assert payload['has_data'] is False

    def test_yesterday_excerpt_empty_when_no_report(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        payload = format_since_yesterday_payload(
            signal_flips=[],
            yesterday_regime=None,
            quant_context={"quant": "Regime: NEUTRAL (50% confidence)"},
        )
        assert payload.get('yesterday_excerpt', '') == ''

    def test_no_flips_produces_placeholder_text(self):
        from morning_engine.data_assembly import format_since_yesterday_payload
        payload = format_since_yesterday_payload(
            signal_flips=[],
            yesterday_regime=None,
            quant_context={"quant": "Regime: NEUTRAL (50% confidence)"},
        )
        flips_text = payload.get('flips_text', '')
        # Placeholder text indicates no flips; contains "no" or "none"
        assert "no" in flips_text.lower()


# ---------------------------------------------------------------------------
# 4. format_since_yesterday_block — rendering
# ---------------------------------------------------------------------------

class TestFormatSinceYesterdayBlock:

    def test_returns_empty_string_when_no_data(self):
        from morning_engine.data_assembly import format_since_yesterday_block
        payload = {'has_data': False}
        assert format_since_yesterday_block(payload) == ''

    def test_block_contains_section_heading(self):
        from morning_engine.data_assembly import (
            format_since_yesterday_payload, format_since_yesterday_block,
        )
        payload = format_since_yesterday_payload(
            signal_flips=["[HIGH] BTC: Golden Cross"],
            yesterday_regime={"state": "FAVORABLE", "probability": 65},
            quant_context={"quant": "Regime: UNFAVORABLE (72% confidence)"},
        )
        block = format_since_yesterday_block(payload)
        assert "## Since Yesterday" in block

    def test_block_contains_flips(self):
        from morning_engine.data_assembly import (
            format_since_yesterday_payload, format_since_yesterday_block,
        )
        flips = ["[HIGH] BTC: 1D signal flip (bullish)"]
        payload = format_since_yesterday_payload(
            signal_flips=flips,
            yesterday_regime=None,
            quant_context={"quant": "Regime: UNFAVORABLE (72% confidence)"},
        )
        block = format_since_yesterday_block(payload)
        assert "BTC" in block

    def test_block_contains_regime_info(self):
        from morning_engine.data_assembly import (
            format_since_yesterday_payload, format_since_yesterday_block,
        )
        payload = format_since_yesterday_payload(
            signal_flips=["[HIGH] BTC: flip"],
            yesterday_regime={"state": "FAVORABLE", "probability": 60},
            quant_context={"quant": "Regime: UNFAVORABLE (72% confidence)"},
        )
        block = format_since_yesterday_block(payload)
        assert "REGIME FLIP" in block or "Regime" in block

    def test_block_contains_yesterday_excerpt_when_present(self):
        from morning_engine.data_assembly import (
            format_since_yesterday_payload, format_since_yesterday_block,
        )
        report = {
            "content": "## Scenarios\nBase case: range-bound 55%.\n",
            "created_at": "2026-06-10T07:00:00", "id": 1,
        }
        payload = format_since_yesterday_payload(
            signal_flips=["[HIGH] ETH: flip"],
            yesterday_regime=None,
            quant_context={"quant": "Regime: NEUTRAL (55% confidence)"},
            yesterday_report=report,
        )
        block = format_since_yesterday_block(payload)
        assert "Base case" in block

    def test_block_contains_today_prices_when_present(self):
        from morning_engine.data_assembly import (
            format_since_yesterday_payload, format_since_yesterday_block,
        )
        chart_raw = {("BTC", "1D"): {"ohlcv": [{"close": 67000.0}]}}
        payload = format_since_yesterday_payload(
            signal_flips=["[HIGH] BTC: flip"],
            yesterday_regime=None,
            quant_context={"quant": "Regime: UNFAVORABLE (72% confidence)"},
            chart_raw=chart_raw,
        )
        block = format_since_yesterday_block(payload)
        assert "67,000" in block or "67000" in block


# ---------------------------------------------------------------------------
# 5. fetch_quant_context — yesterday events actuals injection
# ---------------------------------------------------------------------------

class TestQuantContextYesterdayEvents:
    """Verify that fetch_quant_context injects yesterday's FRED actuals."""

    def _make_events(self, include_actuals=True):
        if include_actuals:
            return [
                {"id": 1, "title": "CPI MoM", "ts": "2026-06-10T12:30:00",
                 "impact": "high", "actual": "+0.5%", "actual_filled_at": "2026-06-10T13:00:00"},
                {"id": 2, "title": "PPI MoM", "ts": "2026-06-10T12:30:00",
                 "impact": "medium", "actual": "+0.3%", "actual_filled_at": "2026-06-10T13:05:00"},
            ]
        else:
            return [
                {"id": 3, "title": "FOMC Minutes", "ts": "2026-06-10T14:00:00",
                 "impact": "high", "actual": None, "actual_filled_at": None},
            ]

    def _make_api_stub(self, yesterday_events, upcoming_events=None):
        """Return a stub for _api that serves different responses by URL."""
        def _api(path, timeout=15, retries=2):
            if 'regime-history' in path:
                return []
            if 'analogues' in path:
                return []
            if 'anomalies' in path:
                return {}
            if 'events' in path:
                # Distinguish yesterday vs upcoming by the date params
                import datetime
                yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
                if f'from={yesterday}&to={yesterday}' in path:
                    return yesterday_events
                return upcoming_events or []
            # All other quant paths
            return {}
        return _api

    def test_yesterday_actuals_injected_into_quant_text(self, monkeypatch):
        from morning_engine import data_assembly as da
        monkeypatch.setattr(da, '_api', self._make_api_stub(self._make_events(True)))
        result = da.fetch_quant_context()
        quant_text = result.get('quant', '')
        assert "Yesterday's releases" in quant_text
        assert "CPI MoM" in quant_text
        assert "+0.5%" in quant_text

    def test_no_actuals_line_when_events_have_no_actuals(self, monkeypatch):
        from morning_engine import data_assembly as da
        monkeypatch.setattr(da, '_api', self._make_api_stub(self._make_events(False)))
        result = da.fetch_quant_context()
        quant_text = result.get('quant', '')
        # Events exist but no actuals → no "Yesterday's releases" line
        assert "Yesterday's releases" not in quant_text

    def test_no_actuals_line_when_no_events(self, monkeypatch):
        from morning_engine import data_assembly as da
        monkeypatch.setattr(da, '_api', self._make_api_stub([]))
        result = da.fetch_quant_context()
        quant_text = result.get('quant', '')
        assert "Yesterday's releases" not in quant_text

    def test_yesterday_events_api_failure_does_not_crash(self, monkeypatch):
        """A failure in the yesterday-events fetch must not crash fetch_quant_context."""
        from morning_engine import data_assembly as da

        def _api(path, timeout=15, retries=2):
            import datetime
            yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
            if f'from={yesterday}&to={yesterday}' in path:
                raise ConnectionError("simulated outage")
            return {} if 'events' not in path else []

        monkeypatch.setattr(da, '_api', _api)
        result = da.fetch_quant_context()
        # Should succeed even with yesterday-events failure
        assert 'quant' in result

    def test_impact_tag_present_in_output(self, monkeypatch):
        from morning_engine import data_assembly as da
        monkeypatch.setattr(da, '_api', self._make_api_stub(self._make_events(True)))
        result = da.fetch_quant_context()
        quant_text = result.get('quant', '')
        # HIGH impact event should appear with impact tag
        assert "[HIGH]" in quant_text


# ---------------------------------------------------------------------------
# 6. Prompt-content assertions — Wire 1/2/3 guidance present in prompts
# ---------------------------------------------------------------------------

class TestPromptContentWires:
    """Verify that MOVE, breadth, and COT instructions appear in the right prompts."""

    def test_shared_report_system_prompt_contains_move_instruction(self):
        from morning_engine.prompts import shared_report_system_prompt
        text = shared_report_system_prompt('', '')
        assert 'MOVE' in text
        # Should mention using it as macro-stress gauge
        assert 'macro-stress' in text or 'stress gauge' in text

    def test_shared_report_system_prompt_contains_breadth_instruction(self):
        from morning_engine.prompts import shared_report_system_prompt
        text = shared_report_system_prompt('', '')
        assert 'breadth' in text.lower()
        # Should mention the washed-out or frothy labels
        assert 'washed-out' in text or 'frothy' in text

    def test_free_tier_report_prompt_contains_move_instruction(self):
        from morning_engine.prompts import free_tier_report_prompt
        text = free_tier_report_prompt('', '')
        assert 'MOVE' in text
        # Should explain it as bond-market volatility
        assert 'bond' in text.lower()

    def test_free_tier_report_prompt_contains_breadth_lead_instruction(self):
        from morning_engine.prompts import free_tier_report_prompt
        text = free_tier_report_prompt('', '')
        assert 'breadth' in text.lower()
        # Should mention leading with it when washed-out or frothy
        assert 'washed-out' in text or 'frothy' in text

    def test_afternoon_delta_prompt_contains_breadth_note(self):
        from morning_engine.prompts import afternoon_delta_prompt
        text = afternoon_delta_prompt()
        assert 'breadth' in text.lower()

    def test_weekly_digest_prompt_contains_cot_guidance(self):
        from morning_engine.prompts import weekly_digest_prompt
        text = weekly_digest_prompt()
        assert 'COT' in text
        # Should mention stretched and crowded trade
        assert 'stretched' in text.lower()
        assert 'crowded' in text.lower()

    def test_weekly_digest_prompt_contains_commercials_disclaimer(self):
        from morning_engine.prompts import weekly_digest_prompt
        text = weekly_digest_prompt()
        # Commercials-are-structural disclaimer
        assert 'commercial' in text.lower()
        assert 'structural' in text.lower()

    def test_week_ahead_shared_prompt_contains_cot_guidance(self):
        from morning_engine.prompts import week_ahead_shared_prompt
        text = week_ahead_shared_prompt()
        assert 'COT' in text
        assert 'stretched' in text.lower()
        # Should mention crowded trade / mean-reversion risk
        assert 'crowded' in text.lower() or 'mean-reversion' in text.lower()

    def test_week_ahead_shared_prompt_contains_commercials_disclaimer(self):
        from morning_engine.prompts import week_ahead_shared_prompt
        text = week_ahead_shared_prompt()
        assert 'commercial' in text.lower()
        assert 'structural' in text.lower()


# ---------------------------------------------------------------------------
# 7. Dedup interaction note — documented, not asserted (was §6 before Wire 1/2/3 tests)
# ---------------------------------------------------------------------------

class TestDedupInteractionNote:
    """Tests that document the dedup interaction with Since Yesterday.

    "Since Yesterday" contains yesterday's prices + regime. "The Story" may
    also reference yesterday's prices as context. These are DIFFERENT numbers
    (yesterday's price vs today's), so the INTRA_DUP_FACT_THRESHOLD=3 risk is
    low in practice. However, the regime phrase (e.g. "UNFAVORABLE") can appear
    in multiple sections. These tests document that:
      a) the dedup check is non-fatal (retry is one attempt)
      b) narrative prose from the self-grade opening is unlikely to match verbatim

    The dedup module is tested in detail in test_dedup_and_general.py.
    """

    def test_same_number_in_two_sections_does_not_exceed_threshold(self):
        """A number appearing in 2 of 3 sections is below INTRA_DUP_FACT_THRESHOLD=3."""
        from morning_engine.dedup import check_intra_report, INTRA_DUP_FACT_THRESHOLD
        assert INTRA_DUP_FACT_THRESHOLD == 3
        # One number (65432) appears in The Story and Since Yesterday but NOT Market Snapshot
        report = (
            "## The Story\nBTC closed at $65,432 yesterday showing weakness.\n\n"
            "## Since Yesterday\nYesterday's main call: BTC at $65,432 — played out.\n\n"
            "## Market Snapshot\n| BTC | $67,100 | Neutral |\n"
        )
        result = check_intra_report(report)
        # Appears in 2 sections → below threshold of 3 → does not flag
        assert result['dup_fact_count'] < INTRA_DUP_FACT_THRESHOLD

    def test_self_grade_prose_does_not_match_other_sections(self):
        """Self-grade opening sentence is unique narrative — won't verbatim-match Story."""
        from morning_engine.dedup import check_intra_report
        report = (
            "## The Story\nBTC fell 3.2% as macro headwinds persisted across all sessions.\n\n"
            "## Since Yesterday\nYesterday's report leaned Capitulation at ~45% — "
            "BTC closed -3.2%, the call played out well.\n\n"
            "## Market Snapshot\n| BTC | $62,000 | Bearish |\n"
        )
        result = check_intra_report(report)
        # Self-grade sentence is distinct enough not to be a verbatim duplicate
        assert result['dup_pair_count'] == 0
