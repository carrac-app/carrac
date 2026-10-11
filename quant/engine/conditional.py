"""
Quant DB -- Conditional Analysis Engine

Three capabilities:
    1. Conditional return distributions (query-time)
       "When VIX z>2 AND HY spread z>1.5, BTC was down -8% median at 30d"
    2. Multi-domain anomaly clustering (nightly)
       Flags when 3+ metrics from 2+ domains are simultaneously extreme
    3. Historical analogue search (query-time)
       Finds past dates most similar to today's z-score profile

Schedule (nightly, anomaly clusters only): 07:00 UTC.

Usage:
    python -m quant.engine.conditional              # anomaly clusters
    python -m quant.engine.conditional --dry-run
"""

import argparse
import json
import logging
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pandas as pd

from carrac.db_pool import get_quant_conn as get_conn
from quant.engine.run_log import log_finish, log_start
from quant.engine.config import (
    ANALOGUE_METRICS,
    CLUSTER_MIN_DOMAINS,
    CLUSTER_MIN_METRICS,
    TARGET_ASSETS,
    ZSCORE_ALIASES,
)

logger = logging.getLogger('quant.engine.conditional')


# =====================================================================
# 1. Conditional return distributions (query-time)
# =====================================================================

def query_conditional(conditions, target='BTC', horizon=30,
                      include_returns=False):
    """Find historical dates matching z-score conditions, compute forward returns.

    Args:
        conditions: list of strings like ['VIX:gt:2', 'HY_SPREAD:gt:1.5']
        target: asset for forward returns (key in TARGET_ASSETS)
        horizon: forward return horizon in days
        include_returns: if True, include individual forward return values

    Returns:
        dict with instances, stats, matching dates, current_match flag
    """
    parsed = [_parse_condition(c) for c in conditions]

    # Fetch z-score time series for each condition metric
    zscore_series = {}
    with get_conn() as conn:
        cur = conn.cursor()
        for metric, op, threshold in parsed:
            db_metric = ZSCORE_ALIASES.get(metric, metric)
            cur.execute("""
                SELECT ts, zscore FROM quant.zscores
                WHERE metric = %s AND window_days = 90
                ORDER BY ts
            """, (db_metric,))
            rows = cur.fetchall()
            if rows:
                zscore_series[metric] = {
                    r['ts']: float(r['zscore']) for r in rows
                }

    if len(zscore_series) < len(parsed):
        missing = [m for m, _, _ in parsed if m not in zscore_series]
        return {'error': f'no z-score data for: {missing}'}

    # Find dates where ALL conditions hold
    all_dates = set.intersection(
        *[set(s.keys()) for s in zscore_series.values()]
    )
    matching_dates = []
    for d in sorted(all_dates):
        if all(_check_condition(zscore_series[m][d], op, thr)
               for m, op, thr in parsed):
            matching_dates.append(d)

    if not matching_dates:
        return {
            'conditions': conditions,
            'target': target,
            'horizon_days': horizon,
            'instances': 0,
            'forward_return': None,
            'current_match': False,
            'dates': [],
        }

    # Fetch close prices for forward returns
    prices = _fetch_close_prices(target)
    if prices.empty:
        return {'error': f'no price data for target {target}'}

    # Unconditional baseline: forward returns for ALL dates
    all_forward = []
    for d in sorted(prices.index):
        ret = _forward_return(prices, d, horizon)
        if ret is not None:
            all_forward.append(ret)

    unconditional = None
    if all_forward:
        base = np.array(all_forward)
        unconditional = {
            'mean': round(float(np.mean(base)), 4),
            'median': round(float(np.median(base)), 4),
            'std': round(float(np.std(base)), 4),
            'hit_rate': round(float((base > 0).mean()), 3),
            'n': len(all_forward),
        }

    # Compute conditional forward returns
    forward_returns = []
    for d in matching_dates:
        ret = _forward_return(prices, d, horizon)
        if ret is not None:
            forward_returns.append(ret)

    # Check if current date matches
    latest_date = max(all_dates)
    current_match = latest_date in matching_dates

    # Statistics
    if forward_returns:
        arr = np.array(forward_returns)
        stats = {
            'mean': round(float(np.mean(arr)), 4),
            'median': round(float(np.median(arr)), 4),
            'std': round(float(np.std(arr)), 4),
            'hit_rate': round(float((arr > 0).mean()), 3),
            'min': round(float(np.min(arr)), 4),
            'max': round(float(np.max(arr)), 4),
            'p25': round(float(np.percentile(arr, 25)), 4),
            'p75': round(float(np.percentile(arr, 75)), 4),
        }
    else:
        stats = None

    result = {
        'conditions': conditions,
        'target': target,
        'horizon_days': horizon,
        'instances': len(forward_returns),
        'total_matching_dates': len(matching_dates),
        'forward_return': stats,
        'unconditional_baseline': unconditional,
        'current_match': current_match,
        'dates': [str(d) for d in matching_dates[-20:]],  # last 20
    }
    if include_returns and forward_returns:
        result['returns'] = [round(r, 4) for r in forward_returns]
    return result


def _parse_condition(cond_str):
    """Parse 'VIX:gt:2' into ('VIX', '>', 2.0)."""
    parts = cond_str.split(':')
    if len(parts) != 3:
        raise ValueError(f"Invalid condition format: {cond_str} "
                         f"(expected METRIC:op:value)")
    metric = parts[0]
    op_map = {'gt': '>', 'lt': '<', 'gte': '>=', 'lte': '<='}
    if parts[1] not in op_map:
        raise ValueError(f"Unknown operator: {parts[1]} "
                         f"(use gt, lt, gte, lte)")
    return metric, op_map[parts[1]], float(parts[2])


def _check_condition(zscore, op, threshold):
    if op == '>':
        return zscore > threshold
    elif op == '<':
        return zscore < threshold
    elif op == '>=':
        return zscore >= threshold
    elif op == '<=':
        return zscore <= threshold
    return False


def _fetch_close_prices(target):
    """Fetch raw close prices for forward return calculation."""
    if target not in TARGET_ASSETS:
        return pd.Series(dtype=float)
    asset, source = TARGET_ASSETS[target]
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT ts, close FROM quant.price_daily
            WHERE asset = %s AND source = %s AND close IS NOT NULL
            ORDER BY ts
        """, (asset, source))
        rows = cur.fetchall()
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series(
        {r['ts']: float(r['close']) for r in rows}
    ).sort_index()


def _forward_return(prices, start_date, horizon):
    """Compute simple return from start_date to start_date + horizon days."""
    # Find closest available price on or after start_date
    start_price = None
    for offset in range(4):  # allow up to 3 days forward (weekends)
        d = start_date + timedelta(days=offset)
        if d in prices.index:
            start_price = prices[d]
            break
    if start_price is None or start_price <= 0:
        return None

    # Find closest price at horizon
    end_date = start_date + timedelta(days=horizon)
    end_price = None
    for offset in range(4):
        d = end_date + timedelta(days=offset)
        if d in prices.index:
            end_price = prices[d]
            break
    if end_price is None:
        return None

    return (end_price / start_price) - 1


# =====================================================================
# 2. Anomaly cluster detection (nightly)
# =====================================================================

def detect_anomaly_clusters(dry_run=False):
    """Find multi-domain anomaly clusters and store in quant.anomalies.

    A cluster exists when 3+ metrics with |z| >= 2 (90d window) span
    2+ different domains on the latest date.

    Returns:
        Number of rows upserted.
    """
    log_id = log_start('anomaly_clusters', dry_run)

    try:
        with get_conn() as conn:
            cur = conn.cursor()

            # Get latest z-scores with |z| >= 2
            cur.execute("""
                SELECT z.ts, z.domain, z.metric, z.asset,
                       z.zscore, z.raw_value
                FROM quant.zscores z
                INNER JOIN (
                    SELECT domain, metric, asset, MAX(ts) as max_ts
                    FROM quant.zscores WHERE window_days = 90
                    GROUP BY domain, metric, asset
                ) latest
                    ON z.ts = latest.max_ts
                    AND z.domain = latest.domain
                    AND z.metric = latest.metric
                    AND z.asset = latest.asset
                WHERE z.window_days = 90 AND ABS(z.zscore) >= 2.0
                ORDER BY ABS(z.zscore) DESC
            """)
            extremes = cur.fetchall()

        if not extremes:
            logger.info("No extreme z-scores found")
            log_finish(log_id, 'success', 0, dry_run)
            return 0

        # Group by domain
        by_domain = defaultdict(list)
        for r in extremes:
            by_domain[r['domain']].append({
                'metric': r['metric'],
                'asset': r['asset'],
                'zscore': round(float(r['zscore']), 2),
                'raw_value': float(r['raw_value']) if r['raw_value'] else None,
            })

        n_domains = len(by_domain)
        n_metrics = len(extremes)
        today_str = str(date.today())
        ref_date = extremes[0]['ts']

        logger.info(f"Found {n_metrics} extreme metrics across "
                    f"{n_domains} domains on {ref_date}")

        total = 0

        if (n_metrics >= CLUSTER_MIN_METRICS and
                n_domains >= CLUSTER_MIN_DOMAINS):
            logger.info(f"CLUSTER ANOMALY: {n_metrics} metrics, "
                        f"{n_domains} domains")

            meta = {
                'domains_affected': n_domains,
                'metrics': [{
                    'domain': r['domain'],
                    'metric': r['metric'],
                    'zscore': round(float(r['zscore']), 2),
                } for r in extremes],
                'domain_breakdown': {
                    domain: len(items) for domain, items in by_domain.items()
                },
            }

            if not dry_run:
                with get_conn() as conn:
                    cur = conn.cursor()
                    # Upsert cluster anomaly
                    cur.execute("""
                        INSERT INTO quant.anomalies
                            (ts, domain, metric, asset, zscore,
                             window_days, metadata)
                        VALUES (%s, 'cluster', 'MULTI_DOMAIN', '_GLOBAL_',
                                %s, 90, %s)
                        ON CONFLICT DO NOTHING
                    """, (ref_date, n_metrics, json.dumps(meta)))
                    total = cur.rowcount

                    # Resolve old cluster anomalies that are no longer active
                    cur.execute("""
                        UPDATE quant.anomalies
                        SET resolved_at = %s
                        WHERE domain = 'cluster'
                          AND metric = 'MULTI_DOMAIN'
                          AND resolved_at IS NULL
                          AND ts < %s
                    """, (today_str, ref_date))
            else:
                total = 1
        else:
            logger.info(f"No cluster anomaly (need {CLUSTER_MIN_METRICS}+ "
                        f"metrics, {CLUSTER_MIN_DOMAINS}+ domains)")
            # Resolve any previous clusters
            if not dry_run:
                with get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE quant.anomalies
                        SET resolved_at = %s
                        WHERE domain = 'cluster'
                          AND metric = 'MULTI_DOMAIN'
                          AND resolved_at IS NULL
                    """, (today_str,))

        logger.info(f"Anomaly clusters complete: {total} rows "
                    f"{'(dry run)' if dry_run else 'upserted'}")
        log_finish(log_id, 'success', total, dry_run)
        return total

    except Exception as e:
        logger.error(f"Anomaly cluster detection failed: {e}", exc_info=True)
        log_finish(log_id, 'error', 0, dry_run, str(e))
        raise


# =====================================================================
# 3. Historical analogues (query-time)
# =====================================================================

def query_analogues(target='BTC', top_n=5, reference_date=None):
    """Find historical dates most similar to a reference date's z-score profile.

    Uses cosine similarity on ANALOGUE_METRICS z-score vectors.

    Args:
        target: asset for forward returns
        top_n: number of analogues to return
        reference_date: date to compare (default: latest available)

    Returns:
        dict with reference vector, top analogues, forward returns
    """
    with get_conn() as conn:
        cur = conn.cursor()

        # Fetch z-scores for analogue metrics (90d window)
        placeholders = ','.join(['%s'] * len(ANALOGUE_METRICS))
        cur.execute(f"""
            SELECT ts, metric, zscore FROM quant.zscores
            WHERE metric IN ({placeholders})
              AND window_days = 90
            ORDER BY ts
        """, ANALOGUE_METRICS)
        rows = cur.fetchall()

    if not rows:
        return {'error': 'no z-score data available'}

    # Build date → metric → zscore matrix
    date_vectors = defaultdict(dict)
    for r in rows:
        date_vectors[r['ts']][r['metric']] = float(r['zscore'])

    # Compute 7-day z-score deltas for momentum-aware matching
    dates_sorted = sorted(date_vectors.keys())
    for i, d in enumerate(dates_sorted):
        if i < 7:
            continue
        d_prev = dates_sorted[i - 7]
        for metric in list(date_vectors[d].keys()):
            if metric in date_vectors[d_prev]:
                delta = date_vectors[d][metric] - date_vectors[d_prev][metric]
                date_vectors[d][f'{metric}_D7'] = delta

    # Determine reference date (must have at least 5 of 9 analogue metrics)
    all_dates = sorted(date_vectors.keys())
    min_required = 5

    if reference_date:
        from datetime import datetime
        ref = datetime.strptime(reference_date, '%Y-%m-%d').date()
        if ref not in date_vectors:
            return {'error': f'no z-score data for reference date {ref}'}
    else:
        # Walk backward from latest date to find one with enough metrics
        ref = None
        for d in reversed(all_dates):
            if len(date_vectors[d]) >= min_required:
                ref = d
                break
        if ref is None:
            return {'error': 'no date with sufficient z-score coverage'}

    ref_vector = date_vectors[ref]
    ref_metrics = set(ref_vector.keys())

    if len(ref_metrics) < min_required:
        return {'error': f'only {len(ref_metrics)} metrics on {ref}, need {min_required}+'}

    # Fetch prices for forward returns
    prices = _fetch_close_prices(target)

    # Compute cosine similarity for each historical date
    similarities = []
    for d in all_dates:
        if d >= ref - timedelta(days=90):
            continue  # Skip recent 90 days (overlapping windows)

        hist_vector = date_vectors[d]
        common = ref_metrics & set(hist_vector.keys())
        if len(common) < min_required:
            continue

        # Build aligned vectors
        a = np.array([ref_vector[m] for m in sorted(common)])
        b = np.array([hist_vector[m] for m in sorted(common)])

        # Cosine similarity
        dot = np.dot(a, b)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            continue
        sim = float(dot / (norm_a * norm_b))

        # Forward returns at multiple horizons
        fwd = {}
        for h in [7, 30, 90]:
            ret = _forward_return(prices, d, h)
            if ret is not None:
                fwd[f'{h}d'] = round(ret, 4)

        similarities.append({
            'date': str(d),
            'similarity': round(sim, 4),
            'vector': {m: round(hist_vector[m], 2) for m in sorted(common)},
            'forward_return': fwd,
            'n_common_metrics': len(common),
            # Same fraction as n_common_metrics, formatted for verbatim quoting
            # alongside a similarity % (e.g. "89% similar (basis: 6/10)").
            'basis': f'{len(common)}/{len(ANALOGUE_METRICS)}',
        })

    # Sort by similarity descending, take top N
    similarities.sort(key=lambda x: x['similarity'], reverse=True)
    top = similarities[:top_n]

    return {
        'reference_date': str(ref),
        'target': target,
        'current_vector': {m: round(v, 2) for m, v in sorted(ref_vector.items())},
        'n_metrics': len(ref_metrics),
        'analogues': top,
        # Top-level disclosure of the varying similarity basis: each analogue's
        # similarity is computed only over the metrics common to both dates
        # (see each entry's n_common_metrics/basis), so two "89% similar"
        # analogues can rest on different metric subsets.
        'similarity_basis': {
            'analogue_metrics_total': len(ANALOGUE_METRICS),
            'reference_metrics_available': len(ref_metrics),
            'min_common_required': min_required,
            'note': (
                "Similarity for each analogue is computed only over the metrics "
                "available on both the reference date and that historical date; "
                "entries may use different subsets — see each entry's basis field."
            ),
        },
    }


# =====================================================================
# CLI (nightly: anomaly clusters only)
# =====================================================================

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(
        description='Quant DB -- Conditional Analysis Engine')
    parser.add_argument('--dry-run', action='store_true',
                        help='Log actions without writing to DB')
    args = parser.parse_args()
    detect_anomaly_clusters(dry_run=args.dry_run)
