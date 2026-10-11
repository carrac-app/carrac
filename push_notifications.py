"""
Push Notification Service for Carrac
Handles push subscriptions, price alerts, and sending notifications.
"""

from carrac.site_url import site_base_url
import os
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List

from carrac.alerts import push_store
from carrac.alerts import store as alert_store

logger = logging.getLogger('push_notifications')


def _capture_exception(exc) -> None:
    """Beacon an exception to the Sentry-compatible error tracker, but never itself raise or block.

    No-op when the SDK is uninitialised (no SENTRY_DSN). Mirrors the guarded
    helpers in carrac/integrations.py and carrac/crm_reconcile.py so a failing
    ntfy/Discord/Telegram/push delivery lands in error tracking instead of
    vanishing into a swallowed log line.
    """
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def _decrypt_tg_token(stored_token: str) -> str:
    """Decrypt Fernet-encrypted Telegram bot token before use.

    Falls back to plaintext (legacy) tokens gracefully. Never raises.
    """
    try:
        from carrac.secret_box import decrypt_token
        return decrypt_token(stored_token)
    except Exception as exc:
        logger.warning('[SECURITY] telegram token decrypt failed: %s', exc)
        return stored_token or ''


# Data directory for persistent storage
DATA_DIR = Path(os.environ.get('DATA_DIR', 'data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)

ALERT_EVENTS_FILE = DATA_DIR / 'alert_events.jsonl'

# Bundle window — multiple triggers within N seconds get digested into one push.
# Flushed from the precompute loop (see _flush_pending_bundles below).

# VAPID keys from environment
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
VAPID_SUBJECT = os.getenv('VAPID_SUBJECT', 'mailto:admin@example.com')


# ── push subscriptions (carrac/alerts/push_store.py) ───────────────────────

def add_subscription(user_id: str, subscription_info: dict) -> dict:
    """Make ``subscription_info`` the user's push subscription; return what was stored.

    Raises ``push_store.InvalidSubscription`` (a ValueError) for a subscription
    that may not be sent to, ValueError for an unknown user, and the database
    error for a failed write.
    """
    owner = _resolve_user_id(str(user_id))
    if owner is None:
        raise ValueError(f'no user matches {user_id!r}')
    stored = push_store.upsert(owner, subscription_info)
    logger.info(f"Push subscription added for user {owner}")
    return stored


def remove_subscription(user_id: str) -> bool:
    """Remove the user's push subscription; False when there was none."""
    owner = _resolve_user_id(str(user_id))
    if owner is None:
        return False
    removed = push_store.delete(owner)
    if removed:
        logger.info(f"Push subscription removed for user {owner}")
    return removed


def get_user_subscription(user_id: str) -> Optional[dict]:
    """The user's stored push subscription (endpoint and keys), or None."""
    owner = _resolve_user_id(str(user_id))
    return push_store.get(owner) if owner is not None else None


def _resolve_admin_uid() -> Optional[int]:
    """Resolve the 'admin' string user_id to the real numeric user ID."""
    try:
        from carrac.db_pool import adapt_query, get_conn, get_db_type
        admin_email = os.getenv('ADMIN_EMAIL', '')
        if not admin_email:
            return None
        if get_db_type() == 'postgresql':
            with get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('SELECT id FROM users WHERE email = ? LIMIT 1'),
                               (admin_email.lower(),))
                row = cursor.fetchone()
                return row['id'] if row else None
        # SQLite: user_db uses a separate file (users.db) — must use thread-local conn.
        from user_db import user_db
        conn = user_db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(adapt_query('SELECT id FROM users WHERE email = ? LIMIT 1'),
                           (admin_email.lower(),))
            row = cursor.fetchone()
            conn.commit()
            return row['id'] if row else None
        except Exception:
            try: conn.rollback()
            except Exception: pass
            raise
    except Exception:
        return None


def _resolve_user_id(user_id: str) -> Optional[int]:
    """Convert user_id string to numeric ID. Handles 'admin' → real ID."""
    if not user_id:
        return None
    if user_id == 'admin':
        return _resolve_admin_uid()
    try:
        return int(user_id)
    except (ValueError, TypeError):
        return None


def user_has_notification_channel(user_id: str) -> bool:
    """Check if a user has at least one notification channel configured.

    Checks: web push subscription, ntfy, discord, telegram.
    """
    uid = _resolve_user_id(user_id)
    if uid is None:
        return False

    if push_store.get(uid):
        return True

    # Check configured notification channels in user prefs
    try:
        from user_db import user_db
        prefs = user_db.get_preferences(uid)
    except Exception:
        return False

    notif = prefs.get('notifications', {})
    if notif.get('ntfy', {}).get('enabled') and notif.get('ntfy', {}).get('url'):
        return True
    if notif.get('discord', {}).get('enabled') and notif.get('discord', {}).get('webhook_url'):
        return True
    tg = notif.get('telegram', {})
    if tg.get('enabled') and tg.get('bot_token') and tg.get('chat_id'):
        return True

    return False


def create_alert(user_id: str, symbol: str, condition: str, price: float,
                 note: str = '', frequency: str = 'once_per_bar',
                 journal_id: int = None, auto_close: str = None,
                 drawing_id: int = None) -> dict:
    """
    Create a price alert.

    Args:
        user_id: User ID
        symbol: Asset symbol (e.g., 'BTC', 'ETH')
        condition: 'above' or 'below'
        price: Target price
        note: Optional note/description
        frequency: 'once' or 'once_per_bar' (fires once per hourly bar)
        journal_id: Optional linked journal entry ID (for auto-close)
        auto_close: Optional auto-close reason ('tp_hit' or 'sl_hit')
        drawing_id: Optional linked drawing ID (so list_drawing_alerts can
            surface horizontal_line/zone alerts spawned via alert_create_from_drawing)

    Returns:
        The created alert dict
    """
    alert = {
        'user_id': user_id,
        'symbol': symbol.upper(),
        'condition': condition,
        'price': float(price),
        'note': note,
        'frequency': frequency if frequency in ('once', 'once_per_bar') else 'once_per_bar',
        'last_triggered_bar': None,
        'triggered': False,
        'triggered_at': None,
        'active': True
    }
    if journal_id is not None:
        alert['journal_id'] = int(journal_id)
    if auto_close in ('tp_hit', 'sl_hit'):
        alert['auto_close'] = auto_close
    if drawing_id is not None:
        alert['drawing_id'] = str(drawing_id)

    alert = insert_alert(alert)
    logger.info(f"Alert created: {symbol} {condition} ${price} ({frequency}) for user {user_id}")
    return alert


def create_trendline_alert(user_id: str, symbol: str, drawing_id: str,
                           p1_ts: float, p1_val: float, p2_ts: float, p2_val: float,
                           note: str = '', frequency: str = 'once') -> dict:
    """
    Create a trendline break alert. Triggers when price crosses the extrapolated line.

    Args:
        user_id: User ID
        symbol: Asset symbol
        drawing_id: ID of the trendline drawing
        p1_ts: First point timestamp (ms)
        p1_val: First point price value
        p2_ts: Second point timestamp (ms)
        p2_val: Second point price value
        note: Optional note
        frequency: 'once' or 'once_per_bar'
    """
    # Compute initial side from current price
    slope = (p2_val - p1_val) / (p2_ts - p1_ts) if p2_ts != p1_ts else 0
    now_ms = datetime.now().timestamp() * 1000
    line_value = p1_val + slope * (now_ms - p1_ts)

    # Try to get current price to determine initial side
    initial_side = 'above'  # default
    try:
        from carrac.precompute import response_cache
        sym = symbol.upper()
        prices = response_cache.get('combined_crypto_prices') or {}
        current_price = prices.get(sym, {}).get('price')
        if current_price and current_price > 0:
            initial_side = 'above' if current_price > line_value else 'below'
    except Exception:
        pass

    alert = {
        'user_id': user_id,
        'symbol': symbol.upper(),
        'alert_type': 'trendline_break',
        'drawing_id': drawing_id,
        'p1_ts': float(p1_ts),
        'p1_val': float(p1_val),
        'p2_ts': float(p2_ts),
        'p2_val': float(p2_val),
        'initial_side': initial_side,
        'note': note,
        'frequency': frequency if frequency in ('once', 'once_per_bar') else 'once',
        'last_triggered_bar': None,
        'triggered': False,
        'triggered_at': None,
        'active': True
    }

    alert = insert_alert(alert)
    logger.info(f"Trendline alert created: {symbol} drawing={drawing_id} initial_side={initial_side} for user {user_id}")
    return alert


def _alert_owner(user_id) -> Optional[int]:
    """The numeric owner id for an alert ``user_id`` ('admin' resolves too)."""
    return _resolve_user_id(str(user_id)) if user_id is not None else None


def insert_alert(alert: dict) -> dict:
    """Store a new alert of any kind and return it as stored.

    Assigns the id. Raises ValueError when ``user_id`` matches no user, and
    lets a failed write raise: a caller must never report an alert as created
    when it was not stored.
    """
    owner = _alert_owner(alert.get('user_id'))
    if owner is None:
        raise ValueError(f"no user matches alert user_id {alert.get('user_id')!r}")
    return alert_store.insert(dict(alert, id=alert_store.new_alert_id(), user_id=str(owner)))


def get_user_alerts(user_id: str, active_only: bool = True) -> List[dict]:
    """Get all alerts for a user."""
    owner = _alert_owner(user_id)
    if owner is None:
        return []
    return alert_store.list_alerts(owner, active_only=active_only)


def update_alert(user_id: str, alert_id: int, updates: dict) -> Optional[dict]:
    """Update editable fields on an alert. Returns the updated alert or None."""
    owner = _alert_owner(user_id)
    if owner is None:
        return None

    def _apply(alert):
        for key, val in updates.items():
            if key == 'price':
                alert['price'] = float(val)
            elif key == 'note':
                alert['note'] = str(val)[:200]
            elif key == 'frequency':
                if val in ('once', 'once_per_bar'):
                    alert['frequency'] = val
                    # Reset trigger state when switching frequency
                    alert['last_triggered_bar'] = None
                    alert['triggered'] = False
                    alert['triggered_at'] = None
            elif key == 'condition':
                if val in ('above', 'below'):
                    alert['condition'] = val

    alert = alert_store.modify(alert_id, _apply, owner=owner)
    if alert is not None:
        logger.info(f"Alert {alert_id} updated for user {user_id}: {list(updates.keys())}")
    return alert


def delete_alert(user_id: str, alert_id: int) -> bool:
    """Delete an alert by ID."""
    owner = _alert_owner(user_id)
    if owner is None or not alert_store.delete(alert_id, owner):
        return False
    logger.info(f"Alert {alert_id} deleted for user {user_id}")
    return True


def delete_alerts_bulk(user_id: str, alert_ids: list) -> tuple:
    """Delete multiple alerts at once, ownership-checked.

    Returns (deleted_ids, skipped_ids). skipped_ids covers IDs that were not
    found OR belonged to a different user.
    """
    if not alert_ids:
        return [], []
    id_set = {int(i) for i in alert_ids}
    owner = _alert_owner(user_id)
    deleted_ids = alert_store.delete_many(id_set, owner) if owner is not None else []
    skipped_ids = sorted(id_set - set(deleted_ids))
    if deleted_ids:
        logger.info(
            f"Bulk-deleted {len(deleted_ids)} alert(s) for user {user_id}: {deleted_ids}"
        )
    return deleted_ids, skipped_ids


def toggle_alert(user_id: str, alert_id: int) -> Optional[dict]:
    """Toggle an alert's active status."""
    owner = _alert_owner(user_id)
    if owner is None:
        return None

    def _apply(alert):
        alert['active'] = not alert['active']

    return alert_store.modify(alert_id, _apply, owner=owner)


def reactivate_alert(user_id: str, alert_id: int) -> Optional[dict]:
    """Reset a triggered 'once' alert so it can fire again."""
    owner = _alert_owner(user_id)
    if owner is None:
        return None

    def _apply(alert):
        alert['triggered'] = False
        alert['triggered_at'] = None
        alert['last_triggered_bar'] = None
        alert['active'] = True

    alert = alert_store.modify(alert_id, _apply, owner=owner)
    if alert is not None:
        logger.info(f"Alert {alert_id} reactivated for user {user_id}")
    return alert


def send_push_notification(user_id: str, title: str, body: str,
                          symbol: str = None, url: str = '/',
                          tag: str = 'price-alert') -> bool:
    """
    Send a push notification to a user.

    Args:
        user_id: User ID
        title: Notification title
        body: Notification body text
        symbol: Optional symbol for navigation
        url: URL to open on click
        tag: Notification tag for grouping

    Returns:
        True if notification was sent successfully
    """
    try:
        from pywebpush import webpush
    except ImportError:
        logger.error("pywebpush not installed")
        return False

    if not VAPID_PRIVATE_KEY:
        logger.error("VAPID keys not configured")
        return False

    stored = get_user_subscription(user_id)
    if not stored:
        logger.warning(f"No push subscription for user {user_id}")
        return False
    # Checked again at every send, not only at subscribe time: a row stored
    # before the check existed must not reach pywebpush either.
    try:
        subscription = push_store.clean(stored)
    except push_store.InvalidSubscription as e:
        logger.warning(f"Push to user {user_id} refused: {e}")
        return False

    payload = json.dumps({
        'title': title,
        'body': body,
        'symbol': symbol,
        'url': url,
        'tag': tag,
        'icon': '/static/icons/icon-192.png'
    })

    def _do_send():
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={'sub': VAPID_SUBJECT}
        )
        return True

    try:
        from carrac.notifications.resilience import deliver
    except Exception:
        deliver = None

    if deliver:
        ok = deliver(
            'web_push', user_id, _do_send,
            payload={'title': title, 'body': body, 'symbol': symbol, 'tag': tag},
        )
    else:
        try:
            ok = _do_send()
        except Exception as e:
            logger.error(f"Error sending push notification: {e}")
            if 'expired' in str(e).lower() or '410' in str(e):
                remove_subscription(user_id)
            return False

    if ok:
        logger.info(f"Push notification sent to user {user_id}: {title}")
        return True

    return False


# The kinds check_alerts evaluates against live prices; a price alert has no
# alert_type key and is stored as 'price'.
PRICE_ALERT_TYPES = ('price', 'trendline_break')

# The kinds check_quant_alerts evaluates nightly.
QUANT_ALERT_TYPES = ('zscore', 'regime_transition', 'derivatives_regime',
                     'derivatives_metric', 'chain_strength_breach',
                     'anomaly_cluster_active', 'conditional_query_match')


def check_alerts(prices: Dict[str, float]) -> List[dict]:
    """
    Check all active alerts against current prices and send notifications.
    Supports 'once' (trigger and deactivate) and 'once_per_bar' (trigger once
    per hourly bar, then rearm).

    Only the alerts that fired are written back, and only the fields this run
    changed on them: an alert the user creates, edits or deletes while the
    notifications go out keeps that change.

    Args:
        prices: Dict mapping symbol to current price

    Returns:
        List of triggered alerts
    """
    alerts = alert_store.list_alerts(active_only=True, alert_types=PRICE_ALERT_TYPES)
    baseline = alert_store.snapshot(alerts)
    triggered = []
    current_bar = datetime.now().replace(minute=0, second=0, microsecond=0).isoformat()

    for alert in alerts:
        if not alert.get('active', True):
            continue

        frequency = alert.get('frequency', 'once')
        symbol = alert.get('symbol')
        if not symbol:
            continue

        # For 'once' alerts, skip if already triggered
        if frequency == 'once':
            if alert.get('triggered'):
                continue
            # re-armed by _rearm_undelivered because the fire
            # reached nobody. `once` alerts do not otherwise consult
            # last_triggered_bar, so without this the retry would run on every
            # ~60s tick instead of once an hour.
            if (alert.get('undelivered_at')
                    and alert.get('last_triggered_bar') == current_bar):
                continue

        # For 'once_per_bar', skip if already triggered this bar
        if frequency == 'once_per_bar' and alert.get('last_triggered_bar') == current_bar:
            continue

        if symbol not in prices:
            continue

        current_price = prices[symbol]
        should_trigger = False
        title = ''
        body = ''
        direction = ''
        notify_price = 0

        if alert.get('alert_type') == 'trendline_break':
            # Diagonal trendline alert
            p1_ts = alert.get('p1_ts', 0)
            p1_val = alert.get('p1_val', 0)
            p2_ts = alert.get('p2_ts', 0)
            p2_val = alert.get('p2_val', 0)
            if p2_ts == p1_ts:
                continue
            slope = (p2_val - p1_val) / (p2_ts - p1_ts)
            now_ms = datetime.now().timestamp() * 1000
            line_value = p1_val + slope * (now_ms - p1_ts)
            if line_value <= 0:
                continue  # Trendline extrapolated to negative — skip
            current_side = 'above' if current_price > line_value else 'below'
            if current_side != alert.get('initial_side'):
                should_trigger = True
                direction = f"broke {'above' if current_side == 'above' else 'below'}"
                notify_price = line_value
                title = f"{symbol} Trendline Break"
                body = f"{symbol} {direction} trendline at ${line_value:,.2f} (now ${current_price:,.2f})"
                if alert.get('note'):
                    body += f"\n{alert['note']}"
        else:
            # Standard price alert
            target_price = alert.get('price', 0)
            condition = alert.get('condition', '')
            if condition == 'above' and current_price >= target_price:
                should_trigger = True
            elif condition == 'below' and current_price <= target_price:
                should_trigger = True
            if should_trigger:
                direction = "above" if condition == "above" else "below"
                notify_price = target_price
                title = f"{symbol} Price Alert"
                body = f"{symbol} is now ${current_price:,.2f} ({direction} ${target_price:,.2f})"
                if alert.get('note'):
                    body += f"\n{alert['note']}"

        if should_trigger:
            if frequency == 'once':
                alert['triggered'] = True
                alert['triggered_at'] = datetime.now().isoformat()
                alert['triggered_price'] = current_price
            elif frequency == 'once_per_bar':
                alert['last_triggered_bar'] = current_bar
                alert['triggered_at'] = datetime.now().isoformat()
                alert['triggered_price'] = current_price
                # Keep active — will rearm on next bar

            triggered.append(alert)

            # Auto-close journal entry if this is a TP/SL alert
            journal_id = alert.get('journal_id')
            auto_close = alert.get('auto_close')
            if journal_id and auto_close:
                try:
                    from carrac.content_db import get_content_db
                    db = get_content_db()
                    db.log_trade_close(
                        journal_id=int(journal_id),
                        exit_price=current_price,
                        exit_reason=auto_close,
                    )
                    # Deactivate both TP and SL alerts for this journal entry
                    for other in alerts:
                        if other.get('journal_id') == journal_id and other['id'] != alert['id']:
                            other['active'] = False
                    alert['active'] = False
                    logger.info(f"Auto-closed journal {journal_id} ({auto_close}) at ${current_price:,.2f}")
                except Exception as e:
                    logger.error(f"Auto-close journal {journal_id} failed: {e}")

            # Capture event for future efficacy analysis
            capture_alert_event(alert, current_price=current_price)

            # Stash for per-tick bundling instead of immediate send
            alert.setdefault('_pending_notification', {}).update({
                'user_id': alert.get('user_id', ''),
                'symbol': symbol,
                'title': title,
                'body': body,
                'direction': direction,
                'target_price': notify_price,
                'current_price': current_price,
                'note': alert.get('note'),
                'alert_id': alert['id'],
                # Webhook payload alert_type — a linked drawing_id
                # means this originated as a horizontal_line/zone alert even
                # though it flows through the same 'standard price alert'
                # branch above; trendline_break already has its own type.
                'alert_type': alert.get('alert_type') or ('trendline' if alert.get('drawing_id') else 'price'),
            })

    if triggered:
        # Persist the triggered state FIRST so it survives even if dispatch
        # raises (build_context_chip can throw) — an alert must not re-fire.
        alert_store.save_changes(alerts, baseline)
        logger.info(f"Triggered {len(triggered)} price alerts")
        outcome = _dispatch_bundle_or_direct(triggered)
        _rearm_undelivered(triggered, outcome, current_bar)
        alert_store.save_changes(triggered, baseline)

    return triggered


def _rearm_undelivered(triggered: list, outcome: Dict, current_bar: str) -> int:
    """Un-consume any alert that fired but reached nobody. Returns the count.

    Persisting the trigger before dispatch is right — it stops a
    crash mid-send from re-firing the alert forever. But it also meant a
    `frequency: 'once'` alert that fired while every channel was down was
    marked triggered and never fired again, and the user was never told.

    THE THROTTLE MATTERS. Naively re-arming would retry on the next ~60s
    precompute tick, so a multi-day channel outage against a still-true
    condition would mean ~1,440 failed sends a day. Stamping `last_triggered_bar`
    holds the retry to the next hourly bar — the alert stays alive, at 24
    attempts a day instead of 1,440. `once_per_bar` alerts already work exactly
    this way, so this is the existing throttle, not a new mechanism.
    """
    if not outcome:
        return 0
    rearmed = 0
    for alert in triggered:
        if outcome.get(alert.get('id')) is not False:
            # Delivered (or unknown) — clear any stale re-arm marker so the
            # alert does not stay throttled after the channel recovers.
            alert.pop('undelivered_at', None)
            continue
        if alert.get('frequency', 'once') == 'once':
            alert['triggered'] = False
            alert['last_triggered_bar'] = current_bar
            alert['undelivered_at'] = datetime.now().isoformat()
            rearmed += 1
    if rearmed:
        logger.warning(
            '%d alert(s) fired but reached NO channel — re-armed '
            'for the next bar rather than consumed', rearmed)
    return rearmed


# ── Page someone when a channel stops delivering ──────────────
#
# Without this, a channel that stops delivering is noticed only when someone
# notices the silence; the one other signal is a red line on the Settings page
# that nobody is paged about.
#
# THE TRIGGER IS PER-CHANNEL, NOT PER-USER, and that is a deliberate departure
# from a streak where EVERY configured channel fails. That condition would miss
# the common case: an account with ntfy and telegram dead for weeks while
# discord still works is never "total", so never alarmed. Per-channel catches
# it, and routing over whatever still works only makes sense that way.
#
# KNOWN BLIND SPOT, by construction: a user with no channels configured has
# nothing attempted, so nothing reaches the DLQ and nothing can be alarmed.
# That happens when every channel was disabled after an outage, which makes the
# account quiet rather than working. It is a separate problem this alarm cannot see.
OUTAGE_WINDOW_HOURS = int(os.getenv('DELIVERY_OUTAGE_WINDOW_HOURS', '24'))
# Alert volume is a property of how many alerts a user set, not of how bad the
# outage is: a quiet account's dead channels produce only a few failures a day.
# A threshold tuned for a busy account would never fire for a quiet one, so this
# is low on purpose and the once-per-outage latch stops it becoming noise.
OUTAGE_MIN_FAILURES = int(os.getenv('DELIVERY_OUTAGE_MIN_FAILURES', '3'))

_OUTAGE_CHANNELS = ('ntfy', 'discord', 'telegram')


def _configured_channels(notif: dict) -> set:
    """Channels this user has both ENABLED and given credentials for.

    Mirrors the enablement tests in _send_user_notifications exactly — if the
    two ever disagree, this alarm reports on a channel that is never tried, or
    stays silent about one that is. Deliberately excludes the webhook (a
    machine integration) and the email digest (queued, not delivered), for the
    same reason `delivered` does.
    """
    out = set()
    if not notif:
        return out
    ntfy_cfg = notif.get('ntfy') or {}
    if ntfy_cfg.get('enabled') and ntfy_cfg.get('url'):
        out.add('ntfy')
    discord_cfg = notif.get('discord') or {}
    if discord_cfg.get('enabled') and discord_cfg.get('webhook_url'):
        out.add('discord')
    tg_cfg = notif.get('telegram') or {}
    if tg_cfg.get('enabled') and tg_cfg.get('bot_token') and tg_cfg.get('chat_id'):
        out.add('telegram')
    return out


def _send_outage_alarm_on(channel: str, notif: dict, uid, title: str, body: str) -> bool:
    """Deliver the outage alarm over one specific working channel."""
    cfg = notif.get(channel) or {}
    if channel == 'ntfy':
        return _send_ntfy_to_url(cfg['url'], 'Carrac', body, user_id=str(uid),
                                 title=title, tags='warning', priority='5')
    if channel == 'discord':
        return _send_discord_alert(cfg['webhook_url'], 'Carrac', body,
                                   user_id=str(uid), title=title)
    if channel == 'telegram':
        return _send_telegram_to_user(
            _decrypt_tg_token(cfg['bot_token']), cfg['chat_id'], 'Carrac',
            0, 0, '', user_id=str(uid), text=f'{title}\n\n{body}', urgent=True)
    return False


_NO_CHANNELS_KEY = '__nochannels__'


def _alarm_no_channels(user_id) -> list:
    """Operator alarm: this user has alerts firing and nowhere to send them."""
    from carrac.notifications.resilience import channel_health, mark_outage_alarmed
    if (channel_health(str(user_id)).get(_NO_CHANNELS_KEY) or {}).get('alarmed_at'):
        return []          # once per outage
    try:
        from carrac.notifications.ntfy import send_ntfy
        reached = send_ntfy(
            f'user {user_id} has an alert firing with NO notification channel '
            f'enabled. Every fire is discarded silently and leaves no DLQ '
            f'record — disabling a broken channel makes it quiet, not fixed.',
            title='Carrac: a user has alerts and no channels',
            priority='5', tags='rotating_light')
        if not reached:
            logger.error('no-channels alarm for user %s reached NOBODY',
                         user_id)
    except Exception:
        logger.exception('no-channels alarm failed for user %s', user_id)
    mark_outage_alarmed(user_id, [_NO_CHANNELS_KEY])
    logger.warning('user %s has alerts firing and no channels configured',
                   user_id)
    return [_NO_CHANNELS_KEY]


def _clear_no_channels_latch(user_id) -> None:
    from carrac.notifications.resilience import clear_outage_latch
    clear_outage_latch(user_id, _NO_CHANNELS_KEY)


def check_delivery_outage(user_id, notif: dict) -> list:
    """Raise ONE alarm per outage for each channel that stopped delivering.

    Returns the channels alarmed about (empty list = nothing to say), so the
    caller and the tests can see what it decided rather than inferring it from
    a side effect.
    """
    from carrac.notifications.resilience import (
        channel_health, mark_outage_alarmed, undelivered_summary,
    )
    configured = _configured_channels(notif)
    if not configured:
        # ZERO CHANNELS IS THE DEGENERATE CASE OF EVERY CHANNEL BEING DEAD, and
        # it is worse, because nothing is attempted so nothing reaches the DLQ
        # and there is no record anywhere that the user was not told.
        #
        # Not hypothetical. Observed in a live deployment: an account had a BTC
        # once_per_bar alert that fired every hour for weeks. Its
        # 775 ntfy failures then stopped — not because delivery was fixed
        # but because the channel was DISABLED, which made it quiet
        # rather than working. Every fire since has gone nowhere, silently.
        #
        # Operator-only: there is by definition no way to reach this user.
        return _alarm_no_channels(user_id)

    # The user has channels again — drop the no-channels latch so a future
    # lapse can alarm again.
    _clear_no_channels_latch(user_id)

    health = channel_health(str(user_id))
    summary = undelivered_summary(str(user_id), since_hours=OUTAGE_WINDOW_HOURS)

    broken, healthy = set(), set()
    for ch in configured:
        h = health.get(ch)
        if not h:
            # No durable record either way. NOT healthy — silence is not health.
            continue
        if h.get('broken'):
            broken.add(ch)
        elif h.get('last_success'):
            healthy.add(ch)

    fresh = sorted(
        ch for ch in broken
        if (summary.get(ch) or {}).get('count', 0) >= OUTAGE_MIN_FAILURES
        and not (health.get(ch) or {}).get('alarmed_at')
    )
    if not fresh:
        return []

    lines = []
    for ch in fresh:
        info = summary.get(ch) or {}
        why = info.get('last_error') or info.get('last_reason') or 'no reason recorded'
        lines.append(f"{ch}: {info.get('count', 0)} alert(s) not delivered in the "
                     f"last {OUTAGE_WINDOW_HOURS}h — {why}")
    title = 'Carrac: alerts are not reaching you'
    body = ('\n'.join(lines) +
            '\n\nFix it under Settings > Notifications. '
            'Alerts that reached nobody are re-armed, not lost.')

    delivered_on = []
    for ch in sorted(healthy):
        try:
            if _send_outage_alarm_on(ch, notif, user_id, title, body):
                delivered_on.append(ch)
        except Exception:
            logger.exception('outage alarm failed on channel %s', ch)

    if not delivered_on:
        # Every channel this user has is down — the circular case. Escalate to
        # the OPERATOR channel, where priority>=4 also mirrors to the operator
        # Telegram (carrac/notifications/ntfy.py). This is the
        # last-resort path, so the operator hears about it.
        try:
            from carrac.notifications.ntfy import send_ntfy
            reached = send_ntfy(
                f'user {user_id}\n' + body,
                title='Carrac: a user is receiving no alerts',
                priority='5', tags='rotating_light')
            if not reached:
                logger.error(
                    'delivery-outage alarm for user %s reached NOBODY '
                    '(channels %s down, operator channel also failed)',
                    user_id, ','.join(fresh))
        except Exception:
            logger.exception('operator escalation failed for user %s', user_id)

    mark_outage_alarmed(user_id, fresh)
    logger.warning('delivery outage alarm for user %s on %s (sent via %s)',
                   user_id, ','.join(fresh), ','.join(delivered_on) or 'operator channel')
    return fresh




def send_batched_position_warnings(user_id: str, items: list) -> bool:
    """Send a single batched notification with all position warnings for a user.

    Returns True if at least one channel accepted it.

    Args:
        user_id: User ID
        items: List of dicts with keys: symbol, direction, warnings, price, trade_id
    """
    if not items:
        return False

    # Build bullet-point list
    lines = []
    for item in items:
        sym = item['symbol']
        direction = item['direction'].upper()
        price = item['price']
        warns = '; '.join(item['warnings'])
        lines.append(f"{sym} {direction}: {warns} — ${price:,.2f}")

    count = len(items)
    title = f"Position Warnings ({count})"

    # Plain text for ntfy/Discord/push
    plain_body = '\n'.join(f"• {line}" for line in lines)

    # Web push — single notification
    try:
        send_push_notification(
            user_id=user_id, title=title, body=plain_body,
            tag='pos-batch'
        )
    except Exception as e:
        logger.debug(f"Batched push notification failed: {e}")

    # Route to user's configured channels
    uid = _resolve_user_id(user_id)
    if uid is None:
        logger.warning('batched warnings skipped: could not resolve user %r', user_id)
        return False

    try:
        from user_db import user_db
        prefs = user_db.get_preferences(uid)
    except Exception as e:
        # A prefs read failure must not fall through to the
        # no-channels `return None` below, which would make a transient DB blip
        # indistinguishable from "user wants no notifications". False =
        # attempted-and-failed, so the caller knows nothing reached anyone.
        logger.error('batched warnings: prefs read failed for user %s — '
                     'reporting delivery failure: %s', user_id, e)
        _capture_exception(e)
        return False

    notif = (prefs or {}).get('notifications')
    if not notif:
        return None

    # These go through the same senders as the single-alert path, so they share
    # its circuit-breaker, DLQ and response-status check. Raw `requests.post`
    # calls would bypass all three — a 401 would read exactly like a 200 and a
    # failed bundled alert would be invisible even to `undelivered_summary`.
    # Titles are passed explicitly so the digest keeps its own.
    delivered = False
    attempted = False

    ntfy_cfg = notif.get('ntfy', {})
    if ntfy_cfg.get('enabled') and ntfy_cfg.get('url'):
        attempted = True
        delivered |= _send_ntfy_to_url(ntfy_cfg['url'], 'MULTI', plain_body,
                                       user_id=str(uid), title=title,
                                       tags='warning', priority='3')

    discord_cfg = notif.get('discord', {})
    if discord_cfg.get('enabled') and discord_cfg.get('webhook_url'):
        attempted = True
        delivered |= _send_discord_alert(discord_cfg['webhook_url'], 'MULTI',
                                         plain_body, user_id=str(uid), title=title)

    # Telegram — markdown formatted
    tg_cfg = notif.get('telegram', {})
    if tg_cfg.get('enabled') and tg_cfg.get('bot_token') and tg_cfg.get('chat_id'):
        tg_lines = []
        for item in items:
            sym = item['symbol']
            direction = item['direction'].upper()
            price = item['price']
            warns = '; '.join(item['warnings'])
            tg_lines.append(f"• *{sym}* {direction}: {warns} — ${price:,.2f}")
        tg_body = f"*{title}*\n\n" + '\n'.join(tg_lines)
        attempted = True
        # Routine: a batched digest of position warnings is a review item,
        # not a reason to make the phone ring.
        delivered |= _send_telegram_to_user(
            _decrypt_tg_token(tg_cfg['bot_token']), tg_cfg['chat_id'], 'MULTI',
            0, 0, '', user_id=str(uid), text=tg_body, urgent=False)

    # Email digest
    if notif.get('email', {}).get('enabled'):
        queue_email_alert(uid, {
            'symbol': 'MULTI',
            'direction': 'position_warnings',
            'target_price': 0,
            'current_price': 0,
            'note': plain_body,
            'alert_type': 'indicator',
        })

    # None = no channel configured, not a failure. See _send_user_notifications.
    return delivered if attempted else None


def _send_user_notifications(user_id: str, symbol: str, direction: str,
                             target_price: float, current_price: float,
                             note: str = None, *, alert_id=None,
                             alert_type: str = None, condition: str = None,
                             webhook_price: float = None,
                             urgent: bool = None, title: str = None):
    """Route alert notifications to the user's configured channels.

    All users (including admin) use per-user notification preferences.
    Web push is handled separately by the caller.

    alert_id/alert_type/condition/webhook_price are optional
    metadata used only to build the webhook payload — every existing
    positional caller keeps working unchanged since these are keyword-only
    with safe fallbacks (alert_type defaults to a price/indicator guess,
    condition falls back to the human-readable `body`/`note` already built
    below). webhook_price exists because target_price/current_price double
    as the ntfy/Telegram text-format switch (0 means "indicator-style,
    note IS the message") — callers with a real current price but no price
    *target* (e.g. play transitions) pass it here instead of forcing
    target_price/current_price non-zero, which would also silently change
    the existing message format for those callers.

    Returns True if at least one channel accepted the message.
    Callers need this to know whether a triggered alert actually reached the
    user — see check_alerts, which must not consume a `once` alert when
    every channel dropped it.
    """
    uid = _resolve_user_id(user_id)
    if uid is None:
        # Returning silently would leave a resolution failure and a user with
        # no channels configured left identical (zero) evidence behind. The
        # warning is the fix; the verdict stays None because nothing was
        # attempted — we never got far enough to know if a channel exists.
        logger.warning('notification skipped: could not resolve user %r', user_id)
        return None

    try:
        from user_db import user_db
        prefs = user_db.get_preferences(uid)
    except Exception as e:
        # Falling through to the no-channels
        # `return None` below would make a transient DB blip read exactly like
        # "user has no channels configured" — and _rearm_undelivered()
        # only re-arms on False, so a `once` alert that fired during the
        # blip was consumed permanently. False = attempted-and-failed:
        # the alert re-arms on the next hourly bar (throttled).
        logger.error('alert delivery: prefs read failed for user %s — '
                     'reporting delivery failure so the alert re-arms: %s',
                     user_id, e)
        _capture_exception(e)
        return False

    notif = (prefs or {}).get('notifications')
    if not notif:
        return None

    if target_price == 0:
        # Indicator/position alert — note IS the message
        body = note or f"{symbol} {direction}"
    else:
        body = f"{symbol} {direction} ${target_price:,.2f}\nNow: ${current_price:,.2f}"
        if note:
            body += f"\n{note}"

    delivered = False
    attempted = False

    # Callers that know better pass `urgent` explicitly; otherwise
    # derive it from the alert type. Defaulting to the type keeps every
    # existing call site correct without touching it.
    if urgent is None:
        urgent = _is_urgent(alert_type if alert_type is not None
                            else ('indicator' if target_price == 0 else 'price'))

    # ntfy
    ntfy_cfg = notif.get('ntfy', {})
    if ntfy_cfg.get('enabled') and ntfy_cfg.get('url'):
        attempted = True
        delivered |= _send_ntfy_to_url(
            ntfy_cfg['url'], symbol, body, user_id=str(uid), title=title,
            tags='rotating_light' if urgent else 'chart_with_upwards_trend',
            priority='5' if urgent else '3')

    # Discord
    discord_cfg = notif.get('discord', {})
    if discord_cfg.get('enabled') and discord_cfg.get('webhook_url'):
        attempted = True
        delivered |= _send_discord_alert(discord_cfg['webhook_url'], symbol, body,
                                         user_id=str(uid), title=title)

    # Telegram
    tg_cfg = notif.get('telegram', {})
    if tg_cfg.get('enabled') and tg_cfg.get('bot_token') and tg_cfg.get('chat_id'):
        attempted = True
        delivered |= _send_telegram_to_user(
            _decrypt_tg_token(tg_cfg['bot_token']), tg_cfg['chat_id'], symbol,
            target_price, current_price, direction, note, user_id=str(uid),
            urgent=urgent)

    # User-defined webhook — no-ops cheaply if not configured,
    # not entitled (closed tier-gate default), or URL fails SSRF validation.
    try:
        from carrac.alerts.webhook_delivery import send_webhook_for_alert
        send_webhook_for_alert(
            uid,
            alert_id=alert_id,
            alert_type=alert_type or ('indicator' if target_price == 0 else 'price'),
            symbol=symbol,
            condition=condition if condition is not None else body,
            price=webhook_price if webhook_price is not None else (current_price or target_price or 0),
            prefs=prefs,
        )
    except Exception as e:
        logger.warning(f"webhook dispatch failed for user {user_id}: {e}")

    # Email digest (batched, not immediate)
    if notif.get('email', {}).get('enabled'):
        queue_email_alert(uid, {
            'symbol': symbol, 'direction': direction,
            'target_price': target_price, 'current_price': current_price,
            'note': note,
            'alert_type': 'indicator' if target_price == 0 else 'price',
        })

    # Deliberately excludes the webhook and the email digest: the webhook is a
    # machine integration, not the user's phone, and a queued digest has not
    # been delivered yet. `delivered` answers one question only — did a human
    # get pinged.
    #
    # THREE-VALUED, and the third value is the important one. None means no
    # channel was even attempted, i.e. the user has configured none — which is
    # NOT a delivery failure and must not re-arm the alert, or someone who
    # deliberately runs with notifications off would have every `once` alert
    # retry forever.
    # Evaluate channel health on every fire that attempted a send —
    # NOT only when the whole fire failed.
    # Runs even when nothing was ATTEMPTED: a user with no channels at all is
    # precisely the case that leaves no evidence behind.
    try:
        check_delivery_outage(uid, notif)
    except Exception:
        # Never let the alarm break the delivery it is watching. Loud, and to
        # the error tracker.
        logger.exception('delivery-outage check failed for user %s', uid)

    return delivered if attempted else None


def _ok_or_raise(r, channel: str) -> bool:
    """True on 2xx; otherwise raise carrying the status AND the body.

    ``resilience.deliver`` has two failure paths and only one of them is worth
    reading: an exception is recorded as ``f'{type(e).__name__}: {e}'``, while a
    send_fn that merely returns False is recorded as the literal string
    'send_fn returned False'. A sender that ends with a bare
    ``return 200 <= r.status_code < 300`` makes a NETWORK failure (requests
    raises) diagnosable and an API REJECTION useless, and the cause of the
    latter is unrecoverable once the worker logs rotate away. Raising with the
    status and a body excerpt keeps the cause in the alarm.

    Body is truncated and newline-flattened: ``last_error`` is stored at [:200]
    and one line per failure goes to the DLQ, so an HTML error page must not
    become the log.
    """
    if 200 <= r.status_code < 300:
        return True
    body = (getattr(r, 'text', '') or '')[:200].replace('\n', ' ').replace('\r', ' ')
    raise RuntimeError(f'{channel} HTTP {r.status_code}: {body}')


def _send_ntfy_to_url(url: str, symbol: str, body: str, user_id: str = '',
                      title: str = None, tags: str = 'chart_with_upwards_trend',
                      priority: str = '4') -> bool:
    """Send alert to a user-provided ntfy URL (through circuit-breaker).

    Returns whether the message actually went out, so callers can distinguish
    a delivered alert from a dropped one.

    `title` is a parameter because a hardcoded '{symbol} Price Alert' would make
    a regime-transition alert arrive on the phone as "REGIME Price Alert", a trendline break lose its
    own title, and the webhook-auto-disabled notice read "Webhook Price Alert".
    """
    try:
        from carrac.notifications.resilience import deliver
    except Exception:
        deliver = None

    def _do_send():
        import requests as req
        r = req.post(url, data=body.encode('utf-8'),
                     headers={
                         'Title': title or f'{symbol} Price Alert',
                         'Tags': tags,
                         'Priority': priority,
                     }, timeout=5, allow_redirects=False)
        return _ok_or_raise(r, 'ntfy')

    if deliver:
        return bool(deliver('ntfy', user_id or 'anon', _do_send,
                            payload={'url': url, 'symbol': symbol, 'body': body}))
    try:
        return bool(_do_send())
    except Exception as e:
        logger.warning(f"ntfy (user URL) alert failed: {e}")
        return False


def _send_discord_alert(webhook_url: str, symbol: str, body: str, user_id: str = '',
                        title: str = None) -> bool:
    """Send alert to a Discord webhook (through circuit-breaker). See
    _send_ntfy_to_url for why this returns a bool and takes a title
."""
    try:
        from carrac.notifications.resilience import deliver
    except Exception:
        deliver = None

    def _do_send():
        import requests as req
        r = req.post(webhook_url,
                     json={'content': f"**{title or f'{symbol} Price Alert'}**\n{body}"},
                     timeout=10, allow_redirects=False)
        return _ok_or_raise(r, 'discord')

    if deliver:
        return bool(deliver('discord', user_id or 'anon', _do_send,
                            payload={'webhook_url': webhook_url,
                                     'symbol': symbol, 'body': body}))
    try:
        return bool(_do_send())
    except Exception as e:
        logger.warning(f"Discord alert failed: {e}")
        return False


# Which alert kinds are allowed to make the phone ring.
# If everything arrives at the same urgency (ntfy Priority 4, same tag, same
# '{symbol} Price Alert' title) a price crossing is indistinguishable from a
# regime transition, a play invalidation, a morning report or a
# webhook-disabled notice, and real alerts get lost amid the noise.
_URGENT_ALERT_TYPES = frozenset({'price', 'trendline', 'trendline_break'})


def _is_urgent(alert_type: str) -> bool:
    """A price level being hit is time-critical; a regime shift is not."""
    return (alert_type or 'price') in _URGENT_ALERT_TYPES


def _tg_safe(fragment: str) -> str:
    """Neutralise Telegram-Markdown control characters in user-supplied text.

    Alert notes come from the user and from the chart — the drawn
    alert path writes the literal note `[drawn]`. Telegram's legacy Markdown
    reads `[` as the start of an inline link, so the message built for
    that alert is unparseable and the API answers 400. It never showed up as a
    distinct failure because the token was 401ing first; fixing the credential
    would have swapped one silent failure for another.

    Stripping rather than escaping: these fragments are prose, the characters
    carry no meaning here, and a backslash-escaped note reads worse on the
    phone than one without brackets. Our own structural markers (the bold
    title, the italic wrapper) are added outside this function and survive.
    """
    if not fragment:
        return ''
    return fragment.translate({ord(c): None for c in '[]*_`'})


def _send_telegram_to_user(bot_token: str, chat_id: str, symbol: str,
                            target_price: float, current_price: float,
                            direction: str, note: str = None,
                            user_id: str = '', text: str = None,
                            urgent: bool = False) -> bool:
    """Send alert via Telegram using the user's own bot token + chat ID
    (through circuit-breaker). See _send_ntfy_to_url for why this returns a
    bool.

    `text` overrides the composed message entirely, for callers that have
    already built their own (the batched-warnings digest)."""
    if text:
        pass
    elif target_price == 0:
        text = f"*{symbol} Position Warning*\n"
        if note:
            text += _tg_safe(note)
        else:
            text += f"{symbol} {direction}"
        if current_price:
            text += f"\nPrice: ${current_price:,.2f}"
    else:
        text = f"{'🔔 ' if urgent else ''}*{symbol} Price Alert*\n"
        text += f"{symbol} is now ${current_price:,.2f} ({direction} ${target_price:,.2f})"
        if note:
            text += f"\n_{_tg_safe(note)}_"

    try:
        from carrac.notifications.resilience import deliver
    except Exception:
        deliver = None

    def _do_send():
        import requests as req
        r = req.post(f'https://api.telegram.org/bot{bot_token}/sendMessage',
                     json={'chat_id': chat_id, 'text': text,
                           'parse_mode': 'Markdown',
                           # The urgent/routine split. Telegram has
                           # no priority header — a silent message is the whole
                           # mechanism. Routine traffic (digests, report-ready,
                           # play transitions, position warnings) arrives
                           # without sound or vibration, so a price alert is the
                           # only thing from Carrac that makes the phone ring.
                           'disable_notification': not urgent},
                     timeout=10)
        return _ok_or_raise(r, 'telegram')

    if deliver:
        return bool(deliver('telegram', user_id or 'anon', _do_send,
                            payload={'chat_id': chat_id, 'symbol': symbol,
                                     'text': text}))
    try:
        return bool(_do_send())
    except Exception as e:
        logger.warning(f"Telegram (user) alert failed: {e}")
        return False


def notify_user_report_ready(user_id, report_id: int = None):
    """Send 'your morning report is ready' notification to all user's configured channels.

    Thin wrapper around existing channel-sending functions, tailored for report
    notifications instead of price alerts.
    """
    uid = _resolve_user_id(str(user_id))
    if uid is None:
        return

    try:
        from user_db import user_db
        prefs = user_db.get_preferences(uid)
    except Exception as e:
        # Lower stakes than the alert path (the report
        # itself is in-app; only the courtesy ping is lost, and there is no
        # re-arm to protect), but the blip should still reach the error tracker.
        logger.warning(f"Could not load prefs for user {user_id}: {e}")
        _capture_exception(e)
        return

    notif = prefs.get('notifications', {})
    if not notif:
        return

    title = "Morning Report Ready"
    body = "Your personalized morning report is ready."
    if report_id:
        body += f"\n{site_base_url()}/report/morning"

    # ntfy
    ntfy_cfg = notif.get('ntfy', {})
    if ntfy_cfg.get('enabled') and ntfy_cfg.get('url'):
        try:
            import requests as req
            req.post(ntfy_cfg['url'], data=body.encode('utf-8'),
                     headers={
                         'Title': title,
                         'Tags': 'newspaper',
                         'Priority': '3',
                     }, timeout=5, allow_redirects=False)
        except Exception:
            # ERROR: the user expected this message and will never get it.
            logger.error("ntfy report notification failed for user %s", user_id,
                         exc_info=True)

    # Discord
    discord_cfg = notif.get('discord', {})
    if discord_cfg.get('enabled') and discord_cfg.get('webhook_url'):
        try:
            import requests as req
            req.post(discord_cfg['webhook_url'],
                     json={'content': f"**{title}**\n{body}"},
                     timeout=10, allow_redirects=False)
        except Exception as e:
            # ERROR as above. No traceback or exception text: the webhook URL
            # is a credential and requests embeds the URL in its errors.
            logger.error("Discord report notification failed for user %s (%s)",
                         user_id, type(e).__name__)

    # Telegram
    tg_cfg = notif.get('telegram', {})
    if tg_cfg.get('enabled') and tg_cfg.get('bot_token') and tg_cfg.get('chat_id'):
        try:
            import requests as req
            text = f"*{title}*\n{body}"
            _tg_tok = _decrypt_tg_token(tg_cfg['bot_token'])
            req.post(f"https://api.telegram.org/bot{_tg_tok}/sendMessage",
                     json={'chat_id': tg_cfg['chat_id'], 'text': text,
                           'parse_mode': 'Markdown'},
                     timeout=10)
        except Exception as e:
            # ERROR as above. No traceback or exception text: the bot token is
            # part of the request URL and requests embeds the URL in its errors.
            logger.error("Telegram report notification failed for user %s (%s)",
                         user_id, type(e).__name__)


# ============================================================
# Email Digest — batched alert notifications (30-min window)
# ============================================================

_email_buffer: Dict[int, list] = {}  # uid -> list of alert dicts
_email_buffer_lock = threading.Lock()
_EMAIL_DIGEST_INTERVAL = 30 * 60  # 30 minutes


def queue_email_alert(uid: int, alert_item: dict):
    """Add an alert to the email digest buffer for a user."""
    with _email_buffer_lock:
        _email_buffer.setdefault(uid, []).append(alert_item)


def flush_email_digests():
    """Send batched email digests and clear the buffer.

    Called periodically from the precompute loop (every 30 min).
    """
    with _email_buffer_lock:
        if not _email_buffer:
            return
        pending = dict(_email_buffer)
        _email_buffer.clear()

    from carrac.email import send_email
    from user_db import user_db

    for uid, items in pending.items():
        try:
            from carrac.db_pool import adapt_query, get_conn, get_db_type
            if get_db_type() == 'postgresql':
                with get_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute(adapt_query('SELECT email FROM users WHERE id = ?'), (uid,))
                    row = cursor.fetchone()
            else:
                # SQLite: user_db uses a separate file (users.db).
                _conn = user_db._get_conn()
                try:
                    cursor = _conn.cursor()
                    cursor.execute(adapt_query('SELECT email FROM users WHERE id = ?'), (uid,))
                    row = cursor.fetchone()
                    _conn.commit()
                except Exception:
                    try: _conn.rollback()
                    except Exception: pass
                    raise
            if not row:
                continue
            email_addr = row['email']
        except Exception as e:
            logger.warning(f"Email digest: could not get email for uid {uid}: {e}")
            continue

        # Build digest HTML
        subject = f"Carrac — {len(items)} alert{'s' if len(items) != 1 else ''} triggered"
        rows_html = ''
        for item in items:
            sym = item.get('symbol', '?')
            direction = item.get('direction', '')
            target = item.get('target_price', 0)
            current = item.get('current_price', 0)
            note = item.get('note', '')
            alert_type = item.get('alert_type', 'price')
            if alert_type == 'indicator':
                rows_html += (
                    f'<tr><td style="padding:6px 12px;border-bottom:1px solid #21262d">{sym}</td>'
                    f'<td style="padding:6px 12px;border-bottom:1px solid #21262d">{direction}</td>'
                    f'<td style="padding:6px 12px;border-bottom:1px solid #21262d;color:#8b949e">{note}</td></tr>'
                )
            else:
                rows_html += (
                    f'<tr><td style="padding:6px 12px;border-bottom:1px solid #21262d">{sym}</td>'
                    f'<td style="padding:6px 12px;border-bottom:1px solid #21262d">{direction} ${target:,.2f}</td>'
                    f'<td style="padding:6px 12px;border-bottom:1px solid #21262d">Now: ${current:,.2f}</td></tr>'
                )

        html_body = f'''<div style="font-family:monospace;background:#0d1117;color:#d1d4dc;padding:24px;border-radius:8px">
<h2 style="color:#e6edf3;font-size:16px;margin:0 0 16px">Alert Digest</h2>
<table style="width:100%;border-collapse:collapse;font-size:13px">
<tr style="color:#8b949e"><th style="text-align:left;padding:6px 12px;border-bottom:1px solid #30363d">Asset</th>
<th style="text-align:left;padding:6px 12px;border-bottom:1px solid #30363d">Trigger</th>
<th style="text-align:left;padding:6px 12px;border-bottom:1px solid #30363d">Details</th></tr>
{rows_html}
</table>
<p style="color:#8b949e;font-size:11px;margin-top:16px">Sent from <a href="{site_base_url()}" style="color:#58a6ff">Carrac</a> — change preferences in Settings &gt; Notifications</p>
</div>'''

        text_body = "Carrac Alert Digest\n\n"
        for item in items:
            sym = item.get('symbol', '?')
            direction = item.get('direction', '')
            target = item.get('target_price', 0)
            current = item.get('current_price', 0)
            if item.get('alert_type') == 'indicator':
                text_body += f"  {sym}: {direction}\n"
            else:
                text_body += f"  {sym}: {direction} ${target:,.2f} (now ${current:,.2f})\n"

        try:
            send_email(email_addr, subject, html_body, text_body)
            logger.info(f"Email digest sent to uid {uid}: {len(items)} alerts")
        except Exception as e:
            logger.warning(f"Email digest failed for uid {uid}: {e}")


def get_vapid_public_key() -> str:
    """Get the VAPID public key for client-side subscription."""
    return VAPID_PUBLIC_KEY


# ============================================================
# Alert trigger events — JSONL capture for future efficacy analysis.
# Every fire writes one line; a later worker can compute realized
# move at +1h / +24h. This file is the foundation; no measurement yet.
# ============================================================

def capture_alert_event(alert: dict, current_price: float = 0.0,
                        context: Optional[dict] = None) -> None:
    """Append a trigger event to data/alert_events.jsonl.

    Captures everything needed to compute efficacy later: alert id/type,
    user, asset, fire timestamp, price at fire, and a context snapshot
    (regime, top z-scores, derivatives regime) pulled from the latest
    QE signals. Never raises — telemetry must not break a notification.
    """
    try:
        ALERT_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ctx = context or _snapshot_quant_context()
        event = {
            'ts': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'alert_id': alert.get('id'),
            'alert_type': alert.get('alert_type', 'price'),
            'user_id': alert.get('user_id'),
            'symbol': alert.get('symbol') or alert.get('metric') or alert.get('chain') or alert.get('target', ''),
            'condition': alert.get('condition', ''),
            'threshold': alert.get('threshold') or alert.get('price'),
            'price_at_fire': float(current_price) if current_price else None,
            'frequency': alert.get('frequency', 'once'),
            'context': ctx,
        }
        with open(ALERT_EVENTS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
    except Exception as e:
        logger.error('alert event capture failed for alert %s: %s', alert.get('id'), e)


def _snapshot_quant_context() -> dict:
    """Pull latest regime + top-3 extreme z-scores + derivatives regime.

    Returns an empty dict on any failure — context is pure enrichment.
    """
    out: dict = {}
    try:
        from carrac.db_pool import get_quant_conn as get_conn
    except Exception:
        return out

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            # Regime label
            cur.execute("""
                SELECT metadata FROM quant.signals
                WHERE source = 'regime_hmm' AND metric = 'REGIME'
                ORDER BY ts DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row and row.get('metadata'):
                meta = row['metadata'] if isinstance(row['metadata'], dict) else json.loads(row['metadata'])
                out['regime'] = meta.get('label', 'unknown')

            # Top-3 extreme z-scores (abs value)
            cur.execute("""
                SELECT DISTINCT ON (metric) metric, zscore
                FROM quant.zscores
                WHERE window_days = 90
                ORDER BY metric, ts DESC
            """)
            rows = cur.fetchall() or []
            by_abs = sorted(
                ((r['metric'], float(r['zscore'])) for r in rows if r.get('zscore') is not None),
                key=lambda kv: abs(kv[1]),
                reverse=True,
            )[:3]
            if by_abs:
                out['top_zscores'] = {m: round(z, 2) for m, z in by_abs}

            # Latest derivatives regime snapshot if available
            try:
                cur.execute("""
                    SELECT basis_pct, skew_25d, iv_rv_premium
                    FROM quant.derivatives
                    WHERE asset = 'BTC'
                    ORDER BY ts DESC LIMIT 1
                """)
                drow = cur.fetchone()
                if drow:
                    out['derivatives'] = {
                        k: (float(v) if v is not None else None)
                        for k, v in drow.items()
                    }
            except Exception:
                pass
    except Exception:
        pass

    return out


def build_context_chip(context: Optional[dict] = None, user_id=None) -> str:
    """Render a one-line context chip for notification enrichment.

    Returns '' when no context is available. The chip is appended to the
    notification body so power users see regime + divergent metrics
    without opening the app.

    ``user_id`` is the RECIPIENT. Deribit-derived parts of the chip are shown
    only to the operator; everything else is unconditional.
    """
    ctx = context if context is not None else _snapshot_quant_context()
    if not ctx:
        return ''
    parts = []
    regime = ctx.get('regime')
    if regime and regime != 'unknown':
        parts.append(f"regime:{regime}")
    zs = ctx.get('top_zscores') or {}
    if zs:
        top = ', '.join(f"{m}={z:+.1f}σ" for m, z in list(zs.items())[:2])
        parts.append(top)
    # skew_25d is Deribit-barred, and this chip is concatenated
    # onto the BODY of every outgoing push/ntfy/Discord/Telegram message. The
    # chip fires at abs() >= 1.5, which real skew values reach, so it is gated
    # to admin.
    #
    # Per RECIPIENT, which is why user_id is a parameter: a chip built once per
    # dispatch and appended to everyone's message has no recipient to ask about.
    from carrac.compliance.deribit_gate import user_is_admin
    if user_is_admin(user_id):
        deriv = ctx.get('derivatives') or {}
        skew = deriv.get('skew_25d')
        if skew is not None and abs(skew) >= 1.5:
            parts.append(f"skew25d={skew:+.1f}")
    if not parts:
        return ''
    return '  •  ' + ' | '.join(parts)


# ============================================================
# Bundling — when one user has more than one trigger in a single tick,
# they are digested into one push/ntfy/Discord/Telegram message instead
# of sent individually. Entry point is _dispatch_bundle_or_direct(),
# called from check_alerts and check_quant_alerts.
# ============================================================

def _dispatch_bundle_or_direct(triggered: list) -> Dict:
    """Per-tick bundling: if a user has >1 trigger this tick, digest.
    Otherwise send direct with context-chip enrichment.

    Reads `_pending_notification` metadata stashed by the checker.

    Returns {alert_id: delivered_bool} so the caller can tell a
    delivered alert from a dropped one. Bundled deliveries report one verdict
    for every alert in the bundle, which is accurate — they go out as a single
    message that either arrives or does not.
    """
    outcome: Dict = {}
    by_user: Dict[str, list] = {}
    for a in triggered:
        pend = a.get('_pending_notification') or {}
        uid = str(pend.get('user_id') or a.get('user_id') or '')
        if not pend:
            continue
        by_user.setdefault(uid, []).append(pend)

    # RENDERED inside the loop now. The chip carries a
    # Deribit-derived part that is operator-only, so it has to be
    # per-recipient; hoisted above the loop there was no recipient to ask
    # about. The DB read stays hoisted — _snapshot_quant_context() is not
    # memoised, so calling it per user would turn one query into N.
    _chip_ctx = _snapshot_quant_context()

    for uid, items in by_user.items():
        chip = build_context_chip(_chip_ctx, user_id=uid)
        if len(items) == 1:
            it = items[0]
            body = it.get('body') or ''
            if chip:
                body = f"{body}{chip}"
            try:
                pushed = send_push_notification(
                    user_id=uid,
                    title=it.get('title', ''),
                    body=body,
                    symbol=it.get('symbol'),
                    tag=f"alert-{it.get('alert_id', '')}",
                )
            except Exception as e:
                # Not logger.debug: a failing web-push delivery would be
                # completely invisible. Surface it and beacon to the error tracker.
                logger.warning(f'push direct failed user={uid}: {e}')
                _capture_exception(e)
                pushed = False
            try:
                sent = _send_user_notifications(
                    uid, it.get('symbol', ''), it.get('direction', ''),
                    it.get('target_price', 0), it.get('current_price', 0),
                    note=(it.get('note') or '') + (chip if chip else ''),
                    alert_id=it.get('alert_id'), alert_type=it.get('alert_type'),
                    condition=it.get('body') or it.get('note'),
                    title=it.get('title') or None,
                )
            except Exception as e:
                # Not logger.debug: a failing ntfy/Discord/Telegram delivery would
                # be invisible, making "alert fired but no notification"
                # undiagnosable. Error-level + error-tracker beacon.
                logger.error(f'channel direct failed user={uid}: {e}')
                _capture_exception(e)
                sent = False
            # A web push that landed is a real ping too — the user saw it.
            # sent is None when the user has no channels configured; that is
            # not a failure, so it must not become False here.
            if pushed or sent is True:
                outcome[it.get('alert_id')] = True
            elif sent is False:
                outcome[it.get('alert_id')] = False
            else:
                outcome[it.get('alert_id')] = None
        else:
            try:
                sent = _deliver_bundle(uid, items)
            except Exception as e:
                logger.warning(f'bundle delivery failed user={uid}: {e}')
                _capture_exception(e)
                sent = False
            for it in items:
                outcome[it.get('alert_id')] = sent

    return outcome


def _deliver_bundle(user_id: str, items: list) -> bool:
    """Deliver a bundle as either a single digest or a lone notification.

    Returns whether it reached the user."""
    if not items:
        return False

    # per-recipient — this function already has the user.
    chip = build_context_chip(user_id=user_id)

    if len(items) == 1:
        # Singleton — send as normal, but attach the context chip.
        it = items[0]
        symbol = it.get('symbol', '')
        body = it.get('body') or ''
        if chip:
            body = f"{body}{chip}"
        pushed = send_push_notification(
            user_id=user_id,
            title=it.get('title', f'{symbol} Alert'),
            body=body,
            symbol=symbol,
            tag=f"alert-{it.get('alert_id', '')}",
        )
        sent = _send_user_notifications(
            user_id, symbol,
            it.get('direction', ''),
            it.get('target_price', 0),
            it.get('current_price', 0),
            note=body,
            alert_id=it.get('alert_id'), alert_type=it.get('alert_type'),
            condition=it.get('body') or it.get('note'),
            title=it.get('title') or None,
        )
        if pushed or sent is True:
            return True
        return False if sent is False else None

    # Multi-item digest — single push + single per-channel message.
    count = len(items)
    title = f'{count} alerts triggered'
    lines = []
    for it in items:
        sym = it.get('symbol', '?')
        direction = it.get('direction', '')
        target = it.get('target_price', 0)
        current = it.get('current_price', 0)
        if target:
            lines.append(f"{sym} {direction} ${target:,.2f} (now ${current:,.2f})")
        else:
            lines.append(f"{sym} — {direction or it.get('note') or 'triggered'}")
    plain_body = '\n'.join(f"• {line}" for line in lines)
    if chip:
        plain_body = f"{plain_body}{chip}"

    try:
        pushed = send_push_notification(
            user_id=user_id, title=title, body=plain_body,
            tag='alert-bundle',
        )
    except Exception as e:
        # Surface + beacon a failing bundled web-push.
        logger.warning(f"bundled push failed user={user_id}: {e}")
        _capture_exception(e)
        pushed = False

    # Route to per-channel destinations via the existing fan-out by
    # synthesising a `send_batched_position_warnings`-style call: we
    # already have a working template so reuse its delivery rather
    # than duplicating channel wiring.
    batch_items = []
    for it in items:
        batch_items.append({
            'symbol': it.get('symbol', '?'),
            'direction': it.get('direction', 'triggered'),
            'warnings': [it.get('note') or f"now ${it.get('current_price', 0):,.2f}"],
            'price': it.get('current_price', 0) or 0,
            'trade_id': it.get('alert_id'),
        })
    try:
        fanned = send_batched_position_warnings(user_id, batch_items)
    except Exception as e:
        logger.warning(f'bundle per-channel fanout failed user={user_id}: {e}')
        _capture_exception(e)
        fanned = False

    # send_batched_position_warnings fans out to ntfy/Discord/
    # Telegram separately — it does not go through _send_user_notifications,
    # so a multi-alert-per-tick bundle would otherwise never reach the webhook
    # dispatch seam. (It now uses the shared, breaker-backed
    # senders, but still does its own fan-out.) Webhook payloads are
    # per-alert (not batched — the receiving automation gets one JSON
    # object per fire), so dispatch once per underlying item here.
    for it in items:
        try:
            from carrac.alerts.webhook_delivery import send_webhook_for_alert
            send_webhook_for_alert(
                user_id,
                alert_id=it.get('alert_id'),
                alert_type=it.get('alert_type') or ('indicator' if not it.get('target_price') else 'price'),
                symbol=it.get('symbol', '?'),
                condition=it.get('body') or it.get('note') or '',
                price=it.get('current_price', 0) or it.get('target_price', 0) or 0,
            )
        except Exception as e:
            logger.warning(f"webhook dispatch failed (bundle) user={user_id} alert={it.get('alert_id')}: {e}")
            _capture_exception(e)

    if pushed or fanned is True:
        return True
    return False if fanned is False else None


# ─── Quant Engine Alerts (z-score + regime) ───────────────────────

def create_zscore_alert(user_id: str, metric: str, condition: str,
                        threshold: float, window: int = 90,
                        note: str = '', frequency: str = 'once') -> dict:
    """Create a z-score threshold alert (checked nightly after QE pipeline)."""
    alert = {
        'user_id': user_id,
        'alert_type': 'zscore',
        'metric': metric,
        'condition': condition,  # 'above' or 'below'
        'threshold': float(threshold),
        'window': int(window),
        'note': note[:200] if note else '',
        'frequency': frequency if frequency in ('once', 'recurring') else 'once',
        'triggered': False,
        'triggered_at': None,
        'active': True,
    }
    alert = insert_alert(alert)
    logger.info(f"Z-score alert created: {metric} {condition} {threshold}σ for user {user_id}")
    return alert


def create_regime_alert(user_id: str, note: str = '',
                        frequency: str = 'once') -> dict:
    """Create a regime transition alert (checked nightly after QE pipeline)."""
    alert = {
        'user_id': user_id,
        'alert_type': 'regime_transition',
        'note': note[:200] if note else '',
        'frequency': frequency if frequency in ('once', 'recurring') else 'once',
        'last_regime': None,  # populated on first check
        'triggered': False,
        'triggered_at': None,
        'active': True,
    }
    alert = insert_alert(alert)
    logger.info(f"Regime alert created for user {user_id}")
    return alert


def create_chain_strength_alert(user_id: str, chain: str,
                                condition: str = 'below',
                                threshold: float = 0.15,
                                frequency: str = 'once',
                                note: str = '') -> dict:
    """Create a transmission chain strength alert.

    Fires when a specific chain's geometric-mean strength crosses the
    configured threshold. Checked nightly after the leadlag engine writes
    chain_strength summaries.

    Args:
        chain: 'monetary_policy' | 'dollar_liquidity' | 'risk_appetite'
        condition: 'above' | 'below'
        threshold: chain strength scalar (typically 0.05-0.5)
    """
    if chain not in ('monetary_policy', 'dollar_liquidity', 'risk_appetite'):
        raise ValueError(f'unknown chain: {chain}')
    if condition not in ('above', 'below'):
        raise ValueError("condition must be 'above' or 'below'")
    alert = {
        'user_id': user_id,
        'alert_type': 'chain_strength_breach',
        'chain': chain,
        'condition': condition,
        'threshold': float(threshold),
        'note': (note or '')[:200],
        'frequency': frequency if frequency in ('once', 'recurring') else 'once',
        'triggered': False,
        'triggered_at': None,
        'active': True,
    }
    alert = insert_alert(alert)
    logger.info(f"Chain-strength alert created for user {user_id} on {chain}")
    return alert


def create_anomaly_cluster_alert(user_id: str, min_domains: int = 2,
                                 min_metrics: int = 3,
                                 frequency: str = 'once',
                                 note: str = '') -> dict:
    """Create an anomaly-cluster alert.

    Fires when an active multi-domain cluster with at least `min_domains`
    domains and `min_metrics` extreme metrics exists in quant.anomalies
    (domain='cluster', resolved_at IS NULL).
    """
    if not (1 <= int(min_domains) <= 6):
        raise ValueError('min_domains must be 1-6')
    if not (1 <= int(min_metrics) <= 20):
        raise ValueError('min_metrics must be 1-20')
    alert = {
        'user_id': user_id,
        'alert_type': 'anomaly_cluster_active',
        'min_domains': int(min_domains),
        'min_metrics': int(min_metrics),
        'note': (note or '')[:200],
        'frequency': frequency if frequency in ('once', 'recurring') else 'once',
        'triggered': False,
        'triggered_at': None,
        'active': True,
    }
    alert = insert_alert(alert)
    logger.info(f"Anomaly-cluster alert created for user {user_id} "
                f"(min_domains={min_domains}, min_metrics={min_metrics})")
    return alert


_DERIV_REGIME_NAMES = frozenset({
    'euphoric_meltup', 'complacent_bull', 'healthy_trend', 'pre_event_tension',
    'distribution_topping', 'sudden_correction', 'full_capitulation',
    'post_crash_stabilization', 'vol_compression', 'bear_grind',
    'short_squeeze_setup', 'structural_carry',
})


def create_derivatives_regime_alert(user_id: str,
                                    from_regime: str = None,
                                    to_regime: str = None,
                                    note: str = '',
                                    frequency: str = 'once') -> dict:
    """Create a derivatives-regime change alert.

    If from_regime and to_regime are both set, only fires on that exact
    transition. If only to_regime is set, fires whenever any regime flips
    to that target. If both are None, fires on any regime change (legacy).

    Args:
        from_regime: optional regime name to filter source state
        to_regime: optional regime name to filter target state
    """
    if from_regime and from_regime not in _DERIV_REGIME_NAMES:
        raise ValueError(f'unknown from_regime: {from_regime}')
    if to_regime and to_regime not in _DERIV_REGIME_NAMES:
        raise ValueError(f'unknown to_regime: {to_regime}')
    if from_regime and to_regime and from_regime == to_regime:
        raise ValueError('from_regime and to_regime must differ')

    alert = {
        'user_id': user_id,
        'alert_type': 'derivatives_regime',
        'from_regime': from_regime,
        'to_regime': to_regime,
        'note': (note or '')[:200],
        'frequency': frequency if frequency in ('once', 'recurring') else 'once',
        'last_deriv_regime': None,
        'triggered': False,
        'triggered_at': None,
        'active': True,
    }
    alert = insert_alert(alert)
    logger.info(
        f"Derivatives-regime alert created for user {user_id} "
        f"(from={from_regime or '*'} → to={to_regime or '*'})"
    )
    return alert


def create_conditional_alert(user_id: str, conditions: list,
                             target: str = 'BTC', horizon: int = 30,
                             frequency: str = 'recurring',
                             note: str = '') -> dict:
    """Create a conditional-query alert.

    Fires when the user's z-score conditions all hold on the latest day.
    Conditions is a list of strings like 'VIX:gt:2', matching the query
    builder format in /api/quant/conditional.
    """
    if not conditions or not isinstance(conditions, list) or len(conditions) > 5:
        raise ValueError('conditions must be a list of 1-5 items')
    # Sanity-parse each condition. Raises ValueError via _parse_condition
    # if anything is malformed — caller returns 400.
    try:
        from quant.engine.conditional import _parse_condition
        for c in conditions:
            _parse_condition(str(c))  # raises on an invalid condition
    except Exception as e:
        raise ValueError(f'invalid condition: {e}')

    alert = {
        'user_id': user_id,
        'alert_type': 'conditional_query_match',
        'conditions': [str(c) for c in conditions],
        'target': (target or 'BTC').upper(),
        'horizon': int(horizon) if horizon else 30,
        'note': (note or '')[:200],
        'frequency': frequency if frequency in ('once', 'recurring') else 'recurring',
        'last_match': None,  # populated on first check; avoids false first-fire
        'triggered': False,
        'triggered_at': None,
        'active': True,
    }
    alert = insert_alert(alert)
    logger.info(f"Conditional-query alert created for user {user_id}: "
                f"{conditions} → {target}")
    return alert


def check_quant_alerts(current_zscores: Dict[str, float],
                       current_regime: str) -> List[dict]:
    """Check z-score and regime alerts against current QE data.

    Called nightly from quant/check_alerts.py after QE pipeline completes.

    Args:
        current_zscores: {metric_name: zscore_value} from quant.zscores (window=90)
        current_regime: 'favorable' or 'unfavorable' from quant.signals
    """
    alerts = alert_store.list_alerts(active_only=True, alert_types=QUANT_ALERT_TYPES)
    baseline = alert_store.snapshot(alerts)
    triggered = []
    changed = False

    for alert in alerts:
        atype = alert.get('alert_type')

        fire = False

        if atype == 'zscore':
            metric = alert.get('metric', '')
            zscore = current_zscores.get(metric)
            if zscore is None:
                continue
            cond = alert.get('condition')
            threshold = alert.get('threshold', 2.0)
            above_now = (cond == 'above' and zscore >= threshold)
            below_now = (cond == 'below' and zscore <= -abs(threshold))
            breached_now = above_now or below_now
            # Edge-detect: fire only on false→true transition
            prev_breach = alert.get('last_breach')
            if prev_breach is None:
                # First evaluation — establish baseline, don't fire
                alert['last_breach'] = breached_now
                changed = True
                continue
            if breached_now and not prev_breach:
                fire = True
                alert['last_breach'] = True
                changed = True
            elif not breached_now and prev_breach:
                alert['last_breach'] = False
                changed = True

        elif atype == 'regime_transition':
            if alert.get('last_regime') is None:
                # First check — establish baseline, don't trigger
                alert['last_regime'] = current_regime
                changed = True
                continue
            if current_regime != alert['last_regime']:
                fire = True
                alert['last_regime'] = current_regime
                changed = True

        elif atype == 'derivatives_regime':
            # Alert on derivatives regime change
            try:
                from carrac.db_pool import get_quant_conn as get_conn
                # classify_asset_row owns the raw-column →
                # classifier-key mapping; raw names dropped three features
                # and made this alert disagree with quant_get_regime.
                from quant.engine.derivatives_regime import classify_asset_row
                with get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT basis_pct, skew_25d, dvol, iv_rv_premium,
                               options_put_call_ratio, term_structure_slope,
                               gex_net, gex_flip_price
                        FROM quant.derivatives WHERE asset = 'BTC'
                        ORDER BY ts DESC LIMIT 1
                    """)
                    row = cur.fetchone()
                    if row:
                        metrics = {k: float(v) for k, v in row.items() if v is not None}
                        result = classify_asset_row(metrics) or {}
                        current_deriv = result.get('regime', 'unknown')
                        if alert.get('last_deriv_regime') is None:
                            alert['last_deriv_regime'] = current_deriv
                            changed = True
                        elif current_deriv != alert['last_deriv_regime']:
                            prev = alert['last_deriv_regime']
                            from_filter = alert.get('from_regime')
                            to_filter = alert.get('to_regime')
                            # Edge-detect with optional filters
                            match = True
                            if from_filter and prev != from_filter:
                                match = False
                            if to_filter and current_deriv != to_filter:
                                match = False
                            if match:
                                fire = True
                            alert['last_deriv_regime'] = current_deriv
                            changed = True
            except Exception as e:
                # Was a bare `pass`. An exception here does not
                # skip one evaluation — it skips this alert TYPE for every
                # user on every cycle, silently and forever. Nothing else in
                # the system would ever mention it.
                logger.error(
                    '[alert-eval] %s failed for alert %s — this alert type will not fire until fixed: %s',
                    'derivatives_regime', alert.get('id'), e, exc_info=True)
                _capture_exception(e)

        elif atype == 'derivatives_metric':
            # Alert when a derivatives metric crosses a threshold.
            # Edge-detected: fires only on false→true transition.
            try:
                from carrac.db_pool import get_quant_conn as get_conn
                # Allowlist the column name before f-string interpolation
                from carrac.conditions import CONDITION_SCHEMAS
                metric_name = alert.get('metric', '')
                if metric_name not in CONDITION_SCHEMAS['derivatives_metric']['metrics']:
                    logger.warning(f"alert {alert.get('id')}: unknown derivatives_metric {metric_name!r}; skipping")
                    continue
                # The SIBLING of the plays evaluator. Both read the
                # same allowlist, so gating only carrac/conditions.py would have
                # left this path wide open — the recurring bug class in this
                # codebase. Gated on the alert's owner: this loop is a nightly
                # background job with no request context.
                from carrac.compliance.deribit_gate import condition_metric_visible
                if not condition_metric_visible(metric_name,
                                                user_id=alert.get('user_id')):
                    continue
                threshold = float(alert.get('threshold', 0))
                condition = alert.get('condition', 'above')
                with get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(f"""
                        SELECT {metric_name} FROM quant.derivatives
                        WHERE asset = 'BTC' AND {metric_name} IS NOT NULL
                        ORDER BY ts DESC LIMIT 1
                    """)
                    row = cur.fetchone()
                    if row and row.get(metric_name) is not None:
                        val = float(row[metric_name])
                        breached_now = (
                            (condition == 'above' and val >= threshold) or
                            (condition == 'below' and val <= threshold)
                        )
                        prev_breach = alert.get('last_breach')
                        if prev_breach is None:
                            # First evaluation — establish baseline, don't fire
                            alert['last_breach'] = breached_now
                            changed = True
                        elif breached_now and not prev_breach:
                            fire = True
                            alert['last_breach'] = True
                            changed = True
                        elif not breached_now and prev_breach:
                            alert['last_breach'] = False
                            changed = True
            except Exception as e:
                logger.error(
                    '[alert-eval] %s failed for alert %s — this alert type will not fire until fixed: %s',
                    'derivatives_metric', alert.get('id'), e, exc_info=True)
                _capture_exception(e)

        elif atype == 'chain_strength_breach':
            # Alert when a transmission chain's strength crosses a threshold.
            # Source: latest chain_strength signal written by leadlag engine.
            # Edge-detected: only fires when the breach flips false→true, so
            # `recurring` alerts don't spam nightly while the threshold holds.
            try:
                from carrac.db_pool import get_quant_conn as get_conn
                chain_name = alert.get('chain', '')
                threshold_c = float(alert.get('threshold', 0.15))
                cond_c = alert.get('condition', 'below')
                with get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT value FROM quant.signals
                        WHERE source = 'chain_strength'
                          AND metric = %s AND asset = '_GLOBAL_'
                        ORDER BY ts DESC LIMIT 1
                    """, (chain_name,))
                    row_c = cur.fetchone()
                    if row_c and row_c.get('value') is not None:
                        strength = float(row_c['value'])
                        breached_now = (
                            (cond_c == 'above' and strength >= threshold_c) or
                            (cond_c == 'below' and strength <= threshold_c)
                        )
                        prev_breach = alert.get('last_breach')
                        if prev_breach is None:
                            # First check — baseline, don't fire
                            alert['last_breach'] = breached_now
                            changed = True
                        elif breached_now and not prev_breach:
                            fire = True
                            alert['last_breach'] = True
                            changed = True
                        elif not breached_now and prev_breach:
                            alert['last_breach'] = False
                            changed = True
            except Exception as e:
                logger.error(
                    '[alert-eval] %s failed for alert %s — this alert type will not fire until fixed: %s',
                    'chain_strength_breach', alert.get('id'), e, exc_info=True)
                _capture_exception(e)

        elif atype == 'anomaly_cluster_active':
            # Fire when an unresolved multi-domain cluster meets the user's
            # thresholds. Uses latest active cluster in quant.anomalies.
            try:
                from carrac.db_pool import get_quant_conn as get_conn
                min_domains = int(alert.get('min_domains', 2))
                min_metrics = int(alert.get('min_metrics', 3))
                with get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT zscore AS extreme_count, metadata
                        FROM quant.anomalies
                        WHERE domain = 'cluster' AND metric = 'MULTI_DOMAIN'
                          AND resolved_at IS NULL
                        ORDER BY ts DESC LIMIT 1
                    """)
                    row_a = cur.fetchone()
                    if row_a:
                        extremes = int(row_a.get('extreme_count') or 0)
                        meta_a = row_a.get('metadata') or {}
                        if isinstance(meta_a, str):
                            import json as _json
                            meta_a = _json.loads(meta_a)
                        domains = int(meta_a.get('domains_affected') or 0)
                        if extremes >= min_metrics and domains >= min_domains:
                            fire = True
            except Exception as e:
                logger.error(
                    '[alert-eval] %s failed for alert %s — this alert type will not fire until fixed: %s',
                    'anomaly_cluster_active', alert.get('id'), e, exc_info=True)
                _capture_exception(e)

        elif atype == 'conditional_query_match':
            # Fire when all user-defined z-score conditions hold on the
            # latest day. We dedupe via `last_match` so a recurring alert
            # only re-fires when the match boolean flips false→true.
            try:
                from quant.engine.conditional import query_conditional
                conds = alert.get('conditions') or []
                tgt = alert.get('target', 'BTC')
                hor = int(alert.get('horizon', 30))
                res = query_conditional(conds, target=tgt, horizon=hor,
                                        include_returns=False)
                matches_now = bool(res and res.get('current_match'))
                prev = alert.get('last_match')
                if prev is None:
                    # First check establishes baseline — no fire on init
                    alert['last_match'] = matches_now
                    changed = True
                elif matches_now and not prev:
                    fire = True
                    alert['last_match'] = True
                    changed = True
                elif not matches_now and prev:
                    # State flipped back; record for next edge detection
                    alert['last_match'] = False
                    changed = True
            except Exception as e:
                logger.error(
                    '[alert-eval] %s failed for alert %s — this alert type will not fire until fixed: %s',
                    'conditional_query_match', alert.get('id'), e, exc_info=True)
                _capture_exception(e)

        if fire:
            alert['triggered'] = True
            alert['triggered_at'] = datetime.now().isoformat()
            changed = True

            # Deactivate 'once' alerts
            if alert.get('frequency') == 'once':
                alert['active'] = False

            triggered.append(alert)

            # Capture event for future efficacy analysis
            capture_alert_event(alert)

            # Build notification shape and stash for bundling
            uid = alert.get('user_id', '')
            if atype == 'zscore':
                zscore = current_zscores.get(alert['metric'], 0)
                direction = f"z={zscore:+.1f}σ ({alert['condition']} {alert['threshold']}σ)"
                symbol = alert['metric']
                note = alert.get('note') or f"{alert['metric']} {direction}"
            elif atype == 'chain_strength_breach':
                chain_name = alert.get('chain', '?')
                cond_c = alert.get('condition', 'below')
                thr_c = alert.get('threshold', 0.15)
                direction = f"chain '{chain_name}' {cond_c} {thr_c}"
                symbol = 'CHAIN'
                note = alert.get('note') or f"Transmission {direction}"
            elif atype == 'anomaly_cluster_active':
                direction = (f"cluster active ≥{alert.get('min_metrics', 3)} metrics "
                             f"across ≥{alert.get('min_domains', 2)} domains")
                symbol = 'ANOMALY'
                note = alert.get('note') or f"Anomaly {direction}"
            elif atype == 'conditional_query_match':
                conds_str = ', '.join(alert.get('conditions') or [])
                direction = f"conditions met for {alert.get('target', 'BTC')}"
                symbol = 'CONDITION'
                note = alert.get('note') or f"Match: {conds_str}"
            elif atype == 'derivatives_regime':
                direction = f"derivatives regime → {alert.get('last_deriv_regime', '?')}"
                symbol = 'DERIV'
                note = alert.get('note') or f"Derivatives {direction}"
            elif atype == 'derivatives_metric':
                direction = (f"{alert.get('metric', '?')} {alert.get('condition', 'above')} "
                             f"{alert.get('threshold', 0)}")
                symbol = 'DERIV'
                note = alert.get('note') or f"Derivatives metric {direction}"
            else:
                direction = f"→ {current_regime}"
                symbol = 'REGIME'
                note = alert.get('note') or f"Regime transition {direction}"

            alert.setdefault('_pending_notification', {}).update({
                'user_id': uid,
                'symbol': symbol,
                'title': f"{symbol} Alert",
                'body': note,
                'direction': direction,
                'target_price': 0,
                'current_price': 0,
                'note': note,
                'alert_id': alert['id'],
                'alert_type': atype,  # webhook payload alert_type
            })

    if changed:
        # Persists the triggered state (a fire always sets changed=True) before
        # dispatch, so a raising dispatch cannot cause a re-fire.
        alert_store.save_changes(alerts, baseline)

    if triggered:
        _dispatch_bundle_or_direct(triggered)

    return triggered
