"""Notification credentials never reach logs, the DLQ or an error response.

A Telegram bot token is part of the request URL, a Discord webhook URL is
itself the credential, and an ntfy topic URL is the subscription. requests
and urllib3 put the request URL into their exception messages, so formatting
an exception is enough to write the secret wherever the message goes.
"""

import importlib
import json
import logging

import pytest
import requests

TOKEN = '123456789:AAHsecretSECRETsecretSECRETsecret_x'
DISCORD = 'https://discord.com/api/webhooks/112233445566/WEBHOOKsecretTOKENvalue-abc_123'
NTFY = 'https://ntfy.sh/private-topic-9f8e7d'


def _conn_error(path):
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='api.example.org', port=443): Max retries exceeded "
        f"with url: {path} (Caused by NewConnectionError('Failed to establish a new connection'))"
    )


def _http_error(url):
    return requests.exceptions.HTTPError(f'401 Client Error: Unauthorized for url: {url}')


# ── the helper ───────────────────────────────────────────────────────

class TestRedactSecrets:
    @pytest.mark.parametrize('text,secret', [
        (str(_conn_error(f'/bot{TOKEN}/getMe')), TOKEN),
        (str(_http_error(f'https://api.telegram.org/bot{TOKEN}/sendMessage')), TOKEN),
        (str(_http_error(DISCORD)), 'WEBHOOKsecretTOKENvalue-abc_123'),
        (str(_conn_error('/private-topic-9f8e7d')), 'private-topic-9f8e7d'),
        (f'posting to {DISCORD} failed', 'WEBHOOKsecretTOKENvalue-abc_123'),
        (f'token {TOKEN} rejected', TOKEN),
    ])
    def test_secret_is_removed(self, text, secret):
        from carrac.notifications.redact import redact_secrets
        out = redact_secrets(text)
        assert secret not in out
        assert '<redacted>' in out

    def test_keeps_the_host_and_the_cause(self):
        from carrac.notifications.redact import redact_secrets
        out = redact_secrets(str(_http_error(f'https://api.telegram.org/bot{TOKEN}/sendMessage')))
        assert '401 Client Error' in out
        assert 'api.telegram.org' in out

    def test_leaves_ordinary_text_alone(self):
        from carrac.notifications.redact import redact_secrets
        text = 'telegram HTTP 400: {"ok":false,"description":"Bad Request: chat not found"}'
        assert redact_secrets(text) == text


# ── the dead-letter queue ────────────────────────────────────────────

@pytest.fixture
def resilience(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    from carrac.notifications import resilience as mod
    importlib.reload(mod)
    mod.reset_all_breakers()
    yield mod
    mod.reset_all_breakers()


def _dlq_text(mod):
    return mod.DLQ_FILE.read_text(encoding='utf-8')


class TestDeadLetterQueue:
    def test_exception_text_is_redacted(self, resilience):
        def send():
            raise _conn_error(f'/bot{TOKEN}/sendMessage')

        assert resilience.deliver('telegram', 'u1', send, payload={'chat_id': '42'}) is False
        raw = _dlq_text(resilience)
        assert TOKEN not in raw
        rec = json.loads(raw.splitlines()[-1])
        assert rec['error'].startswith('ConnectionError: ')
        assert TOKEN not in resilience.breaker_status()['telegram:u1']['last_error']

    @pytest.mark.parametrize('channel,payload,secret', [
        ('discord', {'webhook_url': DISCORD, 'symbol': 'BTC', 'body': 'hit'}, DISCORD),
        ('ntfy', {'url': NTFY, 'symbol': 'BTC', 'body': 'hit'}, NTFY),
    ])
    def test_destination_is_not_stored(self, resilience, channel, payload, secret):
        assert resilience.deliver(channel, 'u1', lambda: False, payload=payload) is False
        raw = _dlq_text(resilience)
        assert secret not in raw
        rec = json.loads(raw.splitlines()[-1])
        # The message survives; the destination is the user's saved setting
        # for this channel, found again from (channel, user_id).
        assert rec['payload']['symbol'] == 'BTC'
        assert rec['payload']['body'] == 'hit'
        assert rec['channel'] == channel and rec['user_id'] == 'u1'

    def test_breaker_open_enqueue_strips_the_destination_too(self, resilience):
        for _ in range(resilience.CONSECUTIVE_FAILURES_TO_OPEN):
            resilience.deliver('discord', 'u2', lambda: False, payload={'webhook_url': DISCORD})
        resilience.deliver('discord', 'u2', lambda: True, payload={'webhook_url': DISCORD})
        raw = _dlq_text(resilience)
        assert 'breaker_open' in raw
        assert DISCORD not in raw


    def test_summary_redacts_records_written_before_redaction(self, resilience):
        from datetime import datetime, timezone
        resilience.DATA_DIR.mkdir(parents=True, exist_ok=True)
        old = {'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
               'channel': 'telegram', 'user_id': 'u3', 'reason': 'exception',
               'error': f'ConnectionError: ... with url: /bot{TOKEN}/sendMessage',
               'payload': {}}
        resilience.DLQ_FILE.write_text(json.dumps(old) + '\n', encoding='utf-8')
        summary = resilience.undelivered_summary('u3')
        assert summary['telegram']['count'] == 1
        assert TOKEN not in summary['telegram']['last_error']


# ── log sites ────────────────────────────────────────────────────────

class TestLogSites:
    def test_telegram_token_check_does_not_log_the_token(self, monkeypatch, caplog):
        from carrac.alerts import routes

        def boom(*a, **k):
            raise _conn_error(f'/bot{TOKEN}/getMe')

        monkeypatch.setattr(requests, 'get', boom)
        with caplog.at_level(logging.WARNING):
            assert routes.telegram_token_rejected(TOKEN) is None
        assert 'could not verify Telegram token' in caplog.text
        assert TOKEN not in caplog.text

    def test_operator_telegram_sender_does_not_log_the_token(self, real_notification_senders,
                                                             monkeypatch, caplog):
        from carrac.notifications import telegram

        def boom(*a, **k):
            raise _conn_error(f'/bot{TOKEN}/sendMessage')

        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', TOKEN)
        monkeypatch.setenv('TELEGRAM_CHAT_ID', '42')
        monkeypatch.setattr(telegram.requests, 'post', boom)
        with caplog.at_level(logging.WARNING):
            assert telegram.send_telegram_message('hi') is False
        assert 'Telegram send failed' in caplog.text
        assert TOKEN not in caplog.text

    @pytest.mark.parametrize('channel,notif,url,secret', [
        ('telegram', {'telegram': {'bot_token': TOKEN, 'chat_id': '42'}},
         f'https://api.telegram.org/bot{TOKEN}/sendMessage', TOKEN),
        ('discord', {'discord': {'webhook_url': DISCORD}}, DISCORD,
         'WEBHOOKsecretTOKENvalue-abc_123'),
    ])
    def test_test_send_failure_hides_the_credential(self, client, monkeypatch, caplog,
                                                    channel, notif, url, secret):
        from carrac.alerts import routes
        import carrac.secret_box as secret_box

        monkeypatch.setattr(routes, 'get_request_tier', lambda: 'premium')
        monkeypatch.setattr(routes.user_db, 'get_preferences',
                            lambda uid: {'notifications': notif})
        monkeypatch.setattr(secret_box, 'decrypt_token', lambda v: v)

        class _Resp:
            def raise_for_status(self):
                raise _http_error(url)

        monkeypatch.setattr(requests, 'post', lambda *a, **k: _Resp())
        with client.session_transaction() as sess:
            sess['user_id'] = 7
        with caplog.at_level(logging.WARNING):
            r = client.post('/api/notification-settings/test', json={'channel': channel})
        assert r.status_code == 400
        assert '401 Client Error' in r.get_json()['error']
        assert secret not in r.get_data(as_text=True)
        assert secret not in caplog.text
