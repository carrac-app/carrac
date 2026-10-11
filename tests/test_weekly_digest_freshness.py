"""The weekly digest's narrative backbone came from
GET /api/report/morning/recent?limit=5 — the last 5 "morning"-TYPE reports.
With ENABLE_MORNING_REPORT false, a fixed report_type='morning' fetch there
silently feeds the SAME handful of legacy rows into every weekly digest
forever, so editions re-narrate old news as current.

Two independent fixes, both exercised here without a live network/DB/LLM:
  1. run_weekly's source switches to 'general' — the report type that still
     generates daily regardless of ENABLE_MORNING_REPORT.
  2. A staleness guard that fires regardless of type: if the freshest source
     report is older than the freshness threshold, log.error AND surface the
     staleness in the digest's own prompt context, mirroring the MCP report
     tools' documented FRESHNESS FALLBACK idiom (carrac/mcp/tools/reports.py).
"""
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from morning_engine import engine


# ---------------------------------------------------------------------------
# 1. _fetch_weekly_source_reports — requests type=general, tolerates failure
# ---------------------------------------------------------------------------

class TestFetchWeeklySourceReports:
    def test_requests_general_type_by_default(self, monkeypatch):
        captured = {}

        class _FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"id": 1, "created_at": "2026-08-08T06:00:00", "summary": "x" * 30}]

        def fake_get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _FakeResp()

        monkeypatch.setattr(engine.requests, "get", fake_get)
        reports = engine._fetch_weekly_source_reports()
        assert captured["params"].get("type") == "general"
        assert "/api/report/morning/recent" in captured["url"]
        assert len(reports) == 1

    def test_returns_empty_list_on_request_failure(self, monkeypatch):
        def fake_get(*a, **kw):
            raise ConnectionError("boom")

        monkeypatch.setattr(engine.requests, "get", fake_get)
        assert engine._fetch_weekly_source_reports() == []

    def test_returns_empty_list_on_non_list_json(self, monkeypatch):
        class _FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"error": "unauthorized"}

        monkeypatch.setattr(engine.requests, "get", lambda *a, **kw: _FakeResp())
        assert engine._fetch_weekly_source_reports() == []


# ---------------------------------------------------------------------------
# 2. _weekly_source_staleness — pure staleness check
# ---------------------------------------------------------------------------

class TestWeeklySourceStaleness:
    def test_empty_reports_list_is_stale(self):
        is_stale, age_days, created = engine._weekly_source_staleness([])
        assert is_stale is True
        assert age_days is None

    def test_missing_created_at_is_stale(self):
        is_stale, age_days, created = engine._weekly_source_staleness([{"id": 1}])
        assert is_stale is True

    def test_fresh_report_is_not_stale(self):
        now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
        fresh = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        is_stale, age_days, created = engine._weekly_source_staleness(
            [{"created_at": fresh}], now=now.replace(tzinfo=None))
        assert is_stale is False
        assert age_days == pytest.approx(1.0, abs=0.01)

    def test_old_report_is_stale(self):
        """The real failure mode: once 'morning' stops generating, a later weekly
        digest run would see only old rows."""
        now = datetime(2026, 8, 8, tzinfo=timezone.utc).replace(tzinfo=None)
        stale = "2026-06-20T06:00:00"
        is_stale, age_days, created = engine._weekly_source_staleness(
            [{"created_at": stale}], now=now)
        assert is_stale is True
        assert age_days > 45

    def test_exactly_at_threshold_is_not_stale(self):
        now = datetime(2026, 8, 8, tzinfo=timezone.utc).replace(tzinfo=None)
        exactly_7d = "2026-08-01T00:00:00"
        is_stale, age_days, created = engine._weekly_source_staleness(
            [{"created_at": exactly_7d}], now=now, max_age_days=7)
        assert is_stale is False

    def test_freshest_is_reports_zero_not_min(self):
        """reports is newest-first (get_all_reports ORDER BY created_at DESC)
        — staleness must key off reports[0], not scan for the min age."""
        now = datetime(2026, 8, 8, tzinfo=timezone.utc).replace(tzinfo=None)
        fresh = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        stale = "2026-06-01T00:00:00"
        # Freshest first (as the real API returns), an older one behind it.
        is_stale, age_days, created = engine._weekly_source_staleness(
            [{"created_at": fresh}, {"created_at": stale}], now=now)
        assert is_stale is False
        assert age_days < 2

    def test_unparseable_timestamp_is_stale(self):
        is_stale, age_days, created = engine._weekly_source_staleness(
            [{"created_at": "not-a-date"}])
        assert is_stale is True


# ---------------------------------------------------------------------------
# 3. _build_weekly_narrative_arc — audio_script preferred, summary fallback
# ---------------------------------------------------------------------------

class TestBuildWeeklyNarrativeArc:
    def test_prefers_audio_script_when_present(self):
        reports = [{"created_at": "2026-08-05", "audio_script": "A" * 30, "summary": "B" * 30}]
        arc = engine._build_weekly_narrative_arc(reports)
        assert "AAAA" in arc
        assert "BBBB" not in arc

    def test_falls_back_to_summary_when_no_audio_script(self):
        """General reports (the digest's new source) carry no
        audio_script — only 'morning' reports get one via the TTS phase."""
        reports = [{"created_at": "2026-08-05", "audio_script": "", "summary": "S" * 30}]
        arc = engine._build_weekly_narrative_arc(reports)
        assert "SSSS" in arc

    def test_falls_back_to_summary_when_audio_script_key_absent(self):
        reports = [{"created_at": "2026-08-05", "summary": "S" * 30}]
        arc = engine._build_weekly_narrative_arc(reports)
        assert "SSSS" in arc

    def test_short_scripts_are_skipped(self):
        reports = [{"created_at": "2026-08-05", "summary": "short"}]
        assert engine._build_weekly_narrative_arc(reports) == ""

    def test_chronological_order_oldest_first(self):
        """API returns newest-first; the arc must read oldest-to-newest."""
        reports = [
            {"created_at": "2026-08-06", "summary": "Tuesday narrative here" * 2},
            {"created_at": "2026-08-05", "summary": "Monday narrative here" * 2},
        ]
        arc = engine._build_weekly_narrative_arc(reports)
        assert arc.index("2026-08-05") < arc.index("2026-08-06")

    def test_empty_reports_gives_empty_arc(self):
        assert engine._build_weekly_narrative_arc([]) == ""


# ---------------------------------------------------------------------------
# 4. Wiring: run_weekly actually uses all of the above, in the right order
# ---------------------------------------------------------------------------

class TestRunWeeklyWiring:
    def test_fetches_general_not_morning(self):
        src = inspect.getsource(engine.run_weekly)
        assert '_fetch_weekly_source_reports(report_type="general"' in src
        assert "report_type='morning'" not in src
        assert 'report_type="morning"' not in src

    def test_calls_the_staleness_guard(self):
        src = inspect.getsource(engine.run_weekly)
        assert "_weekly_source_staleness(reports)" in src

    def test_logs_error_when_stale(self):
        src = inspect.getsource(engine.run_weekly)
        assert "log.error" in src
        # The staleness branch and the log.error call must actually be near
        # each other, not just both present somewhere in the function.
        stale_pos = src.index("if is_stale")
        error_pos = src.index("log.error", stale_pos)
        assert error_pos - stale_pos < 400

    def test_staleness_note_reaches_the_prompt(self):
        """The most common recurring defect: built but never connected. A
        staleness_note computed and never passed into weekly_digest_prompt
        would leave the LLM free to present stale material as current."""
        src = inspect.getsource(engine.run_weekly)
        assert "weekly_digest_prompt(staleness_note=staleness_note)" in src

    def test_staleness_note_also_reaches_the_briefing_header(self):
        """Not just the system prompt — the user-message briefing itself
        must carry the warning in its own header context, per the fix spec.
        staleness_note feeds a staleness_header variable that is spliced
        into the top of the briefing f-string."""
        src = inspect.getsource(engine.run_weekly)
        header_def_pos = src.index("staleness_header = (")
        assert "staleness_note" in src[header_def_pos:header_def_pos + 200]
        briefing_pos = src.index("briefing = f")
        assert "staleness_header" in src[briefing_pos:briefing_pos + 200]


# ---------------------------------------------------------------------------
# 5. weekly_digest_prompt — staleness_note injection
# ---------------------------------------------------------------------------

class TestWeeklyDigestPromptStaleness:
    def test_default_call_unaffected(self):
        """Existing callers (and test_report_prompt_structure.py) call this
        with no arguments — must keep working."""
        from morning_engine.prompts import weekly_digest_prompt
        p = weekly_digest_prompt()
        assert "STALENESS" not in p.upper()

    def test_staleness_note_appears_in_prompt(self):
        from morning_engine.prompts import weekly_digest_prompt
        p = weekly_digest_prompt(staleness_note="THE SOURCE IS 40 DAYS OLD")
        assert "THE SOURCE IS 40 DAYS OLD" in p

    def test_none_staleness_note_is_a_no_op(self):
        from morning_engine.prompts import weekly_digest_prompt
        assert weekly_digest_prompt(staleness_note=None) == weekly_digest_prompt()


class TestStalenessParsesTheTimestampTheDbActuallyReturns:
    """get_all_reports() hands back RFC-2822 strings
    ("Fri, 04 Sep 2026 06:06:19 GMT"); fromisoformat cannot parse them, so the
    check would report every source as stale ("unknown days old") and inject a
    false STALE warning into the digest prompt every single week."""

    def test_rfc2822_created_at_is_measured_not_assumed_stale(self):
        now = datetime(2026, 9, 5, 15, 15, 0)
        stale, age, raw = engine._weekly_source_staleness(
            [{'created_at': 'Fri, 04 Sep 2026 06:06:19 GMT'}], now=now)
        assert stale is False
        assert age == pytest.approx(1.38, abs=0.01)
        assert raw == 'Fri, 04 Sep 2026 06:06:19 GMT'

    def test_rfc2822_older_than_threshold_is_stale(self):
        now = datetime(2026, 9, 12, 15, 15, 0)
        stale, age, _ = engine._weekly_source_staleness(
            [{'created_at': 'Fri, 04 Sep 2026 06:06:19 GMT'}], now=now)
        assert stale is True
        assert age > 7
