"""A failed morning-report delivery is a lost message the user expected, so it
is logged at ERROR with the recipient id. Secret-bearing URLs (Discord webhook,
Telegram bot token) must not reach the log through the exception text."""
import logging
from unittest.mock import patch

import pytest

import push_notifications

SECRET = 'SECRET-TOKEN-123'


def _prefs(channel):
    cfgs = {
        'ntfy': {'ntfy': {'enabled': True, 'url': f'https://ntfy.example/{SECRET}'}},
        'discord': {'discord': {'enabled': True, 'webhook_url': f'https://discord.example/{SECRET}'}},
        'telegram': {'telegram': {'enabled': True, 'bot_token': 'tok', 'chat_id': '1'}},
    }
    return {'notifications': cfgs[channel]}


@pytest.mark.parametrize('channel', ['ntfy', 'discord', 'telegram'])
def test_failed_report_delivery_logs_error_with_recipient(channel, caplog):
    boom = RuntimeError(f'connection to https://x/{SECRET} failed')
    with patch.object(push_notifications, '_resolve_user_id', return_value=42), \
         patch('user_db.user_db.get_preferences', return_value=_prefs(channel)), \
         patch.object(push_notifications, '_decrypt_tg_token', return_value=SECRET), \
         patch('requests.post', side_effect=boom), \
         caplog.at_level(logging.WARNING, logger=push_notifications.logger.name):
        push_notifications.notify_user_report_ready('42', report_id=1)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, [(r.levelname, r.getMessage()) for r in caplog.records]
    assert channel in errors[0].getMessage().lower()
    assert '42' in errors[0].getMessage()
    assert SECRET not in errors[0].getMessage()
    if channel != 'ntfy':
        assert errors[0].exc_info is None
