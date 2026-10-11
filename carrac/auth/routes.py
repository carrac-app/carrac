"""
Authentication routes: login, register, logout, password reset, email verification,
account deletion & data export.
"""

import hashlib
import json
import logging
import os
import secrets
import time

from flask import (
    Blueprint, render_template, request, redirect, url_for, session,
    jsonify, current_app, Response,
)

from carrac.extensions import limiter, csrf
from security import (
    brute_force_protection, security_logger, validate_password,
    validate_email, add_timing_noise, sanitize_string,
)
from user_db import user_db
from carrac.integrations import on_user_signup, on_login, on_funnel_event, on_activity

logger = logging.getLogger('carrac.auth')

auth_bp = Blueprint('auth', __name__)


def _ensure_private_data_dir() -> None:
    """Create data/private/ with restrictive perms at startup.

    Called once from create_app (via blueprint registration) so the directory
    is guaranteed to exist before the first waitlist write, and before any
    static-file middleware can be configured to serve it.
    """
    try:
        data_dir = os.environ.get('DATA_DIR', 'data')
        private_dir = os.path.join(data_dir, 'private')
        os.makedirs(private_dir, exist_ok=True)
        os.chmod(private_dir, 0o700)
    except Exception as exc:
        logger.warning("Could not create/chmod data/private: %s", exc)


# Eagerly enforce the private-data directory at import time so it exists
# even if no waitlist entry is ever written.
_ensure_private_data_dir()


@auth_bp.before_app_request
def _activity_heartbeat_hook():
    """B4: lightweight per-request activity heartbeat for logged-in users.

    on_activity() is itself throttled + fire-and-forget, but this hook is
    ALSO fully wrapped — it runs before every non-static request across the
    whole app (before_app_request, not just auth routes), so it must never
    raise or add latency.
    """
    try:
        if request.endpoint == 'static' or (request.path or '').startswith('/static/'):
            return
        user_id = session.get('user_id')
        email = session.get('email')
        if not user_id or not email:
            return
        on_activity(user_id, email)
    except Exception:
        pass


# ── Helpers ──────────────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    """SHA-256 hash a token for storage (same pattern as widget/auth.py)."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def signups_paused() -> bool:
    """Is new-user creation disabled?

    Two levers:
    - `SIGNUPS_ENABLED` env var (operator kill switch — no redeploy needed).
    - PostHog feature flag `signups_paused` (runtime toggle from admin).

    Env always wins. A missing env var defaults to signups enabled.
    """
    env_val = (os.environ.get('SIGNUPS_ENABLED', 'true') or '').strip().lower()
    if env_val in ('0', 'false', 'no', 'off'):
        return True
    try:
        from carrac.integrations import is_signups_paused_flag
        if is_signups_paused_flag():
            return True
    except Exception:
        pass
    return False


def _log_beta_waitlist(email: str, ip_address: str) -> None:
    """Capture an anonymous waitlist email when signups are paused. Writes
    to the data/private dir as JSONL so the operator can fold them in when
    signups reopen, and pings Telegram + ntfy so nothing sits unnoticed.

    Waitlist data contains PII (email + IP).  Written to
    data/private/ (mode 0o700) with the file itself at 0o600 so the
    directory is not web-reachable and the file is not world-readable.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    entry = {
        'ts': _dt.now(_tz.utc).replace(tzinfo=None).isoformat() + 'Z',
        'email': email,
        'ip': ip_address,
    }
    try:
        # The path comes from user_db so this writer and the eraser
        # (`user_db.delete_from_waitlist`) cannot drift apart — a second copy of
        # this join is how one store gets cleared and the other quietly does not.
        path = user_db.beta_waitlist_path()
        private_dir = os.path.dirname(path)
        # Create data/private/ with restrictive permissions if missing.
        os.makedirs(private_dir, exist_ok=True)
        try:
            os.chmod(private_dir, 0o700)
        except Exception:
            pass
        with open(path, 'a', encoding='utf-8') as f:
            f.write(_json.dumps(entry) + '\n')
        # Tighten file permissions on every write (covers the creation call).
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception as e:
        logger.error("Beta waitlist: signup could not be appended to the waitlist file: %s", e)
    try:
        from carrac.notifications.telegram import send_telegram_message
        # No address here. Telegram is api.telegram.org — a
        # third-country processor with no DPA, undisclosed in the privacy
        # policy — and this line was sending every signup email to it in
        # plaintext. The alert's job is "someone signed up", not "who": the
        # address is in the JSONL above and in the ntfy ping below, and ntfy
        # is self-hosted on the operator's own infrastructure. Dropping it here
        # removes the transfer outright rather than documenting one.
        send_telegram_message("New beta waitlist signup — address in ntfy / "
                              "the admin waitlist view", parse_mode=None)
    except Exception as e:
        logger.warning(f"waitlist telegram failed: {e}")
    try:
        from carrac.notifications.ntfy import send_ntfy
        send_ntfy(body=email, title='Carrac beta waitlist', priority='2')
    except Exception as e:
        logger.warning(f"waitlist ntfy failed: {e}")


# ── Brief Subscribe (public, /brief email-capture band) ─────────────

@auth_bp.route('/api/brief-subscribe', methods=['POST'])
@csrf.exempt
@limiter.limit("5 per minute")
def brief_subscribe():
    """Capture an email from the public /brief page's "be first" band.

    Reuses the beta-waitlist infra (`user_db.add_to_waitlist` + the same
    day-0 PostHog/EspoCRM wiring `on_waitlist_join` gives the register-page
    waitlist), tagged with source='brief-digest' so it's distinguishable in
    the admin /admin/waitlist view. This is a "be first" list, not a live
    subscription — email delivery doesn't exist yet, so the copy on /brief
    is deliberately honest about that; this endpoint just records interest.

    CSRF-exempt (like `heartbeat()` above): /brief is a single Redis-cached
    HTML blob served byte-identical to every anonymous visitor for up to 10
    minutes (see `_BRIEF_CACHE_KEY` in carrac/reports/routes.py), so a
    session-bound `{{ csrf_token() }}` baked into that cached render would
    not match a later visitor's own session — the JSON body + rate limit are
    the actual defenses here, same as the other public JSON POSTs.
    """
    ip_address = request.remote_addr or '0.0.0.0'
    data = request.get_json(silent=True)
    if data is None:
        data = request.form
    email = (data.get('email') or '').strip().lower()

    email_valid, email_error = validate_email(email)
    if not email_valid:
        return jsonify({'ok': False, 'error': email_error}), 400

    email = email[:200]
    try:
        user_db.add_to_waitlist(email, source='brief-digest', ip=ip_address)
    except Exception as e:
        logger.warning(f"brief_subscribe add_to_waitlist failed: {e}")

    try:
        from carrac.integrations import on_waitlist_join
        on_waitlist_join(email, source='brief-digest')
    except Exception:
        pass

    # Redundant audit trail (JSONL + Telegram/ntfy) — same idiom as the
    # register-page waitlist capture.
    _log_beta_waitlist(email, ip_address)

    return jsonify({'ok': True})


def _user_sessions_set_key(user_id: int) -> str:
    return f'mcd:user:{user_id}:sessions'


def track_user_session(user_id: int, session_id: str):
    """Record a session ID against a user so we can invalidate later in O(1).

    Called on login. Stored as a Redis set with a TTL slightly longer than
    the longest session cap (30 days from security.py).
    """
    try:
        import redis as redis_mod
        url = os.getenv('REDIS_SESSION_URL') or os.getenv('REDIS_URL', '')
        if not url or not session_id:
            return
        r = redis_mod.from_url(url, socket_connect_timeout=1, decode_responses=True)
        k = _user_sessions_set_key(user_id)
        r.sadd(k, session_id)
        r.expire(k, 31 * 86400)
    except Exception as exc:
        # A session missing from the registry survives "log out everywhere".
        logger.error("Session registry: failed to track session for user %s: %s", user_id, exc)


# Deleting the session blobs alone is not enough. A request that
# opened the session BEFORE the invalidation and finishes AFTER it re-saves
# the full authenticated blob under the same sid (SESSION_REFRESH_EACH_REQUEST
# re-saves on every response) — resurrecting the "deleted" session. The
# kill-epoch below makes validity independent of blob existence: any session
# created at or before the kill timestamp is rejected per-request.
# The kill-epoch mechanism moved to carrac/auth/session_kill.py so
# the FastAPI front door applies the SAME rule instead of ignoring it. Imported
# here rather than re-implemented — and _KILL_TS_FALLBACK must stay the same
# dict object, because tests/conftest.py clears it through this module.
from carrac.auth.session_kill import (  # noqa: E402,F401
    # Re-exported under their old private names: tests/conftest.py clears
    # _KILL_TS_FALLBACK through this module, and carrac/admin/routes.py
    # imports _invalidate_user_sessions from here. F401 is deliberate.
    _KILL_TS_FALLBACK,
    _KILL_TS_TTL,
    kill_ts_key as _kill_ts_key,
    mark_sessions_killed as _mark_sessions_killed,
    session_is_killed as _session_is_killed,
    sessions_killed_at as _sessions_killed_at,
)


@auth_bp.before_app_request
def _reject_killed_sessions():
    """Fail closed on sessions that predate the user's kill-epoch.

    Sessions re-issued after the kill (login, change_password) carry a fresh
    _created_at and pass; a resurrected pre-kill blob is cleared and rejected.

    The predicate itself is now _session_is_killed, shared with
    carrac/fastapi_app/deps/auth.py::current_user. Two copies of this rule is
    how the FastAPI surface ended up without one at all.
    """
    if not _session_is_killed(session):
        return
    session.clear()
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Session expired'}), 401
    return redirect(url_for('auth.login'))


def _scan_session_keys(client, prefix):
    """Yield every session key. Split out so the fallback can be tested."""
    return client.scan_iter(f'{prefix}*', count=100)


def _session_blob_belongs_to(raw, user_id) -> bool:
    """Does this serialised session blob belong to ``user_id``?

    Flask-Session 0.8 serialises with msgpack. The previous check
    looked for the JSON fragment `"user_id":<id>` in str(raw), which cannot
    occur in msgpack — so it matched nothing, ever. JSON is still tried second
    in case the backend is ever swapped back.
    """
    if raw is None:
        return False
    try:
        import msgspec.msgpack
        blob = msgspec.msgpack.decode(raw)
        if isinstance(blob, dict):
            return str(blob.get('user_id')) == str(user_id)
    except Exception:
        pass
    try:
        import json as _json
        text = raw.decode('utf-8', 'ignore') if isinstance(raw, (bytes, bytearray)) else str(raw)
        blob = _json.loads(text)
        if isinstance(blob, dict):
            return str(blob.get('user_id')) == str(user_id)
    except Exception:
        pass
    return False


def _invalidate_user_sessions(user_id: int):
    """Invalidate all Redis sessions for a user.

    Normal path: read the per-user set of session ids and delete those keys,
    which costs time proportional to that user's sessions, not to the number of
    sessions in Redis. Only when the set is empty (sessions created before the
    set was tracked) does it fall back to scanning every mcd:session:* key,
    which is O(N) in the global keyspace.
    """
    _mark_sessions_killed(user_id)
    try:
        import redis as redis_mod
        url = os.getenv('REDIS_SESSION_URL') or os.getenv('REDIS_URL', '')
        if not url:
            return
        r = redis_mod.from_url(url, socket_connect_timeout=1, decode_responses=True)
        prefix = current_app.config.get('SESSION_KEY_PREFIX', 'mcd:session:')
        set_key = _user_sessions_set_key(user_id)

        # Fast path: cost scales with this user's sessions, not the global keyspace.
        sids = r.smembers(set_key) or set()
        if sids:
            pipe = r.pipeline()
            for sid in sids:
                pipe.delete(f'{prefix}{sid}')
            pipe.delete(set_key)
            pipe.execute()
            return

        # Fallback: if the set is empty (pre-tracking-era sessions) fall back
        # to scan_iter so legacy sessions still get cleared.
        #
        # Do not test `f'"user_id":{user_id}' in str(data)`.
        # Flask-Session 0.8 serialises with msgpack, not JSON — that substring
        # never appears in a msgpack blob, so the fallback would be dead code
        # and legacy sessions would never be cleared. Decode properly. The
        # kill-epoch is the real backstop either way, but a fallback that
        # cannot match is worse than none: it reads as covered.
        # The client above is decode_responses=True (right for the sid set,
        # wrong for a binary blob — r.get() would raise UnicodeDecodeError and
        # the per-key except would swallow it). Read the blobs raw.
        rb = redis_mod.from_url(url, socket_connect_timeout=1,
                                decode_responses=False)
        for key in _scan_session_keys(rb, prefix):
            try:
                data = rb.get(key)
                if data is None:
                    continue
                if _session_blob_belongs_to(data, user_id):
                    rb.delete(key)
            except Exception:
                pass
    except Exception:
        pass


def _build_site_url(endpoint: str, **values) -> str:
    """Build an absolute URL rooted at Config.SITE_URL instead of the request Host.

    Defence against host-header injection: Flask's url_for(_external=True)
    trusts the incoming Host header when building absolute URLs, which
    lets an attacker with any ability to control Host (or X-Forwarded-Host
    through a loose proxy) poison password-reset / verification links
    sent to real users. Pin to Config.SITE_URL instead.
    """
    from carrac.site_url import site_base_url
    relative = url_for(endpoint, **values)  # relative, no host
    base = site_base_url()
    return f"{base}{relative}"


def _send_password_reset_email(email: str, token: str) -> bool:
    """Send password reset email."""
    from carrac.email import send_email
    reset_url = _build_site_url('auth.reset_password', token=token)
    html = render_template('email/password_reset.html', reset_url=reset_url)
    return send_email(email, 'Reset your password — Carrac', html,
                      text_body=f'Reset your password: {reset_url}\nThis link expires in 1 hour.')


def _send_verification_email(email: str, token: str) -> bool:
    """Send email verification email."""
    from carrac.email import send_email
    verify_url = _build_site_url('auth.verify_email', token=token)
    html = render_template('email/email_verification.html', verify_url=verify_url)
    return send_email(email, 'Verify your email — Carrac', html,
                      text_body=f'Verify your email: {verify_url}\nThis link expires in 24 hours.')


def _send_welcome_email(email: str) -> bool:
    """Send a warm welcome once a new member verifies their email.

    Frames them as an early/founding member and points to the community for
    help. Best-effort — never blocks or fails the verification flow.
    """
    from carrac.email import send_email
    from carrac.config import Config
    from carrac.site_url import site_base_url
    app_url = site_base_url()
    discord_url = getattr(Config, 'DISCORD_INVITE_URL', '') or ''
    html = render_template('email/welcome.html',
                           app_url=app_url,
                           discord_url=discord_url)
    text = ("Welcome to Carrac — you're one of our earliest members.\n\n"
            "You now have full access while we're in open beta. Jump in: "
            f"{app_url}\n")
    if discord_url:
        text += f"\nHit a snag or have an idea? Come say hi: {discord_url}\n"
    return send_email(email, "Welcome to Carrac — you're one of the first", html, text_body=text)


def _send_unlock_email(email: str) -> bool:
    """Send a one-click account-unlock link when the email-only
    brute-force threshold is exceeded.  The token clears the email-only
    counter so the real user can log in immediately.
    """
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    # Reuse the password-reset token table — same TTL, same invalidation.
    # We only need the email lookup; the unlock flow doesn't change the password.
    user = user_db.get_user_by_email(email)
    if not user or not user.get('is_active', True):
        return False
    ip_placeholder = 'brute-force-unlock'
    user_db.create_password_reset_token(user['id'], token_hash, ip_placeholder)
    unlock_url = _build_site_url('auth.unlock_account', token=token)
    try:
        from carrac.email import send_email
        html = (
            f'<p>Someone has made many failed login attempts on your Carrac account.</p>'
            f'<p>To confirm it\'s you and clear the protection counter, click the link below:</p>'
            f'<p><a href="{unlock_url}">{unlock_url}</a></p>'
            f'<p>This link expires in 1 hour.  If this wasn\'t you, you can safely ignore it.</p>'
        )
        return send_email(
            email,
            'Security alert: confirm your Carrac account',
            html,
            text_body=f'Confirm your account (many failed login attempts detected): {unlock_url}',
        )
    except Exception as exc:
        logger.warning("_send_unlock_email failed: %s", exc)
        return False


def _on_brute_force_unlock_needed(email: str) -> None:
    """Callback registered with security._email_unlock_callbacks.

    Called by security.py when the email-only counter hits EMAIL_ONLY_THRESHOLD.
    Runs inside a try/except in the caller so exceptions here are safe.
    """
    _send_unlock_email(email)


# Register the callback once at module load time.
import security as _security_module
if _on_brute_force_unlock_needed not in _security_module._email_unlock_callbacks:
    _security_module._email_unlock_callbacks.append(_on_brute_force_unlock_needed)


def _post_login_redirect():
    """Post-login landing: Desk when enabled, chart view otherwise (spec: landing switch is flag-conditional)."""
    if os.environ.get('DESK_ENABLED') == '1':
        return redirect(url_for('dashboard.desk'))
    return redirect(url_for('dashboard.index'))


# ── Login ────────────────────────────────────────────────────────────

@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("25 per minute")
def login():
    """Handle user login with brute force protection."""
    if 'user_id' in session:
        return redirect(url_for('dashboard.index'))

    error = None
    ip_address = request.remote_addr or '0.0.0.0'

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        is_locked, seconds_remaining = brute_force_protection.is_locked(email, ip_address)
        if is_locked:
            minutes = (seconds_remaining or 0) // 60 + 1
            error = f'Account temporarily locked. Try again in {minutes} minute(s).'
            security_logger.log_login_failure(email, ip_address, 'account_locked')
            add_timing_noise()
            return render_template('login.html', error=error)

        email_valid, email_error = validate_email(email)
        if not email_valid:
            error = email_error
            add_timing_noise()
            return render_template('login.html', error=error)

        user = user_db.authenticate(email, password)
        if user:
            # Check email verification before allowing login
            if not user.get('is_admin') and not user_db.is_email_verified(user['id']):
                brute_force_protection.clear_attempts(email, ip_address)
                return render_template('login.html',
                    error='Please verify your email before signing in.',
                    show_resend=True, resend_email=email)

            brute_force_protection.clear_attempts(email, ip_address)
            session.clear()  # Drop anonymous state (rotation happens below)
            session['user_id'] = user['id']
            session['email'] = user['email']
            session['display_name'] = user['display_name']
            session['is_admin'] = user['is_admin']
            # Tier is subscription-based (stored in DB), not credit-based.
            # Check for expired subscriptions and auto-downgrade before setting session.
            if user['is_admin']:
                session['tier'] = 'admin'
            else:
                expiry_status = user_db.check_subscription_expiry(user['id'])
                session['tier'] = expiry_status.get('tier', user.get('tier', 'free'))
            session['_created_at'] = time.time()
            session.permanent = True

            # Actually prevent session fixation.
            #
            # `session.clear()` above carried the comment "Prevent session
            # fixation" for a long time. It does not: clear() empties the
            # session dict but never issues a new session id, so the cookie the
            # browser was already carrying while anonymous survives this
            # transition and ends up identifying the authenticated session.
            # flask-session ships the mechanism — regenerate() in
            # flask_session.base drops the previous store entry, mints a
            # fresh sid and marks the session modified.
            #
            # ⚠ POSITION IS LOAD-BEARING, and the obvious placement is a silent
            # no-op. regenerate() opens with `if session:` — an EMPTY session
            # is falsy, so calling it immediately after session.clear() (which
            # is where it reads most naturally) skips the entire body and
            # rotates nothing. That version looked correct, ran, logged no
            # error, and left the sid byte-identical; only the end-to-end
            # cookie comparison caught it. So it runs HERE, after the session
            # keys are populated and before the sid is read for tracking.
            #
            # Ordering also matters for session kill: track_user_session() below
            # must register the POST-rotation sid, or _invalidate_user_sessions
            # would hold a sid that no longer exists and could never kill the
            # live session.
            #
            # Non-blocking by choice: failing to rotate is a defense-in-depth
            # gap, while refusing every login over a session-store hiccup is a
            # total outage. Loud rather than silent — log.error plus a Bugsink
            # event — so this can never again become a protection that is
            # absent without anyone knowing, which is the whole reason this
            # check exists.
            _regen = getattr(current_app.session_interface, 'regenerate', None)
            if _regen is not None:
                try:
                    _regen(session)
                except Exception:
                    logger.error(
                        "session id rotation FAILED at login — "
                        "the session remains vulnerable to fixation",
                        exc_info=True)
                    try:
                        import sentry_sdk as _sentry
                        _sentry.capture_exception()
                    except Exception:
                        pass
            else:
                # A client-side (cookie) session interface has no sid to
                # rotate. Prod uses redis/filesystem (Config.SESSION_TYPE in carrac/config.py),
                # so this is a misconfiguration worth hearing about, not a
                # supported mode.
                logger.error(
                    "session interface %s cannot rotate a session "
                    "id — login is not protected against fixation",
                    type(current_app.session_interface).__name__)

            # Track the session ID against the user so _invalidate_user_sessions
            # can kill every session for this user in O(1).
            sid = getattr(session, 'sid', None)
            if sid:
                track_user_session(user['id'], sid)
            security_logger.log_login_success(
                email, ip_address, request.headers.get('User-Agent', ''),
            )
            on_login(user['id'], email)
            # Tag Sentry/Bugsink events with an opaque user id
            # (no email — send_default_pii=False) so error reports are
            # attributable without leaking PII into the event stream.
            try:
                import sentry_sdk as _sentry
                _sentry.set_user({'id': str(user['id'])})
            except Exception:
                pass
            return _post_login_redirect()
        else:
            brute_force_protection.record_attempt(email, ip_address)
            remaining = brute_force_protection.get_remaining_attempts(email, ip_address)
            error = 'Invalid credentials'
            # Warn only on the LAST attempt — the threshold is 3,
            # so warning at remaining<=2 alarmed users on their first typo.
            if remaining == 1:
                error = 'Invalid credentials. 1 attempt remaining before lockout.'
            security_logger.log_login_failure(email, ip_address, 'invalid_credentials')
            add_timing_noise()

    return render_template('login.html', error=error)


def _verify_turnstile(token: str, ip: str) -> bool:
    """Verify a Cloudflare Turnstile token server-side (bot-wall for open signup).

    Fails OPEN when no secret is configured (feature off / dev without
    keys) and OPEN on a network error to Cloudflare — a CF blip must not silently
    kill all signups during a launch. Fails CLOSED on an explicit invalid/expired
    token, or a missing token when the secret IS configured (bot / JS disabled).
    """
    secret = os.getenv('TURNSTILE_SECRET_KEY', '')
    if not secret:
        return True  # not configured → skip the check
    if not token:
        return False  # configured but no token → reject
    try:
        import requests
        resp = requests.post(
            'https://challenges.cloudflare.com/turnstile/v0/siteverify',
            data={'secret': secret, 'response': token, 'remoteip': ip},
            timeout=5,
        )
        return bool(resp.json().get('success'))
    except Exception as e:
        logger.warning(f"Turnstile siteverify error (failing open): {e}")
        return True  # network error → don't block legit users


# ── Register ─────────────────────────────────────────────────────────

@auth_bp.route('/register', methods=['GET', 'POST'])
@limiter.limit("10 per hour")
def register():
    """Handle user registration (invite-only)."""
    if 'user_id' in session:
        return redirect(url_for('dashboard.index'))

    error = None
    success = None
    ip_address = request.remote_addr or '0.0.0.0'

    # Invite code may arrive via the invite-email link (?code=, GET) or the
    # form field (POST). Bypasses the signup pause AND prefills the form.
    incoming_code = (request.values.get('invite_code')
                     or request.values.get('code') or '').strip().upper()

    # Signup kill switch. Env + feature flag short-circuit the register
    # flow so we can pause public ingestion without a redeploy when cost or
    # headcount caps bite. Phase 1: a valid invite code BYPASSES the pause
    # (wave-released code-holders register even while public signups are closed);
    # no code → the visitor joins the deduped waitlist (DB + day-0 PostHog/EspoCRM).
    if signups_paused() and not incoming_code:
        if request.method == 'POST':
            submitted_email = request.form.get('email', '').strip().lower()
            if submitted_email and '@' in submitted_email:
                source = (request.values.get('src')
                          or request.values.get('utm_source') or '')[:80] or None
                try:
                    user_db.add_to_waitlist(submitted_email[:200], source=source, ip=ip_address)
                except Exception as e:
                    logger.warning(f"add_to_waitlist failed: {e}")
                try:
                    from carrac.integrations import on_waitlist_join
                    on_waitlist_join(submitted_email[:200], source=source)
                except Exception:
                    pass
                # Keep the JSONL + Telegram/ntfy ping as a redundant audit trail.
                _log_beta_waitlist(submitted_email[:200], ip_address)
                return render_template('signups_paused.html', waitlisted=True,
                                       submitted_email=submitted_email)
        return render_template('signups_paused.html', waitlisted=False)

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('password_confirm', '')
        invite_code = request.form.get('invite_code', '').strip().upper()
        display_name = sanitize_string(request.form.get('display_name', '').strip(), max_length=100)

        agree_terms = request.form.get('agree_terms')
        # Affirmative 18+ attestation required for new signups.
        age_attested = request.form.get('age_attested')

        email_valid, email_error = validate_email(email)
        turnstile_token = request.form.get('cf-turnstile-response', '')
        if not _verify_turnstile(turnstile_token, ip_address):
            error = 'Bot check failed — please reload the page and try again.'
        elif not agree_terms:
            error = 'You must agree to the Terms of Service and Privacy Policy'
        elif not age_attested:
            error = 'You must confirm you are 18 or older'
        elif not email_valid:
            error = email_error
        elif password != confirm_password:
            error = 'Passwords do not match'
        else:
            password_valid, password_error = validate_password(password)
            if not password_valid:
                error = password_error
            elif user_db.get_user_by_email(email):
                error = 'Registration failed. Please check your details and try again.'
            else:
                invite = user_db.validate_invite(email, invite_code if invite_code else None)
                # Open signups (BETA): when public signups are NOT paused an invite
                # is optional — Cloudflare Turnstile is the bot-wall. While signups
                # are paused a valid invite is still required (only wave-released
                # code-holders reach this POST branch, via the ?code= bypass).
                if signups_paused() and not invite:
                    error = 'Invalid or expired invite. Contact admin for access.'
                else:
                    invited_by = invite.get('created_by') if (invite and invite.get('type') != 'admin') else None
                    signup_type = invite.get('type', 'code') if invite else 'public'
                    is_admin = email.lower() == os.getenv('ADMIN_EMAIL', '').lower()
                    user_id = user_db.create_user(
                        email=email,
                        password=password,
                        display_name=display_name or None,
                        invited_by=invited_by,
                        tos_accepted=True,
                        tos_ip=ip_address,
                        tos_user_agent=request.headers.get('User-Agent', '')[:500],
                        age_attested=bool(age_attested),
                    )
                    if user_id:
                        invite_race = False
                        if invite and invite.get('id'):
                            marked = user_db.mark_invite_used(invite['id'], user_id)
                            if not marked:
                                # Concurrent registration consumed this invite first.
                                # The user row was created but the invite is gone; surface
                                # an error so the second registrant doesn't proceed.
                                # Residual risk: a dangling unverified user row remains for
                                # this email (no delete_user helper exists). It cannot log in
                                # without email verification and the invite is exhausted, so
                                # exposure is low.  An admin can manually clean it up.
                                logger.warning(
                                    f"register: invite race for invite_id={invite.get('id')} "
                                    f"user_id={user_id} email={email}; aborting registration"
                                )
                                invite_race = True
                                error = 'This invite has already been used. Please request a new one.'
                        if not invite_race:
                            security_logger.log_registration(email, ip_address, signup_type)
                            signup_source = (request.form.get('utm_source') or '')[:80] or None
                            signup_medium = (request.form.get('utm_medium') or '')[:80] or None
                            signup_campaign = (request.form.get('utm_campaign') or '')[:80] or None
                            signup_content = (request.form.get('utm_content') or '')[:80] or None
                            signup_term = (request.form.get('utm_term') or '')[:80] or None
                            ph_anon_id = (request.form.get('ph_anon_id') or '')[:64] or None
                            on_user_signup(user_id, email, display_name, signup_type,
                                           signup_source=signup_source,
                                           signup_medium=signup_medium,
                                           signup_campaign=signup_campaign,
                                           signup_content=signup_content,
                                           signup_term=signup_term,
                                           ph_anon_id=ph_anon_id)
                            # Close the waitlist loop (idempotent; no-op if not waitlisted).
                            try:
                                user_db.mark_waitlisted_registered(email)
                            except Exception:
                                pass

                            # Admin users are auto-verified
                            if is_admin:
                                user_db.set_email_verified(user_id)
                                return redirect(url_for('auth.login', registered='1'))

                            # Send verification email
                            token = secrets.token_urlsafe(32)
                            token_hash = _hash_token(token)
                            user_db.create_email_verification_token(user_id, token_hash)
                            _send_verification_email(email, token)

                            return render_template('verify_email_sent.html', email=email)
                    else:
                        error = 'Registration failed. Please try again.'

    # Retain form values on error (never retain passwords)
    form_values = {}
    if request.method == 'POST' and error:
        form_values = {
            'email': request.form.get('email', '').strip(),
            'display_name': request.form.get('display_name', '').strip(),
            'invite_code': request.form.get('invite_code', '').strip().upper(),
        }
    elif request.method == 'GET' and incoming_code:
        # Prefill the code from the invite-email link (?code=).
        form_values = {'invite_code': incoming_code}

    # Funnel: count register-page views server-side (not consent-gated, so it
    # isn't undercounted like the client snippet). A per-request anon id keeps
    # this cookieless — gives reliable view counts + UTM attribution, not
    # person-level stitching. GET only (don't count POST re-renders on error).
    if request.method == 'GET':
        try:
            on_funnel_event(secrets.token_urlsafe(12), 'register_page_viewed', {
                'utm_source': request.args.get('utm_source', '')[:80],
                'utm_medium': request.args.get('utm_medium', '')[:80],
                'utm_campaign': request.args.get('utm_campaign', '')[:80],
                'intent': request.args.get('intent', '')[:40],
            })
        except Exception:
            pass

    return render_template('register.html', error=error, success=success, form=form_values,
                           turnstile_site_key=os.getenv('TURNSTILE_SITE_KEY', ''))


# ── Heartbeat ────────────────────────────────────────────────────────

@auth_bp.route('/api/heartbeat', methods=['POST'])
@csrf.exempt
def heartbeat():
    """Lightweight activity ping — the before_request hook handles the actual update."""
    if 'user_id' not in session:
        return jsonify({'ok': False}), 401
    return jsonify({'ok': True}), 200


# ── Logout ───────────────────────────────────────────────────────────

@auth_bp.route('/logout', methods=['POST'])
def logout():
    """Handle user logout. Invalidates server-side session in Redis."""
    email = session.get('email', 'unknown')
    # Delete server-side session from Redis before clearing client cookie
    try:
        sid = getattr(session, 'sid', None)
        if sid and current_app.config.get('SESSION_TYPE') == 'redis':
            prefix = current_app.config.get('SESSION_KEY_PREFIX', 'session:')
            import redis
            r = redis.from_url(current_app.config.get('SESSION_REDIS', '').url
                               if hasattr(current_app.config.get('SESSION_REDIS', ''), 'url')
                               else os.getenv('REDIS_URL', ''), socket_connect_timeout=1)
            r.delete(f"{prefix}{sid}")
    except Exception:
        pass  # Fall through to session.clear() regardless
    # Delete filesystem session file if applicable
    try:
        if current_app.config.get('SESSION_TYPE') == 'filesystem':
            sid = getattr(session, 'sid', None)
            if sid:
                import glob
                session_dir = current_app.config.get('SESSION_FILE_DIR', '/tmp/flask_sessions')
                for f in glob.glob(os.path.join(session_dir, f'*{sid}*')):
                    os.unlink(f)
    except Exception:
        pass
    session.clear()
    logger.info(f"User logged out: {email}")
    return redirect(url_for('auth.login'))


# ── Password Reset ───────────────────────────────────────────────────

@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("3 per hour")
def forgot_password():
    """Request a password reset email."""
    if 'user_id' in session:
        return redirect(url_for('dashboard.index'))

    error = None
    success = None
    ip_address = request.remote_addr or '0.0.0.0'

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()

        email_valid, _ = validate_email(email)
        if not email_valid:
            # Still show generic message to prevent enumeration
            add_timing_noise()
            success = 'If an account exists with that email, we\'ve sent a reset link.'
            return render_template('forgot_password.html', success=success)

        user = user_db.get_user_by_email(email)
        if user and user.get('is_active', True):
            token = secrets.token_urlsafe(32)
            token_hash = _hash_token(token)
            user_db.create_password_reset_token(user['id'], token_hash, ip_address)
            _send_password_reset_email(email, token)
            security_logger.log_password_reset_requested(email, ip_address)

        # Always show same message regardless of whether user exists
        add_timing_noise()
        success = 'If an account exists with that email, we\'ve sent a reset link.'

    return render_template('forgot_password.html', error=error, success=success)


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def reset_password(token):
    """Reset password using a token from email."""
    if 'user_id' in session:
        return redirect(url_for('dashboard.index'))

    token_hash = _hash_token(token)
    token_data = user_db.validate_password_reset_token(token_hash)

    if not token_data:
        return render_template('forgot_password.html',
            error='This reset link is invalid or has expired. Please request a new one.')

    error = None
    ip_address = request.remote_addr or '0.0.0.0'

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('password_confirm', '')

        if password != confirm:
            error = 'Passwords do not match'
        else:
            password_valid, password_error = validate_password(password)
            if not password_valid:
                error = password_error
            else:
                # Update password
                user_db.update_password(token_data['user_id'], password)
                # Mark token as used
                user_db.use_password_reset_token(token_hash)
                # Invalidate all other tokens
                user_db.invalidate_password_reset_tokens(token_data['user_id'])
                # Invalidate all sessions
                _invalidate_user_sessions(token_data['user_id'])

                security_logger.log_password_reset_completed(
                    token_data.get('email', 'unknown'), ip_address)

                return redirect(url_for('auth.login', reset='1'))

    return render_template('reset_password.html', error=error)


# ── Email Verification ───────────────────────────────────────────────

@auth_bp.route('/verify-email/<token>')
@limiter.limit("10 per minute")
def verify_email(token):
    """Verify email address from link in email."""
    token_hash = _hash_token(token)
    token_data = user_db.validate_email_verification_token(token_hash)
    ip_address = request.remote_addr or '0.0.0.0'

    if not token_data:
        return render_template('login.html',
            error='This verification link is invalid or has expired. Please request a new one.')

    user_db.set_email_verified(token_data['user_id'])
    security_logger.log_email_verified(token_data.get('email', 'unknown'), ip_address)

    # Activation event: verification is the real signup gate (unverified users
    # can't log in), so this is the conversion that matters for the funnel.
    try:
        on_funnel_event(token_data['user_id'], 'email_verified', {})
    except Exception:
        pass

    # Warm early-member welcome (best-effort; never blocks verification).
    try:
        if token_data.get('email'):
            _send_welcome_email(token_data['email'])
    except Exception as e:
        # ERROR, not WARNING. Sentry's LoggingIntegration has an
        # event_level of ERROR by default, so a warning here never became a
        # Bugsink event. SMTP failures were already covered — send_email logs
        # its own logger.error (carrac/email.py::send_email) and returns False — but
        # this branch only fires for the failures send_email cannot report,
        # such as a broken welcome.html template, which were fully silent.
        logger.error(f"welcome email failed: {e}", exc_info=True)

    return redirect(url_for('auth.login', verified='1'))


@auth_bp.route('/resend-verification', methods=['POST'])
@limiter.limit("2 per hour")
def resend_verification():
    """Resend email verification link."""
    email = request.form.get('email', '').strip().lower()

    error = None
    success = None

    if email:
        user = user_db.get_user_by_email(email)
        if user and not user_db.is_email_verified(user['id']):
            token = secrets.token_urlsafe(32)
            token_hash = _hash_token(token)
            user_db.create_email_verification_token(user['id'], token_hash)
            _send_verification_email(email, token)
            success = 'Verification email resent. Check your inbox.'
        else:
            # Don't reveal whether account exists
            success = 'If the account needs verification, we\'ve sent a new link.'
        add_timing_noise()

    return render_template('verify_email_sent.html', email=email, success=success, error=error)


# ── Brute-force Unlock ────────────────────────────────────────────────

@auth_bp.route('/unlock-account/<token>')
@limiter.limit("10 per minute")
def unlock_account(token):
    """one-click account unlock when the email-only brute-force
    threshold is exceeded.  Validates the token (same table as password
    reset), clears the email-only counter, and redirects to login.
    """
    token_hash = _hash_token(token)
    token_data = user_db.validate_password_reset_token(token_hash)
    ip_address = request.remote_addr or '0.0.0.0'

    if not token_data:
        return render_template('login.html',
            error='This unlock link is invalid or has expired. Please log in normally.')

    # Mark token used so it can't be replayed.
    user_db.use_password_reset_token(token_hash)
    user_db.invalidate_password_reset_tokens(token_data['user_id'])

    # Clear the email-only brute-force counter so login is unblocked.
    email = token_data.get('email', '')
    if email:
        brute_force_protection.clear_email_only_counter(email)

    security_logger.log_password_reset_completed(email or 'unknown', ip_address)
    logger.info("[SECURITY] Account unlocked via unlock link | Email: %s | IP: %s",
                email, ip_address)

    return redirect(url_for('auth.login', unlocked='1'))


# ── Account Deletion & Data Export ───────────────────────────────────

@auth_bp.route('/api/account/export', methods=['POST'])
@limiter.limit("1 per day")
def export_account_data():
    """Export all user data as JSON (GDPR)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401

    user_id = session['user_id']
    ip_address = request.remote_addr or '0.0.0.0'

    data = user_db.export_user_data(user_id)
    security_logger.log_data_export(session.get('email', 'unknown'), ip_address)

    response = Response(
        json.dumps(data, indent=2, default=str),
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment; filename=carrac-data-export.json'}
    )
    return response


@auth_bp.route('/api/account/delete', methods=['POST'])
@limiter.limit("3 per hour")
def delete_account():
    """Delete user account (anonymize + purge data)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401

    user_id = session['user_id']
    email = session.get('email', 'unknown')
    ip_address = request.remote_addr or '0.0.0.0'

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400

    password = data.get('password', '')
    confirm = data.get('confirm', '')

    if confirm != 'DELETE MY ACCOUNT':
        return jsonify({'error': 'Please type "DELETE MY ACCOUNT" to confirm'}), 400

    # Verify password
    if not user_db.verify_user_password(user_id, password):
        add_timing_noise()
        return jsonify({'error': 'Incorrect password'}), 403

    # Prevent admin self-deletion
    if session.get('is_admin'):
        return jsonify({'error': 'Admin accounts cannot be self-deleted'}), 403

    # Anonymize user
    success = user_db.anonymize_user(user_id)
    if not success:
        return jsonify({'error': 'Account deletion failed. Please try again.'}), 500

    # Invalidate all sessions
    _invalidate_user_sessions(user_id)

    security_logger.log_account_deleted(email, ip_address)

    # Clear current session
    session.clear()

    return jsonify({'ok': True, 'message': 'Account deleted successfully'})


@auth_bp.route('/api/account/info', methods=['GET'])
@limiter.limit("10 per minute")
def account_info():
    """Return basic account details for the settings page."""
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401

    user = user_db.get_user_by_id(session['user_id'])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    return jsonify({
        'email': user.get('email', ''),
        'display_name': user.get('display_name', ''),
        'tier': user.get('tier', 'free'),
        'created_at': str(user.get('created_at', '')),
        'last_login': str(user.get('last_login', '')),
    })


@auth_bp.route('/api/account/change-password', methods=['POST'])
@limiter.limit("3 per hour")
def change_password():
    """Change password for logged-in user."""
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401

    user_id = session['user_id']
    data = request.get_json(silent=True) or {}

    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')
    confirm_password = data.get('confirm_password', '')

    if not current_password or not new_password:
        return jsonify({'error': 'All fields are required'}), 400

    if new_password != confirm_password:
        return jsonify({'error': 'New passwords do not match'}), 400

    # Validate new password strength — validate_password returns (ok: bool, message: str)
    pw_ok, pw_error = validate_password(new_password)
    if not pw_ok:
        return jsonify({'error': pw_error}), 400

    # Verify current password
    if not user_db.verify_user_password(user_id, current_password):
        add_timing_noise()
        return jsonify({'error': 'Current password is incorrect'}), 403

    # Update password
    success = user_db.update_password(user_id, new_password)
    if not success:
        return jsonify({'error': 'Password update failed. Please try again.'}), 500

    ip_address = request.remote_addr or '0.0.0.0'
    security_logger.log_password_reset_completed(session.get('email', 'unknown'), ip_address)

    # Invalidate all other sessions so concurrent sessions can't
    # continue with the previous password.  The reset-by-email path already does
    # this via the same helper; keep both in sync.
    _email = session.get('email', '')
    _display_name = session.get('display_name', '')
    _is_admin = session.get('is_admin', False)
    _tier = session.get('tier', 'free')
    _invalidate_user_sessions(user_id)
    # Re-issue a fresh session for the current request so the caller stays
    # logged in with the new credentials.
    session.clear()
    session['user_id'] = user_id
    session['email'] = _email
    session['display_name'] = _display_name
    session['is_admin'] = _is_admin
    session['tier'] = _tier
    session['_created_at'] = time.time()
    session.permanent = True
    new_sid = getattr(session, 'sid', None)
    if new_sid:
        track_user_session(user_id, new_sid)

    return jsonify({'ok': True, 'message': 'Password changed successfully'})
