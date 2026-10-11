"""
Condition evaluation engine — shared by Plays (and formerly Scenarios).

Evaluates structured conditions against market data from ResponseCache,
chart-internal API, and scanner state. Each condition type has an evaluator
that returns {met: bool, evidence: str}.

Data sources:
- ResponseCache.get('htf') → asset dict with indicators, structure, CPR, etc.
- Scanner scorer output → verdict, criteria scores
- chart-internal API → OHLCV, RSI timeseries, MACD timeseries
"""

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import carrac.indicator_eval as _ie

logger = logging.getLogger('carrac.conditions')

# Dedup set for Sentry beacons — keyed by (rid, rtype, error_type) so we only fire
# once per unique failure kind per process lifetime, not on every eval tick.
_EVAL_ERROR_BEACONED: set = set()

# Map the legacy price_level "condition" vocabulary to the current "relation" field.
_PRICE_LEVEL_CONDITION_MAP = {
    'close_below': 'below',
    'close_above': 'above',
    'below': 'below',
    'above': 'above',
}


def _effective_params(rule: dict) -> dict:
    """Return a rule's eval params, upgrading pre-migration flat-schema rules.

    Old price_level rules (and their play ``invalidation`` twins) stored the level
    and direction as top-level ``price``/``condition`` with NO ``params`` key, so the
    current evaluator — which reads ``params['level']``/``params['relation']`` — raised
    KeyError and the rule silently failed open (the trip-wire never fired). Map the
    flat fields into params so the rule evaluates as intended. New-schema rules
    (``params`` already carries ``level``) pass through untouched.
    """
    params = rule.get('params') or {}
    if rule.get('type') == 'price_level' and 'level' not in params:
        raw_price = rule.get('price', params.get('price'))
        raw_cond = rule.get('condition', params.get('condition', ''))
        if raw_price is not None:
            params = {
                **params,
                'level': raw_price,
                'relation': _PRICE_LEVEL_CONDITION_MAP.get(raw_cond, raw_cond),
            }
    return params


# ── Condition Type Registry ──

CONDITION_CATEGORIES = {
    'price': ['price_level', 'price_zone', 'price_change_pct', 'distance_from'],
    'candle': ['candle_close', 'engulfing_detected', 'bb_breakout_retest_engulfing', 'bb_fade_engulfing', 'bb_squeeze_breakout', 'bb_breakdown_continuation'],
    'indicator': [
        'indicator_value', 'indicator_crosses', 'indicator_state',
        'indicator_divergence', 'consensio_threshold', 'scanner_verdict',
    ],
    # 'drawing': ['trendline_break', 'level_touch'] and 'correlation' (from the
    # cross_asset group below) were removed: permanent stub
    # evaluators that always returned met=False ("not yet evaluated"), so any
    # play built on them could never fire.
    'cross_asset': ['cross_asset', 'scanner_comparison'],
    'formula': ['formula_expression'],
    'derivatives': ['derivatives_metric', 'derivatives_regime'],
    'meta': ['time_limit', 'manual', 'cross_play_reference'],
}

ALL_CONDITION_TYPES = set()
for types in CONDITION_CATEGORIES.values():
    ALL_CONDITION_TYPES.update(types)

# Param schemas: {type: {required: [fields], optional: [fields]}}
CONDITION_SCHEMAS = {
    'price_level': {
        'required': ['level', 'relation'],
        'relations': ('above', 'below', 'between'),
    },
    'price_zone': {
        'required': ['low', 'high', 'relation'],
        'relations': ('inside', 'outside', 'breaks_above', 'breaks_below'),
    },
    'price_change_pct': {
        'required': ['period', 'direction', 'threshold_pct'],
        'periods': ('24h', '7d', '30d'),
        'directions': ('up', 'down'),
    },
    'distance_from': {
        'required': ['reference', 'threshold_pct'],
        'references': ('entry', 'invalidation', 'target', 'level'),
    },
    'candle_close': {
        'required': ['timeframe', 'level', 'relation'],
        'relations': ('above', 'below'),
    },
    'engulfing_detected': {
        'required': ['direction', 'timeframe'],
        'directions': ('bullish', 'bearish'),
        'timeframes': ('1h', '4h', 'D', '3D', 'W'),
    },
    'bb_breakout_retest_engulfing': {
        'required': ['direction', 'timeframe', 'lookback_bars', 'proximity_pct'],
        'optional': ['require_volume'],
        'directions': ('bullish', 'bearish'),
        'timeframes': ('1h', '4h', 'D', '3D', 'W'),
    },
    'bb_fade_engulfing': {
        'required': ['direction', 'timeframe'],
        'directions': ('bullish', 'bearish'),
        'timeframes': ('1h', '4h', 'D', '3D', 'W'),
    },
    'bb_squeeze_breakout': {
        'required': ['direction', 'timeframe', 'lookback_bars', 'min_squeeze_bars'],
        'optional': ['require_volume'],
        'directions': ('bullish', 'bearish'),
        'timeframes': ('1h', '4h', 'D', '3D', 'W'),
    },
    'bb_breakdown_continuation': {
        # Setup F: breakout within lookback → engulfing OR broken-band retest.
        # The only Play rule besides Setup D that accepts custom BB params (offset).
        'required': ['direction', 'timeframe', 'lookback_bars'],
        'optional': ['confirm_mode', 'retest_band', 'proximity_pct', 'bb_params'],
        'directions': ('bullish', 'bearish'),
        'timeframes': ('1h', '4h', 'D', '3D', 'W'),
    },
    'indicator_value': {
        'required': ['indicator', 'timeframe', 'operator', 'value'],
        'operators': ('gt', 'lt', 'eq', 'gte', 'lte'),
    },
    'indicator_crosses': {
        'required': ['indicator', 'timeframe', 'direction', 'value'],
        'directions': ('above', 'below'),
    },
    'indicator_state': {
        'required': ['indicator', 'timeframe', 'state'],
    },
    'indicator_divergence': {
        'required': ['indicator', 'timeframe', 'type'],
        'types': ('bullish', 'bearish'),
    },
    'consensio_threshold': {
        'required': ['min_value'],
        'optional': ['max_value'],
    },
    'scanner_verdict': {
        'required': ['direction', 'min_verdict'],
        'directions': ('long', 'short'),
        'verdicts': ('AVOID', 'WEAK', 'NEUTRAL', 'GOOD', 'STRONG'),
    },
    'cross_asset': {
        'required': ['asset', 'condition_type', 'params'],
    },
    'time_limit': {
        'optional': ['expires_at', 'expires_in_days'],
    },
    'manual': {
        'required': ['label'],
        'optional': ['description'],
    },
    'formula_expression': {
        'required': ['expression'],
        'optional': ['timeframe'],
    },
    'derivatives_metric': {
        'required': ['metric', 'operator', 'value'],
        'metrics': ('basis_pct', 'skew_25d', 'dvol', 'iv_rv_premium',
                    'options_put_call_ratio', 'gex_net', 'term_structure_slope',
                    'funding_rate_avg', 'iv_7d', 'iv_30d', 'iv_90d'),
        'operators': ('gt', 'lt', 'gte', 'lte'),
    },
    'derivatives_regime': {
        'required': ['regime', 'condition'],
        'regimes': ('euphoric_meltup', 'complacent_bull', 'healthy_trend',
                    'pre_event_tension', 'distribution_topping', 'sudden_correction',
                    'full_capitulation', 'post_crash_stabilization',
                    'vol_compression', 'bear_grind', 'short_squeeze_setup',
                    'structural_carry'),
        'conditions': ('matches', 'not_matches'),
    },
    'scanner_comparison': {
        # Compare two assets/baskets via scanner totals.
        # e.g. "BTC long total > ETH long total" or "BASKET A short > BASKET B short"
        'required': ['asset_a', 'asset_b', 'side', 'relation'],
        'sides': ('long', 'short'),
        'relations': ('gt', 'lt', 'gte', 'lte'),
        'optional': ['margin_pct'],
    },
    'cross_play_reference': {
        # Trigger off the status of another play owned by the user.
        'required': ['play_id', 'target_status'],
        'target_statuses': ('watching', 'acting', 'completed', 'invalidated', 'expired'),
    },
}

# Verdict ordering for scanner_verdict comparisons
VERDICT_ORDER = {'AVOID': 0, 'WEAK': 1, 'NEUTRAL': 2, 'GOOD': 3, 'STRONG': 4}


def validate_condition(rule: dict) -> Tuple[bool, Optional[str]]:
    """Validate a single condition rule. Returns (is_valid, error_message)."""
    ctype = rule.get('type')
    if not ctype or ctype not in ALL_CONDITION_TYPES:
        return False, f"Unknown condition type: {ctype}"

    params = rule.get('params', {})
    schema = CONDITION_SCHEMAS.get(ctype, {})

    # Check required params
    for field in schema.get('required', []):
        if field not in params or params[field] is None:
            return False, f"{ctype}: missing required param '{field}'"

    # Check enum constraints
    for key in ('relations', 'directions', 'operators', 'types', 'periods', 'verdicts',
                'references', 'sides', 'target_statuses', 'metrics', 'regimes',
                'conditions', 'timeframes'):
        if key in schema:
            # Map key back to param name
            param_name = {
                'relations': 'relation', 'directions': 'direction',
                'operators': 'operator', 'types': 'type',
                'periods': 'period', 'verdicts': 'min_verdict',
                'references': 'reference', 'sides': 'side',
                'target_statuses': 'target_status',
                'metrics': 'metric', 'regimes': 'regime',
                'conditions': 'condition',
                'timeframes': 'timeframe',
            }.get(key, key)
            val = params.get(param_name)
            if val is not None and val not in schema[key]:
                return False, f"{ctype}: invalid {param_name} '{val}', expected one of {schema[key]}"

    # The allowlist above accepts ten Deribit-barred metrics
    # (dvol, gex_net, skew_25d, basis_pct, iv_*, term_structure_slope,
    # options_put_call_ratio). Nothing filtered it, so any user could build a
    # play or alert on licensed data and be shown the raw number back as
    # evidence — the formula-engine leak that was closed earlier, reappearing
    # through a different door. A deliberate choice: gate to admin,
    # matching deribit_gate's stated posture (collect freely, expose to admin).
    if ctype == 'derivatives_metric':
        from carrac.compliance.deribit_gate import condition_metric_visible
        metric = params.get('metric', '')
        if not condition_metric_visible(metric):
            return False, (
                f"derivatives_metric: '{metric}' is not available. Permitted "
                f"derivatives conditions are funding_rate_avg and the other "
                f"Binance-sourced metrics.")

    # Per-type numeric range validation
    if ctype == 'bb_squeeze_breakout':
        lookback = params.get('lookback_bars')
        min_squeeze = params.get('min_squeeze_bars')
        if lookback is not None:
            try:
                lb = int(lookback)
                if not (5 <= lb <= 30):
                    return False, f"{ctype}: lookback_bars must be between 5 and 30"
            except (TypeError, ValueError):
                return False, f"{ctype}: lookback_bars must be an integer"
        if min_squeeze is not None:
            try:
                ms = int(min_squeeze)
                if not (1 <= ms <= 20):
                    return False, f"{ctype}: min_squeeze_bars must be between 1 and 20"
            except (TypeError, ValueError):
                return False, f"{ctype}: min_squeeze_bars must be an integer"

    if ctype == 'bb_breakout_retest_engulfing':
        lookback = params.get('lookback_bars')
        proximity = params.get('proximity_pct')
        if lookback is not None:
            try:
                lb = int(lookback)
                if not (5 <= lb <= 50):
                    return False, f"{ctype}: lookback_bars must be between 5 and 50"
            except (TypeError, ValueError):
                return False, f"{ctype}: lookback_bars must be an integer"
        if proximity is not None:
            try:
                pct = float(proximity)
                if not (0.1 <= pct <= 2.0):
                    return False, f"{ctype}: proximity_pct must be between 0.1 and 2.0"
            except (TypeError, ValueError):
                return False, f"{ctype}: proximity_pct must be a float"

    if ctype == 'bb_breakdown_continuation':
        lookback = params.get('lookback_bars')
        proximity = params.get('proximity_pct')
        confirm_mode = params.get('confirm_mode')
        retest_band = params.get('retest_band')
        bb_p = params.get('bb_params')
        if lookback is not None:
            try:
                lb = int(lookback)
                if not (5 <= lb <= 50):
                    return False, f"{ctype}: lookback_bars must be between 5 and 50"
            except (TypeError, ValueError):
                return False, f"{ctype}: lookback_bars must be an integer"
        if proximity is not None:
            try:
                pct = float(proximity)
                if not (0.1 <= pct <= 2.0):
                    return False, f"{ctype}: proximity_pct must be between 0.1 and 2.0"
            except (TypeError, ValueError):
                return False, f"{ctype}: proximity_pct must be a float"
        if confirm_mode is not None and confirm_mode not in ('engulfing', 'band_retest', 'either'):
            return False, f"{ctype}: confirm_mode must be engulfing, band_retest, or either"
        if retest_band is not None and retest_band not in ('broken', 'middle'):
            return False, f"{ctype}: retest_band must be broken or middle"
        if bb_p is not None:
            if not isinstance(bb_p, dict):
                return False, f"{ctype}: bb_params must be an object"
            try:
                _p, _s, _o = bb_p.get('period'), bb_p.get('std_dev'), bb_p.get('offset')
                if _p is not None and not (5 <= int(_p) <= 200):
                    return False, f"{ctype}: bb_params.period must be between 5 and 200"
                if _s is not None and not (0.5 <= float(_s) <= 5.0):
                    return False, f"{ctype}: bb_params.std_dev must be between 0.5 and 5.0"
                if _o is not None and not (-20 <= int(_o) <= 20):
                    return False, f"{ctype}: bb_params.offset must be between -20 and 20"
            except (TypeError, ValueError):
                return False, f"{ctype}: bb_params values must be numeric"

    # Rule must have an id
    if not rule.get('id'):
        return False, "Condition rule must have an 'id'"

    return True, None


def validate_conditions(conditions: dict) -> Tuple[bool, Optional[str]]:
    """Validate the full conditions object. Returns (is_valid, error_message)."""
    if not isinstance(conditions, dict):
        return False, "Conditions must be a dict"

    rules = conditions.get('rules', [])
    if not isinstance(rules, list):
        return False, "Conditions.rules must be a list"

    logic = conditions.get('logic', 'all')
    if logic not in ('all', 'any', 'custom'):
        return False, f"Invalid logic: {logic}"

    if logic == 'custom' and not conditions.get('expression'):
        return False, "Custom logic requires an 'expression'"

    # Validate each rule
    rule_ids = set()
    for rule in rules:
        valid, error = validate_condition(rule)
        if not valid:
            return False, error
        rid = rule.get('id')
        if rid in rule_ids:
            return False, f"Duplicate rule id: {rid}"
        rule_ids.add(rid)

    # Validate invalidation_rules if present
    for rule in conditions.get('invalidation_rules', []):
        valid, error = validate_condition(rule)
        if not valid:
            return False, f"invalidation_rules: {error}"

    # Validate custom expression references
    if logic == 'custom':
        expr = conditions.get('expression', '')
        referenced = set(re.findall(r'\b(r\w+)\b', expr))
        unknown = referenced - rule_ids
        if unknown:
            return False, f"Expression references unknown rule IDs: {unknown}"

    return True, None


# ── Indicator State Vocabulary ──
# Maps raw indicator data to human-readable states for indicator_state conditions.

def _get_indicator_state(indicator: str, asset_data: dict) -> Optional[str]:
    """Extract the current state of an indicator from cached asset data.

    Returns a state string matching the vocabulary in the plan, or None.
    """
    ind = indicator.upper().replace(' ', '_')

    if ind == 'EMA_RIBBON':
        # Delegates to indicator_eval.ema_ribbon_state — single source of truth.
        return _ie.ema_ribbon_state(asset_data)

    if ind == 'MACD':
        # Delegates to indicator_eval.macd_state — single source of truth.
        return _ie.macd_state(asset_data)

    if ind == 'RSI':
        # Delegates to indicator_eval.rsi_zone — single source of truth.
        return _ie.rsi_zone(asset_data.get('weekly_rsi'))

    if ind == 'CONSENSIO':
        # Delegates to indicator_eval.consensio_state — None-safe; missing/max_p==0 → None.
        return _ie.consensio_state(asset_data)

    if ind in ('STRUCTURE', 'MKT_STRUCTURE'):
        # Plays vocabulary: HH_HL / LH_LL / range.
        # Accept both keys — the condition-builder's indicator_state
        # picker offers 'MKT_STRUCTURE' (the registry's canonical key, whose
        # `states:` array drives the dropdown) alongside the legacy 'STRUCTURE'
        # alias key (kept for plays saved before the registry existed). Both
        # share the exact same vocabulary, so both must evaluate — new saves use
        # 'MKT_STRUCTURE' (see condition-builder.js _addCondFromForm normalization).
        # Delegates to indicator_eval.structure_pattern — single source of truth for this vocab.
        return _ie.structure_pattern(asset_data)

    if ind == 'CPR':
        # Plays vocabulary: above_pivot / below_pivot / between (3 coarse states).
        # Delegates to indicator_eval.cpr_position — single source of truth.
        return _ie.cpr_position(asset_data, 'cpr')

    if ind == 'BB_SQUEEZE':
        # Delegates to indicator_eval.bb_squeeze_state — single source of truth.
        return _ie.bb_squeeze_state(asset_data)

    if ind == 'BB_POSITION':
        # Delegates to indicator_eval.bb_position_state (includes price-fallback compute).
        return _ie.bb_position_state(asset_data)

    if ind == 'VOLUME':
        vol = asset_data.get('volume_metrics', {})
        ratio = vol.get('ratio', 1)
        if ratio > 2:
            return 'high'
        if ratio > 1:
            return 'above_avg'
        if ratio < 0.5:
            return 'low'
        return 'normal'

    if ind == 'ADX':
        # Delegates to indicator_eval.adx_regime_state — single source of truth
        # (mirrors the ADX_REGIME branch below). Filters to the three
        # canonical Plays-vocabulary regimes; anything else — the 'unknown'
        # sentinel, a stale/unrecognized regime label, or missing adx data —
        # preserves this branch's existing None semantics exactly.
        regime = _ie.adx_regime_state(asset_data)
        return regime if regime in ('ranging', 'trend_forming', 'strong_trend') else None

    if ind == 'SUPER_TREND':
        # Unknown direction → None (not 'neutral') — alert/play states are only bullish/bearish.
        # Delegates to indicator_eval.super_trend_state — single source of truth.
        return _ie.super_trend_state(asset_data)

    if ind == 'TTM_SQUEEZE':
        # Delegates to indicator_eval.ttm_squeeze_state — single source of truth.
        return _ie.ttm_squeeze_state(asset_data)

    if ind == 'STOCH_RSI':
        # Delegates to indicator_eval.stoch_rsi_zone — single source of truth.
        return _ie.stoch_rsi_zone(asset_data)

    if ind == 'DONCHIAN':
        # Delegates to indicator_eval.donchian_position — single source of truth.
        return _ie.donchian_position(asset_data)

    if ind == 'ICHIMOKU':
        # Delegates to indicator_eval.ichimoku_cloud_state — single source of truth.
        return _ie.ichimoku_cloud_state(asset_data)

    if ind == 'EMA_CROSS':
        # Sustained EMA fast/slow relationship: bullish or bearish.
        # Delegates to indicator_eval.ema_cross_state — single source of truth.
        return _ie.ema_cross_state(asset_data)

    if ind == 'ALLIGATOR':
        # eating_bullish / eating_bearish / sleeping / mixed
        # Delegates to indicator_eval.alligator_state — single source of truth.
        return _ie.alligator_state(asset_data)

    if ind == 'WEEKLY_CPR':
        # Plays vocabulary: above_pivot / below_pivot / between.
        # Delegates to indicator_eval.cpr_position — single source of truth.
        return _ie.cpr_position(asset_data, 'weekly_cpr')

    if ind == 'DAILY_CPR':
        # Plays vocabulary: above_pivot / below_pivot / between.
        # Delegates to indicator_eval.cpr_position — single source of truth.
        return _ie.cpr_position(asset_data, 'daily_cpr')

    if ind == 'PSAR':
        # long→'bullish', short→'bearish', else None.
        # Delegates to indicator_eval.psar_state — single source of truth.
        return _ie.psar_state(asset_data)

    if ind == 'ICHIMOKU_TK':
        # 'bullish'/'bearish' on cross bar; 'none' between crosses.
        # Delegates to indicator_eval.ichimoku_tk_state — single source of truth.
        return _ie.ichimoku_tk_state(asset_data)

    # ── Alerts-vocabulary states, wired into Plays ──
    # These use the same key names as indicator_state.TRACKED_STATES (rather
    # than the pre-existing RSI/ADX/CPR/WEEKLY_CPR/DAILY_CPR branches above,
    # which use the Plays-only coarse vocabulary) so a Play condition can
    # opt into the granular Alerts vocabulary by name.

    if ind == 'RSI_ZONE':
        # Delegates to indicator_eval.rsi_zone — single source of truth.
        return _ie.rsi_zone(asset_data.get('weekly_rsi'))

    if ind == 'ADX_REGIME':
        # 'unknown' default preserved — delegates to indicator_eval.adx_regime_state.
        return _ie.adx_regime_state(asset_data)

    if ind == 'CPR_POSITION':
        # Granular 10-zone CPR vocabulary (below_s3 .. above_r3), distinct
        # from the coarse 3-state CPR branch above.
        # Delegates to indicator_eval.cpr_zone — single source of truth.
        return _ie.cpr_zone(asset_data, 'cpr')

    if ind == 'WEEKLY_CPR_POSITION':
        # Delegates to indicator_eval.cpr_zone — single source of truth.
        return _ie.cpr_zone(asset_data, 'weekly_cpr')

    if ind == 'DAILY_CPR_POSITION':
        # Delegates to indicator_eval.cpr_zone — single source of truth.
        return _ie.cpr_zone(asset_data, 'daily_cpr')

    if ind == 'EMA_GAP_SQUEEZE':
        # 'squeeze' / 'none' — delegates to indicator_eval.ema_gap_squeeze_state.
        return _ie.ema_gap_squeeze_state(asset_data)

    if ind == 'V_CPR':
        # 'vacuum' / 'none' — delegates to indicator_eval.v_cpr_state.
        return _ie.v_cpr_state(asset_data)

    if ind == 'CPR_EXTREME':
        # 'near_s3' / 'near_r3' / 'none' — delegates to indicator_eval.cpr_extreme_state.
        return _ie.cpr_extreme_state(asset_data)

    if ind == 'STRUCTURE_DISAGREEMENT':
        # 'bearish' / 'bullish' / 'none' — delegates to indicator_eval.structure_disagreement_state.
        return _ie.structure_disagreement_state(asset_data)

    if ind == 'TF_DIVERGENCE':
        # 'active' / 'none' — delegates to indicator_eval.tf_divergence_state.
        return _ie.tf_divergence_state(asset_data)

    if ind == 'RSI_DIVERGENCE':
        # 'bull' / 'bear' / 'hidden_bull' / 'hidden_bear' / 'none' — reads
        # trend_analysis.patterns via indicator_eval.rsi_divergence_pattern_state.
        # DISTINCT from the pre-existing `indicator_divergence` condition type,
        # which reads the separate asset_data['rsi_divergence'] dict field
        # (daily/weekly divergence_type) — do not confuse the two.
        return _ie.rsi_divergence_pattern_state(asset_data)

    if ind == 'PERP_BASIS':
        # 'steep_contango' / 'backwardation' / 'normal' / 'none' — delegates
        # to indicator_eval.perp_basis_state.
        return _ie.perp_basis_state(asset_data)

    return None


def _get_indicator_value(indicator: str, asset_data: dict) -> Optional[float]:
    """Extract a numeric indicator value from cached asset data."""
    ind = indicator.upper().replace(' ', '_')

    if ind == 'RSI':
        val = asset_data.get('weekly_rsi')
        if isinstance(val, (int, float)):
            return float(val)
        return None

    if ind == 'CONSENSIO':
        cons = asset_data.get('consensio', {})
        total = cons.get('total', {})
        return float(total.get('positions', 0))

    if ind == 'ADX':
        return float(asset_data.get('adx', {}).get('adx', 0))

    if ind in ('MACD', 'MACD_HISTOGRAM'):
        return float(asset_data.get('macd', {}).get('histogram', 0))

    if ind == 'MACD_LINE':
        return float(asset_data.get('macd', {}).get('line', 0))

    if ind in ('BB_WIDTH', 'BB_SQUEEZE'):
        return float(asset_data.get('bb', {}).get('width_pct', 0))

    if ind == 'ATR_PCT':
        adx = asset_data.get('adx', {})
        return float(adx.get('atr_pct', 0))

    if ind in ('VOLUME', 'REL_VOLUME'):
        return float(asset_data.get('volume_metrics', {}).get('ratio', 1))

    if ind == 'EMA_RIBBON':
        ribbon = asset_data.get('ema_ribbon', {})
        return float(ribbon.get('days_since_flip', 0))

    return None


# ── Condition Evaluators ──
# Each returns (met: bool, evidence: str)

def _eval_price_level(params: dict, price: float, **_) -> Tuple[bool, str]:
    level = float(params['level'])
    rel = params['relation']
    if rel == 'above':
        met = price > level
    elif rel == 'below':
        met = price < level
    else:  # between — needs two levels
        met = params.get('level_low', 0) <= price <= level
    pct = abs(price - level) / level * 100 if level else 0
    return met, f"Price ${price:,.2f} {'>' if price > level else '<'} ${level:,.2f} ({pct:.1f}% away)"


def _eval_price_zone(params: dict, price: float, **_) -> Tuple[bool, str]:
    low, high = float(params['low']), float(params['high'])
    rel = params['relation']
    inside = low <= price <= high
    if rel == 'inside':
        met = inside
    elif rel == 'outside':
        met = not inside
    elif rel == 'breaks_above':
        met = price > high
    elif rel == 'breaks_below':
        met = price < low
    else:
        met = False
    return met, f"Price ${price:,.2f} vs zone ${low:,.2f}-${high:,.2f} ({rel})"


def _eval_price_change_pct(params: dict, price: float, asset_data: dict = None, **_) -> Tuple[bool, str]:
    threshold = float(params['threshold_pct'])
    direction = params['direction']
    # Use realtime change data if available
    change_pct = 0
    if asset_data:
        rt = asset_data.get('realtime', {})
        period = params.get('period', '24h')
        if period == '24h':
            change_pct = rt.get('change_pct', 0) or 0
        # 7d/30d require historical data — approximate from structure
    if direction == 'up':
        met = change_pct >= threshold
    else:
        met = change_pct <= -threshold
    return met, f"Price change {change_pct:+.1f}% (need {'+' if direction == 'up' else '-'}{threshold}%)"


def _eval_distance_from(params: dict, price: float, scenario: dict = None, **_) -> Tuple[bool, str]:
    ref = params['reference']
    threshold = float(params['threshold_pct'])

    ref_price = None
    if scenario:
        if ref == 'entry':
            low = scenario.get('entry_low')
            high = scenario.get('entry_high')
            if low and high:
                ref_price = (low + high) / 2
            elif low:
                ref_price = low
        elif ref == 'invalidation':
            ref_price = scenario.get('invalidation')
        elif ref == 'target':
            targets = scenario.get('targets', [])
            if targets:
                ref_price = float(targets[0].get('price', 0))
    if ref == 'level':
        ref_price = float(params.get('level', 0))

    if not ref_price:
        return False, f"Reference '{ref}' not set"

    dist_pct = abs(price - ref_price) / ref_price * 100
    met = dist_pct <= threshold
    return met, f"Price ${price:,.2f} is {dist_pct:.1f}% from {ref} ${ref_price:,.2f} (threshold {threshold}%)"


def _eval_candle_close(params: dict, asset_data: dict = None, **_) -> Tuple[bool, str]:
    level = float(params['level'])
    rel = params['relation']
    # Use latest close from OHLCV data
    close = None
    if asset_data:
        rt = asset_data.get('realtime', {})
        close = rt.get('price')  # Current price as proxy for candle forming
    if close is None:
        return False, "No close data available"
    if rel == 'above':
        met = close > level
    else:
        met = close < level
    return met, f"Close ${close:,.2f} {'>' if close > level else '<'} ${level:,.2f}"


def _eval_engulfing_detected(params: dict, asset_data: dict = None, **_) -> Tuple[bool, str]:
    """Evaluate engulfing candle detection from precompute cached signals."""
    direction = params.get('direction')   # 'bullish' or 'bearish'
    timeframe = params.get('timeframe')   # '1h' or '4h'
    signals = (asset_data or {}).get('engulfing_signals', [])
    matched = any(
        s.get('type') == 'engulfing'
        and s.get('direction') == direction
        and s.get('tf') == timeframe
        for s in signals
    )
    if matched:
        return True, f"engulfing {direction} on {timeframe}"
    return False, f"no engulfing {direction} on {timeframe}"


def _eval_indicator_value(params: dict, asset_data: dict = None, **_) -> Tuple[bool, str]:
    indicator = params['indicator']
    operator = params['operator']
    target = float(params['value'])
    val = _get_indicator_value(indicator, asset_data or {})
    if val is None:
        return False, f"{indicator}: no data"

    ops = {'gt': val > target, 'lt': val < target, 'eq': abs(val - target) < 0.01,
           'gte': val >= target, 'lte': val <= target}
    met = ops.get(operator, False)
    op_sym = {'gt': '>', 'lt': '<', 'eq': '=', 'gte': '>=', 'lte': '<='}
    return met, f"{indicator} = {val:.2f} {op_sym.get(operator, '?')} {target}"


def _eval_indicator_crosses(params: dict, asset_data: dict = None, **_) -> Tuple[bool, str]:
    """Check if indicator has crossed a threshold. Approximated from current value."""
    indicator = params['indicator']
    direction = params['direction']
    threshold = float(params['value'])
    val = _get_indicator_value(indicator, asset_data or {})
    if val is None:
        return False, f"{indicator}: no data"

    # We can't detect exact cross from a snapshot — approximate as "value is past threshold"
    if direction == 'above':
        met = val > threshold
    else:
        met = val < threshold
    return met, f"{indicator} = {val:.2f} {'crossed above' if met and direction == 'above' else 'at'} {threshold}"


# CPR-family indicators use a hierarchical/cumulative state
# vocabulary (see indicator_eval.cpr_position/cpr_state_satisfies) — a saved
# condition on the coarse 'above_pivot'/'below_pivot' state must keep matching
# even when the live reading is a more specific band (e.g. 'above_R2').
_CPR_STATE_INDICATORS = {'CPR', 'WEEKLY_CPR', 'DAILY_CPR'}


def _eval_indicator_state(params: dict, asset_data: dict = None, **_) -> Tuple[bool, str]:
    indicator = params['indicator']
    expected = params['state']
    actual = _get_indicator_state(indicator, asset_data or {})
    if actual is None:
        return False, f"{indicator}: no state data"
    ind = indicator.upper().replace(' ', '_')
    if ind in _CPR_STATE_INDICATORS:
        # Hierarchical containment, not plain equality — see _CPR_STATE_INDICATORS.
        met = _ie.cpr_state_satisfies(actual, expected)
    else:
        met = actual == expected
    return met, f"{indicator} state: {actual} (expected: {expected})"


def _eval_indicator_divergence(params: dict, asset_data: dict = None, **_) -> Tuple[bool, str]:
    indicator = params.get('indicator', 'RSI')
    div_type = params['type']  # 'bullish' or 'bearish'
    div_data = (asset_data or {}).get('rsi_divergence', {})
    tf = params.get('timeframe', '1D')
    tf_key = 'daily' if tf in ('1D', '1d', 'D') else 'weekly'
    div = div_data.get(tf_key, {})
    actual = div.get('divergence_type')
    met = actual == div_type
    return met, f"{indicator} divergence: {actual or 'none'} (looking for {div_type})"


def _eval_consensio_threshold(params: dict, asset_data: dict = None, **_) -> Tuple[bool, str]:
    min_val = int(params.get('min_value', 0))
    max_val = params.get('max_value')
    cons = (asset_data or {}).get('consensio', {}).get('total', {})
    positions = cons.get('positions', 0)
    met = positions >= min_val
    if max_val is not None:
        met = met and positions <= int(max_val)
    return met, f"Consensio {positions}/{cons.get('max_positions', '?')} (need >= {min_val})"


def _eval_scanner_verdict(params: dict, asset_data: dict = None, scenario: dict = None,
                          **_) -> Tuple[bool, str]:
    """Scanner verdict for the play's symbol and the rule's direction.

    Not ``asset_data['opportunity']['verdict']``: the opportunity scorer
    never emits that key, so the verdict would always be AVOID and the
    condition would never fire. The verdict lives on the 0–12
    scanner result (``results[i][direction]['verdict']``), keyed by symbol
    — the same source ``_eval_scanner_comparison`` reads.
    """
    direction = params['direction']
    min_verdict = params['min_verdict']
    symbol = ((asset_data or {}).get('symbol')
              or (scenario or {}).get('asset') or '').upper()
    from carrac.caches import get_cached_scanner
    scan = get_cached_scanner() or {}
    side = next(
        ((r.get(direction) or {}) for r in (scan.get('results') or [])
         if (r.get('symbol') or '').upper() == symbol),
        None,
    )
    actual_verdict = (side or {}).get('verdict') if side else None
    if not actual_verdict:
        return False, f"Scanner verdict: no scanner data for {symbol or '?'} {direction}"
    met = VERDICT_ORDER.get(actual_verdict, 0) >= VERDICT_ORDER.get(min_verdict, 0)
    return met, f"Scanner {direction} verdict: {actual_verdict} (need >= {min_verdict})"


def _eval_cross_asset(params: dict, all_assets: dict = None, **_) -> Tuple[bool, str]:
    """Evaluate a condition on a different asset."""
    target_asset = params.get('asset', '').upper()
    sub_type = params.get('condition_type')
    sub_params = params.get('params', {})

    if not all_assets or target_asset not in all_assets:
        return False, f"Cross-asset {target_asset}: no data"

    target_data = all_assets[target_asset]
    target_price = target_data.get('realtime', {}).get('price', target_data.get('latest_price', 0))

    # Delegate to the appropriate evaluator
    evaluator = _EVALUATORS.get(sub_type)
    if evaluator:
        return evaluator(sub_params, price=target_price, asset_data=target_data)
    return False, f"Cross-asset: unknown sub-condition type {sub_type}"


def _eval_time_limit(params: dict, **_) -> Tuple[bool, str]:
    now = datetime.now(timezone.utc)
    time_action = params.get('time_action', 'skip')

    expires_at = params.get('expires_at')
    expires_in_days = params.get('expires_in_days')

    if expires_at:
        try:
            deadline = datetime.fromisoformat(str(expires_at).replace('Z', '+00:00'))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            expired = now > deadline
            days_left = (deadline - now).days
            if expired:
                if time_action == 'notify':
                    # Notify-only deadlines don't count as "met" for triggering
                    return False, f"Deadline passed ({abs(days_left)} days ago) — review due"
                return True, f"Deadline expired ({abs(days_left)} days ago)"
            return False, f"Deadline: {days_left} days remaining"
        except (ValueError, TypeError):
            return False, "Invalid expires_at format"

    if expires_in_days is not None:
        return False, f"Expires in {expires_in_days} days (relative)"

    return False, "No time limit specified"


def _eval_manual(params: dict, **_) -> Tuple[bool, str]:
    """Manual conditions are never auto-evaluated — user checks them."""
    label = params.get('label', 'Manual check')
    return False, f"Manual: {label} (user must check)"


def _eval_formula_expression(params: dict, asset_data: dict = None, **_) -> Tuple[bool, str]:
    """Evaluate a formula expression against OHLCV data for the asset."""
    from carrac.formula_eval import evaluate_formula

    expression = params.get('expression', '')
    timeframe = params.get('timeframe', '1D')

    symbol = ''
    if asset_data:
        symbol = asset_data.get('symbol', '')

    if not symbol:
        return False, "Formula: no asset symbol available"
    if not expression:
        return False, "Formula: empty expression"

    result = evaluate_formula(expression, symbol, timeframe)
    if result.get('error'):
        return False, f"Formula error: {result['error']}"

    return result['met'], result['evidence']


# ── Derivatives condition evaluators ──

def _eval_derivatives_metric(params: dict, asset_data: dict = None,
                             scenario: dict = None, **_) -> Tuple[bool, str]:
    """Evaluate a derivatives metric against a threshold."""
    metric = params.get('metric', '')
    operator = params.get('operator', 'gt')
    target = float(params.get('value', 0))

    # defense-in-depth — reject any metric not in the allowlist
    # (validate_condition should already enforce this at write, but stored
    # pre-validation rows or direct DB inserts could still carry a bad value).
    if metric not in CONDITION_SCHEMAS['derivatives_metric']['metrics']:
        return False, f"{metric}: unknown metric"

    # Validation blocks NEW barred-metric conditions; this blocks
    # STORED ones. Gated on the play's OWNER, not the caller — this runs in a
    # background monitor thread with no request context, so deribit_visible()
    # would resolve False for the operator too and silently break their own
    # plays. The evidence string names no value: it is rendered back to the
    # user, so a raw number there would leak.
    from carrac.compliance.deribit_gate import condition_metric_visible
    owner = (scenario or {}).get('owner_user_id')
    if not condition_metric_visible(metric, user_id=owner):
        return False, f"{metric}: not available"

    try:
        from carrac.db_pool import get_quant_conn as get_conn
        # Determine asset from context or default to BTC
        symbol = 'BTC'
        if asset_data and asset_data.get('symbol'):
            sym = asset_data['symbol'].upper()
            if sym in ('BTC', 'ETH'):
                symbol = sym

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT {metric} FROM quant.derivatives
                WHERE asset = %s AND {metric} IS NOT NULL
                ORDER BY ts DESC LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
            if not row or row.get(metric) is None:
                return False, f"{metric}: no data"
            val = float(row[metric])
    except Exception as e:
        return False, f"{metric}: error ({e})"

    ops = {'gt': val > target, 'lt': val < target,
           'gte': val >= target, 'lte': val <= target}
    met = ops.get(operator, False)
    op_sym = {'gt': '>', 'lt': '<', 'gte': '>=', 'lte': '<='}
    return met, f"{metric} = {val:.2f} {op_sym.get(operator, '?')} {target}"


def _eval_derivatives_regime(params: dict, **_) -> Tuple[bool, str]:
    """Evaluate whether current derivatives regime matches target."""
    target_regime = params.get('regime', '')
    condition = params.get('condition', 'matches')

    try:
        from carrac.db_pool import get_quant_conn as get_conn
        # classify_asset_row owns the raw-column → classifier-key
        # mapping; raw names here dropped basis/put-call/term-slope and made
        # play conditions disagree with quant_get_regime's label.
        from quant.engine.derivatives_regime import classify_asset_row

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT basis_pct, skew_25d, dvol, iv_rv_premium,
                       options_put_call_ratio, term_structure_slope,
                       gex_net, gex_flip_price
                FROM quant.derivatives WHERE asset = 'BTC'
                ORDER BY ts DESC LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return False, "Derivatives regime: no data"

            metrics = {k: float(v) for k, v in row.items() if v is not None}
            result = classify_asset_row(metrics)
            if result is None:
                return False, "Derivatives regime: no classification signal"
            current = result.get('regime', 'unknown')

        if condition == 'matches':
            met = current == target_regime
        else:
            met = current != target_regime

        return met, f"Regime: {result.get('regime_name', current)} ({result.get('confidence', 0):.0%}) {'==' if condition == 'matches' else '!='} {target_regime}"
    except Exception as e:
        return False, f"Derivatives regime: error ({e})"


_REL_OPS = {'gt': lambda a, b: a > b, 'lt': lambda a, b: a < b,
            'gte': lambda a, b: a >= b, 'lte': lambda a, b: a <= b}


def _eval_scanner_comparison(params: dict, **_) -> Tuple[bool, str]:
    """Compare two assets/baskets via scanner totals on a chosen side."""
    asset_a = (params.get('asset_a') or '').upper().strip()
    asset_b = (params.get('asset_b') or '').upper().strip()
    side = params.get('side')
    relation = params.get('relation')
    margin_pct = params.get('margin_pct')

    if not (asset_a and asset_b and side in ('long', 'short') and relation in _REL_OPS):
        return False, "scanner_comparison: invalid params"
    try:
        from carrac.caches import get_cached_scanner
        scan = get_cached_scanner() or {}
        results = scan.get('results') or []
        get_total = lambda sym: next(
            ((r.get(side) or {}).get('total') for r in results
             if r.get('symbol') == sym), None
        )
        a_total = get_total(asset_a)
        b_total = get_total(asset_b)
        if a_total is None or b_total is None:
            return False, f"scanner_comparison: missing scanner data for {asset_a}/{asset_b}"
        cmp_b = b_total
        if margin_pct:
            cmp_b = b_total * (1 + float(margin_pct) / 100.0)
        met = _REL_OPS[relation](a_total, cmp_b)
        return met, (
            f"{asset_a}.{side}={a_total:.2f} {relation} {asset_b}.{side}={b_total:.2f}"
            + (f' (margin {margin_pct}%)' if margin_pct else '')
        )
    except Exception as e:
        return False, f"scanner_comparison: error ({e})"


def _eval_bb_breakout_retest_engulfing(
    params: dict, asset_data: dict = None, **_
) -> Tuple[bool, str]:
    """Evaluate the BB breakout-retest-engulfing continuation setup.

    Three-part check on current bar:
    1. Price closed past the relevant BB band within the last lookback_bars bars.
    2. Current close is within proximity_pct % of the middle band (retest).
    3. A same-direction engulfing candle just printed on the current bar.

    Transition suppression: fires at most once per bar close. A Redis fingerprint
    keyed as ``mcd:bb_brt:{play_id}:{tf}:{bar_ts_isodate}`` prevents re-firing
    on repeat precompute cycles within the same bar.

    require_volume (default true): the existing engulfing detector already gates
    on vol > 20-SMA, so all cached signals satisfy this implicitly. The param is
    kept for forward-compatibility; a false value has no effect in v1.
    """
    direction = params.get('direction')        # 'bullish' | 'bearish'
    timeframe = params.get('timeframe')        # '1h' | '4h'
    lookback = int(params.get('lookback_bars', 15))
    proximity_pct = float(params.get('proximity_pct', 0.5))

    symbol = (asset_data or {}).get('symbol', '')
    if not symbol:
        return False, "bb_breakout_retest_engulfing: no symbol in asset_data"

    # Map schema enum values to database dict keys
    _HTF_TF_MAP = {'D': '1D', '3D': '3D', 'W': '1W'}

    try:
        from consensio.indicators.bollinger import BollingerBandCalculator
        from carrac.services import get_services

        svc = get_services()
        if timeframe in ('1h', '4h'):
            data = svc.db.get_ltf_multi_timeframe_data(symbol)
            tf_df = data.get(timeframe)
        else:
            # Daily / 3-Day / Weekly — use the HTF data source
            data = svc.db.get_multi_timeframe_data(symbol)
            db_key = _HTF_TF_MAP.get(timeframe, timeframe)
            tf_df = data.get(db_key)
        if tf_df is None or len(tf_df) < 30:
            return False, f"bb_breakout_retest_engulfing: insufficient {timeframe} data"

        bb = BollingerBandCalculator(period=20, std_dev=2.0).calculate_series(tf_df)
        if bb is None:
            return False, "bb_breakout_retest_engulfing: BB series computation failed"

        # Step 1 — breakout check (excludes current bar; current bar is the retest bar)
        window_close = tf_df['close'].iloc[-(lookback + 1):-1]
        if direction == 'bullish':
            window_band = bb.upper.iloc[-(lookback + 1):-1]
            breakout_occurred = (window_close > window_band).any()
        else:
            window_band = bb.lower.iloc[-(lookback + 1):-1]
            breakout_occurred = (window_close < window_band).any()

        if not breakout_occurred:
            return False, f"no breakout in last {lookback} bars"

        # Step 2 — retest check (current bar must be near middle)
        current_close = float(tf_df['close'].iloc[-1])
        current_middle = float(bb.middle.iloc[-1])
        if current_middle <= 0:
            return False, "bb_breakout_retest_engulfing: invalid middle band value"
        distance_pct = abs(current_close - current_middle) / current_middle * 100
        if distance_pct > proximity_pct:
            return (
                False,
                f"price {distance_pct:.2f}% from middle, threshold {proximity_pct}%",
            )

        # Step 3 — engulfing check (current bar, matching direction + tf)
        signals = (asset_data or {}).get('engulfing_signals', [])
        matched = any(
            s.get('type') == 'engulfing'
            and s.get('direction') == direction
            and s.get('tf') == timeframe
            and s.get('candle_pos') == 'current'
            for s in signals
        )
        if not matched:
            return False, f"no {direction} engulfing on current {timeframe} bar"

        # All three checks passed — apply per-bar transition suppression via Redis
        try:
            from carrac.caches import _get_redis

            bar_ts = str(tf_df.index[-1]).split(' ')[0]  # date portion — unique per bar
            r = _get_redis()
            fp_key = f'mcd:bb_brt:{symbol}:{timeframe}:{bar_ts}'
            already_fired = r.get(fp_key) if r else None
            if already_fired:
                return (
                    False,
                    f"bb_breakout_retest_engulfing: already fired this bar ({bar_ts})",
                )
            if r:
                r.set(fp_key, '1', ex=7200)  # 2h TTL — well past bar close
        except Exception:
            pass  # Redis unavailable — allow through (best-effort suppression)

        evidence = (
            f"broke {'above upper' if direction == 'bullish' else 'below lower'} BB "
            f"within {lookback} bars, retested to {distance_pct:.2f}% of middle, "
            f"{direction} engulfing confirmed"
        )
        return True, evidence

    except Exception as e:
        return False, f"bb_breakout_retest_engulfing: error ({e})"


def _eval_bb_breakdown_continuation(
    params: dict, asset_data: dict = None, **_
) -> Tuple[bool, str]:
    """Evaluate the BB breakout/breakdown continuation setup (Setup F).

    Generalises ``bb_breakout_retest_engulfing`` for the breakdown-short pattern:

    1. A band breakout occurred within the last ``lookback_bars`` bars (excludes
       the current bar) — close below the lower band (bearish) / above the upper
       band (bullish).
    2. Entry is confirmed on the current bar by EITHER a same-direction engulfing
       OR a retest of a chosen band (``retest_band='broken'`` → the band that was
       broken, e.g. the lower band for a bearish breakdown; ``'middle'`` → the
       Setup-C-style mid-band pullback). ``confirm_mode`` selects which legs count.

    Unlike Setups B/C, this rule accepts custom BB params (period / std_dev /
    offset) via ``bb_params`` so a chart-side override like a +10 offset carries
    into the signal. ``calculate_series()`` applies the offset (TradingView-style:
    positive = lag the bands).

    Transition suppression: fires at most once per bar close via a Redis
    fingerprint keyed ``mcd:bb_bdc:{symbol}:{tf}:{bar_ts_isodate}`` (2h TTL,
    best-effort — Redis unavailability does not block firing).
    """
    direction = params.get('direction')        # 'bullish' | 'bearish'
    timeframe = params.get('timeframe')
    lookback = int(params.get('lookback_bars', 15))
    confirm_mode = params.get('confirm_mode', 'either')   # engulfing | band_retest | either
    retest_band = params.get('retest_band', 'broken')     # broken | middle
    proximity_pct = float(params.get('proximity_pct', 0.5))
    bb_p = params.get('bb_params') or {}
    period = int(bb_p.get('period', 20))
    std_dev = float(bb_p.get('std_dev', 2.0))
    offset = int(bb_p.get('offset', 0))

    symbol = (asset_data or {}).get('symbol', '')
    if not symbol:
        return False, "bb_breakdown_continuation: no symbol in asset_data"

    _HTF_TF_MAP = {'D': '1D', '3D': '3D', 'W': '1W'}

    try:
        from consensio.indicators.bollinger import BollingerBandCalculator
        from carrac.services import get_services

        svc = get_services()
        if timeframe in ('1h', '4h'):
            data = svc.db.get_ltf_multi_timeframe_data(symbol)
            tf_df = data.get(timeframe)
        else:
            data = svc.db.get_multi_timeframe_data(symbol)
            db_key = _HTF_TF_MAP.get(timeframe, timeframe)
            tf_df = data.get(db_key)
        if tf_df is None or len(tf_df) < 30:
            return False, f"bb_breakdown_continuation: insufficient {timeframe} data"

        bb = BollingerBandCalculator(
            period=period, std_dev=std_dev, offset=offset
        ).calculate_series(tf_df)
        if bb is None:
            return False, "bb_breakdown_continuation: BB series computation failed"

        # Step 1 — breakout/breakdown within lookback (excludes current bar)
        window_close = tf_df['close'].iloc[-(lookback + 1):-1]
        if direction == 'bullish':
            window_band = bb.upper.iloc[-(lookback + 1):-1]
            breakout_occurred = (window_close > window_band).any()
        else:
            window_band = bb.lower.iloc[-(lookback + 1):-1]
            breakout_occurred = (window_close < window_band).any()
        if not breakout_occurred:
            kind = 'breakout' if direction == 'bullish' else 'breakdown'
            return False, f"no {kind} in last {lookback} bars"

        # Step 2 — confirmation legs (engulfing and/or band retest), per confirm_mode
        engulfing_ok = False
        if confirm_mode in ('engulfing', 'either'):
            signals = (asset_data or {}).get('engulfing_signals', [])
            engulfing_ok = any(
                s.get('type') == 'engulfing'
                and s.get('direction') == direction
                and s.get('tf') == timeframe
                and s.get('candle_pos') == 'current'
                for s in signals
            )

        retest_ok = False
        retest_dist = None
        ref_label = ''
        if confirm_mode in ('band_retest', 'either'):
            current_close = float(tf_df['close'].iloc[-1])
            if retest_band == 'middle':
                ref = float(bb.middle.iloc[-1]); ref_label = 'middle'
            elif direction == 'bullish':
                ref = float(bb.upper.iloc[-1]); ref_label = 'upper'   # broken band for a bullish breakout
            else:
                ref = float(bb.lower.iloc[-1]); ref_label = 'lower'   # broken band for a bearish breakdown
            if ref and ref > 0:   # guards NaN (NaN > 0 is False) and non-positive bands
                retest_dist = abs(current_close - ref) / ref * 100
                retest_ok = retest_dist <= proximity_pct

        if not (engulfing_ok or retest_ok):
            bits = []
            if confirm_mode in ('engulfing', 'either'):
                bits.append(f"no {direction} engulfing on current {timeframe} bar")
            if confirm_mode in ('band_retest', 'either'):
                if retest_dist is None:
                    bits.append("no valid retest band value")
                else:
                    bits.append(f"price {retest_dist:.2f}% from {ref_label} band, threshold {proximity_pct}%")
            return False, "; ".join(bits) or "bb_breakdown_continuation: confirmation not met"

        # Confirmation met — per-bar transition suppression via Redis
        try:
            from carrac.caches import _get_redis

            bar_ts = str(tf_df.index[-1]).split(' ')[0]  # date portion — unique per bar
            r = _get_redis()
            fp_key = f'mcd:bb_bdc:{symbol}:{timeframe}:{bar_ts}'
            if r and r.get(fp_key):
                return False, f"bb_breakdown_continuation: already fired this bar ({bar_ts})"
            if r:
                r.set(fp_key, '1', ex=7200)  # 2h TTL — well past bar close
        except Exception:
            pass  # Redis unavailable — allow through (best-effort suppression)

        broke_label = 'above upper' if direction == 'bullish' else 'below lower'
        confirms = []
        if engulfing_ok:
            confirms.append(f"{direction} engulfing")
        if retest_ok:
            confirms.append(f"retest to {retest_dist:.2f}% of {ref_label} band")
        offset_note = f", offset {offset:+d}" if offset else ""
        evidence = (
            f"broke {broke_label} BB within {lookback} bars{offset_note}, "
            f"confirmed by {' + '.join(confirms)}"
        )
        return True, evidence

    except Exception as e:
        return False, f"bb_breakdown_continuation: error ({e})"


def _eval_bb_fade_engulfing(
    params: dict, asset_data: dict = None, **_
) -> Tuple[bool, str]:
    """Evaluate the BB-fade-engulfing setup in a single rule.

    Same-bar AND: price is outside the expected BB band (below lower for bullish fade,
    above upper for bearish fade) AND a same-direction engulfing just printed on the
    current bar of the requested timeframe.

    BB params are fixed at defaults (period=20, std-dev=2.0, offset=0) — the precomputed
    bb.position in asset_data uses these defaults.  Users needing custom BB params should
    fall back to the composable path (indicator_state BB_POSITION + engulfing_detected).
    """
    direction = params['direction']
    timeframe = params['timeframe']

    bb = (asset_data or {}).get('bb', {})
    position = bb.get('position')  # 'inside' | 'above_upper' | 'below_lower' | None
    expected_position = 'below_lower' if direction == 'bullish' else 'above_upper'
    if position != expected_position:
        return False, f"BB position is {position or 'unknown'}, need {expected_position}"

    signals = (asset_data or {}).get('engulfing_signals', [])
    matched = any(
        s.get('type') == 'engulfing'
        and s.get('direction') == direction
        and s.get('tf') == timeframe
        and s.get('candle_pos') == 'current'
        for s in signals
    )
    if not matched:
        return False, f"no {direction} engulfing on current {timeframe} bar"

    return True, f"BB position {expected_position} AND {direction} engulfing confirmed on {timeframe}"


def _longest_true_run(series) -> int:
    """Return the length of the longest consecutive True run in a boolean Series.

    Used by :func:`_eval_bb_squeeze_breakout` to determine whether the squeeze
    was held for at least *min_squeeze_bars* consecutive bars.

    Args:
        series: pandas Series of bool values.

    Returns:
        int: length of the longest consecutive True run, or 0 if none found.
    """
    max_run = 0
    current_run = 0
    for val in series:
        if bool(val):
            current_run += 1
            if current_run > max_run:
                max_run = current_run
        else:
            current_run = 0
    return max_run


def _eval_bb_squeeze_breakout(
    params: dict, asset_data: dict = None, **_
) -> Tuple[bool, str]:
    """Evaluate the BB squeeze → directional breakout setup.

    Classic TTM Squeeze flavor: bands compressed for at least *min_squeeze_bars*
    consecutive bars within the last *lookback_bars* bars → squeeze has released
    on the current bar → current close breaks the relevant band directionally.
    Optionally gates on volume > 20-SMA.

    Transition suppression: fires at most once per bar close via a Redis
    fingerprint keyed as ``mcd:bb_sqbo:{symbol}:{tf}:{bar_ts_isodate}`` with
    a 2-hour TTL — same pattern as ``bb_breakout_retest_engulfing``.

    BB params are fixed at defaults (period=20, std_dev=2.0).
    """
    direction = params.get('direction')        # 'bullish' | 'bearish'
    timeframe = params.get('timeframe')        # '1h' | '4h' | 'D' | '3D' | 'W'
    lookback = int(params.get('lookback_bars', 10))
    min_squeeze = int(params.get('min_squeeze_bars', 3))
    require_vol = params.get('require_volume', True)

    symbol = (asset_data or {}).get('symbol', '')
    if not symbol:
        return False, "bb_squeeze_breakout: no symbol in asset_data"

    _HTF_TF_MAP = {'D': '1D', '3D': '3D', 'W': '1W'}

    try:
        from consensio.indicators.bollinger import BollingerBandCalculator
        from carrac.services import get_services

        svc = get_services()
        if timeframe in ('1h', '4h'):
            data = svc.db.get_ltf_multi_timeframe_data(symbol)
            tf_df = data.get(timeframe)
        else:
            data = svc.db.get_multi_timeframe_data(symbol)
            db_key = _HTF_TF_MAP.get(timeframe, timeframe)
            tf_df = data.get(db_key)

        if tf_df is None or len(tf_df) < 30:
            return False, f"bb_squeeze_breakout: insufficient {timeframe} data"

        bb = BollingerBandCalculator(period=20, std_dev=2.0).calculate_squeeze_series(tf_df)
        if bb is None:
            return False, "bb_squeeze_breakout: BB series computation failed"

        # Step 1 — squeeze history check (excludes current bar)
        recent_squeeze = bb.is_squeeze.iloc[-(lookback + 1):-1]
        max_run = _longest_true_run(recent_squeeze)
        if max_run < min_squeeze:
            return (
                False,
                f"longest squeeze in last {lookback} bars was {max_run} bars, need >= {min_squeeze}",
            )

        # Step 2 — squeeze must have released on current bar
        if bool(bb.is_squeeze.iloc[-1]):
            return False, "squeeze still active on current bar"

        # Step 3 — directional breakout: current close past relevant band
        close = float(tf_df['close'].iloc[-1])
        if direction == 'bullish':
            upper = float(bb.upper.iloc[-1])
            if close <= upper:
                return False, f"current close {close:.2f} not above upper band {upper:.2f}"
        else:  # bearish
            lower = float(bb.lower.iloc[-1])
            if close >= lower:
                return False, f"current close {close:.2f} not below lower band {lower:.2f}"

        # Step 4 — optional volume confirmation
        vol = None
        vol_avg = None
        if require_vol:
            vol = float(tf_df['volume'].iloc[-1])
            vol_avg = float(tf_df['volume'].rolling(20).mean().iloc[-1])
            if vol <= vol_avg:
                return False, f"breakout volume {vol:.0f} not above 20-SMA {vol_avg:.0f}"

        # All checks passed — per-bar transition suppression via Redis
        try:
            from carrac.caches import _get_redis

            bar_ts = str(tf_df.index[-1]).split(' ')[0]  # date portion — unique per bar
            r = _get_redis()
            fp_key = f'mcd:bb_sqbo:{symbol}:{timeframe}:{bar_ts}'
            already_fired = r.get(fp_key) if r else None
            if already_fired:
                return (
                    False,
                    f"bb_squeeze_breakout: already fired this bar ({bar_ts})",
                )
            if r:
                r.set(fp_key, '1', ex=7200)  # 2h TTL — well past bar close
        except Exception:
            pass  # Redis unavailable — allow through (best-effort suppression)

        direction_label = 'above upper' if direction == 'bullish' else 'below lower'
        evidence = (
            f"squeeze of {max_run} bars released, current bar broke {direction_label} band"
        )
        if require_vol and vol is not None and vol_avg is not None:
            evidence += f" with volume {vol:.0f} > avg {vol_avg:.0f}"
        return True, evidence

    except Exception as e:
        return False, f"bb_squeeze_breakout: error ({e})"


def _eval_cross_play_reference(params: dict, user_id: int = None, **_) -> Tuple[bool, str]:
    """Trigger off the status of another play."""
    target_play_id = params.get('play_id')
    target_status = params.get('target_status')
    if not target_play_id or target_status not in (
        'watching', 'acting', 'completed', 'invalidated', 'expired',
    ):
        return False, "cross_play_reference: invalid params"
    try:
        from carrac.plays.models import get_play
        play = get_play(int(target_play_id), user_id=user_id)
        if not play:
            return False, f"cross_play_reference: play {target_play_id} not found"
        met = play.get('status') == target_status
        return met, (
            f"Play #{target_play_id} status={play.get('status')} "
            f"{'==' if met else '!='} {target_status}"
        )
    except Exception as e:
        return False, f"cross_play_reference: error ({e})"


# Evaluator dispatch table
_EVALUATORS = {
    'price_level': _eval_price_level,
    'price_zone': _eval_price_zone,
    'price_change_pct': _eval_price_change_pct,
    'distance_from': _eval_distance_from,
    'candle_close': _eval_candle_close,
    'engulfing_detected': _eval_engulfing_detected,
    'bb_breakout_retest_engulfing': _eval_bb_breakout_retest_engulfing,
    'bb_breakdown_continuation': _eval_bb_breakdown_continuation,
    'bb_fade_engulfing': _eval_bb_fade_engulfing,
    'bb_squeeze_breakout': _eval_bb_squeeze_breakout,
    'indicator_value': _eval_indicator_value,
    'indicator_crosses': _eval_indicator_crosses,
    'indicator_state': _eval_indicator_state,
    'indicator_divergence': _eval_indicator_divergence,
    'consensio_threshold': _eval_consensio_threshold,
    'scanner_verdict': _eval_scanner_verdict,
    'cross_asset': _eval_cross_asset,
    'time_limit': _eval_time_limit,
    'manual': _eval_manual,
    'formula_expression': _eval_formula_expression,
    'derivatives_metric': _eval_derivatives_metric,
    'derivatives_regime': _eval_derivatives_regime,
    'scanner_comparison': _eval_scanner_comparison,
    'cross_play_reference': _eval_cross_play_reference,
}


# ── Custom Logic Expression Parser ──

def _parse_logic_expression(expression: str, rule_states: Dict[str, bool]) -> bool:
    """Evaluate a custom logic expression like "(r1 AND r2) OR NOT r3".

    Grammar (only): rule/condition IDs, AND, OR, NOT, parentheses.

    Implemented as a tiny recursive-descent parser over the ``rule_states``
    bool map — deliberately NOT ``eval()``. The previous implementation ran a
    real ``eval()`` guarded only by a hand-maintained character allowlist; its
    safety was emergent from exactly which letters spell the keywords, so any
    future widening of the token syntax could silently reopen code execution.
    A malformed or hostile expression can never reach a code-execution sink
    here, regardless of the characters it contains.
    """
    expr = (expression or '').strip()
    if not expr:
        return False

    # 1) Tokenize into '(', ')', AND/OR/NOT keywords, or rule-id identifiers.
    tokens = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch in '()':
            tokens.append(ch)
            i += 1
            continue
        m = re.match(r'[A-Za-z_]\w*', expr[i:])
        if not m:
            logger.warning(f"Unsafe expression rejected (bad token): {expression}")
            return False
        word = m.group(0)
        i += len(word)
        up = word.upper()
        tokens.append(up if up in ('AND', 'OR', 'NOT') else ('RULE', word))

    # 2) Recursive-descent parse: OR binds loosest, then AND, then NOT, then atom.
    pos = [0]

    def _peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def _advance():
        pos[0] += 1

    def _parse_or():
        val = _parse_and()
        while _peek() == 'OR':
            _advance()
            val = _parse_and() or val
        return val

    def _parse_and():
        val = _parse_not()
        while _peek() == 'AND':
            _advance()
            rhs = _parse_not()
            val = val and rhs
        return val

    def _parse_not():
        if _peek() == 'NOT':
            _advance()
            return not _parse_not()
        return _parse_atom()

    def _parse_atom():
        tok = _peek()
        if tok == '(':
            _advance()
            val = _parse_or()
            if _peek() != ')':
                raise ValueError('unbalanced parentheses')
            _advance()
            return val
        if isinstance(tok, tuple) and tok[0] == 'RULE':
            _advance()
            return bool(rule_states.get(tok[1], False))
        raise ValueError(f'unexpected token: {tok}')

    try:
        result = _parse_or()
        if pos[0] != len(tokens):
            raise ValueError('trailing tokens')
        return bool(result)
    except (ValueError, IndexError):
        logger.warning(f"Expression evaluation failed: {expression}")
        return False


def evaluate_logic(conditions: dict, rule_states: Dict[str, bool]) -> bool:
    """Evaluate whether the overall conditions are met based on logic type."""
    logic = conditions.get('logic', 'all')

    if logic == 'all':
        return all(rule_states.values()) if rule_states else False
    elif logic == 'any':
        return any(rule_states.values()) if rule_states else False
    elif logic == 'custom':
        expr = conditions.get('expression', '')
        return _parse_logic_expression(expr, rule_states)
    return False


# ── Main Evaluation Entry Points ──

def evaluate_conditions_dict(conditions: dict, market_data: dict,
                              context: Optional[dict] = None) -> dict:
    """Evaluate a conditions dict against market data.

    Lower-level than evaluate_scenario() — no scenario-level invalidation
    price check, no status suggestions. Used for milestone condition evaluation.

    Args:
        conditions: {rules: [...], logic: 'all'|'any'|'custom', expression?: str}
        market_data: {price, asset_data, all_assets}
        context: Optional dict with entry_low, entry_high, invalidation, targets,
                 direction — passed as 'scenario' to evaluators (for distance_from).

    Returns:
        {rules, met_count, total_count, all_met}
    """
    rules = conditions.get('rules', [])
    if not rules:
        return {'rules': [], 'met_count': 0, 'total_count': 0, 'all_met': False}

    price = market_data.get('price', 0)
    asset_data = market_data.get('asset_data', {})
    all_assets = market_data.get('all_assets', {})
    pseudo_scenario = context or {}

    now_iso = datetime.now(timezone.utc).isoformat()
    evaluated_rules = []
    rule_states = {}

    for rule in rules:
        rid = rule.get('id', 'unknown')
        rtype = rule.get('type', 'unknown')
        params = _effective_params(rule)

        evaluator = _EVALUATORS.get(rtype)
        if evaluator:
            try:
                met, evidence = evaluator(
                    params,
                    price=price,
                    asset_data=asset_data,
                    scenario=pseudo_scenario,
                    all_assets=all_assets,
                )
            except Exception as e:
                logger.warning(f"Condition eval error for {rid}/{rtype}: {e}")
                met, evidence = False, f"Evaluation error: {e}"
                # Beacon once per unique (rid, rtype, error-type) so genuinely malformed
                # rules don't silently disappear — but we don't spam on every eval tick.
                _beacon_key = (rid, rtype, type(e).__name__)
                if _beacon_key not in _EVAL_ERROR_BEACONED:
                    _EVAL_ERROR_BEACONED.add(_beacon_key)
                    try:
                        import sentry_sdk
                        sentry_sdk.capture_exception(e)
                    except Exception:
                        pass
        else:
            met, evidence = False, f"Unknown condition type: {rtype}"

        met_at = rule.get('met_at')
        if met and not met_at:
            met_at = now_iso
        elif not met:
            met_at = None

        evaluated_rules.append({
            'id': rid, 'type': rtype, 'params': params,
            'met': met, 'met_at': met_at, 'evidence': evidence,
        })
        rule_states[rid] = met

    met_count = sum(1 for r in evaluated_rules if r['met'])
    total_count = len(evaluated_rules)
    all_met = evaluate_logic(conditions, rule_states)

    return {
        'rules': evaluated_rules,
        'met_count': met_count,
        'total_count': total_count,
        'all_met': all_met,
    }


def evaluate_scenario(scenario: dict, market_data: dict) -> dict:
    """Evaluate all conditions for a scenario against current market data.

    Args:
        scenario: Scenario dict from DB (with conditions, entry_*, invalidation, etc.)
        market_data: Dict with keys:
            - 'price': current price (float)
            - 'asset_data': cached asset dict from ResponseCache
            - 'all_assets': dict of {symbol: asset_data} for cross-asset conditions

    Returns:
        {
            'rules': [{id, type, met, met_at, evidence}],
            'met_count': int,
            'total_count': int,
            'all_met': bool,
            'invalidation_triggered': bool,
            'suggested_status': str or None,
        }
    """
    conditions = scenario.get('conditions') or {'rules': [], 'logic': 'all'}
    price = market_data.get('price', 0)

    if not conditions.get('rules'):
        return {
            'rules': [], 'met_count': 0, 'total_count': 0,
            'all_met': False, 'invalidation_triggered': False,
            'suggested_status': None,
        }

    # Evaluate rules via shared helper (scenario itself is the context)
    result = evaluate_conditions_dict(conditions, market_data, context=scenario)
    all_met = result['all_met']
    met_count = result['met_count']
    total_count = result['total_count']

    # Check invalidation rules
    invalidation_triggered = False
    asset_data = market_data.get('asset_data', {})
    inv_rules = conditions.get('invalidation_rules', [])
    for inv_rule in inv_rules:
        rtype = inv_rule.get('type', 'unknown')
        params = _effective_params(inv_rule)
        evaluator = _EVALUATORS.get(rtype)
        if evaluator:
            try:
                met, _ = evaluator(params, price=price, asset_data=asset_data, scenario=scenario)
                if met:
                    invalidation_triggered = True
                    break
            except Exception:
                logger.exception(
                    "scenario %s: invalidation rule %r (%s) failed to evaluate",
                    scenario.get('id'), inv_rule.get('id'), rtype,
                )

    # Also check scenario-level invalidation price
    inv_price = scenario.get('invalidation')
    if inv_price and price:
        direction = scenario.get('direction')
        if direction == 'long' and price < inv_price:
            invalidation_triggered = True
        elif direction == 'short' and price > inv_price:
            invalidation_triggered = True

    # Suggest status transition
    suggested_status = None
    current = scenario.get('status')
    if invalidation_triggered and current in ('watching', 'approaching', 'triggered'):
        suggested_status = 'invalidated'
    elif all_met and current in ('watching', 'approaching'):
        suggested_status = 'triggered'
    elif met_count > 0 and met_count < total_count and current == 'watching':
        # Check if price is near entry zone
        entry_low = scenario.get('entry_low')
        entry_high = scenario.get('entry_high')
        if entry_low and entry_high and price:
            zone_mid = (entry_low + entry_high) / 2
            dist = abs(price - zone_mid) / zone_mid * 100
            if dist < 5:  # Within 5% of entry zone
                suggested_status = 'approaching'

    return {
        'rules': result['rules'],
        'met_count': met_count,
        'total_count': total_count,
        'all_met': all_met,
        'invalidation_triggered': invalidation_triggered,
        'suggested_status': suggested_status,
    }
