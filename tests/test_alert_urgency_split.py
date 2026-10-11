"""Price alerts must be the only thing Carrac sends that makes the phone ring.

Report: a price alert "either failed to arrive at
the most inconvenient time possible, or arrived and was accidentally discarded
amid the plethora of notifications the platform sends".

The second half was structural. Every message left through the same door at the
same urgency — ntfy Priority 4, tag chart_with_upwards_trend, and a title
hardcoded to '{symbol} Price Alert' regardless of what actually happened. A
regime transition arrived as "REGIME Price Alert"; the webhook-auto-disabled
notice arrived as "Webhook Price Alert"; a morning report and a stop being hit
were indistinguishable at a glance.

Decision: Telegram carries the urgent tier. Telegram has no
priority header, so the mechanism is `disable_notification` — routine traffic
arrives silently, urgent traffic rings.
"""
import push_notifications as pn


class TestWhatCountsAsUrgent:
    def test_a_price_crossing_is_urgent(self):
        assert pn._is_urgent('price') is True

    def test_a_trendline_break_is_urgent(self):
        for t in ('trendline', 'trendline_break'):
            assert pn._is_urgent(t) is True, t

    def test_slower_alert_families_are_not(self):
        """None of these need to wake anyone — a regime alert is evaluated
        once a DAY by quant/check_alerts.py, so ringing for it is noise by
        construction."""
        for t in ('indicator', 'formula', 'zscore', 'regime_transition',
                  'chain_strength_breach', 'anomaly_cluster_active',
                  'conditional_query_match', 'derivatives_regime'):
            assert pn._is_urgent(t) is False, t

    def test_an_unknown_type_defaults_to_urgent_only_when_absent(self):
        """Absent means the legacy price alert (alert_type was never written
        for those); a genuinely unknown string must not sneak into the ring
        tier."""
        assert pn._is_urgent(None) is True
        assert pn._is_urgent('') is True
        assert pn._is_urgent('something_new') is False


class TestTelegramUrgencyMechanism:
    def _capture(self, monkeypatch):
        sent = {}

        class _R:
            status_code = 200
            text = ''

        def fake_post(url, json=None, **kw):
            sent.update(json or {})
            return _R()

        import requests
        monkeypatch.setattr(requests, 'post', fake_post)
        monkeypatch.setattr(pn, '_ok_or_raise', lambda r, c: True)
        return sent

    def test_an_urgent_alert_rings(self, monkeypatch):
        sent = self._capture(monkeypatch)
        pn._send_telegram_to_user('1:tok', '42', 'BTC', 100.0, 150.0, 'above',
                                  user_id='7', urgent=True)
        assert sent['disable_notification'] is False

    def test_a_routine_alert_arrives_silently(self, monkeypatch):
        sent = self._capture(monkeypatch)
        pn._send_telegram_to_user('1:tok', '42', 'BTC', 100.0, 150.0, 'above',
                                  user_id='7', urgent=False)
        assert sent['disable_notification'] is True, (
            'without this every digest and play transition competes with a '
            'stop being hit'
        )

    def test_the_urgent_message_is_visually_distinct(self, monkeypatch):
        sent = self._capture(monkeypatch)
        pn._send_telegram_to_user('1:tok', '42', 'BTC', 100.0, 150.0, 'above',
                                  user_id='7', urgent=True)
        assert sent['text'].startswith('🔔')


class TestTheMarkdownHazard:
    """Telegram's legacy Markdown must not be handed an unparseable note.

    The drawn-alert path writes the literal note `[drawn]`. Telegram's legacy
    Markdown reads `[` as an inline-link opener, so the raw payload is
    unparseable and would be rejected with a 400.
    """

    def test_the_exact_prod_note_is_neutralised(self):
        note = '[drawn]  •  regime:favorable | FEAR_GREED=+8.1σ'
        safe = pn._tg_safe(note)
        assert '[' not in safe and ']' not in safe
        assert 'drawn' in safe, 'the note must survive, only its markup dies'
        assert 'regime:favorable' in safe

    def test_all_markdown_control_characters_are_removed(self):
        assert pn._tg_safe('a[b]c*d_e`f') == 'abcdef'

    def test_our_own_formatting_still_reaches_telegram(self, monkeypatch):
        """The guard against over-correcting: sanitising the note must not
        strip the bold title we add around it."""
        sent = {}

        class _R:
            status_code = 200
            text = ''

        import requests
        monkeypatch.setattr(requests, 'post',
                            lambda url, json=None, **kw: (sent.update(json or {}), _R())[1])
        monkeypatch.setattr(pn, '_ok_or_raise', lambda r, c: True)

        pn._send_telegram_to_user('1:tok', '42', 'BTC', 100.0, 150.0, 'above',
                                  note='[drawn]', user_id='7', urgent=True)
        assert '*BTC Price Alert*' in sent['text']
        assert '[drawn]' not in sent['text']
        assert 'drawn' in sent['text']

    def test_empty_and_none_notes_are_safe(self):
        assert pn._tg_safe('') == ''
        assert pn._tg_safe(None) == ''


class TestNtfyStopsMislabellingEverything:
    """Uses `real_notification_senders` because these exercise the sender's own
    internals — the transport is mocked here instead."""

    def _capture(self, monkeypatch):
        seen = {}

        class _R:
            status_code = 200
            text = ''

        def fake_post(url, data=None, headers=None, **kw):
            seen.update(headers or {})
            return _R()

        import requests
        monkeypatch.setattr(requests, 'post', fake_post)
        monkeypatch.setattr(pn, '_ok_or_raise', lambda r, c: True)
        return seen

    def test_a_caller_supplied_title_is_used(self, monkeypatch, real_notification_senders):
        seen = self._capture(monkeypatch)
        pn._send_ntfy_to_url('https://ntfy.example/t', 'REGIME', 'body',
                             user_id='7', title='Regime transition')
        assert seen['Title'] == 'Regime transition', (
            'a regime alert must carry its own title, not "REGIME Price Alert"'
        )

    def test_the_old_default_still_applies_when_no_title_is_given(self, monkeypatch, real_notification_senders):
        seen = self._capture(monkeypatch)
        pn._send_ntfy_to_url('https://ntfy.example/t', 'BTC', 'body', user_id='7')
        assert seen['Title'] == 'BTC Price Alert'

    def test_urgent_and_routine_get_different_priorities(self, monkeypatch, real_notification_senders):
        seen = self._capture(monkeypatch)
        pn._send_ntfy_to_url('https://ntfy.example/t', 'BTC', 'body',
                             user_id='7', priority='5', tags='rotating_light')
        assert seen['Priority'] == '5'
        assert seen['Tags'] == 'rotating_light'
