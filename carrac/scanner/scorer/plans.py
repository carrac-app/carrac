"""Trade-plan construction for each setup type (trend, pullback, breakout, reversal, mean-reversion, range, mechanical)."""


def _compute_trade_plan(asset, direction, setup_type=None, confluences=None):
    """Derive entry/SL/TP using confluence zones and setup-specific logic.

    If confluences provided, picks from confluence zones (clustered levels).
    Falls back to mechanical nearest-level approach otherwise.
    Dispatches to setup-specific _plan_*() when setup_type is known.
    """
    price = asset.get('latest_price', 0)
    if not price:
        return None

    cpr = asset.get('cpr') or {}

    # Use confluence zones if available, else fall back to raw levels
    if confluences and (confluences.get('supports') or confluences.get('resistances')):
        supports = confluences['supports']  # already sorted nearest-first
        resistances = confluences['resistances']
    else:
        # Legacy fallback: build flat level list (same as old code)
        supports, resistances = _collect_raw_levels(asset, price)

    # Dispatch to setup-specific plan function
    plan_fns = {
        'trend_continuation': _plan_trend,
        'pullback': _plan_pullback,
        'breakout': _plan_breakout,
        'reversal': _plan_reversal,
        'mean_reversion': _plan_mean_reversion,
        'range': _plan_range,
    }

    plan = None
    if setup_type and setup_type in plan_fns:
        plan = plan_fns[setup_type](asset, price, direction, supports, resistances)

    # Fallback: mechanical nearest-level approach
    if not plan:
        plan = _plan_mechanical(asset, price, direction, supports, resistances)

    if plan:
        plan['price_level'] = cpr.get('price_level', '')

    return plan


def _collect_raw_levels(asset, price):
    """Build simple support/resistance lists from raw levels (legacy fallback)."""
    cpr = asset.get('cpr') or {}
    bb = asset.get('bb') or {}
    structure = asset.get('structure') or {}
    ema_ribbon = asset.get('ema_ribbon') or {}

    all_levels = {}
    for key in ('pivot', 'tc', 'bc', 's1', 's2', 's3', 'r1', 'r2', 'r3'):
        val = cpr.get(key)
        if val:
            all_levels[key.upper()] = float(val)
    for key, label in (('upper', 'BB_U'), ('middle', 'BB_M'), ('lower', 'BB_L')):
        val = bb.get(key)
        if val:
            all_levels[label] = float(val)
    for key, label in (('fast_ema', 'EMA30'), ('slow_ema', 'EMA60')):
        val = ema_ribbon.get(key)
        if val:
            all_levels[label] = float(val)
    ma = asset.get('ma_levels') or {}
    for key, label in (('ema21', 'EMA21'), ('ema50', 'EMA50'),
                       ('ema200', 'EMA200'), ('sma200', 'SMA200')):
        val = ma.get(key)
        if val:
            all_levels[label] = float(val)
    for key, label in (('last_high_price', 'SW_H'), ('prev_high_price', 'SW_PH'),
                       ('last_low_price', 'SW_L'), ('prev_low_price', 'SW_PL')):
        val = structure.get(key)
        if val:
            all_levels[label] = float(val)

    min_gap = price * 0.0005
    supports = sorted(
        [{'price': v, 'label': n, 'strength': 1.0} for n, v in all_levels.items() if v < price - min_gap],
        key=lambda x: -x['price']
    )
    resistances = sorted(
        [{'price': v, 'label': n, 'strength': 1.0} for n, v in all_levels.items() if v > price + min_gap],
        key=lambda x: x['price']
    )
    return supports, resistances


# -- Zone helpers for plan functions --

def _zone_price(zone):
    """Get price from a zone dict (confluence) or raw level dict."""
    return zone.get('price', 0)


def _zone_label(zone):
    """Get label from a zone dict."""
    return zone.get('label', '?')


def _build_plan(entry, entry_type, sl, sl_name, sl_pct, tp_list, strategy):
    """Build a plan dict from entry/SL/TP data.

    tp_list: [(price, name, pct, rr), ...]
    """
    plan = {
        'entry': round(entry, 6),
        'entry_type': entry_type,
        'sl': round(sl, 6),
        'sl_name': sl_name,
        'sl_pct': round(sl_pct, 2),
        'setup_strategy': strategy,
    }
    for i, tp in enumerate(tp_list[:2], 1):
        plan[f'tp{i}'] = round(tp[0], 6)
        plan[f'tp{i}_name'] = tp[1]
        plan[f'tp{i}_pct'] = round(tp[2], 2)
        plan[f'rr{i}'] = round(tp[3], 1)
    return plan


def _find_tps(entry, sl_pct, candidates, min_rr=2.0, min_tp_pct=0.3):
    """Find TP levels from candidate zones that meet quality thresholds.

    Returns list of (price, label, pct_from_entry, rr) tuples.
    """
    tps = []
    for zone in candidates:
        zp = _zone_price(zone)
        if not zp:
            continue
        tp_pct = abs(zp - entry) / entry * 100
        if tp_pct < min_tp_pct:
            continue
        rr = _clamp_rr(tp_pct / sl_pct if sl_pct > 0 else 0)
        tps.append((zp, _zone_label(zone), tp_pct, rr))

    # Sort by R:R ascending then pick first >= min_rr
    result = []
    for tp in tps:
        if tp[3] >= min_rr:
            result.append(tp)
            if len(result) >= 2:
                break
    # Fallback: best R:R >= 1.0
    if not result and tps:
        best = max(tps, key=lambda x: x[3])
        if best[3] >= 1.0:
            result.append(best)
    return result


SL_BUFFER = 0.003   # 0.3% beyond the level
MIN_TP_PCT = 0.3
MIN_RR = 2.0

# Volatility-aware SL floor — prevents R:R inflation when price hugs a level.
# SL must be at least max(MIN_SL_PCT_FLOOR, ATR_MULT * atr_pct) away from entry.
ATR_MULT = 1.0
MIN_SL_PCT_FLOOR = 0.8   # absolute floor in %, used when ATR unavailable
MAX_RR = 8.0              # safety-net cap for the rendered R:R column


def _apply_sl_floor(entry, sl_val, direction, atr_pct):
    """Widen SL if it sits inside the noise floor.

    Floor = max(MIN_SL_PCT_FLOOR, ATR_MULT * atr_pct). When the level-based
    SL is closer than the floor, push the SL out so the distance equals the
    floor. Returns the (possibly widened) SL price.
    """
    if entry <= 0:
        return sl_val
    sl_pct_current = abs(entry - sl_val) / entry * 100
    floor = MIN_SL_PCT_FLOOR
    if atr_pct and atr_pct > 0:
        floor = max(MIN_SL_PCT_FLOOR, ATR_MULT * atr_pct)
    if sl_pct_current >= floor:
        return sl_val
    if direction == 'long':
        return entry * (1 - floor / 100)
    return entry * (1 + floor / 100)


def _clamp_rr(rr):
    """Clamp R:R to MAX_RR. Edge-case safety for missing-ATR assets."""
    if rr is None or rr <= 0:
        return 0
    return min(rr, MAX_RR)


def _plan_trend(asset, price, direction, supports, resistances):
    """Trend continuation: MARKET entry, SL below nearest support confluence, TP at resistance confluences."""
    if direction == 'long':
        sl_zones, tp_zones = supports, resistances
    else:
        sl_zones, tp_zones = resistances, supports

    if not sl_zones:
        return None

    sl_zone = sl_zones[0]
    sl_val = _zone_price(sl_zone) * (1 - SL_BUFFER if direction == 'long' else 1 + SL_BUFFER)
    sl_val = _apply_sl_floor(price, sl_val, direction, asset.get('atr_pct'))
    sl_pct = abs(price - sl_val) / price * 100

    tps = _find_tps(price, sl_pct, tp_zones, min_rr=MIN_RR)
    if not tps:
        return None

    return _build_plan(price, 'market', sl_val, _zone_label(sl_zone), sl_pct, tps,
                       "Structure-based reference below the swing low; invalidation above structure.")


def _plan_pullback(asset, price, direction, supports, resistances):
    """Pullback: LIMIT entry at strongest support confluence, SL below that zone."""
    if direction == 'long':
        entry_zones, tp_zones = supports, resistances
    else:
        entry_zones, tp_zones = resistances, supports

    if not entry_zones:
        return None

    # Pick strongest confluence (highest strength) among the nearest 3
    candidates = entry_zones[:3]
    entry_zone = max(candidates, key=lambda z: z.get('strength', 1.0))
    entry_price = _zone_price(entry_zone)

    # SL: below entry zone with buffer
    sl_val = entry_price * (1 - SL_BUFFER if direction == 'long' else 1 + SL_BUFFER)
    sl_val = _apply_sl_floor(entry_price, sl_val, direction, asset.get('atr_pct'))
    sl_pct = abs(entry_price - sl_val) / entry_price * 100

    tps = _find_tps(entry_price, sl_pct, tp_zones, min_rr=MIN_RR)
    if not tps:
        return None

    return _build_plan(entry_price, 'limit', sl_val, _zone_label(entry_zone), sl_pct, tps,
                       "Reference levels: confluence zone and below-zone invalidation boundary.")


def _plan_breakout(asset, price, direction, supports, resistances):
    """Breakout: MARKET entry, SL at BB middle (inside range), TP at 1.5x ATR then resistance confluence."""
    bb = asset.get('bb') or {}
    bb_m = bb.get('middle')

    if direction == 'long':
        sl_zones, tp_zones = supports, resistances
    else:
        sl_zones, tp_zones = resistances, supports

    # SL: prefer BB middle if it's on the correct side, else nearest support
    if bb_m:
        bb_m = float(bb_m)
        if (direction == 'long' and bb_m < price) or (direction == 'short' and bb_m > price):
            sl_val = bb_m
            sl_name = 'BB_M'
        elif sl_zones:
            sl_val = _zone_price(sl_zones[0])
            sl_name = _zone_label(sl_zones[0])
        else:
            return None
    elif sl_zones:
        sl_val = _zone_price(sl_zones[0])
        sl_name = _zone_label(sl_zones[0])
    else:
        return None

    sl_val = _apply_sl_floor(price, sl_val, direction, asset.get('atr_pct'))
    sl_pct = abs(price - sl_val) / price * 100

    # TP1: 1.5x ATR approximation (use SL distance * 3 as proxy for 1.5x ATR)
    atr_tp = price + (price - sl_val) * 3 if direction == 'long' else price - (sl_val - price) * 3
    atr_tp_pct = abs(atr_tp - price) / price * 100
    atr_rr = _clamp_rr(atr_tp_pct / sl_pct if sl_pct > 0 else 0)
    tps = [(atr_tp, 'ATR_1.5x', atr_tp_pct, atr_rr)]

    # TP2: next resistance confluence beyond TP1
    for zone in tp_zones:
        zp = _zone_price(zone)
        if (direction == 'long' and zp > atr_tp) or (direction == 'short' and zp < atr_tp):
            tp2_pct = abs(zp - price) / price * 100
            tp2_rr = _clamp_rr(tp2_pct / sl_pct if sl_pct > 0 else 0)
            tps.append((zp, _zone_label(zone), tp2_pct, tp2_rr))
            break

    return _build_plan(price, 'market', sl_val, sl_name, sl_pct, tps,
                       "Reference levels: range edge and volume-break level; invalidation inside range.")


def _plan_reversal(asset, price, direction, supports, resistances):
    """Reversal: MARKET at first bounce level, SL beyond extreme, TP at mean confluences."""
    if direction == 'long':
        sl_zones, tp_zones = supports, resistances
    else:
        sl_zones, tp_zones = resistances, supports

    if not sl_zones:
        return None

    # SL: furthest support or BB extreme, whichever is more extreme
    bb = asset.get('bb') or {}
    extreme_bb = bb.get('lower') if direction == 'long' else bb.get('upper')
    cpr = asset.get('cpr') or {}
    extreme_cpr = cpr.get('s3') if direction == 'long' else cpr.get('r3')

    extreme_prices = [_zone_price(sl_zones[-1])] if sl_zones else []
    if extreme_bb:
        extreme_prices.append(float(extreme_bb))
    if extreme_cpr:
        extreme_prices.append(float(extreme_cpr))

    if not extreme_prices:
        return None

    if direction == 'long':
        sl_val = min(extreme_prices) * (1 - SL_BUFFER)
    else:
        sl_val = max(extreme_prices) * (1 + SL_BUFFER)

    sl_val = _apply_sl_floor(price, sl_val, direction, asset.get('atr_pct'))
    sl_pct = abs(price - sl_val) / price * 100
    sl_name = 'Extreme'

    # TP: mean confluences (BB_M, pivot, EMAs)
    mean_labels = {'BB_M', 'PIVOT', 'EMA30', 'EMA60', 'EMA21', 'EMA50', 'EMA200', 'SMA200', 'POC'}
    mean_tps = []
    for zone in tp_zones:
        lbl = _zone_label(zone)
        # Check if any source in the zone label matches mean labels
        if any(m in lbl for m in mean_labels):
            zp = _zone_price(zone)
            tp_pct = abs(zp - price) / price * 100
            rr = _clamp_rr(tp_pct / sl_pct if sl_pct > 0 else 0)
            if tp_pct >= MIN_TP_PCT:
                mean_tps.append((zp, lbl, tp_pct, rr))

    if not mean_tps:
        # Fallback: any TP zone
        mean_tps = [(z['price'], _zone_label(z), abs(z['price'] - price) / price * 100,
                     _clamp_rr(abs(z['price'] - price) / price * 100 / sl_pct if sl_pct > 0 else 0))
                    for z in tp_zones if abs(z['price'] - price) / price * 100 >= MIN_TP_PCT]

    if not mean_tps:
        return None

    mean_tps.sort(key=lambda x: x[2])  # nearest first
    return _build_plan(price, 'market', sl_val, sl_name, sl_pct, mean_tps[:2],
                       "Reference levels: reversal zone and extreme invalidation boundary.")


def _plan_mean_reversion(asset, price, direction, supports, resistances):
    """Mean reversion: MARKET (already overextended), tight SL at extreme + 0.5%, TP at first mean confluence."""
    if direction == 'long':
        sl_zones, tp_zones = supports, resistances
    else:
        sl_zones, tp_zones = resistances, supports

    # SL: tight — nearest extreme + 0.5%
    if not sl_zones:
        return None

    extreme = _zone_price(sl_zones[0])
    sl_val = extreme * (1 - 0.005 if direction == 'long' else 1 + 0.005)
    sl_val = _apply_sl_floor(price, sl_val, direction, asset.get('atr_pct'))
    sl_name = _zone_label(sl_zones[0])
    sl_pct = abs(price - sl_val) / price * 100

    # TP: first mean confluence (closest of BB_M, pivot, EMA)
    mean_labels = {'BB_M', 'PIVOT', 'EMA30', 'EMA60', 'EMA21', 'EMA50', 'EMA200', 'SMA200', 'POC'}
    mean_tps = []
    for zone in tp_zones:
        lbl = _zone_label(zone)
        if any(m in lbl for m in mean_labels):
            zp = _zone_price(zone)
            tp_pct = abs(zp - price) / price * 100
            rr = _clamp_rr(tp_pct / sl_pct if sl_pct > 0 else 0)
            if tp_pct >= MIN_TP_PCT:
                mean_tps.append((zp, lbl, tp_pct, rr))

    if not mean_tps:
        # Fallback: nearest TP zone
        for zone in tp_zones:
            zp = _zone_price(zone)
            tp_pct = abs(zp - price) / price * 100
            rr = _clamp_rr(tp_pct / sl_pct if sl_pct > 0 else 0)
            if tp_pct >= MIN_TP_PCT:
                mean_tps.append((zp, _zone_label(zone), tp_pct, rr))
                break

    if not mean_tps:
        return None

    mean_tps.sort(key=lambda x: x[2])  # nearest first
    return _build_plan(price, 'market', sl_val, sl_name, sl_pct, mean_tps[:2],
                       "Reference levels: current price and mean-reversion targets at structural midpoints.")


def _plan_range(asset, price, direction, supports, resistances):
    """Range: LIMIT at BB boundary confluence, SL outside range, TP at midpoint confluence."""
    bb = asset.get('bb') or {}

    if direction == 'long':
        entry_zones, tp_zones = supports, resistances
    else:
        entry_zones, tp_zones = resistances, supports

    if not entry_zones:
        return None

    # Entry: nearest support/resistance confluence
    entry_zone = entry_zones[0]
    entry_price = _zone_price(entry_zone)

    # SL: outside range boundary
    bb_l = bb.get('lower')
    bb_u = bb.get('upper')
    if direction == 'long' and bb_l:
        sl_val = float(bb_l) * (1 - SL_BUFFER)
        sl_name = 'BB_L'
    elif direction == 'short' and bb_u:
        sl_val = float(bb_u) * (1 + SL_BUFFER)
        sl_name = 'BB_U'
    elif len(entry_zones) > 1:
        sl_val = _zone_price(entry_zones[1]) * (1 - SL_BUFFER if direction == 'long' else 1 + SL_BUFFER)
        sl_name = _zone_label(entry_zones[1])
    else:
        sl_val = entry_price * (1 - SL_BUFFER if direction == 'long' else 1 + SL_BUFFER)
        sl_name = _zone_label(entry_zone)

    sl_val = _apply_sl_floor(entry_price, sl_val, direction, asset.get('atr_pct'))
    sl_pct = abs(entry_price - sl_val) / entry_price * 100

    # TP: midpoint confluences (POC, pivot, BB_M)
    mid_labels = {'POC', 'PIVOT', 'BB_M'}
    mid_tps = []
    for zone in tp_zones:
        lbl = _zone_label(zone)
        if any(m in lbl for m in mid_labels):
            zp = _zone_price(zone)
            tp_pct = abs(zp - entry_price) / entry_price * 100
            rr = _clamp_rr(tp_pct / sl_pct if sl_pct > 0 else 0)
            if tp_pct >= MIN_TP_PCT:
                mid_tps.append((zp, lbl, tp_pct, rr))

    if not mid_tps:
        # Fallback: nearest TP zone
        for zone in tp_zones:
            zp = _zone_price(zone)
            tp_pct = abs(zp - entry_price) / entry_price * 100
            rr = _clamp_rr(tp_pct / sl_pct if sl_pct > 0 else 0)
            if tp_pct >= MIN_TP_PCT:
                mid_tps.append((zp, _zone_label(zone), tp_pct, rr))
                break

    if not mid_tps:
        return None

    mid_tps.sort(key=lambda x: x[2])  # nearest first
    return _build_plan(entry_price, 'limit', sl_val, sl_name, sl_pct, mid_tps[:2],
                       "Reference levels: range boundary and midpoint confluence zone.")


def _plan_mechanical(asset, price, direction, supports, resistances):
    """Fallback mechanical plan — nearest SL, best R:R TP (old logic with zone dicts)."""
    if direction == 'long':
        sl_zones, tp_zones = supports, resistances
    else:
        sl_zones, tp_zones = resistances, supports

    if not sl_zones:
        return None

    sl_zone = sl_zones[0]
    sl_val = _zone_price(sl_zone) * (1 - SL_BUFFER if direction == 'long' else 1 + SL_BUFFER)
    sl_val = _apply_sl_floor(price, sl_val, direction, asset.get('atr_pct'))
    sl_pct = abs(price - sl_val) / price * 100

    tps = _find_tps(price, sl_pct, tp_zones, min_rr=MIN_RR)
    if not tps:
        return None

    return _build_plan(price, 'market', sl_val, _zone_label(sl_zone), sl_pct, tps, '')

