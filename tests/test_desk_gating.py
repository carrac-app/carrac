"""Derivatives Desk flag/tier gating — carrac/reports/routes.py.

Tests the three small, independently-testable gating pieces (see their
docstrings in routes.py) WITHOUT touching a real quant DB or Flask request:
``_desk_enabled`` (flag/admin — is the band gathered at all?),
``_desk_unlocked`` (tier/admin — is it shown with real values?), and
``_gather_desk`` (the glue `_editorial_context()` actually calls, stubbing
out `carrac.facts.bands.build_desk` so these tests never need a live
Postgres connection).

Covers the plan's four required scenarios:
    flag off + non-admin -> desk is None
    flag off + admin     -> desk is present
    flag on  + free tier  -> desk present, unlocked=False
    flag on  + paid tier  -> desk present, unlocked=True
"""
from __future__ import annotations

from unittest.mock import patch


from carrac.reports.routes import _desk_enabled, _desk_unlocked, _gather_desk


# ──────────────────────────────────────────────────────────────────────────
# _desk_enabled — env flag OR admin
# ──────────────────────────────────────────────────────────────────────────

class TestDeskEnabled:

    def test_flag_off_non_admin_disabled(self, monkeypatch):
        monkeypatch.delenv("DERIVATIVES_DESK_ENABLED", raising=False)
        assert _desk_enabled(is_admin=False) is False

    def test_flag_off_admin_enabled(self, monkeypatch):
        """Admin always sees it once the code ships — a live preview ahead
        of the flag flip for everyone."""
        monkeypatch.delenv("DERIVATIVES_DESK_ENABLED", raising=False)
        assert _desk_enabled(is_admin=True) is True

    def test_flag_on_non_admin_enabled(self, monkeypatch):
        monkeypatch.setenv("DERIVATIVES_DESK_ENABLED", "1")
        assert _desk_enabled(is_admin=False) is True

    def test_flag_on_admin_enabled(self, monkeypatch):
        monkeypatch.setenv("DERIVATIVES_DESK_ENABLED", "1")
        assert _desk_enabled(is_admin=True) is True

    def test_flag_value_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("DERIVATIVES_DESK_ENABLED", "True")
        assert _desk_enabled(is_admin=False) is True

    def test_flag_garbage_value_treated_as_off(self, monkeypatch):
        monkeypatch.setenv("DERIVATIVES_DESK_ENABLED", "nah")
        assert _desk_enabled(is_admin=False) is False


# ──────────────────────────────────────────────────────────────────────────
# _desk_unlocked — SAME discriminator as the personal-report splice
# (carrac/fastapi_app/routers/reports.py::get_latest_morning_report's
# `serves_personal`): personal_report != 'none', or admin outright.
# ──────────────────────────────────────────────────────────────────────────

class TestDeskUnlocked:

    def test_admin_always_unlocked_regardless_of_tier(self):
        """is_admin short-circuits — never even calls get_tier_limits."""
        with patch("carrac.auth.decorators.get_tier_limits") as mock_limits:
            assert _desk_unlocked(is_admin=True, tier="free") is True
        mock_limits.assert_not_called()

    def test_free_tier_locked(self, monkeypatch):
        monkeypatch.setattr(
            "carrac.auth.decorators.get_tier_limits",
            lambda tier: {"personal_report": "none"},
        )
        assert _desk_unlocked(is_admin=False, tier="free") is False

    def test_paid_tier_unlocked(self, monkeypatch):
        monkeypatch.setattr(
            "carrac.auth.decorators.get_tier_limits",
            lambda tier: {"personal_report": "standard"},
        )
        assert _desk_unlocked(is_admin=False, tier="member") is True

    def test_full_personal_report_tier_unlocked(self, monkeypatch):
        monkeypatch.setattr(
            "carrac.auth.decorators.get_tier_limits",
            lambda tier: {"personal_report": "full"},
        )
        assert _desk_unlocked(is_admin=False, tier="premium") is True

    def test_missing_personal_report_key_treated_as_locked(self, monkeypatch):
        """Defense-in-depth: an unknown/malformed limits dict never
        accidentally unlocks."""
        monkeypatch.setattr(
            "carrac.auth.decorators.get_tier_limits",
            lambda tier: {},
        )
        assert _desk_unlocked(is_admin=False, tier="mystery") is False

    def test_tier_passed_through_to_get_tier_limits(self, monkeypatch):
        seen = {}

        def _fake(tier):
            seen["tier"] = tier
            return {"personal_report": "none"}

        monkeypatch.setattr("carrac.auth.decorators.get_tier_limits", _fake)
        _desk_unlocked(is_admin=False, tier="tester")
        assert seen["tier"] == "tester"


# ──────────────────────────────────────────────────────────────────────────
# _gather_desk — the glue _editorial_context() calls. build_desk is stubbed
# so these never touch a real quant DB connection.
# ──────────────────────────────────────────────────────────────────────────

class TestGatherDesk:

    def test_flag_off_non_admin_desk_is_none(self, monkeypatch):
        monkeypatch.delenv("DERIVATIVES_DESK_ENABLED", raising=False)
        with patch("carrac.facts.bands.build_desk") as mock_build:
            result = _gather_desk(is_admin=False, tier="free")
        assert result is None
        mock_build.assert_not_called()

    def test_flag_off_admin_desk_present(self, monkeypatch):
        monkeypatch.delenv("DERIVATIVES_DESK_ENABLED", raising=False)
        sentinel = {"as_of": "2026-07-02", "unlocked": True, "cta_href": "/plans", "assets": []}
        with patch("carrac.facts.bands.build_desk", return_value=sentinel) as mock_build:
            result = _gather_desk(is_admin=True, tier="admin")
        assert result is sentinel
        mock_build.assert_called_once_with(unlocked=True)

    def test_flag_on_free_tier_present_locked(self, monkeypatch):
        monkeypatch.setenv("DERIVATIVES_DESK_ENABLED", "1")
        monkeypatch.setattr(
            "carrac.auth.decorators.get_tier_limits",
            lambda tier: {"personal_report": "none"},
        )
        captured = {}

        def _fake_build(unlocked):
            captured["unlocked"] = unlocked
            return {"as_of": "2026-07-02", "unlocked": unlocked, "cta_href": "/plans", "assets": []}

        with patch("carrac.facts.bands.build_desk", side_effect=_fake_build):
            result = _gather_desk(is_admin=False, tier="free")
        assert result is not None
        assert result["unlocked"] is False
        assert captured["unlocked"] is False

    def test_flag_on_paid_tier_present_unlocked(self, monkeypatch):
        monkeypatch.setenv("DERIVATIVES_DESK_ENABLED", "1")
        monkeypatch.setattr(
            "carrac.auth.decorators.get_tier_limits",
            lambda tier: {"personal_report": "standard"},
        )

        def _fake_build(unlocked):
            return {"as_of": "2026-07-02", "unlocked": unlocked, "cta_href": "/plans", "assets": []}

        with patch("carrac.facts.bands.build_desk", side_effect=_fake_build):
            result = _gather_desk(is_admin=False, tier="member")
        assert result is not None
        assert result["unlocked"] is True

    def test_non_fatal_when_build_desk_raises(self, monkeypatch):
        monkeypatch.setenv("DERIVATIVES_DESK_ENABLED", "1")
        with patch("carrac.facts.bands.build_desk", side_effect=RuntimeError("boom")):
            assert _gather_desk(is_admin=False, tier="free") is None

    def test_non_fatal_when_tier_lookup_raises(self, monkeypatch):
        monkeypatch.setenv("DERIVATIVES_DESK_ENABLED", "1")
        monkeypatch.setattr(
            "carrac.auth.decorators.get_tier_limits",
            lambda tier: (_ for _ in ()).throw(RuntimeError("db down")),
        )
        assert _gather_desk(is_admin=False, tier="free") is None
