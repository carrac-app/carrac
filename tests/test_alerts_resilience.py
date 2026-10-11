"""Tests for alerts reliability + integration.

Covers:
- Circuit-breaker open / close / half-open (carrac/notifications/resilience.py)
- DLQ enqueue on failure
- Dedup persistence in the alert store (once + once_per_bar)
- Bundling: multi-trigger same-tick digests, singleton direct-send
- Alert event capture to JSONL
- Context chip builder (graceful fallback when QE unavailable)
- Morning report alert summary formatter
"""

import importlib
import json

import pytest


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_push(tmp_path, monkeypatch, rebind_db):
    """Reload push_notifications + resilience with an isolated DATA_DIR and
    database so tests don't collide with each other or a developer's data."""
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    rebind_db(tmp_path / 'db')
    import push_notifications
    importlib.reload(push_notifications)
    from carrac.notifications import resilience
    importlib.reload(resilience)
    resilience.reset_all_breakers()
    yield push_notifications, resilience


# ─────────────────────────────────────────────────────────────
# Circuit-breaker
# ─────────────────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_closed_breaker_passes_success(self, fresh_push):
        _, resilience = fresh_push
        ok = resilience.deliver('ntfy', 'u1', lambda: True, payload={'k': 1})
        assert ok is True
        status = resilience.breaker_status()['ntfy:u1']
        assert status['is_open'] is False
        assert status['consecutive_failures'] == 0

    def test_single_failure_does_not_open(self, fresh_push):
        _, resilience = fresh_push
        resilience.deliver('ntfy', 'u1', lambda: False, payload={'k': 1})
        status = resilience.breaker_status()['ntfy:u1']
        assert status['is_open'] is False
        assert status['consecutive_failures'] == 1

    def test_five_failures_open_the_breaker(self, fresh_push):
        _, resilience = fresh_push
        for _ in range(resilience.CONSECUTIVE_FAILURES_TO_OPEN):
            resilience.deliver('discord', 'u1', lambda: False, payload={})
        status = resilience.breaker_status()['discord:u1']
        assert status['is_open'] is True
        assert status['consecutive_failures'] == resilience.CONSECUTIVE_FAILURES_TO_OPEN

    def test_open_breaker_short_circuits(self, fresh_push):
        _, resilience = fresh_push
        for _ in range(resilience.CONSECUTIVE_FAILURES_TO_OPEN):
            resilience.deliver('telegram', 'u2', lambda: False, payload={'step': 'fail'})

        calls = []

        def probe():
            calls.append(1)
            return True

        ok = resilience.deliver('telegram', 'u2', probe, payload={'step': 'probe'})
        # Breaker is open → send_fn must not be called, DLQ gets the payload.
        assert ok is False
        assert calls == []

    def test_success_clears_failure_counter(self, fresh_push):
        _, resilience = fresh_push
        resilience.deliver('ntfy', 'u3', lambda: False, payload={})
        resilience.deliver('ntfy', 'u3', lambda: False, payload={})
        resilience.deliver('ntfy', 'u3', lambda: True, payload={})
        status = resilience.breaker_status()['ntfy:u3']
        assert status['consecutive_failures'] == 0
        assert status['is_open'] is False

    def test_exception_counts_as_failure(self, fresh_push):
        _, resilience = fresh_push

        def boom():
            raise RuntimeError('channel down')

        ok = resilience.deliver('discord', 'u4', boom, payload={})
        assert ok is False
        status = resilience.breaker_status()['discord:u4']
        assert status['consecutive_failures'] == 1
        assert 'RuntimeError' in status['last_error']


# ─────────────────────────────────────────────────────────────
# DLQ
# ─────────────────────────────────────────────────────────────

class TestDLQ:
    def test_failed_send_enqueued_to_dlq(self, fresh_push, tmp_path):
        _, resilience = fresh_push
        resilience.deliver('ntfy', 'u1', lambda: False, payload={'note': 'hello'})
        records = resilience.dlq_tail(100)
        assert len(records) == 1
        rec = records[0]
        assert rec['channel'] == 'ntfy'
        assert rec['user_id'] == 'u1'
        assert rec['reason'] == 'send_failed'
        assert rec['payload'] == {'note': 'hello'}

    def test_dlq_tail_returns_last_n(self, fresh_push):
        _, resilience = fresh_push
        for i in range(3):
            resilience.deliver('discord', 'u1', lambda: False, payload={'i': i})
        recs = resilience.dlq_tail(2)
        assert len(recs) == 2
        assert [r['payload']['i'] for r in recs] == [1, 2]

    def test_dlq_tail_empty_when_no_failures(self, fresh_push):
        _, resilience = fresh_push
        assert resilience.dlq_tail(100) == []

    def test_open_breaker_enqueues_with_breaker_open_reason(self, fresh_push):
        _, resilience = fresh_push
        for _ in range(resilience.CONSECUTIVE_FAILURES_TO_OPEN):
            resilience.deliver('telegram', 'u1', lambda: False, payload={'ts': 1})
        resilience.deliver('telegram', 'u1', lambda: True, payload={'ts': 2})

        reasons = [r['reason'] for r in resilience.dlq_tail(100)]
        # First five records are plain send_failed, the sixth is breaker_open.
        assert reasons[-1] == 'breaker_open'
        assert reasons[:5] == ['send_failed'] * 5


# ─────────────────────────────────────────────────────────────
# Alert persistence & dedup
# ─────────────────────────────────────────────────────────────

class TestAlertDedup:
    def test_once_alert_persists_triggered_state(self, fresh_push):
        pn, _ = fresh_push
        pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once')
        pn.check_alerts({'BTC': 101.0})

        reloaded = pn.get_user_alerts('1', active_only=False)
        assert len(reloaded) == 1
        assert reloaded[0]['triggered'] is True
        assert reloaded[0]['triggered_at'] is not None
        assert reloaded[0]['triggered_price'] == 101.0

    def test_once_alert_does_not_re_fire(self, fresh_push):
        pn, _ = fresh_push
        pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once')
        first = pn.check_alerts({'BTC': 101.0})
        second = pn.check_alerts({'BTC': 102.0})
        assert len(first) == 1
        assert len(second) == 0

    def test_once_per_bar_rearms_on_next_bar(self, fresh_push, monkeypatch):
        pn, _ = fresh_push
        pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once_per_bar')

        from datetime import datetime as real_dt

        class _FakeDT:
            _now = real_dt(2026, 4, 18, 10, 30)

            @classmethod
            def now(cls, tz=None):
                return cls._now

            @classmethod
            def utcnow(cls):
                return cls._now

            @classmethod
            def fromisoformat(cls, s):
                return real_dt.fromisoformat(s)

        monkeypatch.setattr(pn, 'datetime', _FakeDT)

        # Bar 1: first fire succeeds
        first = pn.check_alerts({'BTC': 101.0})
        assert len(first) == 1

        # Same bar: no re-fire
        second = pn.check_alerts({'BTC': 102.0})
        assert len(second) == 0

        # Advance the clock to the next hourly bar
        _FakeDT._now = real_dt(2026, 4, 18, 11, 5)
        third = pn.check_alerts({'BTC': 103.0})
        assert len(third) == 1

    def test_dedup_survives_module_reload(self, fresh_push):
        pn, _ = fresh_push
        pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once')
        pn.check_alerts({'BTC': 101.0})
        # Simulate a process restart by reloading the module (empties in-memory state)
        import push_notifications as pn_mod
        importlib.reload(pn_mod)
        reloaded = pn_mod.get_user_alerts('1', active_only=False)
        assert reloaded[0]['triggered'] is True


# ─────────────────────────────────────────────────────────────
# Event capture + context chip
# ─────────────────────────────────────────────────────────────

class TestAlertEventCapture:
    def test_trigger_writes_alert_event_line(self, fresh_push, tmp_path):
        pn, _ = fresh_push
        pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once')
        pn.check_alerts({'BTC': 101.25})

        events_file = tmp_path / 'alert_events.jsonl'
        assert events_file.exists()
        lines = events_file.read_text().strip().splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event['symbol'] == 'BTC'
        assert event['alert_type'] == 'price'
        assert event['user_id'] == '1'
        assert event['price_at_fire'] == 101.25
        assert event['condition'] == 'above'

    def test_context_chip_empty_when_no_context(self, fresh_push):
        pn, _ = fresh_push
        # No QE available → chip renders as empty string (not a crash)
        chip = pn.build_context_chip(context={})
        assert chip == ''

    def test_context_chip_renders_regime_and_zscores(self, fresh_push):
        pn, _ = fresh_push
        chip = pn.build_context_chip(context={
            'regime': 'risk-off',
            'top_zscores': {'VIX': 2.4, 'M2': -1.8},
            'derivatives': {'skew_25d': -2.1},
        })
        assert 'regime:risk-off' in chip
        assert 'VIX=+2.4σ' in chip
        # 'skew25d' must NOT appear in the chip. skew_25d is Deribit-barred and the chip is
        # concatenated onto every outgoing notification body, so it is now
        # operator-only and this call names no recipient.
        assert 'skew' not in chip.lower()


# ─────────────────────────────────────────────────────────────
# Bundling
# ─────────────────────────────────────────────────────────────

class TestBundling:
    def test_multi_trigger_same_user_same_tick_bundles(self, fresh_push, monkeypatch):
        pn, _ = fresh_push
        pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once')
        pn.create_alert('1', 'ETH', 'above', 3000.0, frequency='once')
        pn.create_alert('1', 'SOL', 'above', 200.0, frequency='once')

        calls = {'push': 0, 'batched': 0}

        def _fake_push(**kw):
            calls['push'] += 1
            return True

        def _fake_batched(user_id, items):
            calls['batched'] += 1
            assert len(items) == 3

        monkeypatch.setattr(pn, 'send_push_notification', _fake_push)
        monkeypatch.setattr(pn, 'send_batched_position_warnings', _fake_batched)

        triggered = pn.check_alerts({'BTC': 101.0, 'ETH': 3100.0, 'SOL': 210.0})
        assert len(triggered) == 3
        # One digest push (not three) + one batched fanout
        assert calls['push'] == 1
        assert calls['batched'] == 1

    def test_single_trigger_does_not_bundle(self, fresh_push, monkeypatch):
        pn, _ = fresh_push
        pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once')

        calls = {'push': 0, 'user_channels': 0}

        def _fake_push(**kw):
            calls['push'] += 1
            return True

        def _fake_user_channels(*a, **kw):
            calls['user_channels'] += 1

        monkeypatch.setattr(pn, 'send_push_notification', _fake_push)
        monkeypatch.setattr(pn, '_send_user_notifications', _fake_user_channels)

        pn.check_alerts({'BTC': 101.0})
        assert calls['push'] == 1
        assert calls['user_channels'] == 1

    def test_different_users_do_not_cross_bundle(self, fresh_push, monkeypatch):
        pn, _ = fresh_push
        pn.create_alert('1', 'BTC', 'above', 100.0, frequency='once')
        pn.create_alert('2', 'BTC', 'above', 100.0, frequency='once')

        seen_users = []

        def _fake_push(**kw):
            seen_users.append(kw.get('user_id'))
            return True

        monkeypatch.setattr(pn, 'send_push_notification', _fake_push)
        monkeypatch.setattr(pn, '_send_user_notifications', lambda *a, **k: None)

        pn.check_alerts({'BTC': 101.0})
        assert sorted(seen_users) == ['1', '2']


# ─────────────────────────────────────────────────────────────
# Morning report summary formatter
# ─────────────────────────────────────────────────────────────

class TestMorningAlertSummary:
    def test_empty_alerts_yields_friendly_line(self):
        from morning_engine.data_assembly import _format_alerts_summary
        assert _format_alerts_summary({'alerts': []}) == 'No alerts configured.'

    def test_error_payload_returns_unavailable(self):
        from morning_engine.data_assembly import _format_alerts_summary
        out = _format_alerts_summary({'error': 'boom'})
        assert out.startswith('Unavailable:')

    def test_triggered_recent_listed_first(self):
        from datetime import datetime, timezone, timedelta
        from morning_engine.data_assembly import _format_alerts_summary

        fired_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        armed = {
            'symbol': 'ETH', 'alert_type': 'price', 'condition': 'below',
            'price': 2900, 'active': True,
        }
        fired = {
            'symbol': 'BTC', 'alert_type': 'price', 'condition': 'above',
            'price': 65000, 'active': True,
            'triggered_at': fired_ts, 'triggered_price': 64990.5,
        }
        block = _format_alerts_summary({'alerts': [armed, fired]})
        assert 'TRIGGERED IN LAST 24H (1)' in block
        assert 'ACTIVE WATCHLIST (1)' in block
        # Fired block shows up before the watchlist block
        assert block.index('TRIGGERED') < block.index('ACTIVE')
        # Watchlist line excludes the alerts that already fired
        assert 'ETH below $2,900' in block

    def test_old_triggered_alerts_demote_to_watchlist(self):
        from datetime import datetime, timezone, timedelta
        from morning_engine.data_assembly import _format_alerts_summary

        old_ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        old_alert = {
            'symbol': 'BTC', 'alert_type': 'price', 'condition': 'above',
            'price': 65000, 'active': True, 'triggered_at': old_ts,
        }
        block = _format_alerts_summary({'alerts': [old_alert]})
        # Triggered too long ago → not in "last 24h" block
        assert 'TRIGGERED IN LAST 24H' not in block
        assert 'ACTIVE WATCHLIST (1)' in block

    def test_paused_alerts_counted_not_listed(self):
        from morning_engine.data_assembly import _format_alerts_summary
        paused = {'symbol': 'BTC', 'alert_type': 'price', 'active': False}
        block = _format_alerts_summary({'alerts': [paused]})
        assert 'PAUSED: 1' in block


# ─────────────────────────────────────────────────────────────
# Plays monitor push import
# ─────────────────────────────────────────────────────────────

class TestPlaysMonitorPushImport:
    def test_plays_monitor_imports_send_push_notification(self):
        """Pins that plays/monitor.py imports a symbol that exists in
        push_notifications.py (send_push_notification, not send_web_push)."""
        import push_notifications
        # The canonical symbol name must exist.
        assert hasattr(push_notifications, 'send_push_notification')
        # And the legacy name must NOT be re-added silently.
        assert not hasattr(push_notifications, 'send_web_push')


# ─────────────────────────────────────────────────────────────
# Undelivered-alert visibility
# ─────────────────────────────────────────────────────────────

class TestUndeliveredSummary:
    """The settings page must be able to tell a user a channel is dead.

    Prod ran for weeks with 511 undelivered ntfy alerts on disk for one
    user while the settings page showed the channel enabled and healthy.
    """

    def _write(self, resilience, records):
        import json as _json
        resilience.DLQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(resilience.DLQ_FILE, 'w', encoding='utf-8') as f:
            for r in records:
                f.write(_json.dumps(r) + '\n')

    def _rec(self, ch='ntfy', uid='11', reason='breaker_open', ago_hours=1):
        from datetime import datetime, timedelta, timezone
        ts = datetime.now(timezone.utc) - timedelta(hours=ago_hours)
        return {
            'ts': ts.isoformat().replace('+00:00', 'Z'),
            'channel': ch, 'user_id': uid, 'reason': reason, 'payload': {},
        }

    def test_no_dlq_file_reports_healthy(self, fresh_push):
        _, resilience = fresh_push
        assert resilience.undelivered_summary('11') == {}

    def test_counts_are_per_channel(self, fresh_push):
        _, resilience = fresh_push
        self._write(resilience, [
            self._rec(ch='ntfy'), self._rec(ch='ntfy'), self._rec(ch='telegram'),
        ])
        out = resilience.undelivered_summary('11')
        assert out['ntfy']['count'] == 2
        assert out['telegram']['count'] == 1

    def test_another_users_failures_are_not_leaked(self, fresh_push):
        """A shared DLQ file must not show user A's outage to user B."""
        _, resilience = fresh_push
        self._write(resilience, [
            self._rec(uid='11'), self._rec(uid='11'), self._rec(uid='99'),
        ])
        assert resilience.undelivered_summary('99')['ntfy']['count'] == 1
        assert resilience.undelivered_summary('11')['ntfy']['count'] == 2
        assert resilience.undelivered_summary('12345') == {}

    def test_old_failures_fall_out_of_the_window(self, fresh_push):
        """A channel fixed two days ago must stop warning, or the banner
        becomes furniture and gets ignored."""
        _, resilience = fresh_push
        self._write(resilience, [
            self._rec(ago_hours=200), self._rec(ago_hours=100), self._rec(ago_hours=1),
        ])
        assert resilience.undelivered_summary('11', since_hours=48)['ntfy']['count'] == 1

    def test_last_reason_and_ts_come_from_the_newest_record(self, fresh_push):
        _, resilience = fresh_push
        self._write(resilience, [
            self._rec(reason='send_failed', ago_hours=5),
            self._rec(reason='half_open_probe_in_flight', ago_hours=1),
        ])
        out = resilience.undelivered_summary('11')
        assert out['ntfy']['last_reason'] == 'half_open_probe_in_flight'

    def test_a_partial_record_at_the_seek_point_is_discarded(self, fresh_push):
        """Seeking to a byte offset lands mid-line. That half-line must be
        dropped rather than counted or blowing up the parse."""
        _, resilience = fresh_push
        self._write(resilience, [self._rec() for _ in range(50)])
        out = resilience.undelivered_summary('11', max_bytes=500)
        assert out['ntfy']['count'] > 0
        assert out['ntfy']['count'] < 50

    def test_a_corrupt_line_does_not_hide_the_rest(self, fresh_push):
        _, resilience = fresh_push
        import json as _json
        resilience.DLQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(resilience.DLQ_FILE, 'w', encoding='utf-8') as f:
            f.write(_json.dumps(self._rec()) + '\n')
            f.write('{not json at all\n')
            f.write(_json.dumps(self._rec()) + '\n')
        assert resilience.undelivered_summary('11')['ntfy']['count'] == 2

    def test_a_real_failed_delivery_shows_up_end_to_end(self, fresh_push):
        """Not a synthetic file — drive the actual deliver() path."""
        _, resilience = fresh_push
        for _ in range(3):
            resilience.deliver('ntfy', '11', lambda: False, payload={'x': 1})
        out = resilience.undelivered_summary('11')
        assert out['ntfy']['count'] == 3
        assert out['ntfy']['last_reason'] == 'send_failed'
