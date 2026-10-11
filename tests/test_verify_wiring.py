"""Wiring tests for the Haiku fact verifier
(carrac/grounding/verify.py::verify_against_source), which is worthless
unless a production caller invokes it. Same idiom as
test_report_tripwire_wiring.py: run_afternoon / run_weekly / run_week_ahead /
_generate_general_report / _run_personalized_reports are heavy,
network/DB-coupled functions not practical to exercise end-to-end in a unit
test, so wiring correctness is asserted directly on function source
(inspect.getsource); the _verify_tripwire / _log_verify_cost helpers'
behaviour (non-blocking on HALLUCINATION, kill-switch short-circuit, cost
folded into the in-process spend path) is exercised directly.
"""
import inspect
import logging

from morning_engine import engine


# --- Wiring: each report-generation path calls _verify_tripwire -----------

def test_generate_general_report_wires_verify_tripwire():
    """Covers run_dispatch's active path too — ENABLE_MORNING_REPORT=false
    means run_dispatch's only report output is _generate_general_report."""
    src = inspect.getsource(engine._generate_general_report)
    assert ('_verify_tripwire(_llm_narrative_for_verify, gen_briefing, '
            '"general report", "general")') in src
    # Must run AFTER the blocking publish gate (brief requirement), not before.
    gate_pos = src.index('_publish_gate_ok(content, "general report")')
    verify_pos = src.index('_verify_tripwire(_llm_narrative_for_verify')
    assert gate_pos < verify_pos


def test_run_dispatch_reaches_general_report_generation():
    src = inspect.getsource(engine.run_dispatch)
    assert "_generate_general_report(" in src


def test_run_afternoon_wires_verify_tripwire():
    src = inspect.getsource(engine.run_afternoon)
    assert ('_verify_tripwire(_afternoon_llm_content, briefing, '
            '"afternoon report", "afternoon")') in src
    tripwire_pos = src.index('_leadership_tripwire(content, "afternoon report")')
    verify_pos = src.index('_verify_tripwire(_afternoon_llm_content')
    assert tripwire_pos < verify_pos


def test_run_weekly_wires_verify_tripwire():
    src = inspect.getsource(engine.run_weekly)
    assert '_verify_tripwire(content, briefing, "weekly report", "weekly")' in src
    tripwire_pos = src.index('_leadership_tripwire(content, "weekly report")')
    post_pos = src.index('_post_report(content, report_type="weekly"')
    verify_pos = src.index('_verify_tripwire(content, briefing, "weekly report"')
    assert tripwire_pos < verify_pos < post_pos


def test_run_week_ahead_wires_verify_tripwire_shared():
    src = inspect.getsource(engine.run_week_ahead)
    assert ('_verify_tripwire(shared_report, briefing, '
            'f"week_ahead shared report [{region}]", "week_ahead")') in src
    tripwire_pos = src.index(
        '_leadership_tripwire(shared_report, f"week_ahead shared report [{region}]")')
    save_pos = src.index('_post_report(content=shared_report, report_type="week_ahead"')
    verify_pos = src.index('_verify_tripwire(shared_report, briefing,')
    assert tripwire_pos < verify_pos < save_pos


def test_run_week_ahead_wires_verify_tripwire_personal():
    src = inspect.getsource(engine.run_week_ahead)
    assert '_verify_tripwire(personal_content, user_ctx_text,' in src
    tripwire_pos = src.index(
        '_leadership_tripwire(personal_content, f"week_ahead personal report (user {uid})")')
    verify_pos = src.index('_verify_tripwire(personal_content, user_ctx_text,')
    assert tripwire_pos < verify_pos


def test_personalized_reports_wire_verify_tripwire():
    """PERSONALIZED_REPORTS stays off in production (brief requirement) — this
    only proves the call site exists in the shared helper both run_morning
    and run_dispatch use for the personalized reports."""
    src = inspect.getsource(engine._run_personalized_reports)
    assert ('_verify_tripwire(report_content, user_briefing,') in src
    tripwire_pos = src.index(
        '_leadership_tripwire(report_content, f"personalized report (user {uid})")')
    verify_pos = src.index('_verify_tripwire(report_content, user_briefing,')
    assert tripwire_pos < verify_pos


# --- Behaviour: _verify_tripwire never blocks, kill switch short-circuits --

def test_verify_tripwire_kill_switch_short_circuits(monkeypatch, caplog):
    """GROUNDING_VERIFY_ENABLED=0 must short-circuit INSIDE
    verify_against_source() (no network call, no API key needed — see
    carrac/grounding/verify.py) — _verify_tripwire must not error, notify,
    or log a HALLUCINATION when the kill switch is set."""
    monkeypatch.setenv("GROUNDING_VERIFY_ENABLED", "0")
    notified = []
    monkeypatch.setattr(engine, "_notify", lambda *a, **kw: notified.append((a, kw)))

    with caplog.at_level(logging.DEBUG):
        result = engine._verify_tripwire(
            "Some generated text with a claim.", "Some source text.",
            "test report", "test", facts_block_text="",
        )

    assert result is None
    assert "HALLUCINATION" not in caplog.text
    assert notified == []


def test_verify_tripwire_non_blocking_on_hallucination(monkeypatch, caplog):
    """A HALLUCINATION verdict must be logged + notified but the function
    must never raise and never return a value the caller could gate a
    publish decision on — this is a second opinion, not a gate (see
    the shared-report asymmetry)."""
    class _FakeResult:
        verdict = "HALLUCINATION"
        unsupported = [{"quote": "made up number", "why": "not in source"}]
        cost = 0.01
        raw = '{"verdict":"HALLUCINATION"}'

    monkeypatch.setattr(
        "carrac.grounding.verify.verify_against_source",
        lambda *a, **kw: _FakeResult(),
    )
    notified = []
    monkeypatch.setattr(engine, "_notify", lambda *a, **kw: notified.append((a, kw)))
    monkeypatch.setattr(engine, "_log_verify_cost", lambda *a, **kw: None)

    with caplog.at_level(logging.ERROR):
        result = engine._verify_tripwire(
            "generated text", "source text", "test report", "test",
            facts_block_text="",
        )

    assert result is None  # never blocks — no signal the caller could act on
    assert "HALLUCINATION" in caplog.text
    assert "made up number" in caplog.text
    assert len(notified) == 1


def test_verify_tripwire_silent_on_clean(monkeypatch, caplog):
    class _FakeResult:
        verdict = "CLEAN"
        unsupported = []
        cost = 0.01
        raw = '{"verdict":"CLEAN"}'

    monkeypatch.setattr(
        "carrac.grounding.verify.verify_against_source",
        lambda *a, **kw: _FakeResult(),
    )
    notified = []
    monkeypatch.setattr(engine, "_notify", lambda *a, **kw: notified.append((a, kw)))
    monkeypatch.setattr(engine, "_log_verify_cost", lambda *a, **kw: None)

    with caplog.at_level(logging.ERROR):
        engine._verify_tripwire("generated text", "source text", "test report", "test",
                                facts_block_text="")

    assert "HALLUCINATION" not in caplog.text
    assert notified == []


def test_verify_tripwire_skips_empty_text():
    """No generated_text or no source_text — nothing to verify, no crash."""
    assert engine._verify_tripwire("", "source", "label", "type") is None
    assert engine._verify_tripwire("generated", "", "label", "type") is None


# --- Cost wiring: verify spend folds into the SAME in-process path -------

def test_log_verify_cost_increments_global_spend_in_process(monkeypatch):
    """Mirrors test_engine_cost_logging.py's pattern for log_engine_costs:
    the verifier's own spend must fold into the same in-process counter +
    ai_usage_log, NOT an /api/engine/log-cost POST, which silently loses
    spend under load."""
    import carrac.ai.billing as billing
    import user_db as user_db_mod

    incremented, logged = [], []
    monkeypatch.setattr(billing, "_increment_global_spend",
                        lambda cents: incremented.append(cents))
    monkeypatch.setattr(user_db_mod.user_db, "log_ai_usage",
                        lambda **kw: logged.append(kw))

    engine._log_verify_cost("general", 0.03)

    assert incremented == [3]
    assert logged and logged[0]["feature"] == "engine_general_verify"
    assert logged[0]["cost_cents"] == 3
    assert logged[0]["charged_cents"] == 0


def test_log_verify_cost_skips_zero_cost(monkeypatch):
    import carrac.ai.billing as billing
    import user_db as user_db_mod

    incremented = []
    monkeypatch.setattr(billing, "_increment_global_spend",
                        lambda cents: incremented.append(cents))
    monkeypatch.setattr(user_db_mod.user_db, "log_ai_usage", lambda **kw: None)

    engine._log_verify_cost("general", 0.0)

    assert incremented == []


# --- A truncated verdict must be LOUD, not a silent ERROR -----

class _FakeClient:
    """Minimal stand-in for MorningAnthropicClient.chat()."""

    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason = content, stop_reason
        self.max_tokens_seen = None

    def chat(self, **kw):
        self.max_tokens_seen = kw.get("max_tokens")
        return {"content": self.content, "usage": {"cost": 0.05},
                "stop_reason": self.stop_reason}


def test_truncated_verdict_is_named_and_logged_at_error(caplog):
    """The prod failure this test exists for: 2 of the verifier's first 3 real
    runs came back truncated at exactly 600/600 output tokens, both on the
    public `general` report. The truncated JSON failed json.loads and was
    filed as a generic ERROR at debug level, so the verifier went silent
    EXACTLY when it had findings to report. Truncation must be distinguishable
    from a malformed reply, and must reach the logs at ERROR."""
    from carrac.grounding.verify import verify_against_source

    half_a_verdict = ('{"verdict":"HALLUCINATION","unsupported":'
                      '[{"quote":"GEX flip point at 165,694","why":"source say')
    client = _FakeClient(half_a_verdict, stop_reason="max_tokens")

    with caplog.at_level(logging.ERROR, logger="carrac.grounding.verify"):
        result = verify_against_source("generated", "source", "facts",
                                       anthropic_client=client)

    assert result.verdict == "ERROR"
    assert result.raw.startswith("<truncated>"), (
        "truncation must be distinguishable from a parse failure, or the "
        "caller cannot alarm on the recurring case")
    assert result.cost == 0.05, "spend still has to be recorded"
    assert any("TRUNCATED" in r.message or "TRUNCATED" in r.getMessage()
               for r in caplog.records), "truncation must log at ERROR"


def test_verdict_token_cap_is_large_enough_for_a_real_hallucination_list():
    """A CLEAN verdict is ~28 tokens, so a cap that only fits CLEAN is a cap
    that only works when there is nothing to report.

    A 4000-token cap is NOT enough — the `weekly` report (which embeds up to
    5 recent reports at 8000 chars each) hits that cap and has its verdict
    discarded as <truncated>, shipping unchecked. The floor is 8000."""
    from carrac.grounding.verify import _MAX_VERDICT_TOKENS

    assert _MAX_VERDICT_TOKENS >= 8_000, (
        f"_MAX_VERDICT_TOKENS={_MAX_VERDICT_TOKENS}; must stay >= 8000")


def test_complete_verdicts_still_parse():
    """Guard against the truncation branch swallowing healthy responses."""
    from carrac.grounding.verify import verify_against_source

    ok = ('{"verdict":"HALLUCINATION","unsupported":'
          '[{"quote":"GEX 165,694","why":"source says 69109"}]}')
    r = verify_against_source("g", "s", "f", anthropic_client=_FakeClient(ok))
    assert r.verdict == "HALLUCINATION" and len(r.unsupported) == 1

    r = verify_against_source("g", "s", "f", anthropic_client=_FakeClient(
        '{"verdict":"CLEAN","unsupported":[]}'))
    assert r.verdict == "CLEAN" and r.clean


def test_verify_tripwire_notifies_operator_when_verifier_goes_blind(monkeypatch):
    """An unchecked report is the same blind spot as having no verifier; the
    operator has to hear about it."""
    notified = []
    monkeypatch.setattr(engine, "_notify",
                        lambda title, msg, **kw: notified.append((title, msg)))
    monkeypatch.setattr(engine, "_log_verify_cost", lambda *a, **k: None)

    from carrac.grounding import verify as verify_mod
    monkeypatch.setattr(
        verify_mod, "verify_against_source",
        lambda *a, **k: verify_mod.VerifyResult(
            verdict="ERROR", raw="<truncated>{...", cost=0.05))

    engine._verify_tripwire("generated", "source", "general report", "general",
                            facts_block_text="facts")

    assert notified, "a blind verifier must page the operator"
    assert "BLIND" in notified[0][0]


def test_verify_tripwire_end_to_end_truncation_pages_operator(monkeypatch):
    """Full path, only the raw LLM client mocked (as the task specifies):
    an Anthropic response with stop_reason='max_tokens' — exactly the
    condition verify_against_source() checks at carrac/grounding/verify.py's
    truncation branch — must surface all the way through _verify_tripwire to
    an operator notification, not just at the verify_against_source() unit
    level (test_truncated_verdict_is_named_and_logged_at_error above) or with
    verify_against_source() itself mocked out (the sibling test above this
    one). This is the exact prod failure mode: the `weekly` report's verdict
    got truncated at _MAX_VERDICT_TOKENS and the report shipped unchecked."""
    half_a_verdict = ('{"verdict":"HALLUCINATION","unsupported":'
                      '[{"quote":"GEX flip point at 165,694","why":"source say')
    fake_client = _FakeClient(half_a_verdict, stop_reason="max_tokens")

    import morning_engine.anthropic_client as anthropic_client_mod
    monkeypatch.setattr(anthropic_client_mod, "MorningAnthropicClient",
                        lambda *a, **kw: fake_client)

    notified = []
    monkeypatch.setattr(engine, "_notify",
                        lambda title, msg, **kw: notified.append((title, msg)))
    monkeypatch.setattr(engine, "_log_verify_cost", lambda *a, **k: None)

    engine._verify_tripwire("generated text", "source text",
                            "weekly report", "weekly", facts_block_text="facts")

    assert notified, "a truncated LLM response must page the operator end-to-end"
    assert "BLIND" in notified[0][0]
    assert "BLIND" in notified[0][0]
