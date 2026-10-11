"""
Quant DB -- Regime Detection Engine

Classifies the market into "favorable" or "unfavorable" regimes using
a 2-state Hidden Markov Model on BTC returns + VIX + HY spread.

Why HMM?
    Markets alternate between low-volatility trending states and
    high-volatility choppy states. An HMM discovers these states
    from the data without needing predefined thresholds. Filtering
    all other signals by regime is "the highest-ROI feature" per the
    research reference (wiki page 248).

Components:
    1. GaussianHMM (2-state, full covariance) on standardized features
    2. PELT change-point detection as a secondary structural-break signal
    3. State history stored in quant.signals for API + morning bot

Schedule: Nightly at 06:55 UTC (after lead-lag at 06:50).

Usage:
    python -m quant.engine.regime
    python -m quant.engine.regime --dry-run
"""

import argparse
import json
import logging
import math
from datetime import date

import pandas as pd

from carrac.db_pool import get_quant_conn as get_conn
from quant.engine.run_log import log_finish, log_start
from quant.engine.config import REGIME_FEATURES, PELT_MIN_SIZE, PELT_MODEL
from quant.engine.correlations import fetch_all_series

logger = logging.getLogger('quant.engine.regime')

# If the latest input row is older than this many days, skip the refit entirely
# and log a warning rather than publishing a regime label based on stale data.
_STALE_INPUT_DAYS = 5


def compute_regime(dry_run=False, latest_only=False):
    """Train HMM, detect regime states, find changepoints, store results.

    Args:
        dry_run: Log actions without writing to DB.
        latest_only: Only store the latest day's signal (nightly mode).
                     HMM still trains on full history, but DB writes
                     drop from ~1500 rows to 1.

    Returns:
        Total rows upserted to quant.signals.
    """
    log_id = log_start('regime', dry_run)

    try:
        logger.info("Fetching feature series...")
        all_series = fetch_all_series()

        # Check all required features are available
        missing = [f for f in REGIME_FEATURES if f not in all_series]
        if missing:
            raise ValueError(f"Missing regime features: {missing}")

        # Align features on common dates
        features_list = [all_series[f] for f in REGIME_FEATURES]
        aligned = pd.concat(features_list, axis=1, join='inner').dropna()
        aligned.columns = REGIME_FEATURES
        logger.info(f"Aligned {len(aligned)} days "
                    f"({aligned.index[0].date()} to {aligned.index[-1].date()})")

        if len(aligned) < 200:
            raise ValueError(f"Only {len(aligned)} aligned observations, need 200+")

        # ── Stale-input guard ───────────────────────────────────────
        latest_date = aligned.index[-1].date()
        today_date = date.today()
        data_age_days = (today_date - latest_date).days
        if data_age_days > _STALE_INPUT_DAYS:
            logger.warning(
                f"Regime refit skipped: latest input row is {data_age_days}d old "
                f"(latest={latest_date}, today={today_date}, threshold={_STALE_INPUT_DAYS}d). "
                f"Publishing stale data would give a false label."
            )
            log_finish(log_id, 'skipped', 0, dry_run)
            return 0

        # ── Part 1: HMM ─────────────────────────────────────────────
        states, probs, model_info = _fit_hmm(aligned)

        # ── Part 2: PELT changepoints ───────────────────────────────
        changepoints = _detect_changepoints(aligned)

        # ── Compute duration + last transition ──────────────────────
        current_state = int(states[-1])
        current_prob = float(probs[-1, current_state])
        current_label = model_info['labels'][current_state]

        # Count consecutive days in current state (from the end)
        duration = 0
        for i in range(len(states) - 1, -1, -1):
            if states[i] == current_state:
                duration += 1
            else:
                break
        since_date = str(aligned.index[len(states) - duration].date())

        logger.info(f"Current regime: {current_label} "
                    f"(p={current_prob:.3f}, {duration}d since {since_date})")
        logger.info(f"PELT changepoints: {len(changepoints)}")

        # ── Store to quant.signals ──────────────────────────────────
        rows = []
        if latest_only:
            # Only store the latest day's signal (with full model metadata)
            i = len(states) - 1
            day = str(aligned.index[i].date())
            st = int(states[i])
            prob = float(probs[i, st])
            label = model_info['labels'][st]
            meta = {
                'label': label,
                'probability': round(prob, 4),
                'duration_days': duration,
                'since': since_date,
                'converged': model_info['converged'],
                'state_means': model_info['state_means'],
                'state_descriptions': model_info['state_descriptions'],
                'transition_matrix': model_info['transition_matrix'],
                'historical_pct': model_info['historical_pct'],
                'changepoints': changepoints,
            }
            rows.append((day, 'regime_hmm', 'REGIME', '_GLOBAL_',
                         st, json.dumps(meta)))
        else:
            for i in range(len(states)):
                day = str(aligned.index[i].date())
                st = int(states[i])
                prob = float(probs[i, st])
                label = model_info['labels'][st]

                meta = {
                    'label': label,
                    'probability': round(prob, 4),
                }

                # Add full model info only on the latest day
                if i == len(states) - 1:
                    meta.update({
                        'duration_days': duration,
                        'since': since_date,
                        'converged': model_info['converged'],
                        'state_means': model_info['state_means'],
                        'state_descriptions': model_info['state_descriptions'],
                        'transition_matrix': model_info['transition_matrix'],
                        'historical_pct': model_info['historical_pct'],
                        'changepoints': changepoints,
                    })

                rows.append((
                    day,
                    'regime_hmm',
                    'REGIME',
                    '_GLOBAL_',
                    st,
                    json.dumps(meta),
                ))

        if not dry_run:
            _upsert_signals(rows)

        total = len(rows)
        logger.info(f"Regime detection complete: {total} rows "
                    f"{'(dry run)' if dry_run else 'upserted'}")
        log_finish(log_id, 'success', total, dry_run)
        return total

    except Exception as e:
        logger.error(f"Regime engine failed: {e}", exc_info=True)
        log_finish(log_id, 'error', 0, dry_run, str(e))
        raise


# =====================================================================
# HMM
# =====================================================================

def _fit_hmm(aligned):
    """Train 2-state GaussianHMM on standardized features.

    Args:
        aligned: DataFrame with columns = REGIME_FEATURES, DatetimeIndex

    Returns:
        (states, probs, model_info) where:
        - states: np.array of int (0 or 1) per day
        - probs: np.array of shape (n_days, 2) posterior probabilities
        - model_info: dict with labels, means, transition matrix, etc.
    """
    from hmmlearn.hmm import GaussianHMM

    X_raw = aligned.values  # shape (n_days, n_features)

    # Standardize: zero mean, unit variance
    means = X_raw.mean(axis=0)
    stds = X_raw.std(axis=0)
    stds[stds == 0] = 1.0  # avoid division by zero
    X = (X_raw - means) / stds

    logger.info(f"Training HMM on {X.shape[0]} days x {X.shape[1]} features...")

    model = GaussianHMM(
        n_components=2,
        covariance_type='full',
        n_iter=100,
        random_state=42,
    )
    model.fit(X)

    # ── Convergence check ───────────────────────────────────────────
    # hmmlearn sets monitor_.converged after fit() if the EM algorithm
    # reached its tolerance criterion within n_iter iterations.
    converged = bool(getattr(model.monitor_, 'converged', False))
    if not converged:
        logger.warning(
            "HMM did not converge within 100 iterations. "
            "Results may be unreliable — consider increasing n_iter or "
            "inspecting the input feature distributions."
        )
    else:
        logger.info("HMM converged.")

    # Viterbi: most likely state sequence
    states = model.predict(X)

    # Posterior probabilities
    probs = model.predict_proba(X)

    # Unstandardize the learned means for interpretability
    # model.means_ is in standardized space
    real_means = model.means_ * stds + means

    # ── Canonical label ordering by BTC mean return ─────────────────
    # Enforce that state 0 always has the LOWER mean BTC return (unfavorable)
    # and state 1 has the HIGHER mean return (favorable).
    # This prevents label-switching across nightly refits: any downstream
    # consumer reading the integer `value` column from quant.signals gets
    # a stable semantics rather than an arbitrary 0/1 assignment.
    btc_idx = 0  # BTC is first feature
    if real_means[0, btc_idx] > real_means[1, btc_idx]:
        # Currently state 0 is "favorable" — swap so state 0 = unfavorable
        # Flip states array: 0→1, 1→0
        states = 1 - states
        # Flip rows in model output arrays for consistent reporting
        real_means = real_means[[1, 0], :]
        probs = probs[:, [1, 0]]
        logger.info("Applied canonical ordering: swapped states so 0=unfavorable, 1=favorable")

    # After canonical ordering: state 0 = unfavorable, state 1 = favorable.
    fav_state = 1

    labels = {0: 'unfavorable', 1: 'favorable'}

    # State descriptions with real-scale feature means
    feature_names = list(aligned.columns)
    state_means = {}
    state_descriptions = {}
    for st in [0, 1]:
        st_means = {feature_names[j]: round(float(real_means[st, j]), 6)
                    for j in range(len(feature_names))}
        state_means[labels[st]] = st_means

        # Human-readable description
        btc_ret = real_means[st, 0]
        btc_ann = btc_ret * 365 * 100  # annualized %
        vix_val = real_means[st, 1] if len(feature_names) > 1 else 0
        hy_val = real_means[st, 2] if len(feature_names) > 2 else 0
        state_descriptions[labels[st]] = (
            f"BTC {btc_ann:+.0f}%/yr, VIX ~{vix_val:.0f}, HY spread ~{hy_val:.1f}%"
        )

    # Transition matrix (round for readability)
    trans = [[round(float(model.transmat_[i, j]), 4)
              for j in range(2)] for i in range(2)]

    # Historical percentage in each state
    n_fav = int((states == fav_state).sum())
    n_total = len(states)
    hist_pct = {
        'favorable': round(100 * n_fav / n_total, 1),
        'unfavorable': round(100 * (n_total - n_fav) / n_total, 1),
    }

    logger.info(f"  State 0 ({labels[0]}): BTC mean={real_means[0, 0]:.5f}, "
                f"VIX={real_means[0, 1]:.1f}")
    logger.info(f"  State 1 ({labels[1]}): BTC mean={real_means[1, 0]:.5f}, "
                f"VIX={real_means[1, 1]:.1f}")
    logger.info(f"  Historical split: {hist_pct}")

    model_info = {
        'labels': labels,
        'converged': converged,
        'state_means': state_means,
        'state_descriptions': state_descriptions,
        'transition_matrix': trans,
        'historical_pct': hist_pct,
    }

    return states, probs, model_info


# =====================================================================
# Change-point detection
# =====================================================================

def _detect_changepoints(aligned):
    """Run PELT on standardized features to find structural breaks.

    Returns list of date strings where changepoints occurred.
    """
    import ruptures

    X_raw = aligned.values
    means = X_raw.mean(axis=0)
    stds = X_raw.std(axis=0)
    stds[stds == 0] = 1.0
    X = (X_raw - means) / stds

    n, dim = X.shape
    # BIC-like penalty: ln(n) * dim * 2
    penalty = math.log(n) * dim * 2

    algo = ruptures.Pelt(model=PELT_MODEL, min_size=PELT_MIN_SIZE)
    algo.fit(X)
    breakpoints = algo.predict(pen=penalty)

    # breakpoints includes the last index (n), remove it
    breakpoints = [bp for bp in breakpoints if bp < n]

    # Convert indices to dates
    dates = [str(aligned.index[bp].date()) for bp in breakpoints]
    return dates


# =====================================================================
# Storage
# =====================================================================

def _upsert_signals(rows):
    """Batch upsert regime rows into quant.signals."""
    from psycopg2.extras import execute_values

    sql = """
        INSERT INTO quant.signals
            (ts, source, metric, asset, value, metadata)
        VALUES %s
        ON CONFLICT (ts, source, metric, asset)
        DO UPDATE SET value    = EXCLUDED.value,
                      metadata = EXCLUDED.metadata
    """
    with get_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, sql, rows, page_size=500)


# =====================================================================
# CLI
# =====================================================================

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(
        description='Quant DB -- Regime Detection Engine')
    parser.add_argument('--dry-run', action='store_true',
                        help='Log actions without writing to DB')
    parser.add_argument('--latest-only', action='store_true',
                        help='Only store latest day result (nightly mode)')
    args = parser.parse_args()
    compute_regime(dry_run=args.dry_run, latest_only=args.latest_only)
