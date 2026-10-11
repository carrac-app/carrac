"""The afternoon delta's "since this morning" free-text prose
must be checked against the real numbers, not left as ungrounded narrative.

Known flatly-wrong "since morning" claim shapes:
  08-03 regime label mismatch (not covered here — see module docstring)
  08-05 breadth "24% oversold — unchanged from the morning read" vs the
        morning report's actual 7%
  08-07 header spread +0.45 vs body prose "ticked to +0.44 (from +0.45)"
        in the SAME document

"If this breaks, how would I find out?": a finding must reach a human via
The error tracker (Sentry), never sit silent in a logfile only. Modeled directly on
carrac.compliance.deribit_gate.alert_if_barred_prose's own test suite
(tests/test_deribit_prose_scrub.py::TestGenerationRegressionAlarm).
"""
from __future__ import annotations

from carrac.reports.afternoon_grounding import (
    check_afternoon_grounding,
    check_breadth_continuity,
    check_spread_self_consistency,
)


# ---------------------------------------------------------------------------
# Real report text (general + afternoon pair) — the 08-07 case.
# ---------------------------------------------------------------------------

_MACRO_HEADER = (
    "**Macro facts** · as of 2026-08-07\n"
    "Fed funds 3.63% (target 3.50–3.75%) · next FOMC in 40d · "
    "10Y 4.63% / 2Y 4.18% (spread +0.45)\n\n"
)

_AFTERNOON_SPREAD_CONTRADICTS_HEADER = _MACRO_HEADER + (
    "**Treasuries / Rates:** TLT's 1D bullish flip alongside US30Y and US5Y "
    "bearish flips is consistent with a rates-down move. The 10Y-2Y spread "
    "ticked to +0.44% (from +0.45%) — still positive but marginally "
    "compressing. Watch whether TLT sustains the flip through the weekly close."
)

_AFTERNOON_SPREAD_CLEAN = _MACRO_HEADER + (
    "**Treasuries / Rates:** The 10Y-2Y spread sits at +0.45%, unchanged on "
    "the session."
)

# ---------------------------------------------------------------------------
# Real report text (general + afternoon pair) — the 08-05 case.
# ---------------------------------------------------------------------------

_MORNING_BREADTH = (
    "**1. Equity breadth is mixed despite index strength — a classic "
    "late-stage divergence.** Market breadth reads 7% of the tracked "
    "universe in oversold territory with only 34% of names above their "
    "50-day MA, even as SPX sits at 95% of its range."
)

_AFTERNOON_BREADTH_WRONG = (
    "**Breadth note:** The breadth label remains \"mixed\" (24% oversold, "
    "34% above 50-day), unchanged from the morning read — breadth is "
    "neither corroborating the equity bullish signal flips nor confirming "
    "the alt oversold cluster as a washout."
)

_AFTERNOON_BREADTH_CORRECT = (
    "**Breadth note:** The breadth label remains \"mixed\" (7% oversold, "
    "34% above 50-day), unchanged from the morning read."
)

_AFTERNOON_BREADTH_NO_CLAIM = (
    "**Breadth note:** The breadth label has shifted to 24% oversold since "
    "the morning read, a meaningful washout."
)


class TestSpreadSelfConsistency:
    """08-07 shape: header vs body, same document, no morning fetch needed."""

    def test_flags_the_real_08_07_contradiction(self):
        findings = check_spread_self_consistency(_AFTERNOON_SPREAD_CONTRADICTS_HEADER)
        assert findings, "did not catch header +0.45 vs body +0.44"
        assert findings[0]['type'] == 'spread_self_contradiction'
        assert findings[0]['header_value'] == 0.45
        assert findings[0]['body_value'] == 0.44

    def test_clean_when_body_matches_header(self):
        assert check_spread_self_consistency(_AFTERNOON_SPREAD_CLEAN) == []

    def test_clean_when_no_body_restatement(self):
        assert check_spread_self_consistency(_MACRO_HEADER) == []

    def test_clean_when_no_header(self):
        assert check_spread_self_consistency("The 10Y-2Y spread ticked to +0.44%.") == []

    def test_empty_input(self):
        assert check_spread_self_consistency("") == []
        assert check_spread_self_consistency(None) == []


class TestBreadthContinuity:
    """08-05 shape: afternoon claims 'unchanged from morning' but cites a
    different number than the morning report actually stated."""

    def test_flags_the_real_08_05_mismatch(self):
        findings = check_breadth_continuity(_AFTERNOON_BREADTH_WRONG, _MORNING_BREADTH)
        assert findings, "did not catch afternoon's 24% vs morning's actual 7%"
        assert findings[0]['type'] == 'breadth_continuity_mismatch'
        assert findings[0]['afternoon_value'] == 24.0
        assert findings[0]['morning_value'] == 7.0

    def test_clean_when_numbers_actually_match(self):
        assert check_breadth_continuity(_AFTERNOON_BREADTH_CORRECT, _MORNING_BREADTH) == []

    def test_clean_when_afternoon_makes_no_continuity_claim(self):
        """24% != 7% too, but the afternoon text never claims 'unchanged' —
        nothing to contradict."""
        assert check_breadth_continuity(_AFTERNOON_BREADTH_NO_CLAIM, _MORNING_BREADTH) == []

    def test_clean_when_morning_content_missing(self):
        assert check_breadth_continuity(_AFTERNOON_BREADTH_WRONG, "") == []
        assert check_breadth_continuity(_AFTERNOON_BREADTH_WRONG, None) == []

    def test_clean_when_afternoon_content_missing(self):
        assert check_breadth_continuity("", _MORNING_BREADTH) == []


class TestCheckAfternoonGroundingAlarm:
    """"If this breaks, how would I find out?" — a finding must reach a
    human via the error tracker, not sit silent in a logfile. Mirrors
    tests/test_deribit_prose_scrub.py::TestGenerationRegressionAlarm."""

    def test_a_mismatch_reaches_sentry(self, monkeypatch):
        from carrac.reports import afternoon_grounding

        captured = []
        monkeypatch.setattr(afternoon_grounding, '_capture', lambda msg: captured.append(msg))
        findings = check_afternoon_grounding(_AFTERNOON_SPREAD_CONTRADICTS_HEADER, None)

        assert findings, "the check did not spot the spread contradiction"
        assert captured, "nothing reached Sentry — this is the silent-failure shape"
        assert 'spread_self_contradiction' in captured[0]

    def test_both_checks_combine(self, monkeypatch):
        from carrac.reports import afternoon_grounding

        captured = []
        monkeypatch.setattr(afternoon_grounding, '_capture', lambda msg: captured.append(msg))
        content = _AFTERNOON_SPREAD_CONTRADICTS_HEADER + "\n\n" + _AFTERNOON_BREADTH_WRONG
        findings = check_afternoon_grounding(content, _MORNING_BREADTH)

        types = {f['type'] for f in findings}
        assert types == {'spread_self_contradiction', 'breadth_continuity_mismatch'}
        assert len(captured) == 1, "one alarm per save, not one per finding"

    def test_a_clean_report_is_silent(self, monkeypatch):
        """A guard nobody will read becomes noise. Clean saves
        must say nothing."""
        from carrac.reports import afternoon_grounding

        captured = []
        monkeypatch.setattr(afternoon_grounding, '_capture', lambda msg: captured.append(msg))
        findings = check_afternoon_grounding(_AFTERNOON_SPREAD_CLEAN, _MORNING_BREADTH)
        assert findings == []
        assert captured == []

    def test_the_alarm_never_raises(self, monkeypatch):
        """It sits in the report save path. It must not be able to fail a save."""
        from carrac.reports import afternoon_grounding

        def _boom(msg):
            raise RuntimeError("sentry down")

        monkeypatch.setattr(afternoon_grounding, '_capture', _boom)
        # Must not raise.
        check_afternoon_grounding(_AFTERNOON_SPREAD_CONTRADICTS_HEADER, None)

    def test_never_raises_on_malformed_input(self):
        assert check_afternoon_grounding(None, None) == []
        assert check_afternoon_grounding("", "") == []


class TestSaveAfternoonRouteIsWired:
    """"What calls this?" — a grounding module nobody calls is the most
    common defect class in this codebase. The live caller is the
    /api/report/afternoon save route, the ONE write path every afternoon
    report (cron-generated or manual) goes through."""

    def test_save_afternoon_report_is_wired_to_it(self):
        import inspect
        from carrac.reports import routes

        src = inspect.getsource(routes.save_afternoon_report)
        assert 'check_afternoon_grounding' in src, (
            "save_afternoon_report does not call check_afternoon_grounding — "
            "built but never connected"
        )

    def test_posting_a_contradictory_afternoon_report_alarms(self, client, app, monkeypatch):
        """End-to-end: POST the real 08-07 self-contradiction to the actual
        save route and confirm the alarm fires — not just that the pure
        function can detect it in isolation."""
        from carrac.reports import afternoon_grounding

        captured = []
        monkeypatch.setattr(afternoon_grounding, '_capture', lambda msg: captured.append(msg))

        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'admin@example.com'
            sess['is_admin'] = True
            sess['tier'] = 'admin'

        resp = client.post('/api/report/afternoon', json={
            'content': _AFTERNOON_SPREAD_CONTRADICTS_HEADER,
            'assets': [],
            'generation_time_ms': 5,
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert captured, "saving a self-contradictory afternoon report did not alarm"
        assert 'spread_self_contradiction' in captured[0]

    def test_posting_a_clean_afternoon_report_stays_silent(self, client, app, monkeypatch):
        from carrac.reports import afternoon_grounding

        captured = []
        monkeypatch.setattr(afternoon_grounding, '_capture', lambda msg: captured.append(msg))

        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'admin@example.com'
            sess['is_admin'] = True
            sess['tier'] = 'admin'

        resp = client.post('/api/report/afternoon', json={
            'content': _AFTERNOON_SPREAD_CLEAN,
            'assets': [],
            'generation_time_ms': 5,
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert captured == []
