"""
Tests that zscore recurring alerts must edge-detect (fire only on
false→true transition), not re-fire every nightly run while threshold holds.
"""
import pytest

from carrac.alerts import store


@pytest.fixture(autouse=True)
def _alert_db(tmp_path, rebind_db):
    rebind_db(tmp_path / 'db')


def _make_zscore_alert(alert_id=1, metric='BTC_MVRV', condition='above',
                       threshold=2.0, last_breach=None):
    """Helper to build a minimal zscore alert dict."""
    a = {
        'id': alert_id,
        'user_id': '1',
        'alert_type': 'zscore',
        'metric': metric,
        'condition': condition,
        'threshold': threshold,
        'active': True,
        'frequency': 'recurring',
    }
    if last_breach is not None:
        a['last_breach'] = last_breach
    return a


class TestZscoreEdgeDetection:
    """Zscore alerts must fire on transition, not on every check."""

    def _run_check(self, alert, zscore_val, monkeypatch):
        """Run check_quant_alerts with a single stored zscore alert and the
        given value, then refresh ``alert`` in place from the store."""
        from push_notifications import check_quant_alerts

        if not store.list_alerts():
            store.insert(alert)
        # Patch dispatch so no actual push is sent
        monkeypatch.setattr('push_notifications._dispatch_bundle_or_direct', lambda _t: None)

        triggered = check_quant_alerts(
            current_zscores={'BTC_MVRV': zscore_val},
            current_regime='favorable',
        )
        [stored] = store.list_alerts()
        alert.clear()
        alert.update(stored)
        return triggered

    def test_first_eval_establishes_baseline_no_fire(self, monkeypatch):
        """First evaluation (no last_breach baseline) must never fire."""
        alert = _make_zscore_alert()  # no last_breach key
        triggered = self._run_check(alert, zscore_val=2.5, monkeypatch=monkeypatch)
        assert triggered == [], "Should not fire on first eval (baseline establishment)"
        assert 'last_breach' in alert, "Baseline must be recorded after first eval"
        assert alert['last_breach'] is True

    def test_second_eval_above_threshold_no_fire(self, monkeypatch):
        """Second eval still above threshold → NOT a new transition → no fire."""
        alert = _make_zscore_alert(last_breach=True)
        triggered = self._run_check(alert, zscore_val=2.4, monkeypatch=monkeypatch)
        assert triggered == [], "Should not fire — still above, not a new transition"

    def test_transition_to_below_no_fire(self, monkeypatch):
        """Value drops below threshold (prev was above) → state resets, no fire."""
        alert = _make_zscore_alert(last_breach=True)
        triggered = self._run_check(alert, zscore_val=1.5, monkeypatch=monkeypatch)
        assert triggered == [], "Should not fire on downward transition"
        assert alert['last_breach'] is False

    def test_transition_back_above_fires(self, monkeypatch):
        """Value crosses above threshold again (prev was False) → fires once."""
        alert = _make_zscore_alert(last_breach=False)
        triggered = self._run_check(alert, zscore_val=2.7, monkeypatch=monkeypatch)
        assert len(triggered) == 1, "Should fire on false→true transition"
        assert alert['last_breach'] is True

    def test_full_sequence(self, monkeypatch):
        """Full scenario from the spec: eval1→fire, eval2→no, eval3→no, eval4→fire."""
        # eval1: first time, value=2.5 above threshold → baseline set, NO fire
        alert = _make_zscore_alert()
        triggered = self._run_check(alert, zscore_val=2.5, monkeypatch=monkeypatch)
        assert triggered == []        # baseline, no fire
        assert alert['last_breach'] is True

        # eval2: still above (2.4) → NO fire
        triggered = self._run_check(alert, zscore_val=2.4, monkeypatch=monkeypatch)
        assert triggered == []
        assert alert['last_breach'] is True

        # eval3: dropped below (1.5) → transition off, NO fire
        triggered = self._run_check(alert, zscore_val=1.5, monkeypatch=monkeypatch)
        assert triggered == []
        assert alert['last_breach'] is False

        # eval4: above again (2.7) → transition on, FIRE
        triggered = self._run_check(alert, zscore_val=2.7, monkeypatch=monkeypatch)
        assert len(triggered) == 1
        assert alert['last_breach'] is True
