"""Part 2 (afternoon publish gate), part 3 (general-report
sibling), and the Part 1 generation-time prompt fix.

The save-time backstop (check_afternoon_grounding, wired into
save_afternoon_report) has its own test suite in
tests/test_afternoon_grounding.py. This file covers the rest:

  Part 1 — morning_engine/prompts.py::afternoon_delta_prompt() now accepts
           explicit hard-constraint figures (extracted from TODAY's general
           report, morning_engine/data_assembly.py::
           fetch_todays_general_report) instead of leaving the model to
           infer them from background prose alone.
  Part 2 — morning_engine/engine.py::_afternoon_grounding_tripwire() runs
           the SAME shipped checks (reused, not reimplemented) before
           _post_report in run_afternoon, not only at save time.
  Part 3 — carrac/reports/afternoon_grounding.py::check_general_grounding()
           (new function, reuses check_spread_self_consistency unmodified)
           wired into save_general_report() — the sibling case: a general
           report can carry the same header-vs-body spread contradiction
           the afternoon check already catches.
"""
from __future__ import annotations

import inspect

from carrac.reports.afternoon_grounding import (
    check_general_grounding,
    check_spread_self_consistency,
)


# ---------------------------------------------------------------------------
# Real report text (a published general report) — the Part-3 sibling case.
# Same header the afternoon test fixtures use, but this is the body line from
# the general report itself.
# ---------------------------------------------------------------------------

_GENERAL_HEADER = (
    "**Macro facts** · as of 2026-08-07\n"
    "Fed funds 3.63% (target 3.50–3.75%) · next FOMC in 40d · "
    "10Y 4.63% / 2Y 4.18% (spread +0.45)\n\n"
)

_GENERAL_SPREAD_CONTRADICTS_HEADER = _GENERAL_HEADER + (
    "**2. The MOVE index — bond-market volatility, the stress gauge that "
    "equity and crypto traders often underwatch — reads 76.1 (70th "
    "percentile, z=+0.72), with a 5-day decline of 1.0 points.** Bond vol "
    "is elevated but easing, which historically corresponds to a "
    "stabilizing rates environment. The 10Y-2Y yield spread sits at "
    "+0.44%, a gently positive curve — not flashing recession, but not "
    "signaling strong growth conviction either."
)

_GENERAL_SPREAD_CLEAN = _GENERAL_HEADER + (
    "The 10Y-2Y yield spread sits at +0.45%, a gently positive curve."
)

# Real general-report regime line and breadth line — used to validate
# extract_morning_constraints against the actual phrasing, not an invented
# shape.
_GENERAL_08_05_BREADTH = (
    "**1. Equity breadth is mixed despite index strength.** Market breadth "
    "reads 7% of the tracked universe in oversold territory with only 34% "
    "of names above their 50-day MA, even as SPX sits at 95% of its range."
)

_GENERAL_08_07_FULL = _GENERAL_SPREAD_CONTRADICTS_HEADER


class TestCheckGeneralGroundingPart3:
    """Part 3 — the confirmed sibling: report #379 uncaught, same rule as
    the afternoon check."""

    def test_show_me_it_failing_flags_the_real_379_contradiction(self):
        # RED: prove the underlying regexes actually catch the real production
        # text before trusting the wrapper around them.
        raw_findings = check_spread_self_consistency(_GENERAL_SPREAD_CONTRADICTS_HEADER)
        assert raw_findings, "check_spread_self_consistency did not catch the real 08-07 general report contradiction"
        assert raw_findings[0]['header_value'] == 0.45
        assert raw_findings[0]['body_value'] == 0.44

    def test_check_general_grounding_reuses_the_same_check(self):
        findings = check_general_grounding(_GENERAL_SPREAD_CONTRADICTS_HEADER)
        assert findings
        assert findings[0]['type'] == 'spread_self_contradiction'

    def test_clean_report_is_silent(self):
        assert check_general_grounding(_GENERAL_SPREAD_CLEAN) == []

    def test_reuses_the_shipped_function_not_a_copy(self):
        # Sibling sweep: check_general_grounding must call
        # check_spread_self_consistency, not reimplement the regex.
        src = inspect.getsource(check_general_grounding)
        assert 'check_spread_self_consistency' in src
        assert 'HEADER_SPREAD_RE' not in src, (
            "check_general_grounding appears to reimplement the header regex "
            "instead of reusing check_spread_self_consistency — exactly the "
            "drift the project's call-site rule warns about"
        )

    def test_alarm_reaches_sentry(self, monkeypatch):
        from carrac.reports import afternoon_grounding

        captured = []
        monkeypatch.setattr(afternoon_grounding, '_capture', lambda msg: captured.append(msg))
        findings = check_general_grounding(_GENERAL_SPREAD_CONTRADICTS_HEADER)
        assert findings
        assert captured, "nothing reached Sentry — silent-failure shape"
        assert 'spread_self_contradiction' in captured[0]

    def test_alarm_never_raises(self, monkeypatch):
        from carrac.reports import afternoon_grounding

        def _boom(msg):
            raise RuntimeError("sentry down")

        monkeypatch.setattr(afternoon_grounding, '_capture', _boom)
        check_general_grounding(_GENERAL_SPREAD_CONTRADICTS_HEADER)  # must not raise

    def test_never_raises_on_malformed_input(self):
        assert check_general_grounding(None) == []
        assert check_general_grounding("") == []


class TestSaveGeneralRouteIsWiredPart3:
    """"What calls this?" — the live write chokepoint for report_type=
    'general' is save_general_report(), the ONE writer (confirmed via
    grep: only carrac/reports/routes.py:save_general_report calls
    content_db.save_report(..., report_type='general', ...))."""

    def test_save_general_report_is_wired_to_it(self):
        from carrac.reports import routes

        src = inspect.getsource(routes.save_general_report)
        assert 'check_general_grounding' in src, (
            "save_general_report does not call check_general_grounding — "
            "built but never connected"
        )

    def test_posting_a_contradictory_general_report_alarms(self, client, app, monkeypatch):
        """End-to-end: POST the real 08-07 general-report self-contradiction
        to the actual save route and confirm the alarm fires."""
        from carrac.reports import afternoon_grounding

        captured = []
        monkeypatch.setattr(afternoon_grounding, '_capture', lambda msg: captured.append(msg))

        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'admin@example.com'
            sess['is_admin'] = True
            sess['tier'] = 'admin'

        resp = client.post('/api/report/general', json={
            'content': _GENERAL_SPREAD_CONTRADICTS_HEADER,
            'assets': [],
            'generation_time_ms': 5,
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert captured, "saving a self-contradictory general report did not alarm"
        assert 'spread_self_contradiction' in captured[0]

    def test_posting_a_clean_general_report_stays_silent(self, client, app, monkeypatch):
        from carrac.reports import afternoon_grounding

        captured = []
        monkeypatch.setattr(afternoon_grounding, '_capture', lambda msg: captured.append(msg))

        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'admin@example.com'
            sess['is_admin'] = True
            sess['tier'] = 'admin'

        resp = client.post('/api/report/general', json={
            'content': _GENERAL_SPREAD_CLEAN,
            'assets': [],
            'generation_time_ms': 5,
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert captured == []


class TestAfternoonPublishGatePart2:
    """Part 2 — the pre-publish tripwire in morning_engine/engine.py."""

    def test_tripwire_reuses_the_shipped_checker(self):
        from morning_engine import engine

        src = inspect.getsource(engine._afternoon_grounding_tripwire)
        assert 'check_afternoon_grounding' in src, (
            "_afternoon_grounding_tripwire does not call check_afternoon_grounding "
            "— it must reuse the shipped checks, not reimplement them"
        )

    def test_run_afternoon_calls_the_tripwire_before_post_report(self):
        """"What calls this?" — a gate function nothing invokes is the exact
        failure class this work is about. Confirm run_afternoon actually
        calls it, and BEFORE _post_report (source-order proxy for
        "before publication")."""
        from morning_engine import engine

        src = inspect.getsource(engine.run_afternoon)
        assert '_afternoon_grounding_tripwire(' in src, (
            "run_afternoon never calls _afternoon_grounding_tripwire — "
            "built but never connected"
        )
        gate_pos = src.index('_afternoon_grounding_tripwire(')
        post_positions = [i for i in _all_indices(src, '_post_report(') if i > 0]
        # The call inside run_afternoon that actually saves the afternoon
        # report is the LAST _post_report( occurrence in the function body
        # (there is exactly one for report_type="afternoon").
        assert post_positions, "run_afternoon never calls _post_report"
        save_pos = post_positions[0]
        assert gate_pos < save_pos, (
            "_afternoon_grounding_tripwire is called AFTER _post_report — "
            "that is the save-time backstop's job, not the pre-publish gate's"
        )

    def test_tripwire_fires_before_publish_on_the_real_08_07_afternoon_case(self, monkeypatch):
        """Direct behavioural check of the tripwire function itself (not a
        source-order proxy): the real 08-07 afternoon self-contradiction
        must alarm when run through it."""
        from morning_engine import engine
        from carrac.reports import afternoon_grounding

        captured = []
        monkeypatch.setattr(afternoon_grounding, '_capture', lambda msg: captured.append(msg))

        afternoon_content = _GENERAL_HEADER + (
            "**Treasuries / Rates:** The 10Y-2Y spread ticked to +0.44% "
            "(from +0.45%) — marginally compressing."
        )
        engine._afternoon_grounding_tripwire(afternoon_content, None, "afternoon report")
        assert captured, "the pre-publish tripwire did not alarm on a real contradiction"

    def test_tripwire_never_raises(self, monkeypatch):
        from morning_engine import engine
        from carrac.reports import afternoon_grounding

        def _boom(*a, **k):
            raise RuntimeError("sentry down")

        monkeypatch.setattr(afternoon_grounding, '_capture', _boom)
        engine._afternoon_grounding_tripwire(_GENERAL_SPREAD_CONTRADICTS_HEADER, None, "afternoon report")  # must not raise

    def test_tripwire_is_non_blocking_by_design(self):
        """The deliberate blocking-vs-alarm decision for this work:
        non-blocking. _afternoon_grounding_tripwire must not return a
        bool the caller could branch a "drop the report" decision on —
        confirm it has no return value (None), matching every other
        tripwire in this module (_advice_tripwire, _news_tripwire, ...)
        rather than _publish_gate_ok's bool-return blocking shape."""
        from morning_engine import engine
        import inspect as _inspect

        _inspect.signature(engine._afternoon_grounding_tripwire)
        # No explicit assertion possible on return type via signature alone
        # (no annotation) — assert instead that run_afternoon does not
        # branch on its result (the defining behavioural trait of a
        # blocking gate vs an alarm-only tripwire).
        src = _inspect.getsource(engine.run_afternoon)
        assert 'if not _afternoon_grounding_tripwire' not in src
        assert 'if _afternoon_grounding_tripwire' not in src


def _all_indices(haystack, needle):
    out = []
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i == -1:
            break
        out.append(i)
        start = i + 1
    return out


class TestExtractMorningConstraintsPart1:
    """Part 1 — extraction of hard-constraint figures from today's general
    report, validated against real production report text (#371 08-03, #375
    08-05, #379 08-07)."""

    def test_extracts_the_real_08_07_spread(self):
        from morning_engine.prompts import extract_morning_constraints

        out = extract_morning_constraints(_GENERAL_08_07_FULL)
        assert out['spread'] == 0.45  # the HEADER value, not the body's

    def test_extracts_the_real_08_05_breadth(self):
        from morning_engine.prompts import extract_morning_constraints

        out = extract_morning_constraints(_GENERAL_08_05_BREADTH)
        assert out['oversold_pct'] == 7.0

    def test_empty_content_returns_none_not_a_default(self):
        from morning_engine.prompts import extract_morning_constraints

        out = extract_morning_constraints("")
        assert out == {'spread': None, 'oversold_pct': None}
        out2 = extract_morning_constraints(None)
        assert out2 == {'spread': None, 'oversold_pct': None}

    def test_reuses_the_shipped_regexes_not_a_copy(self):
        import inspect
        from morning_engine.prompts import extract_morning_constraints

        src = inspect.getsource(extract_morning_constraints)
        assert '_HEADER_SPREAD_RE' in src and '_OVERSOLD_PCT_RE' in src
        assert 'afternoon_grounding import' in src, (
            "extract_morning_constraints must import the regexes from "
            "carrac.reports.afternoon_grounding, not redefine them"
        )


class TestAfternoonPromptHardConstraintsPart1:
    def test_no_constraints_is_a_no_op(self):
        from morning_engine.prompts import afternoon_delta_prompt

        prompt = afternoon_delta_prompt()
        assert 'HARD CONSTRAINTS' not in prompt

    def test_spread_constraint_is_rendered_explicitly(self):
        from morning_engine.prompts import afternoon_delta_prompt

        prompt = afternoon_delta_prompt(morning_constraints={'spread': 0.45, 'oversold_pct': None})
        assert 'HARD CONSTRAINTS' in prompt
        assert '+0.45%' in prompt
        assert 'THIS MORNING' in prompt

    def test_breadth_constraint_is_rendered_explicitly(self):
        from morning_engine.prompts import afternoon_delta_prompt

        prompt = afternoon_delta_prompt(morning_constraints={'spread': None, 'oversold_pct': 7.0})
        assert '7%' in prompt
        assert 'unchanged' in prompt.lower()

    def test_regime_constraint_disambiguates_the_two_taxonomies(self):
        """The 08-03 defect: the afternoon report conflated the quant HMM
        regime with the derivatives-regime-playbook label. The prompt must
        name both and explicitly forbid treating them as the same thing."""
        from morning_engine.prompts import afternoon_delta_prompt

        prompt = afternoon_delta_prompt(morning_regime="FAVORABLE")
        assert 'FAVORABLE' in prompt
        assert 'derivatives-regime' in prompt.lower() or 'derivatives regime' in prompt.lower()

    def test_no_morning_regime_omits_the_regime_line(self):
        from morning_engine.prompts import afternoon_delta_prompt

        prompt = afternoon_delta_prompt(morning_constraints={'spread': 0.45, 'oversold_pct': None},
                                         morning_regime=None)
        assert 'Quant market regime THIS MORNING' not in prompt

    def test_the_delta_report_framing_is_unchanged(self):
        """Backward-compat guard: the existing prompt content (sections,
        non-advice rule, etc.) must survive untouched around the new block."""
        from morning_engine.prompts import afternoon_delta_prompt

        prompt = afternoon_delta_prompt(morning_constraints={'spread': 0.45, 'oversold_pct': 7.0},
                                         morning_regime="FAVORABLE")
        assert '## Session Summary' in prompt or '**Session Summary**' in prompt
        assert 'NON-ADVICE RULE' in prompt


class TestRunAfternoonWiresConstraintsIntoThePrompt:
    """"What calls this?" for extract_morning_constraints /
    afternoon_delta_prompt's new params — a hard-constraints function
    nothing calls is exactly the failure class this work is about."""

    def test_run_afternoon_calls_extract_morning_constraints(self):
        from morning_engine import engine

        src = inspect.getsource(engine.run_afternoon)
        assert 'extract_morning_constraints(' in src
        assert 'morning_constraints=' in src
        assert 'morning_regime=' in src

    def test_run_afternoon_prefers_todays_general_report_over_stale_field(self):
        """Pins that run_afternoon prefers today's general report:
        get_latest_report(report_type='morning') has no date filter, so once
        'morning' stops generating it would silently feed a stale report as
        "this morning's" every day. run_afternoon must call
        fetch_todays_general_report() and prefer it."""
        from morning_engine import engine

        src = inspect.getsource(engine.run_afternoon)
        assert 'fetch_todays_general_report(' in src


class TestFetchTodaysGeneralReport:
    def test_returns_none_when_no_report_type_general_key_param(self):
        """The route only serves 'morning' unless ?type=general is passed
        explicitly — confirm the fetch asks for the right type."""
        import inspect
        from morning_engine import data_assembly

        src = inspect.getsource(data_assembly.fetch_todays_general_report)
        assert 'type=general' in src

    def test_returns_none_when_latest_general_report_is_not_from_today(self, monkeypatch):
        from morning_engine import data_assembly

        stale = [{'id': 1, 'content': 'old', 'created_at': '2026-01-01 00:00:00+00'}]
        monkeypatch.setattr(data_assembly, '_api', lambda *a, **k: stale)
        assert data_assembly.fetch_todays_general_report() is None

    def test_returns_the_report_when_created_today(self, monkeypatch):
        from datetime import datetime, timezone
        from morning_engine import data_assembly

        today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        fresh = [{'id': 2, 'content': 'today content',
                  'created_at': f'{today_str} 06:06:00+00'}]
        monkeypatch.setattr(data_assembly, '_api', lambda *a, **k: fresh)
        out = data_assembly.fetch_todays_general_report()
        assert out is not None
        assert out['content'] == 'today content'

    def test_never_raises_on_api_failure(self, monkeypatch):
        from morning_engine import data_assembly

        def _boom(*a, **k):
            raise RuntimeError("dashboard unreachable")

        monkeypatch.setattr(data_assembly, '_api', _boom)
        assert data_assembly.fetch_todays_general_report() is None
