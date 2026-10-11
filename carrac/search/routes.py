"""
Search routes: /api/symbol-lookup

Server-side symbol search across curated assets, Binance exchangeInfo,
and Yahoo Finance search API.
"""

import logging
import re

import requests
from flask import Blueprint, jsonify, request

from carrac.extensions import limiter

logger = logging.getLogger('carrac.search')

search_bp = Blueprint('search', __name__)


def _get_db():
    from carrac.services import get_services
    return get_services().db


def _normalize_yahoo_symbol(yahoo_sym: str) -> str:
    """Normalize a Yahoo search-result symbol for use as a Carrac symbol.

    Keeps exchange suffixes ("005930.KS", "SAP.DE") — the chart pipeline
    passes dynamic symbols to Yahoo verbatim, so stripping the suffix made
    every non-US listing unloadable (bare "005930" is Not Found). US
    symbols never carry a dot suffix (class shares use dashes, "BRK-B").
    Caret/=X forms are still stripped: they fail _SAFE_SYMBOL_RE downstream.
    """
    return yahoo_sym.replace('^', '').replace('=X', '').upper()


@search_bp.route('/api/symbol-lookup')
@limiter.limit("20 per minute")
def symbol_lookup():
    """Search for symbols across curated, Binance, and Yahoo Finance.

    Query params:
        q: Search query (min 1 char)
        limit: Max results (default 10, max 20)

    Returns ranked results: [{symbol, name, type, tier, exchange}]
    Curated results are ranked first.
    """
    q = request.args.get('q', '').strip().upper()
    max_results = min(request.args.get('limit', 10, type=int), 20)

    if not q:
        return jsonify([])

    db = _get_db()
    results = []
    seen = set()

    # 1. Search curated symbols (prefix match on symbol + fuzzy on name)
    all_symbols = db.get_all_symbols()
    for sym in all_symbols:
        if len(results) >= max_results:
            break
        sym_upper = sym.upper()
        info = db.get_asset_info(sym_upper)
        name = info.get('name', sym_upper)
        # Match on symbol prefix or name substring
        if sym_upper.startswith(q) or q in name.upper():
            results.append({
                'symbol': sym_upper,
                'name': name,
                'type': info.get('type', db._get_asset_type(sym_upper)),
                'tier': 'curated',
                'exchange': _get_exchange_label(db, sym_upper),
            })
            seen.add(sym_upper)

    # 2. Search Binance exchangeInfo cache (dynamic crypto)
    if len(results) < max_results:
        exchange_info = db.binance.get_exchange_info()
        for base_asset in sorted(exchange_info):
            if len(results) >= max_results:
                break
            if base_asset in seen:
                continue
            if base_asset.startswith(q):
                results.append({
                    'symbol': base_asset,
                    'name': base_asset,
                    'type': 'crypto',
                    'tier': 'dynamic',
                    'exchange': 'Binance',
                })
                seen.add(base_asset)

    # 3. Yahoo Finance search API (stocks, ETFs, indices)
    if len(results) < max_results and len(q) >= 2:
        try:
            resp = requests.get(
                'https://query2.finance.yahoo.com/v1/finance/search',
                params={'q': q, 'quotesCount': max_results, 'newsCount': 0},
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=5,
            )
            if resp.ok:
                quotes = resp.json().get('quotes', [])
                for quote in quotes:
                    if len(results) >= max_results:
                        break
                    yahoo_sym = quote.get('symbol', '')
                    display_sym = _normalize_yahoo_symbol(yahoo_sym)
                    if display_sym in seen:
                        continue
                    # Skip crypto results (already covered by Binance)
                    quote_type = quote.get('quoteType', '').lower()
                    if quote_type == 'cryptocurrency':
                        continue
                    asset_type = 'stock'
                    if quote_type == 'etf' or quote_type == 'mutualfund':
                        asset_type = 'etf'
                    elif quote_type == 'index':
                        asset_type = 'index'
                    elif quote_type == 'currency':
                        asset_type = 'forex'
                    elif quote_type == 'future':
                        asset_type = 'commodity'
                    results.append({
                        'symbol': display_sym,
                        'name': quote.get('shortname') or quote.get('longname') or display_sym,
                        'type': asset_type,
                        'tier': 'dynamic',
                        'exchange': quote.get('exchange', ''),
                    })
                    seen.add(display_sym)
        except Exception as e:
            logger.debug(f"Yahoo search failed for '{q}': {e}")

    return jsonify(results)


_SAFE_SYMBOL_RE = re.compile(r'^[A-Z0-9._-]{1,20}$')


@search_bp.route('/api/quote/<symbol>')
@limiter.limit("30 per minute")
def quote_symbol(symbol: str):
    """Quick quote: price + change for any symbol (curated or dynamic).

    Returns: {symbol, name, price, change_pct, exchange}
    """
    symbol = symbol.strip().upper()
    if not symbol or not _SAFE_SYMBOL_RE.match(symbol):
        return jsonify({'error': 'Invalid symbol'}), 400

    db = _get_db()

    # 1. Curated: already have price in allAssets
    info = db.get_asset_info(symbol)
    if info and info.get('latest_price'):
        change = info.get('realtime', {}).get('change_pct') if info.get('realtime') else None
        return jsonify({
            'symbol': symbol,
            'name': info.get('name', symbol),
            'price': info['latest_price'],
            'change_pct': change,
            'exchange': _get_exchange_label(db, symbol),
        })

    # 2. Binance ticker
    exchange_info = db.binance.get_exchange_info()
    if symbol in exchange_info:
        try:
            resp = requests.get(
                'https://data-api.binance.vision/api/v3/ticker/24hr',
                params={'symbol': symbol + 'USDT'},
                timeout=5,
            )
            if resp.ok:
                data = resp.json()
                return jsonify({
                    'symbol': symbol,
                    'name': symbol,
                    'price': float(data.get('lastPrice', 0)),
                    'change_pct': float(data.get('priceChangePercent', 0)),
                    'exchange': 'Binance',
                })
        except Exception:
            pass

    # 3. Yahoo Finance quote
    try:
        resp = requests.get(
            'https://query1.finance.yahoo.com/v8/finance/chart/' + symbol,
            params={'interval': '1d', 'range': '2d'},
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=5,
        )
        if resp.ok:
            result = resp.json().get('chart', {}).get('result', [])
            if result:
                meta = result[0].get('meta', {})
                price = meta.get('regularMarketPrice', 0)
                prev_close = meta.get('chartPreviousClose') or meta.get('previousClose') or 0
                change = ((price - prev_close) / prev_close * 100) if prev_close else None
                return jsonify({
                    'symbol': symbol,
                    'name': meta.get('shortName') or meta.get('longName') or symbol,
                    'price': price,
                    'change_pct': round(change, 2) if change is not None else None,
                    'exchange': meta.get('exchangeName', ''),
                })
    except Exception as e:
        logger.debug(f"Yahoo quote failed for '{symbol}': {e}")

    return jsonify({'error': f'No quote for {symbol}'}), 404


def _get_exchange_label(db, symbol: str) -> str:
    """Get a human-readable exchange label for a curated symbol."""
    if db._is_crypto(symbol):
        # Non-Binance crypto symbols may be routed via Hyperliquid or CCXT, so
        # no single fallback venue name is accurate here without deeper
        # routing lookups; say 'Other' rather than naming a vendor.
        return 'Binance' if symbol in db.binance_map else 'Other'
    if symbol in db.stock_map:
        stooq = db.stock_map[symbol]
        if stooq.endswith('.US'):
            return 'US'
        elif stooq.endswith('.DE'):
            return 'XETRA'
        elif stooq.endswith('.HK'):
            return 'HKEX'
        return 'US'
    if symbol in db.etf_map:
        return 'US'
    if symbol in db.index_map:
        return 'Index'
    if symbol in db.commodity_map:
        return 'Futures'
    if symbol in db.forex_map:
        return 'Forex'
    return ''


# ── Finnhub proxy ──────────────────────────────────────────
#
# The browser must not open wss://ws.finnhub.io?token=${FINNHUB_API_KEY}
# directly: that exposes the key to every logged-in user. The worker
# process maintains the WS server-side and writes each tick to Redis
# (mcd:finnhub:tick:<SYMBOL>); browsers poll this endpoint instead.

@search_bp.route('/api/finnhub/quotes')
@limiter.limit("60 per minute")
def finnhub_quotes():
    """Return the latest cached Finnhub ticks.

    Query param: symbols=AAPL,TSLA,... (optional — defaults to all cached)
    """
    import json as _json
    import os

    try:
        import redis
    except ImportError:
        return jsonify({'ticks': {}, 'error': 'redis unavailable'}), 503

    redis_url = os.environ.get('REDIS_URL', '')
    if not redis_url:
        return jsonify({'ticks': {}, 'error': 'redis unconfigured'}), 503

    requested = (request.args.get('symbols') or '').strip()
    if requested:
        symbols = [s for s in (x.strip().upper() for x in requested.split(',')) if s and _SAFE_SYMBOL_RE.match(s)]
    else:
        # Default: the curated Finnhub symbol set. Match data_sources.finnhub_ws.
        from data_sources.finnhub_ws import FINNHUB_SYMBOLS
        symbols = list(FINNHUB_SYMBOLS)

    if not symbols:
        return jsonify({'ticks': {}})

    try:
        r = redis.from_url(redis_url, socket_connect_timeout=2, decode_responses=True)
        pipe = r.pipeline()
        for sym in symbols:
            pipe.get(f'mcd:finnhub:tick:{sym}')
        results = pipe.execute()
    except Exception as exc:
        logger.warning("Finnhub proxy redis fetch failed: %s", exc)
        return jsonify({'ticks': {}, 'error': 'redis read failed'}), 503

    ticks = {}
    for sym, raw in zip(symbols, results):
        if not raw:
            continue
        try:
            ticks[sym] = _json.loads(raw)
        except Exception:
            continue

    return jsonify({'ticks': ticks})
