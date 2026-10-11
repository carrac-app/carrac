"""ntfy sender — the single requests.post path shared by push_notifications.py,
morning_engine/engine.py, and quant/collectors/base.py.

Keeps the surface minimal and forgiving: all optional headers are kwargs,
network failures log a warning and return False (callers historically
swallowed exceptions anyway — we just centralise that).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger('carrac.notifications.ntfy')

# Env-required, no default: the base URL depends on where ntfy runs for
# this deployment, so it comes from the environment. Point it at an ntfy
# instance reachable from wherever this process runs (the public ntfy.sh works
# only if that network can reach it; many hosts and containers cannot).
#
# This is the OPERATOR channel only — user-supplied webhook URLs go through the
# allowlisted path in carrac/alerts/routes.py and are untouched by this.
#
# Read at import, like the rest of this module's config. `docker restart` does
# not reload .env; a change needs `up -d --force-recreate`.
NTFY_BASE = os.environ.get('NTFY_BASE', '').rstrip('/')

# One line per process when the operator channel is not configured, then
# quiet. Returning False in silence would hide the misconfiguration; once per
# process, not per call, because an unconfigured box would otherwise log one
# line per alert forever. WARNING, not ERROR: leaving an optional channel
# unconfigured is a legitimate setup. A configured channel that fails to
# deliver is the error case and is reported by the sender.
_UNCONFIGURED_REPORTED = False


def _report_unconfigured() -> None:
    global _UNCONFIGURED_REPORTED
    if _UNCONFIGURED_REPORTED:
        logger.debug('ntfy still unconfigured; message dropped')
        return
    _UNCONFIGURED_REPORTED = True
    logger.warning(
        'NTFY_BASE is not set, so operator alerts from this process '
        '(outage pushes, venue-coverage and delisting alarms, source '
        'regressions, deploy pings) are dropped. Set NTFY_BASE to an ntfy '
        'instance this process can reach to receive them. This message is '
        'logged once per process.'
    )


def _header_value(s: str) -> str:
    """Make a string safe to send as an HTTP header value.

    HTTP/1.1 headers are latin-1 only — a title like "⚡ Carrac data source
    alert" makes requests/urllib3 raise UnicodeEncodeError and the alert is
    silently lost (a circuit-breaker OPEN push never arrives). ntfy decodes RFC 2047 in header values server-side, so
    encode anything non-latin-1 as =?UTF-8?B?...?= and pass ASCII through
    untouched.
    """
    try:
        s.encode('latin-1')
        return s
    except UnicodeEncodeError:
        import base64
        return '=?UTF-8?B?' + base64.b64encode(s.encode('utf-8')).decode('ascii') + '?='


# Which operator alarms are loud enough to also go to Telegram.
#
# A phone reasonably keeps the operator ntfy topic muted, because the
# same topic carries waitlist signups and beta feedback. That made a TradFi
# double outage exactly as quiet as someone joining the waitlist. Telegram is
# the designated urgent tier, so priority>=4 alarms are
# mirrored there as well as published to ntfy.
#
# Deliberately NOT every alert. Mirroring priority 2 and 3 would rebuild the
# same undifferentiated stream on the channel that is supposed to stay
# meaningful — the same lesson, one channel over.
_URGENT_PRIORITIES = frozenset({'4', '5', 'high', 'urgent', 'max'})


def _is_urgent_priority(priority) -> bool:
    return str(priority or '').strip().lower() in _URGENT_PRIORITIES


def _mirror_to_telegram(title: Optional[str], body: str) -> bool:
    """Send an urgent operator alarm to Telegram as well as ntfy.

    parse_mode is None on purpose. Alarm bodies carry symbol lists, exception
    text and file paths — none of it written with Telegram's Markdown in mind,
    and an unmatched bracket would turn the alarm into a 400. A plain-text
    alarm that arrives beats a formatted one that does not.
    """
    try:
        from carrac.notifications.telegram import send_telegram_message
    except Exception as e:
        logger.warning('telegram mirror unavailable: %s', e)
        return False
    text = f'{title}\n\n{body}' if title else body
    try:
        return bool(send_telegram_message(text, parse_mode=None))
    except Exception as e:
        # Must never break the ntfy leg: the mirror is redundancy, not a
        # dependency.
        logger.warning('telegram mirror failed: %s', e)
        return False


def send_ntfy(
    body: str,
    title: Optional[str] = None,
    topic: Optional[str] = None,
    priority: Optional[str] = None,
    tags: Optional[str] = None,
    attach: Optional[str] = None,
    filename: Optional[str] = None,
    timeout: int = 5,
) -> bool:
    """Post a message to ntfy. Returns True if the alarm reached a human.

    topic: if None, falls back to env NTFY_CARRAC_TOPIC — this is Carrac's
    sender, so an unrouted message is a Carrac message. It deliberately does
    NOT fall back to NTFY_TOPIC (the infrastructure alert stream): a silent
    reroute would undo the split.
    Returns False (no-op) when absent.

    A priority>=4 alarm is ALSO mirrored to the operator Telegram
    channel, and the return value means "did this reach anyone" rather than
    "did ntfy accept it" — with two channels, ntfy failing while Telegram
    delivered is a success.
    The mirror runs even when ntfy is unconfigured or unreachable.
    """
    urgent = _is_urgent_priority(priority)

    try:
        import requests
    except ImportError:
        logger.warning('requests not installed; ntfy disabled')
        return _mirror_to_telegram(title, body) if urgent else False

    if not NTFY_BASE:
        _report_unconfigured()
        return _mirror_to_telegram(title, body) if urgent else False

    t = topic or os.environ.get('NTFY_CARRAC_TOPIC')
    if not t:
        return _mirror_to_telegram(title, body) if urgent else False

    headers = {}
    if title:
        headers['Title'] = _header_value(title)
    if priority:
        headers['Priority'] = str(priority)
    if tags:
        headers['Tags'] = _header_value(tags)
    if attach:
        headers['Attach'] = attach
    if filename:
        headers['Filename'] = _header_value(filename)

    ntfy_ok = False
    try:
        r = requests.post(
            f'{NTFY_BASE}/{t}',
            data=body.encode('utf-8') if isinstance(body, str) else body,
            headers=headers,
            timeout=timeout,
        )
        ntfy_ok = 200 <= r.status_code < 300
        if not ntfy_ok:
            logger.warning('ntfy publish rejected: HTTP %s', r.status_code)
    except Exception as e:
        logger.warning(f'ntfy publish failed: {e}')

    if urgent:
        tg_ok = _mirror_to_telegram(title, body)
        if not (ntfy_ok or tg_ok):
            # Both legs of the urgent channel are down. Nothing else can report
            # this, so it goes to the one place left.
            logger.error(
                'URGENT operator alarm reached NOBODY (ntfy and telegram both '
                'failed): %s', title or body[:80])
        return ntfy_ok or tg_ok

    return ntfy_ok
