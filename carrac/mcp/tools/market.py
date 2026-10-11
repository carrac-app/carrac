"""MCP tools: Market Data (6 tools)."""

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.formatters import SCANNER_CONDITION_TOTAL


def _stale_symbols() -> dict:
    """``{symbol: staleness_block}`` for every asset whose history has stopped.

    Read from the HTF precompute cache, which is where ``data_stale`` has
    always been written. Non-fatal: an unavailable cache means "nothing known
    to be stale", which is the earlier behaviour rather than a new
    failure mode.
    """
    try:
        from carrac.precompute import response_cache
        from carrac.staleness import history_is_dead

        htf = response_cache.get('htf') or {}
        return {
            a.get('symbol'): a['data_stale']
            for a in htf.get('assets', [])
            if a.get('symbol') and history_is_dead(a.get('data_stale'))
        }
    except Exception:
        return {}


@mcp_tool(
    'market_get_chart',
    'Lightweight snapshot for one symbol from the precomputed HTF (daily) cache — '
    'returns instantly without DB queries. '
    'Returns: price, change_pct, consensio {total, signal}, rsi, trend, structure, '
    'bb_squeeze_pct, key_levels {pdh, pdl, pwh, pwl, monthly_pivot}, computed_at. '
    'Use this as the fast first-pass for a single asset; prefer market_get_detail '
    'when you also need scanner score, plays, alerts, drawings, or live positions. '
    'Data freshness: precompute cycle (~30 min); on a cold cache a reduced Redis '
    'snapshot may be returned instead (fields differ: rsi_zone, bb_squeeze, '
    'adx_regime, cpr_position, no key_levels). '
    'STALE ASSETS: a response carrying data_stale {stale, reason, last_bar_date, age_days, note} '
    'has no collected price history since last_bar_date; only price and change_pct are returned, '
    'and every derived field (consensio, rsi, trend, structure, key_levels) is withheld. '
    'If symbol is unknown, call market_search_symbol first.',
    {
        'type': 'object',
        'properties': {
            'symbol': {
                'type': 'string',
                'description': 'Asset symbol (e.g. BTC, ETH, SPX, AAPL)',
            },
        },
        'required': ['symbol'],
    },
    min_tier='free',
)
def market_get_chart(arguments):
    symbol = arguments.get('symbol', '').upper().strip()
    if not symbol:
        raise ToolError('symbol is required')

    # Try combined HTF cache first (populated by precompute thread)
    try:
        from carrac.precompute import response_cache
        htf = response_cache.get('htf')
        if htf:
            for asset in htf.get('assets', []):
                if asset.get('symbol') == symbol:
                    # consensio/rsi/trend/structure/key_levels are
                    # all read off the daily series. With no recent bars there
                    # is no reading to reduce — only a price and a reason.
                    from carrac.staleness import history_is_dead
                    if history_is_dead(asset.get('data_stale')):
                        from cache_manager import get_cached_crypto_prices
                        _live = (get_cached_crypto_prices() or {}).get(symbol, {})
                        return {
                            'symbol': symbol,
                            'price': _live.get('price') or asset.get('latest_price'),
                            'change_pct': _live.get('change_pct'),
                            'data_stale': asset['data_stale'],
                            'computed_at': htf.get('computed_at', ''),
                        }
                    cons = asset.get('consensio', {})
                    total_cons = cons.get('total', {})
                    bb = asset.get('bb') or {}
                    kl = asset.get('key_levels') or {}
                    return {
                        'symbol': symbol,
                        'price': asset.get('latest_price'),
                        'change_pct': asset.get('change_pct'),
                        'consensio': {
                            'total': total_cons.get('positions', total_cons) if isinstance(total_cons, dict) else total_cons,
                            'signal': cons.get('signal'),
                        },
                        'rsi': asset.get('rsi'),
                        'trend': asset.get('trend'),
                        'structure': asset.get('structure'),
                        'bb_squeeze_pct': bb.get('squeeze_pct'),
                        'key_levels': {
                            k: v for k, v in {
                                'pdh': kl.get('pdh'), 'pdl': kl.get('pdl'),
                                'pwh': kl.get('pwh'), 'pwl': kl.get('pwl'),
                                'monthly_pivot': kl.get('monthly_pivot'),
                            }.items() if v is not None
                        },
                        'computed_at': htf.get('computed_at', ''),
                    }
    except Exception:
        pass

    # Fallback: per-asset fingerprint cache (Redis, always available)
    try:
        import json
        import redis
        import os
        r = redis.from_url(os.environ.get('REDIS_URL', ''))
        raw = r.get(f'mcd:fp:htf:{symbol}')
        if raw:
            fp = json.loads(raw)
            # Get price from prices cache
            from cache_manager import get_cached_crypto_prices
            prices = get_cached_crypto_prices() or {}
            p = prices.get(symbol, {})
            return {
                'symbol': symbol,
                'price': p.get('price'),
                'change_pct': p.get('change_pct'),
                'consensio': fp.get('consensio'),
                'rsi_zone': fp.get('rsi_zone'),
                'trend': fp.get('trend') or fp.get('ema_ribbon'),
                'structure': fp.get('structure'),
                'bb_squeeze': fp.get('bb_squeeze'),
                'macd': fp.get('macd'),
                'adx_regime': fp.get('adx_regime'),
                'cpr_position': fp.get('cpr_position'),
            }
    except Exception:
        pass

    raise ToolError(f'No data for {symbol}. Try market_search_symbol to check availability.')


@mcp_tool(
    'market_get_detail',
    'Heavier composite fetch for one asset — hits assembler + DB. '
    'Returns: symbol, price, change_24h, technicals {consensio, aligned, rsi_weekly, ema_trend}, '
    'scanner {bullish {conditions_met, conditions_total}, bearish {conditions_met, conditions_total}}, '
    'sentiment summary, '
    'plays_count + top-3 plays {id, title, status}, alerts_count, drawings_count, '
    'positions_count + top-3 positions {asset, direction, pnl_pct, risk}, '
    'facts {deterministic per-asset facts bundle: ma_stack, stretch, levels/clusters, streaks, '
    'decisive_close, rsi, volume, candle, weekly} or null when cache is cold. '
    'STALE ASSETS: if the response carries data_stale {stale, reason, last_bar_date, age_days, note}, '
    'no price data has been collected for this asset since last_bar_date, and technicals, scanner '
    'and facts are ALL null — not missing by accident, withheld because any reading would describe '
    'a market that stopped updating. price and change_24h are still live and safe to quote. '
    'Report the asset as having no current technical data; do not infer trend, levels or momentum '
    'for it from any other source in this response. '
    'Use when you need the full picture in one call; prefer market_get_chart for price + '
    'technicals-only lookups (faster, no DB). '
    'Follow up with play_list, trade_get_positions, or drawing_list for full detail lists.',
    {
        'type': 'object',
        'properties': {
            'symbol': {
                'type': 'string',
                'description': 'Asset symbol (e.g. BTC, ETH, SPX)',
            },
        },
        'required': ['symbol'],
    },
    min_tier='free',
)
def market_get_detail(arguments):
    from carrac.composite.assemblers import assemble_asset_detail
    from carrac.composite.formatters import compact_asset_detail
    from carrac.facts.wire import get_asset_facts

    symbol = arguments.get('symbol', '').upper().strip()
    if not symbol:
        raise ToolError('symbol is required')

    result = compact_asset_detail(assemble_asset_detail(symbol, user_id=None))

    # The exact call that fabricated MKR's breakdown. The live
    # price is genuinely known and stays; technicals, scanner conditions and
    # the facts bundle are all read off bars that stopped, so they are nulled
    # rather than flagged. Erroring the whole tool would also throw away the
    # one number an agent can honestly report.
    # An asset precompute never processed cannot carry a
    # `data_stale` flag, because precompute is what writes it. FTM (zero OHLCV
    # rows) was served with a live price and a 362% 24h change, unmarked.
    #
    # The facts bundle is the corroborating signal, and it is fetched FIRST for
    # that reason: it lives in Redis independently of the HTF cache, so
    # "technicals and scanner are both empty" alone would also be true for a
    # momentarily cold cache — and nulling a perfectly good bundle on that
    # basis would be a worse bug than the one being fixed. All three empty is
    # unambiguous.
    # Asked FIRST, and not gated on the three-empty test
    # below. A delisted symbol is dropped from the precompute universe, so it
    # arrives here looking exactly like a cold cache; `no_history_block` would
    # then say "no price history is available", which is both misleading (the
    # bars are retained) and silent about the one thing worth knowing.
    from carrac.staleness import delisted_block, history_is_dead, no_history_block
    result['facts'] = get_asset_facts(symbol)
    _delisted = delisted_block(symbol)
    if _delisted is not None:
        result['data_stale'] = _delisted
    elif not result.get('data_stale') and not result.get('technicals') \
            and not result.get('scanner') and not result.get('facts'):
        result['data_stale'] = no_history_block(symbol)

    if history_is_dead(result.get('data_stale')):
        result['technicals'] = None
        result['scanner'] = None
        result['facts'] = None
        return result

    # Non-advice framing on the agent surface. The shared composite
    # formatter still emits the legacy long/short + verdict shape because the
    # HTTP composite routes (web UI, TUI) consume it — so reshape here rather
    # than upstream.
    scanner_block = result.get('scanner')
    if isinstance(scanner_block, dict):
        result['scanner'] = {
            label: {
                'conditions_met': (scanner_block.get(raw_key) or {}).get('score'),
                'conditions_total': SCANNER_CONDITION_TOTAL,
            }
            for raw_key, label in (('long', 'bullish'), ('short', 'bearish'))
        }
    # result['facts'] was already set above — the no-history check needs it as
    # corroboration, so it is fetched before that branch rather than here.
    try:
        from carrac.facts.cost import bump_usage
        from carrac.mcp.runtime import get_mcp_caller_tier
        bump_usage('mcp_asset_detail', get_mcp_caller_tier())
    except Exception:
        pass
    return result


@mcp_tool(
    'market_get_overview',
    'Ranked market snapshot from the scanner cache — best entry point for broad analysis. '
    'Carrac reports OBSERVATIONS, never signals or recommendations. '
    'Returns: results [{symbol, price, bullish {conditions_met, conditions_total}, '
    'bearish {conditions_met, conditions_total}}], macro context, computed_at, '
    'result_count, total_available, excluded_stale. '
    'excluded_stale lists symbols withheld from the ranking because their price history '
    'stopped updating — their condition counts would have been read off bars that no longer '
    'arrive. They are absent from results by design, not missing data. '
    '"bullish"/"bearish" describe the conditions observed in the market — they are not '
    'position suggestions, and no entries, exits or targets are published. '
    'Default limit=20 (≈4KB); pass limit=100 for the widest view. '
    'Filter by min_conditions (0-12) and/or alignment (bullish/bearish/any). '
    'Use scanner_get_screen when you also want the per-criterion evidence behind each count. '
    'Data freshness: scanner recompute cycle (~30 min).',
    {
        'type': 'object',
        'properties': {
            'alignment': {
                'type': 'string',
                'enum': ['bullish', 'bearish', 'any'],
                'description': 'Which observed alignment to filter and sort on '
                               '(default "any" = whichever side has more conditions met).',
            },
            'min_conditions': {
                'type': 'integer',
                'minimum': 0,
                'maximum': 12,
                'description': 'Only return assets meeting at least this many of the 12 '
                               'conditions (default 0 = no filter).',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max assets to return (default 20, max 100)',
            },
        },
    },
    min_tier='free',
)
def market_get_overview(arguments):
    from carrac.caches import get_cached_scanner
    from carrac.formatters import evidence_scanner
    from carrac.staleness import history_is_dead

    scanner_data = get_cached_scanner()
    if not scanner_data:
        raise ToolError('Scanner data not yet available — server is warming up')

    data = dict(scanner_data)
    results = data.get('results', [])

    # Sibling of scanner_get_screen — same cache, same blind spot.
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

    # Sort by conditions met so the default top-N is the most active rows,
    # not cache order.
    results = sorted(results, key=_met, reverse=True)

    # Cap result count — the full universe is ~340 assets, ~50KB of JSON.
    # 20 keeps the response under 4KB which is appropriate for a starting-point tool.
    # A non-integer limit (e.g. "twenty") must not be silently swallowed and
    # replaced with the default, or the caller cannot tell their argument was
    # discarded. Mirrors
    # scanner_get_screen's limit handling (scanner.py): reject a non-integer
    # with a ToolError naming the expected type; an out-of-range integer is
    # still clamped to [1, 100], same as quant_get_anomalies clamps `top`.
    limit = arguments.get('limit')
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ToolError('limit must be an integer.')
        limit = max(1, min(limit, 100))
    else:
        limit = 20
    data['results'] = results[:limit]
    data['result_count'] = len(results[:limit])
    data['total_available'] = len(results)
    # include_evidence=False: this is the light ranked snapshot; scanner_get_screen is
    # the tool that carries the per-criterion evidence strings.
    out = evidence_scanner(data, include_evidence=False)
    out['excluded_stale'] = excluded_stale
    return out


@mcp_tool(
    'market_search_symbol',
    'Resolve a symbol or name query to Carrac-compatible asset symbols. '
    'Searches in order: curated watchlist → Binance exchange info → Yahoo Finance. '
    'Returns: {results: [{symbol, name, type (crypto/stock/etf/index/forex/commodity), tier (curated/dynamic)}], count}. '
    'A result carrying data_stale {stale, reason, last_bar_date, age_days, note} has stopped '
    'collecting price data — market_get_detail will return no technicals for it, so prefer a '
    'different symbol or tell the user the feed is dead rather than spending a lookup on it. '
    'Call this before market_get_chart or market_get_detail when you are unsure if a symbol is available. '
    'Curated symbols have full scanner + indicator coverage; dynamic symbols may have limited data.',
    {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'Search query (e.g. "BTC", "AAPL", "gold")',
            },
            'limit': {
                'type': 'integer',
                'description': 'Max results (default 10, max 20)',
            },
        },
        'required': ['query'],
    },
    min_tier='free',
)
def market_search_symbol(arguments):
    from carrac.services import get_services
    import requests as http_requests
    import logging

    query = arguments.get('query', '').strip().upper()
    max_results = min(arguments.get('limit', 10), 20)
    if not query:
        raise ToolError('query is required')

    db = get_services().db
    results = []
    seen = set()
    # The cheapest save available: an agent resolving a symbol
    # learns the feed is dead here, before it spends a market_get_detail call
    # forming a view on it.
    stale = _stale_symbols()

    # 1. Curated symbols
    for sym in db.get_all_symbols():
        if len(results) >= max_results:
            break
        sym_upper = sym.upper()
        info = db.get_asset_info(sym_upper)
        name = info.get('name', sym_upper)
        if sym_upper.startswith(query) or query in name.upper():
            row = {
                'symbol': sym_upper,
                'name': name,
                'type': info.get('type', 'crypto'),
                'tier': 'curated',
            }
            if sym_upper in stale:
                row['data_stale'] = stale[sym_upper]
            results.append(row)
            seen.add(sym_upper)

    # 2. Binance
    if len(results) < max_results:
        try:
            exchange_info = db.binance.get_exchange_info()
            for base_asset in sorted(exchange_info):
                if len(results) >= max_results:
                    break
                if base_asset in seen:
                    continue
                if base_asset.startswith(query):
                    results.append({
                        'symbol': base_asset,
                        'name': base_asset,
                        'type': 'crypto',
                        'tier': 'dynamic',
                    })
                    seen.add(base_asset)
        except Exception:
            pass

    # 3. Yahoo Finance
    if len(results) < max_results and len(query) >= 2:
        try:
            resp = http_requests.get(
                'https://query2.finance.yahoo.com/v1/finance/search',
                params={'q': query, 'quotesCount': max_results, 'newsCount': 0},
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=5,
            )
            if resp.ok:
                for quote in resp.json().get('quotes', []):
                    if len(results) >= max_results:
                        break
                    yahoo_sym = quote.get('symbol', '')
                    display_sym = yahoo_sym.split('.')[0].replace('^', '').replace('=X', '').upper()
                    if display_sym in seen:
                        continue
                    if quote.get('quoteType', '').lower() == 'cryptocurrency':
                        continue
                    qt = quote.get('quoteType', '').lower()
                    asset_type = {'etf': 'etf', 'index': 'index', 'currency': 'forex',
                                  'future': 'commodity'}.get(qt, 'stock')
                    results.append({
                        'symbol': display_sym,
                        'name': quote.get('shortname') or display_sym,
                        'type': asset_type,
                        'tier': 'dynamic',
                    })
                    seen.add(display_sym)
        except Exception:
            logging.getLogger('carrac.mcp').debug("Yahoo search failed for %s", query)

    return {'results': results, 'count': len(results)}


@mcp_tool(
    'market_get_sentiment',
    'Crypto market-wide sentiment snapshot. '
    'Returns: fear_greed (0–100), fear_greed_label, funding_rate_pct (BTC), '
    'mvrv_zscore + mvrv_zone, alt_season_index + alt_season label, '
    'regime + regime_score, long_short ratios {BTC, ETH, SOL}, '
    'open_interest_usd + oi_change_pct. '
    'Cache refreshes automatically every ~2 minutes; on cache miss builds inline (~25s). '
    'On partial failure returns available fields with status="partial". '
    'Not asset-specific — for single-asset technicals use market_get_chart.',
    {
        'type': 'object',
        'properties': {},
    },
    min_tier='free',
)
def market_get_sentiment(arguments):
    from carrac.formatters import compact_sentiment
    import time

    # Cross-worker shared cache — populated by /api/market-sentiment regardless
    # of which gunicorn worker served the browser request.
    try:
        from carrac.caches import get_cached_sentiment
        shared = get_cached_sentiment()
        if shared:
            return compact_sentiment(shared)
    except Exception:
        pass

    # Legacy in-process cache (fast path for the same worker)
    try:
        from carrac.sentiment.routes import _sentiment_response_cache, _sentiment_response_lock
        with _sentiment_response_lock:
            cached = _sentiment_response_cache.get('data')
            if cached and (time.time() - cached[0]) < 600:
                return compact_sentiment(cached[1])
    except Exception:
        pass

    # Last-resort: build it inline so the MCP tool is never empty.
    # Mirrors morning_engine/data_assembly.py — calls the same fetchers.
    try:
        result = _build_sentiment_inline()
        if result:
            try:
                from carrac.caches import set_cached_sentiment
                set_cached_sentiment(result)
            except Exception:
                pass
            return compact_sentiment(result)
    except Exception:
        pass

    # Fallback: try regime cache at minimum
    try:
        from carrac.caches import get_cached_regime
        regime = get_cached_regime()
        if regime:
            return {
                'status': 'partial',
                'sentiment': {
                    'regime': regime.get('regime'),
                    'regime_score': regime.get('score'),
                },
                'note': 'Full sentiment data not cached — partial data from regime engine',
            }
    except Exception:
        pass

    raise ToolError('Sentiment data not yet available — it refreshes automatically every 2 minutes')


def _build_sentiment_inline():
    """Build a sentiment snapshot on demand by calling the same fetchers as the
    HTTP endpoint. Used as a last-resort fallback when no cache has been
    populated yet (e.g. fresh worker, no browser traffic). Best-effort — fields
    that fail return their fallback shape rather than aborting the whole call.
    """
    from datetime import datetime
    from concurrent.futures import ThreadPoolExecutor
    from carrac.services import get_services
    from carrac.sentiment.routes import (
        _fetch_fear_greed,
        _format_ls_ratio,
        _get_fear_greed_color,
        _get_alt_season_color,
        _get_mvrv_color,
        _get_funding_rate_color,
    )
    db = get_services().db

    def _fg():
        fg = _fetch_fear_greed()
        value = fg.get('value')
        out = {
            'value': value,
            'classification': fg.get('classification'),
            'color': _get_fear_greed_color(value),
        }
        if value is None:
            out['error'] = fg.get('error') or 'unavailable'
        return out

    def _ls():
        ratios = db.fetch_long_short_ratio(['BTC', 'ETH', 'SOL'])
        return {
            sym: _format_ls_ratio(ratios.get(sym, {}))
            for sym in ('BTC', 'ETH', 'SOL')
        }

    def _alt():
        alt = db.calculate_alt_season_index()
        # Never substitute 50/neutral — an LLM reading this tool
        # cannot tell an invented reading from a measured one.
        index = alt.get('index')
        out = {
            'index': index,
            'season': alt.get('season'),
            'outperformers': alt.get('outperformers'),
            'total_analyzed': alt.get('total_analyzed'),
            'color': _get_alt_season_color(index),
        }
        if index is None:
            out['error'] = alt.get('error') or 'unavailable'
        return out

    def _mvrv():
        m = db.fetch_mvrv_zscore()
        return {
            'zscore': m.get('zscore'),
            'zone': m.get('zone', 'neutral'),
            'color': _get_mvrv_color(m.get('zscore', 0)),
        }

    def _fr():
        fr = db.get_funding_rate('BTC')
        return {
            'rate': fr,
            'rate_pct': round(fr * 100, 4) if fr else None,
            'color': _get_funding_rate_color(fr),
        }

    def _oi():
        oi = db.fetch_open_interest('BTC')
        return {
            'value': oi.get('value'),
            'value_usd': oi.get('value_usd'),
            'change_pct': oi.get('change_pct'),
            'color': '#adb5bd',
        }

    def _regime():
        from carrac.sentiment.regime import get_or_compute_regime
        regime = get_or_compute_regime()
        if not regime:
            # get_or_compute_regime() returns None (never raises) on total
            # failure — raise so the ThreadPoolExecutor `except Exception`
            # below substitutes fallbacks['regime'] (previous behaviour).
            raise RuntimeError('Regime unavailable — cache miss and fresh compute failed')
        return regime

    fallbacks = {
        'fear_greed': {'value': None, 'classification': None, 'error': True},
        'long_short': {'error': True},
        'alt_season': {'index': None, 'season': None, 'error': True},
        'mvrv_zscore': {'zscore': None, 'zone': 'unknown', 'error': True},
        'funding_rate': {'rate': None, 'rate_pct': None, 'error': True},
        'open_interest': {'value': None, 'value_usd': None, 'change_pct': None, 'error': True},
        'regime': {'regime': 'N/A', 'score': 0, 'color': '#8b949e', 'components': []},
    }
    tasks = {
        'fear_greed': _fg,
        'long_short': _ls,
        'alt_season': _alt,
        'mvrv_zscore': _mvrv,
        'funding_rate': _fr,
        'open_interest': _oi,
        'regime': _regime,
    }
    sentiment = {}
    with ThreadPoolExecutor(max_workers=7) as ex:
        futs = {k: ex.submit(fn) for k, fn in tasks.items()}
        for k, f in futs.items():
            try:
                sentiment[k] = f.result(timeout=25)
            except Exception:
                sentiment[k] = fallbacks[k]
    return {
        'status': 'ok',
        'timestamp': datetime.now().isoformat(),
        'sentiment': sentiment,
    }


@mcp_tool(
    'market_get_calendar',
    'Query macro economic events from the quant DB (quant.events table). '
    'Returns: {events: [{date (ISO), type, title, impact}], count}. '
    'Default window: 7 days back → 30 days forward. Override with from_date/to_date (YYYY-MM-DD). '
    'Covers FOMC, CPI, NFP, GDP releases, and other high-impact macro catalysts. '
    'Use before market_get_sentiment or trade planning to check upcoming risk events.',
    {
        'type': 'object',
        'properties': {
            'from_date': {
                'type': 'string',
                'description': 'Start date YYYY-MM-DD (default: 7 days ago)',
            },
            'to_date': {
                'type': 'string',
                'description': 'End date YYYY-MM-DD (default: 30 days ahead)',
            },
        },
    },
    min_tier='free',
)
def market_get_calendar(arguments):
    from datetime import datetime, timedelta, timezone

    def _validate_date(value, param):
        """Reject a malformed date BEFORE it reaches the query.

        Passing it through let Postgres raise, and the raw driver error —
        including the literal SQL and the quant.events schema — was returned
        verbatim to the MCP caller.
        """
        try:
            datetime.strptime(value, '%Y-%m-%d')
        except (TypeError, ValueError):
            raise ToolError(
                f'{param} must be a date in YYYY-MM-DD format (got {value!r}).'
            )
        return value

    from_date = arguments.get('from_date')
    to_date = arguments.get('to_date')
    if not from_date:
        from_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d')
    else:
        from_date = _validate_date(from_date, 'from_date')
    if not to_date:
        to_date = (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%d')
    else:
        to_date = _validate_date(to_date, 'to_date')

    if from_date > to_date:
        raise ToolError(
            f'from_date ({from_date}) is after to_date ({to_date}) — '
            'the range is empty. Swap them.'
        )

    try:
        from carrac.db_pool import get_quant_conn as get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT ts, event_type, title, impact FROM quant.events '
                'WHERE ts BETWEEN %s AND %s ORDER BY ts',
                (from_date, to_date),
            )
            events = [
                {
                    'date': row['ts'].isoformat() if hasattr(row['ts'], 'isoformat') else str(row['ts']),
                    'type': row['event_type'],
                    'title': row['title'],
                    'impact': row['impact'],
                }
                for row in cur.fetchall()
            ]
        return {'events': events, 'count': len(events)}
    except ToolError:
        raise
    except Exception:
        # Never surface the raw driver error — it carries the literal SQL and
        # the quant.events schema.
        raise ToolError(
            'Economic calendar query failed. Check that from_date/to_date are '
            'YYYY-MM-DD; if they are, the calendar store is unavailable.'
        )
