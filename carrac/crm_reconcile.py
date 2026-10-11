"""Weekly CRM reconciliation — repairs drift between the app's user table and
EspoCRM Contacts / TargetLists / Opportunities.

D-RECON: runs as a worker.py background thread (see
worker._start_crm_reconcile_scheduler), NOT a crontab entry — prod crontab
edits are classifier-blocked. Uses the app-scoped EspoCRM API key (the same
`_espocrm_request` the rest of `carrac.integrations` uses) — TargetList and
Opportunity access on that key is granted to that key in EspoCRM; no
admin-scoped auth needed here.
"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger('carrac.crm_reconcile')

# Interface 4 — exact TargetList names (owned/created by D; we never create
# one ourselves, only sync membership).
TARGET_LIST_NAMES = {
    'free_tier': 'Free Tier Users',
    'premium': 'Premium Users',
    'active_14d': 'Active Users (14d)',
    'waitlist': 'Waitlist',
}

ACTIVE_WINDOW_DAYS = 14
_PAGE_SIZE = 200


def _capture_exception(exc):
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def _effective_tier(user_row):
    """Mirror the 'admin' virtual-tier convention used elsewhere in user_db
    (e.g. get_user_by_email) — the tier column itself is not set to 'admin'."""
    if user_row.get('is_admin'):
        return 'admin'
    return user_row.get('tier') or 'free'


def _create_contact(request, payload, uid):
    """POST a Contact; on Espo's 409 retry with the duplicate check skipped.

    Espo's duplicate check matches on the contact NAME, not the
    email. A user whose display name equals another contact's got 409 on every weekly run, the
    per-user except swallowed it, and the user stayed unsynced for three
    weeks. The record is distinct by email and by cConvexityUserId, so a
    409 here is never a real duplicate of OUR making — retry once with the
    ``X-Skip-Duplicate-Check: true`` header, which is how Espo's own UI
    saves past the warning (the ``skipDuplicateCheck`` query parameter is
    ignored on Espo 9.3.8). Any other
    failure, or a second 409, propagates to the caller as before.
    """
    try:
        return request('POST', 'Contact', payload)
    except Exception as exc:  # requests.HTTPError carries .response
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status != 409:
            raise
        logger.warning(
            "crm_reconcile: user %s: Espo reported a duplicate on create "
            "(name collision) — retrying with skipDuplicateCheck", uid,
        )
        return request('POST', 'Contact', payload,
                       extra_headers={'X-Skip-Duplicate-Check': 'true'})


def _get_all_espo_contacts():
    """GET all Espo contacts with the fields reconciliation needs, paginated."""
    from carrac.integrations import _espocrm_request
    contacts = []
    offset = 0
    while True:
        result = _espocrm_request(
            'GET',
            'Contact?select=id,emailAddress,cConvexityUserId,cTier,cLifecycleStage,cLastActive'
            f'&maxSize={_PAGE_SIZE}&offset={offset}'
        )
        batch = (result or {}).get('list') or []
        contacts.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return contacts


def _find_targetlist_id(name):
    from carrac.integrations import _espocrm_request
    result = _espocrm_request(
        'GET',
        f'TargetList?where[0][type]=equals&where[0][attribute]=name&where[0][value]={name}'
    )
    if result and result.get('list'):
        return result['list'][0]['id']
    return None


def _targetlist_member_ids(list_id):
    from carrac.integrations import _espocrm_request
    result = _espocrm_request('GET', f'TargetList/{list_id}/contacts?select=id')
    return {c['id'] for c in (result or {}).get('list') or []}


def _sync_targetlist_membership(list_id, desired_ids):
    """Link missing members, unlink stale ones. Returns (added, removed)."""
    from carrac.integrations import _espocrm_request
    current_ids = _targetlist_member_ids(list_id)
    desired_ids = set(desired_ids)
    to_add = list(desired_ids - current_ids)
    to_remove = list(current_ids - desired_ids)
    if to_add:
        _espocrm_request('POST', f'TargetList/{list_id}/contacts', {'ids': to_add})
    if to_remove:
        _espocrm_request('DELETE', f'TargetList/{list_id}/contacts', {'ids': to_remove})
    return len(to_add), len(to_remove)


def _last_active_dt(user_row):
    raw = user_row.get('last_active')
    if not raw:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def run_reconciliation() -> dict:
    """Repair drift between app users and EspoCRM. Returns summary counts.

    Every step is independently try/excepted so a single failure (e.g. one
    missing TargetList, one bad contact payload) doesn't abort the rest of
    the run.
    """
    summary = {
        'users_scanned': 0,
        'contacts_scanned': 0,
        'contacts_created': 0,
        'tier_patched': 0,
        'targetlist_added': 0,
        'targetlist_removed': 0,
        'targetlists_skipped_missing': 0,
        'opportunities_created': 0,
        'errors': 0,
    }

    try:
        from carrac.integrations import (
            _espocrm_request,
            _find_opportunity_for_contact,
            _create_subscription_opportunity,
        )
        from user_db import user_db

        users = user_db.list_users()
        if not users:
            # user_db.list_users() swallows its own exceptions and returns []
            # rather than raising (user_db.py::list_users) — so this branch also
            # covers the "DB errored" case, not just a genuinely empty table.
            # An empty user table is not a realistic production state (the admin
            # account always exists), so treat empty == outage and refuse to
            # run: without this guard every membership set below computes to
            # empty and _sync_targetlist_membership would unlink EVERY
            # existing member from all 4 TargetLists while reporting
            # errors == 0 (a fail-open wipe).
            logger.error(
                "crm_reconcile: reconcile aborted: user_db returned no users — "
                "refusing fail-open sync"
            )
            _capture_exception(RuntimeError(
                "crm_reconcile: reconcile aborted: user_db returned no users — "
                "refusing fail-open sync"
            ))
            summary['errors'] += 1
            summary['aborted'] = True
            return summary
        summary['users_scanned'] = len(users)

        contacts = _get_all_espo_contacts()
        summary['contacts_scanned'] = len(contacts)

        by_uid = {}
        by_email = {}
        for c in contacts:
            uid = c.get('cConvexityUserId')
            if uid is not None:
                by_uid[str(uid)] = c
            em = (c.get('emailAddress') or '').lower()
            if em:
                by_email[em] = c

        # Steps 3+4: create-missing contacts / patch tier mismatches.
        for u in users:
            try:
                uid = str(u['id'])
                email = (u.get('email') or '').lower()
                tier = _effective_tier(u)
                contact = by_uid.get(uid) or by_email.get(email)
                if not contact:
                    name = (u.get('display_name') or '').strip() or email.split('@')[0] or 'Unknown'
                    parts = name.split()
                    # Consistent with on_tier_change's lifecycle semantics
                    # (carrac/integrations.py): premium -> 'paying'; otherwise
                    # 'activated' if the user has ever been active, else
                    # 'registered' (never logged in since signup).
                    if tier == 'premium':
                        lifecycle_stage = 'paying'
                    elif _last_active_dt(u) is not None:
                        lifecycle_stage = 'activated'
                    else:
                        lifecycle_stage = 'registered'
                    payload = {
                        'firstName': parts[0][:50] if parts else email.split('@')[0],
                        'lastName': parts[-1][:50] if len(parts) > 1 else '—',
                        'emailAddress': u.get('email'),
                        'cConvexityUserId': u['id'],
                        'cTier': tier,
                        'cLifecycleStage': lifecycle_stage,
                    }
                    created = _create_contact(_espocrm_request, payload, uid)
                    if created and created.get('id'):
                        summary['contacts_created'] += 1
                        created = dict(created)
                        created['cConvexityUserId'] = u['id']
                        created['emailAddress'] = u.get('email')
                        created['cTier'] = tier
                        by_uid[uid] = created
                        if email:
                            by_email[email] = created
                        contacts.append(created)
                elif (contact.get('cTier') or '') != tier:
                    _espocrm_request('PUT', f"Contact/{contact['id']}", {'cTier': tier})
                    contact['cTier'] = tier
                    summary['tier_patched'] += 1
            except Exception as e:
                logger.error(f"crm_reconcile: user {u.get('id')} repair failed: {e}")
                _capture_exception(e)
                summary['errors'] += 1

        # Rebuild the 4 TargetLists by exact name.
        now = datetime.now(timezone.utc)
        active_cutoff = now - timedelta(days=ACTIVE_WINDOW_DAYS)

        def _contact_id_for(u):
            uid = str(u['id'])
            email = (u.get('email') or '').lower()
            c = by_uid.get(uid) or by_email.get(email)
            return c.get('id') if c else None

        free_ids, premium_ids, active_ids = [], [], []
        for u in users:
            cid = _contact_id_for(u)
            if not cid:
                continue
            tier = _effective_tier(u)
            if tier == 'free':
                free_ids.append(cid)
            elif tier == 'premium':
                premium_ids.append(cid)
            dt = _last_active_dt(u)
            if dt and dt >= active_cutoff:
                active_ids.append(cid)

        waitlist_ids = [
            c['id'] for c in contacts
            if (c.get('cLifecycleStage') or '') in ('waitlist', 'invited') and c.get('id')
        ]

        desired_by_list = {
            TARGET_LIST_NAMES['free_tier']: free_ids,
            TARGET_LIST_NAMES['premium']: premium_ids,
            TARGET_LIST_NAMES['active_14d']: active_ids,
            TARGET_LIST_NAMES['waitlist']: waitlist_ids,
        }
        for list_name, desired_ids in desired_by_list.items():
            try:
                list_id = _find_targetlist_id(list_name)
                if not list_id:
                    logger.error(
                        f"crm_reconcile: TargetList '{list_name}' not found — skipping "
                        f"(D owns TargetList creation/naming)"
                    )
                    summary['targetlists_skipped_missing'] += 1
                    continue
                added, removed = _sync_targetlist_membership(list_id, desired_ids)
                summary['targetlist_added'] += added
                summary['targetlist_removed'] += removed
            except Exception as e:
                logger.error(f"crm_reconcile: TargetList '{list_name}' sync failed: {e}")
                _capture_exception(e)
                summary['errors'] += 1

        # Premium contacts without an Opportunity.
        for cid in premium_ids:
            try:
                if not _find_opportunity_for_contact(cid):
                    contact = next((c for c in contacts if c.get('id') == cid), None)
                    email = (contact or {}).get('emailAddress') or ''
                    _create_subscription_opportunity(cid, email)
                    summary['opportunities_created'] += 1
            except Exception as e:
                logger.error(f"crm_reconcile: opportunity backfill for contact {cid} failed: {e}")
                _capture_exception(e)
                summary['errors'] += 1

    except Exception as e:
        logger.error(f"crm_reconcile: run failed: {e}")
        _capture_exception(e)
        summary['errors'] += 1

    logger.info(f"crm_reconcile: {summary}")
    return summary
