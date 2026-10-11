"""
Fire-and-forget integration hooks for PostHog analytics and EspoCRM.

All calls run in background threads to never block user requests.
PostHog events sent via direct HTTP to avoid SDK consumer thread issues in
forked gunicorn workers.
"""

import logging
import threading
import uuid as _uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone

# The SDK no-ops when uninitialised (no SENTRY_DSN) — see the Sentry init block in worker.py.
# Safe to import unconditionally; every call site below still wraps the
# actual capture in its own try/except (_capture_exception) as a second
# guard against any unexpected SDK-internal error.
import sentry_sdk

logger = logging.getLogger('carrac.integrations')

# Cache PostHog config at module level (filled lazily by _init_posthog_config below)
_posthog_api_key = None
_posthog_host = None

# Shared bounded executor for fire-and-forget integration calls (PostHog,
# EspoCRM). The bounded pool + done_callback pattern means failures are
# captured as ERROR events and worker spawn is rate-limited.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='integrations')


def _capture_exception(exc):
    """sentry_sdk.capture_exception, but never itself raise or block."""
    try:
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def _log_future_exception(future: Future) -> None:
    """Done-callback that promotes silent thread failures to ERROR events."""
    exc = future.exception()
    if exc is not None:
        # logger.error with exc_info becomes a first-class Sentry event.
        logger.error('Integration call failed', exc_info=exc)
        _capture_exception(exc)


def _init_posthog_config():
    """Lazy-load PostHog config from environment (called once per worker).

    Reads the same Config.POSTHOG_HOST source (and its EU default) as the
    rest of the app instead of a separately-hardcoded US default — the two
    had drifted (config.py said eu.i.posthog.com, this module said
    us.i.posthog.com), which would have silently pointed direct-HTTP capture
    calls at the wrong region if POSTHOG_HOST was ever left unset.
    """
    global _posthog_api_key, _posthog_host
    if _posthog_api_key is None:
        from carrac.config import Config
        _posthog_api_key = Config.POSTHOG_API_KEY
        _posthog_host = Config.POSTHOG_HOST


# Beta cohort window. Signups landing inside this window get the
# `cohort=beta_closed_2026_04` tag so analytics can compare beta
# testers vs. general-availability users.
# Disabled unless BETA_END_TS is set (ISO-8601 with timezone, e.g.
# 'YYYY-MM-DDTHH:MM:SS+00:00'); no signup is tagged while it is unset.
BETA_COHORT_NAME = 'beta_closed_2026_04'


def _beta_cohort_tag_now():
    """Return BETA_COHORT_NAME if 'now' is inside the configured beta window."""
    import os
    end_iso = os.environ.get('BETA_END_TS', '').strip()
    if not end_iso:
        return None
    try:
        # fromisoformat understands the 'Z' suffix only on 3.11+; accept either.
        end_ts = datetime.fromisoformat(end_iso.replace('Z', '+00:00'))
        if end_ts.tzinfo is None:
            end_ts = end_ts.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) <= end_ts:
            return BETA_COHORT_NAME
    except Exception:
        logger.warning(f"Invalid BETA_END_TS {end_iso!r}; skipping cohort tag")
    return None


# PostHog-backed feature flag so operators can pause signups without
# a redeploy. Cached for 60 s per worker to avoid hammering /decide on every
# register hit — the env var still wins instantly for emergencies.
_SIGNUPS_FLAG_CACHE = {'value': False, 'checked_at': 0.0}
# ai_chat defaults OFF — in-app chat is disabled for the free MCP-first beta
# (per-token LLM cost). Flip the PostHog flag on to re-enable it as a paid perk.
_AI_CHAT_FLAG_CACHE = {'value': False, 'checked_at': 0.0}


# Idempotent first-event capture. claim_first_event() flips a
# JSONB flag in users.flags so the event fires exactly once per user. Calls
# from request handlers should use fire_first_event(user_id, flag, props)
# rather than _posthog_capture directly.
def fire_first_event(user_id, flag, properties=None):
    if not user_id:
        return False
    try:
        from user_db import user_db
        if not user_db.claim_first_event(int(user_id), flag):
            return False
    except Exception as e:
        logger.warning(f"fire_first_event claim failed [{flag}]: {e}")
        return False
    _fire_and_forget(_posthog_capture, str(user_id), flag, properties or {})
    return True


def is_signups_paused_flag() -> bool:
    import time, requests
    _init_posthog_config()
    if not _posthog_api_key:
        return False
    now = time.time()
    if now - _SIGNUPS_FLAG_CACHE['checked_at'] < 60:
        return bool(_SIGNUPS_FLAG_CACHE['value'])
    try:
        resp = requests.post(
            f'{_posthog_host.rstrip("/")}/decide?v=3',
            json={'api_key': _posthog_api_key, 'distinct_id': 'server'},
            timeout=3,
        )
        flags = (resp.json() or {}).get('featureFlags', {}) if resp.ok else {}
        val = bool(flags.get('signups_paused', False))
    except Exception:
        val = bool(_SIGNUPS_FLAG_CACHE['value'])
    _SIGNUPS_FLAG_CACHE['value'] = val
    _SIGNUPS_FLAG_CACHE['checked_at'] = now
    return val


def is_ai_chat_enabled_flag() -> bool:
    """Whether the in-app AI chat is enabled. Defaults OFF (MCP-first beta).

    Mirrors is_signups_paused_flag() but with inverted default: chat stays
    disabled unless the PostHog `ai_chat_enabled` flag is explicitly ON. On any
    config/network failure we fall back to the cached value (fail-closed to OFF
    on first call, since the cache starts False).
    """
    import time, requests
    _init_posthog_config()
    if not _posthog_api_key:
        return False
    now = time.time()
    if now - _AI_CHAT_FLAG_CACHE['checked_at'] < 60:
        return bool(_AI_CHAT_FLAG_CACHE['value'])
    try:
        resp = requests.post(
            f'{_posthog_host.rstrip("/")}/decide?v=3',
            json={'api_key': _posthog_api_key, 'distinct_id': 'server'},
            timeout=3,
        )
        flags = (resp.json() or {}).get('featureFlags', {}) if resp.ok else {}
        val = bool(flags.get('ai_chat_enabled', False))
    except Exception:
        val = bool(_AI_CHAT_FLAG_CACHE['value'])
    _AI_CHAT_FLAG_CACHE['value'] = val
    _AI_CHAT_FLAG_CACHE['checked_at'] = now
    return val


def ai_chat_allowed(is_admin) -> bool:
    """The single rule for the in-app Copilot chat: the PostHog flag OR the
    caller is an admin (so the operator can demo the chat with it on;
    everyone else stays flag-gated, and the flag stays OFF). Every chat surface — the index.html UI, /api/ai/chat and
    /api/ai/execute-action — must call THIS, not the raw flag, so the rule
    cannot drift between them (the failure mode being: wired in one place, not the other).
    A flag lookup failure counts as OFF, never as an exception."""
    if bool(is_admin):
        return True
    try:
        return bool(is_ai_chat_enabled_flag())
    except Exception:
        return False


def _fire_and_forget(fn, *args, **kwargs):
    """Submit to the shared executor. Exceptions surface as ERROR events."""
    future = _executor.submit(fn, *args, **kwargs)
    future.add_done_callback(_log_future_exception)


# ── PostHog helpers (direct HTTP, no SDK consumer dependency) ────────

def _posthog_capture(distinct_id, event, properties=None):
    """Send a PostHog event via direct HTTP POST to /batch/ endpoint."""
    try:
        _init_posthog_config()
        if not _posthog_api_key:
            return
        import requests
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            'api_key': _posthog_api_key,
            'historical_migration': False,
            'sentAt': now,
            'batch': [{
                'event': event,
                'distinct_id': str(distinct_id),
                'properties': {
                    **(properties or {}),
                    '$process_person_profile': True,
                    '$lib': 'posthog-python',
                    '$lib_version': 'direct-http',
                },
                'timestamp': now,
                'uuid': str(_uuid.uuid4()),
            }],
        }
        resp = requests.post(
            f'{_posthog_host.rstrip("/")}/batch/',
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"PostHog capture failed [{event}]: HTTP {resp.status_code} {resp.text[:200]}")
        else:
            logger.info(f"PostHog event sent: {event} for user {distinct_id}")
    except Exception as e:
        logger.error(f"PostHog capture failed [{event}]: {e}")
        _capture_exception(e)


def _posthog_identify(distinct_id, properties=None):
    """Set person properties in PostHog via $set event."""
    try:
        _init_posthog_config()
        if not _posthog_api_key:
            return
        import requests
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            'api_key': _posthog_api_key,
            'historical_migration': False,
            'sentAt': now,
            'batch': [{
                'event': '$set',
                'distinct_id': str(distinct_id),
                'properties': {
                    '$set': properties or {},
                    '$process_person_profile': True,
                },
                'timestamp': now,
                'uuid': str(_uuid.uuid4()),
            }],
        }
        resp = requests.post(
            f'{_posthog_host.rstrip("/")}/batch/',
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"PostHog identify failed: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"PostHog identify failed: {e}")
        _capture_exception(e)


# ── EspoCRM helpers ──────────────────────────────────────────────────

def _espocrm_request(method, endpoint, data=None, extra_headers=None):
    """Make an EspoCRM API request (no-op if not configured).

    ``extra_headers`` is for Espo's header-carried switches — e.g.
    ``X-Skip-Duplicate-Check: true``; the ``skipDuplicateCheck``
    query parameter is NOT honoured on 9.3.8, verified against prod.
    """
    from carrac.config import Config
    if not Config.ESPOCRM_URL or not Config.ESPOCRM_API_KEY:
        return None
    import requests
    url = f"{Config.ESPOCRM_URL.rstrip('/')}/api/v1/{endpoint}"
    headers = {
        'X-Api-Key': Config.ESPOCRM_API_KEY,
        'Content-Type': 'application/json',
    }
    if extra_headers:
        headers.update(extra_headers)
    resp = requests.request(method, url, json=data, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _espocrm_find_contact_by_email(email):
    """Find a contact by email in EspoCRM.

    Returns ``(contact_dict_or_None, lookup_ok)``. ``lookup_ok`` is False only
    when the HTTP call itself failed (network/5xx/parse error) — as opposed to
    a clean "no contact found" response — so create-if-missing callers can
    tell an Espo outage apart from a genuine new lead and skip minting a
    duplicate contact when the outage resolves and the lookup would have
    found one.
    """
    try:
        result = _espocrm_request('GET', f'Contact?where[0][type]=equals&where[0][attribute]=emailAddress&where[0][value]={email}')
        if result and result.get('list'):
            return result['list'][0], True
        return None, True
    except Exception as e:
        logger.error(f"EspoCRM find contact by email failed: {e}")
        _capture_exception(e)
        return None, False


def _map_lead_source(source, medium):
    """Map signup UTM source/medium to the EspoCRM ``cLeadSource`` enum.

    Interface 3: cLeadSource in ["Web Site", "Waitlist", "Invite", "Referral",
    "Social", "Search", "Newsletter", "Other"].
    """
    def _classify(val):
        v = (val or '').strip().lower()
        if not v:
            return None
        if v == 'waitlist':
            return 'Waitlist'
        if v in ('invite', 'invite_code'):
            return 'Invite'
        if v == 'referral':
            return 'Referral'
        if v in ('twitter', 'x', 'reddit', 'facebook', 'linkedin', 'social'):
            return 'Social'
        if v in ('google', 'bing', 'search'):
            return 'Search'
        if v in ('newsletter', 'email'):
            return 'Newsletter'
        if v in ('direct', 'web'):
            return 'Web Site'
        return None

    result = _classify(source) or _classify(medium)
    if result:
        return result
    if not source and not medium:
        return 'Web Site'
    return 'Other'


# ── EspoCRM Opportunity helpers (also reused by crm_reconcile) ──

PAID_TIERS = {'premium'}

# Guards the check-then-act (GET Opportunity -> POST/PATCH) in
# _sync_opportunity_for_tier_change against two near-simultaneous
# on_tier_change calls (fire-and-forget, up to 4 concurrent workers) both
# observing "no Opportunity" and double-creating one. Volume here is tiny
# (one tier change at a time per user in practice), so a single global lock
# is fine — matches the module's existing fire-and-forget simplicity.
_OPPORTUNITY_SYNC_LOCK = threading.Lock()


def _find_opportunity_for_contact(contact_id):
    """Return the first Opportunity linked to this contact, or None."""
    result = _espocrm_request(
        'GET',
        f'Opportunity?where[0][type]=equals&where[0][attribute]=contactId&where[0][value]={contact_id}'
    )
    if result and result.get('list'):
        return result['list'][0]
    return None


def _create_subscription_opportunity(contact_id, email):
    """POST a Closed-Won Carrac-subscription Opportunity for a newly-paid contact."""
    from datetime import date
    return _espocrm_request('POST', 'Opportunity', {
        'name': f"Carrac subscription — {email}",
        'stage': 'Closed Won',
        'amount': 29,
        'closeDate': date.today().isoformat(),
        'contactId': contact_id,
    })


def _sync_opportunity_for_tier_change(contact_id, email, old_tier, new_tier):
    """Upgrade to paid -> create an Opportunity (once); downgrade from paid ->
    flip the existing one to Closed Lost.

    The GET-check + POST/PATCH below is a check-then-act — held under
    _OPPORTUNITY_SYNC_LOCK so two near-simultaneous on_tier_change calls
    (fire-and-forget, up to 4 concurrent workers) for the same contact can't
    both observe "no Opportunity" and double-create one."""
    was_paid = old_tier in PAID_TIERS
    is_paid = new_tier in PAID_TIERS
    with _OPPORTUNITY_SYNC_LOCK:
        if is_paid and not was_paid:
            if not _find_opportunity_for_contact(contact_id):
                _create_subscription_opportunity(contact_id, email)
        elif was_paid and not is_paid:
            existing_opp = _find_opportunity_for_contact(contact_id)
            if existing_opp:
                _espocrm_request('PUT', f"Opportunity/{existing_opp['id']}", {'stage': 'Closed Lost'})


# ── Public integration hooks ─────────────────────────────────────────

def on_user_signup(user_id, email, display_name=None, invite_type='code',
                   signup_source=None, signup_medium=None, signup_campaign=None,
                   signup_content=None, signup_term=None, ph_anon_id=None):
    """Called after successful user registration."""
    def _do():
        uid = str(user_id)
        # Tag signups that land during the closed beta window so
        # post-beta analytics can segment learning cohorts. BETA_END_TS is
        # ISO-8601 UTC; tagging is off while it is unset.
        props = {
            'email': email,
            'display_name': display_name or '',
            'tier': 'free',
            'invite_type': invite_type,
        }
        cohort = _beta_cohort_tag_now()
        if cohort:
            props['cohort'] = cohort
        # Server-side acquisition attribution (the client-side PostHog UTM
        # capture is consent-gated; this path is not, it's our own form data).
        if signup_source:
            props['signup_source'] = signup_source
        if signup_medium:
            props['signup_medium'] = signup_medium
        # D-IDENTITY / Interface 1: the extra UTM fields + signup timestamp,
        # added to the $set payload only (not the capture event props above).
        if signup_campaign:
            props['utm_campaign'] = signup_campaign
        if signup_content:
            props['utm_content'] = signup_content
        if signup_term:
            props['utm_term'] = signup_term
        props['signup_date'] = datetime.now(timezone.utc).isoformat()
        # PostHog
        _posthog_identify(uid, props)
        _posthog_capture(uid, 'user_signed_up', {
            'invite_type': invite_type,
            'signup_source': signup_source or '',
            'signup_medium': signup_medium or '',
        })
        # D-IDENTITY: stitch the pre-signup anonymous PostHog identity (and the
        # waitlist person, keyed by email) onto the new user_id distinct_id.
        if ph_anon_id:
            _posthog_capture(uid, '$create_alias', {'alias': ph_anon_id, 'distinct_id': uid})
        _posthog_capture(uid, '$create_alias', {'alias': email, 'distinct_id': uid})
        # EspoCRM — find-or-create contact (custom fields use c-prefix). A
        # waitlist join may have created the Contact first, so update it in place
        # rather than POSTing a duplicate.
        try:
            name = display_name or email.split('@')[0]
            source_note = (f"Signup source: {signup_source}"
                           + (f" / {signup_medium}" if signup_medium else '')) if signup_source else None
            lead_source = _map_lead_source(signup_source, signup_medium)
            existing, lookup_ok = _espocrm_find_contact_by_email(email)
            if existing:
                payload = {
                    'cConvexityUserId': user_id,
                    'cTier': 'free',
                    'cLeadSource': lead_source,
                    'cLifecycleStage': 'registered',
                }
                if source_note:
                    prior = (existing.get('description') or '').strip()
                    payload['description'] = f"{prior}\n{source_note}".strip()
                _espocrm_request('PUT', f"Contact/{existing['id']}", payload)
            elif lookup_ok:
                payload = {
                    'firstName': name.split()[0][:50] if name else email.split('@')[0],
                    'lastName': name.split()[-1][:50] if name and len(name.split()) > 1 else '—',
                    'emailAddress': email,
                    'cConvexityUserId': user_id,
                    'cTier': 'free',
                    'cLeadSource': lead_source,
                    'cLifecycleStage': 'registered',
                }
                if source_note:
                    payload['description'] = source_note
                _espocrm_request('POST', 'Contact', payload)
            else:
                # Espo lookup itself failed (outage) — do NOT create, or a
                # contact that actually exists would be duplicated once Espo
                # is back up and the next sync runs.
                logger.error(f"EspoCRM lookup failed for {email}; skipping contact create to avoid duplicate (signup)")
        except Exception as e:
            logger.error(f"EspoCRM create contact failed: {e}")
            _capture_exception(e)

    _fire_and_forget(_do)


def on_waitlist_join(email, source=None):
    """Called when an anonymous visitor joins the beta waitlist (day-0 wiring).

    Identifies the lead in PostHog by email (so it merges with the user on
    signup) + fires a `waitlist_joined` funnel event, and find-or-creates an
    EspoCRM Contact so the lead is in the pipeline before they ever register.
    Fire-and-forget — never blocks the request.
    """
    def _do():
        try:
            _posthog_identify(email, {'email': email, 'lifecycle': 'waitlist',
                                      'source': source or ''})
            _posthog_capture(email, 'waitlist_joined', {'source': source or ''})
        except Exception as e:
            logger.error(f"PostHog waitlist_joined failed: {e}")
            _capture_exception(e)
        try:
            note = "Beta waitlist join" + (f" (source: {source})" if source else "")
            lead_source = _map_lead_source(source, None)
            existing, lookup_ok = _espocrm_find_contact_by_email(email)
            if existing:
                _espocrm_request('PUT', f"Contact/{existing['id']}", {
                    'description': note,
                    'cLeadSource': lead_source,
                    'cLifecycleStage': 'waitlist',
                })
            elif lookup_ok:
                local = email.split('@')[0]
                _espocrm_request('POST', 'Contact', {
                    'firstName': local[:50],
                    'lastName': '—',
                    'emailAddress': email,
                    'description': note,
                    'cLeadSource': lead_source,
                    'cLifecycleStage': 'waitlist',
                })
            else:
                logger.error(f"EspoCRM lookup failed for {email}; skipping contact create to avoid duplicate (waitlist)")
        except Exception as e:
            logger.error(f"EspoCRM waitlist contact failed: {e}")
            _capture_exception(e)

    _fire_and_forget(_do)


def on_login(user_id, email):
    """Called after successful login."""
    def _do():
        uid = str(user_id)
        _posthog_capture(uid, 'user_logged_in')
        # EspoCRM — update lastActive (create contact if missing)
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            contact, lookup_ok = _espocrm_find_contact_by_email(email)
            if contact:
                _espocrm_request('PUT', f"Contact/{contact['id']}", {
                    'cLastActive': now,
                })
            elif lookup_ok:
                name = email.split('@')[0]
                _espocrm_request('POST', 'Contact', {
                    'firstName': name[:50],
                    'lastName': '—',
                    'emailAddress': email,
                    'cConvexityUserId': user_id,
                    'cLastActive': now,
                })
            else:
                logger.error(f"EspoCRM lookup failed for {email}; skipping contact create to avoid duplicate (login)")
        except Exception as e:
            logger.error(f"EspoCRM update lastActive failed: {e}")
            _capture_exception(e)

    _fire_and_forget(_do)


def on_tier_change(user_id, email, old_tier, new_tier):
    """Called when a user's tier changes.

    Finds-or-creates the Espo contact (an update-only call would silently
    no-op on a tier change for a contact-less user), advances
    cLifecycleStage on paid transitions, and syncs an Opportunity.
    """
    def _do():
        uid = str(user_id)
        _posthog_capture(uid, 'tier_changed', {
            'old_tier': old_tier,
            'new_tier': new_tier,
        })
        _posthog_identify(uid, {'tier': new_tier})

        if new_tier in PAID_TIERS:
            lifecycle_stage = 'paying'
        elif new_tier == 'free':
            lifecycle_stage = 'activated'
        else:
            lifecycle_stage = None

        contact_id = None
        # EspoCRM — update tier (find-or-create; guarded by lookup_ok)
        try:
            contact, lookup_ok = _espocrm_find_contact_by_email(email)
            update = {'cTier': new_tier}
            if lifecycle_stage:
                update['cLifecycleStage'] = lifecycle_stage
            if contact:
                contact_id = contact['id']
                _espocrm_request('PUT', f"Contact/{contact_id}", update)
            elif lookup_ok:
                name = email.split('@')[0]
                created = _espocrm_request('POST', 'Contact', {
                    'firstName': name[:50],
                    'lastName': '—',
                    'emailAddress': email,
                    'cConvexityUserId': user_id,
                    **update,
                })
                contact_id = created.get('id') if created else None
            else:
                logger.error(f"EspoCRM lookup failed for {email}; skipping contact create to avoid duplicate (tier_change)")
        except Exception as e:
            logger.error(f"EspoCRM update tier failed: {e}")
            _capture_exception(e)

        # Opportunity sync: upgrade to paid -> create (once); downgrade from
        # paid -> flip existing Opportunity to Closed Lost.
        try:
            if contact_id:
                _sync_opportunity_for_tier_change(contact_id, email, old_tier, new_tier)
        except Exception as e:
            logger.error(f"EspoCRM opportunity sync failed: {e}")
            _capture_exception(e)

    _fire_and_forget(_do)


def on_trade_opened(user_id, email, trade):
    """Called after a trade journal entry is created."""
    uid = str(user_id)

    # PostHog event: send synchronously (direct HTTP, fast)
    try:
        _posthog_capture(uid, 'trade_opened', {
            'asset': trade.get('asset'),
            'direction': trade.get('direction'),
            'setup_type': trade.get('setup_type') or '',
            'timeframe': trade.get('timeframe') or '',
            'entry_price': trade.get('entry_price'),
            'rr_planned': trade.get('rr_planned'),
            'confidence': trade.get('confidence'),
        })
    except Exception as e:
        logger.error(f"PostHog trade_opened sync failed: {e}")
        _capture_exception(e)

    # EspoCRM in background
    def _do_espo():
        try:
            contact, _lookup_ok = _espocrm_find_contact_by_email(email)
            if contact:
                _espocrm_request('PUT', f"Contact/{contact['id']}", {
                    'cCActivePositions': (contact.get('cCActivePositions') or 0) + 1,
                })
        except Exception as e:
            logger.error(f"EspoCRM update active positions failed: {e}")
            _capture_exception(e)

    _fire_and_forget(_do_espo)


def on_trade_closed(user_id, email, trade, stats):
    """Called after a trade is closed. stats = get_journal_stats(user_id, days=365)."""
    uid = str(user_id)

    # ── PostHog event: send synchronously (direct HTTP, fast) ────────
    try:
        rr = trade.get('rr_actual')
        if rr is not None:
            outcome = 'win' if rr > 0 else ('breakeven' if rr == 0 else 'loss')
        else:
            outcome = 'unknown'

        hold_hours = None
        if trade.get('opened_at') and trade.get('closed_at'):
            try:
                fmt = '%Y-%m-%d %H:%M:%S'
                opened = trade['opened_at']
                closed = trade['closed_at']
                if isinstance(opened, str):
                    opened = datetime.strptime(opened[:19], fmt)
                if isinstance(closed, str):
                    closed = datetime.strptime(closed[:19], fmt)
                hold_hours = round((closed - opened).total_seconds() / 3600, 1)
            except Exception:
                pass

        _posthog_capture(uid, 'trade_closed', {
            'asset': trade.get('asset'),
            'direction': trade.get('direction'),
            'setup_type': trade.get('setup_type') or '',
            'exit_reason': trade.get('exit_reason') or 'manual',
            'rr_actual': rr,
            'pnl_currency': trade.get('pnl_currency'),
            'outcome': outcome,
            'hold_hours': hold_hours,
            'trade_grade': trade.get('trade_grade') or '',
            'mistake_tags': trade.get('mistake_tags') or '',
        })
    except Exception as e:
        logger.error(f"PostHog trade_closed sync failed: {e}")
        _capture_exception(e)

    # ── Slow work (analytics + identify + EspoCRM) in background ─────
    def _do_enrichment():
        analytics = {}
        try:
            from carrac.content_db import get_content_db
            db = get_content_db()
            analytics = db.get_journal_analytics(user_id, days=365) or {}
        except Exception as e:
            logger.warning(f"Analytics fetch for PostHog/EspoCRM failed: {e}")

        # Derive best/worst setup from analytics breakdown
        best_setup = worst_setup = ''
        by_setup = analytics.get('by_setup', {})
        min_trades_for_rank = 3
        ranked = [(k, v) for k, v in by_setup.items()
                  if k != 'unknown' and v.get('trades', 0) >= min_trades_for_rank]
        if ranked:
            best_setup = max(ranked, key=lambda x: x[1].get('win_rate', 0))[0]
            worst_setup = min(ranked, key=lambda x: x[1].get('win_rate', 100))[0]

        preferred_direction = 'both'
        by_dir = analytics.get('by_direction', {})
        if by_dir:
            long_n = by_dir.get('long', {}).get('trades', 0)
            short_n = by_dir.get('short', {}).get('trades', 0)
            total_dir = long_n + short_n
            if total_dir > 0:
                if long_n / total_dir >= 0.7:
                    preferred_direction = 'long'
                elif short_n / total_dir >= 0.7:
                    preferred_direction = 'short'

        _posthog_identify(uid, {
            'total_trades': stats.get('trades', 0),
            'win_rate': stats.get('win_rate', 0),
            'profit_factor': analytics.get('profit_factor'),
            'expectancy': analytics.get('expectancy', 0),
            'avg_rr': stats.get('avg_rr', 0),
            'best_setup_type': best_setup,
            'worst_setup_type': worst_setup,
            'preferred_direction': preferred_direction,
        })

        # EspoCRM — enrich contact with trading stats
        try:
            now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            contact, _lookup_ok = _espocrm_find_contact_by_email(email)
            if contact:
                update = {
                    'cCTotalTrades': stats.get('trades', 0),
                    'cCWinRate': round(stats.get('win_rate', 0), 1),
                    'cCProfitFactor': round(analytics.get('profit_factor') or 0, 2),
                    'cCExpectancy': round(analytics.get('expectancy', 0), 3),
                    'cCAvgRR': round(stats.get('avg_rr', 0), 2),
                    'cCLastTradeDate': now,
                    'cCActivePositions': max((contact.get('cCActivePositions') or 1) - 1, 0),
                }
                if best_setup:
                    update['cCBestSetup'] = best_setup[:30]
                if preferred_direction:
                    update['cCPreferredDirection'] = preferred_direction[:10]
                _espocrm_request('PUT', f"Contact/{contact['id']}", update)
        except Exception as e:
            logger.error(f"EspoCRM update trading stats failed: {e}")
            _capture_exception(e)

    _fire_and_forget(_do_enrichment)


def on_satisfaction_rating(user_id, email, rating, comment=None):
    """Called after a user submits a satisfaction survey response."""
    def _do():
        uid = str(user_id)

        # PostHog event
        _posthog_capture(uid, 'satisfaction_survey_completed', {
            'rating': rating,
            'has_comment': bool(comment),
        })

        # PostHog person properties (fetch stats for running avg)
        from user_db import user_db
        stats = user_db.get_satisfaction_stats(user_id)
        _posthog_identify(uid, {
            'last_satisfaction_rating': rating,
            'last_satisfaction_date': datetime.now(timezone.utc).isoformat(),
            'satisfaction_response_count': stats['count'],
            'avg_satisfaction_rating': stats['avg_rating'],
        })

        # EspoCRM — update contact
        try:
            now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            contact, _lookup_ok = _espocrm_find_contact_by_email(email)
            if contact:
                update = {
                    'cSatisfactionRating': rating,
                    'cSatisfactionDate': now,
                }
                if comment:
                    update['cSatisfactionComment'] = comment[:500]
                _espocrm_request('PUT', f"Contact/{contact['id']}", update)
        except Exception as e:
            logger.error(f"EspoCRM satisfaction update failed: {e}")
            _capture_exception(e)

    _fire_and_forget(_do)


def on_funnel_event(user_id, event, properties=None):
    """Fire a single PostHog funnel event. Use for lightweight signals that
    don't need CRM enrichment — e.g. upgrade-button clicks, tier-limit hits,
    free-tier gate-denials.

    user_id=None / 0 is allowed for pre-auth events; distinct_id falls back
    to 'anon'.
    """
    def _do():
        uid = str(user_id) if user_id else 'anon'
        _posthog_capture(uid, event, properties or {})
    _fire_and_forget(_do)


def on_billing_event(user_id, email, event_type, amount_cents):
    """Called after a billing event (top-up, subscription). Updates PostHog person properties."""
    def _do():
        uid = str(user_id)
        from user_db import user_db
        stats = user_db.get_ai_usage_stats(user_id)
        _posthog_identify(uid, {
            'ai_total_spent_cents': stats.get('total_spent_cents', 0),
            'ai_total_deposited_cents': stats.get('total_deposited_cents', 0),
            'ai_balance_cents': stats.get('balance_cents', 0),
        })
        _posthog_capture(uid, f'billing_{event_type}', {
            'amount_cents': amount_cents,
        })
    _fire_and_forget(_do)


# ── Activity heartbeat ──────────────────────────────────────────

_ACTIVITY_HEARTBEAT_TTL = 21600  # 6h
# Fallback throttle store when Redis is unavailable — per-worker only (not
# shared across gunicorn processes), which is an acceptable degrade since the
# Redis path is what runs in every real deployment.
_activity_heartbeat_fallback = {}


def _activity_heartbeat_due(user_id):
    """Throttle on_activity to once per _ACTIVITY_HEARTBEAT_TTL per user.

    Tries the app's shared Redis client first (SET NX EX — atomic across
    gunicorn workers); falls back to an in-process dict when Redis is
    unavailable.
    """
    key = f'activity_hb:{user_id}'
    try:
        from carrac.caches import _get_redis
        r = _get_redis()
        if r:
            return bool(r.set(key, '1', nx=True, ex=_ACTIVITY_HEARTBEAT_TTL))
    except Exception:
        pass
    import time
    now = time.time()
    expiry = _activity_heartbeat_fallback.get(user_id)
    if expiry and expiry > now:
        return False
    _activity_heartbeat_fallback[user_id] = now + _ACTIVITY_HEARTBEAT_TTL
    return True


def on_activity(user_id, email):
    """Lightweight per-request activity heartbeat — throttled to once per 6h
    per user (see _activity_heartbeat_due). Hooked from
    auth_bp.before_app_request for logged-in requests; keeps cLastActive /
    last_active fresh without hammering Espo/PostHog on every request.
    """
    if not user_id:
        return
    if not _activity_heartbeat_due(user_id):
        return

    def _do():
        try:
            contact, _lookup_ok = _espocrm_find_contact_by_email(email)
            if contact:
                now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                _espocrm_request('PUT', f"Contact/{contact['id']}", {'cLastActive': now})
        except Exception as e:
            logger.error(f"EspoCRM activity heartbeat failed: {e}")
            _capture_exception(e)
        try:
            from user_db import user_db
            user_db.update_last_active(int(user_id))
        except Exception as e:
            logger.error(f"user_db.update_last_active failed: {e}")
            _capture_exception(e)
        _posthog_identify(str(user_id), {'last_active': datetime.now(timezone.utc).isoformat()})

    _fire_and_forget(_do)
