"""
Alert and push-notification routes: alert reads, toggles, notification
channel tests and the shared notification-settings logic.

Alert create/update/delete and the notification-settings and analyst-source
saves are served by carrac/fastapi_app/routers/alerts.py, which calls
``apply_notification_settings`` from this module.
"""

import logging
import os
import re

from flask import Blueprint, g, jsonify, render_template, request, session

import push_notifications
from carrac.auth.decorators import login_required, auth_or_key_required, verify_api_key, get_request_tier, get_tier_limits
from carrac.extensions import limiter
from carrac.openapi.spec_decorator import spec
from security import validate_outbound_url
from carrac.notifications.redact import describe_exception, redact_secrets
from user_db import PreferencesWriteError, user_db


def _resolve_alert_user_id():
    """Resolve caller's user_id for alert ownership.

    Priority:
      1. Per-user API key (cvx_k_) sets ``g.api_key_user_id`` — return that.
      2. Session-authenticated user — return session['user_id'].
      3. Anything else (shared admin key with no per-user binding,
         or unauthenticated) — return ``None`` and let the caller emit 401.

    Previous code hardcoded ``user_id = 'admin'`` whenever
    ``verify_api_key()`` returned True, so a per-user API key wrote into
    admin's alert namespace. The shared admin key path now also returns
    None to refuse cross-user writes (no morning-bot caller actually
    writes alerts — only reads — so this is a no-op for prod bots).
    """
    uid = getattr(g, 'api_key_user_id', None)
    if uid is not None:
        return str(uid)
    sess_uid = session.get('user_id')
    if sess_uid is not None:
        return str(sess_uid)
    return None


def _ntfy_allow_hosts():
    """Allowlist for outbound ntfy URLs.

    Always permits the public ntfy.sh service. If the operator has set
    NTFY_BASE_HOST in the environment (e.g. for a self-hosted instance)
    that host is allowed too. Resolved at call-time so changing the env
    var doesn't require a restart of the alerts blueprint.
    """
    extra = (os.environ.get('NTFY_BASE_HOST', '') or '').strip()
    return ('ntfy.sh', extra) if extra and extra != 'ntfy.sh' else ('ntfy.sh',)

logger = logging.getLogger('carrac.alerts')


def telegram_token_rejected(token: str):
    """Does Telegram itself refuse this bot token?

`_validate_notification_settings` proves the token is well
    SHAPED (\\d+:[A-Za-z0-9_-]+) — it cannot tell a live credential from a
    revoked one. A token regenerated in BotFather leaves the copy in a
    user's preferences stale: every Telegram alert then 401s while the
    Settings page goes on showing the channel as enabled. Old and new tokens
    share the same bot id, so nothing about the stored value looked wrong.

    Three-valued ON PURPOSE:
      True  — Telegram answered, and the credential is dead. Block the save.
      False — Telegram answered and accepted it.
      None  — we could not ask (network, timeout, DNS).

    None must NOT block. A settings page that refuses to save because
    api.telegram.org is having an outage is a worse failure than a token that
    goes unverified until the next save — and the DLQ now records the real
    reason if it does turn out to be dead.
    """
    if not token:
        return None
    try:
        import requests as _req
        resp = _req.get(f'https://api.telegram.org/bot{token}/getMe', timeout=5)
    except Exception as exc:
        # requests quotes the request URL, token included, in its errors.
        logger.warning('could not verify Telegram token: %s', describe_exception(exc))
        return None
    if resp.status_code in (401, 404):
        return True
    return False


alerts_bp = Blueprint('alerts', __name__)


@alerts_bp.before_request
def _enforce_alerts_csrf():
    """Enforce CSRF for session-authenticated mutating requests.

    API-key callers (X-API-Key header) are exempt — they cannot be targeted
    by a CSRF attack because the attacker's page cannot read or inject
    custom request headers cross-origin.
    Session-authenticated callers on mutation methods MUST pass a valid
    CSRF token via X-CSRFToken header or csrf_token form field.
    """
    from flask import current_app
    if not current_app.config.get('WTF_CSRF_ENABLED', True):
        return None
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if api_key and verify_api_key(api_key):
        return None
    if session.get('user_id'):
        from flask_wtf.csrf import validate_csrf
        try:
            csrf_token = request.headers.get('X-CSRFToken') or request.form.get('csrf_token')
            validate_csrf(csrf_token)
        except Exception:
            return jsonify({'error': 'CSRF token missing or invalid'}), 403
    return None


# ============================================================
# Push Notification API Endpoints
# ============================================================

@alerts_bp.route('/api/push/vapid-key')
@login_required
def get_vapid_key():
    """Get the VAPID public key for client-side push subscription."""
    return jsonify({
        'publicKey': push_notifications.get_vapid_public_key()
    })


@alerts_bp.route('/api/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    """Subscribe to push notifications."""
    data = request.get_json()
    if not data or 'subscription' not in data:
        return jsonify({'error': 'Missing subscription data'}), 400

    user_id = str(session.get('user_id'))
    try:
        push_notifications.add_subscription(user_id, data['subscription'])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'status': 'subscribed'})


@alerts_bp.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def push_unsubscribe():
    """Unsubscribe from push notifications."""
    user_id = str(session.get('user_id'))
    push_notifications.remove_subscription(user_id)
    return jsonify({'status': 'unsubscribed'})


# ============================================================
# Alert CRUD Endpoints
# ============================================================

@alerts_bp.route('/api/alerts/triggered')
@auth_or_key_required
def alert_list_triggered():
    """Return alerts triggered since a given timestamp (for in-app toasts).

    Query param `since` is an ISO timestamp. Returns only alerts triggered
    after that time for the current user.
    """
    user_id = _resolve_alert_user_id()
    if user_id is None:
        return jsonify({'error': 'Authentication required'}), 401
    since = request.args.get('since', '')
    alerts = push_notifications.get_user_alerts(user_id, active_only=False)
    triggered = []
    for a in alerts:
        ts = a.get('triggered_at')
        if not ts:
            continue
        if since and ts <= since:
            continue
        triggered.append({
            'id': a.get('id'),
            'symbol': a.get('symbol'),
            'condition': a.get('condition', ''),
            'price': a.get('price'),
            'triggered_price': a.get('triggered_price'),
            'triggered_at': ts,
            'note': a.get('note', ''),
            'alert_type': a.get('alert_type', 'price'),
            'indicator': a.get('indicator'),
            'target_state': a.get('target_state'),
        })
    return jsonify({'triggered': triggered})


@alerts_bp.route('/api/alerts', methods=['GET'])
@spec(
    summary='List alerts',
    description='Return the caller\'s price/indicator alerts.',
    tags=['Alerts'],
    responses={200: 'AlertList', 401: 'Error'},
    parameters=[
        {'name': 'active', 'in': 'query', 'schema': {'type': 'boolean', 'default': True}},
        {'name': 'triggered_since', 'in': 'query', 'schema': {'type': 'string', 'format': 'date-time'}},
    ],
    min_tier='member',
)
def get_alerts():
    """Get user's price alerts. Supports session auth or API key."""
    if request.args.get('key'):
        return jsonify({'error': 'API key via URL parameter is deprecated. Use X-API-Key header.'}), 400
    key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
    if not verify_api_key(key) and 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401
    user_id = _resolve_alert_user_id()
    if user_id is None:
        # Shared admin key authenticated but is not bound to a user.
        # Refuse alert ownership rather than dumping into 'admin' literal.
        return jsonify({'error': 'Per-user API key required for alert operations'}), 401
    active_only = request.args.get('active', 'true').lower() == 'true'
    alerts = push_notifications.get_user_alerts(user_id, active_only=active_only)

    # Optional: filter to alerts triggered after a given ISO timestamp
    triggered_since = request.args.get('triggered_since')
    if triggered_since:
        alerts = [a for a in alerts if a.get('triggered_at') and a['triggered_at'] > triggered_since]

    return jsonify({'alerts': alerts})


@alerts_bp.route('/api/alerts/<int:alert_id>/toggle', methods=['POST'])
@spec(
    summary='Toggle alert active state',
    tags=['Alerts'],
    responses={200: 'Alert', 404: 'Error'},
    min_tier='member',
)
@auth_or_key_required
def toggle_alert(alert_id):
    """Toggle a price alert's active status."""
    user_id = _resolve_alert_user_id()
    if user_id is None:
        return jsonify({'error': 'Authentication required'}), 401
    alert = push_notifications.toggle_alert(user_id, alert_id)
    if alert:
        return jsonify({'alert': alert})
    return jsonify({'error': 'Alert not found'}), 404


@alerts_bp.route('/api/alerts/<int:alert_id>/reactivate', methods=['POST'])
@login_required
def reactivate_alert(alert_id):
    """Reactivate a triggered 'once' alert so it can fire again."""
    user_id = str(session.get('user_id'))
    alert = push_notifications.reactivate_alert(user_id, alert_id)
    if alert:
        return jsonify({'alert': alert})
    return jsonify({'error': 'Alert not found'}), 404


# ============================================================
# Push Test Endpoint
# ============================================================

@alerts_bp.route('/api/push/test', methods=['POST'])
@login_required
def test_push():
    """Send a test push notification to the current user."""
    user_id = str(session.get('user_id'))
    success = push_notifications.send_push_notification(
        user_id=user_id,
        title='Test Notification',
        body='Push notifications are working!',
        tag='test'
    )
    if success:
        return jsonify({'status': 'sent'})
    return jsonify({'error': 'Failed to send notification. Check subscription.'}), 400


# ============================================================
# Notification Settings
# ============================================================

_NOTIF_DEFAULTS = {
    'ntfy': {'enabled': False, 'url': ''},
    'discord': {'enabled': False, 'webhook_url': ''},
    'telegram': {'enabled': False, 'bot_token': '', 'chat_id': ''},
    'email': {'enabled': False},
    # user-defined webhook, fires for every alert type. 'secret'
    # is server-generated + encrypted at rest (never client-supplied) —
    # see carrac.alerts.webhook_delivery. consecutive_failures/last_delivery_*
    # are dispatch-owned bookkeeping, also never client-supplied.
    'webhook': {
        'enabled': False, 'url': '', 'secret': '',
        'consecutive_failures': 0,
        'last_delivery_status': None, 'last_delivery_at': None,
    },
    # Notification TYPE toggle (not a channel). Position-health warnings fire
    # when an indicator flips against an open position; useful for some, noisy
    # for others. Default ON (preserves prior behaviour); per-user opt-out.
    'position_warnings': {'enabled': True},
}


def _validate_notification_settings(data: dict) -> tuple:
    """Validate notification prefs. Returns (cleaned_dict, error_string|None)."""
    cleaned = {}
    ntfy = data.get('ntfy', {})
    cleaned['ntfy'] = {
        'enabled': bool(ntfy.get('enabled')),
        'url': str(ntfy.get('url', '')).strip()[:500],
    }
    if cleaned['ntfy']['enabled'] and cleaned['ntfy']['url']:
        # SSRF hardening. startswith('https://') is not enough —
        # an attacker can still point the URL at RFC1918, loopback, or
        # cloud metadata hosts. validate_outbound_url enforces scheme,
        # host allowlist, IDN/homograph normalisation, and DNS resolution
        # with private/metadata IP rejection.
        ok, reason = validate_outbound_url(
            cleaned['ntfy']['url'],
            allowed_schemes=('https',),
            allow_hosts=_ntfy_allow_hosts(),
        )
        if not ok:
            return None, f'ntfy URL rejected: {reason}'

    discord = data.get('discord', {})
    cleaned['discord'] = {
        'enabled': bool(discord.get('enabled')),
        'webhook_url': str(discord.get('webhook_url', '')).strip()[:500],
    }
    if cleaned['discord']['enabled'] and cleaned['discord']['webhook_url']:
        if not re.match(r'https://discord\.com/api/webhooks/\d+/.+',
                        cleaned['discord']['webhook_url']):
            return None, 'Invalid Discord webhook URL'

    tg = data.get('telegram', {})
    cleaned['telegram'] = {
        'enabled': bool(tg.get('enabled')),
        'bot_token': str(tg.get('bot_token', '')).strip(),
        'chat_id': str(tg.get('chat_id', '')).strip(),
    }
    if cleaned['telegram']['enabled']:
        if cleaned['telegram']['bot_token'] and not re.match(
                r'\d+:[A-Za-z0-9_-]+$', cleaned['telegram']['bot_token']):
            return None, 'Invalid Telegram bot token format'
        if cleaned['telegram']['chat_id'] and not re.match(
                r'-?\d+$', cleaned['telegram']['chat_id']):
            return None, 'Telegram chat ID must be numeric'

    email_data = data.get('email', {})
    cleaned['email'] = {
        'enabled': bool(email_data.get('enabled')),
    }

    # Webhook URL/enabled only — secret + delivery bookkeeping
    # are server-managed and merged in by the caller (save_notification_settings),
    # never accepted from client input here.
    webhook = data.get('webhook', {})
    cleaned['webhook'] = {
        'enabled': bool(webhook.get('enabled')),
        'url': str(webhook.get('url', '')).strip()[:500],
    }
    if cleaned['webhook']['enabled'] and cleaned['webhook']['url']:
        ok, reason = validate_outbound_url(
            cleaned['webhook']['url'],
            allowed_schemes=('https',),
        )
        if not ok:
            return None, f'Webhook URL rejected: {reason}'

    # Notification TYPE toggle — position-health warnings. Default ON when the
    # key is absent so omitting it never silently disables warnings.
    pw = data.get('position_warnings', {})
    cleaned['position_warnings'] = {'enabled': bool(pw.get('enabled', True))}

    return cleaned, None


@alerts_bp.route('/settings')
@login_required
def settings_page():
    """Render the notification settings page."""
    return render_template('settings.html')


_TOKEN_MASK = '****'


def _mask_token(plain: str) -> str:
    """First four characters + '****' — what the UI is shown instead of a
    bot token, and therefore what the page sends back on Save."""
    return (plain[:4] + _TOKEN_MASK) if plain else ''


@alerts_bp.route('/api/notification-settings', methods=['GET'])
@login_required
def get_notification_settings():
    """Get current user's notification preferences."""
    try:
        uid = int(session.get('user_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid session'}), 401
    prefs = user_db.get_preferences(uid)
    notif = prefs.get('notifications', _NOTIF_DEFAULTS)
    # Merge defaults for any missing keys
    for ch, defaults in _NOTIF_DEFAULTS.items():
        if ch not in notif:
            notif[ch] = dict(defaults)
    # Decrypt bot_token for the UI but only expose a masked version
    # so the raw token is never sent to the browser in plaintext.
    from carrac.secret_box import decrypt_token
    tg = notif.get('telegram', {})
    stored_token = tg.get('bot_token', '')
    if stored_token:
        plain = decrypt_token(stored_token)
        # Mask: show first 4 chars + asterisks so UI can confirm it's set
        # without exposing the full token.
        masked = _mask_token(plain)
        tg = dict(tg)
        tg['bot_token'] = masked
        notif = dict(notif)
        notif['telegram'] = tg

    # Never return the raw (decrypted) webhook secret — only a
    # masked fingerprint + whether one exists. The plaintext is only ever
    # returned once, at generation time (save / regenerate responses).
    wh = notif.get('webhook', {})
    if wh.get('secret'):
        from carrac.alerts.webhook_delivery import mask_secret
        plain_secret = decrypt_token(wh['secret'])
        wh = dict(wh)
        wh['secret'] = mask_secret(plain_secret)
        wh['has_secret'] = bool(plain_secret)
        notif = dict(notif)
        notif['webhook'] = wh

    # Tell the user when a channel they have switched ON is not
    # actually delivering. Without this the page shows "enabled" for a
    # channel that has silently dropped every alert. Read from the DLQ, not the circuit-breaker: the breaker lives in
    # the worker's memory and is invisible from here.
    try:
        from carrac.notifications.resilience import undelivered_summary
        notif = dict(notif)
        notif['delivery_health'] = undelivered_summary(str(uid))
    except Exception as e:
        # A health read must never take the settings page down with it.
        # It is logged rather than swallowed so a broken indicator is
        # itself findable.
        logger.warning(f'delivery_health unavailable for user {uid}: {e}')

    return jsonify(notif)


def apply_notification_settings(uid: int, data: dict, limits: dict) -> tuple[dict, int]:
    """Validate and store a user's notification settings. Returns (payload, status).

    Called by ``save_notification_settings`` in
    carrac/fastapi_app/routers/alerts.py, which serves the settings page's
    POST. It lives here, beside the GET that renders the same settings, so
    the two halves cannot drift apart.
    """
    tg_data = data.get('telegram', {})
    dc_data = data.get('discord', {})
    wh_data = data.get('webhook', {})
    if not limits.get('telegram') and tg_data.get('enabled'):
        return ({'error': 'Telegram notifications require a Member or Premium subscription',
                 'upgrade_required': True}, 403)
    if not limits.get('discord') and dc_data.get('enabled'):
        return ({'error': 'Discord notifications require a Member or Premium subscription',
                 'upgrade_required': True}, 403)
    # Webhooks are a paid feature; a tier whose limits lack 'webhooks' is
    # refused.
    if not limits.get('webhooks', False) and wh_data.get('enabled'):
        return ({'error': 'Webhooks require a paid plan', 'upgrade_required': True}, 403)

    # Second press: the GET above shows the stored bot token as
    # `_mask_token()` output and the page sends that mask straight back on
    # Save, so the format check rejected every save by a user with Telegram
    # configured. A submitted mask that matches the stored token means
    # "unchanged": validate and store the real one, and do not re-ask
    # Telegram about a credential that did not change.
    prefs = user_db.get_preferences(uid)
    token_unchanged = False
    submitted_token = str((data.get('telegram') or {}).get('bot_token', '')).strip()
    if submitted_token.endswith(_TOKEN_MASK):
        from carrac.secret_box import decrypt_token
        stored_enc = ((prefs.get('notifications') or {}).get('telegram') or {}).get('bot_token', '')
        plain = decrypt_token(stored_enc) if stored_enc else ''
        if plain and submitted_token == _mask_token(plain):
            data = dict(data)
            data['telegram'] = dict(data.get('telegram') or {})
            data['telegram']['bot_token'] = plain
            token_unchanged = True

    cleaned, err = _validate_notification_settings(data)
    if err:
        return ({'error': err}, 400)

    # The format regex above proves the token is well-SHAPED, not
    # that it WORKS. A token regenerated in BotFather leaves a stale copy here,
    # and every Telegram alert then 401s while Settings keeps showing the
    # channel enabled. Ask Telegram whether the credential is live before storing it.
    #
    # Deliberately asymmetric: a credential Telegram REJECTS blocks the save,
    # but an unreachable api.telegram.org does NOT — a settings page that
    # cannot be saved during someone else's outage is a worse failure than a
    # token checked one save later.
    tg_clean = cleaned.get('telegram', {})
    if tg_clean.get('enabled') and tg_clean.get('bot_token') and not token_unchanged:
        if telegram_token_rejected(tg_clean['bot_token']) is True:
            return ({'error': 'Telegram rejected this bot token (Unauthorized). '
                              'If you regenerated the bot in BotFather, paste the '
                              'new token here.'}, 400)

    # Encrypt bot token at write time
    from carrac.secret_box import encrypt_token
    raw_token = cleaned.get('telegram', {}).get('bot_token', '')
    if raw_token:
        cleaned['telegram']['bot_token'] = encrypt_token(raw_token)

    # Webhook secret + delivery bookkeeping are server-managed —
    # never accepted from client input (_validate_notification_settings only
    # cleans enabled/url). Preserve whatever is already stored, minting a
    # fresh secret the first time the user enables it with none set yet.
    existing_webhook = (prefs.get('notifications') or {}).get('webhook', {}) or {}
    new_secret_plain = None
    secret_enc = existing_webhook.get('secret', '')
    if cleaned['webhook']['enabled'] and not secret_enc:
        from carrac.alerts.webhook_delivery import generate_webhook_secret
        new_secret_plain = generate_webhook_secret()
        secret_enc = encrypt_token(new_secret_plain)
    cleaned['webhook']['secret'] = secret_enc
    cleaned['webhook']['consecutive_failures'] = existing_webhook.get('consecutive_failures', 0)
    cleaned['webhook']['last_delivery_status'] = existing_webhook.get('last_delivery_status')
    cleaned['webhook']['last_delivery_at'] = existing_webhook.get('last_delivery_at')

    prefs['notifications'] = cleaned
    try:
        user_db.save_preferences(uid, prefs)
    except PreferencesWriteError:
        logger.exception('notification settings not saved for user %s', uid)
        # A webhook secret minted above was never stored, so it must not
        # be shown: the user would configure a secret nothing signs with.
        return ({'error': 'Your notification settings could not be saved. '
                          'Nothing was changed; please try again.'}, 500)
    resp = {'status': 'saved'}
    if new_secret_plain:
        # Reveal ONCE — the plaintext is never returned again after this.
        resp['webhook_secret'] = new_secret_plain
    return (resp, 200)


@alerts_bp.route('/api/notification-settings/test', methods=['POST'])
@limiter.limit("10 per minute")
@login_required
def test_notification_channel():
    """Send a test notification to a specific channel."""
    data = request.get_json()
    channel = (data or {}).get('channel', '')

    # Gate Telegram/Discord behind premium
    tier = get_request_tier()
    limits = get_tier_limits(tier)
    if channel == 'telegram' and not limits.get('telegram'):
        return jsonify({'error': 'Telegram notifications require a Member or Premium subscription',
                        'upgrade_required': True}), 403
    if channel == 'discord' and not limits.get('discord'):
        return jsonify({'error': 'Discord notifications require a Member or Premium subscription',
                        'upgrade_required': True}), 403
    if channel == 'webhook' and not limits.get('webhooks', False):
        return jsonify({'error': 'Webhooks require a paid plan',
                        'upgrade_required': True}), 403

    try:
        uid = int(session.get('user_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid session'}), 401
    prefs = user_db.get_preferences(uid)
    notif = prefs.get('notifications', {})

    test_body = "Test alert from Carrac\nIf you see this, notifications are working!"

    try:
        import requests as req
    except ImportError:
        return jsonify({'error': 'requests library not available'}), 500

    try:
        if channel == 'ntfy':
            url = notif.get('ntfy', {}).get('url', '')
            if not url:
                return jsonify({'error': 'No ntfy URL configured'}), 400
            # re-validate stored URL on every test send.
            # Prefs predating the URL validator may contain unsafe
            # URLs; without this defense-in-depth check the test
            # endpoint becomes a free SSRF gadget for any logged-in
            # user.
            ok, reason = validate_outbound_url(
                url,
                allowed_schemes=('https',),
                allow_hosts=_ntfy_allow_hosts(),
            )
            if not ok:
                return jsonify({'error': f'ntfy URL rejected: {reason}'}), 400
            resp = req.post(url, data=test_body.encode('utf-8'),
                            headers={'Title': 'Carrac Test',
                                     'Tags': 'white_check_mark',
                                     'Priority': '3'}, timeout=10)
            resp.raise_for_status()

        elif channel == 'discord':
            webhook = notif.get('discord', {}).get('webhook_url', '')
            if not webhook:
                return jsonify({'error': 'No Discord webhook configured'}), 400
            resp = req.post(webhook, json={'content': f"**Carrac Test**\n{test_body}"},
                            timeout=10)
            resp.raise_for_status()

        elif channel == 'telegram':
            cfg = notif.get('telegram', {})
            # Decrypt stored token before use
            from carrac.secret_box import decrypt_token
            token = decrypt_token(cfg.get('bot_token', ''))
            chat_id = cfg.get('chat_id', '')
            if not token or not chat_id:
                return jsonify({'error': 'Telegram bot token and chat ID required'}), 400
            resp = req.post(f'https://api.telegram.org/bot{token}/sendMessage',
                            json={'chat_id': chat_id,
                                  'text': f'*Carrac Test*\n{test_body}',
                                  'parse_mode': 'Markdown'}, timeout=10)
            resp.raise_for_status()

        elif channel == 'email':
            from carrac.email import send_email
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
                    _conn.rollback()
                except Exception:
                    try: _conn.rollback()
                    except Exception: pass
                    raise
            if not row:
                return jsonify({'error': 'Could not find your email address'}), 400
            success = send_email(
                row['email'],
                'Carrac — Test Notification',
                f'<div style="font-family:monospace;background:#0d1117;color:#d1d4dc;padding:24px;border-radius:8px">'
                f'<h2 style="color:#e6edf3;font-size:16px;margin:0 0 8px">Test Notification</h2>'
                f'<p>{test_body}</p>'
                f'<p style="color:#8b949e;font-size:11px;margin-top:16px">Email digest notifications are working.</p></div>',
                test_body,
            )
            if not success:
                return jsonify({'error': 'Email sending failed — check server logs'}), 500

        elif channel == 'webhook':
            from carrac.alerts.webhook_delivery import send_test_webhook
            ok, wh_error = send_test_webhook(uid)
            if not ok:
                return jsonify({'error': wh_error or 'Test webhook failed'}), 400

        else:
            return jsonify({'error': f'Unknown channel: {channel}'}), 400

        return jsonify({'status': 'sent'})
    except Exception as e:
        # The exception text quotes the request URL, which for Telegram
        # holds the bot token and for Discord and ntfy is the credential.
        logger.warning('Test notification failed (%s): %s', channel, describe_exception(e))
        return jsonify({'error': redact_secrets(str(e))}), 400


@alerts_bp.route('/api/notification-settings/webhook/regenerate', methods=['POST'])
@limiter.limit("10 per minute")
@login_required
def regenerate_webhook_secret():
    """Rotate the signing secret for the current user's alert webhook.

    The plaintext secret is returned ONCE in this response —
    GET /api/notification-settings only ever returns a masked fingerprint.
    Resets the delivery bookkeeping (a rotated secret means any consumer
    still using the previous one will fail signature checks, which
    is expected, not a fault of the new secret).
    """
    tier = get_request_tier()
    limits = get_tier_limits(tier)
    if not limits.get('webhooks', False):
        return jsonify({'error': 'Webhooks require a paid plan',
                        'upgrade_required': True}), 403

    try:
        uid = int(session.get('user_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid session'}), 401

    from carrac.alerts.webhook_delivery import generate_webhook_secret
    from carrac.secret_box import encrypt_token

    plain_secret = generate_webhook_secret()
    prefs = user_db.get_preferences(uid)
    notif = prefs.setdefault('notifications', {})
    webhook_cfg = notif.setdefault('webhook', dict(_NOTIF_DEFAULTS['webhook']))
    webhook_cfg['secret'] = encrypt_token(plain_secret)
    webhook_cfg['consecutive_failures'] = 0
    webhook_cfg['last_delivery_status'] = None
    webhook_cfg['last_delivery_at'] = None
    try:
        user_db.save_preferences(uid, prefs)
    except PreferencesWriteError:
        logger.exception('webhook secret not rotated for user %s', uid)
        return jsonify({'error': 'The new secret could not be saved. '
                                 'Your current secret still applies.'}), 500
    return jsonify({'secret': plain_secret})


# ============================================================
# Analyst Sources (X handles + YouTube channels)
# ============================================================


# Suggested X handles shown to users with no saved list; the operator sets
# them in X_SEARCH_HANDLES (comma-separated), empty by default.
_DEFAULT_X_HANDLES = [
    h.strip().lstrip('@')
    for h in os.environ.get('X_SEARCH_HANDLES', '').split(',')
    if h.strip()
]
# No channels are followed by default; each user picks their own in Settings.
_DEFAULT_YT_CHANNELS: dict = {}


@alerts_bp.route('/api/user/analyst-sources', methods=['GET'])
@login_required
def get_analyst_sources():
    """Get user's followed X handles and YouTube channels."""
    try:
        uid = int(session.get('user_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid session'}), 401
    prefs = user_db.get_preferences(uid)
    return jsonify({
        'x_handles': prefs.get('followed_x_handles', _DEFAULT_X_HANDLES),
        'yt_channels': prefs.get('followed_yt_channels', _DEFAULT_YT_CHANNELS),
    })

