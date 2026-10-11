"""A failed notification must say WHY, and a handled reconnect must not page.

Two findings from error-tracker triage, same root shape: a signal that
fires without carrying what you need to act on it.

**1. The breaker records nothing useful for an HTTP rejection.**
``resilience.deliver`` has two failure paths and they are not equally
informative (carrac/notifications/resilience.py::deliver):

    except Exception as e:  ->  _record_failure(..., f'{type(e).__name__}: {e}')
    if not ok:              ->  _record_failure(..., 'send_fn returned False')

A channel whose ``_do_send`` ends with ``return 200 <= r.status_code < 300``
throws away the status and body. A *network* failure then lands on the rich
path (requests raises) while an *API rejection* lands on the useless one.

Measured: ntfy's breaker context carried the full
``ConnectionError: HTTPSConnectionPool(host='ntfy.sh', port=443)...``, which
root-causes it in minutes. Telegram failed 9 times and recorded
only ``send_fn returned False`` — reachable host (28ms), so an API rejection,
cause unknown and unknowable once the worker logs that held it rotate
away on a container recreate.

**2. A third-party library double-reports a condition we already handle.**
The Finnhub websocket client logs its own disconnect at WARNING and reconnects
with exponential backoff (data_sources/finnhub_ws.py::_on_error and its reconnect loop). The
underlying ``websocket`` library ALSO logs it, at ERROR, and Sentry's logging
integration turns any ERROR record into an event. Result: 8313 error-tracker events
for a condition that is handled, against a 3000-event project cap.

Silencing that is not muting an alarm — Carrac's own log line and its reconnect
logic are untouched. It stops a library we do not control from raising an issue
about something we already report ourselves at the honest level.
"""
import logging

import pytest


class _Resp:
    def __init__(self, status_code, text=''):
        self.status_code = status_code
        self.text = text


class TestTheFailureCarriesTheReason:
    def test_a_2xx_is_success(self):
        from push_notifications import _ok_or_raise

        assert _ok_or_raise(_Resp(200), 'telegram') is True
        assert _ok_or_raise(_Resp(204), 'ntfy') is True

    def test_a_rejection_raises_with_status_and_body(self):
        """The whole point: the status and the body have to survive, because
        they are the difference between 'bad token', 'chat not found' and
        'rate limited' — three failures with three different fixes."""
        from push_notifications import _ok_or_raise

        with pytest.raises(Exception) as ei:
            _ok_or_raise(
                _Resp(400, '{"ok":false,"error_code":400,'
                           '"description":"Bad Request: chat not found"}'),
                'telegram',
            )
        msg = str(ei.value)
        assert '400' in msg
        assert 'chat not found' in msg
        assert 'telegram' in msg

    def test_the_body_is_truncated(self):
        """Bounded: last_error is stored at [:200] and the DLQ line is written
        per failure. An HTML error page must not become the log."""
        from push_notifications import _ok_or_raise

        with pytest.raises(Exception) as ei:
            _ok_or_raise(_Resp(500, 'x' * 5000), 'discord')
        assert len(str(ei.value)) < 400

    def test_newlines_do_not_break_the_log_line(self):
        from push_notifications import _ok_or_raise

        with pytest.raises(Exception) as ei:
            _ok_or_raise(_Resp(403, 'line one\nline two'), 'ntfy')
        assert '\n' not in str(ei.value)


class TestEveryChannelReportsTheSameWay:
    """The sibling sweep. Three channels had the identical
    status-discarding line; fixing one and leaving two is how they drift."""

    # Read the source from DISK, not via getattr. tests/conftest.py replaces
    # every notification sender with a `_recorder` stub (no test may
    # send a real notification), so `inspect.getsource(getattr(pn, name))`
    # returns the stub's body and the assertion below would pass on any code at
    # all — a test that cannot see what it is checking.
    @staticmethod
    def _sender_source(fn_name):
        import ast
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / 'push_notifications.py'
        text = path.read_text()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == fn_name:
                return ast.get_source_segment(text, node)
        raise AssertionError(f'{fn_name} not found in push_notifications.py')

    @pytest.mark.parametrize('fn_name,channel', [
        ('_send_ntfy_to_url', 'ntfy'),
        ('_send_discord_alert', 'discord'),
        ('_send_telegram_to_user', 'telegram'),
    ])
    def test_no_sender_returns_a_bare_status_comparison(self, fn_name, channel):
        src = self._sender_source(fn_name)
        assert 'return 200 <= r.status_code < 300' not in src, (
            f'{fn_name} still discards the status code, so a {channel} '
            f'rejection reaches the breaker as "send_fn returned False"'
        )
        assert f"_ok_or_raise(r, '{channel}')" in src, (
            f'{fn_name} does not route its response through the shared helper'
        )


class TestTheBreakerActuallyRecordsIt:
    """End-to-end through the real breaker — the helper is only worth anything
    if `deliver` puts its message somewhere a human reads."""

    def test_an_http_rejection_reaches_last_error(self, monkeypatch):
        from carrac.notifications import resilience
        from push_notifications import _ok_or_raise

        monkeypatch.setattr(resilience, '_dlq_enqueue',
                            lambda *a, **k: None)

        def send():
            return _ok_or_raise(_Resp(401, 'Unauthorized: bot token revoked'),
                                'telegram')

        ok = resilience.deliver('telegram', 'test-user-diag', send, payload={})
        assert ok is False

        st = resilience._get_state('telegram', 'test-user-diag')
        assert 'send_fn returned False' not in st.last_error
        assert '401' in st.last_error
        assert 'bot token revoked' in st.last_error


class TestTheHandledReconnectDoesNotPage:
    def test_the_websocket_library_logger_is_ignored_by_sentry(self):
        """`websocket` is the third-party client library, NOT
        carrac.data_sources.finnhub_ws — that one keeps logging, and its
        warning still shows in docker logs and as a Sentry breadcrumb."""
        from carrac import _SENTRY_IGNORED_LOGGERS

        assert 'websocket' in _SENTRY_IGNORED_LOGGERS
        assert not any(n.startswith('carrac') for n in _SENTRY_IGNORED_LOGGERS), (
            'ignoring one of our own loggers would hide real faults — only '
            'third-party libraries that double-report belong here'
        )

    def test_carracs_own_disconnect_log_is_untouched(self, caplog):
        """The guard against over-correcting: we still say it happened."""
        logger = logging.getLogger('carrac.data_sources.finnhub_ws')
        with caplog.at_level(logging.WARNING,
                             logger='carrac.data_sources.finnhub_ws'):
            logger.warning('Finnhub WS error: %s',
                           'Connection to remote host was lost.')
        assert any('Connection to remote host was lost' in r.message
                   for r in caplog.records)


class TestTheDLQCarriesTheReasonToo:
    """`last_error` lives in an in-process dict that dies with the
    container; the DLQ is the only durable record. Until now it stored
    `reason='exception'` and dropped the message, so prod's 1,085 undelivered
    alerts — ntfy ConnectionError and a Telegram 401, two completely different
    faults — were indistinguishable on disk and had to be re-run by hand to
    tell apart."""

    def _isolate(self, monkeypatch, tmp_path):
        from carrac.notifications import resilience
        monkeypatch.setattr(resilience, 'DATA_DIR', tmp_path)
        monkeypatch.setattr(resilience, 'DLQ_FILE', tmp_path / 'alert_dlq.jsonl')
        return resilience

    def _records(self, tmp_path):
        import json
        return [json.loads(line) for line
                in (tmp_path / 'alert_dlq.jsonl').read_text().splitlines() if line]

    def test_an_exception_lands_in_the_dlq_with_its_message(self, monkeypatch, tmp_path):
        resilience = self._isolate(monkeypatch, tmp_path)
        from push_notifications import _ok_or_raise

        def send():
            return _ok_or_raise(_Resp(401, 'Unauthorized'), 'telegram')

        assert resilience.deliver('telegram', 'u-dlq-1', send, payload={}) is False

        rec = self._records(tmp_path)[-1]
        assert rec['reason'] == 'exception'
        assert '401' in rec['error'], (
            'the DLQ recorded THAT it failed and not WHY — the exact gap that '
            'hides a revoked Telegram token'
        )
        assert 'Unauthorized' in rec['error']

    def test_the_two_real_prod_faults_are_now_distinguishable(self, monkeypatch, tmp_path):
        """The point of the field: ntfy's network block and Telegram's rejected
        credential need different fixes and must not look identical on disk."""
        resilience = self._isolate(monkeypatch, tmp_path)
        from push_notifications import _ok_or_raise

        def unreachable():
            raise ConnectionError("HTTPSConnectionPool(host='ntfy.sh', port=443)")

        def rejected():
            return _ok_or_raise(_Resp(401, 'Unauthorized'), 'telegram')

        resilience.deliver('ntfy', 'u-dlq-2', unreachable, payload={})
        resilience.deliver('telegram', 'u-dlq-2', rejected, payload={})

        errs = {r['channel']: r['error'] for r in self._records(tmp_path)}
        assert 'ntfy.sh' in errs['ntfy']
        assert '401' in errs['telegram']
        assert errs['ntfy'] != errs['telegram']

    def test_the_error_is_truncated_and_single_line(self, monkeypatch, tmp_path):
        """An HTML error page must not become the log — same rule `_ok_or_raise`
        already applies to the breaker."""
        resilience = self._isolate(monkeypatch, tmp_path)

        def send():
            raise RuntimeError('x\ny\r' + 'A' * 5000)

        resilience.deliver('discord', 'u-dlq-3', send, payload={})

        err = self._records(tmp_path)[-1]['error']
        assert len(err) <= 300
        assert '\n' not in err and '\r' not in err

    def test_undelivered_summary_surfaces_the_error(self, monkeypatch, tmp_path):
        """What the Settings banner reads. A red line saying 'not delivering'
        without the cause sends you to docker logs, which is where the earlier problem
        already had to go."""
        resilience = self._isolate(monkeypatch, tmp_path)
        from push_notifications import _ok_or_raise

        def send():
            return _ok_or_raise(_Resp(401, 'Unauthorized'), 'telegram')

        resilience.deliver('telegram', 'u-dlq-4', send, payload={})

        summary = resilience.undelivered_summary('u-dlq-4')
        assert summary['telegram']['count'] == 1
        assert '401' in (summary['telegram']['last_error'] or '')
