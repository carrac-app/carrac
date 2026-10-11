"""
Quant DB -- Rolling Correlation Engine

Computes Spearman rank correlations between canonical metrics across all
domains (price, macro, derivatives, sentiment).

Why Spearman instead of Pearson?
    - Robust to outliers and fat tails (common in financial returns)
    - Captures monotonic relationships (not just linear)
    - Doesn't assume normality

Data pipeline:
    1. Fetch raw values from DB for each canonical metric
    2. Apply transform (log_return / first_diff / raw)
    3. Forward-fill to daily frequency (handles weekends, sparse weekly/monthly)
    4. Align pairs on common daily dates
    5. Compute rolling Spearman for 60d and 120d windows
    6. Batch upsert to quant.correlations

Schedule: Nightly at 06:45 UTC (after normalize.py at 06:30).

Usage:
    python -m quant.engine.correlations
    python -m quant.engine.correlations --dry-run
    python -m quant.engine.correlations --latest-only
"""

import argparse
import logging
import math
from itertools import combinations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from carrac.db_pool import get_quant_conn as get_conn
from quant.engine.run_log import log_finish, log_start
from quant.engine.config import (
    CANONICAL_METRICS,
    CORRELATION_WINDOWS,
    MIN_OBSERVATIONS,
)

logger = logging.getLogger('quant.engine.correlations')


# =====================================================================
# Data fetching & transformation
# =====================================================================

def fetch_all_series():
    """Fetch and transform all canonical metric time series.

    Returns:
        dict: metric_name -> pd.Series (DatetimeIndex, daily, forward-filled)
    """
    result = {}

    with get_conn() as conn:
        cur = conn.cursor()
        _fetch_price(cur, result)
        _fetch_macro(cur, result)
        _fetch_derivatives(cur, result)
        _fetch_sentiment(cur, result)
        _fetch_onchain(cur, result)

    return result


def _fetch_price(cur, result):
    """Fetch close prices for all price_daily metrics."""
    cfgs = {k: v for k, v in CANONICAL_METRICS.items()
            if v['table'] == 'price_daily'}
    if not cfgs:
        return

    # Collect unique (asset, source) pairs needed
    needed = set()
    for cfg in cfgs.values():
        needed.add((cfg['asset'], cfg['source']))

    conds = ' OR '.join(['(asset = %s AND source = %s)'] * len(needed))
    params = []
    for a, s in needed:
        params.extend([a, s])

    cur.execute(f"""
        SELECT ts, asset, source, close FROM quant.price_daily
        WHERE close IS NOT NULL AND ({conds})
        ORDER BY asset, source, ts
    """, params)

    groups = {}
    for row in cur.fetchall():
        key = (row['asset'], row['source'])
        groups.setdefault(key, []).append((row['ts'], float(row['close'])))

    for name, cfg in cfgs.items():
        raw = groups.get((cfg['asset'], cfg['source']), [])
        if not raw:
            logger.warning(f"No price data for {name}")
            continue
        result[name] = _transform(raw, cfg['transform'])


def _fetch_macro(cur, result):
    """Fetch values for all macro metrics."""
    cfgs = {k: v for k, v in CANONICAL_METRICS.items()
            if v['table'] == 'macro'}
    if not cfgs:
        return

    metrics = list(set(v['metric'] for v in cfgs.values()))
    ph = ','.join(['%s'] * len(metrics))
    cur.execute(f"""
        SELECT COALESCE(released_at::date, ts) AS ts, metric, value FROM quant.macro
        WHERE metric IN ({ph}) AND value IS NOT NULL
        ORDER BY metric, COALESCE(released_at::date, ts)
    """, metrics)

    groups = {}
    for row in cur.fetchall():
        groups.setdefault(row['metric'], []).append(
            (row['ts'], float(row['value'])))

    for name, cfg in cfgs.items():
        raw = groups.get(cfg['metric'], [])
        if not raw:
            logger.warning(f"No macro data for {name}")
            continue
        result[name] = _transform(raw, cfg['transform'])


def _fetch_derivatives(cur, result):
    """Fetch values for all derivatives metrics."""
    cfgs = {k: v for k, v in CANONICAL_METRICS.items()
            if v['table'] == 'derivatives'}
    if not cfgs:
        return

    assets = list(set(v['asset'] for v in cfgs.values()))
    columns = sorted(set(v['column'] for v in cfgs.values()))
    col_sql = ', '.join(columns)
    ph = ','.join(['%s'] * len(assets))

    cur.execute(f"""
        SELECT ts, asset, {col_sql} FROM quant.derivatives
        WHERE asset IN ({ph})
        ORDER BY asset, ts
    """, assets)

    rows_by_asset = {}
    for row in cur.fetchall():
        rows_by_asset.setdefault(row['asset'], []).append(row)

    for name, cfg in cfgs.items():
        col = cfg['column']
        asset_rows = rows_by_asset.get(cfg['asset'], [])
        raw = [(r['ts'], float(r[col])) for r in asset_rows
               if r.get(col) is not None]
        if not raw:
            logger.warning(f"No derivatives data for {name}")
            continue
        result[name] = _transform(raw, cfg['transform'])


def _fetch_sentiment(cur, result):
    """Fetch values for all sentiment metrics."""
    cfgs = {k: v for k, v in CANONICAL_METRICS.items()
            if v['table'] == 'sentiment'}
    if not cfgs:
        return

    conds = ' OR '.join(['(metric = %s AND asset = %s)'] * len(cfgs))
    params = []
    for cfg in cfgs.values():
        params.extend([cfg['metric'], cfg['asset']])

    cur.execute(f"""
        SELECT COALESCE(released_at::date, ts) AS ts, metric, asset, value FROM quant.sentiment
        WHERE ({conds})
        ORDER BY metric, asset, COALESCE(released_at::date, ts)
    """, params)

    groups = {}
    for row in cur.fetchall():
        key = (row['metric'], row['asset'])
        groups.setdefault(key, []).append(
            (row['ts'], float(row['value'])))

    for name, cfg in cfgs.items():
        key = (cfg['metric'], cfg['asset'])
        raw = groups.get(key, [])
        if not raw:
            logger.warning(f"No sentiment data for {name}")
            continue
        result[name] = _transform(raw, cfg['transform'])


def _fetch_onchain(cur, result):
    """Fetch values for all onchain metrics."""
    cfgs = {k: v for k, v in CANONICAL_METRICS.items()
            if v['table'] == 'onchain'}
    if not cfgs:
        return

    conds = ' OR '.join(['(metric = %s AND asset = %s)'] * len(cfgs))
    params = []
    for cfg in cfgs.values():
        params.extend([cfg['metric'], cfg['asset']])

    cur.execute(f"""
        SELECT ts, metric, asset, value FROM quant.onchain
        WHERE ({conds})
        ORDER BY metric, asset, ts
    """, params)

    groups = {}
    for row in cur.fetchall():
        key = (row['metric'], row['asset'])
        groups.setdefault(key, []).append(
            (row['ts'], float(row['value'])))

    for name, cfg in cfgs.items():
        key = (cfg['metric'], cfg['asset'])
        raw = groups.get(key, [])
        if not raw:
            logger.warning(f"No onchain data for {name}")
            continue
        result[name] = _transform(raw, cfg['transform'])


def _transform(raw_pairs, transform):
    """Apply transform to (date, value) pairs, return forward-filled daily Series.

    Pipeline:
        1. Build raw-level Series from original observation dates
        2. Forward-fill to daily frequency with a staleness limit
        3. Apply mathematical transform on the filled daily series

    Forward-filling levels first, then transforming, avoids duplicating
    change values as artificial observations (e.g. a weekly COT diff
    repeated 7 times would inflate Spearman r and deflate p-values).

    A ffill limit is applied so that a dead/stale collector does not
    silently feed its last value forever into correlations.  Limits are
    frequency-aware (monthly ≈ 35 days, weekly ≈ 10 days, daily ≈ 5
    days).  A series whose collector has been silent longer than its
    limit will have NaN gaps, which propagate through the transform and
    are dropped by dropna() — the pair will fail the MIN_OBSERVATIONS
    guard and be skipped rather than silently correlating stale data.

    Limitation: for sparse series (COT weekly, FRED monthly), forward-
    filling levels then differencing still produces runs of zeros between
    actual observations, so effective sample size is lower than the
    nominal window. The ideal approach would correlate on common actual
    observation dates only; this is a known limitation that is partially
    addressed by the effective-n correction in _compute_n_eff().

    Args:
        raw_pairs: [(date, value), ...] sorted by date
        transform: 'log_return', 'first_diff', or 'raw'

    Returns:
        pd.Series with DatetimeIndex, daily frequency
    """
    if not raw_pairs:
        return pd.Series(dtype=float)

    # Step 1: Build raw-level Series
    data = {d: v for d, v in raw_pairs}
    s = pd.Series(data)
    s.index = pd.DatetimeIndex(s.index)
    s = s.sort_index()

    # Infer observation frequency from typical gap between raw points.
    # Use the median gap rather than the mean to be robust to irregular
    # bursts (e.g. FRED catch-up releases).
    ffill_limit = _ffill_limit(s)

    # Step 2: Forward-fill to daily frequency (with staleness limit)
    s = s.resample('D').ffill(limit=ffill_limit)

    # Step 3: Apply transform on the filled daily series
    if transform == 'log_return':
        s = np.log(s / s.shift(1))
        s = s.iloc[1:]
    elif transform == 'first_diff':
        s = s.diff()
        s = s.iloc[1:]

    return s.dropna()


def _ffill_limit(s):
    """Compute a sensible forward-fill limit (in days) from the raw observation cadence.

    Returns:
        int — maximum number of calendar days to carry a value forward.
        Daily series → 5 days (covers weekends + 1 missed day).
        Weekly series → 10 days (~1.5 weeks, tolerates 1 missed week).
        Monthly series → 35 days (~5 weeks, tolerates 1 missed month + margin).
        Sparser → 35 days (conservative default; very sparse series
        should correlate on actual-observation dates, not ffilled daily).
    """
    if len(s) < 2:
        return 35  # can't infer, use conservative default
    gaps = s.index.to_series().diff().dropna().dt.days
    if len(gaps) == 0:
        return 35
    median_gap = float(gaps.median())
    if median_gap <= 1.5:
        return 5    # daily
    if median_gap <= 8:
        return 10   # weekly
    return 35       # monthly or sparser


def _compute_n_eff(a_win, b_win):
    """Compute effective sample size corrected for serial autocorrelation and
    forward-fill ties, to use in Fisher-z confidence intervals.

    Effective n is the minimum of:
      (a) Count of distinct non-zero values in the shorter (more sparse) series
          — a proxy for actual observation count after ffill expansion.
          For daily series (no ties) this equals the window length.
      (b) Bartlett-formula ESS: n * (1 - r1a*r1b) / (1 + r1a*r1b)
          where r1a, r1b are the lag-1 autocorrelations of each series.
          This corrects for serial dependence introduced by ffill and
          any genuine autocorrelation in the raw signal.

    Returns an int in [4, len(a_win)].  Returns len(a_win) if the
    computation cannot be performed (too short, constant, etc.).
    """
    n = len(a_win)
    if n < 8:
        return n

    # (a) distinct-value count: non-zero differences → actual new obs
    #     Use the minimum across both series (most sparse wins).
    def _distinct_count(arr):
        diffs = np.diff(arr.astype(float))
        n_changes = int(np.count_nonzero(diffs))
        return max(n_changes + 1, 4)  # at least 4

    n_distinct_a = _distinct_count(a_win)
    n_distinct_b = _distinct_count(b_win)
    n_distinct = min(n_distinct_a, n_distinct_b)

    # (b) Bartlett ESS — protect against constant arrays
    try:
        # lag-1 autocorrelation via np.corrcoef on shifted views
        def _lag1_autocorr(arr):
            if arr.std() == 0:
                return 0.0
            a0, a1 = arr[:-1], arr[1:]
            if a0.std() == 0 or a1.std() == 0:
                return 0.0
            r = float(np.corrcoef(a0, a1)[0, 1])
            return r if not np.isnan(r) else 0.0

        r1a = _lag1_autocorr(a_win.astype(float))
        r1b = _lag1_autocorr(b_win.astype(float))
        denom = 1.0 + r1a * r1b
        if abs(denom) < 1e-9:
            n_bartlett = n
        else:
            n_bartlett = max(4, int(n * (1.0 - r1a * r1b) / denom))
    except Exception:
        n_bartlett = n

    # Take the conservative (smaller) of both estimates, capped at n
    return min(n_distinct, n_bartlett, n)


# =====================================================================
# Correlation computation
# =====================================================================

def compute_rolling_correlations(dry_run=False, latest_only=False):
    """Compute rolling Spearman correlations for all canonical metric pairs.

    For each pair of N=23 metrics (253 unique pairs) and each window
    (60d, 120d), computes rolling Spearman rank correlation on their
    common daily dates.

    Args:
        dry_run: Log without writing to DB
        latest_only: Only compute for the most recent date (nightly mode)

    Returns:
        Total rows upserted
    """
    log_id = log_start('correlations', dry_run)

    try:
        logger.info("Fetching canonical metric series...")
        all_series = fetch_all_series()
        loaded = sorted(all_series.keys())
        logger.info(f"Loaded {len(loaded)} metrics: {loaded}")

        # Fetch regime states for regime-dependent correlations
        regime_dates = {}  # date → 'favorable' | 'unfavorable'
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT ts, metadata->>'label' AS label
                    FROM quant.signals
                    WHERE source = 'regime_hmm' AND metric = 'REGIME'
                """)
                for row in cur.fetchall():
                    ts = row['ts']
                    # Normalize to date for consistent lookup
                    if hasattr(ts, 'date'):
                        ts = ts.date()
                    regime_dates[ts] = row['label']
            logger.info(f"Loaded {len(regime_dates)} regime state days")
        except Exception as e:
            logger.warning(f"Could not load regime states (regime-split skipped): {e}")

        for name, s in all_series.items():
            logger.info(f"  {name}: {len(s)} daily points, "
                        f"{s.index[0].date()} to {s.index[-1].date()}")

        pairs = list(combinations(loaded, 2))
        logger.info(f"Computing correlations for {len(pairs)} pairs x "
                    f"{len(CORRELATION_WINDOWS)} windows")

        total_rows = 0
        batch = []
        skipped = 0

        for pair_idx, (name_a, name_b) in enumerate(pairs):
            sa = all_series[name_a]
            sb = all_series[name_b]

            # Align on common dates (inner join)
            combined = pd.concat([sa, sb], axis=1, join='inner').dropna()
            if len(combined) < MIN_OBSERVATIONS:
                skipped += 1
                continue

            dates = combined.index
            vals_a = combined.iloc[:, 0].values
            vals_b = combined.iloc[:, 1].values

            for window in CORRELATION_WINDOWS:
                n_dates = len(dates)
                if n_dates < window:
                    continue

                start = (n_dates - 1) if latest_only else (window - 1)

                for i in range(start, n_dates):
                    a_win = vals_a[i - window + 1:i + 1]
                    b_win = vals_b[i - window + 1:i + 1]

                    # Spearman is undefined for constant arrays
                    if a_win.max() == a_win.min() or b_win.max() == b_win.min():
                        continue

                    r, p = spearmanr(a_win, b_win)
                    if np.isnan(r):
                        continue

                    # Effective sample size: corrects CIs for ffill ties and
                    # serial autocorrelation. Stored alongside nominal n_obs
                    # (= window constant) — CIs are computed from n_eff.
                    n_eff = _compute_n_eff(a_win, b_win)

                    # Sub-window stability: std of correlations over half-window segments
                    stability = None
                    half = window // 2
                    if len(a_win) >= half and half >= MIN_OBSERVATIONS:
                        sub_rs = []
                        step = max(1, (window - half) // 4)
                        for j in range(0, window - half + 1, step):
                            a_sub = a_win[j:j + half]
                            b_sub = b_win[j:j + half]
                            if a_sub.max() != a_sub.min() and b_sub.max() != b_sub.min():
                                rs, _ = spearmanr(a_sub, b_sub)
                                if not np.isnan(rs):
                                    sub_rs.append(rs)
                        if len(sub_rs) >= 3:
                            stability = round(float(np.std(sub_rs)), 4)

                    batch.append((
                        str(dates[i].date()),
                        name_a,
                        name_b,
                        window,
                        round(float(r), 6),
                        round(float(p), 8),
                        window,      # n_obs = nominal window length (preserved)
                        'spearman',
                        stability,
                        n_eff,       # effective n for CI computation
                    ))

                    # Regime-dependent correlations
                    if regime_dates:
                        win_dates = dates[i - window + 1:i + 1]
                        for regime in ('favorable', 'unfavorable'):
                            mask = np.array([regime_dates.get(d.date(), None) == regime
                                             for d in win_dates])
                            n_regime = mask.sum()
                            if n_regime < MIN_OBSERVATIONS:
                                continue
                            a_r = a_win[mask]
                            b_r = b_win[mask]
                            if a_r.max() == a_r.min() or b_r.max() == b_r.min():
                                continue
                            rr, pr = spearmanr(a_r, b_r)
                            if np.isnan(rr):
                                continue
                            n_eff_r = _compute_n_eff(a_r, b_r)
                            batch.append((
                                str(dates[i].date()),
                                name_a, name_b, window,
                                round(float(rr), 6),
                                round(float(pr), 8),
                                int(n_regime),
                                f'spearman_{regime}',
                                None,
                                n_eff_r,
                            ))

                # Flush every 10K rows to limit memory. Mid-run flushes
                # can straddle FDR buckets, so q_value/significant stay null
                # on those rows. In --latest-only mode the whole batch is
                # a single day, so the final flush carries the FDR pass.
                if len(batch) >= 10000:
                    if not dry_run:
                        _upsert_batch(batch, apply_fdr=False)
                    total_rows += len(batch)
                    batch = []

            if (pair_idx + 1) % 50 == 0:
                logger.info(f"  Progress: {pair_idx + 1}/{len(pairs)} pairs, "
                            f"{total_rows} rows so far")

        # Final flush — FDR correction is safe when the whole batch is one
        # day's tests (always true in --latest-only; usually true otherwise
        # because full backfill flushes incrementally).
        if batch:
            if not dry_run:
                _upsert_batch(batch, apply_fdr=latest_only)
            total_rows += len(batch)

        logger.info(f"Correlations complete: {total_rows} rows "
                    f"{'(dry run)' if dry_run else 'upserted'}, "
                    f"{skipped} pairs skipped (insufficient data)")
        log_finish(log_id, 'success', total_rows, dry_run)
        return total_rows

    except Exception as e:
        logger.error(f"Correlation engine failed: {e}", exc_info=True)
        log_finish(log_id, 'error', 0, dry_run, str(e))
        raise


def _fisher_z_ci(r_val, n):
    """Fisher z-transform 95% CI for a Spearman correlation.

    Approximation — exact for Pearson on normal data, biased toward wider
    intervals for Spearman (conservative). Cheap: no resampling.

    Returns (ci_low, ci_high) or (None, None) for degenerate cases.
    """
    if r_val is None or n is None or n <= 3:
        return (None, None)
    if abs(r_val) >= 1.0:
        return (r_val, r_val)
    z = 0.5 * math.log((1 + r_val) / (1 - r_val))
    se = 1.0 / math.sqrt(n - 3)
    z_lo, z_hi = z - 1.96 * se, z + 1.96 * se
    try:
        lo = round((math.exp(2 * z_lo) - 1) / (math.exp(2 * z_lo) + 1), 6)
        hi = round((math.exp(2 * z_hi) - 1) / (math.exp(2 * z_hi) + 1), 6)
        return (lo, hi)
    except OverflowError:
        return (-1.0, 1.0)


def _augment_batch(rows, apply_fdr):
    """Fill q_value/ci_low/ci_high/significant on each row.

    Row tuple going in (10 fields):
        (ts, metric_a, metric_b, window_days, correlation, p_value,
         n_obs, method, stability, n_eff)

    n_obs is the nominal window constant (preserved for API compatibility).
    n_eff is the effective sample size corrected for forward-fill ties and
    serial autocorrelation — used for Fisher-z CI computation so CIs are
    appropriately wider for sparse/autocorrelated series.

    Returns new list with 13 fields — q_value/significant left None when
    apply_fdr=False (backfill mode, batches can straddle FDR buckets).

    FDR (Benjamini-Hochberg) is per (ts, method, window) so only co-computed
    tests share a correction. CI always filled — per-row, no dependencies.
    """
    if not rows:
        return rows

    q_by_idx = {}
    if apply_fdr:
        buckets = {}
        for idx, r in enumerate(rows):
            ts, _, _, window, _, pval, _, method, _, _n_eff = r
            if pval is None:
                continue
            buckets.setdefault((ts, method, window), []).append((idx, float(pval)))
        for _key, items in buckets.items():
            items.sort(key=lambda x: x[1])
            m = len(items)
            running_min = 1.0
            for k in range(m - 1, -1, -1):
                idx_k, p_k = items[k]
                adj = p_k * m / (k + 1)
                if adj < running_min:
                    running_min = adj
                q_by_idx[idx_k] = min(1.0, running_min)

    out = []
    for idx, r in enumerate(rows):
        ts, a, b, window, corr, pval, n_obs, method, stability, n_eff = r
        r_val = float(corr) if corr is not None else None
        n = int(n_obs) if n_obs is not None else None
        # Use effective n for CI computation — wider/more honest for sparse series.
        # n_eff may be None for rows constructed outside compute_rolling_correlations
        # (e.g. backfill scripts); fall back to nominal n in that case.
        n_for_ci = int(n_eff) if n_eff is not None else n
        ci_low, ci_high = _fisher_z_ci(r_val, n_for_ci)

        q = q_by_idx.get(idx)
        significant = None
        if q is not None and n_for_ci is not None and r_val is not None:
            significant = bool(q < 0.05 and n_for_ci >= 30 and abs(r_val) >= 0.1)

        out.append((
            ts, a, b, window, corr, pval, n_obs, method, stability,
            round(q, 8) if q is not None else None,
            ci_low, ci_high, significant,
        ))
    return out


def _upsert_batch(rows, apply_fdr=False):
    """Batch upsert correlation rows using execute_values for performance.

    `apply_fdr`: True in --latest-only mode where the full batch is a single
    day's tests (FDR buckets are complete). False in backfill mode where
    mid-run flushes could straddle buckets — CI still written, q/sig null.
    """
    from psycopg2.extras import execute_values

    rows = _augment_batch(rows, apply_fdr=apply_fdr)

    sql = """
        INSERT INTO quant.correlations
            (ts, metric_a, metric_b, window_days, correlation,
             p_value, n_obs, method, stability,
             q_value, ci_low, ci_high, significant)
        VALUES %s
        ON CONFLICT (ts, metric_a, metric_b, window_days, method)
        DO UPDATE SET correlation = EXCLUDED.correlation,
                      p_value     = EXCLUDED.p_value,
                      n_obs       = EXCLUDED.n_obs,
                      stability   = EXCLUDED.stability,
                      q_value     = EXCLUDED.q_value,
                      ci_low      = EXCLUDED.ci_low,
                      ci_high     = EXCLUDED.ci_high,
                      significant = EXCLUDED.significant
    """
    with get_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, sql, rows, page_size=1000)


# =====================================================================
# CLI
# =====================================================================

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(
        description='Quant DB -- Rolling Correlation Engine')
    parser.add_argument('--dry-run', action='store_true',
                        help='Log actions without writing to DB')
    parser.add_argument('--latest-only', action='store_true',
                        help='Only compute for the most recent date')
    args = parser.parse_args()
    compute_rolling_correlations(dry_run=args.dry_run,
                                latest_only=args.latest_only)
