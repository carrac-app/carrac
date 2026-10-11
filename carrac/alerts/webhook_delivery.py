"""User-defined webhook delivery on alert fire.

v1: one webhook endpoint per user, fires for every alert type (price,
trendline, indicator, formula, zscore, regime transition, derivatives,
chain-strength, anomaly cluster, conditional-query, plays). All trigger
paths route through this module's ``send_webhook_for_alert`` — see the
call sites in push_notifications.py (_send_user_notifications,
_deliver_bundle), carrac/indicator_state.py, and carrac/plays/monitor.py.

Security (non-negotiable, reused rather than reinvented):
  - HTTPS only, enforced by security.validate_outbound_url (the same
    SSRF defence used for the ntfy/custom-RSS-feed
    outbound paths): private/loopback/link-local/reserved/multicast/
    cloud-metadata ranges rejected via a resolve-once-and-pin check that
    defeats DNS rebinding.
  - The resolved IP is dialled directly (never a second DNS lookup) with
    the original hostname sent as the Host header + TLS SNI override, so
    a rebind between validation and request can't redirect the request —
    mirrors carrac/news/routes.py::_fetch_feed_pinned exactly.
  - allow_redirects=False — a 3xx is never followed (redirect-defeats-SSRF
    is a classic SSRF bypass).
  - 5s timeout, up to MAX_RETRIES retries with exponential backoff.
  - After MAX_CONSECUTIVE_FAILURES consecutive failures the webhook is
    auto-disabled and the user is notified via whatever OTHER channel
    (ntfy/Discord/Telegram/push) they already have configured.

Tier gating: reads TIER_LIMITS via ``limits.get('webhooks', False)`` — a
closed default, so the feature fails dark if the knob is ever removed.
The knob ships in carrac/auth/decorators.py TIER_LIMITS:
False for free, True for paid tiers. See get_tier_limits_for_user.

Secret storage: the signing secret is symmetric-encrypted at rest via
carrac.secret_box, the shared Fernet helper (one module and one key
env var for every secret stored at rest).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Tuple
from urllib.parse import quote, urlparse, urlunparse

from security import validate_outbound_url
from carrac.secret_box import decrypt_token

logger = logging.getLogger('carrac.alerts.webhook_delivery')

# ── Tunables (spec: 5s timeout, 3 retries w/ backoff, 10-failure auto-disable) ──
TIMEOUT_SECONDS = 5
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
MAX_CONSECUTIVE_FAILURES = 10

# Non-symbol placeholder "symbols" used by some quant alert types (see
# push_notifications.check_quant_alerts) — a deep link to these makes no
# sense, so fall back to the bare app URL for them.
_GENERIC_SYMBOLS = {'CHAIN', 'ANOMALY', 'CONDITION', 'REGIME', 'DERIV'}


# ============================================================
# Secret generation / signing
# ============================================================

def generate_webhook_secret() -> str:
    """Server-generated signing secret — 64 hex chars (32 bytes)."""
    return secrets.token_hex(32)


def sign_payload(secret: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of the raw JSON body, per the X-Carrac-Signature spec."""
    if not secret:
        return ''
    return hmac.new(secret.encode('utf-8'), body, hashlib.sha256).hexdigest()


def mask_secret(secret: str) -> str:
    """Mask a plaintext secret for display (never send the raw value back)."""
    if not secret:
        return ''
    if len(secret) <= 8:
        return '****'
    return f'{secret[:4]}…{secret[-4:]}'


def build_webhook_payload(alert_id, alert_type: str, symbol: str,
                          condition: str, price) -> dict:
    """Build the JSON payload delivered to the user's webhook endpoint."""
    triggered_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {
        'alert_id': alert_id,
        'alert_type': alert_type or 'alert',
        'symbol': symbol or '',
        'condition': condition or '',
        'price': price or 0,
        'triggered_at': triggered_at,
        'link': _build_alert_link(symbol),
    }


def _build_alert_link(symbol: Optional[str]) -> str:
    from carrac.site_url import site_base_url
    base = site_base_url()
    sym = (symbol or '').strip().upper()
    if sym and sym not in _GENERIC_SYMBOLS:
        return f'{base}/?symbol={quote(sym)}'
    return base


# ============================================================
# Dispatch entry points
# ============================================================

def _resolve_uid(user_id) -> Optional[int]:
    try:
        from push_notifications import _resolve_user_id
        return _resolve_user_id(str(user_id))
    except Exception:
        try:
            return int(user_id)
        except (TypeError, ValueError):
            return None


_DELIVERY_MAX_WORKERS = 4
_delivery_executor = None
_delivery_executor_lock = threading.Lock()


def _get_delivery_executor():
    """Bounded, long-lived delivery pool.

    One-shot threads each strand a pooled DB connection forever (UserDB
    caches one connection per thread and only releases it on same-thread
    reuse); long-lived workers cap that at _DELIVERY_MAX_WORKERS and
    bound concurrent deliveries during alert storms.
    """
    global _delivery_executor
    if _delivery_executor is None:
        with _delivery_executor_lock:
            if _delivery_executor is None:
                from concurrent.futures import ThreadPoolExecutor
                _delivery_executor = ThreadPoolExecutor(
                    max_workers=_DELIVERY_MAX_WORKERS,
                    thread_name_prefix='webhook-delivery',
                )
    return _delivery_executor


def send_webhook_for_alert(user_id, *, alert_id=None, alert_type: str = 'alert',
                           symbol: str = '', condition: str = '', price=0,
                           prefs=None) -> None:
    """Fire-and-forget webhook dispatch for a triggered alert.

    Safe to call UNCONDITIONALLY from any alert-trigger path. Callers that
    already hold the user's preferences dict should pass ``prefs`` so the
    evaluator thread pays no extra DB read; otherwise one prefs lookup
    runs synchronously here. Tier gate / SSRF validation happen inside
    _deliver_webhook. Network I/O — including retries/backoff — runs on a
    bounded delivery pool, so a slow or unreachable target can never
    block the alert-evaluation loop that triggered it.
    """
    uid = _resolve_uid(user_id)
    if uid is None:
        return

    if prefs is None:
        from user_db import user_db
        try:
            prefs = user_db.get_preferences(uid)
        except Exception as e:
            logger.debug(f'webhook dispatch: could not load prefs for user {uid}: {e}')
            return
    webhook_cfg = (prefs.get('notifications') or {}).get('webhook') or {}
    if not (webhook_cfg.get('enabled') and webhook_cfg.get('url')):
        return  # not configured — nothing submitted

    _get_delivery_executor().submit(
        _deliver_webhook, uid, alert_id, alert_type, symbol, condition, price,
    )


def send_test_webhook(user_id) -> Tuple[bool, Optional[str]]:
    """Synchronous test-fire for the settings-page 'Send test webhook' button.

    Returns (ok, error_or_None). Deliberately does NOT touch the persisted
    consecutive-failure counter or last-delivery status — a failed test
    while the user is still wiring up their endpoint shouldn't count
    towards auto-disable.
    """
    uid = _resolve_uid(user_id)
    if uid is None:
        return False, 'Invalid user'
    return _deliver_webhook(
        uid, alert_id=None, alert_type='test', symbol='TEST',
        condition='Test webhook from Carrac', price=0, is_test=True,
    )


def _deliver_webhook(uid: int, alert_id, alert_type: str, symbol: str,
                     condition: str, price, is_test: bool = False) -> Tuple[bool, Optional[str]]:
    """Core synchronous delivery (validate → sign → POST w/ retries).

    Called on a background thread for real alert fires, and synchronously
    (is_test=True) for the settings-page test button.
    """
    from user_db import user_db

    try:
        prefs = user_db.get_preferences(uid)
    except Exception as e:
        logger.warning(f'webhook dispatch: could not load prefs for user {uid}: {e}')
        return False, 'Could not load preferences'

    notif = prefs.get('notifications') or {}
    webhook_cfg = notif.get('webhook') or {}

    if not is_test and not (webhook_cfg.get('enabled') and webhook_cfg.get('url')):
        return False, None  # not configured / disabled — silent no-op

    url = (webhook_cfg.get('url') or '').strip()
    if not url:
        return False, 'No webhook URL configured'

    # Tier gate — closed default (see module docstring). Looked up per-user
    # (not per-session) since this runs from background loops with no Flask
    # request context.
    try:
        user_row = user_db.get_user_by_id(uid)
        tier = (user_row or {}).get('tier') or 'free'
        from carrac.auth.decorators import get_tier_limits_for_user
        limits = get_tier_limits_for_user(uid, tier)
    except Exception as e:
        logger.debug(f'webhook dispatch: tier lookup failed for user {uid}: {e}')
        limits = {}
    if not limits.get('webhooks', False):
        logger.debug(f'webhook dispatch: user {uid} tier does not include webhooks — skipping')
        return False, 'Webhooks require a paid plan'

    # SSRF re-validation at dispatch time (defense-in-depth): a URL saved
    # before a future tightening of validate_outbound_url, or a DNS record
    # that changed after save, must not get a free pass.
    ok, info = validate_outbound_url(url, allowed_schemes=('https',))
    if not ok:
        logger.warning(f'webhook URL for user {uid} rejected at dispatch: {info}')
        if not is_test:
            _record_delivery(uid, False, f'URL rejected: {info}')
        return False, f'URL rejected: {info}'
    pinned_ip = info

    secret = decrypt_token(webhook_cfg.get('secret') or '')
    if not secret:
        # Never send unsigned: an undecryptable secret (rotated/missing
        # Fernet key) must fail loudly, not silently drop the HMAC.
        if not is_test:
            _record_delivery(uid, False, 'signing unavailable — regenerate the webhook secret')
        return False, 'Webhook secret cannot be read — regenerate it in Settings'
    payload = build_webhook_payload(alert_id, alert_type, symbol, condition, price)
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
    signature = sign_payload(secret, body)

    attempts = 1 if is_test else (1 + MAX_RETRIES)
    backoff = INITIAL_BACKOFF_SECONDS
    last_error = 'Unknown error'
    for attempt in range(attempts):
        try:
            resp = _post_pinned(url, pinned_ip, body, signature)
            status = getattr(resp, 'status_code', None)
            if status is not None and 200 <= status < 300:
                if not is_test:
                    _record_delivery(uid, True, None)
                return True, None
            last_error = f'HTTP {status}'
        except Exception as e:
            last_error = str(e)[:200]
        if attempt < attempts - 1:
            time.sleep(backoff)
            backoff *= 2

    if not is_test:
        _record_delivery(uid, False, last_error)
    return False, last_error


def _post_pinned(url: str, pinned_ip: str, body: bytes, signature: str):
    """POST to a pre-resolved (pinned) IP — never a second DNS lookup.

    Mirrors carrac/news/routes.py::_fetch_feed_pinned: dial the pinned IP
    directly, send the original hostname as Host + TLS SNI so certificate
    validation still checks the real domain, and never follow redirects.
    """
    import requests as req
    from requests.adapters import HTTPAdapter

    parsed = urlparse(url)
    original_host = parsed.hostname
    port = parsed.port or 443
    netloc_pinned = f'[{pinned_ip}]:{port}' if ':' in pinned_ip else f'{pinned_ip}:{port}'
    pinned_url = urlunparse((
        parsed.scheme,
        netloc_pinned,
        parsed.path or '/',
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))

    headers = {
        'Content-Type': 'application/json',
        'X-Carrac-Signature': signature,
        'User-Agent': 'Carrac-Webhook/1.0',
        'Host': original_host,
    }

    class _PinnedSNIAdapter(HTTPAdapter):
        # server_hostname/assert_hostname are urllib3 POOL kwargs (SNI +
        # cert-hostname verification against the real domain while the
        # socket dials the pinned IP) — they are NOT HTTPAdapter.send()
        # kwargs; routing them through send() raises TypeError before any
        # socket opens.
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            pool_kwargs['server_hostname'] = original_host
            pool_kwargs['assert_hostname'] = original_host
            return super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

    session = req.Session()
    session.mount('https://', _PinnedSNIAdapter())
    try:
        return session.post(
            pinned_url,
            data=body,
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
            verify=True,
        )
    finally:
        session.close()


# ============================================================
# Delivery-status bookkeeping + auto-disable
# ============================================================

def _record_delivery(uid: int, success: bool, error: Optional[str]) -> None:
    """Persist last-delivery status/timestamp + consecutive-failure counter.

    Re-reads prefs immediately before writing (same read-modify-write
    pattern used throughout push_notifications.py / user_db — no new race
    introduced) so a concurrent settings save isn't blindly clobbered.
    """
    from user_db import user_db

    try:
        prefs = user_db.get_preferences(uid)
    except Exception as e:
        logger.warning(f'webhook: could not reload prefs to record delivery for user {uid}: {e}')
        return

    notif = prefs.setdefault('notifications', {})
    webhook_cfg = notif.setdefault('webhook', {})
    webhook_cfg['last_delivery_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    auto_disabled = False
    if success:
        webhook_cfg['last_delivery_status'] = 'success'
        webhook_cfg['consecutive_failures'] = 0
        webhook_cfg.pop('last_delivery_error', None)
    else:
        webhook_cfg['last_delivery_status'] = 'failed'
        webhook_cfg['last_delivery_error'] = (error or '')[:200]
        fails = int(webhook_cfg.get('consecutive_failures', 0) or 0) + 1
        webhook_cfg['consecutive_failures'] = fails
        if fails >= MAX_CONSECUTIVE_FAILURES and webhook_cfg.get('enabled'):
            webhook_cfg['enabled'] = False
            auto_disabled = True

    try:
        user_db.save_preferences(uid, prefs)
    except Exception as e:
        logger.error('webhook: could not save delivery status for user %s: %s', uid, e)

    if auto_disabled:
        _notify_autodisabled(uid, error)


def _notify_autodisabled(uid: int, error: Optional[str]) -> None:
    """Tell the user their webhook was auto-disabled, via an EXISTING channel.

    Reuses the same senders push_notifications already has for ntfy/
    Discord/Telegram/web-push rather than inventing a new delivery path.
    """
    try:
        from user_db import user_db
        from push_notifications import (
            send_push_notification, _send_ntfy_to_url, _send_discord_alert,
            _send_telegram_to_user, _decrypt_tg_token,
        )

        prefs = user_db.get_preferences(uid)
        notif = prefs.get('notifications') or {}
        title = 'Webhook auto-disabled'
        body = (
            f'Your alert webhook was disabled after {MAX_CONSECUTIVE_FAILURES} '
            f'consecutive delivery failures (last error: {(error or "unknown")[:150]}). '
            f'Fix your endpoint and re-enable it in Settings → Notifications.'
        )

        try:
            send_push_notification(user_id=str(uid), title=title, body=body, tag='webhook-disabled')
        except Exception:
            pass

        # An administrative notice, so routine urgency — and its
        # own title. Reusing the price-alert senders' hardcoded
        # '{symbol} Price Alert' title would show "Webhook Price Alert" on the
        # phone, at the same priority as a stop being hit.
        ntfy_cfg = notif.get('ntfy', {})
        if ntfy_cfg.get('enabled') and ntfy_cfg.get('url'):
            _send_ntfy_to_url(ntfy_cfg['url'], 'Webhook', body, user_id=str(uid),
                              title=title, tags='warning', priority='3')

        discord_cfg = notif.get('discord', {})
        if discord_cfg.get('enabled') and discord_cfg.get('webhook_url'):
            _send_discord_alert(discord_cfg['webhook_url'], 'Webhook', body,
                                user_id=str(uid), title=title)

        tg_cfg = notif.get('telegram', {})
        if tg_cfg.get('enabled') and tg_cfg.get('bot_token') and tg_cfg.get('chat_id'):
            _send_telegram_to_user(
                _decrypt_tg_token(tg_cfg['bot_token']), tg_cfg['chat_id'], 'Webhook',
                0, 0, 'disabled', body, user_id=str(uid), urgent=False,
            )
    except Exception as e:
        logger.warning(f'Failed to notify user {uid} of webhook auto-disable: {e}')
