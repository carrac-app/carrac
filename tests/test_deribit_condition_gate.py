"""Deribit values reached notification bodies and play evidence.

Beyond the report pipeline, MCP tools, formula vars, quant/bridge, the
snapshot JSON island, anomalies and transmission chains, two more surfaces
could publish barred values: NOTIFICATIONS and PLAY-CONDITION EVIDENCE.

  * push_notifications.build_context_chip must not append "skew25d=-2.0" to the body
    of every outgoing push/ntfy/Discord/Telegram alert. It had no gate and is
    reachable with live data: a BTC skew_25d of -1.95 trips the chip, which
    fires at abs() >= 1.5.
  * CONDITION_SCHEMAS['derivatives_metric']['metrics'] accepted ten barred
    metrics, and _eval_derivatives_metric rendered the raw value back as the
    evidence string shown to the user — the formula-engine leak the earlier gate
    closes, reappearing through a different door.

A deliberate choice: gate to admin, matching deribit_gate's stated
posture — collect freely, expose to admin only.

The identity question is the subtle part. deribit_visible() asks "is the CALLER
the operator" from an MCP contextvar or a Flask request. BOTH evaluation paths
are background jobs with neither, so a caller-based gate would resolve False for
the operator too and silently break their own plays. The stored OWNER is the
right question there; the live caller is the right question at creation time.
"""
import pytest

from carrac.compliance.deribit_gate import (
    DERIBIT_COLUMNS, condition_metric_visible, user_is_admin)


class TestUserIsAdminFailsClosed:
    """An unresolvable owner is not the operator."""

    @pytest.mark.parametrize("uid", [None, "", "not-an-int"])
    def test_unresolvable_ids_are_not_admin(self, uid):
        assert user_is_admin(uid) is False

    def test_the_user_store_symbol_it_imports_actually_exists(self):
        """Importing a name that does not exist must fail the test, not the gate.

        The unit tests inject a FAKE `user_db` module; this test imports the
        real thing and asserts nothing else.
        """
        import user_db as real
        assert hasattr(real, "user_db"), "the module singleton is gone"
        assert callable(getattr(real.user_db, "get_user_by_id", None)), \
            "user_is_admin calls get_user_by_id on the singleton"

    def test_an_unreadable_user_store_is_not_admin(self, monkeypatch):
        """A broken DB must not open the gate."""
        import carrac.compliance.deribit_gate as gate
        import user_db as real

        def _boom(_uid):
            raise RuntimeError("db down")

        monkeypatch.setattr(real.user_db, "get_user_by_id", _boom)
        assert gate.user_is_admin(1) is False

    def test_a_real_admin_resolves_true(self, monkeypatch):
        """Guard against 'fixing' the leak by breaking the feature. With the
        import broken this returned False for everyone, which looks like a
        working gate right up until the operator needs their own data."""
        _as_user(monkeypatch, True)
        import carrac.compliance.deribit_gate as gate
        assert gate.user_is_admin(1) is True

    def test_a_non_admin_resolves_false(self, monkeypatch):
        _as_user(monkeypatch, False)
        import carrac.compliance.deribit_gate as gate
        assert gate.user_is_admin(2) is False


def _as_user(monkeypatch, is_admin):
    """Patch the REAL user_db singleton, never a fabricated module.

    The first cut of these tests injected a fake `user_db` module exposing
    `UserDB`. That name does not exist — the class is `UserDatabase` and the
    module exposes a singleton `user_db` — so the tests passed against a fake
    that matched the production code's BUG rather than the production API, and
    `user_is_admin` raised ImportError on every real call. Patching an
    attribute of the real module cannot drift that way.
    """
    import user_db as real
    monkeypatch.setattr(
        real.user_db, "get_user_by_id",
        lambda uid: {"id": uid, "is_admin": is_admin}, raising=True)


class TestConditionMetricVisibility:
    def test_permitted_metrics_are_never_gated(self, monkeypatch):
        """A licensing boundary, not a paywall."""
        _as_user(monkeypatch, False)
        assert condition_metric_visible("funding_rate_avg", user_id=7) is True

    @pytest.mark.parametrize("metric", sorted(DERIBIT_COLUMNS))
    def test_every_barred_column_is_gated_for_a_normal_user(self, monkeypatch, metric):
        """Derived from DERIBIT_COLUMNS, not a second hand-written list — the
        drift that let options_iv_atm reach /brief."""
        _as_user(monkeypatch, False)
        assert condition_metric_visible(metric, user_id=7) is False

    def test_the_operator_keeps_them(self, monkeypatch):
        _as_user(monkeypatch, True)
        for metric in ("dvol", "gex_net", "skew_25d"):
            assert condition_metric_visible(metric, user_id=1) is True


class TestValidationBlocksNewBarredConditions:
    def test_a_barred_metric_is_refused_at_creation(self, monkeypatch):
        import carrac.compliance.deribit_gate as gate
        monkeypatch.setattr(gate, "deribit_visible", lambda: False)
        from carrac.conditions import validate_condition
        ok, err = validate_condition({
            "type": "derivatives_metric",
            "params": {"metric": "dvol", "operator": "gt", "value": 40},
        })
        assert ok is False
        assert "not available" in err

    def test_a_permitted_metric_still_validates(self, monkeypatch):
        import carrac.compliance.deribit_gate as gate
        monkeypatch.setattr(gate, "deribit_visible", lambda: False)
        from carrac.conditions import validate_condition
        ok, err = validate_condition({
            "id": "r1",
            "type": "derivatives_metric",
            "params": {"metric": "funding_rate_avg", "operator": "gt", "value": 0},
        })
        assert ok is True, err

    def test_the_operator_can_still_create_one(self, monkeypatch):
        import carrac.compliance.deribit_gate as gate
        monkeypatch.setattr(gate, "deribit_visible", lambda: True)
        from carrac.conditions import validate_condition
        ok, err = validate_condition({
            "id": "r1",
            "type": "derivatives_metric",
            "params": {"metric": "dvol", "operator": "gt", "value": 40},
        })
        assert ok is True, err


class TestEvaluationBlocksStoredBarredConditions:
    """Validation covers new ones; this covers rows already in the DB, and rows
    written directly."""

    def test_a_stored_barred_condition_does_not_evaluate(self, monkeypatch):
        _as_user(monkeypatch, False)
        from carrac.conditions import _eval_derivatives_metric
        met, evidence = _eval_derivatives_metric(
            {"metric": "dvol", "operator": "gt", "value": 40},
            scenario={"owner_user_id": 7})
        assert met is False
        assert "not available" in evidence

    def test_the_evidence_string_leaks_no_number(self, monkeypatch):
        """The leak was the VALUE, rendered back to the user as the reason the
        condition did or did not fire. Refusing to evaluate while still
        printing the reading would fix nothing."""
        import re
        _as_user(monkeypatch, False)
        from carrac.conditions import _eval_derivatives_metric
        _, evidence = _eval_derivatives_metric(
            {"metric": "gex_net", "operator": "gt", "value": 1},
            scenario={"owner_user_id": 7})
        assert not re.search(r"\d", evidence), f"evidence carries a number: {evidence!r}"

    def test_a_missing_owner_fails_closed(self, monkeypatch):
        """No scenario, no owner — must not fall open."""
        _as_user(monkeypatch, False)
        from carrac.conditions import _eval_derivatives_metric
        met, evidence = _eval_derivatives_metric(
            {"metric": "dvol", "operator": "gt", "value": 40})
        assert met is False
        assert "not available" in evidence


class TestBothEvaluatorsAgree:
    """The recurring defect class here: wired into the Plays evaluator and not
    the Alerts one. Both read the SAME allowlist, so gating one and not the
    other leaves the door open."""

    def test_the_alert_evaluator_gates_on_the_owner(self):
        src = open("push_notifications.py", encoding="utf-8").read()
        assert "condition_metric_visible(metric_name," in src
        assert "user_id=alert.get('user_id')" in src

    def test_the_plays_monitor_threads_the_owner(self):
        """Without this the evaluator's gate has no owner to ask about and
        fails closed on the operator's own plays."""
        src = open("carrac/plays/monitor.py", encoding="utf-8").read()
        assert "'owner_user_id': play.get('user_id')" in src

    def test_the_plays_evaluator_reads_it(self):
        import inspect
        from carrac import conditions
        src = inspect.getsource(conditions._eval_derivatives_metric)
        assert "scenario or {}).get('owner_user_id')" in src
        assert "condition_metric_visible(metric, user_id=owner)" in src


class TestTheNotificationChip:
    _CTX = {"regime": "favorable",
            "top_zscores": {"FEAR_GREED": 2.1},
            "derivatives": {"skew_25d": -1.95}}

    def test_skew_is_withheld_from_a_normal_user(self, monkeypatch):
        """The live leak: a BTC skew_25d of -1.95 trips the chip (it
        fires at abs() >= 1.5), so this value would go out in message bodies."""
        _as_user(monkeypatch, False)
        from push_notifications import build_context_chip
        chip = build_context_chip(self._CTX, user_id=7)
        assert "skew" not in chip.lower()
        assert "1.9" not in chip
        # the non-licensed parts still ship — this is not a paywall
        assert "regime:favorable" in chip

    def test_the_operator_still_sees_it(self, monkeypatch):
        _as_user(monkeypatch, True)
        from push_notifications import build_context_chip
        chip = build_context_chip(self._CTX, user_id=1)
        assert "skew25d=-1.9" in chip

    def test_no_recipient_means_no_skew(self, monkeypatch):
        """Fails closed — an unnamed recipient is not the operator."""
        _as_user(monkeypatch, False)
        from push_notifications import build_context_chip
        assert "skew" not in build_context_chip(self._CTX).lower()

    def test_both_dispatch_paths_pass_a_recipient(self):
        """The defect underneath the defect: the chip was built ONCE per
        dispatch and appended to every user's message, so there was no
        recipient to gate on. A per-user gate on a shared string is not a gate.
        """
        src = open("push_notifications.py", encoding="utf-8").read()
        assert "chip = build_context_chip(_chip_ctx, user_id=uid)" in src
        assert "chip = build_context_chip(user_id=user_id)" in src
        assert "chip = build_context_chip()\n" not in src, \
            "a call site still builds one chip for everyone"

    def test_the_db_read_stays_hoisted(self):
        """Rendering moved into the loop; the query must not follow it, or one
        query becomes N."""
        src = open("push_notifications.py", encoding="utf-8").read()
        assert "_chip_ctx = _snapshot_quant_context()" in src
