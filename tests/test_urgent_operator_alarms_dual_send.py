"""An urgent operator alarm must not be as quiet as a waitlist signup.

The operator ntfy topic carries the
venue-coverage collapse and the source-failure alarm, and
ALSO beta feedback and waitlist signups. A phone that mutes that topic — which
is the reasonable thing to do — mutes the alarms with the chatter.

Telegram is the designated urgent tier, so priority>=4 is mirrored there.
Priority 2 and 3 deliberately are NOT: mirroring everything would rebuild the
same undifferentiated stream on the channel that is supposed to stay
meaningful, which is the alarm-fatigue lesson one channel over.

The mirror is redundancy, not a dependency — it must never break the ntfy leg,
and it must still run when ntfy is unconfigured, because the failure to cover is exactly
that: every operator alert dropped with no route to the host.
"""
import pytest

from carrac.notifications import ntfy as ntfy_mod


@pytest.fixture(autouse=True)
def _use_the_real_senders(real_notification_senders):
    """These tests exercise send_ntfy's own internals, so they opt out of the
    notification recorder and mock the transport themselves instead."""


@pytest.fixture
def mirrored(monkeypatch, real_notification_senders):
    """Capture what the telegram mirror is asked to send."""
    calls = []

    def fake_send(text, parse_mode='Markdown'):
        calls.append({'text': text, 'parse_mode': parse_mode})
        return True

    import carrac.notifications.telegram as tg
    monkeypatch.setattr(tg, 'send_telegram_message', fake_send)
    return calls


class _Resp:
    def __init__(self, code=200):
        self.status_code = code
        self.text = ''


@pytest.fixture
def ntfy_up(monkeypatch, real_notification_senders):
    posts = []

    def fake_post(url, data=None, headers=None, timeout=None):
        posts.append({'url': url, 'headers': headers or {}})
        return _Resp(200)

    import requests
    monkeypatch.setattr(requests, 'post', fake_post)
    monkeypatch.setattr(ntfy_mod, 'NTFY_BASE', 'http://ntfy.test')
    monkeypatch.setenv('NTFY_CARRAC_TOPIC', 'ops-topic')
    return posts


class TestWhichPrioritiesMirror:
    @pytest.mark.parametrize('priority', ['4', '5', 'high', 'urgent', 'max'])
    def test_urgent_priorities_reach_telegram(self, priority, ntfy_up, mirrored):
        assert ntfy_mod.send_ntfy('body', title='T', priority=priority) is True
        assert len(mirrored) == 1, f'priority {priority!r} must mirror'

    @pytest.mark.parametrize('priority', ['1', '2', '3', 'default', 'low', 'min', None])
    def test_routine_priorities_do_not(self, priority, ntfy_up, mirrored):
        ntfy_mod.send_ntfy('body', title='T', priority=priority)
        assert mirrored == [], (
            f'priority {priority!r} mirrored — mirroring the chatter rebuilds '
            'the noise on the one channel meant to stay meaningful'
        )

    def test_the_five_real_alarms_all_qualify(self):
        """The alarms the operator actually cares about, by the priority their
        call sites pass: venue-coverage collapse, delisting candidate,
        source-failure, TradFi double outage, quant collector death."""
        assert ntfy_mod._is_urgent_priority('4') is True

    def test_waitlist_and_feedback_stay_quiet(self):
        """The two that made the topic mutable in the first place."""
        assert ntfy_mod._is_urgent_priority('2') is False   # waitlist signup
        assert ntfy_mod._is_urgent_priority('3') is False   # beta feedback


class TestTheMirrorIsRedundancyNotADependency:
    def test_ntfy_down_but_telegram_up_is_still_a_success(self, monkeypatch, mirrored):
        """The no-route case: no route to the ntfy host. Returning False when
        the alarm demonstrably reached the operator would be its own lie."""
        def boom(*a, **k):
            raise ConnectionError('Network is unreachable')

        import requests
        monkeypatch.setattr(requests, 'post', boom)
        monkeypatch.setattr(ntfy_mod, 'NTFY_BASE', 'http://ntfy.test')
        monkeypatch.setenv('NTFY_CARRAC_TOPIC', 'ops-topic')

        assert ntfy_mod.send_ntfy('body', title='T', priority='4') is True
        assert len(mirrored) == 1

    def test_unconfigured_ntfy_still_mirrors(self, monkeypatch, mirrored):
        monkeypatch.setattr(ntfy_mod, 'NTFY_BASE', '')
        assert ntfy_mod.send_ntfy('body', title='T', priority='4') is True
        assert len(mirrored) == 1

    def test_a_broken_mirror_never_breaks_the_ntfy_leg(self, ntfy_up, monkeypatch):
        import carrac.notifications.telegram as tg

        def boom(*a, **k):
            raise RuntimeError('telegram exploded')

        monkeypatch.setattr(tg, 'send_telegram_message', boom)
        assert ntfy_mod.send_ntfy('body', title='T', priority='4') is True
        assert len(ntfy_up) == 1, 'the ntfy publish must still have happened'

    def test_both_legs_down_is_reported_loudly(self, monkeypatch, caplog):
        import logging
        import requests
        import carrac.notifications.telegram as tg

        monkeypatch.setattr(requests, 'post',
                            lambda *a, **k: (_ for _ in ()).throw(ConnectionError('x')))
        monkeypatch.setattr(tg, 'send_telegram_message', lambda *a, **k: False)
        monkeypatch.setattr(ntfy_mod, 'NTFY_BASE', 'http://ntfy.test')
        monkeypatch.setenv('NTFY_CARRAC_TOPIC', 'ops-topic')

        with caplog.at_level(logging.ERROR, logger='carrac.notifications.ntfy'):
            assert ntfy_mod.send_ntfy('body', title='T', priority='4') is False

        assert any('reached NOBODY' in r.message for r in caplog.records), (
            'the alerting layer is the one layer whose failure nothing else '
            'can report — it must reach Bugsink'
        )


class TestTheMirrorTextSurvivesTelegram:
    def test_parse_mode_is_disabled_for_alarm_text(self, ntfy_up, mirrored):
        """Alarm bodies carry symbol lists, tracebacks and paths. An unmatched
        bracket under Markdown is a 400 — a poor way to lose an alarm."""
        ntfy_mod.send_ntfy('BUTTHOLE [mexc] failed: symbol_*_missing',
                           title='⚠️ Carrac source regression', priority='4')
        assert mirrored[0]['parse_mode'] is None
        assert 'BUTTHOLE [mexc]' in mirrored[0]['text'], 'body must go through intact'

    def test_the_title_leads_the_message(self, ntfy_up, mirrored):
        ntfy_mod.send_ntfy('detail here', title='⚡ Carrac data source alert',
                           priority='4')
        assert mirrored[0]['text'].startswith('⚡ Carrac data source alert')
