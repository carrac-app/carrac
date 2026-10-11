"""Beta waitlist wave-release job.

Picks the next N oldest 'waiting' emails, issues a unique single-use invite code
for each, emails it via Brevo, and marks the row 'invited'. Run manually or via
cron in the prod container:

    docker exec carrac python -m carrac.waitlist_release --count 25 [--cohort wave1]
    docker exec carrac python -m carrac.waitlist_release --stats
    docker exec carrac python -m carrac.waitlist_release --count 5 --dry-run

Email-send is at-least-once: a row is marked 'invited' ONLY after its email
sends, so a Brevo failure leaves the row 'waiting' for the next wave (the
already-generated code is harmless — single-use + it expires).
"""

import argparse
import logging
import os
import sys

logger = logging.getLogger('carrac.waitlist_release')


def _resolve_created_by(override=None):
    """invites.created_by is an FK to users(id); use the admin account."""
    from user_db import user_db
    if override:
        return int(override)
    admin_email = os.getenv('ADMIN_EMAIL', '').strip().lower()
    if admin_email:
        u = user_db.get_user_by_email(admin_email)
        if u:
            return u['id'] if isinstance(u, dict) else u['id']
    return None


def _espo_mark_invited(email, cohort):
    try:
        from carrac.integrations import _espocrm_find_contact_by_email, _espocrm_request
        c, _lookup_ok = _espocrm_find_contact_by_email(email)
        note = "Invited (code issued)" + (f", cohort {cohort}" if cohort else "")
        if c:
            _espocrm_request('PUT', f"Contact/{c['id']}", {
                'description': note,
                'cLifecycleStage': 'invited',
            })
    except Exception as e:
        logger.error(f"EspoCRM mark-invited update failed: {e}")
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(e)
        except Exception:
            pass


def run(count, cohort=None, expires_days=14, dry_run=False, created_by=None):
    from user_db import user_db
    from carrac.email import send_invite_email

    waiting = user_db.count_waitlist('waiting')
    logger.info("Waitlist: %s waiting, %s invited, %s registered.",
                waiting, user_db.count_waitlist('invited'), user_db.count_waitlist('registered'))

    created_by = _resolve_created_by(created_by)
    if not created_by and not dry_run:
        logger.error("No created_by — set ADMIN_EMAIL env or pass --created-by <user_id>.")
        return 1

    batch = user_db.pick_next_waitlist(count)
    if not batch:
        logger.info("Nothing to release (no 'waiting' rows).")
        return 0

    logger.info("Releasing a wave of %d (cohort=%s, expires=%sd, dry_run=%s)...",
                len(batch), cohort or '-', expires_days, dry_run)
    sent = failed = 0
    for row in batch:
        email = row['email']
        if dry_run:
            logger.info("  [dry-run] would invite %s", email)
            continue
        code = user_db.generate_invite_code(created_by, expires_days=expires_days)
        if not code:
            logger.error("Waitlist release: could not generate an invite code for %s", email)
            failed += 1
            continue
        if send_invite_email(email, code, expires_days=expires_days):
            user_db.mark_waitlisted_invited(email, code, cohort=cohort)
            try:
                from carrac.integrations import on_funnel_event
                # Use the invitee's email as distinct_id (not a shared 'anon'), so the
                # event lands on their own PostHog person and stitches with their existing waitlist_joined identity.
                on_funnel_event(email, 'waitlist_invited', {'cohort': cohort or ''})
            except Exception:
                pass
            _espo_mark_invited(email, cohort)
            sent += 1
            logger.info("  invited %s (code %s)", email, code)
        else:
            # Email failed → leave 'waiting' for the next wave; code is orphaned (expires).
            failed += 1
            logger.error("Waitlist release: invite email failed for %s (left waiting; code %s orphaned)",
                         email, code)

    logger.info("Wave done: %d invited, %d failed, %d picked.", sent, failed, len(batch))
    return 0 if failed == 0 else 2


def main():
    p = argparse.ArgumentParser(description="Carrac beta waitlist wave-release")
    p.add_argument('--count', type=int, default=25, help='How many to invite this wave (default 25)')
    p.add_argument('--cohort', default=None, help='Tag this wave (e.g. wave1) — stored on the row + CRM/PostHog')
    p.add_argument('--expires-days', type=int, default=14, help='Invite-code validity (default 14)')
    p.add_argument('--created-by', default=None, help='users(id) for the invite FK (default: ADMIN_EMAIL)')
    p.add_argument('--dry-run', action='store_true', help='Show who would be invited; no codes, no emails, no writes')
    p.add_argument('--stats', action='store_true', help='Print waitlist counts and exit')
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    if args.stats:
        from user_db import user_db
        print(f"waiting={user_db.count_waitlist('waiting')} "
              f"invited={user_db.count_waitlist('invited')} "
              f"registered={user_db.count_waitlist('registered')} "
              f"total={user_db.count_waitlist()}")
        return 0

    return run(count=args.count, cohort=args.cohort, expires_days=args.expires_days,
               dry_run=args.dry_run, created_by=args.created_by)


if __name__ == '__main__':
    sys.exit(main() or 0)
