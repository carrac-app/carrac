"""MCP tools: Scanner (4 tools)."""

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError

# Free tier sees only the top N scanner results, mirroring the HTTP endpoint.
# Keep in sync with FREE_SCANNER_RESULT_LIMIT in
# carrac/fastapi_app/routers/scanner.py.
FREE_SCANNER_RESULT_LIMIT = 5


DEFAULT_SCANNER_LIMIT = 25
MAX_SCANNER_LIMIT = 100


def _staleness_for_symbol(symbol):
    """The staleness block for one symbol, or None when its history is current.

    A per-tool allowlist lets an unlisted tool keep publishing
    support/resistance off a series that stopped updating; this helper exists
    so any tool that publishes a technical read can ask the question in one
    line.

    Non-fatal: an unavailable cache means "nothing known to be stale", which adds
    no new failure mode.
    """
    try:
        from carrac.composite.assemblers import _get_htf_by_symbol
        from carrac.staleness import history_is_dead

        asset = (_get_htf_by_symbol([symbol]) or {}).get(symbol) or {}
        block = asset.get('data_stale')
        return block if history_is_dead(block) else None
    except Exception:
        return None


@mcp_tool(
    'scanner_get_screen',
    'Screen the asset universe by how many of the 12 scanner conditions are currently met, '
    'and read the per-criterion evidence behind each one. '
    'Carrac reports OBSERVATIONS, never signals or recommendations: results describe what the '
    'market is doing, not what to do about it. '
    'Returns: {results [{symbol, price, '
    'bullish {conditions_met, conditions_total, evidence {trend, momentum, rsi, structure, level, macro}, vetoes}, '
    'bearish {…same shape…}}], macro, computed_at, excluded_stale}. '
    'excluded_stale lists symbols withheld from the screen because no price data has been '
    'collected for them recently — their condition counts and evidence strings would describe '
    'bars that stopped arriving. Their absence from results is deliberate, not a data gap; '
    'do not treat an excluded symbol as having zero conditions met. '
    '"bullish"/"bearish" describe the conditions observed in the market — they are not position '
    'suggestions, and no entries, exits or targets are published. '
    'Filter with min_conditions (0-12) and/or alignment (bullish/bearish/any); '
    'results are sorted by conditions met, descending. '
    'Member+ tiers get the full universe; free tier is capped to the top 5. '
    'Use market_get_overview for a lighter ranked snapshot without the evidence strings. '
    'Data freshness: scanner recompute cycle (~30 min).',
    {
        'type': 'object',
        'properties': {
            'min_conditions': {
                'type': 'integer',
                'minimum': 0,
                'maximum': 12,
                'description': 'Only return assets meeting at least this many of the 12 '
                               'conditions (default 0 = no filter).',
            },
            'alignment': {
                'type': 'string',
                'enum': ['bullish', 'bearish', 'any'],
                'description': 'Which observed alignment to filter and sort on '
                               '(default "any" = whichever side has more conditions met).',
            },
            'limit': {
                'type': 'integer',
                'minimum': 1,
                'maximum': MAX_SCANNER_LIMIT,
                'description': f'Max assets to return (default {DEFAULT_SCANNER_LIMIT}, '
                               f'max {MAX_SCANNER_LIMIT}). Evidence strings make rows large — '
                               'raise this deliberately.',
            },
        },
    },
    min_tier='free',
)
def scanner_get_screen(arguments):
    from carrac.caches import get_cached_scanner
    from carrac.formatters import evidence_scanner
    from carrac.mcp.runtime import get_mcp_caller_tier
    from carrac.staleness import history_is_dead

    data = get_cached_scanner()
    if not data:
        raise ToolError('Scanner data not available — server is warming up')

    results = list(data.get('results', []))

    # A screen answers "what is the market doing"; an asset whose
    # feed stopped is not doing anything, and its conditions are counted off
    # bars that no longer exist. KAS ranked 12/12 that way. Named, not dropped
    # silently — a filtered universe that does not say what it filtered reads
    # as a complete one.
    excluded_stale = sorted(
        r.get('symbol') for r in results if history_is_dead(r.get('data_stale'))
    )
    if excluded_stale:
        results = [r for r in results if not history_is_dead(r.get('data_stale'))]

    alignment = (arguments.get('alignment') or 'any').lower()
    if alignment not in ('bullish', 'bearish', 'any'):
        raise ToolError(
            f'Unknown alignment "{alignment}". Valid values: bullish, bearish, any.'
        )
    # Descriptive label -> the raw engine key it reads from.
    raw_key = {'bullish': 'long', 'bearish': 'short'}.get(alignment)

    def _met(result):
        """Conditions met on the side being filtered/sorted on."""
        if raw_key:
            return (result.get(raw_key) or {}).get('total') or 0
        return max(
            (result.get('long') or {}).get('total') or 0,
            (result.get('short') or {}).get('total') or 0,
        )

    min_conditions = arguments.get('min_conditions')
    if min_conditions is not None:
        try:
            min_conditions = int(min_conditions)
        except (TypeError, ValueError):
            raise ToolError('min_conditions must be an integer between 0 and 12.')
        if not 0 <= min_conditions <= 12:
            raise ToolError('min_conditions must be between 0 and 12.')
        results = [r for r in results if _met(r) >= min_conditions]

    results = sorted(results, key=_met, reverse=True)

    limit = arguments.get('limit')
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ToolError('limit must be an integer.')
        limit = max(1, min(limit, MAX_SCANNER_LIMIT))
    else:
        limit = DEFAULT_SCANNER_LIMIT
    results = results[:limit]

    # Free tier sees only the top results, mirroring /api/scanner (HTTP).
    # Without this, a free API key could pull the full universe via MCP.
    if get_mcp_caller_tier() == 'free' and len(results) > FREE_SCANNER_RESULT_LIMIT:
        results = results[:FREE_SCANNER_RESULT_LIMIT]

    out = evidence_scanner({
        'results': results,
        'macro': data.get('macro', {}),
        'computed_at': data.get('computed_at', ''),
    })
    out['excluded_stale'] = excluded_stale
    return out


@mcp_tool(
    'scanner_get_confluence_zones',
    'Clustered S/R zones for one asset, pulled from the scanner cache — no computation, instant. '
    'Returns: {symbol, price, '
    'supports [{price, label, label_friendly, sources, members, strength, tier, side}][:5], '
    'resistances [{price, label, label_friendly, sources, members, strength, tier, side}][:5], '
    'total_zones}. '
    'label_friendly gives a plain-English level name (e.g. "Point of Control + SMA 200"); '
    'tier classifies zone strength as "strong", "medium", or "faint". '
    'Zones are sorted by strength (descending) and deduped within 0.1% price distance. '
    'Use for entry/exit level research or to contextualize a trade idea. '
    'For full VP-based liquidity analysis use chart_get_volume_profile (heavier, computes from klines). '
    'min_tier: member.',
    {
        'type': 'object',
        'properties': {
            'symbol': {
                'type': 'string',
                'description': 'Asset symbol (e.g. BTC, ETH)',
            },
        },
        'required': ['symbol'],
    },
    min_tier='member',
)
def scanner_get_confluence_zones(arguments):
    from carrac.scanner.confluence_query import get_symbol_confluence

    symbol = arguments.get('symbol', '').upper().strip()
    if not symbol:
        raise ToolError('symbol is required')

    payload, status = get_symbol_confluence(symbol)

    if status == 'cold':
        raise ToolError('Scanner data not available')
    if status == 'not_found':
        raise ToolError(f'Symbol {symbol} not found in scanner data')

    _ZONE_KEYS = ('price', 'label', 'label_friendly', 'sources', 'members',
                  'strength', 'tier', 'side')

    def _pick(zone):
        return {k: zone.get(k) for k in _ZONE_KEYS}

    # The zones ARE the fabrication for a dead symbol: a swing low
    # from a series that stopped two months ago, offered against a live price.
    # Keep the price (genuinely known), publish no levels.
    _stale = _staleness_for_symbol(symbol)
    if _stale:
        return {
            'symbol': payload['symbol'],
            'price': payload['price'],
            'data_stale': _stale,
            'supports': [],
            'resistances': [],
            'total_zones': 0,
        }

    supports = sorted(payload['supports'], key=lambda z: z.get('strength', 0) or 0, reverse=True)[:5]
    resistances = sorted(payload['resistances'], key=lambda z: z.get('strength', 0) or 0, reverse=True)[:5]

    return {
        'symbol': payload['symbol'],
        'price': payload['price'],
        'supports': [_pick(z) for z in supports],
        'resistances': [_pick(z) for z in resistances],
        'total_zones': len(payload['all']),
    }


@mcp_tool(
    'scanner_get_transitions',
    'Recent indicator state-change events across all assets (or one symbol). '
    'Returns: {transitions [{symbol, indicator, from, to, significance (0–3), timestamp, ...}], count}. '
    'Examples of events: RSI crossing 70, consensio flipping bullish, BB squeeze releasing. '
    'Filter by symbol and/or min_significance (0=all, 1=notable, 2=important, 3=critical). '
    'Use for "what changed recently" questions or to spot regime shifts before they appear in the scanner. '
    'For a per-asset live view that combines zones + signals + transitions use scanner_get_live_stream. '
    'min_tier: premium.',
    {
        'type': 'object',
        'properties': {
            'symbol': {
                'type': 'string',
                'description': 'Filter to one asset (optional)',
            },
            'min_significance': {
                'type': 'integer',
                'description': 'Minimum significance 0-3 (default: 0, higher = more important)',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max events (default: 50, max: 200)',
            },
        },
    },
    min_tier='premium',
)
def scanner_get_transitions(arguments):
    from carrac.indicator_state import get_recent_transitions

    symbol = arguments.get('symbol', '').upper().strip() or None
    min_sig = arguments.get('min_significance', 0)
    limit = min(arguments.get('limit', 50), 200)

    events = get_recent_transitions(limit=limit, symbol=symbol, min_significance=min_sig)
    return {
        'transitions': events,
        'count': len(events),
    }


@mcp_tool(
    'scanner_get_live_stream',
    'Per-asset real-time snapshot combining scanner zones, engulfing signals, and recent transitions. '
    'Returns per asset: {symbol, price, '
    'supports [{price, label, strength}][:3], resistances [{price, label, strength}][:3], '
    'signals [{tf, type, direction}][:3] (HTF + on-demand LTF), '
    'transitions [{indicator, from, to, significance}][:3]}. '
    'Default: all scanner assets. Pass symbols="BTC,ETH,SOL" to filter to a watchlist (triggers on-demand LTF compute). '
    'Heavier than scanner_get_confluence_zones for a single asset but richer (adds signals + transitions). '
    'Use for monitoring a focused watchlist or building a real-time dashboard context block. '
    'min_tier: member.',
    {
        'type': 'object',
        'properties': {
            'symbols': {
                'type': 'string',
                'description': 'Comma-separated symbols to include (default: all scanner assets)',
            },
        },
    },
    min_tier='member',
)
def scanner_get_live_stream(arguments):
    from carrac.caches import get_cached_scanner
    from carrac.formatters import compact_stream

    scanner_data = get_cached_scanner()
    if not scanner_data:
        raise ToolError('Stream data not available — server is warming up')

    # Get HTF cache for BB squeeze, key levels; LTF computed on-demand
    try:
        from carrac.precompute import response_cache
        htf_data = response_cache.get('htf')
    except Exception:
        htf_data = None

    htf_by_symbol = {a.get('symbol', ''): a for a in (htf_data or {}).get('assets', [])}
    ltf_by_symbol = {}  # populated on-demand below

    # Filter symbols if specified
    filter_symbols = None
    if arguments.get('symbols'):
        filter_symbols = {s.strip().upper() for s in arguments['symbols'].split(',')}

    # On-demand LTF for filtered symbols (cached in Redis, 40 min TTL)
    if filter_symbols:
        try:
            from carrac.precompute import _compute_single_asset_ltf
            for sym in filter_symbols:
                ltf_asset = _compute_single_asset_ltf(sym)
                if ltf_asset:
                    ltf_by_symbol[sym] = ltf_asset
        except Exception:
            pass

    entries = []
    for result in scanner_data.get('results', []):
        symbol = result.get('symbol', '')
        if filter_symbols and symbol not in filter_symbols:
            continue

        # Extract zones
        all_zones = []
        seen_prices = []
        for direction in ('long', 'short'):
            for zone in result.get(direction, {}).get('confluences', {}).get('all', []):
                zp = zone.get('price', 0)
                if not zp:
                    continue
                dup = any(sp > 0 and abs(zp - sp) / sp <= 0.001 for sp in seen_prices)
                if not dup:
                    seen_prices.append(zp)
                    all_zones.append({
                        'price': zp, 'label': zone.get('label', ''),
                        'strength': zone.get('strength', 0), 'side': zone.get('side', ''),
                    })

        htf_asset = htf_by_symbol.get(symbol, {})
        bb = htf_asset.get('bb') or {}
        kl = htf_asset.get('key_levels') or {}

        entry = {
            'symbol': symbol,
            'price': result.get('price', 0),
            'zones': all_zones,
            'bb_squeeze_pct': bb.get('squeeze_pct'),
            'pdh': kl.get('pdh'),
            'pdl': kl.get('pdl'),
        }

        # Engulfing signals
        signals = list(htf_asset.get('engulfing_signals') or [])
        ltf_signals = ltf_by_symbol.get(symbol, {}).get('engulfing_signals') or []
        if ltf_signals:
            signals.extend(ltf_signals)
        if signals:
            entry['signals'] = signals

        entries.append(entry)

    # Attach transitions
    try:
        from carrac.indicator_state import get_recent_transitions
        all_transitions = get_recent_transitions(limit=200, min_significance=1)
        trans_by_sym = {}
        for t in all_transitions:
            trans_by_sym.setdefault(t.get('symbol', ''), []).append(t)
        for entry in entries:
            sym_trans = trans_by_sym.get(entry['symbol'], [])
            if sym_trans:
                entry['transitions'] = sym_trans[:5]
    except Exception:
        pass

    return compact_stream({
        'entries': entries,
        'computed_at': scanner_data.get('computed_at', ''),
    })
