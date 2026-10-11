"""
Shared indicator-state evaluation primitives.

This module is the single source of truth for the state-classification logic
shared by:
  - carrac/indicator_state.py  (Alerts evaluator / fingerprinting)
  - carrac/conditions.py       (Plays condition evaluator)

Rule: adding a new indicator's classification logic here automatically makes it
available to BOTH evaluators.  After adding a function here, wire it up in:
  1. indicator_state.py  — add/update the private helper + TRACKED_STATES entry
  2. conditions.py        — add/update the `if ind == '...'` branch in
                            `_get_indicator_state()`

Each public function below:
  - Takes an ``asset_data`` dict (the precomputed per-asset dict from
    ResponseCache / _compute_assets_response).
  - Returns a state string (or None if data is absent / cannot be determined).
  - Is BEHAVIOUR-PRESERVING relative to the code it replaced (same thresholds,
    same return strings, same None-handling).

Indicators whose Alerts and Plays vocabularies are unified here:
  - consensio_state     → consensio_state() — None-safe; missing/max_p==0 → None
  - structure_trend     → structure_trend()  — Alerts vocabulary: bullish/bearish/neutral
  - structure_pattern   → structure_pattern() — Plays vocabulary: HH_HL/LH_LL/range
  - cpr_zone            → cpr_zone()  — Alerts vocabulary: 10 granular zones
  - cpr_position        → cpr_position() — Plays vocabulary: 3 coarse states
                            (covers the full 9-state
                            registry vocabulary — above_pivot/below_pivot/between
                            plus above_R1/R2/R3 and below_S1/S2/S3 — while keeping
                            the 3-state saved-condition semantics intact via
                            cpr_state_satisfies()'s hierarchical containment check)
  - super_trend_state   → super_trend_state() — unknown direction → None (not 'neutral')
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cockpit(asset_data: dict, key: str) -> dict:
    """Return a sub-dict from cockpit_extended (None-safe)."""
    return ((asset_data.get('cockpit_extended') or {}).get(key)) or {}


def _current_price(asset_data: dict) -> float:
    """Resolve the best-available current price from an asset dict."""
    price = asset_data.get('latest_price') or 0
    rt = asset_data.get('realtime') or {}
    if rt.get('price'):
        price = rt['price']
    return price


# ---------------------------------------------------------------------------
# RSI zone
# ---------------------------------------------------------------------------

def rsi_zone(val: Optional[float]) -> Optional[str]:
    """Classify a raw RSI value into a discrete zone.

    Returns:
        'oversold'   if val < 30
        'overbought' if val > 70
        'neutral'    otherwise
        None         if val is None
    """
    if val is None:
        return None
    if val < 30:
        return 'oversold'
    if val > 70:
        return 'overbought'
    return 'neutral'


# ---------------------------------------------------------------------------
# EMA Ribbon
# ---------------------------------------------------------------------------

def ema_ribbon_state(asset_data: dict) -> str:
    """Normalise EMA Ribbon trend to 'bullish' / 'bearish' / 'neutral'.

    Accepts both the short form ('bull'/'bear') stored in older data and the
    long form ('bullish'/'bearish') produced by newer precompute runs.
    """
    ribbon = asset_data.get('ema_ribbon') or {}
    trend = ribbon.get('trend', '')
    if trend in ('bull', 'bullish'):
        return 'bullish'
    if trend in ('bear', 'bearish'):
        return 'bearish'
    return 'neutral'


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def macd_state(asset_data: dict) -> str:
    """Return MACD histogram direction: 'bullish' / 'bearish' / 'flat'."""
    macd = asset_data.get('macd') or {}
    hist = macd.get('histogram', 0) or 0
    if hist > 0:
        return 'bullish'
    if hist < 0:
        return 'bearish'
    return 'flat'


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def bb_squeeze_state(asset_data: dict) -> str:
    """Classify BB squeeze into 'squeezed' / 'expanded' / 'normal'.

    squeezed  — bb.squeeze is truthy
    expanded  — bb.width_pct > 6  (bands are unusually wide)
    normal    — everything else
    """
    bb = asset_data.get('bb') or {}
    if bb.get('squeeze'):
        return 'squeezed'
    width = bb.get('width_pct', 0) or 0
    if width > 6:
        return 'expanded'
    return 'normal'


def bb_position_state(asset_data: dict) -> Optional[str]:
    """Classify price position relative to Bollinger Bands.

    Returns:
        'above_upper'  / 'below_lower' / 'inside'
        None           if insufficient data

    Preferred source: ``bb.position`` (pre-stored by precompute).
    Fallback:         compute from bb.upper / bb.lower + current price.
    """
    bb = asset_data.get('bb') or {}
    bb_pos = bb.get('position')
    if bb_pos is not None:
        return bb_pos

    # Fallback: compute from upper/lower/price if position not pre-stored
    upper = bb.get('upper')
    lower = bb.get('lower')
    if upper is None or lower is None:
        return None

    price = _current_price(asset_data)
    if not price:
        return None

    if price > upper:
        return 'above_upper'
    if price < lower:
        return 'below_lower'
    return 'inside'


# ---------------------------------------------------------------------------
# Ichimoku
# ---------------------------------------------------------------------------

def ichimoku_cloud_state(asset_data: dict) -> Optional[str]:
    """Strip color suffix from ichimoku.cloud_position.

    Returns:
        'above_cloud' / 'below_cloud' / 'inside_cloud' / None
    """
    pos = _cockpit(asset_data, 'ichimoku').get('cloud_position')
    if not pos:
        return None
    if pos.startswith('above_'):
        return 'above_cloud'
    if pos.startswith('below_'):
        return 'below_cloud'
    return 'inside_cloud'


# ---------------------------------------------------------------------------
# TTM Squeeze
# ---------------------------------------------------------------------------

# "just released" window — firing_up/firing_down only fire when
# the squeeze ended within this many bars. Named so the threshold is a single
# place to tune, not a magic number buried in the branch below.
TTM_RELEASE_WINDOW_BARS = 5


def ttm_squeeze_state(asset_data: dict) -> Optional[str]:
    """Combine ttm_squeeze.active + momentum + release recency into a 4-state
    vocabulary.

    Returns:
        'squeeze_on'   — currently in a squeeze
        'firing_up'    — squeeze released within the last
                         TTM_RELEASE_WINDOW_BARS bars AND momentum is bullish
        'firing_down'  — same, but momentum is bearish
        'squeeze_off'  — no active squeeze, and either no recent release or
                         momentum doesn't point either way
        None           — data absent

    firing_up/firing_down must not fire merely because there is no
    active squeeze and momentum happens to point that way — that is NOT a
    release transition (a long-dead squeeze with drifting momentum would fire
    indefinitely). They are gated on `ttm.bars_since_release` (computed by _ttm_squeeze() /
    compute_cockpit_snapshot() backward-scanning the squeeze boolean series)
    being within TTM_RELEASE_WINDOW_BARS. A missing/None bars_since_release
    (old cached payloads computed before this field existed, or a window that
    never saw a qualifying squeeze) is treated as "no recent release" — it
    never fires, safely degrading to 'squeeze_off' rather than crashing.
    """
    ttm = _cockpit(asset_data, 'ttm_squeeze')
    if not ttm:
        return None
    if ttm.get('active'):
        return 'squeeze_on'
    bars_since_release = ttm.get('bars_since_release')
    recently_released = (
        bars_since_release is not None and bars_since_release <= TTM_RELEASE_WINDOW_BARS
    )
    if recently_released:
        mom = ttm.get('momentum') or 'neutral'
        if mom.startswith('bullish'):
            return 'firing_up'
        if mom.startswith('bearish'):
            return 'firing_down'
    return 'squeeze_off'


# ---------------------------------------------------------------------------
# Stochastic RSI
# ---------------------------------------------------------------------------

def stoch_rsi_zone(asset_data: dict) -> Optional[str]:
    """Return Stoch RSI zone from cockpit_extended.

    Returns whatever the precompute stored (typically 'oversold' / 'overbought'
    / 'neutral'), or None if absent.
    """
    return _cockpit(asset_data, 'stoch_rsi').get('zone')


# ---------------------------------------------------------------------------
# Donchian Channel
# ---------------------------------------------------------------------------

def donchian_position(asset_data: dict) -> Optional[str]:
    """Classify price position relative to Donchian channel.

    Returns:
        'at_high' — price >= upper channel
        'at_low'  — price <= lower channel
        'inside'  — between the bands
        None      — data absent
    """
    don = _cockpit(asset_data, 'donchian')
    upper = don.get('upper')
    lower = don.get('lower')
    if upper is None or lower is None:
        return None

    price = _current_price(asset_data)
    if not price:
        return None

    if price >= upper:
        return 'at_high'
    if price <= lower:
        return 'at_low'
    return 'inside'


# ---------------------------------------------------------------------------
# EMA Cross
# ---------------------------------------------------------------------------

def ema_cross_state(asset_data: dict) -> Optional[str]:
    """Return sustained EMA fast/slow state: 'bullish' / 'bearish' / None."""
    return _cockpit(asset_data, 'ema_cross').get('state')


# ---------------------------------------------------------------------------
# Alligator
# ---------------------------------------------------------------------------

def alligator_state(asset_data: dict) -> Optional[str]:
    """Return Alligator state: 'eating_bullish' / 'eating_bearish' / 'sleeping' / 'mixed' / None."""
    return _cockpit(asset_data, 'alligator').get('state')


# ---------------------------------------------------------------------------
# Consensio
# ---------------------------------------------------------------------------

def consensio_state(asset_data: dict) -> Optional[str]:
    """Classify Consensio total positions into a named bucket.

    Canonical None-safe implementation — both the Alerts evaluator
    (indicator_state.py) and the Plays evaluator (conditions.py) delegate here.

    Buckets (ratio = positions / max_positions):
        >= 0.95  → full_bull
        >= 0.75  → strong_bull
        >= 0.55  → bull
        >= 0.45  → neutral
        >= 0.25  → bear
        <  0.25  → strong_bear
        missing / max_p == 0 → None  (no data — not 'strong_bear')

    None-safety rules:
      - positions is None → return None (missing data, not a bearish signal)
      - max_positions is 0 or falsy → return None (division guard)
      - non-numeric positions or max_positions → return None
    """
    cons = asset_data.get('consensio') or {}
    total = cons.get('total') or {}
    positions = total.get('positions')
    max_p = total.get('max_positions', 9)

    if positions is None:
        return None
    try:
        n = int(positions)
        # Use explicit None-check so that max_p=0 is caught by the m<=0 guard
        # below rather than silently substituted with 9. Only substitute the
        # default (9) when max_positions was truly absent (None).
        m = int(max_p) if max_p is not None else 9
    except (TypeError, ValueError):
        return None
    if m <= 0:
        return None

    ratio = n / m
    if ratio >= 0.95:
        return 'full_bull'
    if ratio >= 0.75:
        return 'strong_bull'
    if ratio >= 0.55:
        return 'bull'
    if ratio >= 0.45:
        return 'neutral'
    if ratio >= 0.25:
        return 'bear'
    return 'strong_bear'


# ---------------------------------------------------------------------------
# Structure — two output vocabularies, one input parser
# ---------------------------------------------------------------------------

def _parse_structure_trend(asset_data: dict) -> str:
    """Extract the raw structure_trend string from asset data (internal helper)."""
    struct = asset_data.get('structure') or {}
    return struct.get('structure_trend', struct.get('trend', ''))


def structure_trend(asset_data: dict) -> str:
    """Return the raw structure trend for Alerts (passthrough, default 'neutral').

    Preserves the historical Alerts behavior: the raw ``structure_trend`` field is
    returned as-is. Real data legitimately includes uptrend/downtrend/unknown as well
    as bullish/bearish/neutral, and the Alerts matcher + fingerprint expect the raw
    value — do NOT normalise here (that would change live alert evaluation).
    """
    struct = asset_data.get('structure') or {}
    return struct.get('structure_trend', struct.get('trend', 'neutral'))


def structure_pattern(asset_data: dict) -> str:
    """Return structure state in the Plays vocabulary: HH_HL / LH_LL / range.

    Plays consumers (condition-builder.js STRUCTURE state options, indicator-registry.js
    MKT_STRUCTURE states) use this 3-state vocabulary.  Returns 'range' when data is
    absent or unrecognised.
    """
    trend = _parse_structure_trend(asset_data)
    if trend in ('bullish', 'uptrend'):
        return 'HH_HL'
    if trend in ('bearish', 'downtrend'):
        return 'LH_LL'
    return 'range'


# ---------------------------------------------------------------------------
# CPR — two output granularities, one input source
# ---------------------------------------------------------------------------

_CPR_LEVEL_MAP = {
    'Below S3': 'below_s3',
    'S2-S3': 's2_s3',
    'S1-S2': 's1_s2',
    'BC-S1': 'bc_s1',
    'BC-CP': 'below_pivot',
    'CP-TC': 'above_pivot',
    'TC-R1': 'tc_r1',
    'R1-R2': 'r1_r2',
    'R2-R3': 'r2_r3',
    'Above R3': 'above_r3',
}


def cpr_zone(asset_data: dict, key: str = 'cpr') -> Optional[str]:
    """Return granular CPR zone for Alerts (10 zones: below_s3 .. above_r3).

    Reads the pre-computed price_level field from CPRCalculator and maps it to
    a snake_case key.  Returns None if the CPR data is absent.

    Alerts consumers (alert-panel.js _CPR_ZONES, INDICATOR_ALERT_CONDITIONS
    cpr_position/weekly_cpr_position/daily_cpr_position) use this vocabulary.
    """
    cpr = asset_data.get(key) or {}
    level = cpr.get('price_level')
    if not level:
        return None
    return _CPR_LEVEL_MAP.get(level)


# Plays 9-state CPR vocabulary (matches indicator-registry.js's
# CPR/WEEKLY_CPR/DAILY_CPR `states:` array exactly). price_position (above /
# below / inside) is authoritative for WHICH SIDE of the pivot the price is
# on; price_level (the same 10-band string cpr_zone() reads) only refines
# specificity WITHIN that side — it can never flip 'above' into a 'below_*'
# state or vice versa, even if the two fields were ever out of sync (in real
# precompute data they're always computed together and can't disagree).
_CPR_ABOVE_BANDS = {
    'TC-R1': 'above_pivot',
    'R1-R2': 'above_R1',
    'R2-R3': 'above_R2',
    'Above R3': 'above_R3',
}
_CPR_BELOW_BANDS = {
    'BC-S1': 'below_pivot',
    'S1-S2': 'below_S1',
    'S2-S3': 'below_S2',
    'Below S3': 'below_S3',
}
# 'BC-CP' / 'CP-TC' — inside the CPR's own BC..TC range — both collapse to
# 'between', preserving the earlier (coarse) meaning of that state exactly.

# Ordered from most-bearish to most-bullish. Used by cpr_state_satisfies() to
# check hierarchical/cumulative containment: being 'above_R2' also means the
# price is 'above_R1' and 'above_pivot' (an inequality, not an exclusive band).
_CPR_STATE_ORDER = [
    'below_S3', 'below_S2', 'below_S1', 'below_pivot',
    'between',
    'above_pivot', 'above_R1', 'above_R2', 'above_R3',
]
_CPR_PIVOT_IDX = _CPR_STATE_ORDER.index('between')


def cpr_position(asset_data: dict, key: str = 'cpr') -> str:
    """Return CPR position for Plays in the 9-state vocabulary:
    above_pivot / below_pivot / between / above_R1 / above_R2 / above_R3 /
    below_S1 / below_S2 / below_S3.

    price_position (above/below/inside, the earlier coarse field) is
    authoritative for which side of the pivot the price is on. price_level
    (the 10-band string CPRCalculator computes alongside it — the same field
    cpr_zone() reads) only refines specificity *within* that side, e.g. a
    price_position='above' + price_level='R2-R3' pair returns the most
    specific applicable state 'above_R2' (price is above R2 but below R3),
    not the coarser 'above_pivot'. If price_level doesn't recognisably refine
    the established side (e.g. it's missing, or — only possible with
    hand-built/stale data — inconsistent with price_position), the coarse
    above_pivot/below_pivot default for that side is used, so this never
    regresses below the original 3-state behaviour.

    price_position == 'inside' (or price_level in {'BC-CP', 'CP-TC'} when
    price_position itself is absent) always returns 'between', regardless of
    price_level — matching the earlier (coarse) meaning of "inside the CPR's
    own BC..TC range" exactly.

    Returns 'between' when there isn't enough data to place the price at all.

    Plays consumers (conditions.py _get_indicator_state CPR/WEEKLY_CPR/DAILY_CPR)
    use this vocabulary. IMPORTANT: a saved condition on one of the original 3
    coarse states (esp. 'above_pivot'/'below_pivot') must keep matching even
    though this function can now return a more specific state for the same
    underlying price — see cpr_state_satisfies(), which conditions.py's
    _eval_indicator_state() uses instead of a plain equality check for CPR-
    family indicators.
    """
    cpr = asset_data.get(key) or {}
    pos = cpr.get('price_position', cpr.get('position', ''))
    level = cpr.get('price_level')

    if pos == 'above':
        return _CPR_ABOVE_BANDS.get(level, 'above_pivot')
    if pos == 'below':
        return _CPR_BELOW_BANDS.get(level, 'below_pivot')
    if pos == 'inside':
        return 'between'

    # pos absent/unrecognised — fall back to price_level alone.
    if level in _CPR_ABOVE_BANDS:
        return _CPR_ABOVE_BANDS[level]
    if level in _CPR_BELOW_BANDS:
        return _CPR_BELOW_BANDS[level]
    return 'between'


def cpr_state_satisfies(actual: Optional[str], expected: str) -> bool:
    """Check whether a live cpr_position() reading satisfies a saved (possibly
    coarser) expected state — hierarchical/cumulative containment rather than
    plain equality.

    Why: the 9-state vocabulary is cumulative on each side of the pivot —
    'above_R2' implies 'above_R1' and 'above_pivot' are also true of the same
    price. A Play saved before the 9-state vocabulary (or one deliberately using the
    coarse 'above_pivot'/'below_pivot' state) must keep matching even when the
    live reading has become more specific, e.g. actual='above_R2' must still
    satisfy expected='above_pivot'. This function implements that "at least as
    far in the same direction" containment check.

    'between' is NOT part of either side's hierarchy (it isn't a "distance
    from pivot" state) and only satisfies itself.

    Returns False if either value is None or not a recognised CPR state.
    """
    if actual is None or expected is None:
        return False
    if actual == expected:
        return True
    if actual not in _CPR_STATE_ORDER or expected not in _CPR_STATE_ORDER:
        return False
    if actual == 'between' or expected == 'between':
        return False  # exact-match only; already handled by the equality check above

    actual_idx = _CPR_STATE_ORDER.index(actual)
    expected_idx = _CPR_STATE_ORDER.index(expected)

    # Both on the bullish (above-pivot) side: actual must be at least as far above.
    if actual_idx > _CPR_PIVOT_IDX and expected_idx > _CPR_PIVOT_IDX:
        return actual_idx >= expected_idx
    # Both on the bearish (below-pivot) side: actual must be at least as far below
    # (earlier in the list == further below).
    if actual_idx < _CPR_PIVOT_IDX and expected_idx < _CPR_PIVOT_IDX:
        return actual_idx <= expected_idx
    return False


# ---------------------------------------------------------------------------
# SuperTrend
# ---------------------------------------------------------------------------

def super_trend_state(asset_data: dict) -> Optional[str]:
    """Return SuperTrend direction: 'bullish' / 'bearish' / None.

    Canonical None-safe implementation.  An unknown or absent direction is
    "no signal" (None), NOT 'neutral' — alert-panel.js and indicator-registry.js
    both list only ['bullish', 'bearish'] as valid SuperTrend states; returning
    'neutral' would produce a state that no alert or play condition can ever match.

    Both the Alerts evaluator (TRACKED_STATES super_trend lambda) and the Plays
    evaluator (conditions.py SUPER_TREND branch) delegate here.
    """
    direction = _cockpit(asset_data, 'supertrend').get('direction')
    if direction == 'long':
        return 'bullish'
    if direction == 'short':
        return 'bearish'
    return None


# ---------------------------------------------------------------------------
# PSAR
# ---------------------------------------------------------------------------

def psar_state(asset_data: dict) -> Optional[str]:
    """Return Parabolic SAR direction: 'bullish' / 'bearish' / None.

    Maps cockpit_extended.psar.direction:
        'long'  → 'bullish'
        'short' → 'bearish'
        absent / other → None (no signal)

    Both the Alerts evaluator (TRACKED_STATES psar entry) and the Plays
    evaluator (conditions.py PSAR branch) delegate here.
    """
    direction = _cockpit(asset_data, 'psar').get('direction')
    if direction == 'long':
        return 'bullish'
    if direction == 'short':
        return 'bearish'
    return None


# ---------------------------------------------------------------------------
# Ichimoku TK Cross
# ---------------------------------------------------------------------------

def ichimoku_tk_state(asset_data: dict) -> Optional[str]:
    """Return Ichimoku Tenkan/Kijun cross state: 'bullish' / 'bearish' / 'none' / None.

    Reads cockpit_extended.ichimoku.tk_cross and returns it as-is.  The
    precompute stores 'bullish' / 'bearish' on the cross bar and 'none'
    between crosses; None is returned only when data is entirely absent.

    Both the Alerts evaluator (TRACKED_STATES ichimoku_tk entry) and the Plays
    evaluator (conditions.py ICHIMOKU_TK branch) delegate here.
    """
    val = _cockpit(asset_data, 'ichimoku').get('tk_cross')
    return val if val is not None else None


# ---------------------------------------------------------------------------
# ADX Regime
# ---------------------------------------------------------------------------

def adx_regime_state(asset_data: dict) -> str:
    """Return the ADX regime label from asset_data['adx']['regime'].

    Defaults to 'unknown' when adx is absent, non-dict, or regime is None —
    the historical Alerts default (preserved exactly; do NOT
    change to None, callers rely on the 'unknown' sentinel).

    Both the Alerts evaluator (TRACKED_STATES adx_regime entry) and the Plays
    evaluator (conditions.py ADX_REGIME branch) delegate here.
    """
    adx = asset_data.get('adx')
    if not isinstance(adx, dict):
        return 'unknown'
    regime = adx.get('regime')
    return regime if regime is not None else 'unknown'


# ---------------------------------------------------------------------------
# Tier 1 — sidebar LIVE signal vocabulary (promoted here)
#
# These read score_b.alerts (consensio/scoring/balanced.py's compound-signal
# alerts list) and trend_analysis.patterns (consensio/analysis/trend.py's
# TrendAnalyzer). Both fields are already carried on the precompute asset
# dict — no new upstream computation, just new state extractors.
#
# None-safety: unlike some indicators above, these never return Python None —
# an absent/None score_b or trend_analysis (or an alert simply not firing)
# resolves to the explicit string 'none', mirroring the ichimoku_tk /
# bb_squeeze convention of using 'none' as a real vocabulary state rather
# than null. This matters for indicator_state.detect_transitions: see the
# first-cycle-storm note there.
# ---------------------------------------------------------------------------

def _score_b_alert(asset_data: dict, alert_name: str) -> bool:
    """True if alert_name is present in score_b.alerts (None-safe).

    Missing score_b, missing/non-list alerts -> False.
    """
    score_b = asset_data.get('score_b')
    alerts = score_b.get('alerts') if isinstance(score_b, dict) else None
    if not isinstance(alerts, list):
        return False
    return alert_name in alerts


def _trend_patterns(asset_data: dict) -> list:
    """Return trend_analysis.patterns list (None-safe). Missing/non-list -> []."""
    trend_analysis = asset_data.get('trend_analysis')
    patterns = trend_analysis.get('patterns') if isinstance(trend_analysis, dict) else None
    return patterns if isinstance(patterns, list) else []


def ema_gap_squeeze_state(asset_data: dict) -> str:
    """EMA Ribbon convergence squeeze from score_b.alerts: 'squeeze' / 'none'.

    DISTINCT from the existing `bb_squeeze_state` (Bollinger Band width
    squeeze) — this is the EMA-gap squeeze flagged by
    consensio/scoring/balanced.py (ema_ribbon.is_squeeze), a different
    indicator entirely. Named ema_gap_squeeze so the two can't be confused.

    Both the Alerts evaluator (TRACKED_STATES ema_gap_squeeze entry) and the
    Plays evaluator (conditions.py EMA_GAP_SQUEEZE branch) delegate here.
    """
    return 'squeeze' if _score_b_alert(asset_data, 'ema_squeeze') else 'none'


def v_cpr_state(asset_data: dict) -> str:
    """V-CPR (CPR vacuum) alert from score_b.alerts: 'vacuum' / 'none'.

    Both the Alerts evaluator (TRACKED_STATES v_cpr entry) and the Plays
    evaluator (conditions.py V_CPR branch) delegate here.
    """
    return 'vacuum' if _score_b_alert(asset_data, 'v_cpr') else 'none'


def cpr_extreme_state(asset_data: dict) -> str:
    """CPR extreme zone from score_b.alerts: 'near_s3' / 'near_r3' / 'none'.

    balanced.py appends at most one of near_s3/near_r3 per cycle (they are
    mutually exclusive price zones), so no further tie-break is needed.

    Both the Alerts evaluator (TRACKED_STATES cpr_extreme entry) and the
    Plays evaluator (conditions.py CPR_EXTREME branch) delegate here.
    """
    if _score_b_alert(asset_data, 'near_s3'):
        return 'near_s3'
    if _score_b_alert(asset_data, 'near_r3'):
        return 'near_r3'
    return 'none'


def structure_disagreement_state(asset_data: dict) -> str:
    """Ribbon-vs-Structure conflict from score_b.alerts: 'bearish'/'bullish'/'none'.

    'structure_bearish' = EMA Ribbon bullish but Structure bearish (STR-down warning).
    'structure_bullish' = EMA Ribbon bearish but Structure bullish (STR-up warning).
    Mutually exclusive by construction in balanced.py. DISTINCT from the
    existing `structure_trend`/`structure_pattern` tracked states (raw
    structure_trend passthrough) — this flags disagreement BETWEEN two
    indicators, not the structure itself.

    Both the Alerts evaluator (TRACKED_STATES structure_disagreement entry)
    and the Plays evaluator (conditions.py STRUCTURE_DISAGREEMENT branch)
    delegate here.
    """
    if _score_b_alert(asset_data, 'structure_bearish'):
        return 'bearish'
    if _score_b_alert(asset_data, 'structure_bullish'):
        return 'bullish'
    return 'none'


def tf_divergence_state(asset_data: dict) -> str:
    """1D-vs-1W consensio conflict from trend_analysis.patterns: 'active'/'none'.

    Both the Alerts evaluator (TRACKED_STATES tf_divergence entry) and the
    Plays evaluator (conditions.py TF_DIVERGENCE branch) delegate here.
    """
    return 'active' if 'divergence' in _trend_patterns(asset_data) else 'none'


def rsi_divergence_pattern_state(asset_data: dict) -> str:
    """RSI divergence flavor from trend_analysis.patterns.

    States: 'bull' / 'bear' / 'hidden_bull' / 'hidden_bear' / 'none'.

    trend_analysis.patterns is direction-exclusive by construction (Trend-
    Analyzer emits either its bullish_patterns or bearish_patterns list,
    whichever score wins — never both), so rsi_bull_div and rsi_bear_div
    cannot co-occur in practice. Tie-break, documented in case that
    assumption ever changes: regular divergence (reversal signal) outranks
    hidden divergence (continuation signal); within a tier, bull is checked
    before bear (arbitrary but deterministic).

    Both the Alerts evaluator (TRACKED_STATES rsi_divergence entry) and the
    Plays evaluator (conditions.py RSI_DIVERGENCE branch) delegate here.
    This is DISTINCT from the pre-existing `indicator_divergence` condition
    type in conditions.py, which reads the separate asset_data['rsi_divergence']
    dict field (daily/weekly divergence_type) — do not confuse the two.
    """
    patterns = _trend_patterns(asset_data)
    if 'rsi_bull_div' in patterns:
        return 'bull'
    if 'rsi_bear_div' in patterns:
        return 'bear'
    if 'rsi_hidden_bull_div' in patterns:
        return 'hidden_bull'
    if 'rsi_hidden_bear_div' in patterns:
        return 'hidden_bear'
    return 'none'


def perp_basis_state(asset_data: dict) -> str:
    """Perp futures basis (markPrice vs indexPrice) regime.

    States: 'steep_contango' (basis_pct >= +0.30) / 'backwardation'
    (basis_pct <= -0.10) / 'normal' (in between) / 'none' (no data).

    Reads asset_data['perp_basis_pct'], set in precompute.py alongside
    funding_rate (crypto-only, None when markPrice/indexPrice weren't both
    available). Missing data resolves to the explicit 'none' state — NOT
    'normal' — so a data gap never masquerades as a calm/no-signal market.
    This mirrors the ema_gap_squeeze/tf_divergence convention where 'none'
    is real vocabulary (no signal / no data), distinct from an in-range
    reading.

    Both the Alerts evaluator (TRACKED_STATES perp_basis entry) and the
    Plays evaluator (conditions.py PERP_BASIS branch) delegate here.
    """
    basis_pct = asset_data.get('perp_basis_pct')
    if basis_pct is None:
        return 'none'
    try:
        basis_pct = float(basis_pct)
    except (TypeError, ValueError):
        return 'none'
    if basis_pct >= 0.30:
        return 'steep_contango'
    if basis_pct <= -0.10:
        return 'backwardation'
    return 'normal'
