#!/usr/bin/env python3
"""
Backfill q_value + Fisher-z CIs on historical quant.correlations rows.

Older rows have q_value/ci_low/ci_high/significant = NULL
(those columns were added nullable). This one-shot script
applies BH-FDR per (ts, method, window) bucket and Fisher-z 95% CIs per row
for rows missing q_value.

Usage (inside container):
  docker exec carrac python scripts/backfill_correlations_fdr_ci.py --dry-run
  docker exec carrac python scripts/backfill_correlations_fdr_ci.py --days 365

Staging-first:
  docker exec carrac-worker python scripts/backfill_correlations_fdr_ci.py --days 365

The backfill only touches rows where q_value IS NULL (idempotent — safe to
re-run). Leadlag rows keep q_value NULL by design (cross-correlogram lags
aren't a meaningful FDR family), but their CIs still get filled.
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.exists(os.path.join(_project_root, 'changelog.py')):
    _project_root = '/app'  # inside Docker container
sys.path.insert(0, _project_root)
os.chdir(_project_root)

from carrac.db_pool import get_quant_conn as get_conn
from quant.engine.correlations import _fisher_z_ci

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('backfill-fdr-ci')


SPEARMAN_METHODS = ('spearman', 'spearman_favorable', 'spearman_unfavorable')
LEADLAG_METHODS = ('leadlag', 'leadlag_weekly')


def _bh_qvalues(rows):
    """Return dict {row_idx: q_value} for a list of (idx, p_value) tuples.

    Mirrors _augment_batch's BH math: sort ascending by p, walk from largest
    index down, adj = p * m / (k+1), enforce monotone via running min.
    """
    items = [(idx, float(p)) for idx, p in rows if p is not None]
    items.sort(key=lambda x: x[1])
    m = len(items)
    out = {}
    running_min = 1.0
    for k in range(m - 1, -1, -1):
        idx_k, p_k = items[k]
        adj = p_k * m / (k + 1)
        if adj < running_min:
            running_min = adj
        out[idx_k] = min(1.0, running_min)
    return out


def _process_day_spearman(cur, ts, method, dry_run):
    """Backfill one (ts, method) slice for the three spearman families.

    Both windows (60, 120) are co-computed the same day, so BH-FDR runs per
    window to match the nightly engine's bucketing.
    """
    updated = 0
    for window in (60, 120):
        cur.execute("""
            SELECT metric_a, metric_b, correlation, p_value, n_obs
            FROM quant.correlations
            WHERE ts = %s AND method = %s AND window_days = %s
              AND q_value IS NULL
        """, (ts, method, window))
        rows = cur.fetchall()
        if not rows:
            continue

        # Build (idx, p) list for BH — idx is list-position
        p_items = []
        augmented = []
        for i, r in enumerate(rows):
            r_val = float(r['correlation']) if r['correlation'] is not None else None
            n = int(r['n_obs']) if r['n_obs'] is not None else None
            ci_low, ci_high = _fisher_z_ci(r_val, n)
            augmented.append({
                'metric_a': r['metric_a'],
                'metric_b': r['metric_b'],
                'r_val': r_val,
                'n': n,
                'ci_low': ci_low,
                'ci_high': ci_high,
            })
            if r['p_value'] is not None:
                p_items.append((i, r['p_value']))

        q_by_idx = _bh_qvalues(p_items) if p_items else {}

        # Build UPDATE batch
        batch = []
        for i, a in enumerate(augmented):
            q = q_by_idx.get(i)
            significant = None
            if q is not None and a['n'] is not None and a['r_val'] is not None:
                significant = bool(q < 0.05 and a['n'] >= 30 and abs(a['r_val']) >= 0.1)
            batch.append((
                round(q, 8) if q is not None else None,
                a['ci_low'], a['ci_high'], significant,
                ts, a['metric_a'], a['metric_b'], window, method,
            ))

        if dry_run:
            updated += len(batch)
            continue

        cur.executemany("""
            UPDATE quant.correlations
            SET q_value = %s, ci_low = %s, ci_high = %s, significant = %s
            WHERE ts = %s AND metric_a = %s AND metric_b = %s
              AND window_days = %s AND method = %s
        """, batch)
        updated += len(batch)

    return updated


def _process_day_leadlag(cur, ts, method, dry_run):
    """Backfill CIs only for leadlag rows on this date. q_value stays NULL
    because cross-correlogram lags aren't a meaningful FDR family (one pair
    contributes many rows at different lags; bucketing mixes lag families or
    gives trivial m=1 corrections).
    """
    cur.execute("""
        SELECT metric_a, metric_b, window_days AS lag,
               correlation, n_obs
        FROM quant.correlations
        WHERE ts = %s AND method = %s
          AND ci_low IS NULL AND correlation IS NOT NULL
    """, (ts, method))
    rows = cur.fetchall()
    if not rows:
        return 0

    batch = []
    for r in rows:
        r_val = float(r['correlation']) if r['correlation'] is not None else None
        n = int(r['n_obs']) if r['n_obs'] is not None else None
        ci_low, ci_high = _fisher_z_ci(r_val, n)
        batch.append((
            ci_low, ci_high,
            ts, r['metric_a'], r['metric_b'], r['lag'], method,
        ))

    if dry_run:
        return len(batch)

    cur.executemany("""
        UPDATE quant.correlations
        SET ci_low = %s, ci_high = %s
        WHERE ts = %s AND metric_a = %s AND metric_b = %s
          AND window_days = %s AND method = %s
    """, batch)
    return len(batch)


def backfill(days: int, dry_run: bool):
    end = date.today()
    start = end - timedelta(days=days)
    log.info(f"Backfill window: {start} → {end} ({'DRY RUN' if dry_run else 'APPLY'})")

    total_updated = 0
    with get_conn() as conn:
        cur = conn.cursor()

        # Enumerate distinct ts values in range once — cheap compared to
        # per-day existence checks and gives us accurate progress logging
        cur.execute("""
            SELECT DISTINCT ts
            FROM quant.correlations
            WHERE ts >= %s AND ts <= %s
              AND (q_value IS NULL OR ci_low IS NULL)
            ORDER BY ts
        """, (start, end))
        days_to_process = [r['ts'] for r in cur.fetchall()]

        log.info(f"Found {len(days_to_process)} days with missing q_value or ci_low")

        for i, ts in enumerate(days_to_process):
            day_updated = 0
            for m in SPEARMAN_METHODS:
                day_updated += _process_day_spearman(cur, ts, m, dry_run)
            for m in LEADLAG_METHODS:
                day_updated += _process_day_leadlag(cur, ts, m, dry_run)

            total_updated += day_updated
            if (i + 1) % 25 == 0:
                log.info(f"  Progress: {i+1}/{len(days_to_process)} days — {total_updated} rows so far")

    verb = 'would update' if dry_run else 'updated'
    log.info(f"Done: {verb} {total_updated} rows across {len(days_to_process)} days")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backfill q_value + CIs on quant.correlations')
    parser.add_argument('--days', type=int, default=365, help='Look-back window in days (default 365)')
    parser.add_argument('--dry-run', action='store_true', help='Count updates without writing')
    args = parser.parse_args()
    backfill(args.days, args.dry_run)
