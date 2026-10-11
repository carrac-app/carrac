"""
Quant DB -- Composite Factor Construction

Builds PCA-based composite factors from groups of correlated metrics.
Each factor is the first principal component of its component group,
capturing the dominant shared variance in a single time series.

Factors:
    RISK_APPETITE  -- PCA(VIX, HY_SPREAD, TLT, DXY)
    LIQUIDITY      -- PCA(M2, NET_LIQUIDITY, FED_BALANCE)
    CREDIT_STRESS  -- PCA(HY_SPREAD, HY_SPREAD_EU, YIELD_10Y2Y)

Factors are stored in quant.sentiment and registered as canonical
metrics, so they flow through correlations and lead-lag automatically.

Uses numpy eigendecomposition (no sklearn dependency).

Schedule: Nightly at 06:35 UTC (after normalize, before correlations).

Usage:
    python -m quant.engine.factors
    python -m quant.engine.factors --dry-run
    python -m quant.engine.factors --latest-only
"""

import argparse
import logging

import numpy as np
import pandas as pd

from carrac.db_pool import get_quant_conn as get_conn
from quant.engine.run_log import log_finish, log_start
from quant.engine.config import COMPOSITE_FACTORS
from quant.engine.correlations import fetch_all_series

logger = logging.getLogger('quant.engine.factors')

MIN_COMPONENTS = 2   # Need at least 2 valid components for PCA
MIN_HISTORY = 60     # Minimum aligned observations for a stable factor


def _compute_pca_factor(series_dict, components, flip_ref):
    """Compute PC1 of aligned, standardized component series.

    Args:
        series_dict: metric_name -> pd.Series (from fetch_all_series)
        components: list of metric names for this factor
        flip_ref: metric name whose loading should be positive
                  (determines sign convention)

    Returns:
        (pd.Series of PC1 scores, explained_variance_ratio) or (None, 0)
        if insufficient data.
    """
    available = [m for m in components if m in series_dict]
    if len(available) < MIN_COMPONENTS:
        return None, 0.0

    # Align on common dates (inner join)
    frames = {m: series_dict[m] for m in available}
    aligned = pd.DataFrame(frames).dropna()
    if len(aligned) < MIN_HISTORY:
        return None, 0.0

    X = aligned.values
    mu = X.mean(axis=0)
    sigma = X.std(axis=0)
    # Guard against constant columns
    sigma[sigma == 0] = 1.0
    Z = (X - mu) / sigma

    # Covariance + eigendecomposition
    cov = np.cov(Z, rowvar=False)
    eigenvalues, eigenvectors = np.linalg.eigh(cov)

    # eigh returns ascending order — PC1 is the last
    pc1_idx = -1
    pc1 = eigenvectors[:, pc1_idx]
    explained = eigenvalues[pc1_idx] / eigenvalues.sum()

    # Sign convention: ensure flip_ref loading is positive
    if flip_ref in available:
        ref_col = available.index(flip_ref)
        if pc1[ref_col] < 0:
            pc1 = -pc1

    scores = Z @ pc1
    return pd.Series(scores, index=aligned.index, name='factor'), explained


def compute_factors(dry_run=False, latest_only=False):
    """Compute composite PCA factors and store in quant.sentiment.

    Args:
        dry_run: Log without writing to DB.
        latest_only: Accepted for CLI consistency.

    Returns:
        Total rows upserted across all factors.
    """
    log_id = log_start('factors', dry_run)

    try:
        logger.info("Fetching canonical metric series for factor construction...")
        all_series = fetch_all_series()
        logger.info(f"Loaded {len(all_series)} metrics")

        total_rows = 0

        for factor_name, fconfig in COMPOSITE_FACTORS.items():
            components = fconfig['components']
            flip_ref = fconfig['flip_sign_ref']
            metric_name = f'FACTOR_{factor_name}'

            factor_series, explained = _compute_pca_factor(
                all_series, components, flip_ref
            )

            if factor_series is None:
                logger.warning(f"Factor {factor_name}: insufficient data "
                               f"(components: {components})")
                continue

            logger.info(f"Factor {factor_name}: {len(factor_series)} obs, "
                        f"explained variance = {explained:.1%} "
                        f"(components: {components})")

            if explained < 0.3:
                logger.warning(f"Factor {factor_name}: low explained variance "
                               f"({explained:.1%}) — components may not share "
                               f"a dominant factor")

            if dry_run:
                total_rows += len(factor_series)
                continue

            # Upsert to quant.sentiment
            rows = [
                (ts.date() if hasattr(ts, 'date') else ts,
                 metric_name, '_GLOBAL_', float(val), 'pca_factor')
                for ts, val in factor_series.items()
                if not np.isnan(val)
            ]

            if rows:
                _upsert_factor_rows(rows)
                total_rows += len(rows)
                logger.info(f"Factor {factor_name}: {len(rows)} rows upserted")

        logger.info(f"Factors complete: {total_rows} rows "
                    f"{'(dry run)' if dry_run else 'upserted'}")
        log_finish(log_id, 'success', total_rows, dry_run)
        return total_rows

    except Exception as e:
        logger.error(f"Factor engine failed: {e}", exc_info=True)
        log_finish(log_id, 'error', 0, dry_run, str(e))
        raise


def _upsert_factor_rows(rows):
    """Batch upsert factor scores to quant.sentiment."""
    from psycopg2.extras import execute_values

    sql = """
        INSERT INTO quant.sentiment (ts, metric, asset, value, source)
        VALUES %s
        ON CONFLICT (ts, metric, asset)
        DO UPDATE SET value  = EXCLUDED.value,
                      source = EXCLUDED.source
    """
    with get_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, sql, rows, page_size=500)


# ── CLI ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(
        description='Quant DB -- Composite Factor Construction')
    parser.add_argument('--dry-run', action='store_true',
                        help='Log actions without writing to DB')
    parser.add_argument('--latest-only', action='store_true',
                        help='Accepted for CLI consistency')
    args = parser.parse_args()
    compute_factors(dry_run=args.dry_run, latest_only=args.latest_only)
