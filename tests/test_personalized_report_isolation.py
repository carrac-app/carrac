"""Cross-user data must not leak into personalized reports.

The ``context`` blob carries the OPERATOR's alerts
(``get_user_alerts('admin')``) and a cross-user position read
(``drawing_list(user_id=-1)``) assembled for the admin morning
report. ``_run_personalized_reports`` must not inject that blob into any
paid user's prompt, or one user's alert notes / position labels could surface
in another user's AI report context.

These tests drive ``_run_personalized_reports`` with a fake LLM client that
captures the exact prompt each user gets:

  * a non-admin user's prompt must NOT contain the operator-context canaries,
    and MUST contain the user's own alert note / position label;
  * the admin/operator keeps the global context (legitimate:
    the operator owns that cross-user view).
"""

from __future__ import annotations

import json
import threading

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


ADMIN_ALERT_CANARY = "ADMIN-ALERT-CANARY-e5f1"
CROSS_USER_POSITION_CANARY = "OTHER-USERS-POSITION-CANARY-b7c2"
OWN_ALERT_NOTE = "BRAVO-OWN-ALERT-NOTE-93ad"
OWN_POSITION_LABEL = "BRAVO-OWN-POSITION-LABEL-41fe"


class FakeSonnet:
    """Capture chat() prompts per call; return a minimal valid result."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def chat(self, system_prompt=None, user_message=None, **kwargs):
        with self._lock:
            self.calls.append({"system": system_prompt, "user": user_message})
        return {
            "content": "## The Story\nStub personalized report.",
            "stop_reason": None,
            "usage": {"cost": 0.01, "input_tokens": 100, "output_tokens": 50},
        }


@pytest.fixture
def operator_context():
    """Context dict with operator/cross-user canaries."""
    return {
        "alerts": f"ACTIVE WATCHLIST (1):\n  - BTC above $70,000 — note: {ADMIN_ALERT_CANARY}",
        "positions": json.dumps(
            {"positions": [{"asset": "ETH", "label": CROSS_USER_POSITION_CANARY}]}
        ),
        "signal_diff": "None",
        "yesterday_report": "None",
    }


@pytest.fixture
def seeded_user(app):
    """A member user with one alert and one position drawing of their own."""
    from user_db import user_db
    import push_notifications
    from carrac.content_db import get_content_db

    uid = user_db.create_user(
        "bravo-isolation@test.dev", "StrongPass!123", tos_accepted=True
    )
    assert uid
    user_db.set_user_tier(uid, "member")

    push_notifications.create_alert(
        user_id=str(uid), symbol="BTC", condition="above",
        price=70000.0, note=OWN_ALERT_NOTE,
    )

    db = get_content_db()
    db.save_drawing(
        asset="BTCUSDT", timeframe="1D", drawing_type="position",
        price_level=65000.0, label=OWN_POSITION_LABEL,
        metadata={
            "points": [
                {"value": 65000.0}, {"value": 60000.0}, {"value": 75000.0},
            ]
        },
        user_id=uid,
    )
    return uid


def _run_personalized(monkeypatch, eligible_users, context, fake_sonnet):
    """Invoke _run_personalized_reports with external seams stubbed."""
    import morning_engine.engine as engine

    saved = []

    monkeypatch.setattr(
        engine, "_get_user_analyst_sources",
        lambda uid: {
            "report_assets": [{"asset": "BTC", "timeframes": ["1D"]}],
            "report_indicators": ["RSI"],
            "trader_profile": "",
            "morning_report_prefs": {"voiceBriefing": False},
        },
    )
    monkeypatch.setattr(
        engine, "_save_personal_section",
        lambda *a, **k: saved.append((a, k)),
    )
    # Continuity/credit HTTP calls: point at a dead port so requests fail
    # fast and the (already non-fatal) paths take their except branches.
    monkeypatch.setattr(engine, "DASHBOARD_BASE_URL", "http://127.0.0.1:1")

    engine._run_personalized_reports(
        report_id=123,
        shared_briefing_parts=["## Market\nShared market analysis."],
        chart_raw={},
        signal_data={"per_asset": {}},
        context=context,
        signal_conflicts=None,
        steering={},
        patterns={},
        sonnet=fake_sonnet,
        grok=None,
        _record_phase=lambda *a, **k: None,
        eligible_users=eligible_users,
        quant_context=None,
    )
    return saved


class TestPersonalizedReportIsolation:

    def test_non_admin_prompt_excludes_operator_context(
        self, monkeypatch, seeded_user, operator_context
    ):
        """A member's prompt must not carry admin alerts / others' positions."""
        fake = FakeSonnet()
        saved = _run_personalized(
            monkeypatch,
            eligible_users=[{"id": seeded_user, "tier": "tester"}],
            context=operator_context,
            fake_sonnet=fake,
        )
        assert saved, "personal section was never saved"
        assert fake.calls, "LLM was never called for the configured user"
        prompt = fake.calls[0]["user"]

        assert ADMIN_ALERT_CANARY not in prompt, (
            "operator alert text leaked into a non-admin user's prompt"
        )
        assert CROSS_USER_POSITION_CANARY not in prompt, (
            "cross-user position text leaked into a non-admin user's prompt"
        )

    def test_non_admin_prompt_contains_own_data(
        self, monkeypatch, seeded_user, operator_context
    ):
        """The member's own alert note and position label are injected."""
        fake = FakeSonnet()
        _run_personalized(
            monkeypatch,
            eligible_users=[{"id": seeded_user, "tier": "tester"}],
            context=operator_context,
            fake_sonnet=fake,
        )
        assert fake.calls
        prompt = fake.calls[0]["user"]

        assert OWN_ALERT_NOTE in prompt, "user's own alert note missing from prompt"
        assert OWN_POSITION_LABEL in prompt, (
            "user's own position label missing from prompt"
        )

    def test_admin_keeps_operator_context(
        self, monkeypatch, app, operator_context
    ):
        """The operator's own report keeps the global context."""
        from user_db import user_db

        admin_uid = user_db.create_user(
            "admin-isolation@test.dev", "StrongPass!123", tos_accepted=True
        )
        assert admin_uid
        user_db.set_user_tier(admin_uid, "admin")

        fake = FakeSonnet()
        _run_personalized(
            monkeypatch,
            eligible_users=[{"id": admin_uid, "tier": "admin"}],
            context=operator_context,
            fake_sonnet=fake,
        )
        assert fake.calls
        prompt = fake.calls[0]["user"]
        assert ADMIN_ALERT_CANARY in prompt, (
            "operator context should remain in the admin's own prompt"
        )
