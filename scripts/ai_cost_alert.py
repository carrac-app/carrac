#!/usr/bin/env python3
"""Nightly AI cost-leak alert.

Checks today's global AI spend against warning (50%) and alert (83%) thresholds
of the $30 daily cap, then notifies via ntfy + Telegram.

Idempotent: uses a Redis dedup key so duplicate alerts never fire in the same
UTC day at the same threshold.

Run nightly from cron (see deploy/crontab.example):

    0 23 * * *  cd /path/to/carrac && \
                docker compose exec -T carrac python scripts/ai_cost_alert.py

Environment variables (read from .env or shell environment):
    REDIS_URL                  Redis connection string (required for spend data)
    AI_GLOBAL_DAILY_USD_CAP    Daily cap in USD (default: 30)
    NTFY_CARRAC_TOPIC                 ntfy topic (optional)
    TELEGRAM_BOT_TOKEN         Telegram bot token (optional)
    TELEGRAM_CHAT_ID           Telegram chat ID (optional)
"""
from __future__ import annotations

import os
import sys
import logging
from datetime import datetime, timezone


if __name__ == "__main__":
    # Answer --help before anything below is imported or touched.
    import argparse
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()

# Base URL of your ntfy server. Set it to https://ntfy.sh, or to a self-hosted
# instance if your host cannot reach ntfy.sh. No default: the address depends
# on where ntfy runs for your deployment, so it comes from the environment.
# Supplied by an `NTFY_BASE=` line at the top of the crontab (or, for
# container-run scripts, by the container env). Empty means alerting is off,
# and the send path says so.
NTFY_BASE = os.environ.get("NTFY_BASE", "")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('ai_cost_alert')

# Thresholds
_WARN_FRAC = 0.50   # 50% of cap → WARN
_ALERT_FRAC = 0.833  # 83% of cap → ALERT

_DEDUP_KEY_PREFIX = 'ai_cost_alerted:'   # ai_cost_alerted:YYYY-MM-DD:WARN|ALERT
_DEDUP_TTL = 2 * 24 * 3600              # keep for 2 days


def _redis_client():
    url = os.environ.get('REDIS_URL', '')
    if not url:
        logger.warning('REDIS_URL not set — cannot check spend or deduplicate alerts')
        return None
    try:
        import redis
        client = redis.from_url(url, socket_connect_timeout=5, decode_responses=True)
        client.ping()
        return client
    except Exception as e:
        logger.error('Redis connection failed: %s', e)
        return None


def _get_today_spend_cents(client) -> int:
    """Read today's AI cost counter from Redis (key: mcd:ai:cost:today:YYYY-MM-DD)."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    key = f'mcd:ai:cost:today:{today}'
    try:
        val = client.get(key)
        return int(val) if val else 0
    except Exception as e:
        logger.warning('Could not read spend counter: %s', e)
        return 0


def _daily_cap_cents() -> int:
    raw = os.environ.get('AI_GLOBAL_DAILY_USD_CAP', '30')
    try:
        return max(1, int(round(float(raw) * 100)))
    except (TypeError, ValueError):
        return 30 * 100


def _already_alerted(client, level: str) -> bool:
    """Return True if we already sent this level of alert today."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    key = f'{_DEDUP_KEY_PREFIX}{today}:{level}'
    return bool(client.get(key))


def _mark_alerted(client, level: str) -> None:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    key = f'{_DEDUP_KEY_PREFIX}{today}:{level}'
    client.set(key, '1', ex=_DEDUP_TTL)


def _send_ntfy(title: str, body: str, priority: str = '3') -> bool:
    # No default — see morning_engine/config.py (NTFY_CARRAC_TOPIC).
    topic = os.environ.get('NTFY_CARRAC_TOPIC', '')
    if not topic:
        return False
    # Say it out loud. This runs via `docker exec carrac`, so it
    # inherits the CONTAINER's environment, not the crontab header — a
    # different supply path from the bare-host scripts, and one that changes
    # only on `up -d --force-recreate`.
    if not NTFY_BASE:
        print('WARN: NTFY_BASE unset — cost alert NOT sent',
              file=sys.stderr)
        return False
    try:
        import requests
        r = requests.post(
            f'{NTFY_BASE}/{topic}',
            data=body.encode('utf-8'),
            headers={'Title': title, 'Priority': priority, 'Tags': 'warning,money'},
            timeout=8,
        )
        return 200 <= r.status_code < 300
    except Exception as e:
        logger.warning('ntfy send failed: %s', e)
        return False


def _send_telegram(text: str) -> bool:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token or not chat_id:
        return False
    try:
        import requests
        r = requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'},
            timeout=10,
        )
        return 200 <= r.status_code < 300
    except Exception as e:
        # The request URL carries the bot token and requests copies it into
        # the exception text, so log the exception type only.
        logger.warning('Telegram send failed: %s', type(e).__name__)
        return False


def run(client=None) -> int:
    """Run the cost-alert check. Returns 0 on success, 1 on error.

    `client` may be injected for testing (bypasses Redis connection).
    """
    if client is None:
        client = _redis_client()
    if client is None:
        return 1

    cap = _daily_cap_cents()
    spent = _get_today_spend_cents(client)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    spent_usd = spent / 100.0
    cap_usd = cap / 100.0
    pct = spent / cap if cap > 0 else 0.0

    logger.info(
        'AI spend today (%s): $%.2f / $%.2f (%.0f%%)',
        today, spent_usd, cap_usd, pct * 100,
    )

    # ALERT tier (83%) — checked first; if spend is at ALERT level, skip WARN
    # (ALERT supersedes WARN — no double-notify).
    if pct >= _ALERT_FRAC:
        if not _already_alerted(client, 'ALERT'):
            title = 'Carrac AI cost ALERT'
            body = (
                f'AI spend at {pct * 100:.0f}% of daily cap: '
                f'${spent_usd:.2f} / ${cap_usd:.2f}. '
                f'Cap will trigger at ${cap_usd:.2f}.'
            )
            logger.warning('ALERT threshold hit — notifying')
            # Telegram is the fallback, not a second copy.
            if not _send_ntfy(title, body, priority='4'):
                _send_telegram(f'*{title}*\n{body}')
            _mark_alerted(client, 'ALERT')
        else:
            logger.info('ALERT already sent today — skipping')
        # At ALERT level, WARN is intentionally skipped (ALERT is higher severity)
        return 0

    # WARN tier (50%) — only reached when spend is below ALERT threshold
    if pct >= _WARN_FRAC:
        if not _already_alerted(client, 'WARN'):
            title = 'Carrac AI cost warning'
            body = (
                f'AI spend at {pct * 100:.0f}% of daily cap: '
                f'${spent_usd:.2f} / ${cap_usd:.2f}.'
            )
            logger.info('WARN threshold hit — notifying')
            # Telegram is the fallback, not a second copy.
            if not _send_ntfy(title, body, priority='3'):
                _send_telegram(f'*{title}*\n{body}')
            _mark_alerted(client, 'WARN')
        else:
            logger.info('WARN already sent today — skipping')

    return 0


if __name__ == '__main__':
    # Load .env if present (a container started by compose already has it in its environment)
    _env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    if os.path.isfile(_env_file):
        with open(_env_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith('#') and '=' in _line:
                    _k, _, _v = _line.partition('=')
                    os.environ.setdefault(_k.strip(), _v.strip())

    sys.exit(run())
