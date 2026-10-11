"""
Data assembly functions for composite endpoints.

Each assembler fetches from multiple internal data sources (caches, DB)
and returns a structured dict. No HTTP sub-requests — all direct imports.

Follows the _safe() pattern from carrac/reports/routes.py::morning_report for
graceful degradation when individual sources fail.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

logger = logging.getLogger('carrac.composite')

# Inner thread-pool for assemble_asset_detail parallelism.
# Cap at 4 threads so we don't blow the anyio limiter (10 tokens/worker)
# or the psycopg2 pool when multiple requests are in-flight concurrently.
_DETAIL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='carrac-detail')


# ── Safe wrapper ─────────────────────────────────────────────────

def _safe(name, fn, availability, fallback=None):
    """Run fn(), return result on success or fallback on failure.
    Updates availability dict with 'ok' or 'error'."""
    try:
        val = fn()
        availability[name] = 'ok'
        return val
    except Exception as e:
        logger.warning("Composite: %s failed: %s", name, e)
        availability[name] = 'error'
        return fallback if fallback is not None else ([] if name in ('plays', 'alerts', 'positions') else {})


# ── Shared helpers ───────────────────────────────────────────────

def _get_response_cache():
    from carrac.precompute import response_cache
    return response_cache


def _get_db():
    from carrac.services import get_services
    return get_services().db


def _get_content_db():
    from carrac.content_db import get_content_db
    return get_content_db()


def _get_prices():
    return _get_db().get_combined_crypto_prices() or {}


def _get_htf_by_symbol(symbols=None):
    """Return HTF precomputed cache as {symbol: asset_dict}.

    When ``symbols`` is given, try the per-symbol Redis HASH
    (``mcd:response:htf:by_sym``, written by precompute alongside the blob)
    first via HMGET — moves a few KB per requested symbol instead of the
    whole ~2.1MB htf blob. Falls back to the existing whole-blob path when
    the hash returns nothing for any requested symbol (hash not populated
    yet — deploy ordering, or Redis down) or when ``symbols`` isn't given
    (callers that need the whole universe keyed by every symbol).
    """
    if symbols:
        try:
            from carrac.caches import get_htf_by_symbol_hash
            by_sym = get_htf_by_symbol_hash(symbols)
            # None = hash unavailable -> blob fallback. {} = hash exists but
            # none of these symbols are in the dataset — TRUST it (the blob
            # wouldn't have them either; refetching 2.1MB to learn that costs
            # 2-3s on every stocks/commodities batch group).
            if by_sym is not None:
                return by_sym
        except Exception:
            logger.debug("HTF by-symbol hash read failed; falling back to blob", exc_info=True)
    try:
        cached = _get_response_cache().get('htf')
        if cached:
            return {a.get('symbol', ''): a for a in cached.get('assets', [])}
    except Exception:
        pass
    return {}


def _get_scanner_by_symbol(symbols=None):
    """Return scanner results as {symbol: result_dict}.

    When ``symbols`` is given, try the per-symbol Redis HASH
    (``mcd:c:scanner:by_sym``, written by set_cached_scanner alongside the
    blob) first via HMGET, instead of the whole ~10.8MB scanner blob. Falls
    back to ``get_cached_scanner()`` when the hash returns nothing for any
    requested symbol, or when ``symbols`` isn't given.
    """
    if symbols:
        try:
            from carrac.caches import get_scanner_by_symbol_hash
            by_sym = get_scanner_by_symbol_hash(symbols)
            # None = unavailable -> fallback; {} = populated hash, symbols
            # legitimately absent -> trust it (see htf note above).
            if by_sym is not None:
                return by_sym
        except Exception:
            logger.debug("Scanner by-symbol hash read failed; falling back to blob", exc_info=True)
    from carrac.caches import get_cached_scanner
    data = get_cached_scanner()
    if data:
        return {r.get('symbol', ''): r for r in data.get('results', [])}
    return {}


def _extract_technicals(symbol, htf_by_symbol):
    """Extract technical indicators from HTF precomputed cache for one asset."""
    asset = htf_by_symbol.get(symbol)
    if not asset:
        return None

    cons = asset.get('consensio', {})
    total = cons.get('total', {})

    technicals = {
        'consensio': {},
        'consensio_aligned': total.get('positions', 0) >= total.get('max_positions', 6) - 1,
    }

    for tf in ('1D', '3D', '1W'):
        tf_data = cons.get(tf, {})
        technicals['consensio'][tf.lower()] = tf_data.get('signal', '')

    rsi = asset.get('weekly_rsi')
    if rsi is not None:
        technicals['rsi_weekly'] = round(rsi, 1)

    ema = asset.get('ema_ribbon')
    if ema:
        technicals['ema_ribbon_trend'] = ema.get('trend')

    adx = asset.get('adx')
    if adx and adx.get('adx') is not None:
        technicals['adx'] = round(adx['adx'], 1)

    for field in ('supertrend', 'ttm_squeeze', 'structure'):
        val = asset.get(field)
        if val:
            technicals[field] = val

    bb = asset.get('bb')
    if bb and bb.get('squeeze_pct') is not None:
        technicals['bb'] = {
            'squeeze_pct': round(bb['squeeze_pct'], 1),
            'width_pct': round(bb['width_pct'], 2) if bb.get('width_pct') is not None else None,
        }

    return technicals


def _extract_levels(symbol, htf_by_symbol):
    """Extract key price levels from HTF cache."""
    asset = htf_by_symbol.get(symbol)
    if not asset:
        return None

    levels = {}
    for cpr_key in ('cpr', 'weekly_cpr', 'daily_cpr'):
        cpr = asset.get(cpr_key)
        if cpr:
            levels[cpr_key] = {
                'pivot': cpr.get('pivot'), 'tc': cpr.get('tc'), 'bc': cpr.get('bc'),
                'r1': cpr.get('r1'), 'r2': cpr.get('r2'), 'r3': cpr.get('r3'),
                's1': cpr.get('s1'), 's2': cpr.get('s2'), 's3': cpr.get('s3'),
            }

    # precompute stores these nested under asset_result['key_levels']
    # (carrac/precompute.py::_compute_single_asset_ltf), not top-level — top-level is only a
    # safety-net fallback.
    key_levels = asset.get('key_levels') or {}
    for level_key in ('pdh', 'pdl', 'pwh', 'pwl'):
        val = key_levels.get(level_key)
        if val is None:
            val = asset.get(level_key)
        if val is not None:
            levels[level_key] = val

    return levels or None


def _extract_market(symbol, htf_by_symbol, prices):
    """Extract market data (price + change) for one symbol."""
    result = {'symbol': symbol}

    price_data = prices.get(symbol, {})
    if price_data:
        result['price'] = price_data.get('price') or price_data.get('usd')
        result['change_24h'] = price_data.get('change_pct') or price_data.get('change_24h') or price_data.get('usd_24h_change')
    else:
        asset = htf_by_symbol.get(symbol, {})
        result['price'] = asset.get('latest_price')
        result['change_24h'] = asset.get('change_pct') or asset.get('change_24h')

    # Carried, not acted on. The HTTP composite routes (web UI,
    # TUI) share this shape, so suppression happens at the MCP surface; this
    # layer only stops losing the flag on the way through.
    _stale = (htf_by_symbol.get(symbol) or {}).get('data_stale')
    # A delisted symbol is no longer in the HTF snapshot at
    # all — precompute stops collecting it — so there is no flag left to carry
    # here, while `prices` can still hand back a live tick. That is the
    # classic stale-flag case (a symbol precompute never processed cannot
    # carry a flag precompute writes), so the block is built from reference
    # data instead of inherited.
    if not _stale:
        from carrac.staleness import delisted_block
        _stale = delisted_block(symbol)
    if _stale:
        result['data_stale'] = _stale

    return result


def _extract_scanner(symbol, scanner_by_symbol):
    """Extract scanner data for one symbol."""
    result = scanner_by_symbol.get(symbol)
    if not result:
        return None
    extracted = {
        'long': {'score': result.get('long', {}).get('total'), 'verdict': result.get('long', {}).get('verdict')},
        'short': {'score': result.get('short', {}).get('total'), 'verdict': result.get('short', {}).get('verdict')},
        'best_verdict': result.get('best_verdict'),
    }

    # The verdict and the reason to distrust it must travel
    # together. `assemble_batch` only populates `htf_by_symbol` when 'market'
    # is among the requested blocks, and `_extract_market` is the only other
    # place the flag is attached — so on an include='scanner' call the marker
    # existed nowhere at all, while this function returned a confident verdict
    # scored off dead bars. The scanner row already carries the block
    # (scanner/scorer/engine.py sets it per row for the ~25 of ~334 that need
    # it), so this stops discarding it rather than plumbing a new source.
    if result.get('data_stale'):
        extracted['data_stale'] = result['data_stale']

    return extracted


def _time_since(iso_str):
    """Return human-readable time since an ISO timestamp."""
    if not iso_str:
        return None
    try:
        if isinstance(iso_str, str):
            dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        else:
            dt = iso_str
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        days = delta.days
        hours = delta.seconds // 3600
        if days > 0:
            return f"{days}d {hours}h"
        return f"{hours}h {(delta.seconds % 3600) // 60}m"
    except Exception:
        return None


# ── Asset Detail ─────────────────────────────────────────

def assemble_asset_detail(symbol, user_id):
    """Assemble everything about one asset from caches + DB.

    The 8 independent sub-fetches (4 global cache reads + 4 user-scoped DB
    queries) run concurrently via ``_DETAIL_POOL`` (ThreadPoolExecutor,
    max_workers=4). This mirrors the asyncio.gather pattern used by
    ``_gather_and_assemble`` in fastapi_app/routers/composite.py for the
    /api/batch route.

    Return shape is byte-identical to the original serial implementation.
    """
    symbol = symbol.upper()
    availability = {}

    # ── Define all sub-fetch callables ───────────────────────────────────

    def _get_sentiment():
        from carrac.caches import get_cached_regime
        regime = get_cached_regime() or {}
        return {
            'regime': regime.get('regime'),
            'regime_score': regime.get('score'),
        }

    def _plays():
        from carrac.plays.models import list_plays
        return list_plays(user_id, asset=symbol)

    def _alerts():
        from push_notifications import get_user_alerts
        all_alerts = get_user_alerts(str(user_id))
        return [a for a in all_alerts if a.get('symbol') == symbol]

    def _drawings():
        if user_id is None:
            return {}
        db = _get_content_db()
        all_grouped = db.get_all_drawings_grouped(user_id)
        return all_grouped.get(symbol, {})

    def _positions():
        db = _get_content_db()
        return db.get_open_trades(user_id, asset=symbol)

    # Sub-fetch registry: (name, callable, default_fallback)
    # All 8 are independent — none depends on the result of another.
    sub_fetches = [
        ('htf',     _get_htf_by_symbol,    {}),
        ('scanner', _get_scanner_by_symbol, {}),
        ('prices',  _get_prices,            {}),
        ('sentiment', _get_sentiment,       {}),
        ('plays',   _plays,                 []),
        ('alerts',  _alerts,                []),
        ('drawings', _drawings,             {}),
        ('positions', _positions,           []),
    ]

    # ── Submit all tasks to the pool and collect ─────────────────────────
    results: dict[str, object] = {}

    future_to_name = {
        _DETAIL_POOL.submit(_safe, name, fn, availability, fallback): name
        for name, fn, fallback in sub_fetches
    }
    for future in as_completed(future_to_name):
        name = future_to_name[future]
        try:
            results[name] = future.result()
        except Exception as exc:  # noqa: BLE001 — _safe already catches; this is a safety net
            logger.warning("Composite detail: unexpected future error %s: %s", name, exc)
            results[name] = next(fb for n, _, fb in sub_fetches if n == name)

    htf_by_symbol    = results['htf']
    scanner_by_symbol = results['scanner']
    prices           = results['prices']
    sentiment        = results['sentiment']
    plays            = results['plays']
    alerts           = results['alerts']
    drawings         = results['drawings']
    positions        = results['positions']

    # ── Assemble (pure CPU, unchanged) ───────────────────────────────────
    market = _extract_market(symbol, htf_by_symbol, prices)

    # Enrich positions with live price
    current_price = market.get('price')
    for pos in positions:
        if current_price:
            entry = pos.get('entry_price', 0)
            direction = pos.get('direction', 'long')
            if entry and entry > 0:
                if direction == 'long':
                    pos['unrealized_pnl_pct'] = round((current_price - entry) / entry * 100, 2)
                else:
                    pos['unrealized_pnl_pct'] = round((entry - current_price) / entry * 100, 2)
            pos['current_price'] = current_price

    # Simplify plays for response
    plays_summary = []
    for p in plays:
        conditions = p.get('conditions') or {'rules': []}
        rules = conditions.get('rules', [])
        met = sum(1 for r in rules if r.get('met'))
        plays_summary.append({
            'id': p.get('id'),
            'title': p.get('title'),
            'status': p.get('status'),
            'direction': p.get('direction'),
            'conditions_met': f"{met}/{len(rules)}" if rules else None,
        })

    # Simplify drawings
    drawing_list = []
    for tf, tf_drawings in drawings.items():
        for d in (tf_drawings if isinstance(tf_drawings, list) else []):
            drawing_list.append({
                'type': d.get('drawing_type'),
                'label': d.get('label'),
                'timeframe': tf,
                'price_level': d.get('price_level'),
            })

    return {
        'symbol': symbol,
        'price': market.get('price'),
        'change_24h': market.get('change_24h'),
        # This dict cherry-picks from `market` rather than spreading it, so the
        # staleness flag has to be named here too or it dies one layer short of
        # the surface that needs it.
        **({'data_stale': market['data_stale']} if market.get('data_stale') else {}),
        'technicals': _extract_technicals(symbol, htf_by_symbol),
        'levels': _extract_levels(symbol, htf_by_symbol),
        'scanner': _extract_scanner(symbol, scanner_by_symbol),
        'sentiment': sentiment,
        'plays': plays_summary,
        'alerts': alerts,
        'drawings': drawing_list,
        'positions': positions,
        'availability': availability,
    }


# ── Live Positions ───────────────────────────────────────

def assemble_live_positions(user_id, asset_filter=None):
    """Enrich open trades with live price, PnL, scanner verdict, linked plays."""
    availability = {}

    def _trades():
        db = _get_content_db()
        return db.get_open_trades(user_id, asset=asset_filter)
    open_trades = _safe('positions', _trades, availability, [])

    if not open_trades:
        return {
            'positions': [],
            'portfolio_heat': {'total_exposure': 0, 'long_count': 0, 'short_count': 0, 'notional_usd': 0},
            'availability': availability,
        }

    prices = _safe('prices', _get_prices, availability, {})
    scanner_by_symbol = _safe('scanner', _get_scanner_by_symbol, availability, {})

    def _plays():
        from carrac.plays.models import get_active_plays
        return get_active_plays(user_id)
    active_plays = _safe('plays', _plays, availability, [])

    enriched = []
    for trade in open_trades:
        sym = trade.get('asset', '')
        direction = trade.get('direction', 'long')
        entry = trade.get('entry_price') or 0
        stop = trade.get('stop_price')

        # Current price
        price_data = prices.get(sym, {})
        current_price = price_data.get('price') or price_data.get('usd')
        if not current_price:
            try:
                current_price = _get_db().get_realtime_price(sym)
            except Exception:
                current_price = None

        # PnL
        pnl_pct = None
        if current_price and entry and entry > 0:
            if direction == 'long':
                pnl_pct = round((current_price - entry) / entry * 100, 2)
            else:
                pnl_pct = round((entry - current_price) / entry * 100, 2)

        # R-multiple
        current_rr = None
        if stop and entry and current_price:
            risk = abs(entry - stop)
            if risk > 0:
                reward = abs(current_price - entry)
                current_rr = round(reward / risk, 2)

        # Risk status
        risk_status = 'neutral'
        if stop and current_price:
            if direction == 'long' and current_price < stop:
                risk_status = 'stopped_out'
            elif direction == 'short' and current_price > stop:
                risk_status = 'stopped_out'
        if pnl_pct is not None:
            if pnl_pct > 5:
                risk_status = 'profitable'
            elif pnl_pct < -3 and risk_status != 'stopped_out':
                risk_status = 'at_risk'

        # Scanner verdict
        scanner_result = scanner_by_symbol.get(sym, {})
        scanner_verdict = scanner_result.get(direction, {}).get('verdict') if scanner_result else None

        # Linked play
        linked_play = None
        for p in active_plays:
            if p.get('primary_asset') == sym:
                conditions = p.get('conditions') or {'rules': []}
                rules = conditions.get('rules', [])
                met = sum(1 for r in rules if r.get('met'))
                linked_play = {
                    'id': p['id'],
                    'title': p.get('title'),
                    'conditions_met': f"{met}/{len(rules)}" if rules else None,
                }
                break

        # Dollar-denominated view. size is optional on manual entries, so
        # every derived figure stays None rather than silently becoming 0.
        size = trade.get('size')
        notional = cost_basis = pnl_usd = None
        if size and current_price and entry:
            notional = round(size * current_price, 2)
            cost_basis = round(size * entry, 2)
            move = (current_price - entry) if direction == 'long' else (entry - current_price)
            pnl_usd = round(move * size, 2)

        enriched.append({
            'id': trade.get('id'),
            'asset': sym,
            'direction': direction,
            'entry_price': entry,
            'current_price': current_price,
            'stop_price': stop,
            'target_price': trade.get('target_price'),
            'unrealized_pnl_pct': pnl_pct,
            'current_rr': current_rr,
            'risk_status': risk_status,
            'scanner_verdict': scanner_verdict,
            'linked_play': linked_play,
            'time_in_trade': _time_since(trade.get('opened_at')),
            'setup_type': trade.get('setup_type'),
            'opened_at': trade.get('opened_at'),
            # Rolling MFE/MAE snapshot (null until first precompute fires)
            'mfe_r': trade.get('mfe'),
            'mae_r': trade.get('mae'),
            'mfe_price': trade.get('mfe_price'),
            'mae_price': trade.get('mae_price'),
            'mfe_computed_at': trade.get('mfe_computed_at'),
            'size': size,
            'account_tag': trade.get('account_tag'),
            'notional': notional,
            'cost_basis': cost_basis,
            'unrealized_pnl_usd': pnl_usd,
            # Correlation id back to the drawing this trade was promoted
            # from (spot buys) — lets the sell path find and close THIS
            # trade instead of logging a fresh, unrelated one.
            'drawing_id': trade.get('drawing_id'),
        })

    heat = {
        # NOTE: total_exposure is a COUNT, not a dollar figure. The name is
        # wrong but it is published — carrac/composite/formatters.py::compact_live_positions,
        # carrac/openapi/paths/composite.py (typed integer), and the MCP
        # tool trade_get_positions all depend on it. Renaming it is a
        # deliberate breaking change, not a drive-by fix.
        'total_exposure': len(enriched),
        'long_count': sum(1 for t in enriched if t.get('direction') == 'long'),
        'short_count': sum(1 for t in enriched if t.get('direction') == 'short'),
        # The actual dollar exposure, added alongside.
        'notional_usd': round(
            sum(t['notional'] for t in enriched if t.get('notional') is not None), 2),
    }

    return {'positions': enriched, 'portfolio_heat': heat, 'availability': availability}


# ── Batch Query ──────────────────────────────────────────

VALID_BLOCKS = frozenset({'market', 'scanner', 'sentiment', 'alerts', 'plays', 'positions', 'drawings'})
MAX_BATCH_ASSETS = 10


def assemble_batch(assets, include_blocks, user_id):
    """Multi-asset, multi-data-type assembly. Fetches each source once, filters per-asset."""
    availability = {}

    # Normalize
    assets = [s.upper() for s in assets]
    blocks = set(include_blocks) & VALID_BLOCKS
    if not blocks:
        blocks = {'market', 'scanner'}

    # ── Fetch global data once ──
    htf_by_symbol = {}
    prices = {}
    scanner_by_symbol = {}
    sentiment = {}

    if 'market' in blocks or 'scanner' in blocks:
        # `assets` is bounded (<=MAX_BATCH_ASSETS) and known
        # up-front here — read only those symbols via the per-symbol hash
        # instead of the whole scanner blob.
        scanner_by_symbol = _safe('scanner', lambda: _get_scanner_by_symbol(assets), availability, {})
    if 'market' in blocks:
        htf_by_symbol = _safe('htf', lambda: _get_htf_by_symbol(assets), availability, {})
        prices = _safe('prices', _get_prices, availability, {})
    if 'sentiment' in blocks:
        def _sentiment():
            from carrac.caches import get_cached_regime
            regime = get_cached_regime() or {}
            return {'regime': regime.get('regime'), 'regime_score': regime.get('score')}
        sentiment = _safe('sentiment', _sentiment, availability, {})

    # ── Fetch user data once ──
    all_alerts = []
    all_plays = []
    all_positions = []
    all_drawings = {}

    if 'alerts' in blocks:
        def _alerts():
            from push_notifications import get_user_alerts
            return get_user_alerts(str(user_id))
        all_alerts = _safe('alerts', _alerts, availability, [])

    if 'plays' in blocks:
        def _plays():
            from carrac.plays.models import get_active_plays
            # get_active_plays(None) is a morning-engine helper that returns
            # ALL users' plays. Never pass None from a user-facing path.
            if user_id is None:
                return []
            return get_active_plays(user_id)
        all_plays = _safe('plays', _plays, availability, [])

    if 'positions' in blocks:
        def _positions():
            return _get_content_db().get_open_trades(user_id)
        all_positions = _safe('positions', _positions, availability, [])

    if 'drawings' in blocks:
        def _drawings():
            # drawing_list(user_id=None) skips the user filter entirely.
            # Guard against None coming from shared-admin callers.
            if user_id is None:
                return {}
            return _get_content_db().get_all_drawings_grouped(user_id)
        all_drawings = _safe('drawings', _drawings, availability, {})

    # ── Assemble per-asset ──
    result = {}
    for sym in assets:
        entry = {}
        if 'market' in blocks:
            entry['market'] = _extract_market(sym, htf_by_symbol, prices)
        if 'scanner' in blocks:
            entry['scanner'] = _extract_scanner(sym, scanner_by_symbol)
        if 'alerts' in blocks:
            entry['alerts'] = [a for a in all_alerts if a.get('symbol') == sym]
        if 'plays' in blocks:
            entry['plays'] = [
                {'id': p.get('id'), 'title': p.get('title'), 'status': p.get('status'), 'direction': p.get('direction')}
                for p in all_plays if p.get('primary_asset') == sym
            ]
        if 'positions' in blocks:
            entry['positions'] = [t for t in all_positions if t.get('asset') == sym]
        if 'drawings' in blocks:
            sym_drawings = all_drawings.get(sym, {})
            count = sum(len(v) if isinstance(v, list) else 0 for v in sym_drawings.values())
            entry['drawings'] = {'count': count}
        result[sym] = entry

    global_data = {}
    if 'sentiment' in blocks:
        global_data['sentiment'] = sentiment

    return {'assets': result, 'global': global_data, 'availability': availability}
