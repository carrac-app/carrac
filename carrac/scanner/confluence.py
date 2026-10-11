"""
Confluence zone computation — clusters nearby indicator levels into zones.

Single source of truth for S/R confluence: feeds the scorer's SL/TP picks
(carrac/scanner/scorer/plans.py), the /api/confluence endpoint, and the
Confluence Zones chart indicator.

Backward-compat: zone['label'] stays a CODE join (e.g. 'POC+SMA200') because
the scorer parses it by code substring. Friendly names live in 'label_friendly';
per-level detail in 'members'. zone['sources'] stays the legacy family list.
"""

from carrac.scanner.labels import (
    LEVEL_WEIGHTS, DEFAULT_LEVEL_WEIGHT, LEVEL_FAMILY, DEFAULT_FAMILY,
    LABEL_NAMES, DIVERSITY_BONUS, tier_for_strength,
)

# Levels within this fraction of the running zone max merge (single-linkage).
CLUSTER_PCT = 0.005
# Hard cap on total zone span so a long chain can't merge into one giant band.
MAX_ZONE_PCT = 0.010


def compute_confluences(asset_data, price, direction):
    """Cluster nearby indicator levels into confluence zones.

    Args:
        asset_data: dict with cpr, bb, ema_ribbon, ma_levels, structure,
            vp_levels, key_levels
        price: current price
        direction: 'long' or 'short' (used for side labelling)

    Returns:
        {'supports': [...], 'resistances': [...], 'all': [...]}
        Each zone:
            price, label (CODE join), label_friendly, sources (family list),
            members [{code,name,price,weight,family}], strength (weighted +
            diversity), tier, side, count, span [lo, hi]
    """
    if not price:
        return {'supports': [], 'resistances': [], 'all': []}

    # 1. Collect named levels as (code, value).
    levels = []

    cpr = asset_data.get('cpr') or {}
    for key in ('pivot', 'tc', 'bc', 's1', 's2', 's3', 'r1', 'r2', 'r3'):
        val = cpr.get(key)
        if val:
            levels.append((key.upper(), float(val)))

    bb = asset_data.get('bb') or {}
    for key, label in (('upper', 'BB_U'), ('middle', 'BB_M'), ('lower', 'BB_L')):
        val = bb.get(key)
        if val:
            levels.append((label, float(val)))

    ema_ribbon = asset_data.get('ema_ribbon') or {}
    for key, label in (('fast_ema', 'EMA30'), ('slow_ema', 'EMA60')):
        val = ema_ribbon.get(key)
        if val:
            levels.append((label, float(val)))

    ma = asset_data.get('ma_levels') or {}
    for key, label in (('ema21', 'EMA21'), ('ema50', 'EMA50'),
                       ('ema200', 'EMA200'), ('sma200', 'SMA200')):
        val = ma.get(key)
        if val:
            levels.append((label, float(val)))

    structure = asset_data.get('structure') or {}
    for key, label in (('last_high_price', 'SW_H'), ('prev_high_price', 'SW_PH'),
                       ('last_low_price', 'SW_L'), ('prev_low_price', 'SW_PL')):
        val = structure.get(key)
        if val:
            levels.append((label, float(val)))

    vp = asset_data.get('vp_levels') or {}
    for key, label in (('poc', 'POC'), ('vah', 'VAH'), ('val', 'VAL')):
        val = vp.get(key)
        if val:
            levels.append((label, float(val)))

    kl = asset_data.get('key_levels') or {}
    for key, label in (('pdh', 'PDH'), ('pdl', 'PDL'), ('mh', 'MH'), ('ml', 'ML'),
                       ('pwh', 'PWH'), ('pwl', 'PWL'), ('pmh', 'PMH'), ('pml', 'PML')):
        val = kl.get(key)
        if val:
            levels.append((label, float(val)))

    if not levels:
        return {'supports': [], 'resistances': [], 'all': []}

    # 2. Sort by price; single-linkage cluster with a span cap. Merge a level
    #    if it is within CLUSTER_PCT of the cluster's running max AND the total
    #    cluster span stays within MAX_ZONE_PCT (prevents runaway chaining).
    levels.sort(key=lambda x: x[1])
    clusters = []
    current = [levels[0]]
    for code, val in levels[1:]:
        last_price = current[-1][1]
        first_price = current[0][1]
        near = last_price > 0 and (val - last_price) / last_price <= CLUSTER_PCT
        within_span = first_price > 0 and (val - first_price) / first_price <= MAX_ZONE_PCT
        if near and within_span:
            current.append((code, val))
        else:
            clusters.append(current)
            current = [(code, val)]
    clusters.append(current)

    # 3. Build enriched zones.
    min_gap = price * 0.0005  # ignore levels within 0.05% of price
    zones = []
    for cluster in clusters:
        member_prices = [v for _, v in cluster]
        codes = [c for c, _ in cluster]
        zone_price = sum(member_prices) / len(cluster)
        members = [{
            'code': c,
            'name': LABEL_NAMES.get(c, c),
            'price': round(v, 8),
            'weight': LEVEL_WEIGHTS.get(c, DEFAULT_LEVEL_WEIGHT),
            'family': LEVEL_FAMILY.get(c, DEFAULT_FAMILY),
        } for c, v in cluster]
        families = {m['family'] for m in members}
        base = sum(m['weight'] for m in members)
        strength = round(base * (1 + DIVERSITY_BONUS * (len(families) - 1)), 2)
        side = 'support' if zone_price < price - min_gap else (
            'resistance' if zone_price > price + min_gap else 'neutral')
        zones.append({
            'price': round(zone_price, 8),
            'label': '+'.join(codes),
            'label_friendly': ' + '.join(LABEL_NAMES.get(c, c) for c in codes),
            'sources': sorted(families),
            'members': members,
            'strength': strength,
            'tier': tier_for_strength(strength),
            'side': side,
            'count': len(cluster),
            'span': [round(min(member_prices), 8), round(max(member_prices), 8)],
        })

    # 4. Sort: supports descending (nearest first), resistances ascending.
    supports = sorted([z for z in zones if z['side'] == 'support'], key=lambda z: -z['price'])
    resistances = sorted([z for z in zones if z['side'] == 'resistance'], key=lambda z: z['price'])

    return {'supports': supports, 'resistances': resistances, 'all': zones}
