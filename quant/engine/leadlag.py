"""
Quant DB -- Lead-Lag Analysis Engine

For key macro→crypto pairs, computes the cross-correlogram at every lag
in a configurable range and identifies the optimal lead time.

Example insight: "M2 turned up 10 weeks ago — historically this led
BTC +15% within the following 10 weeks."

How it works:
    For pair (A, B) at lag k, we compute Spearman(A[t], B[t+k]).
    If peak |r| is at k=+68, A leads B by 68 calendar days.
    Negative k means B leads A.

Data pipeline:
    1. Reuse fetch_all_series() from correlations.py (same transforms)
    2. Align each pair on common daily dates
    3. Trim to analysis window (most recent 730 days)
    4. Compute Spearman at every lag in the pair's range
    5. Store full cross-correlogram in quant.correlations (method='leadlag')

Schedule: Nightly at 06:50 UTC (after correlations at 06:45).

Usage:
    python -m quant.engine.leadlag
    python -m quant.engine.leadlag --dry-run
    python -m quant.engine.leadlag --latest-only
"""

import argparse
import json
import logging
import math
from datetime import date

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from carrac.db_pool import get_quant_conn as get_conn
from quant.engine.run_log import log_finish, log_start
from quant.engine.config import LEADLAG_PAIRS, LEADLAG_WINDOW, MIN_OBSERVATIONS
from quant.engine.correlations import fetch_all_series

PERSISTENCE_TOLERANCE = 7  # days — sub-window best lags must be within ±7d of full

# Maximum permutations for the circular-shift null distribution.
# Capped at 200 so the nightly job stays cheap on a 2-CPU host.
# 200 permutations → resolution ~0.005 in p_corrected.
_MAX_PERMS = 200

logger = logging.getLogger('quant.engine.leadlag')


def _best_lag_for_window(vals_a, vals_b, lag_min, lag_max):
    """Find the lag with highest |Spearman r| for a given data window."""
    best_lag, best_r = 0, 0.0
    for lag in range(lag_min, lag_max + 1):
        if lag > 0:
            a, b = vals_a[:-lag], vals_b[lag:]
        elif lag < 0:
            a, b = vals_a[-lag:], vals_b[:lag]
        else:
            a, b = vals_a, vals_b
        if len(a) < MIN_OBSERVATIONS:
            continue
        if a.max() == a.min() or b.max() == b.min():
            continue
        r, _ = spearmanr(a, b)
        if not np.isnan(r) and abs(r) > abs(best_r):
            best_r, best_lag = r, lag
    return best_lag, best_r


def _permutation_pvalue(vals_a, vals_b, lag_min, lag_max, best_r, n_perms=_MAX_PERMS):
    """Compute a multiple-comparison-corrected p-value for the lead-lag max-statistic.

    Method: circular-shift null distribution for the max|r| over all lags.
    A circular shift of vals_b by a random offset breaks any temporal
    relationship while preserving each series' marginal distribution and
    autocorrelation structure (unlike iid permutation, which would destroy
    ffill-induced autocorrelation and deflate the null).

    Algorithm:
        1. Compute max|r| over all lags on the real data (= |best_r|).
        2. For each of n_perms random circular shifts of vals_b:
           a. Compute the cross-correlogram over the same lag range.
           b. Record the maximum |r| over all lags for this permuted pair.
        3. p_corrected = fraction of permuted max-|r| values >= |best_r|.

    This is the exact p-value for the max-statistic null, controlling
    for multiple comparisons across lags without assuming independence
    between lags (which they are not — overlapping windows).

    CPU cost: ~n_perms * n_lags spearmanr calls.  For M2→BTC (181 lags,
    200 perms) that is ~36 200 calls — each on ≤730 obs → <2 s total on
    the 2-CPU box.  For short-range pairs (±14 lags, ~28 lags) it is
    ~5 600 calls (<0.3 s).  Total across all pairs in the nightly job
    is within the existing ~20-min job window.

    Args:
        vals_a, vals_b: np.ndarray, the aligned leader/target series.
        lag_min, lag_max: lag range tested.
        best_r: float, the observed max-statistic (signed).
        n_perms: int, number of permutations (default _MAX_PERMS=200).

    Returns:
        float p_corrected in [0, 1].  Returns 1.0 if too few observations.
    """
    n = len(vals_a)
    if n < MIN_OBSERVATIONS or n_perms < 1:
        return 1.0

    observed_stat = abs(best_r)
    count_ge = 0  # how many perm max-|r| values >= observed

    rng = np.random.default_rng(seed=42)  # deterministic for reproducibility

    for _ in range(n_perms):
        shift = int(rng.integers(1, n))  # at least 1 so we don't repeat the data
        b_shifted = np.roll(vals_b, shift)

        perm_max_r = 0.0
        for lag in range(lag_min, lag_max + 1):
            if lag > 0:
                a_sl = vals_a[:-lag]
                b_sl = b_shifted[lag:]
            elif lag < 0:
                a_sl = vals_a[-lag:]
                b_sl = b_shifted[:lag]
            else:
                a_sl = vals_a
                b_sl = b_shifted

            if len(a_sl) < MIN_OBSERVATIONS:
                continue
            if a_sl.max() == a_sl.min() or b_sl.max() == b_sl.min():
                continue

            pr, _ = spearmanr(a_sl, b_sl)
            if not np.isnan(pr) and abs(pr) > perm_max_r:
                perm_max_r = abs(pr)

        if perm_max_r >= observed_stat:
            count_ge += 1

    return count_ge / n_perms


def _resample_weekly(vals_a, vals_b, index):
    """Resample daily-aligned arrays to weekly (Friday close).

    Returns (weekly_a, weekly_b, weekly_index) or (None, None, None)
    if insufficient data after resampling.
    """
    df = pd.DataFrame({'a': vals_a, 'b': vals_b}, index=index)
    weekly = df.resample('W-FRI').last().dropna()
    if len(weekly) < MIN_OBSERVATIONS:
        return None, None, None
    return weekly['a'].values, weekly['b'].values, weekly.index


def compute_leadlag(dry_run=False, latest_only=False, frequency='daily'):
    """Compute lead-lag cross-correlograms for all focus pairs.

    For each pair and each lag in its range, computes Spearman rank
    correlation between the lagged leader and the target. Stores the
    full correlogram so the API can find the best lag.

    Args:
        dry_run: Log without writing to DB.
        latest_only: Only compute for today (nightly mode). For lead-lag
                     the result is the same since we always use the most
                     recent analysis window — this flag is accepted for
                     CLI consistency but doesn't change behavior.
        frequency: 'daily' or 'weekly'. Weekly resamples to Friday close
                   and divides lag ranges by 7 (each lag step = 1 week).

    Returns:
        Total rows upserted.
    """
    weekly = frequency == 'weekly'
    method = 'leadlag_weekly' if weekly else 'leadlag'
    summary_source = 'leadlag_weekly_summary' if weekly else 'leadlag_summary'
    collector_name = 'leadlag_weekly' if weekly else 'leadlag'
    log_id = log_start(collector_name, dry_run)

    try:
        logger.info("Fetching canonical metric series...")
        all_series = fetch_all_series()
        logger.info(f"Loaded {len(all_series)} metrics (frequency={frequency})")

        today = str(date.today())
        total_rows = 0
        batch = []
        summary_rows = []

        for leader, target, (lag_min, lag_max), desc in LEADLAG_PAIRS:
            if leader not in all_series:
                logger.warning(f"Leader '{leader}' not in loaded series, skipping")
                continue
            if target not in all_series:
                logger.warning(f"Target '{target}' not in loaded series, skipping")
                continue

            sa = all_series[leader]
            sb = all_series[target]

            # Align on common dates
            aligned = pd.concat([sa, sb], axis=1, join='inner').dropna()
            if len(aligned) < MIN_OBSERVATIONS:
                logger.warning(f"{leader} → {target}: only {len(aligned)} "
                               f"common points, skipping")
                continue

            # Trim to analysis window (most recent N days)
            aligned = aligned.tail(LEADLAG_WINDOW)

            if weekly:
                wa, wb, widx = _resample_weekly(
                    aligned.iloc[:, 0].values,
                    aligned.iloc[:, 1].values,
                    aligned.index,
                )
                if wa is None:
                    logger.warning(f"{leader} → {target}: insufficient weekly "
                                   f"data after resampling, skipping")
                    continue
                vals_a, vals_b = wa, wb
                # Convert daily lag range to weekly
                wlag_min = math.ceil(lag_min / 7)
                wlag_max = math.floor(lag_max / 7)
            else:
                vals_a = aligned.iloc[:, 0].values
                vals_b = aligned.iloc[:, 1].values
                wlag_min, wlag_max = lag_min, lag_max

            n_total = len(vals_a)

            best_lag = 0
            best_r = 0.0
            pair_rows = 0

            for lag in range(wlag_min, wlag_max + 1):
                if lag > 0:
                    a = vals_a[:-lag]
                    b = vals_b[lag:]
                elif lag < 0:
                    a = vals_a[-lag:]
                    b = vals_b[:lag]
                else:
                    a = vals_a
                    b = vals_b

                n = len(a)
                if n < MIN_OBSERVATIONS:
                    continue

                # Skip constant arrays
                if a.max() == a.min() or b.max() == b.min():
                    continue

                r, p = spearmanr(a, b)
                if np.isnan(r):
                    continue

                batch.append((
                    today,
                    leader,
                    target,
                    lag,              # stored in window_days column
                    round(float(r), 6),
                    round(float(p), 8),
                    n,
                    method,
                ))
                pair_rows += 1

                if abs(r) > abs(best_r):
                    best_r = r
                    best_lag = lag

            # Sub-window persistence check
            half = n_total // 2
            persistent = False
            sub1_lag, sub1_r, sub2_lag, sub2_r = 0, 0.0, 0, 0.0
            tol = max(1, PERSISTENCE_TOLERANCE // 7) if weekly else PERSISTENCE_TOLERANCE
            if half >= MIN_OBSERVATIONS:
                sub1_a, sub1_b = vals_a[:half], vals_b[:half]
                sub2_a, sub2_b = vals_a[half:], vals_b[half:]
                sub1_lag, sub1_r = _best_lag_for_window(sub1_a, sub1_b, wlag_min, wlag_max)
                sub2_lag, sub2_r = _best_lag_for_window(sub2_a, sub2_b, wlag_min, wlag_max)
                persistent = (abs(best_lag - sub1_lag) <= tol and
                              abs(best_lag - sub2_lag) <= tol)

            # Multiple-comparisons correction for the max-statistic.
            # We tested max|r| over (wlag_max - wlag_min + 1) lags; a naive
            # per-lag p-value is anti-conservative.  Circular-shift permutation
            # gives the exact null for the max-statistic at low CPU cost.
            p_corrected = _permutation_pvalue(
                vals_a, vals_b, wlag_min, wlag_max, best_r, n_perms=_MAX_PERMS
            )

            unit = "w" if weekly else "d"
            tag = "✓" if persistent else "✗"
            logger.info(f"  {leader:20s} → {target}: best_lag={best_lag:+4d}{unit}  "
                        f"r={best_r:+.4f}  p_corrected={p_corrected:.3f}  "
                        f"({pair_rows} lags, n={n_total} obs)  persist={tag} "
                        f"[sub1={sub1_lag:+d}, sub2={sub2_lag:+d}]")

            # Store persistence summary in quant.signals
            summary_rows.append((
                today, summary_source, f'{leader}__{target}', '_GLOBAL_',
                best_lag,
                json.dumps({
                    'best_r': round(best_r, 4),
                    'p_corrected': round(p_corrected, 4),
                    'n_lags_tested': pair_rows,
                    'sub1_lag': sub1_lag, 'sub1_r': round(sub1_r, 4),
                    'sub2_lag': sub2_lag, 'sub2_r': round(sub2_r, 4),
                    'persistent': persistent,
                    'tolerance': tol,
                    'frequency': frequency,
                }),
            ))

        # Flush correlogram rows
        if batch:
            if not dry_run:
                _upsert_batch(batch)
            total_rows = len(batch)

        # Flush persistence summaries to quant.signals
        if summary_rows and not dry_run:
            _upsert_summaries(summary_rows)

        # Store chain strength time series
        if summary_rows and not dry_run:
            _store_chain_strengths(today, summary_rows, frequency)

        persistent_count = sum(1 for r in summary_rows
                               if json.loads(r[5]).get('persistent'))
        logger.info(f"Lead-lag ({frequency}) complete: {total_rows} rows "
                    f"{'(dry run)' if dry_run else 'upserted'}, "
                    f"{persistent_count}/{len(summary_rows)} pairs persistent")
        log_finish(log_id, 'success', total_rows, dry_run)
        return total_rows

    except Exception as e:
        logger.error(f"Lead-lag engine failed: {e}", exc_info=True)
        log_finish(log_id, 'error', 0, dry_run, str(e))
        raise


def _store_chain_strengths(today, summary_rows, frequency):
    """Compute and persist chain_strength for all transmission chains."""
    import math
    from quant.engine.config import TRANSMISSION_CHAINS

    summary_source = 'leadlag_weekly_summary' if frequency == 'weekly' else 'leadlag_summary'

    # Build lookup: metric -> best_r from just-computed summaries
    best_rs = {}
    for row in summary_rows:
        # row = (today, source, metric, asset, value, metadata_json)
        if row[1] == summary_source:
            meta = json.loads(row[5])
            best_rs[row[2]] = meta.get('best_r', 0)

    chain_rows = []
    suffix = '_weekly' if frequency == 'weekly' else ''
    for cname, cdef in TRANSMISSION_CHAINS.items():
        abs_rs = []
        per_link = {}
        weakest = {'from': '', 'to': '', 'r': 999}
        for a, b in cdef['links']:
            key = f'{a}__{b}'
            r_val = best_rs.get(key, 0)
            ar = max(abs(r_val), 0.001)
            abs_rs.append(ar)
            per_link[key] = round(r_val, 4)
            if ar < abs(weakest['r']):
                weakest = {'from': a, 'to': b, 'r': round(r_val, 4)}

        geo = math.exp(sum(math.log(r) for r in abs_rs) / len(abs_rs))

        chain_rows.append((
            today, f'chain_strength{suffix}', cname, '_GLOBAL_',
            round(geo, 4),
            json.dumps({
                'weakest_link': weakest,
                'per_link': per_link,
                'frequency': frequency,
            }),
        ))

    if chain_rows:
        _upsert_summaries(chain_rows)
        logger.info(f"Stored {len(chain_rows)} chain_strength{suffix} rows")


def _upsert_batch(rows):
    """Batch upsert lead-lag rows. Populates ci_low/ci_high via Fisher-z
    (per-row, no dependencies). q_value/significant stay NULL for leadlag
    because BH-FDR across cross-correlogram lags isn't the right correction
    — one pair contributes many rows at different lags, so bucketing would
    either mix lag families or give each row a trivial m=1 bucket.
    """
    from psycopg2.extras import execute_values
    from quant.engine.correlations import _fisher_z_ci

    # Row tuple currently: (ts, leader, target, lag, r, p, n, method)
    # Expand to 10 fields by appending (ci_low, ci_high).
    augmented = []
    for r in rows:
        ts, a, b, lag, corr, pval, n_obs, method = r
        r_val = float(corr) if corr is not None else None
        n = int(n_obs) if n_obs is not None else None
        ci_low, ci_high = _fisher_z_ci(r_val, n)
        augmented.append((ts, a, b, lag, corr, pval, n_obs, method, ci_low, ci_high))

    sql = """
        INSERT INTO quant.correlations
            (ts, metric_a, metric_b, window_days, correlation,
             p_value, n_obs, method, ci_low, ci_high)
        VALUES %s
        ON CONFLICT (ts, metric_a, metric_b, window_days, method)
        DO UPDATE SET correlation = EXCLUDED.correlation,
                      p_value     = EXCLUDED.p_value,
                      n_obs       = EXCLUDED.n_obs,
                      ci_low      = EXCLUDED.ci_low,
                      ci_high     = EXCLUDED.ci_high
    """
    with get_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, sql, augmented, page_size=500)


def _upsert_summaries(rows):
    """Store lead-lag persistence summaries in quant.signals."""
    from psycopg2.extras import execute_values

    sql = """
        INSERT INTO quant.signals (ts, source, metric, asset, value, metadata)
        VALUES %s
        ON CONFLICT (ts, source, metric, asset)
        DO UPDATE SET value    = EXCLUDED.value,
                      metadata = EXCLUDED.metadata
    """
    with get_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, sql, rows, page_size=100)


# ── CLI ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(
        description='Quant DB -- Lead-Lag Analysis Engine')
    parser.add_argument('--dry-run', action='store_true',
                        help='Log actions without writing to DB')
    parser.add_argument('--latest-only', action='store_true',
                        help='Accepted for CLI consistency (no-op)')
    parser.add_argument('--frequency', choices=['daily', 'weekly'],
                        default='daily',
                        help='Aggregation frequency (weekly resamples to Friday)')
    args = parser.parse_args()
    compute_leadlag(dry_run=args.dry_run, latest_only=args.latest_only,
                    frequency=args.frequency)
