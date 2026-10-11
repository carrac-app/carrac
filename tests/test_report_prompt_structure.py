from morning_engine.prompts import free_tier_report_prompt


def test_one_thing_to_learn_removed():
    p = free_tier_report_prompt("", "")
    assert "One Thing to Learn" not in p


def test_has_spine_voice_news_axes_teaser():
    p = free_tier_report_prompt("", "").lower()
    assert "spine" in p or "backbone" in p
    assert "price-action" in p or "price action" in p
    assert "reportedly" in p                       # news-hedge directive
    assert "risk posture" in p and "quant regime" in p   # axes
    # teaser is now code-appended, not prompt-instructed — see test_general_report_footer_constant_appends_teaser


def test_still_grounded_calendar_and_levels():
    p = free_tier_report_prompt("", "")
    assert "Levels in Focus" in p and "calendar" in p.lower()


def test_teaser_directive_removed_from_prompt():
    # The footer is now code-appended, not prompt-instructed
    p = free_tier_report_prompt("", "")
    assert "CLOSE with ONE quiet footer" not in p


def test_data_figure_attribution_directive_present():
    p = free_tier_report_prompt("", "").lower()
    assert "this morning's data" in p or "actual-vs-expected" in p


def test_general_report_footer_constant_appends_teaser():
    from morning_engine.engine import _GENERAL_REPORT_FOOTER
    assert "what you're trading" in _GENERAL_REPORT_FOOTER


def test_briefing_labels_signal_mix_as_risk_posture():
    # Assert on briefing.py source: the signal-mix header must be "Risk Posture",
    # not "Market Regime" (which collides with the Pulse cockpit tile).
    import pathlib
    src = pathlib.Path(__file__).parent.parent / "morning_engine" / "briefing.py"
    text = src.read_text()
    assert "## Risk Posture:" in text, "briefing.py must emit '## Risk Posture:' for the signal-mix header"
    assert "## Market Regime:" not in text, "briefing.py must not use '## Market Regime:' (collides with Pulse tile)"


def test_free_prompt_length_target_is_current():
    # The stale 5,000–7,500 "hard target" contradicted the lifted 13K char cap.
    import morning_engine.prompts as prompts
    blob = open(prompts.__file__).read()
    assert "9,000–13,000 CHARACTERS" in blob
    assert "over 7,500 in the stored report is a failure" not in blob
    assert "Levels in Focus ~900 chars" in blob


def test_data_figure_attribution_is_hedged():
    # The offered attribution phrasing must carry a hedge token the data-release
    # tripwire recognizes, else a compliant report false-trips.
    import morning_engine.prompts as prompts
    blob = open(prompts.__file__).read()
    assert "this morning's data reportedly showed" in blob
    assert "this morning's data showed" not in blob   # un-hedged example removed


def test_engine_personalized_header_uses_risk_posture():
    import morning_engine.engine as eng
    blob = open(eng.__file__).read()
    assert "## Market Regime:" not in blob, "engine.py must use '## Risk Posture:' (collides with Pulse tile)"


def test_afternoon_prompt_has_grounding_rules():
    from morning_engine.prompts import afternoon_delta_prompt
    p = afternoon_delta_prompt().lower()
    assert "50-day moving average vs the 200-day" in p   # 50/200 death-cross rule
    assert "reportedly" in p                              # news-hedge directive
    # The prompt must NOT coach the writer on how to phrase the futures basis
    # (e.g. "still contango"): basis_pct is Deribit-barred and must never
    # appear at all, and coaching its wording invites producing it. The rule
    # is the stronger one — the term is forbidden outright.
    assert "contango and backwardation are not in the briefing" in p
    assert "still contango" not in p


def test_shared_prompt_has_grounding_rules():
    # The flagship paid report had NO news-hedge directive, and its
    # AD-HOC EVENT PRIORITY rule told the model to lead with unscheduled events
    # unhedged.
    from morning_engine.prompts import shared_report_system_prompt
    p = shared_report_system_prompt("", "").lower()
    assert "breaking news is colour, not fact" in p
    assert "reportedly" in p
    assert "never build the report's main thread on an unverified event" in p


def test_personalized_prompt_has_grounding_rules():
    from morning_engine.prompts import personalized_report_prompt
    p = personalized_report_prompt("", "").lower()
    assert "breaking news is colour, not fact" in p
    assert "reportedly" in p
    # The illustrative example must not model an unhedged event assertion.
    assert "the ceasefire is bad news for your oil long" not in p


def test_weekly_prompt_has_grounding_rules():
    from morning_engine.prompts import weekly_digest_prompt
    p = weekly_digest_prompt().lower()
    assert "reportedly" in p                              # news-hedge
    assert "signing/ratification dates we cannot verify" in p   # unverified-event lock
    assert "50-day moving average vs the 200-day" in p   # 50/200 death-cross rule
    # See the note in test_afternoon_prompt_has_grounding_rules.
    assert "contango and backwardation are not in the briefing" in p
    assert "still contango" not in p
    assert "one value throughout the report" in p        # internal-consistency rule


def test_report_routes_use_50_200_cross_not_20_200():
    # The afternoon delta's signal-diff cross label must be the 50/200 standard,
    # matching the morning report — not the legacy SMA20 < SMA200.
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "carrac" / "reports" / "routes.py").read_text()
    assert "SMA20 > SMA200" not in src and "SMA20 < SMA200" not in src
    assert "Death Cross (SMA50 < SMA200)" in src
    assert "Golden Cross (SMA50 > SMA200)" in src
