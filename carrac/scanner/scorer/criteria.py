"""Scoring components: trend / momentum / RSI / structure / level / macro.

Each criterion scores 0-2, total 0-12 across: Trend (Consensio), Momentum
(EMA Ribbon+MACD), RSI, Structure, Level, Macro.
"""

VERDICT_THRESHOLDS = [
    (11, 'STRONG'),
    (8, 'GOOD'),
    (6, 'NEUTRAL'),
    (3, 'WEAK'),
    (0, 'AVOID'),
]

def _verdict(total):
    for threshold, label in VERDICT_THRESHOLDS:
        if total >= threshold:
            return label
    return 'AVOID'


def _score_trend(asset, direction):
    """Consensio multi-TF alignment (0-2)."""
    consensio = asset.get('consensio') or {}
    signals = {}
    for tf in ('1D', '3D', '1W'):
        tf_data = consensio.get(tf) or {}
        signals[tf] = tf_data.get('signal', 0)

    # For LONG: positive signal confirms. For SHORT: negative confirms.
    if direction == 'long':
        confirms = {tf: s > 0 for tf, s in signals.items()}
        opposes = {tf: s < 0 for tf, s in signals.items()}
    else:
        confirms = {tf: s < 0 for tf, s in signals.items()}
        opposes = {tf: s > 0 for tf, s in signals.items()}

    all_confirm = all(confirms.values())
    entry_confirms = confirms.get('1D', False)
    htf_confirm_count = sum(1 for tf in ('3D', '1W') if confirms.get(tf))
    htf_oppose_count = sum(1 for tf in ('3D', '1W') if opposes.get(tf))

    if all_confirm:
        score = 2
    elif entry_confirms and htf_confirm_count >= 1:
        score = 1
    else:
        score = 0

    # Build evidence string
    parts = [f"{tf}={signals[tf]:+d}" for tf in ('1D', '3D', '1W')]
    evidence = f"Consensio {', '.join(parts)}"

    # Track unanimous opposition for veto
    all_opposed = htf_oppose_count == 2 and opposes.get('1D', False)

    return score, evidence, {'all_opposed': all_opposed}


def _score_momentum(asset, direction):
    """EMA Ribbon + MACD alignment (0-2)."""
    ema_ribbon = asset.get('ema_ribbon') or {}
    macd = asset.get('macd') or {}

    ema_ribbon_trend = ema_ribbon.get('trend', 'neutral')
    macd_hist = macd.get('histogram', 0)

    if direction == 'long':
        ema_ribbon_confirms = ema_ribbon_trend in ('bull', 'bullish')
        macd_confirms = macd_hist > 0
    else:
        ema_ribbon_confirms = ema_ribbon_trend in ('bear', 'bearish')
        macd_confirms = macd_hist < 0

    confirms = int(ema_ribbon_confirms) + int(macd_confirms)
    score = min(confirms, 2)

    parts = []
    _ema_desc = ('EMAs bullish' if ema_ribbon_trend in ('bull', 'bullish')
                 else 'EMAs bearish' if ema_ribbon_trend in ('bear', 'bearish')
                 else 'EMAs neutral')
    parts.append(_ema_desc)
    if macd_hist != 0:
        parts.append(f"MACD {'positive' if macd_hist > 0 else 'negative'} ({macd_hist:+.1f})")
    else:
        parts.append("MACD flat")

    return score, ', '.join(parts), {}


def _score_rsi(asset, direction):
    """Weekly RSI positioning (0-2)."""
    wrsi = asset.get('weekly_rsi')
    if wrsi is None:
        return 1, 'Weekly RSI N/A', {'exhaustion': False}

    div_data = asset.get('rsi_divergence') or {}
    daily_div = div_data.get('daily') or {}
    weekly_div = div_data.get('weekly') or {}

    # Check for divergences opposing the direction
    has_opposing_div = False
    if direction == 'long':
        has_opposing_div = (
            daily_div.get('divergence_type') == 'bearish' or
            weekly_div.get('divergence_type') == 'bearish'
        )
    else:
        has_opposing_div = (
            daily_div.get('divergence_type') == 'bullish' or
            weekly_div.get('divergence_type') == 'bullish'
        )

    exhaustion = False

    if direction == 'long':
        if wrsi > 80:
            exhaustion = True
        favorable = 40 <= wrsi <= 65
        against = wrsi > 70
    else:
        if wrsi < 20:
            exhaustion = True
        favorable = 35 <= wrsi <= 60
        against = wrsi < 30

    if favorable and not has_opposing_div:
        score = 2
    elif against or has_opposing_div:
        score = 0
    else:
        score = 1

    parts = [f"wRSI {wrsi:.1f}"]
    if has_opposing_div:
        parts.append("divergence opposing")
    if exhaustion:
        parts.append("EXHAUSTION")

    return score, ', '.join(parts), {'exhaustion': exhaustion}


def _score_structure(asset, direction):
    """Swing structure analysis (0-2)."""
    struct = asset.get('structure') or {}
    high_type = struct.get('last_high_type', '')
    low_type = struct.get('last_low_type', '')

    if direction == 'long':
        high_confirms = high_type == 'HH'
        low_confirms = low_type == 'HL'
    else:
        high_confirms = high_type == 'LH'
        low_confirms = low_type == 'LL'

    confirms = int(high_confirms) + int(low_confirms)
    score = min(confirms, 2)

    trend = struct.get('structure_trend', 'unknown')
    evidence = f"Structure {trend} ({high_type}/{low_type})"

    return score, evidence, {}


def _score_level(asset, direction):
    """Proximity to key S/R levels (0-2).

    Also detects extension: shorting near S2/S3 or buying near R2/R3
    is penalized because the move is already extended.
    """
    price = asset.get('latest_price', 0)
    if not price:
        return 1, 'No price data', {}

    cpr = asset.get('cpr') or {}
    bb = asset.get('bb') or {}

    # Collect all key levels (include S3/R3)
    # Human-readable names for evidence text
    LEVEL_NAMES = {
        'pivot': 'Pivot', 'tc': 'Top Central', 'bc': 'Bottom Central',
        's1': 'S1', 's2': 'S2', 's3': 'S3', 'r1': 'R1', 'r2': 'R2', 'r3': 'R3',
        'upper': 'BB Upper', 'lower': 'BB Lower', 'middle': 'BB Middle',
    }
    levels = {}
    for key in ('pivot', 'tc', 'bc', 's1', 's2', 's3', 'r1', 'r2', 'r3'):
        val = cpr.get(key)
        if val:
            levels[LEVEL_NAMES[key]] = float(val)

    for key in ('upper', 'lower', 'middle'):
        val = bb.get(key)
        if val:
            levels[LEVEL_NAMES[key]] = float(val)

    if not levels:
        return 1, 'No level data', {}

    # Count favorable levels within proximity
    favorable_count = 0
    nearby_levels = []
    threshold_tight = 0.02  # 2%
    threshold_loose = 0.025  # 2.5%

    for name, level in levels.items():
        dist_pct = (price - level) / price if price else 0

        if direction == 'long':
            # Support below price is favorable
            is_favorable = level < price
        else:
            # Resistance above price is favorable
            is_favorable = level > price

        if is_favorable and abs(dist_pct) <= threshold_tight:
            favorable_count += 1
            nearby_levels.append(f"{name} ({dist_pct:+.1%})")
        elif is_favorable and abs(dist_pct) <= threshold_loose:
            nearby_levels.append(f"{name} ({dist_pct:+.1%})")

    if favorable_count >= 2:
        score = 2
    elif favorable_count >= 1 or len(nearby_levels) >= 1:
        score = 1
    else:
        score = 0

    # Extension penalty: price already at extreme levels opposing the trade
    price_level = cpr.get('price_level', '')
    extended = False
    if direction == 'short' and price_level in ('S2-S3', 'Below S3'):
        score = max(score - 1, 0)
        extended = True
    elif direction == 'long' and price_level in ('R2-R3', 'Above R3'):
        score = max(score - 1, 0)
        extended = True

    if nearby_levels:
        evidence = f"{len(nearby_levels)} level(s) nearby: {', '.join(nearby_levels[:3])}"
    else:
        evidence = "No nearby levels"
    if extended:
        evidence += f" [EXTENDED: {price_level}]"

    return score, evidence, {'extended': extended}


def _score_macro(macro, direction):
    """Market regime + sentiment (0-2)."""
    # `available` defaults True so hand-built macro dicts (tests, older
    # callers that don't set the flag) keep scoring normally. Only an
    # EXPLICIT `available: False` (set by _build_macro_data() when the
    # regime is genuinely unobtainable) takes this branch — never treat a
    # missing flag as "unavailable".
    if not macro.get('available', True):
        return 0, 'Regime: unavailable (macro data missing — not scored, not neutral)', {
            'hard_zero_long': False,
            'hard_zero_short': False,
        }

    regime = macro.get('regime', 'Unknown')
    fg = macro.get('fear_greed', 50)
    score_val = macro.get('regime_score', 0)

    if direction == 'long':
        if regime in ('Risk-On', 'Cautious') and score_val >= 2:
            score = 2
        elif regime == 'Neutral' or (regime in ('Risk-On', 'Cautious') and score_val < 2):
            score = 1
        else:
            score = 0
    else:  # short
        if regime in ('Risk-Off', 'Crisis') and score_val <= -2:
            score = 2
        elif regime == 'Neutral' or (regime in ('Risk-Off', 'Crisis') and score_val > -2):
            score = 1
        else:
            score = 0

    evidence = f"Regime: {regime} ({score_val:+d}), F&G: {fg}"

    # Track hard veto conditions
    veto_info = {
        'hard_zero_long': regime in ('Risk-Off', 'Crisis') and fg is not None and fg < 20,
        'hard_zero_short': regime in ('Risk-On',) and fg is not None and fg > 80,
    }

    return score, evidence, veto_info


# ---------------------------------------------------------------------------
# Score MODIFIERS — NOT criteria.
#
# These do not add a 7th criterion, do not change the 0-12 scale, and do not
# change the criteria count. They nudge the already-computed 0-12 `total`
# by at most ±1 per modifier, applied in
# scorer/engine.py AFTER vetoes and BEFORE verdict classification, so the
# verdict reflects them. Each fires only when its data source is present on
# the asset — assets lacking the data (non-crypto, non-covered) score exactly
# as before (no modifier, no evidence, no exception).
# ---------------------------------------------------------------------------

# Funding-rate contrarian thresholds — mirrors carrac/sentiment/routes.py
# (_get_funding_rate_color): funding_rate arrives as a raw fraction (e.g.
# Binance lastFundingRate ~0.0001 == 0.01%/8h), so we convert to a percent
# before comparing against the ±0.05% band used there.
FUNDING_CROWDED_PCT = 0.05

# Smart-money (Hyperliquid) bias thresholds — bias is a 0-100 oscillator,
# 50 = balanced. >=70 crowd is long-skewed, <=30 crowd is short-skewed.
SMART_MONEY_LONG_SKEW = 70
SMART_MONEY_SHORT_SKEW = 30


def _funding_modifier(funding_rate, direction):
    """Contrarian funding-rate modifier: crowded positioning fades the crowd.

    funding_rate is a raw fraction (e.g. 0.0006 == 0.06%/8h) or None.
    Returns (delta, evidence) where delta is -1/0/+1 and evidence is a
    string (or None when no modifier applies).
    """
    if funding_rate is None:
        return 0, None
    try:
        rate = float(funding_rate)
    except (TypeError, ValueError):
        return 0, None

    rate_pct = rate * 100

    if rate_pct >= FUNDING_CROWDED_PCT:
        skew = 'crowded longs'
        delta = -1 if direction == 'long' else 1
    elif rate_pct <= -FUNDING_CROWDED_PCT:
        skew = 'crowded shorts'
        delta = 1 if direction == 'long' else -1
    else:
        return 0, None

    evidence = f"Funding {rate_pct:+.3f}%/8h — {skew} (score {delta:+d})"
    return delta, evidence


def _smart_money_modifier(smart_money_bias, direction):
    """Hyperliquid smart-money-bias modifier: follow the smart-money skew.

    smart_money_bias is a 0-100 float (50 = balanced) or None.
    Returns (delta, evidence) where delta is -1/0/+1 and evidence is a
    string (or None when no modifier applies).
    """
    if smart_money_bias is None:
        return 0, None
    try:
        bias = float(smart_money_bias)
    except (TypeError, ValueError):
        return 0, None

    if bias >= SMART_MONEY_LONG_SKEW:
        skew = 'long-skewed'
        delta = 1 if direction == 'long' else -1
    elif bias <= SMART_MONEY_SHORT_SKEW:
        skew = 'short-skewed'
        delta = -1 if direction == 'long' else 1
    else:
        return 0, None

    evidence = f"Smart money {bias:.0f} {skew} ({delta:+d})"
    return delta, evidence


