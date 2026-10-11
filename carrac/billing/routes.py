"""
Billing routes: credit balance, top-up page, Paddle webhook, customer portal.

Paddle is the Merchant of Record — handles VAT, invoicing, compliance.
Credit ledger (add/deduct/refund) is provider-agnostic.
"""

import hashlib
import hmac
import logging
import time

from flask import Blueprint

from carrac.config import Config
from carrac.notifications import send_telegram_message
from user_db import user_db

logger = logging.getLogger('carrac.billing')

billing_bp = Blueprint('billing', __name__)


# ── Public/auth pages ──────────────────────────────────────────────────

def _verify_paddle_signature(sig_header: str, raw_body: str, secret: str) -> bool:
    """Verify Paddle-Signature header: ts=<timestamp>;h1=<hmac-sha256>."""
    if not sig_header:
        return False

    parts = {}
    for part in sig_header.split(';'):
        if '=' in part:
            k, v = part.split('=', 1)
            parts[k.strip()] = v.strip()

    ts = parts.get('ts', '')
    h1 = parts.get('h1', '')
    if not ts or not h1:
        return False

    # Only reject future-dated signatures (>5 min ahead). Paddle
    # retries failed webhooks for up to 3 days with the ORIGINAL timestamp,
    # so the previous symmetric `abs(now - ts) > 300` bound silently rejected
    # every retry whenever the service had been unavailable for more than
    # five minutes. Idempotency is still enforced via
    # `user_db.mark_payment_processed(event_id)` in every handler.
    try:
        ts_int = int(ts)
        now = int(time.time())
        if ts_int - now > 300:
            logger.warning(f"Paddle webhook: signature timestamp is in the future (ts={ts_int}, now={now}); rejecting")
            return False
        if now - ts_int > 72 * 3600:
            logger.warning(f"Paddle webhook: signature timestamp is too old ({now - ts_int}s behind now); rejecting")
            return False
        if now - ts_int > 300:
            logger.info(f"Paddle webhook: accepting replay/retry with old ts ({now - ts_int}s behind now)")
    except (ValueError, TypeError):
        return False

    # Paddle signs: ts:raw_body
    signed_payload = f"{ts}:{raw_body}"
    expected = hmac.new(
        secret.encode('utf-8'),
        signed_payload.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(h1, expected)


def _resolve_user_from_event(data: dict) -> tuple:
    """Extract user_id from webhook event data.

    Resolve authoritatively from `paddle_customer_id` first. The
    `custom_data.user_id` field is set by the browser during checkout and
    CAN be tampered with (Paddle.js init intercepts). If it mismatches the
    Paddle customer we already have on file for this `paddle_customer_id`,
    reject the webhook: it's either a client-side attack or a misconfigured
    integration, never a legitimate signal.

    When there is no existing `paddle_customer_id → user` mapping yet
    (e.g. a user's very first `transaction.completed` before
    `customer.created` has been processed), we accept `custom_data.user_id`
    as a hint — there is no other source — but we verify the named user
    actually exists before trusting it.

    Returns (user_id, paddle_customer_id) or (None, None).
    """
    custom_data = data.get('custom_data') or {}
    raw_custom_uid = custom_data.get('user_id')
    hinted_uid: int | None = None
    if raw_custom_uid:
        try:
            hinted_uid = int(raw_custom_uid)
        except (ValueError, TypeError):
            hinted_uid = None

    paddle_cid = data.get('customer_id') or ''

    resolved_uid: int | None = None
    if paddle_cid:
        user = user_db.get_user_by_paddle_customer_id(paddle_cid)
        if user:
            resolved_uid = user['id']

    if resolved_uid is not None:
        if hinted_uid is not None and hinted_uid != resolved_uid:
            logger.warning(
                f"Paddle webhook: custom_data.user_id={hinted_uid} mismatches "
                f"paddle_customer_id={paddle_cid} (really user {resolved_uid}); "
                f"rejecting — possible tampered checkout."
            )
            return None, paddle_cid
        return resolved_uid, paddle_cid

    # No existing mapping — only source is custom_data. Verify it points to
    # a real user row before trusting it.
    if hinted_uid is not None:
        try:
            if user_db.get_user_by_id(hinted_uid):
                return hinted_uid, paddle_cid
        except Exception as e:
            logger.warning(f"Paddle webhook: hinted user lookup failed: {e}")

    return None, paddle_cid


def _tier_allows_topup(user_id: int) -> bool:
    """Check whether the user's current tier permits AI credit top-ups.

    Free tier has `topups_allowed=False` in TIER_LIMITS; crediting them
    produces unusable credits (ai_queries_monthly=0) and a refund demand.
    """
    try:
        user = user_db.get_user_by_id(user_id)
        tier = (user or {}).get('tier') or 'free'
        from carrac.auth.decorators import get_tier_limits
        return bool(get_tier_limits(tier).get('topups_allowed'))
    except Exception as e:
        logger.warning(f"_tier_allows_topup({user_id}): lookup failed ({e}); refusing top-up")
        return False


def _notify_topup_on_ineligible_tier(user_id: int, credits: int, event_id: str):
    """Telegram alert so the operator can issue a Paddle refund manually.

    We cannot auto-refund without the transaction_id (some webhook event
    shapes omit it); surfacing the event_id is enough for a human to find
    it in the Paddle dashboard.
    """
    try:
        send_telegram_message(
            f"⚠️ Paddle top-up for user {user_id} ({credits}c) "
            f"rejected — tier does not allow top-ups. Issue manual refund "
            f"for event {event_id} in the Paddle dashboard."
        )
    except Exception as e:
        logger.error(f"failed to notify operator of ineligible top-up: {e}")


def _handle_customer_created(data: dict, event_id: str = ''):
    """Store paddle_customer_id on user when Paddle creates a customer.

    Marker INSERT ordered AFTER the ID write so a failed
    `set_paddle_customer_id` doesn't strand us flagged-as-processed without
    the mapping written. Idempotent on its own (UPDATE), but consistent.
    """
    paddle_cid = data.get('id', '')
    custom_data = data.get('custom_data') or {}
    raw_uid = custom_data.get('user_id')

    try:
        user_id = int(raw_uid) if raw_uid is not None else 0
    except (ValueError, TypeError):
        user_id = 0

    def _work():
        if user_id and paddle_cid:
            existing_user = user_db.get_user_by_id(user_id)
            if not existing_user:
                logger.warning(
                    f"Paddle customer.created: custom_data.user_id={user_id} does not exist; "
                    f"skipping paddle_customer_id={paddle_cid} mapping"
                )
                return
            user_db.set_paddle_customer_id(user_id, paddle_cid, _commit=False)
            logger.info(f"Stored paddle_customer_id={paddle_cid} for user {user_id}")

    try:
        ran = user_db.process_paddle_event_atomic(event_id, user_id, _work)
        if not ran:
            logger.info(f"Paddle: event {event_id} already processed (no-op)")
        elif paddle_cid:
            # Drain any events that arrived before this customer mapping existed.
            _drain_pending_events(paddle_cid)
    except Exception as e:
        logger.error(f"Failed to store paddle_customer_id: {e}")


def _derive_subscription_days(data: dict, default: int = 30) -> int:
    """Derive subscription duration from Paddle billing-period or next_billed_at fields.

    Falls back to ``default`` (30) when the payload lacks the field or parse fails.
    Checks common paths where Paddle embeds the billing period end date:
      - data['subscription']['current_billing_period']['ends_at']
      - data['subscription']['next_billed_at']
      - data['current_billing_period']['ends_at']
      - data['next_billed_at']
    """
    import math
    from datetime import datetime, timezone as _tz

    for key_path in [
        ('subscription', 'current_billing_period', 'ends_at'),
        ('subscription', 'next_billed_at'),
        ('current_billing_period', 'ends_at'),
        ('next_billed_at',),
    ]:
        node = data
        for k in key_path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(k)
        if node and isinstance(node, str):
            try:
                ends_at = datetime.fromisoformat(node.replace('Z', '+00:00'))
                if ends_at.tzinfo is None:
                    ends_at = ends_at.replace(tzinfo=_tz.utc)
                delta = ends_at - datetime.now(_tz.utc)
                # A past/near-past ends_at (e.g. stale period on a long-paused
                # resume) must not grant a 1-day sub — fall through to default.
                if delta.total_seconds() < 86400 / 2:
                    continue
                return math.ceil(delta.total_seconds() / 86400)
            except (ValueError, TypeError):
                pass
    return default


def _handle_transaction_completed(data: dict, event_id: str):
    """Handle completed transaction — subscription payment or one-time top-up.

    All entitlement writes (tier flip, credits, deposit ledger)
    happen inside `process_paddle_event_atomic` so they share ONE transaction
    with the idempotency-marker INSERT. If any step raises (DB blip, deadlock,
    serialization conflict), the marker is rolled back along with the work,
    and Paddle's retry sees no marker → replays cleanly. The previous
    "marker-FIRST" pattern silently committed the marker, so a mid-handler
    failure left the user paid-but-without-entitlement.
    """
    user_id, paddle_cid = _resolve_user_from_event(data)
    if not user_id:
        # Quarantine the event so it can be replayed once customer.created
        # confirms the paddle_customer_id → user mapping. Without this, a race
        # (transaction.completed arrives before customer.created) silently drops
        # the entitlement grant.
        _quarantine_paddle_event(paddle_cid or '', 'transaction.completed', event_id, data)
        logger.warning(
            f"Paddle transaction.completed: could not resolve user (customer={paddle_cid}); "
            f"event {event_id} quarantined — will replay on customer.created"
        )
        return

    # Paddle can send subscription_id at the top level or nested inside
    # items[*].subscription_id depending on billing event shape. Take the first
    # non-null from either location so out-of-order deliveries don't leave a
    # NULL mapping and break subsequent subscription.updated / cancelled events.
    subscription_id = data.get('subscription_id')
    items = data.get('items', [])
    if not subscription_id:
        for _item in items:
            _sid = _item.get('subscription_id')
            if _sid:
                subscription_id = _sid
                logger.info(
                    f"Paddle transaction.completed: resolved subscription_id={subscription_id} "
                    f"from items (not top-level)"
                )
                break

    price_to_tier = Config.paddle_price_to_tier()
    price_to_credits = Config.paddle_price_to_credits()

    # Funnel event payloads queued during work — fire AFTER commit only.
    funnel_events = []
    # Billing events (top-ups) queued the same way — on_billing_event
    # needs the user's email, which we only fetch once, after commit.
    billing_events = []

    def _work():
        # Store paddle_customer_id if not yet stored (no-commit: same txn)
        if paddle_cid:
            user_db.set_paddle_customer_id(user_id, paddle_cid, _commit=False)

        for item in items:
            price = item.get('price', {})
            price_id = price.get('id', '')

            # Subscription payment (new or renewal)
            if price_id in price_to_tier:
                tier = price_to_tier[price_id]
                days = _derive_subscription_days(data)
                user_db.activate_subscription(
                    user_id, tier=tier, days=days,
                    payment_id=subscription_id or event_id, _commit=False,
                )
                if subscription_id:
                    user_db.set_paddle_subscription_id(user_id, subscription_id, _commit=False)
                logger.info(f"Paddle: activated {tier} subscription for user {user_id} (sub={subscription_id})")
                funnel_events.append(('subscription_started',
                                      {'tier': tier, 'paddle_subscription_id': subscription_id or ''}))

            # One-time credit top-up
            elif price_id in price_to_credits:
                credits = price_to_credits[price_id]
                # Refuse top-ups on tiers that disallow them. Free tier has
                # `ai_queries_monthly=0` so purchased credits are unusable; leaving
                # them on the account guarantees a refund request. Flag for manual
                # refund via Telegram instead of silently pocketing the charge.
                if not _tier_allows_topup(user_id):
                    logger.warning(
                        f"Paddle top-up rejected: user {user_id} tier does not "
                        f"allow top-ups. credits={credits}c event={event_id}. "
                        f"REFUND MANUALLY via Paddle dashboard."
                    )
                    _notify_topup_on_ineligible_tier(user_id, credits, event_id)
                    continue
                user_db.add_ai_credits(user_id, credits, _commit=False)
                user_db.record_ai_deposit(
                    user_id, credits, payment_id=event_id, source='paddle', _commit=False,
                )
                logger.info(f"Paddle: credited {credits}c to user {user_id} (topup)")
                billing_events.append(('topup', credits))

            else:
                logger.warning(f"Paddle transaction: unknown price_id={price_id}")

    ran = user_db.process_paddle_event_atomic(event_id, user_id, _work)
    if not ran:
        logger.info(f"Paddle: event {event_id} already processed (no-op)")
        return

    # subscription_started funnel event (one-shot per user). Fire only
    # AFTER the txn commits — otherwise a rolled-back webhook would leak a
    # "subscription_started" PostHog event with no actual subscription.
    for ev_name, props in funnel_events:
        try:
            from carrac.integrations import fire_first_event
            fire_first_event(user_id, ev_name, props)
        except Exception:
            pass

    # Wire on_billing_event for top-ups — was defined but never called
    # from the actual credit-purchase path, so PostHog person props
    # (ai_total_deposited_cents etc.) never updated on a top-up.
    if billing_events:
        try:
            u = user_db.get_user_by_id(user_id)
            if u and u.get('email'):
                from carrac.integrations import on_billing_event
                for event_type, amount_cents in billing_events:
                    on_billing_event(user_id, u['email'], event_type, amount_cents)
        except Exception as e:
            logger.warning(f"on_billing_event (paddle topup) failed: {e}")


def _handle_subscription_activated(data: dict, event_id: str):
    """Handle subscription activation — store IDs only.

    The actual tier activation is handled by transaction.completed (which fires
    alongside this event).  Calling activate_subscription() here too would double
    the granted days (60 instead of 30).

    Marker INSERT ordered AFTER the ID writes so a failed write
    doesn't leave us flagged-as-processed with the IDs unset.
    """
    user_id, paddle_cid = _resolve_user_from_event(data)
    if not user_id:
        logger.warning(f"Paddle subscription.activated: could not resolve user (customer={paddle_cid})")
        return

    sub_id = data.get('id', '')

    def _work():
        if paddle_cid:
            user_db.set_paddle_customer_id(user_id, paddle_cid, _commit=False)
        if sub_id:
            user_db.set_paddle_subscription_id(user_id, sub_id, _commit=False)

    ran = user_db.process_paddle_event_atomic(event_id, user_id, _work)
    if not ran:
        logger.info(f"Paddle: event {event_id} already processed (no-op)")
        return

    logger.info(f"Paddle: subscription.activated user={user_id} sub={sub_id} (IDs stored, tier set by transaction.completed)")


def _handle_subscription_updated(data: dict, event_id: str):
    """Handle subscription plan change (upgrade/downgrade).

    Only updates the tier column — must NOT touch subscription_expires_at.
    set_user_tier() NULLs the expiry (admin function), so we do a targeted
    UPDATE here to preserve the billing period.

    Marker INSERT ordered AFTER the tier flip so a failed UPDATE
    doesn't strand us with the marker set and the user's tier unchanged.
    """
    user_id, paddle_cid = _resolve_user_from_event(data)
    if not user_id:
        return

    items = data.get('items', [])
    price_to_tier = Config.paddle_price_to_tier()
    new_tier = None

    # Snapshot the tier BEFORE _work() flips it — same accessor
    # (user_db.get_user_by_id) the admin tier-override route uses to build its
    # `old_tier` — so the CRM sync below reports a real transition instead of
    # the previous hardcoded None (which made every webhook-driven tier
    # change look like a fresh signup in PostHog/Espo).
    old_tier = None
    try:
        u_before = user_db.get_user_by_id(user_id)
        if u_before:
            old_tier = u_before.get('tier')
    except Exception as e:
        logger.warning(f"Paddle subscription.updated: could not fetch old tier for user {user_id}: {e}")

    def _work():
        nonlocal new_tier
        for item in items:
            price_id = item.get('price', {}).get('id', '')
            if price_id in price_to_tier:
                new_tier = price_to_tier[price_id]
                user_db.update_tier_only(user_id, new_tier, _commit=False)
                logger.info(f"Paddle: subscription.updated tier={new_tier} user={user_id}")
                break

    ran = user_db.process_paddle_event_atomic(event_id, user_id, _work)
    if not ran:
        logger.info(f"Paddle: event {event_id} already processed (no-op)")
        return
    # Sync the new tier to PostHog + EspoCRM (fire-and-forget, after commit).
    if new_tier:
        try:
            u = user_db.get_user_by_id(user_id)
            if u and u.get('email'):
                from carrac.integrations import on_tier_change
                on_tier_change(user_id, u['email'], old_tier, new_tier)
        except Exception as e:
            logger.warning(f"on_tier_change (paddle subscription.updated) failed: {e}")


def _handle_subscription_canceled(data: dict, event_id: str):
    """Log cancellation. Actual downgrade happens via check_subscription_expiry().

    notification-only (no entitlement changes), so the marker-only
    `mark_payment_processed` pattern is still safe here. We use the atomic
    helper for consistency with the other handlers.

    Fix: if subscription_expires_at is NULL the sweep (sweep_expired_subs.py)
    will never catch this user because of its ``IS NOT NULL`` predicate.  Set a
    fallback expiry of SUBSCRIPTION_GRACE_DAYS from now so the sweep can
    downgrade the user on the next run.
    """
    user_id, paddle_cid = _resolve_user_from_event(data)
    sub_id = data.get('id', '')

    _fallback_expiry_set = {'value': None}

    def _work():
        if user_id:
            from carrac.db_pool import adapt_query as _aq
            conn = user_db._get_conn()
            cursor = conn.cursor()
            cursor.execute(_aq("SELECT subscription_expires_at FROM users WHERE id = ?"), (user_id,))
            row = cursor.fetchone()
            if row and not row['subscription_expires_at']:
                # NULL expiry — sweep can't catch this; set a sweep-visible deadline
                from datetime import datetime, timedelta, timezone as _tz
                from user_db import SUBSCRIPTION_GRACE_DAYS
                fallback_expiry = datetime.now(_tz.utc) + timedelta(days=SUBSCRIPTION_GRACE_DAYS)
                cursor.execute(
                    _aq("UPDATE users SET subscription_expires_at = ? WHERE id = ?"),
                    (fallback_expiry.isoformat(), user_id),
                )
                _fallback_expiry_set['value'] = fallback_expiry.isoformat()
                logger.warning(
                    f"Paddle canceled: user {user_id} had NULL subscription_expires_at — "
                    f"set to {fallback_expiry.isoformat()} so sweep can downgrade"
                )

    ran = user_db.process_paddle_event_atomic(event_id, user_id or 0, _work)
    if not ran:
        logger.info(f"Paddle: event {event_id} already processed (no-op)")
        return

    logger.info(f"Paddle: subscription.canceled user={user_id} sub={sub_id} "
                f"(will expire at subscription_expires_at)")
    if _fallback_expiry_set['value']:
        send_telegram_message(
            f"⚠️ *Paddle SUBSCRIPTION CANCELED* (NULL expiry patched)\n"
            f"User: {user_id}\nSubscription: `{sub_id}`\n"
            f"Fallback expiry set to: {_fallback_expiry_set['value']}"
        )
    else:
        send_telegram_message(
            f"🚫 *Paddle SUBSCRIPTION CANCELED*\nUser: {user_id}\nSubscription: `{sub_id}`\n"
            f"Tier retained until billing period ends."
        )


def _handle_subscription_paused(data: dict, event_id: str):
    """Handle subscription.paused.

    Paddle semantics: a paused subscription stops the billing cycle but the
    CURRENT period remains active until it naturally expires. We treat pause
    identically to cancellation: the user keeps their tier until
    `subscription_expires_at` lapses, at which point `check_subscription_expiry`
    (or `sweep_expired_subs.py`) downgrades them to free. No new state to invent —
    the existing expiry + sweep machinery handles it.

    Mirrors `_handle_subscription_canceled`: no entitlement writes here, just
    logging and a Telegram notification.
    """
    user_id, paddle_cid = _resolve_user_from_event(data)
    sub_id = data.get('id', '')

    def _work():
        # No state writes — tier preserved until natural expiry (same as cancel).
        pass

    ran = user_db.process_paddle_event_atomic(event_id, user_id or 0, _work)
    if not ran:
        logger.info(f"Paddle: event {event_id} already processed (no-op)")
        return

    logger.info(
        f"Paddle: subscription.paused user={user_id} sub={sub_id} "
        f"(tier preserved until subscription_expires_at; sweep will downgrade)"
    )
    send_telegram_message(
        f"⏸️ *Paddle SUBSCRIPTION PAUSED*\nUser: {user_id}\nSubscription: `{sub_id}`\n"
        f"Tier retained until billing period ends."
    )


def _handle_subscription_resumed(data: dict, event_id: str):
    """Handle subscription resumed after pause — re-activate tier.

    Marker INSERT ordered AFTER `activate_subscription` so a failed
    activation doesn't silently flag the event as processed.
    """
    user_id, paddle_cid = _resolve_user_from_event(data)
    if not user_id:
        logger.warning(f"Paddle subscription.resumed: could not resolve user (customer={paddle_cid})")
        return

    sub_id = data.get('id', '')
    items = data.get('items', [])
    price_to_tier = Config.paddle_price_to_tier()

    activated_tier = {'tier': None}

    def _work():
        for item in items:
            price_id = item.get('price', {}).get('id', '')
            if price_id in price_to_tier:
                tier = price_to_tier[price_id]
                days = _derive_subscription_days(data)
                user_db.activate_subscription(
                    user_id, tier=tier, days=days, payment_id=sub_id, _commit=False,
                )
                activated_tier['tier'] = tier
                logger.info(f"Paddle: subscription.resumed tier={tier} user={user_id} sub={sub_id}")
                break

    ran = user_db.process_paddle_event_atomic(event_id, user_id, _work)
    if not ran:
        logger.info(f"Paddle: event {event_id} already processed (no-op)")
        return

    # Telegram notification AFTER commit — don't notify on rolled-back work.
    send_telegram_message(
        f"✅ *Paddle SUBSCRIPTION RESUMED*\nUser: {user_id}\nSubscription: `{sub_id}`",
    )


def _handle_subscription_past_due(data: dict, event_id: str):
    """Handle subscription.past_due — extend expiry by 7 days (Paddle dunning window).

    When Paddle marks a subscription past_due it begins retrying
    the charge for up to 7 days. We extend subscription_expires_at by 7 days
    so the SUBSCRIPTION_GRACE_DAYS window activates from this event rather than
    from the moment the original period lapsed.
    """
    from datetime import datetime, timedelta, timezone as _tz
    sub_id = data.get('id', '')
    user_id_pd, _ = _resolve_user_from_event(data)

    def _work():
        if user_id_pd:
            from carrac.db_pool import adapt_query as _aq
            conn = user_db._get_conn()
            cursor = conn.cursor()
            cursor.execute(
                _aq("SELECT subscription_expires_at FROM users WHERE id = ?"), (user_id_pd,)
            )
            row = cursor.fetchone()
            if row and row['subscription_expires_at']:
                try:
                    current_expiry = datetime.fromisoformat(row['subscription_expires_at'])
                    if current_expiry.tzinfo is None:
                        current_expiry = current_expiry.replace(tzinfo=_tz.utc)
                    # Extend from whichever is later: current expiry or now
                    base = max(current_expiry, datetime.now(_tz.utc))
                    new_expiry = base + timedelta(days=7)
                    cursor.execute(
                        _aq("UPDATE users SET subscription_expires_at = ? WHERE id = ?"),
                        (new_expiry.isoformat(), user_id_pd),
                    )
                    logger.info(
                        f"Paddle past_due: extended subscription for user {user_id_pd} "
                        f"to {new_expiry.isoformat()} (sub={sub_id})"
                    )
                except (ValueError, TypeError) as exc:
                    logger.warning(f"Paddle past_due: could not parse expiry for user {user_id_pd}: {exc}")

    ran = user_db.process_paddle_event_atomic(event_id, user_id_pd or 0, _work)
    if ran:
        logger.warning(f"Paddle: subscription past_due — sub_id={sub_id} user={user_id_pd}")
        send_telegram_message(
            f"⚠️ *Paddle PAST DUE*\nUser: {user_id_pd}\nSubscription: `{sub_id}`",
        )
    else:
        logger.info(f"Paddle: event {event_id} already processed (no-op)")


def _handle_transaction_refunded(data: dict, event_id: str):
    """Handle refund — reverse credits or downgrade subscription tier.

    Refund operations are also entitlement-affecting; if the
    clawback fails after the marker is set, the user keeps a refunded
    balance. Atomic helper guarantees the marker only sticks if the
    reversal commits.
    """
    user_id, paddle_cid = _resolve_user_from_event(data)
    if not user_id:
        logger.warning(f"Paddle transaction.refunded: could not resolve user (customer={paddle_cid})")
        send_telegram_message(
            f"🔴 *Paddle REFUND — UNRESOLVED USER*\n"
            f"Event: `{event_id}`\nCustomer: `{paddle_cid}`\n"
            f"Manual review required.",
        )
        return

    items = data.get('items', [])
    price_to_tier = Config.paddle_price_to_tier()
    price_to_credits = Config.paddle_price_to_credits()
    actions = []

    # Detect full vs partial refund.
    # Paddle sends totals under data['details']['totals']. Try multiple paths
    # defensively — payload shape may vary between billing v1 and v2 events.
    refund_amount_cents = 0
    original_amount_cents = 0
    try:
        totals = data.get('details', {}).get('totals', {})
        # 'grand_total' is the full transaction amount; 'refund' is the refunded portion.
        original_amount_cents = int(totals.get('grand_total') or totals.get('fee') or 0)
        refund_amount_cents = int(totals.get('refund') or totals.get('refunded_amount') or 0)
    except (ValueError, TypeError):
        pass
    # Fallback: check payments array
    if refund_amount_cents == 0:
        try:
            for payment in (data.get('payments') or []):
                refund_amount_cents += int(payment.get('refunded_amount') or 0)
                if original_amount_cents == 0:
                    original_amount_cents += int(payment.get('amount') or 0)
        except (ValueError, TypeError):
            pass

    is_full_refund = (
        refund_amount_cents == 0  # can't determine → treat as full (safe default)
        or (original_amount_cents > 0 and refund_amount_cents >= original_amount_cents)
    )

    def _work():
        for item in items:
            price = item.get('price', {})
            price_id = price.get('id', '')

            if price_id in price_to_tier:
                tier = price_to_tier[price_id]
                # Only downgrade on full refund
                user_db.refund_subscription(
                    user_id, full=is_full_refund,
                    refund_amount_cents=refund_amount_cents,
                    event_id=event_id, _commit=False,
                )
                if is_full_refund:
                    actions.append(f"downgraded from {tier} to free (full refund)")
                    logger.info(f"Paddle: full refund — downgraded user {user_id} from {tier} to free")
                else:
                    actions.append(
                        f"partial refund {refund_amount_cents}c of {original_amount_cents}c — "
                        f"tier preserved, audit row written"
                    )
                    logger.info(
                        f"Paddle: partial refund {refund_amount_cents}c/{original_amount_cents}c "
                        f"for user {user_id} — tier {tier} preserved"
                    )

            elif price_id in price_to_credits:
                credits = price_to_credits[price_id]
                user_db.clawback_ai_credits(user_id, credits, _commit=False)
                user_db.record_ai_deposit(
                    user_id, -credits, payment_id=event_id, source='paddle_refund', _commit=False,
                )
                actions.append(f"clawed back {credits}c")
                logger.info(f"Paddle: refund — clawed back {credits}c from user {user_id}")

            else:
                actions.append(f"unknown price_id={price_id}")
                logger.warning(f"Paddle refund: unknown price_id={price_id}")

    ran = user_db.process_paddle_event_atomic(event_id, user_id, _work)
    if not ran:
        logger.info(f"Paddle: event {event_id} already processed (no-op)")
        return

    action_str = ', '.join(actions) if actions else 'no items'
    send_telegram_message(
        f"🔴 *Paddle REFUND*\nUser: {user_id}\nEvent: `{event_id}`\nActions: {action_str}",
    )


def _handle_transaction_payment_failed(data: dict, event_id: str):
    """Handle failed payment — log and notify admin.

    notification-only; uses the atomic helper for consistency.
    """
    user_id, paddle_cid = _resolve_user_from_event(data)

    def _work():
        pass  # No state writes — notification only.

    ran = user_db.process_paddle_event_atomic(event_id, user_id or 0, _work)
    if not ran:
        logger.info(f"Paddle: event {event_id} already processed (no-op)")
        return

    sub_id = data.get('subscription_id', '')
    logger.warning(f"Paddle: payment failed — user={user_id} customer={paddle_cid} sub={sub_id}")

    send_telegram_message(
        f"⚠️ *Paddle PAYMENT FAILED*\nUser: {user_id or 'unknown'}\n"
        f"Customer: `{paddle_cid}`\nSubscription: `{sub_id}`",
    )


# ── Pending-event quarantine ────────────────────────────────

def _quarantine_paddle_event(paddle_cid: str, event_type: str, event_id: str, data: dict) -> None:
    """Store a paddle event that arrived before the customer mapping was confirmed.

    Drain happens in `_handle_customer_created` once the mapping is written.
    Idempotent: UNIQUE constraint on event_id; duplicate quarantine attempts are
    silently ignored.
    """
    import json as _json
    from carrac.db_pool import adapt_query as _aq, handle_integrity_error as _hie, get_conn, get_db_type
    try:
        if get_db_type() == 'postgresql':
            try:
                with get_conn() as conn:
                    conn.execute(
                        _aq('''INSERT INTO pending_paddle_events
                               (paddle_customer_id, event_id, event_type, event_json)
                               VALUES (?, ?, ?, ?)'''),
                        (paddle_cid, event_id, event_type, _json.dumps(data)),
                    )
            except _hie():
                pass  # UNIQUE violation — already quarantined; no-op
        else:
            conn = user_db._get_conn()
            try:
                conn.execute(
                    _aq('''INSERT INTO pending_paddle_events
                           (paddle_customer_id, event_id, event_type, event_json)
                           VALUES (?, ?, ?, ?)'''),
                    (paddle_cid, event_id, event_type, _json.dumps(data)),
                )
                conn.commit()
            except _hie():
                try: conn.rollback()
                except Exception: pass
        logger.info(f"Paddle: quarantined event {event_id} (type={event_type} customer={paddle_cid})")
    except Exception as exc:
        logger.error(f"Paddle: failed to quarantine event {event_id}: {exc}")


def _drain_pending_events(paddle_cid: str) -> None:
    """Replay queued pending_paddle_events for this customer now that the
    mapping is confirmed.

    Replays only 'transaction.completed' events for now (the only type we
    quarantine). Each event is dispatched through the normal handler so
    idempotency / atomic markers apply exactly as for live events.
    """
    import json as _json
    from carrac.db_pool import adapt_query as _aq, get_conn, get_db_type
    try:
        if get_db_type() == 'postgresql':
            with get_conn() as conn:
                rows = conn.execute(
                    _aq('''SELECT id, event_id, event_type, event_json
                           FROM pending_paddle_events
                           WHERE paddle_customer_id = ? AND processed_at IS NULL'''),
                    (paddle_cid,),
                ).fetchall()
        else:
            _conn = user_db._get_conn()
            try:
                rows = _conn.execute(
                    _aq('''SELECT id, event_id, event_type, event_json
                           FROM pending_paddle_events
                           WHERE paddle_customer_id = ? AND processed_at IS NULL'''),
                    (paddle_cid,),
                ).fetchall()
                _conn.rollback()
            except Exception:
                try: _conn.rollback()
                except Exception: pass
                raise

        if not rows:
            return

        for row in rows:
            eid = row['event_id']
            etype = row['event_type']
            try:
                edata = _json.loads(row['event_json'])
            except Exception:
                logger.error(f"Paddle: could not parse quarantined event {eid}; skipping")
                continue

            logger.info(f"Paddle: draining quarantined event {eid} (type={etype})")
            try:
                if etype == 'transaction.completed':
                    _handle_transaction_completed(edata, eid)
                else:
                    logger.warning(f"Paddle: no drain handler for quarantined event type {etype}")
                    continue

                # Mark as processed (separate RAII connection for each mark)
                if get_db_type() == 'postgresql':
                    with get_conn() as conn:
                        conn.execute(
                            _aq('''UPDATE pending_paddle_events
                                   SET processed_at = CURRENT_TIMESTAMP
                                   WHERE id = ?'''),
                            (row['id'],),
                        )
                else:
                    _conn = user_db._get_conn()
                    try:
                        _conn.execute(
                            _aq('''UPDATE pending_paddle_events
                                   SET processed_at = CURRENT_TIMESTAMP
                                   WHERE id = ?'''),
                            (row['id'],),
                        )
                        _conn.commit()
                    except Exception:
                        try: _conn.rollback()
                        except Exception: pass
                        raise
            except Exception as exc:
                logger.error(f"Paddle: drain failed for event {eid}: {exc}")
    except Exception as exc:
        logger.error(f"Paddle: _drain_pending_events failed for customer {paddle_cid}: {exc}")


# ── Webhook inbox ────────────────────────────────────────────

MAX_INBOX_RETRIES = 3

def _inbox_record(event_id: str, event_type: str, payload: dict) -> None:
    """Write an inbox row for this event (idempotent via UNIQUE event_id).

    Called at the TOP of the webhook route before any processing begins.
    If writing the row fails (e.g. DB down), the function logs and returns
    without raising so we still try to process the event.
    """
    import json as _json
    from carrac.db_pool import adapt_query as _aq, handle_integrity_error as _hie, get_conn, get_db_type
    try:
        if get_db_type() == 'postgresql':
            try:
                with get_conn() as conn:
                    conn.execute(
                        _aq('''INSERT INTO paddle_webhook_inbox
                               (event_id, event_type, payload)
                               VALUES (?, ?, ?)'''),
                        (event_id, event_type, _json.dumps(payload)),
                    )
            except _hie():
                pass  # UNIQUE violation — already in inbox; no-op
        else:
            conn = user_db._get_conn()
            try:
                conn.execute(
                    _aq('''INSERT INTO paddle_webhook_inbox
                           (event_id, event_type, payload)
                           VALUES (?, ?, ?)'''),
                    (event_id, event_type, _json.dumps(payload)),
                )
                conn.commit()
            except _hie():
                try: conn.rollback()
                except Exception: pass
    except Exception as exc:
        logger.error("Paddle inbox: failed to record event %s (%s): %s", event_id, event_type, exc)


def _inbox_mark_processed(event_id: str) -> None:
    """Mark an inbox row as successfully processed."""
    from carrac.db_pool import adapt_query as _aq, get_conn, get_db_type
    try:
        if get_db_type() == 'postgresql':
            with get_conn() as conn:
                conn.execute(
                    _aq('''UPDATE paddle_webhook_inbox
                           SET processed_at = CURRENT_TIMESTAMP
                           WHERE event_id = ?'''),
                    (event_id,),
                )
        else:
            conn = user_db._get_conn()
            try:
                conn.execute(
                    _aq('''UPDATE paddle_webhook_inbox
                           SET processed_at = CURRENT_TIMESTAMP
                           WHERE event_id = ?'''),
                    (event_id,),
                )
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
                raise
    except Exception as exc:
        logger.error("Paddle inbox: failed to mark %s processed: %s", event_id, exc)


def _inbox_record_error(event_id: str, error: str) -> int:
    """Increment retry_count and store last_error. Returns the new retry_count."""
    from carrac.db_pool import adapt_query as _aq, get_conn, get_db_type
    try:
        if get_db_type() == 'postgresql':
            with get_conn() as conn:
                conn.execute(
                    _aq('''UPDATE paddle_webhook_inbox
                           SET last_error = ?, retry_count = retry_count + 1
                           WHERE event_id = ?'''),
                    (error[:2000], event_id),
                )
                row = conn.execute(
                    _aq('SELECT retry_count FROM paddle_webhook_inbox WHERE event_id = ?'),
                    (event_id,),
                ).fetchone()
            return row['retry_count'] if row else MAX_INBOX_RETRIES + 1
        else:
            conn = user_db._get_conn()
            try:
                conn.execute(
                    _aq('''UPDATE paddle_webhook_inbox
                           SET last_error = ?, retry_count = retry_count + 1
                           WHERE event_id = ?'''),
                    (error[:2000], event_id),
                )
                conn.commit()
                row = conn.execute(
                    _aq('SELECT retry_count FROM paddle_webhook_inbox WHERE event_id = ?'),
                    (event_id,),
                ).fetchone()
                conn.commit()
                return row['retry_count'] if row else MAX_INBOX_RETRIES + 1
            except Exception:
                try: conn.rollback()
                except Exception: pass
                raise
    except Exception as exc:
        logger.error("Paddle inbox: failed to record error for %s: %s", event_id, exc)
        return MAX_INBOX_RETRIES + 1


# ── Helpers ────────────────────────────────────────────────────────────

def _paddle_api_base():
    return ('https://api.paddle.com'
            if Config.PADDLE_ENVIRONMENT == 'production'
            else 'https://sandbox-api.paddle.com')


def _paddle_headers():
    return {
        'Authorization': f'Bearer {Config.PADDLE_API_KEY}',
        'Content-Type': 'application/json',
    }
