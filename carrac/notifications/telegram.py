"""
Telegram notification sender.
"""

import logging
import os

import requests

from carrac.notifications.redact import describe_exception

logger = logging.getLogger('carrac.notifications')


def send_telegram_message(text: str, parse_mode: str = 'Markdown') -> bool:
    """Send a message to Telegram. Splits long messages into 4096-char chunks.

    Returns True only if EVERY chunk was accepted. The response status must be
    checked: otherwise a 401 from a revoked token reads exactly like a
    delivered message.

    parse_mode is omitted from the request when falsy. Pass parse_mode=None for
    anything whose text you do not control: Telegram's legacy Markdown treats
    an unmatched `[`, `*` or `_` as a syntax error and rejects the whole
    message with a 400, which is a poor way to lose an alarm.
    """
    token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
    if not token or not chat_id:
        logger.warning("Telegram not configured (missing token or chat_id)")
        return False

    url = f'https://api.telegram.org/bot{token}/sendMessage'
    chunks = []
    while text:
        if len(text) <= 4096:
            chunks.append(text)
            break
        split_at = text.rfind('\n', 0, 4096)
        if split_at <= 0:
            split_at = 4096
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')

    all_ok = True
    for chunk in chunks:
        payload = {'chat_id': chat_id, 'text': chunk}
        if parse_mode:
            payload['parse_mode'] = parse_mode
        try:
            r = requests.post(url, json=payload, timeout=10)
            if not (200 <= r.status_code < 300):
                all_ok = False
                logger.warning(
                    'Telegram rejected the message: HTTP %s %s',
                    r.status_code, (getattr(r, 'text', '') or '')[:200].replace('\n', ' '))
        except Exception as e:
            all_ok = False
            # The token is in the request URL, and requests quotes the URL
            # in its exception messages.
            logger.warning('Telegram send failed: %s', describe_exception(e))
    return all_ok
