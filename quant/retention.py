"""
Quant DB data retention — periodic cleanup of high-volume tables.

Runs weekly to delete rows older than the configured retention period.
Raw data tables (price, macro, sentiment, derivatives, signals) are kept forever.

Usage:
    python -m quant.retention
    python -m quant.retention --dry-run
"""

import argparse
import logging

from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger('quant.retention')

# (table, time_column, retention_days)
# Correlations and z-scores are kept forever — they're the analytical
# backbone for conditional analysis, analogues, and regime history.
# Raw data can regenerate them, but it's expensive and loses regime-
# dependent correlations that depend on HMM state history.
RETENTION_RULES = [
    ('quant.collection_log', 'started_at', 90),
    ('quant.anomalies', 'ts', 1095),  # 3 years
]


def cleanup(dry_run: bool = False):
    """Delete rows older than retention period from high-volume tables."""
    total = 0
    with get_conn() as conn:
        cur = conn.cursor()
        for table, col, days in RETENTION_RULES:
            if dry_run:
                cur.execute(
                    f"SELECT COUNT(*) AS cnt FROM {table} "
                    f"WHERE {col} < CURRENT_DATE - INTERVAL '%s days'",
                    (days,),
                )
                count = cur.fetchone()['cnt']
                logger.info(f"  {table}: {count} rows older than {days}d (dry run)")
            else:
                cur.execute(
                    f"DELETE FROM {table} "
                    f"WHERE {col} < CURRENT_DATE - INTERVAL '%s days'",
                    (days,),
                )
                count = cur.rowcount
                logger.info(f"  {table}: {count} rows deleted (>{days}d)")
            total += count

    logger.info(f"Retention cleanup complete: {total} total rows {'would be ' if dry_run else ''}removed")
    return total


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(description='Quant DB retention cleanup')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show counts without deleting')
    args = parser.parse_args()

    logger.info("Running retention cleanup%s...", " (dry run)" if args.dry_run else "")
    cleanup(dry_run=args.dry_run)
