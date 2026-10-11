"""
Admin routes: user management, invite management, security monitoring, feedback.
"""

import logging
import threading

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, jsonify, g,
)

from carrac.audit import audit_log
from carrac.auth.decorators import admin_required, TIER_LIMITS, invalidate_tier_config_cache
from carrac.tiers import ALL_TIERS, ASSIGNABLE_TIERS, CONFIGURABLE_TIERS
from carrac.db_pool import get_conn, adapt_query
from security import validate_password, security_logger
from user_db import user_db

logger = logging.getLogger('carrac.admin')

admin_bp = Blueprint('admin', __name__)

# CSP violation tracking
_csp_violation_count = 0
_csp_lock = threading.Lock()


@admin_bp.before_request
def _inject_admin_globals():
    """Make feedback count available to all admin templates via g."""
    if session.get('is_admin'):
        try:
            g.new_feedback_count = user_db.count_new_feedback()
        except Exception:
            g.new_feedback_count = 0


@admin_bp.route('/admin/intelligence')
def admin_intelligence():
    """Legacy URL — redirect to /intelligence (smart-routed by tier)."""
    return redirect(url_for('dashboard.intelligence_page'), code=301)


@admin_bp.route('/admin')
@admin_required
def admin_panel():
    """Admin overview with KPIs and invite management."""
    from datetime import datetime as dt, timedelta, timezone
    users = user_db.list_users()
    invites = user_db.list_invites(include_used=False)
    platform_econ = user_db.get_platform_economics(30)

    # User KPIs
    cutoff_7d = (dt.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    with get_conn() as conn:
        active_7d = conn.execute(adapt_query(
            'SELECT COUNT(DISTINCT id) as cnt FROM users WHERE last_active >= ? AND id > 0'
        ), (cutoff_7d,)).fetchone()
        new_7d = conn.execute(adapt_query(
            'SELECT COUNT(*) as cnt FROM users WHERE created_at >= ? AND id > 0'
        ), (cutoff_7d,)).fetchone()

    return render_template('admin.html',
        invites=invites,
        total_users=len(users),
        active_users_7d=active_7d['cnt'] if active_7d else 0,
        new_users_7d=new_7d['cnt'] if new_7d else 0,
        platform_econ=platform_econ,
    )


@admin_bp.route('/admin/users')
@admin_required
def admin_users_page():
    """Dedicated users management page."""
    users = user_db.list_users()
    return render_template('admin_users.html', users=users)


@admin_bp.route('/admin/user/<int:user_id>')
@admin_required
def admin_user_detail(user_id: int):
    """Detailed admin view of a single user."""
    user = user_db.get_user(user_id)
    if not user:
        flash('User not found', 'error')
        return redirect(url_for('admin.admin_panel'))

    # Watchlist & preferences
    watchlist = user_db.get_watchlist(user_id)
    preferences = user_db.get_preferences(user_id)

    # AI credits & usage
    ai_balance = user_db.get_ai_balance(user_id)
    ai_stats = user_db.get_ai_usage_stats(user_id)
    user_economics = user_db.get_user_economics(user_id)
    conversations = user_db.get_conversations(user_id, limit=10)

    # Recent AI usage log (last 20 entries)
    with get_conn() as conn:
        ai_log = [dict(r) for r in conn.execute(adapt_query(
            '''SELECT feature, asset, input_tokens, output_tokens, cost_cents,
                      charged_cents, model, created_at
               FROM ai_usage_log WHERE user_id = ?
               ORDER BY created_at DESC LIMIT 20'''), (user_id,)
        ).fetchall()]

        # AI deposits
        ai_deposits = [dict(r) for r in conn.execute(adapt_query(
            '''SELECT amount_cents, crypto_amount, crypto_currency, status, created_at
               FROM ai_deposits WHERE user_id = ?
               ORDER BY created_at DESC LIMIT 10'''), (user_id,)
        ).fetchall()]

        # Feedback submitted by this user
        user_feedback = [dict(r) for r in conn.execute(adapt_query(
            '''SELECT id, type, area, description, status, created_at
               FROM feedback WHERE user_id = ?
               ORDER BY created_at DESC LIMIT 20'''), (user_id,)
        ).fetchall()]

    # Widget tokens + API keys
    widget_tokens = user_db.list_widget_tokens(user_id)
    api_keys = user_db.list_api_keys(user_id)

    # Drawings & scenarios (from content_db)
    drawing_stats = {}
    scenarios = []
    open_trades = []
    journal_stats = {}
    try:
        from carrac.content_db import get_content_db
        cdb = get_content_db()

        with get_conn() as db_conn:
            # Drawing counts by type
            rows = db_conn.execute(adapt_query(
                '''SELECT drawing_type, COUNT(*) as cnt
                   FROM drawings WHERE user_id = ? AND scenario_id IS NULL
                   GROUP BY drawing_type'''), (user_id,)
            ).fetchall()
            drawing_stats = {r['drawing_type']: r['cnt'] for r in rows}

            # Scenarios (top-level)
            scenario_rows = db_conn.execute(adapt_query(
                '''SELECT id, asset, timeframe, label, metadata, created_at, updated_at
                   FROM drawings WHERE user_id = ? AND drawing_type = 'scenario' AND scenario_id IS NULL
                   ORDER BY updated_at DESC LIMIT 20'''), (user_id,)
            ).fetchall()
            scenarios = [dict(r) for r in scenario_rows]

        # Open trades
        open_trades = cdb.get_open_trades(user_id)

        # Journal stats (90 days)
        journal_stats = cdb.get_journal_stats(user_id, days=90)
    except Exception as e:
        logger.warning(f"Error loading drawing data for user {user_id}: {e}")

    # Price alerts
    alerts = []
    try:
        from push_notifications import get_user_alerts
        alerts = get_user_alerts(str(user_id), active_only=True)
    except Exception:
        pass

    return render_template('admin_user_detail.html',
        detail_user=user,
        watchlist=watchlist,
        preferences=preferences,
        ai_balance=ai_balance,
        ai_stats=ai_stats,
        user_economics=user_economics,
        ai_log=ai_log,
        ai_deposits=ai_deposits,
        conversations=conversations,
        widget_tokens=widget_tokens,
        api_keys=api_keys,
        user_feedback=user_feedback,
        drawing_stats=drawing_stats,
        scenarios=scenarios,
        open_trades=open_trades,
        journal_stats=journal_stats,
        alerts=alerts,
    )


@admin_bp.route('/admin/invite/email', methods=['POST'])
@admin_required
def admin_create_email_invite():
    """Add email to whitelist."""
    email = request.form.get('email', '').strip().lower()
    if not email or '@' not in email:
        flash('Valid email required', 'error')
        return redirect(url_for('admin.admin_panel'))

    if user_db.create_email_invite(email, session['user_id']):
        logger.info(f"Admin {session['email']} whitelisted: {email}")
        flash(f'Email whitelisted: {email}', 'success')
    else:
        flash('Failed to create invite', 'error')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/invite/code', methods=['POST'])
@admin_required
def admin_generate_invite_code():
    """Generate an invite code."""
    code = user_db.generate_invite_code(session['user_id'])
    if code:
        logger.info(f"Admin {session['email']} generated invite code: {code}")
        flash(f'Invite code generated: {code}', 'success')
    else:
        flash('Failed to generate code', 'error')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/invite/<int:invite_id>/delete', methods=['POST'])
@admin_required
def admin_delete_invite(invite_id: int):
    """Delete an unused invite."""
    if user_db.delete_invite(invite_id):
        flash('Invite deleted', 'success')
    else:
        flash('Failed to delete invite (may already be used)', 'error')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/user/<int:user_id>/toggle', methods=['POST'])
@admin_required
def admin_toggle_user(user_id: int):
    """Activate/deactivate a user."""
    user = user_db.get_user(user_id)
    if not user:
        flash('User not found', 'error')
        return redirect(url_for('admin.admin_panel'))

    # Prevent self-deactivation
    if user_id == session['user_id']:
        flash('Cannot deactivate yourself', 'error')
        return redirect(url_for('admin.admin_panel'))

    new_status = not user['is_active']
    if user_db.set_user_active(user_id, new_status):
        action = 'activated' if new_status else 'deactivated'
        logger.info(f"Admin {session['email']} {action} user: {user['email']}")
        flash(f"User {user['email']} {action}", 'success')
        # Kill the user's active sessions immediately on deactivation so an
        # abuser can't keep hammering endpoints for up to 5 minutes (the
        # session-check refresh interval). API-key access is already blocked
        # in user_db.verify_api_key_token() on is_active=False.
        if not new_status:
            from carrac.auth.routes import _invalidate_user_sessions
            try:
                _invalidate_user_sessions(user_id)
            except Exception as e:
                logger.warning(f"Session invalidation failed for user_id={user_id}: {e}")
    else:
        flash('Failed to update user', 'error')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/user/<int:user_id>/tier', methods=['POST'])
@admin_required
def admin_set_tier(user_id: int):
    """Set a user's subscription tier."""
    user = user_db.get_user(user_id)
    if not user:
        flash('User not found', 'error')
        return redirect(url_for('admin.admin_panel'))

    if user.get('is_admin'):
        flash('Cannot change admin tier', 'error')
        return redirect(url_for('admin.admin_panel'))

    tier = request.form.get('tier', '')
    if tier not in ASSIGNABLE_TIERS:
        flash('Invalid tier', 'error')
        return redirect(url_for('admin.admin_panel'))

    old_tier = user.get('tier', 'free')
    if user_db.set_user_tier(user_id, tier):
        logger.info(f"Admin {session['email']} set tier for {user['email']}: {tier}")
        # Persist audit trail
        audit_log.log(
            action=audit_log.TIER_OVERRIDE,
            actor=session.get('user_id'),
            subject=user_id,
            before={'tier': old_tier},
            after={'tier': tier},
            source='admin_ui',
            note=f"set by {session['email']}",
        )
        # Sync the tier change to PostHog + EspoCRM (fire-and-forget).
        try:
            from carrac.integrations import on_tier_change
            on_tier_change(user_id, user['email'], old_tier, tier)
        except Exception as e:
            logger.warning(f"on_tier_change (admin) failed: {e}")
        flash(f"Tier for {user['email']} set to {tier}", 'success')
    else:
        flash('Failed to update tier', 'error')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/user/<int:user_id>/adjust-credits', methods=['POST'])
@admin_required
def admin_adjust_credits(user_id: int):
    """Add or deduct credits for a user."""
    user = user_db.get_user(user_id)
    if not user:
        flash('User not found', 'error')
        return redirect(url_for('admin.admin_panel'))

    try:
        amount_cents = int(request.form.get('amount_cents', 0))
    except (ValueError, TypeError):
        flash('Invalid amount', 'error')
        return redirect(url_for('admin.admin_user_detail', user_id=user_id))

    if amount_cents == 0:
        flash('Amount cannot be zero', 'error')
        return redirect(url_for('admin.admin_user_detail', user_id=user_id))

    reason = request.form.get('reason', '').strip() or 'Admin adjustment'

    if amount_cents > 0:
        old_balance = user_db.get_ai_balance(user_id)
        user_db.add_ai_credits(user_id, amount_cents)
        user_db.record_ai_deposit(user_id, amount_cents, payment_id=None, source='admin')
        logger.info(f"Admin {session['email']} added {amount_cents}c to user {user['email']}: {reason}")
        # Persist audit trail
        audit_log.log(
            action=audit_log.ADD_CREDITS,
            actor=session.get('user_id'),
            subject=user_id,
            before={'balance_cents': old_balance},
            after={'balance_cents': old_balance + amount_cents},
            source='admin_ui',
            note=reason,
        )
        flash(f"Added ${amount_cents/100:.2f} credits to {user['email']}", 'success')
    else:
        abs_amount = abs(amount_cents)
        old_balance = user_db.get_ai_balance(user_id)
        if user_db.deduct_ai_credits(user_id, abs_amount):
            user_db.record_ai_deposit(user_id, -abs_amount, payment_id=None, source='admin_deduct')
            logger.info(f"Admin {session['email']} deducted {abs_amount}c from user {user['email']}: {reason}")
            # Persist audit trail
            audit_log.log(
                action=audit_log.DEDUCT_CREDITS,
                actor=session.get('user_id'),
                subject=user_id,
                before={'balance_cents': old_balance},
                after={'balance_cents': max(0, old_balance - abs_amount)},
                source='admin_ui',
                note=reason,
            )
            flash(f"Deducted ${abs_amount/100:.2f} credits from {user['email']}", 'success')
        else:
            flash(f"Insufficient balance to deduct ${abs_amount/100:.2f}", 'error')

    return redirect(url_for('admin.admin_user_detail', user_id=user_id))


@admin_bp.route('/admin/user/<int:user_id>/reset-password', methods=['POST'])
@admin_required
def admin_reset_password(user_id: int):
    """Reset a user's password."""
    user = user_db.get_user(user_id)
    if not user:
        flash('User not found', 'error')
        return redirect(url_for('admin.admin_panel'))

    new_password = request.form.get('new_password', '')
    password_valid, password_error = validate_password(new_password)
    if not password_valid:
        flash(password_error, 'error')
        return redirect(url_for('admin.admin_panel'))

    if user_db.update_password(user_id, new_password):
        security_logger.log_password_reset(user['email'], session['email'], request.remote_addr or '0.0.0.0')
        flash(f"Password reset for {user['email']}", 'success')
    else:
        flash('Failed to reset password', 'error')
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/admin/impersonate-tier', methods=['POST'])
@admin_required
def admin_impersonate_tier():
    """Switch admin's own session tier for testing. Session-only, no DB changes.

    CSRF is enforced — the admin UI posts with X-CSRFToken. Impersonation
    sets a session flag so track_user_activity respects the impersonated
    tier (otherwise the 60s DB tier refresh overwrites it within a minute).
    """
    tier = (request.json or {}).get('tier', 'admin')
    if tier not in ALL_TIERS:
        return jsonify({'error': 'Invalid tier'}), 400
    session['tier'] = tier
    session['_impersonating_tier'] = True
    logger.info(f"Admin {session['email']} switched session tier to: {tier}")
    return jsonify({'ok': True, 'tier': tier})


# ==================== Feedback ====================


def _notify_feedback_submission(fb_id: int, fb_type: str, area: str, description: str, session) -> None:
    """Telegram + ntfy notifier for new feedback."""
    email = session.get('email', 'anonymous')
    try:
        user = user_db.get_user(session.get('user_id')) if session.get('user_id') else None
        tier = (user or {}).get('tier', 'free')
    except Exception:
        tier = 'unknown'

    body_preview = (description or '')[:300]
    area_label = area or 'general'
    emoji = 'bug' if fb_type == 'bug' else 'star'

    # Telegram (channel the operator watches for pager-style alerts)
    try:
        from carrac.notifications.telegram import send_telegram_message
        # No address and no body text here. Telegram is
        # api.telegram.org — a third-country processor with no DPA and no
        # mention in the privacy policy — and this was sending the submitter's
        # email plus up to 300 characters of their own words to it. The tier
        # and area are enough for the operator to triage; the full content is
        # in the ntfy push below (self-hosted) and in the admin feedback view.
        telegram_text = (
            f"Feedback #{fb_id} ({fb_type}) [{tier}]\n"
            f"Area: {area_label}\n\nContent in ntfy / the admin feedback view."
        )
        send_telegram_message(telegram_text, parse_mode=None)
    except Exception as e:
        logger.warning(f"Telegram feedback notify failed: {e}")

    # ntfy (phone push)
    try:
        from carrac.notifications.ntfy import send_ntfy
        send_ntfy(
            body=f"{email} ({tier}) — {area_label}\n{body_preview}",
            title=f"Carrac feedback #{fb_id}",
            priority='3',
            tags=emoji,
        )
    except Exception as e:
        logger.warning(f"ntfy feedback notify failed: {e}")

    # Optional EspoCRM Case — only if configured. Skipped silently otherwise.
    try:
        import os, requests
        espo_url = os.environ.get('ESPOCRM_URL')
        espo_key = os.environ.get('ESPOCRM_API_KEY')
        if espo_url and espo_key:
            payload = {
                'name': f"Feedback #{fb_id} — {area_label}",
                'description': description,
                'type': 'Question' if fb_type == 'feature' else 'Incident',
                'priority': 'Normal',
                'status': 'New',
                'contactName': email,
            }
            requests.post(
                f"{espo_url.rstrip('/')}/api/v1/Case",
                json=payload,
                headers={'X-Api-Key': espo_key},
                timeout=5,
            )
    except Exception as e:
        logger.warning(f"EspoCRM case create failed: {e}")


@admin_bp.route('/admin/feedback')
@admin_required
def admin_feedback():
    """Admin view for feedback submissions."""
    feedback = user_db.list_feedback()
    return render_template('admin_feedback.html', feedback=feedback)


@admin_bp.route('/admin/feedback/<int:feedback_id>/status', methods=['POST'])
@admin_required
def admin_update_feedback_status(feedback_id: int):
    """Update feedback status."""
    status = request.form.get('status', '')
    if status not in ('new', 'reviewed', 'done', 'dismissed'):
        flash('Invalid status', 'error')
        return redirect(url_for('admin.admin_feedback'))
    if user_db.update_feedback_status(feedback_id, status):
        flash(f'Feedback #{feedback_id} marked as {status}', 'success')
    else:
        flash('Failed to update', 'error')
    return redirect(url_for('admin.admin_feedback'))


# ------------------------------------------------------------------
# Tier Configuration
# ------------------------------------------------------------------

@admin_bp.route('/admin/tiers')
@admin_required
def admin_tiers():
    """Admin page to view/edit tier configurations."""
    from carrac.auth.decorators import get_tier_limits

    # Build effective limits for each tier (hardcoded + DB overrides)
    tiers_data = {}
    _tier_display_order = ['free', 'member', 'tester', 'premium', 'admin']
    for tier_name in _tier_display_order:
        tiers_data[tier_name] = get_tier_limits(tier_name)

    # Get all keys (union of all tiers)
    all_keys = sorted(set(k for limits in tiers_data.values() for k in limits
                          if k != 'blocked_tfs'))

    # Get DB overrides to show which values are customized
    db_configs = user_db.get_all_tier_configs()

    # Get all user overrides
    overrides = []
    try:
        with get_conn() as conn:
            rows = conn.execute(adapt_query(
                '''SELECT o.user_id, u.email, u.tier, o.override_json, o.reason
                   FROM user_tier_overrides o JOIN users u ON o.user_id = u.id'''
            )).fetchall()
            import json as _json
            for row in rows:
                ov = _json.loads(row['override_json']) if isinstance(row['override_json'], str) else row['override_json']
                overrides.append({
                    'user_id': row['user_id'],
                    'email': row['email'],
                    'tier': row['tier'],
                    'overrides': ov,
                    'reason': row['reason'],
                })
    except Exception:
        pass

    return render_template('admin_tiers.html',
        tiers_data=tiers_data,
        all_keys=all_keys,
        db_configs=db_configs,
        overrides=overrides,
    )


@admin_bp.route('/admin/tiers/<tier_name>', methods=['POST'])
@admin_required
def admin_update_tier_config(tier_name):
    """Update a tier's limits from the admin form."""
    if tier_name not in CONFIGURABLE_TIERS:
        flash('Cannot modify this tier', 'error')
        return redirect(url_for('admin.admin_tiers'))

    # Build expected types from hardcoded defaults (free tier has all keys)
    valid_keys = {}
    for k, v in TIER_LIMITS['free'].items():
        if k == 'blocked_tfs':
            continue  # Excluded from admin form
        valid_keys[k] = type(v)  # int, bool, or str

    data = request.form.to_dict()
    limits = {}
    rejected = []
    for key, value in data.items():
        if key == 'csrf_token':
            continue
        if key not in valid_keys:
            rejected.append(key)
            continue
        expected = valid_keys[key]
        if expected is bool:
            if value.lower() in ('true', 'false'):
                limits[key] = value.lower() == 'true'
            else:
                rejected.append(f"{key} (expected bool)")
        elif expected is int:
            try:
                v = int(value)
                if v < 0:
                    rejected.append(f"{key} (negative)")
                else:
                    limits[key] = v
            except ValueError:
                rejected.append(f"{key} (expected int)")
        else:  # str
            limits[key] = value

    if rejected:
        flash(f'Rejected invalid keys: {", ".join(rejected)}', 'warning')

    if not limits:
        flash('No valid changes to save', 'error')
        return redirect(url_for('admin.admin_tiers'))

    admin_id = session.get('user_id')
    if user_db.set_tier_config(tier_name, limits, admin_id):
        invalidate_tier_config_cache()
        logger.info(f"Admin {session.get('email')} updated tier config for {tier_name}: {limits}")
        flash(f'Tier config for {tier_name} updated ({len(limits)} keys)', 'success')
    else:
        flash('Failed to update tier config', 'error')
    return redirect(url_for('admin.admin_tiers'))


@admin_bp.route('/admin/user/<int:user_id>/overrides', methods=['POST'])
@admin_required
def admin_set_user_overrides(user_id):
    """Set per-user limit overrides."""
    import json as _json
    user = user_db.get_user(user_id)
    if not user:
        flash('User not found', 'error')
        return redirect(url_for('admin.admin_tiers'))

    overrides_raw = request.form.get('overrides_json', '{}')
    reason = request.form.get('reason', '')
    try:
        overrides = _json.loads(overrides_raw)
    except _json.JSONDecodeError:
        flash('Invalid JSON', 'error')
        return redirect(url_for('admin.admin_tiers'))

    admin_id = session.get('user_id')
    if user_db.set_user_overrides(user_id, overrides, admin_id, reason):
        logger.info(f"Admin set overrides for user {user_id}: {overrides} reason={reason}")
        flash(f'Overrides set for {user.get("email")}', 'success')
    else:
        flash('Failed to set overrides', 'error')
    return redirect(url_for('admin.admin_tiers'))


@admin_bp.route('/admin/user/<int:user_id>/overrides/clear', methods=['POST'])
@admin_required
def admin_clear_user_overrides(user_id):
    """Remove per-user overrides."""
    if user_db.clear_user_overrides(user_id):
        flash('Overrides cleared', 'success')
    else:
        flash('Failed to clear overrides', 'error')
    return redirect(url_for('admin.admin_tiers'))


# ── Cost Dashboard ────────────────────────────────────────

@admin_bp.route('/admin/costs')
@admin_required
def admin_costs_page():
    """Admin cost analytics dashboard."""
    return render_template('admin_costs.html')


@admin_bp.route('/admin/billing')
@admin_required
def admin_billing_page():
    """Billing / subscription overview dashboard."""
    return render_template('admin_billing.html')


@admin_bp.route('/admin/waitlist')
@admin_required
def admin_waitlist_page():
    """Waitlist management dashboard."""
    return render_template('admin_waitlist.html')


@admin_bp.route('/admin/usage')
@admin_required
def admin_usage_page():
    """Per-user API / MCP usage dashboard."""
    return render_template('admin_usage.html')


@admin_bp.route('/admin/intents')
@admin_required
def admin_intents_page():
    """MCP request-intent telemetry dashboard (live feed +
    pinned unmet-needs panel)."""
    return render_template('admin_intents.html')


# ── System-Health Dashboard ────────────────────────────────────────

@admin_bp.route('/admin/health')
@admin_required
def admin_health_page():
    """System-health matrix — green/amber/red component status."""
    from carrac.admin.health_checks import run_all_checks, GREEN, AMBER, RED

    refresh = request.args.get('refresh') == '1'
    checks  = run_all_checks(force=refresh)

    # Derive overall badge from worst component
    statuses = [c['status'] for c in checks]
    if RED in statuses:
        overall = RED
    elif AMBER in statuses:
        overall = AMBER
    else:
        overall = GREEN

    # Group by category for the template
    from collections import OrderedDict
    categories: dict = OrderedDict()
    for c in checks:
        cat = c['category']
        categories.setdefault(cat, []).append(c)

    return render_template(
        'admin_health.html',
        checks=checks,
        categories=categories,
        overall=overall,
        GREEN=GREEN, AMBER=AMBER, RED=RED,
    )


@admin_bp.route('/api/admin/health')
@admin_required
def api_admin_health():
    """JSON health endpoint — for Kuma/Watchdog external monitoring."""
    from carrac.admin.health_checks import run_all_checks, GREEN, AMBER, RED

    refresh = request.args.get('refresh') == '1'
    checks  = run_all_checks(force=refresh)

    statuses = [c['status'] for c in checks]
    if RED in statuses:
        overall = RED
    elif AMBER in statuses:
        overall = AMBER
    else:
        overall = GREEN

    return jsonify({
        'overall': overall,
        'checks':  checks,
    }), (200 if overall != RED else 503)


