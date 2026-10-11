"""A `once` alert that reaches nobody must not be consumed.

`check_alerts` persists the triggered state before dispatching,
which is correct — a crash mid-send must not re-fire the alert forever. But
when every channel is down the alert would still be marked triggered, and a
`frequency: 'once'` alert would never fire again: during a long outage (a
dead push endpoint, a revoked bot token) every `once` alert that fired would
be lost with nothing telling the user, while data/alert_dlq.jsonl piled up
undelivered notifications.

The re-arm is deliberately throttled to one retry per hourly bar. Retrying on
every ~60s precompute tick would turn a multi-day outage into ~1,440 failed
sends a day against a condition that is still true.
"""
import pytest

import push_notifications as pn
from carrac.alerts import store


@pytest.fixture(autouse=True)
def _alert_db(tmp_path, rebind_db):
    rebind_db(tmp_path / 'db')


def _alert(**kw):
    base = {
        'id': 1, 'user_id': '1', 'symbol': 'BTC', 'condition': 'above',
        'price': 100.0, 'frequency': 'once', 'active': True, 'triggered': False,
    }
    base.update(kw)
    return base


def _stored():
    return {a['id']: a for a in store.list_alerts()}


def _run(monkeypatch, outcome, prices=None):
    """Drive check_alerts with dispatch stubbed at the SEAM, not inside the
    unit — _dispatch_bundle_or_direct is the network boundary, and
    check_alerts' own trigger/consume logic is what is under test. Returns
    what fired and the alerts as stored afterwards."""
    monkeypatch.setattr(pn, 'capture_alert_event', lambda *a, **k: None)
    monkeypatch.setattr(pn, '_dispatch_bundle_or_direct', lambda t: outcome)
    fired = pn.check_alerts(prices if prices is not None else {'BTC': 150.0})
    return fired, _stored()


class TestAnUndeliveredAlertIsNotConsumed:
    def test_once_alert_is_rearmed_when_no_channel_took_it(self, monkeypatch):
        store.insert(_alert())
        fired, alerts = _run(monkeypatch, outcome={1: False})

        assert len(fired) == 1, 'the price condition was met, so it must fire'
        assert alerts[1]['triggered'] is False, (
            'delivery reached nobody — consuming the alert here is how a '
            'once-only alert gets lost permanently'
        )
        assert alerts[1].get('undelivered_at')

    def test_once_alert_stays_consumed_when_delivery_succeeded(self, monkeypatch):
        store.insert(_alert())
        _, alerts = _run(monkeypatch, outcome={1: True})

        assert alerts[1]['triggered'] is True
        assert 'undelivered_at' not in alerts[1], (
            'a delivered alert must not carry a re-arm marker'
        )

    def test_a_rearmed_alert_does_not_retry_within_the_same_bar(self, monkeypatch):
        """The throttle. Without it, a channel outage means a retry every ~60s."""
        store.insert(_alert())
        _, alerts = _run(monkeypatch, outcome={1: False})
        assert alerts[1]['triggered'] is False

        fired_again, _ = _run(monkeypatch, outcome={1: False})
        assert fired_again == [], (
            'same hourly bar — the re-armed alert must wait, or a multi-day '
            'outage becomes ~1440 failed sends a day'
        )

    def test_a_rearmed_alert_does_retry_on_the_next_bar(self, monkeypatch):
        store.insert(_alert())
        _run(monkeypatch, outcome={1: False})

        # Simulate the clock moving into the next hourly bar.
        store.modify(1, lambda a: a.update(last_triggered_bar='1999-01-01T00:00:00'))
        fired_again, alerts = _run(monkeypatch, outcome={1: True})

        assert len(fired_again) == 1, 'a new bar must give the alert another go'
        assert alerts[1]['triggered'] is True
        assert 'undelivered_at' not in alerts[1]

    def test_delivery_is_tracked_per_alert_not_globally(self, monkeypatch):
        """Two alerts, one delivered and one not — only the failed one re-arms."""
        store.insert(_alert(id=1, symbol='BTC'))
        store.insert(_alert(id=2, symbol='ETH'))
        _, alerts = _run(monkeypatch, outcome={1: True, 2: False},
                         prices={'BTC': 150.0, 'ETH': 150.0})

        assert alerts[1]['triggered'] is True
        assert alerts[2]['triggered'] is False


class TestRearmHelperInIsolation:
    def test_once_per_bar_alerts_are_left_alone(self):
        """They rearm every bar by design — nothing to repair."""
        a = _alert(frequency='once_per_bar', last_triggered_bar='BAR')
        n = pn._rearm_undelivered([a], {1: False}, 'BAR')
        assert n == 0
        assert a['last_triggered_bar'] == 'BAR'

    def test_an_empty_outcome_map_changes_nothing(self):
        """A dispatcher that reported nothing must not be read as failure."""
        a = _alert(triggered=True)
        assert pn._rearm_undelivered([a], {}, 'BAR') == 0
        assert a['triggered'] is True


class TestNoChannelsIsNotAFailure:
    """The distinction that makes the re-arm safe.

    'Every channel rejected it' and 'the user configured no channels' both
    delivered nothing, but only the first is a fault. Treating them alike would
    make every `once` alert of a notifications-off user retry forever — which
    is how re-arming a silently dropped alert turns into a busy-loop.
    """

    def test_no_configured_channels_reports_none_not_false(self, monkeypatch,
                                                           notifications):
        # This path now alarms the OPERATOR: a user with alerts and
        # no channels leaves no DLQ record, so nothing else can see it. The
        # verdict under test is unchanged and must stay None — the alarm is a
        # side effect, not a delivery. `notifications` declares the send to the
        # notification-send guard instead of silencing it.
        monkeypatch.setattr(pn, '_resolve_user_id', lambda u: 7)
        monkeypatch.setattr(pn, 'send_push_notification', lambda **k: False)

        class _DB:
            def get_preferences(self, uid):
                return {'notifications': {'ntfy': {'enabled': False},
                                          'discord': {'enabled': False},
                                          'telegram': {'enabled': False}}}

        import sys, types
        mod = types.ModuleType('user_db')
        mod.user_db = _DB()
        monkeypatch.setitem(sys.modules, 'user_db', mod)

        verdict = pn._send_user_notifications('7', 'BTC', 'above', 100.0, 150.0)
        assert verdict is None, (
            'no channel was attempted — reporting False here would re-arm the '
            'alert every bar for a user who wants no notifications'
        )
        assert any('notification channel enabled' in str(s.get('args', ('',))[0])
                   for s in notifications), \
            'The operator must be told this user can receive nothing'

    def test_an_attempted_channel_that_fails_reports_false(self, monkeypatch):
        monkeypatch.setattr(pn, '_resolve_user_id', lambda u: 7)
        monkeypatch.setattr(pn, '_send_ntfy_to_url', lambda *a, **k: False)

        class _DB:
            def get_preferences(self, uid):
                return {'notifications': {
                    'ntfy': {'enabled': True, 'url': 'https://ntfy.sh/x'}}}

        import sys, types
        mod = types.ModuleType('user_db')
        mod.user_db = _DB()
        monkeypatch.setitem(sys.modules, 'user_db', mod)

        verdict = pn._send_user_notifications('7', 'BTC', 'above', 100.0, 150.0)
        assert verdict is False

    def test_a_none_verdict_leaves_the_alert_consumed(self):
        a = _alert(triggered=True)
        assert pn._rearm_undelivered([a], {1: None}, 'BAR') == 0
        assert a['triggered'] is True


class TestAPrefsReadFailureIsNotMisreadAsNoChannels:
    """A transient DB failure while loading the
    user's notification prefs must not return None — the same verdict as
    'user has no channels configured'. _rearm_undelivered() only re-arms on
    False, so a momentary blip would consume a `once` alert permanently, with
    one warning log line and nothing in the error tracker."""

    def _raising_db(self, monkeypatch):
        class _DB:
            def get_preferences(self, uid):
                raise RuntimeError('simulated transient DB outage')
        import sys, types
        mod = types.ModuleType('user_db')
        mod.user_db = _DB()
        monkeypatch.setitem(sys.modules, 'user_db', mod)

    def test_send_user_notifications_reports_false_on_prefs_failure(self, monkeypatch):
        monkeypatch.setattr(pn, '_resolve_user_id', lambda u: 7)
        captured = []
        monkeypatch.setattr(pn, '_capture_exception', lambda e: captured.append(e))
        self._raising_db(monkeypatch)

        verdict = pn._send_user_notifications('7', 'BTC', 'above', 100.0, 150.0)
        assert verdict is False, (
            'a prefs read failure must read as attempted-and-failed (False, '
            're-armed next bar), never as no-channels (None, consumed forever)'
        )
        assert captured, 'the blip must reach Bugsink, not just a log line'

    def test_batched_warnings_report_false_on_prefs_failure(self, monkeypatch):
        monkeypatch.setattr(pn, '_resolve_user_id', lambda u: 7)
        monkeypatch.setattr(pn, 'send_push_notification', lambda **k: False)
        captured = []
        monkeypatch.setattr(pn, '_capture_exception', lambda e: captured.append(e))
        self._raising_db(monkeypatch)

        verdict = pn.send_batched_position_warnings(
            '7', [{'symbol': 'BTC', 'direction': 'long', 'price': 100.0,
                   'warnings': ['stop near']}])
        assert verdict is False
        assert captured

    def test_no_channels_still_reports_none(self, monkeypatch):
        """The ALLOWED branch, proven against the same seam: an intact prefs
        read with no channels must stay None — False here would re-arm an
        alert every bar for a user who wants no notifications."""
        monkeypatch.setattr(pn, '_resolve_user_id', lambda u: 7)
        monkeypatch.setattr(pn, 'check_delivery_outage', lambda *a, **k: [])

        class _DB:
            def get_preferences(self, uid):
                return {'notifications': {}}
        import sys, types
        mod = types.ModuleType('user_db')
        mod.user_db = _DB()
        monkeypatch.setitem(sys.modules, 'user_db', mod)

        verdict = pn._send_user_notifications('7', 'BTC', 'above', 100.0, 150.0)
        assert verdict is None
