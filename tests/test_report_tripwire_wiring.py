"""Wiring tests.

The recurring bug class in this codebase is "wired into X but not Y, and
tests still pass" (grep every call site). run_afternoon,
run_weekly and run_week_ahead are heavy, network/DB-coupled functions that
aren't practical to exercise end-to-end in a unit test, so — following the
existing precedent in this test suite (e.g. test_dedup_and_general.py's
`inspect.getsource` assertions on chat()) — these tests assert directly on
the function source that the required tripwire calls are present, scoped to
the right report content variable and label.
"""
import inspect

from morning_engine import engine


# --- _advice_tripwire wired into run_afternoon --------------

def test_run_afternoon_wires_advice_tripwire():
    src = inspect.getsource(engine.run_afternoon)
    assert "_advice_tripwire(content, \"afternoon report\")" in src
    # The news and data-release tripwires stay wired.
    assert "_news_tripwire(content, \"afternoon report\")" in src
    assert "_data_release_tripwire(content, \"afternoon report\")" in src
    assert "_leadership_tripwire(content, \"afternoon report\")" in src


def test_run_afternoon_advice_tripwire_grouped_with_the_others():
    """Same non-blocking idiom as the pre-existing news/data-release/
    leadership tripwires in run_afternoon (all run AFTER _post_report —
    they're advisory-only, never gate publish there): the new advice
    tripwire call sits immediately alongside them, not bolted on elsewhere."""
    src = inspect.getsource(engine.run_afternoon)
    advice_pos = src.index('_advice_tripwire(content, "afternoon report")')
    news_pos = src.index('_news_tripwire(content, "afternoon report")')
    # Adjacent, in the same block — within 200 chars of each other.
    assert abs(advice_pos - news_pos) < 200


# --- weekly and week_ahead had NO tripwires at all -------------

def test_run_weekly_wires_advisory_tripwires():
    src = inspect.getsource(engine.run_weekly)
    for fn in ("_calendar_tripwire", "_advice_tripwire", "_news_tripwire",
               "_data_release_tripwire", "_leadership_tripwire"):
        assert f'{fn}(content, "weekly report")' in src, f"{fn} not wired into run_weekly"


def test_run_weekly_tripwires_run_before_publish():
    src = inspect.getsource(engine.run_weekly)
    tripwire_pos = src.index('_advice_tripwire(content, "weekly report")')
    post_pos = src.index('_post_report(content, report_type="weekly"')
    assert tripwire_pos < post_pos


def test_run_week_ahead_wires_shared_report_tripwires():
    src = inspect.getsource(engine.run_week_ahead)
    for fn in ("_calendar_tripwire", "_advice_tripwire", "_news_tripwire",
               "_data_release_tripwire", "_leadership_tripwire"):
        assert f"{fn}(shared_report, f\"week_ahead shared report [{{region}}]\")" in src, (
            f"{fn} not wired into run_week_ahead's shared-report path"
        )


def test_run_week_ahead_wires_personal_section_tripwires():
    src = inspect.getsource(engine.run_week_ahead)
    for fn in ("_calendar_tripwire", "_news_tripwire",
               "_data_release_tripwire", "_leadership_tripwire"):
        assert f'{fn}(personal_content, f"week_ahead personal report (user {{uid}})")' in src, (
            f"{fn} not wired into run_week_ahead's per-user personal path"
        )
    # Unlike the shared week_ahead report (log+notify only —
    # blocking it would drop the report for every user on the shared-report
    # asymmetry), the PERSONAL section is a gate: advice-phrase sentences are
    # scrubbed from personal_content itself before save.
    assert (
        'personal_content = _advice_gate(personal_content, '
        'f"week_ahead personal report (user {uid})")'
    ) in src, "_advice_gate not wired (as a gate, reassigning personal_content) into run_week_ahead"


def test_run_week_ahead_shared_tripwires_run_before_save():
    src = inspect.getsource(engine.run_week_ahead)
    tripwire_pos = src.index('_advice_tripwire(shared_report, f"week_ahead shared report [{region}]")')
    save_pos = src.index('_post_report(content=shared_report, report_type="week_ahead"')
    assert tripwire_pos < save_pos


# --- Functional smoke test: the tripwire functions really do log on the
# real violating sentences and stay silent on clean text, exercised through
# the same call shape run_afternoon/run_weekly/run_week_ahead now use. ---

def test_advice_tripwire_fires_on_real_violation_and_not_on_clean_text(caplog, notifications):
    import logging
    with caplog.at_level(logging.ERROR):
        engine._advice_tripwire(
            "Reduce or hedge ETH longs if this level fails on a closing basis.",
            "test report",
        )
    assert "advice-phrase tripwire in test report" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.ERROR):
        engine._advice_tripwire(
            "BTC printed a golden cross with RSI at 60 and a range position "
            "of 92%, corroborating the broader risk-on regime.",
            "test report",
        )
    assert "advice-phrase tripwire" not in caplog.text


# --- run_dispatch's shared_report ran ZERO tripwires -----------
# The paid, region-specific dispatch report was the one row in the wiring
# matrix with no checks at all — the exact same shape on the exact
# report type that replaced the flagship.

def test_run_dispatch_wires_advisory_tripwires_on_shared_report():
    src = inspect.getsource(engine.run_dispatch)
    for fn in ("_calendar_tripwire", "_advice_tripwire", "_news_tripwire",
               "_data_release_tripwire", "_leadership_tripwire",
               "_scenario_advisory_tripwire", "_internal_arithmetic_tripwire"):
        assert f'{fn}(shared_report, _tw_label)' in src, f"{fn} not wired on dispatch shared_report"


def test_run_dispatch_wires_grounded_tripwires_with_in_scope_data():
    """range_pctile_map and options_claims are already computed on the
    dispatch path — the grounded tripwires must consume them, not a
    stand-in."""
    src = inspect.getsource(engine.run_dispatch)
    assert "_range_pctile_tripwire(shared_report, range_pctile_map, _tw_label)" in src
    assert "_grounded_field_tripwire(shared_report, options_claims, _tw_label)" in src


def test_run_dispatch_tripwires_run_before_report_is_saved():
    """Advisory-but-early: the scans run after _enforce_char_limit produced
    the final text and BEFORE _post_report persists it, so an error-tracker event
    always precedes a bad report reaching users, never trails it."""
    src = inspect.getsource(engine.run_dispatch)
    tw = src.index("_calendar_tripwire(shared_report, _tw_label)")
    saved = src.index("_post_report(")
    limit = src.index("_enforce_char_limit(")
    assert limit < tw < saved


# --- five tripwires that only logged to /var/log/morning-engine.log
# (invisible to the error tracker/Dozzle/the phone) now also reach the operator through the
# SAME _notify() path (ntfy web inbox — see morning_engine/notify.py) every
# other report alarm uses (e.g. _internal_arithmetic_tripwire,
# _publish_gate_ok, _verify_tripwire). Verified here with the conftest
# `notifications` fixture (the autouse `_no_real_notifications` recorder),
# which is how the notification is proven to actually FIRE rather than just
# assumed from reading the source. --------------------------------------

def test_news_tripwire_notifies_operator_on_violation(notifications):
    engine._news_tripwire(
        "The U.S. Treasury issued a 60-day sanctions waiver.", "test report",
    )
    assert notifications, "unhedged news claim must reach the operator, not only the logfile"
    assert any("morning_engine.engine._notify" == s["sender"] for s in notifications)


def test_news_tripwire_does_not_notify_on_clean_text(notifications):
    engine._news_tripwire(
        "The Treasury reportedly issued a sanctions waiver.", "test report",
    )
    assert notifications == []


def test_data_release_tripwire_notifies_operator_on_violation(notifications):
    engine._data_release_tripwire(
        "US Manufacturing PMI printing 55.7, the highest since 2022.", "test report",
    )
    assert notifications, "unattributed data-release figure must reach the operator"


def test_calendar_tripwire_notifies_operator_on_violation(monkeypatch, notifications):
    from datetime import datetime, timedelta
    nfp_date = (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d')

    def fake_api(path, **kwargs):
        return []  # nothing grounded — any calendar row cites fabrication

    monkeypatch.setattr("morning_engine.data_assembly._api", fake_api)
    engine._calendar_tripwire(
        f"| {nfp_date} | Non-Farm Payrolls (Jun) | high | — |\n", "test report",
    )
    assert notifications, "calendar fabrication must reach the operator"


def test_rotation_quadrant_tripwire_notifies_operator_on_violation(monkeypatch, notifications):
    def fake_api(path, **kwargs):
        return {"assets": [
            {"symbol": "BTC", "quadrant": "leading"},
            {"symbol": "ETH", "quadrant": "leading"},
        ]}

    monkeypatch.setattr("morning_engine.data_assembly._api", fake_api)
    bad = (
        "| Sector | Status | Key Signal | Thesis Alignment |\n"
        "|---|---|---|---|\n"
        "| BTC/ETH | Lagging | weak breadth | contradicts |\n"
    )
    engine._rotation_quadrant_tripwire(bad, "test report")
    assert notifications, "rotation quadrant mismatch must reach the operator"


def test_advice_tripwire_notifies_operator_on_violation(notifications):
    engine._advice_tripwire(
        "Reduce or hedge ETH longs if this level fails on a closing basis.",
        "test report",
    )
    assert notifications, "advice-phrase tripwire must reach the operator"


# --- part (b): the advice-phrase tripwire becomes a GATE for
# PERSONAL reports — a personal report reaches exactly one reader, so unlike
# the shared/general reports (blocking would drop the report for everyone,
# per the shared-report asymmetry the other tripwires stay non-blocking
# for), the offending sentence is removed from the saved content. Mirrors
# carrac.compliance.deribit_gate's alert_if_barred_prose (alarm) +
# scrub_deribit_prose (sentence-level scrub) shape. ------------------------

def test_advice_gate_scrubs_offending_sentence_and_notifies(notifications):
    content = (
        "BTC printed a golden cross with RSI at 60.\n"
        "Reduce or hedge ETH longs if this level fails on a closing basis.\n"
        "The weekly structure remains intact above the CPR pivot."
    )
    out = engine._advice_gate(content, "test personal report")
    assert "Reduce or hedge ETH longs" not in out
    assert "golden cross" in out and "CPR pivot" in out, (
        "only the offending line should be removed, not the whole report")
    assert notifications, "a personal report advice-phrase gate hit must still page the operator"


def test_advice_gate_is_a_noop_and_silent_on_clean_text(notifications):
    content = ("BTC printed a golden cross with RSI at 60 and a range "
               "position of 92%, corroborating the broader risk-on regime.")
    out = engine._advice_gate(content, "test personal report")
    assert out == content
    assert notifications == []


def test_advice_gate_never_returns_empty_when_every_line_is_barred(notifications):
    """Fail-open: a personal report reduced to nothing is worse than one
    flagged sentence surviving — same fail-open posture as _publish_gate_ok
    and scrub_deribit_prose's orphan-drop guard."""
    content = "Reduce or hedge ETH longs now."
    out = engine._advice_gate(content, "test personal report")
    assert out, "gate must never empty the report outright"


# --- Wiring: the personalized-report path (run_morning/run_dispatch's
# personalized-report step, via _run_personalized_reports) calls _advice_gate, not the
# non-blocking _advice_tripwire, and actually reassigns report_content to
# the scrubbed result before it is saved. ----------------------------------

def test_run_personalized_reports_wires_advice_gate():
    src = inspect.getsource(engine._run_personalized_reports)
    assert (
        'report_content = _advice_gate(report_content, '
        'f"personalized report (user {uid})")'
    ) in src, "_advice_gate not wired (as a gate, reassigning report_content) into _run_personalized_reports"
    # The calendar, news, data-release and leadership tripwires stay wired.
    for fn in ("_calendar_tripwire", "_news_tripwire",
               "_data_release_tripwire", "_leadership_tripwire"):
        assert f'{fn}(report_content, f"personalized report (user {{uid}})")' in src


# --- The remaining two tripwires, which must also notify rather than stay
# log-only. Same _notify() path, same proof via the recorder. ----

def test_confluence_tripwire_notifies_operator_on_violation(notifications):
    focus = [{"asset": "BTC", "price": 62890,
              "support": {"price": 62020}, "resistance": {"price": 66008}}]
    engine._confluence_tripwire(
        "## Levels in Focus\nBTC support now sits at 58,500.\n\n## Next\nmore", focus, "test report",
    )
    assert notifications, "an invented level in Levels-in-Focus must reach the operator, not only the logfile"
    assert any("morning_engine.engine._notify" == s["sender"] for s in notifications)


def test_confluence_tripwire_does_not_notify_on_grounded_levels(notifications):
    focus = [{"asset": "BTC", "price": 62890,
              "support": {"price": 62020}, "resistance": {"price": 66008}}]
    engine._confluence_tripwire(
        "## Levels in Focus\nBTC sits above support at 62,020 with resistance at 66,008.\n", focus, "test report",
    )
    assert notifications == []


def test_leadership_tripwire_notifies_operator_on_violation(notifications):
    engine._leadership_tripwire("Incoming Fed Chair Kevin Warsh will take over in May.", "test report")
    assert notifications, "an unhedged leadership claim must reach the operator, not only the logfile"
    assert any("morning_engine.engine._notify" == s["sender"] for s in notifications)


def test_leadership_tripwire_does_not_notify_when_hedged(notifications):
    engine._leadership_tripwire("Kevin Warsh is reportedly the incoming Fed Chair.", "test report")
    assert notifications == []


# --- The nightly per-user personal section (run_morning's worker) saves
# through _write_personal_section. Exercised end to end with a stub model:
# the row handed to the save call must not carry the advice sentence. -------

class _StubModel:
    def __init__(self, content):
        self._content = content

    def chat(self, **_kw):
        return {"content": self._content,
                "usage": {"cost": 0.0, "input_tokens": 1, "output_tokens": 1}}


def test_nightly_personal_section_is_gated_before_save(monkeypatch, notifications):
    saved = []
    monkeypatch.setattr(engine, "_save_personal_section",
                        lambda report_id, uid, content, **kw: saved.append(content))
    monkeypatch.setattr(engine, "_debit_credits", lambda **kw: None)
    content = (
        "## Executive Summary\n"
        "BTC printed a golden cross with RSI at 60.\n"
        "Reduce or hedge ETH longs if this level fails on a closing basis.\n"
        "The weekly structure remains intact above the CPR pivot."
    )
    out, _audio = engine._write_personal_section(
        uid=7, user_tier="admin", sys_prompt="s", max_tok=100,
        sonnet=_StubModel(content), grok=None, report_id=123,
        shared_report="", mr_prefs={"voiceBriefing": False},
    )
    assert len(saved) == 1, "the personal section must be saved exactly once"
    assert "Reduce or hedge ETH longs" not in saved[0]
    assert "golden cross" in saved[0] and "CPR pivot" in saved[0]
    assert out == saved[0]
    assert notifications, "a gate hit must still page the operator"


def test_audio_scripts_are_scanned_for_advice_phrasing():
    """Audio scripts are a second model call over report text, so they can
    reintroduce phrasing the report itself was cleared of. Personal scripts
    are gated; shared ones alert, like the shared reports they come from."""
    personal = inspect.getsource(engine._write_personal_section)
    assert 'raw = _advice_gate(pa_result["content"]' in personal
    single = inspect.getsource(engine._run_personalized_reports)
    assert 'raw = _advice_gate(pa_result["content"]' in single
    assert '_advice_tripwire(scan_target, "shared audio briefing")' in inspect.getsource(engine._morning_audio_briefing)
    assert '_morning_audio_briefing(' in inspect.getsource(engine.run_morning)
    assert '_advice_tripwire(scan_target, "dispatch audio briefing")' in inspect.getsource(engine.run_dispatch)
    assert '_advice_tripwire(afternoon_script, "afternoon audio briefing")' in inspect.getsource(engine.run_afternoon)
