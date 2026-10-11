"""Read clustered confluence zones for a single symbol from the scanner cache.

Shared by the /api/confluence endpoint (FastAPI) and the scanner_get_confluence_zones
MCP tool so both return one consistent, enriched shape. Zones are produced by
carrac/scanner/confluence.py during precompute and cached per asset under
result[direction]['confluences']; long/short hold the same (direction-independent)
zones, so we read both and dedup within 0.1%.
"""

_DEDUP_PCT = 0.001  # collapse zones within 0.1% (duplicates across long/short)

# Enriched fields carried through to consumers (zone shape).
_ZONE_FIELDS = ('price', 'label', 'label_friendly', 'sources', 'members',
                'strength', 'tier', 'side', 'span', 'count')


def _zone_out(zone):
    return {k: zone.get(k) for k in _ZONE_FIELDS}


def _payload_from_result(result, symbol, computed_at):
    """Build the enriched per-symbol payload from one scanner result dict
    (dedup long/short, split + sort supports/resistances). Pure — no cache I/O."""
    price = result.get('price', 0) or 0
    seen = []
    zones = []
    for direction in ('long', 'short'):
        conf = (result.get(direction) or {}).get('confluences') or {}
        for zone in conf.get('all', []):
            zp = zone.get('price', 0)
            if not zp:
                continue
            if any(sp > 0 and abs(zp - sp) / sp <= _DEDUP_PCT for sp in seen):
                continue
            seen.append(zp)
            zones.append(_zone_out(zone))

    supports = sorted([z for z in zones if z['side'] == 'support'],
                      key=lambda z: -(z['price'] or 0))
    resistances = sorted([z for z in zones if z['side'] == 'resistance'],
                         key=lambda z: (z['price'] or 0))
    return {
        'symbol': symbol,
        'price': price,
        'computed_at': computed_at,
        'supports': supports,
        'resistances': resistances,
        'all': zones,
    }


def get_symbol_confluence(symbol):
    """Return (payload, status).

    status:
        'cold'      — scanner cache not ready yet
        'not_found' — symbol absent from the cache (or blank)
        'ok'        — payload populated

    payload (status 'ok'):
        {symbol, price, computed_at, supports, resistances, all}
        supports nearest-first (desc price), resistances nearest-first (asc price),
        all = every deduped zone. Each zone carries the full enriched shape.

    NOTE: each call re-reads + re-parses the WHOLE scanner blob (~350 assets).
    Callers needing many symbols MUST use ``get_confluence_map`` instead.
    """
    from carrac.caches import get_cached_scanner

    symbol = (symbol or '').upper().strip()
    if not symbol:
        return None, 'not_found'

    data = get_cached_scanner()
    if not data:
        return None, 'cold'

    computed_at = data.get('computed_at', '')
    for result in data.get('results', []):
        if result.get('symbol') != symbol:
            continue
        return _payload_from_result(result, symbol, computed_at), 'ok'

    return None, 'not_found'


def get_confluence_map(symbols=None):
    """Fetch the scanner cache ONCE and return ``{symbol: payload}`` ('ok' shape).

    The single-symbol ``get_symbol_confluence`` re-deserialises the whole scanner
    blob on every call (~1.3s each), so a page that needs many symbols (the
    editorial report's spotlight + board) must use this — one fetch for all of
    them. Returns ``{}`` when the cache is cold. ``symbols`` (optional, iterable)
    restricts the map; matching is case-insensitive.
    """
    from carrac.caches import get_cached_scanner

    data = get_cached_scanner()
    if not data:
        return {}

    want = ({(s or '').upper().strip() for s in symbols}
            if symbols is not None else None)
    computed_at = data.get('computed_at', '')
    out = {}
    for result in data.get('results', []):
        sym = result.get('symbol')
        if not sym:
            continue
        if want is not None and sym not in want:
            continue
        out[sym] = _payload_from_result(result, sym, computed_at)
    return out
