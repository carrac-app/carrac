"""
Quant alert checker — runs nightly after QE pipeline completes.

Fetches current z-scores and regime state from the quant DB,
then checks all active z-score and regime alerts.

Usage:
    python -m quant.check_alerts
    python -m quant.check_alerts --dry-run

Cron: 25 5 * * * docker exec carrac python -m quant.check_alerts
"""

import argparse
import json
import logging

from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger('quant.check_alerts')


def _fetch_current_zscores() -> dict:
    """Fetch latest z-scores (window=90) as {metric: zscore} dict."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ON (metric)
                metric, zscore
            FROM quant.zscores
            WHERE window_days = 90
            ORDER BY metric, ts DESC
        """)
        return {row['metric']: float(row['zscore']) for row in cur.fetchall()}


def _fetch_current_regime() -> str:
    """Fetch current regime label from latest signal."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT metadata FROM quant.signals
            WHERE source = 'regime_hmm' AND metric = 'REGIME'
            ORDER BY ts DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row and row['metadata']:
            meta = row['metadata'] if isinstance(row['metadata'], dict) else json.loads(row['metadata'])
            return meta.get('label', 'unknown')
    return 'unknown'


def run(dry_run: bool = False):
    """Check all quant alerts against current data."""
    zscores = _fetch_current_zscores()
    regime = _fetch_current_regime()

    logger.info(f"Current regime: {regime}, z-scores for {len(zscores)} metrics")

    if dry_run:
        logger.info("Dry run — skipping alert checks")
        for metric in sorted(zscores.keys())[:10]:
            logger.info(f"  {metric}: z={zscores[metric]:+.2f}")
        return

    import push_notifications
    triggered = push_notifications.check_quant_alerts(zscores, regime)

    if triggered:
        logger.info(f"Triggered {len(triggered)} quant alerts")
        for a in triggered:
            logger.info(f"  {a.get('alert_type')}: {a.get('metric', 'REGIME')} -> user {a.get('user_id')}")
    else:
        logger.info("No quant alerts triggered")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(description='Quant alert checker')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show current data without checking alerts')
    args = parser.parse_args()
    run(dry_run=args.dry_run)
