"""
Derivatives regime classifier.

Deterministic classifier that matches current derivatives metrics
to one of 12 canonical regimes from the playbook YAML.

Usage:
    from quant.engine.derivatives_regime import classify_derivatives_regime

    result = classify_derivatives_regime({
        'basis_apr': 3.4,
        'skew_25d': 9.6,
        'dvol': 53.6,
        'iv_rv_premium': 9.0,
        'put_call_ratio': 0.68,
        'term_slope': 0.067,
        'gex_net': 219000000,
        'gex_flip_price': 73500,
        'spot': 74600,
    })
    # result = {
    #   'regime': 'bear_grind',
    #   'regime_name': 'Bear Grind / Slow Bleed',
    #   'confidence': 0.82,
    #   'secondary': 'vol_compression',
    #   'secondary_name': 'Vol Compression / Coiling',
    #   'secondary_confidence': 0.45,
    #   'description': '...',
    #   'implications': [...],
    #   'transitions': [...],
    #   'historical_analog': '...',
    # }
"""

import logging
import os

import yaml

logger = logging.getLogger('quant.engine.derivatives_regime')

# Load regime definitions once at import time
_REGIMES = None
_CRYPTO_CONTEXT = None
_HEURISTICS = None


def _load_regimes():
    global _REGIMES, _CRYPTO_CONTEXT, _HEURISTICS
    if _REGIMES is not None:
        return

    # Resolved relative to the repository root, wherever it is checked out.
    paths = [
        os.path.join(os.path.dirname(__file__), '..', '..', 'morning_engine', 'config', 'derivatives_regimes.yaml'),
    ]

    for path in paths:
        path = os.path.abspath(path)
        if os.path.exists(path):
            with open(path, 'r') as f:
                data = yaml.safe_load(f)
            _REGIMES = data.get('regimes', {})
            _CRYPTO_CONTEXT = data.get('crypto_context', {})
            _HEURISTICS = data.get('heuristics', [])
            logger.info(f"Loaded {len(_REGIMES)} derivatives regimes from {path}")
            return

    logger.warning("derivatives_regimes.yaml not found, using empty regime set")
    _REGIMES = {}
    _CRYPTO_CONTEXT = {}
    _HEURISTICS = []


def classify_derivatives_regime(metrics: dict) -> dict:
    """Classify current market into a derivatives regime.

    Args:
        metrics: dict with keys:
            basis_apr       - Annualized futures basis (%)
            skew_25d        - 25-delta skew (put IV - call IV, %)
            dvol            - DVOL index value
            iv_rv_premium   - IV minus RV (%)
            put_call_ratio  - Put/Call OI ratio
            term_slope      - Term structure slope ((iv_90d - iv_7d) / iv_7d)
            gex_net         - Net GEX in USD
            gex_flip_price  - GEX flip point price
            spot            - Current spot price

    Returns:
        dict with regime classification, confidence, implications, transitions.
    """
    _load_regimes()

    if not _REGIMES:
        return {
            'regime': 'unknown',
            'regime_name': 'Unknown',
            'confidence': 0.0,
            'description': 'Regime playbook not loaded',
        }

    # Derive GEX-based features
    gex_net = metrics.get('gex_net')
    gex_flip = metrics.get('gex_flip_price')
    spot = metrics.get('spot')

    gex_sign = None
    if gex_net is not None:
        gex_sign = 'positive' if gex_net > 0 else 'negative'

    gex_flip_vs_spot = None
    if gex_flip and spot:
        gex_flip_vs_spot = 'above' if gex_flip > spot else 'below'

    # Score each regime
    scores = {}
    for regime_key, regime_def in _REGIMES.items():
        fp = regime_def.get('fingerprint', {})
        weight = regime_def.get('weight', 1.0)

        matched = 0
        total = 0

        for metric_key, condition in fp.items():
            # Handle GEX sign (string match)
            if metric_key == 'gex_sign':
                if gex_sign is not None:
                    total += 1
                    if gex_sign == condition:
                        matched += 1
                continue

            if metric_key == 'gex_flip_vs_spot':
                if gex_flip_vs_spot is not None:
                    total += 1
                    if gex_flip_vs_spot == condition:
                        matched += 1
                continue

            # Numeric range match
            value = metrics.get(metric_key)
            if value is None:
                continue  # Don't penalize missing metrics

            total += 1
            if isinstance(condition, dict):
                lo = condition.get('min', float('-inf'))
                hi = condition.get('max', float('inf'))
                if lo <= value <= hi:
                    matched += 1
            elif isinstance(condition, (int, float)):
                if abs(value - condition) < 0.01:
                    matched += 1

        if total > 0:
            raw_score = matched / total
            # Weight by regime importance and by number of conditions matched
            # (more conditions matched = higher confidence)
            scores[regime_key] = raw_score * weight * (0.7 + 0.3 * min(total / 6, 1.0))
        else:
            scores[regime_key] = 0.0

    if not scores:
        return {
            'regime': 'unknown',
            'regime_name': 'Unknown',
            'confidence': 0.0,
            'description': 'No metrics available for classification',
        }

    # Sort by score
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    primary_key, primary_score = ranked[0]
    primary_def = _REGIMES[primary_key]

    secondary_key, secondary_score = ranked[1] if len(ranked) > 1 else (None, 0)
    secondary_def = _REGIMES.get(secondary_key, {})

    # Build narrative seed with metric values substituted
    narrative = primary_def.get('narrative_seed', '')
    try:
        narrative = narrative.format(**{
            'basis_apr': _fmt(metrics.get('basis_apr'), '.1f'),
            'skew_25d': _fmt(metrics.get('skew_25d'), '+.1f'),
            'dvol': _fmt(metrics.get('dvol'), '.0f'),
            'iv_rv_premium': _fmt(metrics.get('iv_rv_premium'), '+.1f'),
            'put_call_ratio': _fmt(metrics.get('put_call_ratio'), '.2f'),
            'term_slope': _fmt(metrics.get('term_slope'), '+.3f'),
            'gex_flip_price': _fmt(metrics.get('gex_flip_price'), ',.0f'),
            'gex_net': _fmt(metrics.get('gex_net'), ',.0f'),
        })
    except (KeyError, ValueError):
        pass  # Use raw template if substitution fails

    result = {
        'regime': primary_key,
        'regime_name': primary_def.get('name', primary_key),
        'confidence': round(min(primary_score, 1.0), 2),
        'description': primary_def.get('description', ''),
        'narrative_seed': narrative.strip(),
        'implications': primary_def.get('implications', []),
        'transitions': primary_def.get('transitions', []),
        'historical_analog': primary_def.get('historical_analog', ''),
        'crypto_notes': primary_def.get('crypto_notes', ''),
    }

    if secondary_key and secondary_score > 0.3:
        result['secondary'] = secondary_key
        result['secondary_name'] = secondary_def.get('name', secondary_key)
        result['secondary_confidence'] = round(min(secondary_score, 1.0), 2)

    return result


# ── Shared single-row fetch + classify ────────────────────
#
# carrac/mcp/tools/quant.py quant_get_regime and carrac/plays/shares.py
# _chart_context_regime_asset both need "the latest quant.derivatives row for
# ONE asset, classified". Separate hand-rolled SQL + metric mappings drift
# (e.g. hardcoding a 'BTC' lookup for classification, so an ETH-only request
# never gets its own regime). Centralizing here means both call sites share
# the exact same column list and basis_pct/skew_25d/…
# → basis_apr/skew_25d/… mapping.

def fetch_latest_derivatives_row(asset: str):
    """Fetch the most recent quant.derivatives row for ONE asset.

    Returns the row (dict-like, psycopg2 RealDictCursor row) or None if no
    row exists at all for ``asset``. Note quant.collectors.deribit_options
    only writes the options/GEX/skew/dvol columns for BTC and ETH — other
    assets (SOL, XRP, …) may still have a row here from the funding-rate
    collectors (binance_daily / ccxt_funding) with those columns NULL, which
    is why callers should also check classify_asset_row's return (None
    means "no classification signal") rather than just "row exists".
    """
    from carrac.db_pool import get_quant_conn as get_conn

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT ts, basis_pct, skew_25d, dvol, iv_rv_premium,
                   options_put_call_ratio, options_oi_usd, options_max_pain,
                   options_iv_atm, term_structure_slope, iv_7d, iv_30d, iv_90d,
                   gex_net, gex_flip_price, funding_rate_avg
            FROM quant.derivatives WHERE asset = %s
            ORDER BY ts DESC LIMIT 1
        """, (asset,))
        return cur.fetchone()


def classify_asset_row(row) -> "dict | None":
    """Map a quant.derivatives row (or an already-rounded metrics dict built
    from one — both use the same raw column names as keys) to
    classify_derivatives_regime's expected keys and classify it.

    Returns None — NOT a bogus classification — when every relevant field is
    missing, e.g. an asset outside BTC/ETH options/GEX/skew coverage that
    still has a funding-only row. classify_derivatives_regime itself would
    otherwise score every regime 0.0 and (since ties are broken by dict
    insertion order, not "unknown") return an arbitrary regime name at
    confidence 0 — misleading. Callers should treat None as "no derivatives
    data for this asset".
    """
    args = {
        'basis_apr': row.get('basis_pct'),
        'skew_25d': row.get('skew_25d'),
        'dvol': row.get('dvol'),
        'iv_rv_premium': row.get('iv_rv_premium'),
        'put_call_ratio': row.get('options_put_call_ratio'),
        'term_slope': row.get('term_structure_slope'),
        'gex_net': row.get('gex_net'),
        'gex_flip_price': row.get('gex_flip_price'),
    }
    if all(v is None for v in args.values()):
        return None
    return classify_derivatives_regime(args)


def _fmt(value, fmt_str):
    """Format a value, returning 'N/A' if None."""
    if value is None:
        return 'N/A'
    return format(value, fmt_str)
