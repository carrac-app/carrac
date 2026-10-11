"""Something must PAGE a user when a channel stops delivering.

A dead channel can silently drop notifications for weeks if the only signal
is a red line on the Settings page that someone has to go and look at.

WHY THIS IS PER-CHANNEL. An alarm on a streak where EVERY configured channel
fails would not fire when one channel keeps delivering while the others are
dead. Per-channel catches it — and it is also the only trigger for which the
routing rule (send the alarm over whatever still works) means anything.

The tests below stub only the outbound HTTP senders — the process boundary.
The unit under test (channel_health, the latch, the routing decision) runs for
real against a real health file in tmp_path.
"""
import json

import pytest


@pytest.fixture
def health(tmp_path, monkeypatch):
    """Point the durable ledger and the DLQ at tmp_path, nothing else."""
    from carrac.notifications import resilience as res
    monkeypatch.setattr(res, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(res, 'HEALTH_FILE', tmp_path / 'delivery_health.json')
    monkeypatch.setattr(res, 'DLQ_FILE', tmp_path / 'alert_dlq.jsonl')
    return res


def _dlq(res, user_id, channel, n, error='HTTP 404: topic gone'):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    with open(res.DLQ_FILE, 'a', encoding='utf-8') as f:
        for _ in range(n):
            f.write(json.dumps({
                'ts': now, 'channel': channel, 'user_id': str(user_id),
                'reason': 'exception', 'error': error, 'payload': {},
            }) + '\n')


NOTIF_TWO = {
    'ntfy': {'enabled': True, 'url': 'https://ntfy.example/topic'},
    'discord': {'enabled': True, 'webhook_url': 'https://discord.example/hook'},
}


@pytest.fixture
def sent(monkeypatch):
    """Capture what each channel was asked to send, without sending."""
    import push_notifications as pn
    calls = {'ntfy': [], 'discord': [], 'telegram': [], 'operator': []}
    monkeypatch.setattr(pn, '_send_ntfy_to_url',
                        lambda *a, **k: calls['ntfy'].append((a, k)) or True)
    monkeypatch.setattr(pn, '_send_discord_alert',
                        lambda *a, **k: calls['discord'].append((a, k)) or True)
    monkeypatch.setattr(pn, '_send_telegram_to_user',
                        lambda *a, **k: calls['telegram'].append((a, k)) or True)
    import carrac.notifications.ntfy as ntfy_mod
    monkeypatch.setattr(ntfy_mod, 'send_ntfy',
                        lambda *a, **k: calls['operator'].append((a, k)) or True)
    return calls


class TestDeliveryOutageAlarm:

    def test_broken_channel_alarms_over_the_one_still_working(self, health, sent):
        """The realistic outage: ntfy dead, discord alive."""
        import push_notifications as pn
        health.record_delivery_outcome('ntfy', '1', False)
        health.record_delivery_outcome('discord', '1', True)
        _dlq(health, '1', 'ntfy', 3)

        alarmed = pn.check_delivery_outage('1', NOTIF_TWO)

        assert alarmed == ['ntfy']
        assert len(sent['discord']) == 1, 'alarm must go out over the WORKING channel'
        assert not sent['operator'], 'operator escalation is only for total failure'
        body = sent['discord'][0][0][2]
        assert 'ntfy' in body and 'HTTP 404' in body, 'must name the channel and the cause'

    def test_total_failure_escalates_to_the_operator_channel(self, health, sent):
        """Telling a user over a dead channel that their channels are dead is
        circular — this is the escape."""
        import push_notifications as pn
        for ch in ('ntfy', 'discord'):
            health.record_delivery_outcome(ch, '1', False)
            _dlq(health, '1', ch, 3)

        alarmed = pn.check_delivery_outage('1', NOTIF_TWO)

        assert alarmed == ['discord', 'ntfy']
        assert len(sent['operator']) == 1
        assert sent['operator'][1 - 1][1].get('priority') == '5', \
            'priority>=4 is what mirrors the alarm to operator Telegram'

    def test_it_raises_once_per_incident_not_once_per_fire(self, health, sent):
        """An alarm that repeats every tick becomes furniture."""
        import push_notifications as pn
        health.record_delivery_outcome('ntfy', '1', False)
        health.record_delivery_outcome('discord', '1', True)
        _dlq(health, '1', 'ntfy', 3)

        assert pn.check_delivery_outage('1', NOTIF_TWO) == ['ntfy']
        assert pn.check_delivery_outage('1', NOTIF_TWO) == []
        assert pn.check_delivery_outage('1', NOTIF_TWO) == []
        assert len(sent['discord']) == 1

    def test_a_success_clears_the_latch_so_the_next_outage_alarms(self, health, sent):
        import push_notifications as pn
        health.record_delivery_outcome('ntfy', '1', False)
        health.record_delivery_outcome('discord', '1', True)
        _dlq(health, '1', 'ntfy', 3)
        assert pn.check_delivery_outage('1', NOTIF_TWO) == ['ntfy']

        health.record_delivery_outcome('ntfy', '1', True)      # channel recovers
        assert health.channel_health('1')['ntfy']['broken'] is False
        health.record_delivery_outcome('ntfy', '1', False)     # and dies again
        _dlq(health, '1', 'ntfy', 3)

        assert pn.check_delivery_outage('1', NOTIF_TWO) == ['ntfy'], \
            'a recovered-then-failed channel must be able to alarm again'

    def test_one_blip_does_not_alarm(self, health, sent):
        """A discord blip (two failures in 65 minutes) is
        not an outage. That must not page anyone."""
        import push_notifications as pn
        health.record_delivery_outcome('discord', '1', False)
        health.record_delivery_outcome('ntfy', '1', True)
        _dlq(health, '1', 'discord', 2)          # below OUTAGE_MIN_FAILURES

        assert pn.check_delivery_outage('1', NOTIF_TWO) == []
        assert not any(sent.values())

    def test_no_channels_at_all_alarms_the_operator(self, health, sent):
        """Zero channels is the degenerate case of every channel being dead,
        and it is worse: nothing is attempted, so nothing reaches the DLQ and
        there is no record anywhere that the user was not told.

        A BTC once_per_bar alert firing every hour for weeks. Its 775 ntfy
        failures stopped because the channel was DISABLED — quiet, not fixed."""
        import push_notifications as pn
        assert pn.check_delivery_outage('11', {'ntfy': {'enabled': False}}) == ['__nochannels__']
        assert len(sent['operator']) == 1
        assert not sent['ntfy'] and not sent['discord'] and not sent['telegram'], \
            'there is by definition no way to reach this user'

    def test_no_channels_alarm_is_once_per_incident_and_clears(self, health, sent):
        import push_notifications as pn
        none_cfg = {'ntfy': {'enabled': False}}
        assert pn.check_delivery_outage('11', none_cfg) == ['__nochannels__']
        assert pn.check_delivery_outage('11', none_cfg) == []
        assert len(sent['operator']) == 1, 'must not re-page every hour'

        # The user configures a channel that then works — latch must drop, so a
        # future lapse can alarm again. Nothing else would ever clear it: there
        # is no delivery outcome on a channel that does not exist.
        health.record_delivery_outcome('discord', '11', True)
        assert pn.check_delivery_outage('11', {
            'discord': {'enabled': True, 'webhook_url': 'https://x.example/h'}}) == []
        assert pn.check_delivery_outage('11', none_cfg) == ['__nochannels__']
        assert len(sent['operator']) == 2

    def test_silence_is_not_health(self, health, sent):
        """A channel with no durable record must not be treated as working, or
        the alarm routes into a void. That is breaker_status()'s structural
        mistake and this must not repeat it."""
        import push_notifications as pn
        health.record_delivery_outcome('ntfy', '1', False)
        _dlq(health, '1', 'ntfy', 3)
        # discord configured but never attempted -> unproven, not healthy

        assert pn.check_delivery_outage('1', NOTIF_TWO) == ['ntfy']
        assert not sent['discord'], 'must not route to an unproven channel'
        assert len(sent['operator']) == 1, 'unproven means escalate, not assume'

    def test_configured_channels_matches_the_real_send_path(self):
        """Sibling sweep. If this drifts from the enablement tests in
        _send_user_notifications, the alarm reports on a channel that is never
        tried, or stays quiet about one that is."""
        import push_notifications as pn
        assert pn._configured_channels({
            'ntfy': {'enabled': True},                       # enabled, no url
            'discord': {'enabled': False, 'webhook_url': 'x'},  # url, disabled
            'telegram': {'enabled': True, 'bot_token': 't'},  # no chat_id
        }) == set()
        assert pn._configured_channels({
            'telegram': {'enabled': True, 'bot_token': 't', 'chat_id': 'c'},
        }) == {'telegram'}
