"""Remove notification credentials from text bound for logs and records.

A Telegram bot token is part of the request URL, a Discord webhook URL is
itself the credential, and an ntfy topic URL is the subscription. requests
and urllib3 copy the request URL into their exception messages
(``... with url: /bot<token>/sendMessage``, ``... for url: https://...``), so
any formatted exception from a send can carry one. Run such text through
``redact_secrets`` before it is logged, stored or shown.
"""

from __future__ import annotations

import re

_MARK = '<redacted>'

# "with url: /path" (urllib3 connection errors) and "for url: https://host/path"
# (requests HTTPError). Keep the scheme and host, drop the path and query.
_EXCEPTION_URL = re.compile(r'(\burl: )(https?://[^/\s]+)?(/\S*)')
# Telegram bot token, with or without the "bot" URL prefix.
_TELEGRAM_TOKEN = re.compile(r'\b(bot)?\d{5,}:[A-Za-z0-9_-]{20,}')
# Discord webhook: /api/webhooks/<id>/<token>.
_DISCORD_WEBHOOK = re.compile(r'(/api/webhooks/\d+/)[A-Za-z0-9_.-]+')


def redact_secrets(text) -> str:
    """``text`` with request paths, bot tokens and webhook tokens masked."""
    out = str(text)
    out = _EXCEPTION_URL.sub(lambda m: f'{m.group(1)}{m.group(2) or ""}/{_MARK}', out)
    out = _TELEGRAM_TOKEN.sub(lambda m: f'{m.group(1) or ""}{_MARK}', out)
    out = _DISCORD_WEBHOOK.sub(lambda m: f'{m.group(1)}{_MARK}', out)
    return out


def describe_exception(exc: BaseException) -> str:
    """``'<Type>: <message>'`` with credentials removed."""
    return redact_secrets(f'{type(exc).__name__}: {exc}')
