"""Public entrypoints: score_asset, score_all_assets, get_quick_score."""

import logging
import os
from datetime import datetime

logger = logging.getLogger('carrac.scanner.scorer')

from carrac.scanner.scorer.criteria import (
    _verdict, _score_trend, _score_momentum, _score_rsi,
    _score_structure, _score_level, _score_macro,
    _funding_modifier, _smart_money_modifier,
)
from carrac.scanner.scorer.plans import _compute_trade_plan
from carrac.scanner.scorer.setups import classify_setup

# Temporal event-risk flag. Window (hours) + the verdict-cap
# toggle are both ON by default and overridable via env, read once per
# score_all_assets pass (never per asset — see _event_risk_config below).
EVENT_RISK_HOURS_DEFAULT = 36


def _event_risk_config():
    """Read SCANNER_EVENT_RISK_* env config. Cheap; call once per pass."""
    try:
        hours = float(os.environ.get('SCANNER_EVENT_RISK_HOURS', EVENT_RISK_HOURS_DEFAULT))
    except (TypeError, ValueError):
        hours = EVENT_RISK_HOURS_DEFAULT
    cap_enabled = os.environ.get('SCANNER_EVENT_RISK_CAP', '1').strip().lower() not in (
        '0', 'false', 'no', 'off',
    )
    return hours, cap_enabled


def score_asset(asset_data, macro_data, direction, event_risk=None):
    """Score a single asset for long or short.

    Execution order: criteria → vetoes → setup → confluences → trade plan.
    Setup and confluences feed into trade plan for setup-aware SL/TP selection.

    event_risk: optional {title, hours_until, ...} dict from
    carrac.scanner.event_risk.get_imminent_event(), fetched ONCE per
    score_all_assets pass and passed down here (never fetched per-asset).
    When present, a STRONG verdict is demoted one notch to GOOD — direction-
    agnostic, score untouched, only the verdict label + modifiers evidence
    change.
    """
    from carrac.scanner.confluence import compute_confluences

    criteria = {}
    vetoes = []
    meta = {}

    # Score all 6 criteria
    for name, fn, args in [
        ('trend', _score_trend, (asset_data, direction)),
        ('momentum', _score_momentum, (asset_data, direction)),
        ('rsi', _score_rsi, (asset_data, direction)),
        ('structure', _score_structure, (asset_data, direction)),
        ('level', _score_level, (asset_data, direction)),
        ('macro', _score_macro, (macro_data, direction)),
    ]:
        score, evidence, info = fn(*args)
        criteria[name] = {'score': score, 'evidence': evidence}
        meta[name] = info

    total = sum(c['score'] for c in criteria.values())

    # Veto conditions: zero out vetoed criterion scores so the per-criterion
    # breakdown reflects them, then cap `total` at 5 for hard-cap vetoes.
    trend_info = meta.get('trend', {})
    macro_info = meta.get('macro', {})
    rsi_info = meta.get('rsi', {})

    # Track whether a hard-cap at 5 was applied (re-asserted after modifiers)
    hard_cap = False

    if criteria['trend']['score'] == 0 and trend_info.get('all_opposed'):
        # Zero out trend + macro so neither contributes to weighted sum
        criteria['trend']['score'] = 0
        criteria['macro']['score'] = 0
        if total > 5:
            total = 5
        hard_cap = True
        vetoes.append('All TFs unanimously opposed — capped at WEAK')

    if criteria['macro']['score'] == 0:
        if direction == 'long' and macro_info.get('hard_zero_long'):
            # Zero out trend so high trend weight can't override the macro veto
            criteria['trend']['score'] = 0
            criteria['macro']['score'] = 0
            if total > 5:
                total = 5
            hard_cap = True
            vetoes.append('Risk-Off + F&G<20 on LONG — capped at WEAK')
        elif direction == 'short' and macro_info.get('hard_zero_short'):
            criteria['trend']['score'] = 0
            criteria['macro']['score'] = 0
            if total > 5:
                total = 5
            hard_cap = True
            vetoes.append('Risk-On + F&G>80 on SHORT — capped at WEAK')

    if rsi_info.get('exhaustion'):
        total = max(total - 1, 0)
        # Reduce the rsi criterion by 1 so the breakdown also reflects this penalty
        criteria['rsi']['score'] = max(criteria['rsi']['score'] - 1, 0)
        vetoes.append('RSI exhaustion — score reduced by 1')

    # Score modifiers — funding rate (contrarian) + smart-money
    # bias (Hyperliquid). These are MODIFIERS, not criteria: they never touch
    # `criteria`/its count, run AFTER vetoes, and are clamped back into the
    # existing 0-12 scale. Missing data (non-crypto, non-covered asset) is a
    # silent no-op — no modifier, no evidence, no exception.
    modifiers = []

    funding_delta, funding_evidence = _funding_modifier(asset_data.get('funding_rate'), direction)
    if funding_delta:
        total = max(0, min(12, total + funding_delta))
        modifiers.append(funding_evidence)

    sm_delta, sm_evidence = _smart_money_modifier(asset_data.get('smart_money_bias'), direction)
    if sm_delta:
        total = max(0, min(12, total + sm_delta))
        modifiers.append(sm_evidence)

    # Vetoes still win independently of modifiers: re-assert the hard cap so
    # a modifier bump can never lift a hard-veto'd asset back above WEAK.
    if hard_cap and total > 5:
        total = 5

    verdict = _verdict(total)

    # Event-risk verdict cap — an imminent HIGH-impact macro
    # event (FOMC/CPI/NFP/GDP within the configured window) demotes a STRONG
    # verdict to GOOD. Direction-agnostic (applies to long and short alike);
    # only the verdict label is touched — total is
    # never changed. Lower verdicts (GOOD/NEUTRAL/WEAK/AVOID) are unaffected.
    if event_risk and verdict == 'STRONG':
        verdict = 'GOOD'
        modifiers.append(
            f"{event_risk['title']} in {round(event_risk['hours_until'])}h — verdict capped"
        )

    # 1. Setup first
    setup = None
    try:
        setup = classify_setup(asset_data, direction)
    except Exception:
        pass

    # 2. Confluences
    price = asset_data.get('latest_price', 0)
    confluences = None
    try:
        confluences = compute_confluences(asset_data, price, direction)
    except Exception:
        pass

    # 3. Trade plan using both
    trade_plan = None
    try:
        trade_plan = _compute_trade_plan(
            asset_data, direction,
            setup_type=setup['type'] if setup else None,
            confluences=confluences,
        )
    except Exception:
        pass

    result = {
        'total': total,
        'verdict': verdict,
        'criteria': criteria,
        'vetoes': vetoes,
        'modifiers': modifiers,
    }
    if trade_plan:
        result['trade_plan'] = trade_plan
    if setup:
        result['setup'] = setup
    if confluences:
        result['confluences'] = confluences
    return result


def _build_macro_data():
    """Assemble macro context from the shared regime reading.

    Uses carrac.sentiment.regime.get_or_compute_regime() — the SAME
    get-or-compute-and-cache helper the working market_get_sentiment MCP tool and
    /api/market-sentiment already use — instead of a bare cache read. This
    matters because the underlying regime cache (get_cached_regime, 300s
    TTL) is only ever warmed as a side effect of something calling that
    helper; a bare `get_cached_regime() or {}` read (the previous
    implementation here) would silently see a cache MISS most of the time,
    since nothing else refreshes it on a schedule anywhere near the
    scanner's own ~30-45min precompute cadence — observed live: the scanner's precomputed macro block was `regime=Unknown,
    fear_greed=50` (indistinguishable from a genuine neutral reading) while
    the regime cache was cold, and flipped to real values only once
    something else (a /api/market-sentiment hit or a market_get_sentiment MCP
    call) happened to warm it in between precompute passes. No exception was
    ever raised or logged — the macro criterion just silently scored 0
    for the entire universe on most passes.

    If the regime is genuinely unavailable (cache miss AND a fresh compute
    also fails — e.g. upstream data sources down), this returns an EXPLICIT
    'Unavailable' sentinel with `available: False` rather than fabricating
    neutral-looking values, so `_score_macro` (and any other consumer) can
    tell "no reading" apart from "read as neutral". The failure is also
    logged so it is no longer invisible.
    """
    from carrac.sentiment.regime import get_or_compute_regime
    regime = get_or_compute_regime()

    if not regime:
        logger.warning(
            "Scanner macro data unavailable — regime cache miss and fresh "
            "compute also failed; marking macro block unavailable rather "
            "than defaulting to neutral-looking values"
        )
        return {
            'regime': 'Unavailable',
            'regime_score': 0,
            'fear_greed': None,
            'breadth': {},
            'available': False,
        }

    fg = None
    breadth = {}

    # Extract F&G from regime components
    for comp in regime.get('components', []):
        if comp['name'] == 'F&G':
            try:
                fg = int(comp['detail']) if comp['detail'] != 'N/A' else None
            except (ValueError, TypeError):
                fg = None
        elif comp['name'] == 'Breadth':
            try:
                pct_str = comp['detail'].replace('%', '')
                breadth = {'pct': float(pct_str)} if pct_str != 'N/A' else {}
            except (ValueError, TypeError):
                breadth = {}

    return {
        'regime': regime.get('regime', 'Unknown'),
        'regime_score': regime.get('score', 0),
        'fear_greed': fg if fg is not None else 50,
        'breadth': breadth,
        'available': True,
    }


def get_quick_score(symbol, direction=None):
    """Score a single asset by symbol, fetching data from ResponseCache.

    If direction is None, scores both long and short and returns the better one
    with an 'opposite' summary included.

    Returns dict with total (0-12), verdict, criteria, vetoes, setup, trade_plan,
    or None if asset not found in cache.
    """
    try:
        from carrac.precompute import response_cache
    except ImportError:
        return None

    cached = response_cache.get('htf')
    if not cached:
        return None

    asset_data = None
    for a in cached.get('assets', []):
        if a.get('symbol') == symbol:
            asset_data = a
            break
    if not asset_data:
        return None

    macro_data = _build_macro_data()

    if direction and direction in ('long', 'short'):
        result = score_asset(asset_data, macro_data, direction)
        result['symbol'] = symbol
        result['direction'] = direction
        return result

    # Score both, return better + opposite summary
    long_result = score_asset(asset_data, macro_data, 'long')
    short_result = score_asset(asset_data, macro_data, 'short')

    if long_result['total'] >= short_result['total']:
        best = long_result
        best['direction'] = 'long'
        opp_result = short_result
        opp_dir = 'short'
    else:
        best = short_result
        best['direction'] = 'short'
        opp_result = long_result
        opp_dir = 'long'

    best['symbol'] = symbol
    best['opposite'] = {
        'direction': opp_dir,
        'total': opp_result['total'],
        'verdict': opp_result['verdict'],
    }
    return best


def score_all_assets(assets, macro_data=None):
    """Score all assets for both long and short directions.

    Args:
        assets: list of asset dicts from response_cache
        macro_data: dict from _build_macro_data(), or None to auto-build

    Returns:
        {
            'results': [{symbol, price, long, short, best, best_score, best_verdict}],
            'macro': macro_data,
            'event_risk': {title, hours_until} or None,
            'computed_at': ISO timestamp,
        }
    """
    if macro_data is None:
        macro_data = _build_macro_data()

    # Fetch the imminent-event flag ONCE for the whole pass (not
    # per asset — 337 assets * 2 directions would otherwise mean hundreds of
    # lookups; get_imminent_event() is itself TTL-cached as a backstop, but
    # the discipline of calling it once here is what keeps this to one query
    # per pass). Any failure (import or DB) degrades to "no event" — the
    # Scanner is never affected by event-calendar availability.
    event_risk_hours, event_risk_cap_enabled = _event_risk_config()
    event_risk = None
    try:
        from carrac.scanner.event_risk import get_imminent_event
        event_risk = get_imminent_event(hours=event_risk_hours)
    except Exception as e:
        logger.warning(f"Scanner event-risk lookup failed (ignored): {e}")
        event_risk = None

    # The verdict cap (score_asset's event_risk= arg) is gated by the CAP
    # toggle; the informational badge below is attached whenever an event
    # is within the window, regardless of the cap toggle.
    cap_event_risk = event_risk if event_risk_cap_enabled else None

    results = []
    for asset in assets:
        symbol = asset.get('symbol', '')

        # Skip non-tradeable assets (indices, metrics)
        asset_type = asset.get('type', '')
        if asset_type in ('metric', 'crypto_metric'):
            continue

        try:
            long_score = score_asset(asset, macro_data, 'long', event_risk=cap_event_risk)
            short_score = score_asset(asset, macro_data, 'short', event_risk=cap_event_risk)

            if long_score['total'] >= short_score['total']:
                best = 'long'
                best_score = long_score['total']
                best_verdict = long_score['verdict']
            else:
                best = 'short'
                best_score = short_score['total']
                best_verdict = short_score['verdict']

            price = asset.get('latest_price', 0)
            rt = asset.get('realtime') or {}
            if rt.get('price'):
                price = rt['price']

            row = {
                'symbol': symbol,
                'price': price,
                'type': asset_type,
                'category': asset.get('category', ''),
                'long': long_score,
                'short': short_score,
                'best': best,
                'best_score': best_score,
                'best_verdict': best_verdict,
            }
            # Carry staleness, or the board ranks fossils. KAS
            # scored 12/12 off history that stopped 57 days earlier, and the
            # `price` above is the LIVE tick — so the row read as a maximally
            # aligned asset at a price its own evidence never saw. Only ~25 of
            # ~334 rows carry this key, so set it rather than defaulting it.
            if asset.get('data_stale'):
                row['data_stale'] = asset['data_stale']
            results.append(row)
        except Exception as e:
            logger.warning(f"Scanner scoring failed for {symbol}: {e}")
            continue

    # Sort by best_score descending
    results.sort(key=lambda r: r['best_score'], reverse=True)

    return {
        'results': results,
        'macro': macro_data,
        'event_risk': (
            {'title': event_risk['title'], 'hours_until': event_risk['hours_until']}
            if event_risk else None
        ),
        'computed_at': datetime.now().isoformat(),
    }
