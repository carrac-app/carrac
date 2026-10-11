#!/usr/bin/env python3
"""Hourly subscription expiry sweep.

Finds users with subscription_status = 'cancelled' (or any tier with an
expired subscription_expires_at that the lazy-login check hasn't caught yet)
and downgrades them to 'free'.

Lazy expiry check only fires on login — users who cancel and never
return stay on Premium forever without this sweep. This script closes that
revenue-leak / permission-creep gap.

Cron (hourly; see deploy/crontab.example):
    0 * * * * cd /path/to/carrac && \
        docker compose exec -T carrac python scripts/sweep_expired_subs.py >> /var/log/sub-sweep.log 2>&1

Idempotent: users already on 'free' are skipped.
"""

import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


if __name__ == "__main__":
    # Answer --help before anything below is imported or touched.
    import argparse
    argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    ).parse_args()

# Ensure repo root is on sys.path when run directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Bootstrap config (.env loading) before importing anything that reads os.environ.
from carrac.config import Config  # noqa: E402,F401 — loads .env first (side effect)
from carrac.db_pool import get_db_type  # noqa: E402
from user_db import user_db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s sweep_expired_subs %(levelname)s %(message)s',
)
logger = logging.getLogger('sweep_expired_subs')

# Grace period mirrors SUBSCRIPTION_GRACE_DAYS in user_db.py (3 days). The
# sweep only acts AFTER the grace window has elapsed so it doesn't race with
# Paddle's dunning retries.
GRACE_DAYS = 3


def run_sweep() -> int:
    """Downgrade all qualifying expired-subscription users to free.

    Returns the number of rows downgraded.
    """
    now_utc = datetime.now(timezone.utc)
    grace_cutoff = now_utc - timedelta(days=GRACE_DAYS)
    grace_cutoff_iso = grace_cutoff.isoformat()

    conn = user_db._get_conn()

    # Find users whose subscription has expired beyond the grace window and
    # whose tier is still elevated (member / premium / tester).
    # We intentionally do NOT filter by subscription_status because Paddle's
    # cancellation handler only logs (no state write); the expiry date is the
    # authoritative gate.
    if get_db_type() == 'postgresql':
        candidates = conn.execute(
            '''SELECT id, email, tier, subscription_expires_at
               FROM users
               WHERE tier IN ('member', 'premium', 'tester')
                 AND subscription_expires_at IS NOT NULL
                 AND subscription_expires_at < %s''',
            (grace_cutoff_iso,),
        ).fetchall()
    else:
        candidates = conn.execute(
            '''SELECT id, email, tier, subscription_expires_at
               FROM users
               WHERE tier IN ('member', 'premium', 'tester')
                 AND subscription_expires_at IS NOT NULL
                 AND subscription_expires_at < ?''',
            (grace_cutoff_iso,),
        ).fetchall()

    if not candidates:
        logger.info('sweep: no expired subscriptions found')
        try:
            conn.rollback()  # release idle-in-tx opened by the SELECT
        except Exception:
            pass
        return 0

    downgraded = 0
    for row in candidates:
        uid = row['id']
        old_tier = row['tier']
        try:
            if get_db_type() == 'postgresql':
                conn.execute(
                    '''UPDATE users SET tier = 'free', subscription_expires_at = NULL
                       WHERE id = %s AND tier != 'free' ''',
                    (uid,),
                )
            else:
                conn.execute(
                    '''UPDATE users SET tier = 'free', subscription_expires_at = NULL
                       WHERE id = ? AND tier != 'free' ''',
                    (uid,),
                )
            conn.commit()
            downgraded += 1
            logger.info(
                f'sweep: downgraded user {uid} ({row["email"]}) '
                f'{old_tier} → free '
                f'(expired {row["subscription_expires_at"]})'
            )
        except Exception as exc:
            logger.error(f'sweep: failed to downgrade user {uid}: {exc}')
            try:
                conn.rollback()
            except Exception:
                pass

    logger.info(f'sweep: done — {downgraded}/{len(candidates)} users downgraded')
    return downgraded


if __name__ == '__main__':
    run_sweep()
