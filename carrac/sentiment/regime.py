"""
Market Regime classifier — composite macro signal from cached data.

Scores 9 signals (VIX, F&G, SPX trend, DXY trend, funding, breadth,
yield spread, M2 growth, Fed Funds direction)
each -2 to +2.  Sum maps to: Risk-On / Cautious / Neutral / Risk-Off / Crisis.
"""

import logging
from datetime import datetime

logger = logging.getLogger('carrac.sentiment.regime')


def compute_market_regime(vix=None, spx_above_sma=None, dxy_trend=None,
                          fear_greed=None, funding=None, breadth_pct=None,
                          yield_spread=None, m2_yoy=None, fed_funds_trend=None):
    """Score 7 macro signals and classify the market regime.

    Returns dict with regime, score, color, components, computed_at.
    """
    components = []

    # 1. VIX
    if vix is not None:
        if vix < 15:
            s = 2
        elif vix < 20:
            s = 1
        elif vix < 25:
            s = 0
        elif vix < 35:
            s = -1
        else:
            s = -2
        components.append({'name': 'VIX', 'score': s, 'detail': f'{vix:.1f}'})
    else:
        components.append({'name': 'VIX', 'score': 0, 'detail': 'N/A'})

    # 2. Fear & Greed
    if fear_greed is not None:
        if fear_greed > 75:
            s = 1
        elif fear_greed >= 25:
            s = 0
        elif fear_greed >= 10:
            s = -1
        else:
            s = -2
        components.append({'name': 'F&G', 'score': s, 'detail': str(int(fear_greed))})
    else:
        components.append({'name': 'F&G', 'score': 0, 'detail': 'N/A'})

    # 3. SPX above 200 SMA (EMA Ribbon slow EMA proxy)
    if spx_above_sma is not None:
        s = 1 if spx_above_sma else -1
        label = 'above' if spx_above_sma else 'below'
        components.append({'name': 'SPX 200SMA', 'score': s, 'detail': label})
    else:
        components.append({'name': 'SPX 200SMA', 'score': 0, 'detail': 'N/A'})

    # 4. DXY trend
    if dxy_trend == 'falling':
        s = 1
    elif dxy_trend == 'rising':
        s = -1
    else:
        s = 0
    components.append({'name': 'DXY', 'score': s, 'detail': dxy_trend or 'N/A'})

    # 5. BTC Funding rate (as percentage, e.g. 0.01 = 0.01%)
    if funding is not None:
        pct = funding * 100 if abs(funding) < 1 else funding
        if pct > 0.03:
            s = 1
        elif pct >= 0:
            s = 0
        else:
            s = -1
        components.append({'name': 'Funding', 'score': s, 'detail': f'{pct:.4f}%'})
    else:
        components.append({'name': 'Funding', 'score': 0, 'detail': 'N/A'})

    # 6. Market breadth (% of assets with consensio positions >= 4)
    if breadth_pct is not None:
        if breadth_pct > 60:
            s = 1
        elif breadth_pct >= 30:
            s = 0
        else:
            s = -1
        components.append({'name': 'Breadth', 'score': s, 'detail': f'{breadth_pct:.0f}%'})
    else:
        components.append({'name': 'Breadth', 'score': 0, 'detail': 'N/A'})

    # 7. Yield spread (US10Y - US5Y)
    if yield_spread is not None:
        if yield_spread > 0.5:
            s = 1
        elif yield_spread >= -0.1:
            s = 0
        elif yield_spread >= -0.5:
            s = -1
        else:
            s = -2
        components.append({'name': 'Yield Spread', 'score': s, 'detail': f'{yield_spread:.2f}'})
    else:
        components.append({'name': 'Yield Spread', 'score': 0, 'detail': 'N/A'})

    # 8. M2 Money Supply YoY growth
    if m2_yoy is not None:
        if m2_yoy > 6:
            s = 2
        elif m2_yoy > 3:
            s = 1
        elif m2_yoy >= 0:
            s = 0
        elif m2_yoy >= -3:
            s = -1
        else:
            s = -2
        components.append({'name': 'M2 YoY', 'score': s, 'detail': f'{m2_yoy:.1f}%'})
    else:
        components.append({'name': 'M2 YoY', 'score': 0, 'detail': 'N/A'})

    # 9. Fed Funds direction (cutting = risk-on, hiking = risk-off)
    if fed_funds_trend == 'falling':
        s = 1
    elif fed_funds_trend == 'rising':
        s = -1
    else:
        s = 0
    components.append({'name': 'Fed Funds', 'score': s, 'detail': fed_funds_trend or 'N/A'})

    total = sum(c['score'] for c in components)

    # Thresholds for 9 components (max ±14)
    if total >= 6:
        regime, color = 'Risk-On', '#3fb950'
    elif total >= 3:
        regime, color = 'Cautious', '#7ee787'
    elif total >= -2:
        regime, color = 'Neutral', '#d29922'
    elif total >= -6:
        regime, color = 'Risk-Off', '#f0883e'
    else:
        regime, color = 'Crisis', '#f85149'

    return {
        'regime': regime,
        'score': total,
        'color': color,
        'components': components,
        'computed_at': datetime.now().isoformat(),
    }


def get_or_compute_regime():
    """Return the market regime, computing + caching it on a cache miss.

    This is the ONE canonical "read-or-compute-and-cache" entrypoint for the
    regime cache (``carrac.caches.get_cached_regime``/``set_cached_regime``,
    a 300s-TTL SharedTTLCache). Consumers must use this rather than a
    copy-pasted read-or-compute pattern: a consumer that copies one
    (``sentiment/routes.py``'s ``_fetch_regime()``, ``mcp/tools/market.py``'s
    inline ``_regime()``) self-heals a cold cache, but any consumer that does a bare
    ``get_cached_regime() or {}`` (e.g. the scanner's ``_build_macro_data``)
    silently gets neutral-looking defaults on every cache miss.

    The regime cache is only ever warmed as a side effect of something
    calling this function — nothing refreshes it on a fixed schedule — so
    its effective freshness window is the 300s TTL, not "always". Any
    consumer that only reads (never computes-on-miss) will see it cold most
    of the time whenever it is polled less often than every ~5 minutes.

    Never raises. Returns None (not a fabricated neutral dict) if both the
    cache and a fresh compute fail — callers MUST treat None as "unavailable"
    and must not substitute a neutral-looking default, or the failure becomes
    silent and indistinguishable from a genuine neutral reading.
    """
    from carrac.caches import get_cached_regime, set_cached_regime
    regime = get_cached_regime()
    if regime:
        return regime
    try:
        inputs = gather_regime_inputs()
        regime = compute_market_regime(**inputs)
        set_cached_regime(regime)
        return regime
    except Exception:
        logger.warning("Regime compute failed on cache miss", exc_info=True)
        return None


def gather_regime_inputs():
    """Read VIX, SPX, DXY, F&G, funding, breadth, yield spread, M2, Fed Funds.

    All reads are wrapped in try/except — missing data returns None.
    """
    inputs = {
        'vix': None,
        'spx_above_sma': None,
        'dxy_trend': None,
        'fear_greed': None,
        'funding': None,
        'breadth_pct': None,
        'yield_spread': None,
        'm2_yoy': None,
        'fed_funds_trend': None,
    }

    try:
        from carrac.precompute import response_cache
        cached = response_cache.get('htf')
    except Exception:
        cached = None

    if not cached:
        return inputs

    assets = cached.get('assets', [])

    def _find(sym):
        for a in assets:
            if a.get('symbol') == sym:
                return a
        return None

    # VIX
    try:
        vix_asset = _find('VIX')
        if vix_asset:
            inputs['vix'] = float(vix_asset.get('latest_price', 0))
    except Exception:
        pass

    # SPX above 200 SMA (use EMA Ribbon slow EMA as proxy)
    try:
        spx = _find('SPX')
        if spx:
            price = float(spx.get('latest_price', 0))
            ema_ribbon = spx.get('ema_ribbon') or {}
            slow_ema = ema_ribbon.get('slow_ema')
            if price and slow_ema:
                inputs['spx_above_sma'] = price > slow_ema
    except Exception:
        pass

    # DXY trend
    try:
        dxy = _find('DXY')
        if dxy:
            price = float(dxy.get('latest_price', 0))
            ema_ribbon = dxy.get('ema_ribbon') or {}
            slow_ema = ema_ribbon.get('slow_ema')
            if price and slow_ema:
                inputs['dxy_trend'] = 'rising' if price > slow_ema else 'falling'
    except Exception:
        pass

    # Fear & Greed
    try:
        from carrac.analysis import fetch_fear_greed_index
        fg = fetch_fear_greed_index()
        if fg and fg.get('value') is not None:
            inputs['fear_greed'] = int(fg['value'])
    except Exception:
        pass

    # BTC Funding rate
    try:
        from carrac.services import get_services
        fr = get_services().db.get_funding_rate('BTC')
        if fr is not None:
            inputs['funding'] = fr
    except Exception:
        pass

    # Market breadth (% of assets with consensio positions >= 4)
    try:
        total = len(assets)
        if total > 0:
            bullish = sum(
                1 for a in assets
                if a.get('consensio', {}).get('total', {}).get('positions', 0) >= 4
            )
            inputs['breadth_pct'] = (bullish / total) * 100
    except Exception:
        pass

    # FRED — yield spread, M2, Fed Funds in ONE bounded fan-out.
    # Three sequential, untimed calls through the shared 2-slot
    # _FRED_REQUEST_SEMAPHORE would be slow: with `fred`'s own get_all() already queued on
    # the same slots, a slow FRED held 'regime' for the whole 12 s request
    # budget while `fred` gave up at 5 s (observed on real data). Sharing
    # get_all()'s deadline makes regime degrade on the same clock.
    try:
        from data_sources.fred import get_fred_client
        fred = get_fred_client().get_all(keys=('yield_spread', 'm2', 'fed_funds')) or {}
    except Exception:
        fred = {}

    # Yield spread — prefer FRED T10Y2Y (authoritative 10Y-2Y), fall back to Stooq
    try:
        spread = fred.get('yield_spread')
        if spread and spread.get('value') is not None:
            inputs['yield_spread'] = spread['value']
        else:
            # Fallback: Stooq-derived US10Y - US5Y
            us10y = _find('US10Y')
            us5y = _find('US5Y')
            if us10y and us5y:
                inputs['yield_spread'] = float(us10y['latest_price']) - float(us5y['latest_price'])
    except Exception:
        pass

    # M2 Money Supply YoY growth (FRED)
    try:
        m2 = fred.get('m2')
        if m2 and m2.get('yoy_pct') is not None:
            inputs['m2_yoy'] = m2['yoy_pct']
    except Exception:
        pass

    # Fed Funds rate trend (FRED)
    try:
        ff = fred.get('fed_funds')
        if ff and ff.get('trend'):
            inputs['fed_funds_trend'] = ff['trend']
    except Exception:
        pass

    return inputs
