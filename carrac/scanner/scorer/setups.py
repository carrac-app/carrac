"""Setup detection + classifier."""

import logging

logger = logging.getLogger('carrac.scanner.scorer')

SETUP_STRATEGIES = {
    'trend_continuation': "Trend aligned across timeframes; invalidation sits below the last structure low.",
    'pullback': "Retracement into support inside a trend; invalidation sits below the pullback low.",
    'breakout': "Price leaving a range on volume; invalidation sits back inside the range.",
    'reversal': "Counter-trend move from an extreme; invalidation sits beyond the extreme, and these fail more often.",
    'mean_reversion': "Price stretched from its mean; the mean is the reference level, invalidation sits beyond the stretch.",
    'range': "Price inside a defined range; the boundaries and the midpoint are the reference levels.",
}


def _detect_trend_continuation(asset, direction):
    """HH/HL or LH/LL + Consensio multi-TF aligned."""
    evidence = []
    confidence = 0.0
    readiness = 'watch'

    consensio = asset.get('consensio') or {}
    structure = asset.get('structure') or {}
    macd = asset.get('macd') or {}
    adx = asset.get('adx') or {}
    ema_ribbon = asset.get('ema_ribbon') or {}
    div_data = asset.get('rsi_divergence') or {}

    # MUST: structure aligned
    high_type = structure.get('last_high_type', '')
    low_type = structure.get('last_low_type', '')
    if direction == 'long':
        struct_aligned = high_type == 'HH' and low_type == 'HL'
    else:
        struct_aligned = high_type == 'LH' and low_type == 'LL'

    if not struct_aligned:
        return 0, [], 'watch', ''

    confidence += 0.25
    struct_desc = 'Higher Highs / Higher Lows' if direction == 'long' else 'Lower Highs / Lower Lows'
    evidence.append(struct_desc)

    # MUST: Consensio 1D confirms
    sig_1d = (consensio.get('1D') or {}).get('signal', 0)
    if (direction == 'long' and sig_1d > 0) or (direction == 'short' and sig_1d < 0):
        confidence += 0.15
        _dir_word = 'bullish' if sig_1d > 0 else 'bearish'
        evidence.append(f"Daily trend {_dir_word} ({sig_1d:+d})")
    else:
        return 0, [], 'watch', ''

    # MUST: at least one HTF confirms
    sig_3d = (consensio.get('3D') or {}).get('signal', 0)
    sig_1w = (consensio.get('1W') or {}).get('signal', 0)
    htf_confirms = 0
    if (direction == 'long' and sig_3d > 0) or (direction == 'short' and sig_3d < 0):
        htf_confirms += 1
    if (direction == 'long' and sig_1w > 0) or (direction == 'short' and sig_1w < 0):
        htf_confirms += 1
    if htf_confirms == 0:
        return 0, [], 'watch', ''

    confidence += 0.1 * htf_confirms
    evidence.append(f"Higher timeframes aligned ({htf_confirms}/2)")
    readiness = 'ready'

    # SHOULD: MACD expansion
    momentum = macd.get('momentum', '')
    if (direction == 'long' and momentum == 'bullish_expansion') or \
       (direction == 'short' and momentum == 'bearish_expansion'):
        confidence += 0.1
        evidence.append("Momentum expanding")

    # SHOULD: ADX > 20
    adx_val = adx.get('adx', 0) if isinstance(adx, dict) else 0
    if adx_val and adx_val > 20:
        confidence += 0.1
        evidence.append(f"Strong trend (ADX {adx_val:.0f})")

    # SHOULD: EMA Ribbon matches
    ema_ribbon_trend = ema_ribbon.get('trend', 'neutral')
    if (direction == 'long' and ema_ribbon_trend == 'bullish') or \
       (direction == 'short' and ema_ribbon_trend == 'bearish'):
        confidence += 0.1
        evidence.append(f"EMAs {ema_ribbon_trend}")

    # BONUS: hidden divergence confirming trend
    daily_div = (div_data.get('daily') or {}).get('divergence_type')
    if (direction == 'long' and daily_div == 'hidden_bullish') or \
       (direction == 'short' and daily_div == 'hidden_bearish'):
        confidence += 0.05
        evidence.append("Hidden divergence confirms")

    invalidation = f"Structure break ({'Lower Low' if direction == 'long' else 'Higher High'})"
    return min(confidence, 1.0), evidence, readiness, invalidation


def _detect_pullback(asset, direction):
    """HTF trend intact, 1D opposed, price at S/R."""
    evidence = []
    confidence = 0.0
    readiness = 'watch'

    consensio = asset.get('consensio') or {}
    ema_ribbon = asset.get('ema_ribbon') or {}
    bb = asset.get('bb') or {}
    cpr = asset.get('cpr') or {}
    macd = asset.get('macd') or {}
    div_data = asset.get('rsi_divergence') or {}
    price = asset.get('latest_price', 0)

    # MUST: HTF Consensio intact (3D or 1W confirms direction)
    sig_3d = (consensio.get('3D') or {}).get('signal', 0)
    sig_1w = (consensio.get('1W') or {}).get('signal', 0)
    htf_confirms = False
    if direction == 'long':
        htf_confirms = sig_3d > 0 or sig_1w > 0
    else:
        htf_confirms = sig_3d < 0 or sig_1w < 0

    if not htf_confirms:
        return 0, [], 'watch', ''

    # MUST: 1D opposed (pulling back against HTF trend)
    sig_1d = (consensio.get('1D') or {}).get('signal', 0)
    if direction == 'long':
        daily_opposed = sig_1d < 0
    else:
        daily_opposed = sig_1d > 0

    if not daily_opposed:
        return 0, [], 'watch', ''

    confidence += 0.3
    evidence.append(f"Higher TFs intact, daily pulling back ({sig_1d:+d})")

    # SHOULD: price near key level (within 2%)
    near_level = False
    if price and price > 0:
        slow_ema = ema_ribbon.get('slow_ema', 0)
        if slow_ema and abs(price - slow_ema) / price < 0.02:
            near_level = True
            confidence += 0.15
            evidence.append("Price near slow EMA")

        bb_mid = bb.get('middle', 0)
        if bb_mid and abs(price - bb_mid) / price < 0.02:
            near_level = True
            confidence += 0.1
            evidence.append("Price near BB middle")

        pivot = cpr.get('pivot', 0)
        if pivot and abs(price - float(pivot)) / price < 0.02:
            near_level = True
            confidence += 0.1
            evidence.append("Price near pivot")

    if near_level:
        readiness = 'ready'
    else:
        confidence += 0.05
        evidence.append("Approaching key levels")

    # SHOULD: MACD contracting
    momentum = macd.get('momentum', '')
    if (direction == 'long' and 'contraction' in momentum) or \
       (direction == 'short' and 'contraction' in momentum):
        confidence += 0.1
        evidence.append("Momentum fading")

    # BONUS: hidden divergence
    daily_div = (div_data.get('daily') or {}).get('divergence_type')
    if (direction == 'long' and daily_div == 'hidden_bullish') or \
       (direction == 'short' and daily_div == 'hidden_bearish'):
        confidence += 0.05
        evidence.append("Hidden divergence confirms")

    invalidation = f"Higher TF trend flips ({'3D/1W turn bearish' if direction == 'long' else '3D/1W turn bullish'})"
    return min(confidence, 1.0), evidence, readiness, invalidation


def _detect_breakout(asset, direction):
    """BB squeeze + EMA Ribbon squeeze + structure flip."""
    evidence = []
    confidence = 0.0
    readiness = 'watch'

    bb = asset.get('bb') or {}
    ema_ribbon = asset.get('ema_ribbon') or {}
    structure = asset.get('structure') or {}
    adx = asset.get('adx') or {}
    volume = asset.get('volume_metrics') or {}
    cpr_trend = asset.get('cpr_trend') or {}

    # MUST: BB squeeze (squeeze_pct < 70)
    squeeze_pct = bb.get('squeeze_pct', 100)
    if squeeze_pct >= 70:
        return 0, [], 'watch', ''

    confidence += 0.2
    evidence.append(f"Bollinger squeeze ({squeeze_pct:.0f}%)")

    # SHOULD: EMA Ribbon squeeze
    if ema_ribbon.get('is_squeeze'):
        confidence += 0.15
        evidence.append("EMAs compressing")

    # SHOULD: structure flip
    trend = structure.get('structure_trend', 'neutral')
    if (direction == 'long' and trend == 'bullish') or \
       (direction == 'short' and trend == 'bearish'):
        confidence += 0.15
        evidence.append(f"Structure turning {trend}")

    # SHOULD: ADX rising (from low base)
    adx_val = adx.get('adx', 0) if isinstance(adx, dict) else 0
    if adx_val and 15 <= adx_val <= 30:
        confidence += 0.1
        evidence.append(f"Trend building (ADX {adx_val:.0f})")

    # SHOULD: volume surge
    vol_ratio = None
    if volume.get('avg_volume') and volume.get('latest_volume'):
        avg_v = volume['avg_volume']
        if avg_v > 0:
            vol_ratio = volume['latest_volume'] / avg_v
    if vol_ratio and vol_ratio > 1.5:
        confidence += 0.15
        evidence.append(f"Volume {vol_ratio:.1f}x average")
        readiness = 'ready'

    # BONUS: CPR trend divergence (pivots falling + price breaking up = bullish)
    cpr_dir = cpr_trend.get('direction', '')
    if (direction == 'long' and cpr_dir == 'falling') or \
       (direction == 'short' and cpr_dir == 'rising'):
        confidence += 0.05
        evidence.append(f"Pivots diverge from price ({cpr_dir})")

    # If BB is in deep squeeze and volume is surging, it's ready
    if squeeze_pct < 50 and vol_ratio and vol_ratio > 1.3:
        readiness = 'ready'

    invalidation = "Fails to break range, volume fades"
    return min(confidence, 1.0), evidence, readiness, invalidation


def _detect_reversal(asset, direction):
    """Regular RSI divergence + structure flip + extreme level."""
    evidence = []
    confidence = 0.0
    readiness = 'watch'

    div_data = asset.get('rsi_divergence') or {}
    structure = asset.get('structure') or {}
    cpr = asset.get('cpr') or {}
    adx = asset.get('adx') or {}
    ema_ribbon = asset.get('ema_ribbon') or {}

    # SHOULD: regular RSI divergence
    daily_div = (div_data.get('daily') or {}).get('divergence_type')
    weekly_div = (div_data.get('weekly') or {}).get('divergence_type')
    has_div = False
    if direction == 'long' and daily_div == 'bullish':
        has_div = True
        confidence += 0.25
        evidence.append("Daily bullish divergence")
    elif direction == 'long' and weekly_div == 'bullish':
        has_div = True
        confidence += 0.3
        evidence.append("Weekly bullish divergence")
    elif direction == 'short' and daily_div == 'bearish':
        has_div = True
        confidence += 0.25
        evidence.append("Daily bearish divergence")
    elif direction == 'short' and weekly_div == 'bearish':
        has_div = True
        confidence += 0.3
        evidence.append("Weekly bearish divergence")

    # SHOULD: structure flip
    trend = structure.get('structure_trend', 'neutral')
    if (direction == 'long' and trend == 'bullish') or \
       (direction == 'short' and trend == 'bearish'):
        confidence += 0.2
        evidence.append(f"Structure flipping {trend}")
        if has_div:
            readiness = 'ready'

    # SHOULD: extreme CPR level
    price_level = cpr.get('price_level', '')
    if direction == 'long' and price_level in ('S2-S3', 'Below S3'):
        confidence += 0.15
        evidence.append(f"At extreme support ({price_level})")
    elif direction == 'short' and price_level in ('R2-R3', 'Above R3'):
        confidence += 0.15
        evidence.append(f"At extreme resistance ({price_level})")

    # SHOULD: ADX declining (trend weakening)
    adx_val = adx.get('adx', 0) if isinstance(adx, dict) else 0
    if adx_val and adx_val > 25:
        confidence += 0.1
        evidence.append(f"Trend weakening (ADX {adx_val:.0f})")

    # SHOULD: EMA Ribbon flip
    ema_ribbon_trend = ema_ribbon.get('trend', 'neutral')
    if (direction == 'long' and ema_ribbon_trend == 'bullish') or \
       (direction == 'short' and ema_ribbon_trend == 'bearish'):
        confidence += 0.1
        evidence.append(f"EMAs turning {ema_ribbon_trend}")

    # Minimum threshold
    if confidence < 0.2:
        return 0, [], 'watch', ''

    invalidation = f"New {'lower low' if direction == 'long' else 'higher high'} beyond extreme"
    return min(confidence, 1.0), evidence, readiness, invalidation


def _detect_mean_reversion(asset, direction):
    """Price overextended from EMA Ribbon/BB/CPR mean."""
    evidence = []
    confidence = 0.0

    ema_ribbon = asset.get('ema_ribbon') or {}
    bb = asset.get('bb') or {}
    cpr = asset.get('cpr') or {}
    price = asset.get('latest_price', 0)

    if not price or price <= 0:
        return 0, [], 'watch', ''

    targets = []
    overextended = False

    # Check EMA Ribbon gap
    ema_gap = ema_ribbon.get('ema_gap_pct', 0)
    if ema_gap and ema_gap > 3:
        slow_ema = ema_ribbon.get('slow_ema', 0)
        # For long mean reversion: price below mean (gap is always positive)
        # Need to check direction vs price position
        fast_ema = ema_ribbon.get('fast_ema', 0)
        if direction == 'long' and fast_ema and fast_ema < slow_ema:
            overextended = True
            confidence += 0.25
            evidence.append(f"EMAs {ema_gap:.1f}% apart (stretched below)")
            if slow_ema:
                targets.append(('EMA Ribbon mean', slow_ema))
        elif direction == 'short' and fast_ema and fast_ema > slow_ema:
            overextended = True
            confidence += 0.25
            evidence.append(f"EMAs {ema_gap:.1f}% apart (stretched above)")
            if slow_ema:
                targets.append(('EMA Ribbon mean', slow_ema))

    # Check BB distance
    bb_upper = bb.get('upper', 0)
    bb_lower = bb.get('lower', 0)
    bb_mid = bb.get('middle', 0)
    if bb_upper and bb_lower and bb_mid and price:
        bb_range = bb_upper - bb_lower
        if bb_range > 0:
            if direction == 'long':
                dist_from_lower = (price - bb_lower) / bb_range
                if dist_from_lower < 0.1:  # near bottom of BB
                    overextended = True
                    confidence += 0.2
                    evidence.append("Price at Bollinger Band floor")
                    targets.append(('BB middle', bb_mid))
            else:
                dist_from_upper = (bb_upper - price) / bb_range
                if dist_from_upper < 0.1:  # near top of BB
                    overextended = True
                    confidence += 0.2
                    evidence.append("Price at Bollinger Band ceiling")
                    targets.append(('BB middle', bb_mid))

    # Check CPR pivot distance
    pivot = cpr.get('pivot', 0)
    if pivot:
        pivot_f = float(pivot)
        pivot_dist = abs(price - pivot_f) / price
        if pivot_dist > 0.05:
            price_level = cpr.get('price_level', '')
            if (direction == 'long' and 'S' in price_level) or \
               (direction == 'short' and 'R' in price_level):
                overextended = True
                confidence += 0.15
                evidence.append(f"Far from pivot ({pivot_dist:.1%})")
                targets.append(('CPR pivot', pivot_f))

    # MUST: at least one overextension signal
    if not overextended:
        return 0, [], 'watch', ''

    # BONUS: V-CPR (volatile CPR — wide range)
    cpr_type = cpr.get('cpr_type', '')
    if cpr_type == 'V-CPR':
        confidence += 0.05
        evidence.append("V-CPR (volatile)")

    # Mean reversion is always "ready" — entry is now
    readiness = 'ready'

    # Build invalidation from targets
    if targets:
        target_str = ', '.join(f"{n} @{v:.2f}" for n, v in targets[:2])
        invalidation = f"Targets: {target_str}. Invalidated if new extreme."
    else:
        invalidation = "Invalidated if move extends further."

    return min(confidence, 1.0), evidence, readiness, invalidation


def _detect_range(asset, direction):
    """ADX<25 + neutral structure + price at BB boundary."""
    evidence = []
    confidence = 0.0
    readiness = 'watch'

    adx = asset.get('adx') or {}
    structure = asset.get('structure') or {}
    bb = asset.get('bb') or {}
    price = asset.get('latest_price', 0)

    # MUST: ADX < 25
    adx_val = adx.get('adx', 0) if isinstance(adx, dict) else 0
    if not adx_val or adx_val >= 25:
        return 0, [], 'watch', ''

    confidence += 0.2
    evidence.append(f"No trend (ADX {adx_val:.0f})")

    # MUST: structure neutral or mixed
    trend = structure.get('structure_trend', 'neutral')
    if trend not in ('neutral', 'mixed', 'unknown'):
        confidence -= 0.05

    _trend_label = {'neutral': 'Choppy structure', 'mixed': 'Mixed structure'}.get(trend, f'Structure {trend}')
    evidence.append(_trend_label)

    # SHOULD: price near BB boundary
    bb_upper = bb.get('upper', 0)
    bb_lower = bb.get('lower', 0)
    if bb_upper and bb_lower and price and (bb_upper - bb_lower) > 0:
        bb_range = bb_upper - bb_lower
        position_in_bb = (price - bb_lower) / bb_range  # 0=lower, 1=upper

        if direction == 'long' and position_in_bb < 0.25:
            confidence += 0.25
            evidence.append("Price near range bottom")
            readiness = 'ready'
        elif direction == 'short' and position_in_bb > 0.75:
            confidence += 0.25
            evidence.append("Price near range top")
            readiness = 'ready'
        else:
            confidence += 0.05
            evidence.append(f"Mid-range ({position_in_bb:.0%})")

    # BONUS: SFP detection (swing failure pattern)
    high_type = structure.get('last_high_type', '')
    low_type = structure.get('last_low_type', '')
    if direction == 'long' and low_type == 'HL':
        confidence += 0.1
        evidence.append("Higher low at range bottom")
    elif direction == 'short' and high_type == 'LH':
        confidence += 0.1
        evidence.append("Lower high at range top")

    invalidation = f"Breaks {'below range low' if direction == 'long' else 'above range high'}"
    return min(confidence, 1.0), evidence, readiness, invalidation


def classify_setup(asset, direction):
    """Run all 6 detectors and pick the best setup type.

    Returns:
        dict with type, confidence, readiness, evidence, invalidation,
        strategy, and optional secondary setup.
    """
    detectors = [
        ('trend_continuation', _detect_trend_continuation),
        ('pullback', _detect_pullback),
        ('breakout', _detect_breakout),
        ('reversal', _detect_reversal),
        ('mean_reversion', _detect_mean_reversion),
        ('range', _detect_range),
    ]

    results = []
    for name, fn in detectors:
        try:
            conf, ev, ready, inval = fn(asset, direction)
            if conf > 0:
                results.append({
                    'type': name,
                    'confidence': round(conf, 2),
                    'readiness': ready,
                    'evidence': ev,
                    'invalidation': inval,
                })
        except Exception as e:
            logger.debug(f"Detector {name} failed: {e}")
            continue

    if not results:
        return None

    # Sort by confidence descending
    results.sort(key=lambda r: r['confidence'], reverse=True)
    best = results[0]

    # CVD spot trend bonus/note
    cvd = asset.get('cvd_trend') or {}
    cvd_dir = cvd.get('direction', '')
    if cvd_dir:
        if (direction == 'long' and cvd_dir == 'bullish') or \
           (direction == 'short' and cvd_dir == 'bearish'):
            best['confidence'] = min(round(best['confidence'] + 0.05, 2), 1.0)
            best['evidence'].append(f"CVD confirms ({cvd_dir})")
        elif (direction == 'long' and cvd_dir == 'bearish') or \
             (direction == 'short' and cvd_dir == 'bullish'):
            best['evidence'].append(f"CVD opposes ({cvd_dir})")

    # Add strategy
    best['strategy'] = SETUP_STRATEGIES.get(best['type'], '')

    # Secondary setup if runner-up is within 70% of best confidence
    if len(results) > 1:
        runner = results[1]
        if runner['confidence'] >= best['confidence'] * 0.7:
            best['secondary'] = {
                'type': runner['type'],
                'confidence': runner['confidence'],
                'readiness': runner['readiness'],
            }

    return best


