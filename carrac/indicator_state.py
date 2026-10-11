"""
Indicator State Service — unified access to per-asset indicator state.

Reads from the pre-computed response cache and provides structured access
for consumers: Screening Matrix, Asset Cockpit, Scanner.

Also handles state transition detection: compares current indicator
state to previous cycle, emits structured events for Stream and Alerts.

Part of the Indicator Coherence work.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import carrac.indicator_eval as _ie

logger = logging.getLogger('carrac.indicator_state')

# Transition event TTL in Redis (24 hours)
TRANSITION_TTL = 86400
TRANSITION_KEY = 'mcd:transitions'
FINGERPRINT_PREFIX = 'mcd:fp:'


def _safe(data, *keys, default=None):
    """Safely traverse nested dicts."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def format_matrix_row(asset: dict) -> dict:
    """
    Extract the 10 screening matrix columns from a pre-computed asset dict.

    Returns a flat dict suitable for table rendering + API response.
    """
    symbol = asset.get('symbol', '')
    price = asset.get('latest_price', 0)
    rt = asset.get('realtime') or {}
    if rt.get('price'):
        price = rt['price']
    change_pct = rt.get('change_pct')

    # 1. EMA Ribbon
    ema = asset.get('ema_ribbon') or {}
    ema_trend = ema.get('trend', 'neutral')
    ema_days = ema.get('days_since_flip')

    # 2. RSI (weekly)
    rsi_val = asset.get('weekly_rsi')

    # 3. BB Squeeze
    bb = asset.get('bb') or {}
    bb_squeeze = bb.get('squeeze', False)
    bb_width_pct = bb.get('width_pct')
    bb_squeeze_pct = bb.get('squeeze_pct')

    # 4. Consensio
    cons = asset.get('consensio') or {}
    cons_total = _safe(cons, 'total', 'signal', default=0)
    cons_positions = _safe(cons, 'total', 'positions', default=0)
    cons_max = _safe(cons, 'total', 'max_positions', default=6)

    # 5. Structure
    struct = asset.get('structure') or {}
    struct_trend = struct.get('structure_trend', 'unknown')
    last_high = struct.get('last_high_type', '')
    last_low = struct.get('last_low_type', '')

    # 6. MACD
    macd = asset.get('macd') or {}
    macd_hist = macd.get('histogram', 0)

    # 7. ADX
    adx = asset.get('adx') or {}
    adx_val = adx.get('adx')
    adx_regime = adx.get('regime', 'unknown')
    adx_direction = adx.get('trend_direction', 'neutral')

    # 8. CPR
    cpr = asset.get('cpr') or {}
    cpr_pivot = cpr.get('pivot')
    if cpr_pivot and price:
        if price > (cpr.get('tc') or cpr_pivot):
            cpr_pos = 'above'
        elif price < (cpr.get('bc') or cpr_pivot):
            cpr_pos = 'below'
        else:
            cpr_pos = 'inside'
    else:
        cpr_pos = None

    # 9. ATR %
    atr_pct = adx.get('atr_pct')

    # 10. Relative Volume
    vol = asset.get('volume_metrics') or {}
    rel_vol = vol.get('ratio')

    return {
        'symbol': symbol,
        'name': asset.get('name', symbol),
        'type': asset.get('type', ''),
        'category': asset.get('category', ''),
        'price': price,
        'change_pct': change_pct,
        # 10 indicator columns
        'ema_ribbon': {
            'trend': ema_trend,
            'days': ema_days,
        },
        'rsi': {
            'value': round(rsi_val, 1) if rsi_val is not None else None,
            'zone': (
                'oversold' if rsi_val and rsi_val < 30
                else 'overbought' if rsi_val and rsi_val > 70
                else 'neutral'
            ) if rsi_val is not None else None,
        },
        'bb_squeeze': {
            'squeezed': bb_squeeze,
            'width_pct': round(bb_width_pct, 1) if bb_width_pct is not None else None,
            # Squeeze % = band width vs its 60-day average — the metric the web
            # screener's BB column colours by (<30 extreme, <50 squeeze,
            # >150 expansion). Exposed so thin clients render the same column.
            'squeeze_pct': round(bb_squeeze_pct, 1) if bb_squeeze_pct is not None else None,
        },
        'consensio': {
            'signal': cons_total,
            'positions': cons_positions,
            'max_positions': cons_max,
        },
        'structure': {
            'trend': struct_trend,
            'high': last_high,
            'low': last_low,
            'label': f"{last_high}/{last_low}" if last_high and last_low else None,
        },
        'macd': {
            'direction': 'bullish' if macd_hist > 0 else ('bearish' if macd_hist < 0 else 'flat'),
            'histogram': round(macd_hist, 4) if macd_hist else 0,
        },
        'adx': {
            'value': adx_val,
            'regime': adx_regime,
            'direction': adx_direction,
        },
        'cpr': {
            'position': cpr_pos,
            'pivot': cpr_pivot,
            # Fine zone label from CPRCalculator (e.g. "TC-R1", "S1-BC", "Above R3").
            # Lets thin clients render the same CPR column as the web screener.
            'price_level': cpr.get('price_level'),
        },
        'atr_pct': {
            'value': atr_pct,
        },
        'rel_volume': {
            'ratio': rel_vol,
            'above_avg': vol.get('is_above_average', False),
        },
    }


def get_matrix_data(response_cache, mode='htf') -> list:
    """
    Build screening matrix from cached response.

    Returns list of matrix rows (10 indicator columns each), sorted by symbol.
    """
    cached = response_cache.get(mode)
    if not cached:
        return []

    rows = []
    for asset in cached.get('assets', []):
        # Skip metrics (not tradeable)
        if asset.get('type') in ('metric', 'crypto_metric'):
            continue
        rows.append(format_matrix_row(asset))

    return rows


# ================================================================
# State Transition Detection
# ================================================================

# Which states to track for transitions. Each entry:
# (indicator_name, extract_fn, human_label)
# extract_fn takes an asset dict, returns a hashable state value.

def _rsi_zone(val):
    """Discrete RSI zone for fingerprinting. Backend computes 3 zones only.

    Delegates to indicator_eval.rsi_zone — single source of truth.
    """
    return _ie.rsi_zone(val)


def _consensio_state(asset):
    """Return consensio named bucket.

    Delegates to indicator_eval.consensio_state — single source of truth.
    Missing/None positions or max_p==0 → None (not 'strong_bear').
    """
    return _ie.consensio_state(asset)


def _bb_state(asset):
    """3-state Bollinger Band squeeze: squeezed / normal / expanded.

    Delegates to indicator_eval.bb_squeeze_state — single source of truth.
    """
    return _ie.bb_squeeze_state(asset)


def _bb_position(asset):
    """Price position relative to Bollinger Bands: inside / above_upper / below_lower.

    Delegates to indicator_eval.bb_position_state — single source of truth.
    """
    return _ie.bb_position_state(asset)


def _cpr_level(asset, key='cpr'):
    """Return granular CPR zone (below_s3 .. above_r3) for given variant.

    Delegates to indicator_eval.cpr_zone — single source of truth.
    Uses the pre-computed price_level field from CPRCalculator. Returns None
    if the CPR data isn't populated for this asset.
    """
    return _ie.cpr_zone(asset, key)


def _cockpit(asset, key):
    """Get a sub-dict from cockpit_extended (None-safe)."""
    return ((asset.get('cockpit_extended') or {}).get(key)) or {}


def _ichimoku_cloud(asset):
    """Strip color suffix from ichimoku.cloud_position → above_cloud / inside / below_cloud.

    Delegates to indicator_eval.ichimoku_cloud_state — single source of truth.
    """
    return _ie.ichimoku_cloud_state(asset)


def _ttm_state(asset):
    """Combine ttm_squeeze.active + momentum into 4-state vocabulary.

    squeeze_on: currently in squeeze
    firing_up: just released, momentum bullish
    firing_down: just released, momentum bearish
    squeeze_off: not in squeeze, no fresh fire

    Delegates to indicator_eval.ttm_squeeze_state — single source of truth.
    """
    return _ie.ttm_squeeze_state(asset)


def _alligator_state(asset):
    """Pass through alligator.state: eating_bullish/eating_bearish/sleeping/mixed.

    Delegates to indicator_eval.alligator_state — single source of truth.
    """
    return _ie.alligator_state(asset)


def _ema_cross_state(asset):
    """Sustained EMA fast/slow state: bullish or bearish.

    Delegates to indicator_eval.ema_cross_state — single source of truth.
    """
    return _ie.ema_cross_state(asset)


def _donchian_pos(asset):
    """Position relative to Donchian channel: at_high / at_low / inside.

    Delegates to indicator_eval.donchian_position — single source of truth.
    """
    return _ie.donchian_position(asset)


def _stoch_rsi_zone(asset):
    """Stoch RSI zone from cockpit. Skipped on 3D/1W timeframes — returns None.

    Delegates to indicator_eval.stoch_rsi_zone — single source of truth.
    """
    return _ie.stoch_rsi_zone(asset)


def _adx_regime_state(asset):
    """ADX regime label, defaulting to 'unknown' when absent.

    Delegates to indicator_eval.adx_regime_state — single source of truth.
    """
    return _ie.adx_regime_state(asset)


# ---------------------------------------------------------------------------
# Tier 1 — sidebar LIVE signal vocabulary
#
# These read score_b.alerts (consensio/scoring/balanced.py's compound-signal
# alerts list) and trend_analysis.patterns (consensio/analysis/trend.py's
# TrendAnalyzer). Both fields are already carried on the precompute asset
# dict — no new upstream computation, just new fingerprint extractors.
#
# None-safety: unlike some Tier-2 extractors above, these never return
# Python None — an absent/None score_b or trend_analysis (or an alert simply
# not firing) resolves to the explicit string 'none', mirroring the
# ichimoku_tk / bb_squeeze convention of using 'none' as a real vocabulary
# state rather than null. This matters for detect_transitions: see the
# first-cycle-storm note there.
#
# Classification logic itself now lives in indicator_eval.py
# (single source of truth, shared with conditions.py) — these are thin
# delegators, same pattern as the Tier 2 wrappers above.
# ---------------------------------------------------------------------------

def _ema_gap_squeeze_state(asset):
    """EMA Ribbon convergence squeeze from score_b.alerts: 'squeeze' / 'none'.

    DISTINCT from the existing `bb_squeeze` tracked state (Bollinger Band
    width squeeze) — this is the EMA-gap squeeze flagged by
    consensio/scoring/balanced.py (ema_ribbon.is_squeeze), a different
    indicator entirely. Named ema_gap_squeeze so the two can't be confused.

    Delegates to indicator_eval.ema_gap_squeeze_state — single source of truth.
    """
    return _ie.ema_gap_squeeze_state(asset)


def _v_cpr_state(asset):
    """V-CPR (CPR vacuum) alert from score_b.alerts: 'vacuum' / 'none'.

    Delegates to indicator_eval.v_cpr_state — single source of truth.
    """
    return _ie.v_cpr_state(asset)


def _cpr_extreme_state(asset):
    """CPR extreme zone from score_b.alerts: 'near_s3' / 'near_r3' / 'none'.

    balanced.py appends at most one of near_s3/near_r3 per cycle (they are
    mutually exclusive price zones), so no further tie-break is needed.

    Delegates to indicator_eval.cpr_extreme_state — single source of truth.
    """
    return _ie.cpr_extreme_state(asset)


def _structure_disagreement_state(asset):
    """Ribbon-vs-Structure conflict from score_b.alerts: 'bearish'/'bullish'/'none'.

    'structure_bearish' = EMA Ribbon bullish but Structure bearish (STR-down warning).
    'structure_bullish' = EMA Ribbon bearish but Structure bullish (STR-up warning).
    Mutually exclusive by construction in balanced.py. DISTINCT from the
    existing `structure` tracked state (raw structure_trend passthrough) —
    this flags disagreement BETWEEN two indicators, not the structure itself.

    Delegates to indicator_eval.structure_disagreement_state — single source of truth.
    """
    return _ie.structure_disagreement_state(asset)


def _tf_divergence_state(asset):
    """1D-vs-1W consensio conflict from trend_analysis.patterns: 'active'/'none'.

    Delegates to indicator_eval.tf_divergence_state — single source of truth.
    """
    return _ie.tf_divergence_state(asset)


def _perp_basis_state(asset):
    """Perp futures basis (markPrice vs indexPrice) regime.

    States: 'steep_contango' (basis_pct >= +0.30) / 'backwardation'
    (basis_pct <= -0.10) / 'normal' (in between) / 'none' (no data).

    Reads asset['perp_basis_pct'], set in precompute.py alongside
    funding_rate (crypto-only, None when markPrice/indexPrice weren't both
    available). Missing data resolves to the explicit 'none' state — NOT
    'normal' — so a data gap never masquerades as a calm/no-signal market.
    This mirrors the ema_gap_squeeze/tf_divergence convention where 'none'
    is real vocabulary (no signal / no data), distinct from an in-range
    reading.

    Delegates to indicator_eval.perp_basis_state — single source of truth.
    """
    return _ie.perp_basis_state(asset)


def _rsi_divergence_pattern_state(asset):
    """RSI divergence flavor from trend_analysis.patterns.

    States: 'bull' / 'bear' / 'hidden_bull' / 'hidden_bear' / 'none'.

    trend_analysis.patterns is direction-exclusive by construction (Trend-
    Analyzer emits either its bullish_patterns or bearish_patterns list,
    whichever score wins — never both), so rsi_bull_div and rsi_bear_div
    cannot co-occur in practice. Tie-break, documented in case that
    assumption ever changes: regular divergence (reversal signal) outranks
    hidden divergence (continuation signal); within a tier, bull is checked
    before bear (arbitrary but deterministic).

    Delegates to indicator_eval.rsi_divergence_pattern_state — single source of truth.
    """
    return _ie.rsi_divergence_pattern_state(asset)


TRACKED_STATES = [
    # ema_ribbon: delegate to indicator_eval.ema_ribbon_state (normalises 'bull'/'bullish').
    ('ema_ribbon', _ie.ema_ribbon_state, 'EMA Ribbon'),
    ('rsi_zone', lambda a: _rsi_zone(a.get('weekly_rsi')), 'Weekly RSI'),
    ('bb_squeeze', _bb_state, 'BB Squeeze'),
    ('bb_position', _bb_position, 'BB Position'),
    ('consensio', _consensio_state, 'Consensio'),
    # structure: Alerts vocabulary (bullish/bearish/neutral) — delegates to indicator_eval.structure_trend.
    ('structure', _ie.structure_trend, 'Structure'),
    # macd: delegate to indicator_eval.macd_state.
    ('macd', _ie.macd_state, 'MACD'),
    # adx_regime: 'unknown' default preserved exactly — delegates to indicator_eval.adx_regime_state.
    ('adx_regime', _adx_regime_state, 'ADX'),
    ('cpr_position', lambda a: _cpr_level(a, 'cpr'), 'Monthly CPR'),
    ('weekly_cpr_position', lambda a: _cpr_level(a, 'weekly_cpr'), 'Weekly CPR'),
    ('daily_cpr_position', lambda a: _cpr_level(a, 'daily_cpr'), 'Daily CPR'),
    # super_trend: unknown direction → None (not 'neutral') — delegates to indicator_eval.super_trend_state.
    ('super_trend', _ie.super_trend_state, 'SuperTrend'),
    # psar: long→'bullish', short→'bearish', else None — delegates to indicator_eval.psar_state.
    ('psar', _ie.psar_state, 'PSAR'),
    # Tier 2 — extended indicators from cockpit_extended
    ('ichimoku', _ichimoku_cloud, 'Ichimoku Cloud'),
    # ichimoku_tk: 'bullish'/'bearish' on cross bar, 'none' between — delegates to indicator_eval.ichimoku_tk_state.
    ('ichimoku_tk', _ie.ichimoku_tk_state, 'Ichimoku TK Cross'),
    ('ttm_squeeze', _ttm_state, 'TTM Squeeze'),
    ('alligator', _alligator_state, 'Alligator'),
    ('ema_cross', _ema_cross_state, 'EMA Cross'),
    ('donchian', _donchian_pos, 'Donchian'),
    ('stoch_rsi', _stoch_rsi_zone, 'Stoch RSI'),
    # Tier 1 — sidebar LIVE signal vocabulary
    ('ema_gap_squeeze', _ema_gap_squeeze_state, 'EMA Gap Squeeze'),
    ('v_cpr', _v_cpr_state, 'V-CPR'),
    ('cpr_extreme', _cpr_extreme_state, 'CPR Extreme'),
    ('structure_disagreement', _structure_disagreement_state, 'Structure Disagreement'),
    ('tf_divergence', _tf_divergence_state, 'Timeframe Divergence'),
    ('rsi_divergence', _rsi_divergence_pattern_state, 'RSI Divergence'),
    # Perp futures basis (markPrice vs indexPrice) regime.
    ('perp_basis', _perp_basis_state, 'Perp Basis'),
]


def extract_fingerprint(asset: dict) -> dict:
    """Extract a compact state fingerprint from a precomputed asset dict.

    Returns dict of {indicator_key: state_value} for all tracked indicators.
    """
    fp = {}
    for key, extract_fn, _ in TRACKED_STATES:
        try:
            fp[key] = extract_fn(asset)
        except Exception:
            fp[key] = None
    return fp


# Significance weights: higher = more important transition
_SIGNIFICANCE = {
    'ema_ribbon': 3,
    'structure': 3,
    'consensio': 2,
    'rsi_zone': 2,
    'bb_squeeze': 2,
    'bb_position': 3,  # band-break events are major signals
    'macd': 1,
    'adx_regime': 1,
    'cpr_position': 1,
    'weekly_cpr_position': 2,
    'daily_cpr_position': 1,
    # Tier 2 — extended indicators
    'ichimoku': 3,        # cloud breaks are major structural events
    'ichimoku_tk': 2,
    'ttm_squeeze': 2,     # squeeze fires are noteworthy
    'alligator': 2,
    'ema_cross': 2,
    'donchian': 2,        # at_high / at_low are breakout signals
    'stoch_rsi': 1,
    # Tier 1 — sidebar LIVE signal vocabulary
    'ema_gap_squeeze': 2,
    'v_cpr': 2,
    'cpr_extreme': 2,
    'structure_disagreement': 3,  # ribbon-vs-structure conflict is a major warning
    'tf_divergence': 2,
    'rsi_divergence': 3,          # reversal/continuation signal, same tier as ema_ribbon/structure
    'perp_basis': 2,
}


def detect_transitions(current_fp: dict, previous_fp: dict, symbol: str) -> list:
    """Compare two fingerprints and return list of transition events.

    Each event: {indicator, from_state, to_state, label, significance, symbol, ts}
    """
    if not previous_fp:
        return []

    events = []
    now = datetime.now(tz=None).isoformat() + 'Z'  # server-local time

    for key, _, label in TRACKED_STATES:
        prev = previous_fp.get(key)
        curr = current_fp.get(key)

        # Skip if either is None or unchanged
        if prev is None or curr is None or prev == curr:
            continue

        events.append({
            'indicator': key,
            'label': label,
            'from': prev,
            'to': curr,
            'significance': _SIGNIFICANCE.get(key, 1),
            'symbol': symbol,
            'ts': now,
        })

    return events


def run_incremental_transition(symbol: str, asset_data: dict, mode: str = 'ltf'):
    """Detect transitions for a single asset (on-demand mode).

    Same fingerprint logic as run_transition_detection but for one asset.
    Called from _compute_single_asset_ltf() when LTF data is computed on-demand.
    """
    from carrac.caches import _get_redis

    if asset_data.get('type') in ('metric', 'crypto_metric'):
        return []

    r = _get_redis()
    if not r:
        return []

    current_fp = extract_fingerprint(asset_data)
    fp_key = f'{FINGERPRINT_PREFIX}{mode}:{symbol}'

    previous_fp = None
    try:
        raw = r.get(fp_key)
        if raw:
            previous_fp = json.loads(raw)
    except Exception:
        pass

    events = detect_transitions(current_fp, previous_fp, symbol)

    # Store new fingerprint
    try:
        r.set(fp_key, json.dumps(current_fp), ex=TRANSITION_TTL)
    except Exception:
        pass

    # Append events to Redis list
    if events and r:
        try:
            pipe = r.pipeline()
            for event in events:
                pipe.lpush(TRANSITION_KEY, json.dumps(event))
            pipe.ltrim(TRANSITION_KEY, 0, 999)
            pipe.expire(TRANSITION_KEY, TRANSITION_TTL)
            pipe.execute()
            logger.info(f"Incremental transition: {len(events)} events for {symbol} ({mode})")
        except Exception:
            pass

    return events


def run_transition_detection(assets: list, mode: str = 'htf'):
    """Run transition detection for all assets against stored fingerprints.

    Called after each precompute cycle. Stores new fingerprints and emits events.

    Args:
        assets: list of asset dicts from _compute_assets_response
        mode: 'htf' or 'ltf'

    Returns:
        list of all transition events detected this cycle
    """
    from carrac.caches import _get_redis

    r = _get_redis()
    all_events = []

    for asset in assets:
        symbol = asset.get('symbol', '')
        if not symbol:
            continue
        # Skip metrics
        if asset.get('type') in ('metric', 'crypto_metric'):
            continue

        current_fp = extract_fingerprint(asset)
        fp_key = f'{FINGERPRINT_PREFIX}{mode}:{symbol}'

        # Load previous fingerprint
        previous_fp = None
        if r:
            try:
                raw = r.get(fp_key)
                if raw:
                    previous_fp = json.loads(raw)
            except Exception:
                pass

        # Detect transitions
        events = detect_transitions(current_fp, previous_fp, symbol)
        if events:
            all_events.extend(events)

        # Store current fingerprint for next cycle
        if r:
            try:
                r.set(fp_key, json.dumps(current_fp), ex=TRANSITION_TTL)
            except Exception:
                pass

    # Store events in Redis list (prepend, trim to last 1000)
    if all_events and r:
        try:
            pipe = r.pipeline()
            for event in all_events:
                pipe.lpush(TRANSITION_KEY, json.dumps(event))
            pipe.ltrim(TRANSITION_KEY, 0, 999)
            pipe.expire(TRANSITION_KEY, TRANSITION_TTL)
            pipe.execute()
            logger.info(f"Detected {len(all_events)} indicator transitions ({mode})")
        except Exception as e:
            logger.warning(f"Failed to store transitions: {e}")

    return all_events


def get_recent_transitions(limit: int = 100, symbol: str = None,
                           min_significance: int = 0) -> list:
    """Read recent transition events from Redis.

    Args:
        limit: max events to return
        symbol: filter to specific asset (None = all)
        min_significance: minimum significance level (0-3)

    Returns:
        list of transition event dicts, newest first
    """
    from carrac.caches import _get_redis

    r = _get_redis()
    if not r:
        return []

    try:
        # Read more than limit to allow filtering
        fetch_count = limit * 3 if (symbol or min_significance) else limit
        raw_events = r.lrange(TRANSITION_KEY, 0, fetch_count - 1)
    except Exception:
        return []

    events = []
    for raw in raw_events:
        try:
            event = json.loads(raw)
        except Exception:
            continue

        if symbol and event.get('symbol') != symbol:
            continue
        if min_significance and event.get('significance', 0) < min_significance:
            continue

        events.append(event)
        if len(events) >= limit:
            break

    return events


# ================================================================
# Compound Indicator Alerts
# ================================================================

# Valid indicator alert conditions. Each entry must match what TRACKED_STATES
# can actually produce — anything else will silently never fire.
_CPR_ZONES = [
    'below_s3', 's2_s3', 's1_s2', 'bc_s1',
    'below_pivot', 'above_pivot',
    'tc_r1', 'r1_r2', 'r2_r3', 'above_r3',
]

INDICATOR_ALERT_CONDITIONS = {
    'ema_ribbon': ['bullish', 'bearish', 'neutral'],
    'rsi_zone': ['oversold', 'overbought', 'neutral'],
    'bb_squeeze': ['squeezed', 'normal', 'expanded'],
    'bb_position': ['inside', 'above_upper', 'below_lower'],
    'consensio': ['full_bull', 'strong_bull', 'bull', 'neutral', 'bear', 'strong_bear'],
    'structure': ['bullish', 'bearish', 'neutral'],
    'macd': ['bullish', 'bearish', 'flat'],
    'adx_regime': ['ranging', 'trend_forming', 'strong_trend'],
    'cpr_position': _CPR_ZONES,
    'weekly_cpr_position': _CPR_ZONES,
    'daily_cpr_position': _CPR_ZONES,
    'super_trend': ['bullish', 'bearish'],
    'psar': ['bullish', 'bearish'],
    # Tier 2 — extended indicators
    'ichimoku': ['above_cloud', 'inside_cloud', 'below_cloud'],
    'ichimoku_tk': ['bullish', 'bearish', 'none'],
    'ttm_squeeze': ['squeeze_on', 'squeeze_off', 'firing_up', 'firing_down'],
    'alligator': ['eating_bullish', 'eating_bearish', 'sleeping', 'mixed'],
    'ema_cross': ['bullish', 'bearish'],
    'donchian': ['at_high', 'at_low', 'inside'],
    'stoch_rsi': ['oversold', 'overbought', 'neutral'],
    # Tier 1 — sidebar LIVE signal vocabulary
    'ema_gap_squeeze': ['squeeze', 'none'],
    'v_cpr': ['vacuum', 'none'],
    'cpr_extreme': ['near_s3', 'near_r3', 'none'],
    'structure_disagreement': ['bearish', 'bullish', 'none'],
    'tf_divergence': ['active', 'none'],
    'rsi_divergence': ['bull', 'bear', 'hidden_bull', 'hidden_bear', 'none'],
    'perp_basis': ['steep_contango', 'backwardation', 'normal', 'none'],
}


def _compute_bb_position_custom(symbol: str, bb_params: dict) -> Optional[str]:
    """Recompute bb_position on-the-fly using custom BB params (non-default).

    Used by check_indicator_alerts when a bb_position alert stores custom
    period/std_dev/offset values that differ from the precompute defaults.

    Returns 'above_upper' / 'below_lower' / 'inside', or None on error.
    """
    try:
        from consensio.indicators.bollinger import BollingerBandCalculator
        from carrac.services import get_services

        period = bb_params.get('period', 20)
        std_dev = bb_params.get('std_dev', 2.0)
        offset = bb_params.get('offset', 0)

        svc = get_services()
        data = svc.db.get_ltf_multi_timeframe_data(symbol)
        # Use 1h bars (mapped as '1D' in the LTF data dict) — same TF the
        # alert creation UI reads from when the user is on the chart.
        tf_df = data.get('1h') or data.get('1D')
        if tf_df is None or tf_df.empty:
            return None

        calc = BollingerBandCalculator(period=period, std_dev=std_dev, offset=offset)
        bb = calc.calculate(tf_df)
        if not bb:
            return None

        import math
        upper_val = bb['upper']
        lower_val = bb['lower']
        if math.isnan(upper_val) or math.isnan(lower_val):
            return None

        price = float(tf_df['close'].iloc[-1])
        if price > upper_val:
            return 'above_upper'
        if price < lower_val:
            return 'below_lower'
        return 'inside'

    except Exception as e:
        logger.debug(f"bb_position custom recompute failed for {symbol}: {e}")
        return None


# Default BB params (must match BollingerBandCalculator defaults)
_BB_DEFAULT_PARAMS = {'period': 20, 'std_dev': 2.0, 'offset': 0}


def _bb_params_are_default(bb_params: dict) -> bool:
    """Return True if bb_params match the precompute defaults."""
    return (
        bb_params.get('period', 20) == 20
        and bb_params.get('std_dev', 2.0) == 2.0
        and bb_params.get('offset', 0) == 0
    )


def check_indicator_alerts(transitions: list):
    """Check transition events against active indicator alerts.

    Called from precompute loop after transition detection.
    Fires notifications for matching alerts.

    For bb_position alerts with custom params (period/std_dev/offset), the
    evaluator bypasses the transition list and recomputes on-the-fly so the
    user's snapshotted settings are respected.
    """
    from carrac.alerts import store as alert_store
    from push_notifications import send_push_notification, _send_user_notifications

    alerts = alert_store.list_alerts(active_only=True, alert_types=('indicator',))
    baseline = alert_store.snapshot(alerts)
    triggered = []

    for alert in alerts:
        if alert.get('triggered') and alert.get('frequency', 'once') == 'once':
            continue

        symbol = alert.get('symbol', '')
        indicator = alert.get('indicator', '')
        target_state = alert.get('target_state')

        # ── bb_position with custom params: direct evaluation path ──
        if indicator == 'bb_position':
            bb_params = alert.get('bb_params', {})
            if bb_params and not _bb_params_are_default(bb_params):
                # Recompute using snapshotted user params
                current_pos = _compute_bb_position_custom(symbol, bb_params)
                if current_pos is None or str(current_pos) != str(target_state):
                    continue

                # Match found — need previous state to confirm a *transition*
                # Use Redis fingerprint to avoid re-firing on every cycle.
                try:
                    from carrac.caches import _get_redis
                    r = _get_redis()
                    fp_key = f'mcd:bbpos_custom:{alert.get("id", "")}'
                    prev_pos = r.get(fp_key) if r else None
                    if prev_pos:
                        prev_pos = prev_pos.decode() if isinstance(prev_pos, bytes) else prev_pos
                    if prev_pos == current_pos:
                        # No transition — update stored value and skip
                        if r:
                            r.set(fp_key, current_pos, ex=TRANSITION_TTL)
                        continue
                    if r:
                        r.set(fp_key, current_pos, ex=TRANSITION_TTL)
                except Exception:
                    pass

                alert['triggered'] = True
                alert['triggered_at'] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'
                triggered.append(alert)

                period = bb_params.get('period', 20)
                std_dev = bb_params.get('std_dev', 2.0)
                offset = bb_params.get('offset', 0)
                label = f"BB Position (p={period}, σ={std_dev}, off={offset:+d})"
                title = f"{symbol} BB Position Alert"
                body = f"{symbol}: price is now {target_state} (BB period={period}, std={std_dev}, offset={offset:+d})"
                if alert.get('note'):
                    body += f"\n{alert['note']}"

                try:
                    send_push_notification(
                        user_id=alert.get('user_id', ''),
                        title=title,
                        body=body,
                        symbol=symbol,
                        tag=f"ind-alert-{alert.get('id', '')}"
                    )
                    _send_user_notifications(
                        alert.get('user_id', ''), symbol,
                        str(target_state), 0, 0, alert.get('note'),
                        alert_id=alert.get('id'), alert_type='indicator',
                        condition=body,
                    )
                except Exception as e:
                    logger.warning(f"bb_position custom alert notification failed: {e}")
                continue  # handled — skip transition matching below

        # Find matching transition
        for t in transitions:
            if t['symbol'] != symbol:
                continue
            if t['indicator'] != indicator:
                continue
            if str(t['to']) != str(target_state):
                continue

            # Match found — trigger the alert
            alert['triggered'] = True
            alert['triggered_at'] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'
            triggered.append(alert)

            # Build notification
            label = t.get('label', indicator)
            title = f"{symbol} {label} Alert"
            body = f"{symbol}: {label} changed from {t['from']} to {t['to']}"
            if alert.get('note'):
                body += f"\n{alert['note']}"

            # Send notifications
            try:
                send_push_notification(
                    user_id=alert.get('user_id', ''),
                    title=title,
                    body=body,
                    symbol=symbol,
                    tag=f"ind-alert-{alert.get('id', '')}"
                )
                _send_user_notifications(
                    alert.get('user_id', ''), symbol,
                    str(t['to']), 0, 0, alert.get('note'),
                    alert_id=alert.get('id'), alert_type='indicator',
                    condition=body,
                )
            except Exception as e:
                logger.warning(f"Indicator alert notification failed: {e}")

            break  # One match per alert per cycle

    if triggered:
        alert_store.save_changes(triggered, baseline)
        logger.info(f"Triggered {len(triggered)} indicator alerts")


def check_formula_alerts():
    """Check formula-based alerts.

    Evaluates each active formula alert's expression against current OHLCV data.
    Fires on false → true transition. Stores previous state in Redis.
    """
    from carrac.alerts import store as alert_store
    from push_notifications import send_push_notification, _send_user_notifications
    from carrac.formula_eval import evaluate_formula

    r = None
    try:
        from carrac.caches import _get_redis  # NOT carrac.redis_cache — _get_redis lives in caches; the wrong module silently left r=None, killing the false→true dedup so formula alerts re-fired every check cycle
        r = _get_redis()
    except Exception:
        pass
    if r is None:
        # r=None would silently disable the false→true dedup, and every
        # repeating formula alert would re-fire every ~60s cycle. Without the
        # state store the only non-spamming option is to sit the cycle out;
        # alerts resume the moment Redis answers again.
        _dedup_beacon('formula alerts skipped this cycle: dedup store (Redis) '
                      'unavailable — evaluating without it re-fires every '
                      'repeating alert every cycle')
        return

    alerts = alert_store.list_alerts(active_only=True, alert_types=('formula',))
    baseline = alert_store.snapshot(alerts)
    triggered = []
    dedup_failures = 0

    for alert in alerts:
        if alert.get('triggered') and alert.get('frequency', 'once') == 'once':
            continue

        symbol = alert.get('symbol', '')
        expression = alert.get('expression', '')
        timeframe = alert.get('timeframe', '1D')

        if not symbol or not expression:
            continue

        result = evaluate_formula(expression, symbol, timeframe)
        if result.get('error'):
            continue

        current_met = result['met']

        # Read + write the false→true edge state. One rule for any failure:
        # no dedup state, no fire — skip this alert until the store answers
        # (a fire the store cannot record re-fires on the next cycle).
        state_key = f'mcd:formula_alert:{alert.get("id")}:state'
        try:
            prev_raw = r.get(state_key)
            prev_met = prev_raw == b'1' if prev_raw is not None else False
            r.set(state_key, '1' if current_met else '0', ex=86400)
        except Exception:
            dedup_failures += 1
            continue

        # Fire on false → true transition
        if current_met and not prev_met:
            alert['triggered'] = True
            alert['triggered_at'] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'
            triggered.append(alert)

            expr_short = expression[:50] + ('...' if len(expression) > 50 else '')
            title = f"{symbol} Formula Alert"
            body = f"{symbol}: {result['evidence']}"
            if alert.get('note'):
                body += f"\n{alert['note']}"

            try:
                send_push_notification(
                    user_id=alert.get('user_id', ''),
                    title=title,
                    body=body,
                    symbol=symbol,
                    tag=f"formula-alert-{alert.get('id', '')}"
                )
                _send_user_notifications(
                    alert.get('user_id', ''), symbol,
                    expr_short, 0, 0, alert.get('note'),
                    alert_id=alert.get('id'), alert_type='formula',
                    condition=body,
                )
            except Exception as e:
                logger.warning(f"Formula alert notification failed: {e}")

    if dedup_failures:
        _dedup_beacon(f'formula alerts: dedup store failed for {dedup_failures} '
                      f'alert(s) this cycle — those alerts were skipped, not '
                      f'fired, and retry next cycle')

    if triggered:
        alert_store.save_changes(triggered, baseline)
        logger.info(f"Triggered {len(triggered)} formula alerts")


def _dedup_beacon(msg: str) -> None:
    """ERROR-log + Sentry one formula-dedup alarm. Seam for tests."""
    logger.error(msg)
    try:
        import sentry_sdk
        sentry_sdk.capture_message(msg, level='error')
    except Exception:
        pass


# ================================================================
# Position Monitoring
# ================================================================

def check_position_health(response_cache):
    """Check open journal positions against current indicator state.

    Alerts if key indicators have flipped against the trade direction.
    Called from precompute loop after transition detection.
    """
    try:
        from carrac.content_db import get_content_db  # noqa: F401 — availability guard only
    except ImportError:
        return

    cached = response_cache.get('htf')
    if not cached:
        return

    # Build symbol → asset state lookup
    state_by_symbol = {}
    for asset in cached.get('assets', []):
        state_by_symbol[asset.get('symbol', '')] = asset

    # Get all open trades across all users (RAII get_conn() avoids pinning a
    # pool slot per background-thread invocation).
    try:
        from carrac.db_pool import get_conn as _pool_get_conn, adapt_query
        with _pool_get_conn() as conn:
            rows = conn.execute(adapt_query(
                'SELECT * FROM trade_journal WHERE closed_at IS NULL'
            )).fetchall()
    except Exception as e:
        logger.debug(f"Position monitoring query failed: {e}")
        return

    from carrac.caches import _get_redis
    r = _get_redis()
    # Dedup key is per (user, symbol, direction), not per trade, so multiple
    # trades on the same asset don't multiply the alert. TTL is 7d.
    # Hysteresis: only re-fire when warnings are a strict superset of the last
    # fire (i.e. things got worse) — flapping between two indicator states
    # does not renotify.
    MONITOR_KEY = 'mcd:pos_monitor:v2:'
    DEDUP_TTL_SEC = 7 * 86400

    # Collapse multiple trades on the same (symbol, direction) so the user gets
    # one warning per asset+side, not one per trade.
    # key: (user_id, symbol, direction) -> {symbol, direction, warnings, price, trade_ids}
    collapsed = {}

    for row in rows:
        trade = dict(row)
        symbol = trade.get('asset', '')
        direction = trade.get('direction', 'long')
        user_id = str(trade.get('user_id', ''))
        trade_id = trade.get('id')

        asset_data = state_by_symbol.get(symbol)
        if not asset_data:
            continue

        fp = extract_fingerprint(asset_data)

        # Check for deterioration: indicators opposing the trade direction
        warnings = []

        ema = fp.get('ema_ribbon', 'neutral')
        if direction == 'long' and ema == 'bearish':
            warnings.append('EMA Ribbon turned bearish')
        elif direction == 'short' and ema == 'bullish':
            warnings.append('EMA Ribbon turned bullish')

        macd = fp.get('macd', 'flat')
        if direction == 'long' and macd == 'bearish':
            warnings.append('MACD turned bearish')
        elif direction == 'short' and macd == 'bullish':
            warnings.append('MACD turned bullish')

        structure = fp.get('structure', 'unknown')
        if direction == 'long' and structure == 'bearish':
            warnings.append('Structure shifted to downtrend')
        elif direction == 'short' and structure == 'bullish':
            warnings.append('Structure shifted to uptrend')

        if not warnings:
            continue

        current_price = (asset_data.get('realtime') or {}).get('price') or asset_data.get('latest_price') or 0
        ck = (user_id, symbol, direction)
        if ck not in collapsed:
            collapsed[ck] = {
                'symbol': symbol,
                'direction': direction,
                'warnings': set(warnings),
                'price': current_price,
                'trade_ids': [trade_id],
            }
        else:
            # Merge: union of warning sets, keep latest price, append trade_id
            collapsed[ck]['warnings'].update(warnings)
            collapsed[ck]['price'] = current_price
            collapsed[ck]['trade_ids'].append(trade_id)

    # Per-user opt-out (notification fatigue): users disable position-health
    # warnings in Settings -> Notifications. Default ON; fail-open on read error.
    from user_db import user_db as _udb
    disabled_users = set()
    for _uid in {u for (u, _, _) in collapsed}:
        try:
            _notif = (_udb.get_preferences(int(_uid)) or {}).get('notifications', {})
            if _notif.get('position_warnings', {}).get('enabled', True) is False:
                disabled_users.add(_uid)
        except Exception:
            pass  # fail-open: keep warnings on if the pref read fails

    # Hysteresis dedup: only re-fire if warning set is a STRICT SUPERSET of last fire.
    # user_id -> list of items
    user_warnings = {}
    for (user_id, symbol, direction), entry in collapsed.items():
        if user_id in disabled_users:
            continue  # user opted out — skip (don't advance hysteresis either)
        warnings_set = entry['warnings']
        warning_key = f"{MONITOR_KEY}{user_id}:{symbol}:{direction}"
        suppressed = False
        if r:
            try:
                prev_raw = r.get(warning_key)
                if prev_raw:
                    if isinstance(prev_raw, bytes):
                        prev_raw = prev_raw.decode('utf-8', errors='replace')
                    prev_set = set(json.loads(prev_raw)) if prev_raw else set()
                    # Suppress unless the new set strictly adds new warnings
                    if not (warnings_set - prev_set):
                        suppressed = True
                if not suppressed:
                    r.set(warning_key, json.dumps(sorted(warnings_set)), ex=DEDUP_TTL_SEC)
            except Exception:
                pass

        if suppressed:
            continue

        if user_id not in user_warnings:
            user_warnings[user_id] = []
        user_warnings[user_id].append({
            'symbol': symbol,
            'direction': direction,
            'warnings': sorted(warnings_set),
            'price': entry['price'],
            'trade_id': entry['trade_ids'][0],  # representative; UI may link to first trade
        })

    # Send one batched notification per user
    for user_id, items in user_warnings.items():
        try:
            from push_notifications import send_batched_position_warnings
            send_batched_position_warnings(user_id, items)
            symbols = [i['symbol'] for i in items]
            logger.info(f"Batched position warning for user {user_id}: {symbols}")
        except Exception as e:
            logger.debug(f"Batched position warning failed for user {user_id}: {e}")
