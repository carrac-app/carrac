#!/usr/bin/env python3
"""Paddle webhook DLQ retry worker.

Re-dispatches failed webhook events from paddle_webhook_inbox that have
retry_count > 0 AND processed_at IS NULL AND retry_count < MAX_RETRIES.

Events with retry_count >= MAX_RETRIES are treated as permanent DLQ items
and generate Telegram admin alerts but are not retried further.

Cron (every 15 minutes; see deploy/crontab.example):
    */15 * * * * cd /path/to/carrac && \
        docker compose exec -T carrac python scripts/paddle_dlq_retry.py >> /var/log/paddle-dlq.log 2>&1

Exponential backoff: only retries events where the age since last attempt
exceeds 2^retry_count minutes (1m, 2m, 4m, …).
"""

import json
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

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from carrac.config import Config  # noqa: E402,F401 — loads .env first (side effect)
from carrac.db_pool import get_db_type  # noqa: E402
from user_db import user_db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s paddle_dlq_retry %(levelname)s %(message)s',
)
logger = logging.getLogger('paddle_dlq_retry')

MAX_RETRIES = 3


def _backoff_elapsed(row) -> bool:
    """Return True if enough time has passed for the next retry attempt."""
    retry_count = row.get('retry_count', 0)
    received_at = row.get('received_at')
    if not received_at:
        return True
    if isinstance(received_at, str):
        try:
            received_at = datetime.fromisoformat(received_at.replace('Z', '+00:00'))
        except ValueError:
            return True
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    backoff_minutes = 2 ** max(0, retry_count - 1)
    return datetime.now(timezone.utc) >= received_at + timedelta(minutes=backoff_minutes)


def run_retry() -> tuple[int, int]:
    """Retry eligible failed webhook events.

    Returns (retried_count, dlq_count).
    """
    from carrac.notifications import send_telegram_message

    conn = user_db._get_conn()

    if get_db_type() == 'postgresql':
        rows = conn.execute(
            '''SELECT id, event_id, event_type, payload, received_at, retry_count
               FROM paddle_webhook_inbox
               WHERE processed_at IS NULL
                 AND retry_count > 0
                 AND retry_count < %s
               ORDER BY received_at''',
            (MAX_RETRIES,),
        ).fetchall()
    else:
        rows = conn.execute(
            '''SELECT id, event_id, event_type, payload, received_at, retry_count
               FROM paddle_webhook_inbox
               WHERE processed_at IS NULL
                 AND retry_count > 0
                 AND retry_count < ?
               ORDER BY received_at''',
            (MAX_RETRIES,),
        ).fetchall()
    # Release idle-in-tx opened by the SELECT (autocommit=False pool)
    try:
        conn.rollback()
    except Exception:
        pass

    retried = 0
    for row in rows:
        row = dict(row)
        if not _backoff_elapsed(row):
            continue

        event_id = row['event_id']
        event_type = row['event_type']
        logger.info(f'dlq_retry: retrying event {event_id} ({event_type}), attempt {row["retry_count"] + 1}')

        try:
            payload = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
            data = payload.get('data', {})
        except Exception as exc:
            logger.error(f'dlq_retry: could not parse payload for {event_id}: {exc}')
            continue

        # Import handlers lazily to avoid circular import at module level.
        from carrac.billing.routes import (
            _handle_transaction_completed,
            _handle_subscription_activated,
            _handle_subscription_updated,
            _handle_subscription_canceled,
            _handle_transaction_refunded,
            _handle_transaction_payment_failed,
            _handle_subscription_past_due,
            _handle_subscription_paused,
            _handle_subscription_resumed,
            _handle_customer_created,
            _inbox_mark_processed,
            _inbox_record_error,
        )

        handler_map = {
            'transaction.completed': _handle_transaction_completed,
            'subscription.activated': _handle_subscription_activated,
            'subscription.updated': _handle_subscription_updated,
            'subscription.canceled': _handle_subscription_canceled,
            'transaction.refunded': _handle_transaction_refunded,
            'transaction.payment_failed': _handle_transaction_payment_failed,
            'subscription.past_due': _handle_subscription_past_due,
            'subscription.paused': _handle_subscription_paused,
            'subscription.resumed': _handle_subscription_resumed,
            'customer.created': _handle_customer_created,
        }

        handler = handler_map.get(event_type)
        if not handler:
            logger.warning(f'dlq_retry: no handler for event type {event_type}; skipping {event_id}')
            continue

        try:
            handler(data, event_id)
            _inbox_mark_processed(event_id)
            retried += 1
            logger.info(f'dlq_retry: event {event_id} succeeded on retry')
        except Exception as exc:
            err = str(exc)
            new_count = _inbox_record_error(event_id, err)
            logger.error(f'dlq_retry: event {event_id} still failing (count={new_count}): {exc}')

    # Report permanent DLQ items (exhausted retries) — send Telegram once per run.
    conn = user_db._get_conn()
    if get_db_type() == 'postgresql':
        dlq_rows = conn.execute(
            '''SELECT event_id, event_type, last_error
               FROM paddle_webhook_inbox
               WHERE processed_at IS NULL AND retry_count >= %s''',
            (MAX_RETRIES,),
        ).fetchall()
    else:
        dlq_rows = conn.execute(
            '''SELECT event_id, event_type, last_error
               FROM paddle_webhook_inbox
               WHERE processed_at IS NULL AND retry_count >= ?''',
            (MAX_RETRIES,),
        ).fetchall()
    try:
        conn.rollback()
    except Exception:
        pass

    dlq_count = len(dlq_rows)
    if dlq_count > 0:
        msg_lines = [f'*Paddle DLQ* — {dlq_count} event(s) exhausted retries:']
        for r in dlq_rows[:5]:
            msg_lines.append(f'  • `{r["event_id"]}` ({r["event_type"]}): {str(r["last_error"] or "")[:80]}')
        if dlq_count > 5:
            msg_lines.append(f'  … and {dlq_count - 5} more. Check paddle_webhook_inbox table.')
        try:
            send_telegram_message('\n'.join(msg_lines))
        except Exception as exc:
            logger.error('dlq_retry: DLQ alert for %d event(s) could not be sent: %s', dlq_count, exc)

    logger.info(f'dlq_retry: done — retried={retried} dlq_count={dlq_count}')
    return retried, dlq_count


if __name__ == '__main__':
    run_retry()
