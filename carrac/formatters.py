"""
Compact response formatters for LLM/MCP consumers.

Each function takes the full response dict from an existing endpoint
and returns a compact version optimized for token efficiency (~800 bytes
vs ~500KB for charts). Used by ?format=compact query parameter and
future MCP tools.

Keys are readable (not abbreviated like the widget format) because
LLMs parse readable keys better and bandwidth is negligible at <1KB.
"""

# Sentinel used by is_compact_requested / maybe_compact to distinguish
# "Flask caller that omits format_param" from "FastAPI caller with format=None".
_UNSET = object()


def compact_chart(data: dict) -> dict:
    """Chart response → ~800 byte summary.

    Keeps: symbol, timeframe, latest price, change%, consensio states,
    RSI, MACD signal, trend, key levels (CPR, PDH/PDL, PWH/PWL).
    Drops: OHLCV arrays, per-candle indicator arrays, CPR history, volume.
    """
    ohlcv = data.get('ohlcv', [])
    last_close = None
    prev_close = None
    if ohlcv:
        # Handle both object and columnar format
        if isinstance(ohlcv, list) and ohlcv:
            if isinstance(ohlcv[-1], dict):
                last_close = ohlcv[-1].get('close')
                if len(ohlcv) >= 2:
                    prev_close = ohlcv[-2].get('close')
            elif isinstance(ohlcv, dict) and 'close' in ohlcv:
                # Columnar format
                closes = ohlcv.get('close', [])
                if closes:
                    last_close = closes[-1]
                    if len(closes) >= 2:
                        prev_close = closes[-2]

    change_pct = None
    if last_close and prev_close and prev_close != 0:
        change_pct = round((last_close - prev_close) / prev_close * 100, 2)

    # Extract indicator states
    indicators = data.get('indicators', {})
    consensio = indicators.get('consensio', {})

    # Extract latest RSI value from array
    rsi_data = data.get('rsi', [])
    rsi_latest = None
    if isinstance(rsi_data, list) and rsi_data:
        last_rsi = rsi_data[-1]
        if isinstance(last_rsi, dict):
            rsi_latest = last_rsi.get('value')
        elif isinstance(last_rsi, (int, float)):
            rsi_latest = last_rsi

    # Extract key levels from CPR
    key_levels = {}
    for cpr_key, label in [('cpr', 'monthly'), ('weekly_cpr', 'weekly'), ('daily_cpr', 'daily')]:
        cpr = data.get(cpr_key)
        if cpr and isinstance(cpr, dict):
            key_levels[f'{label}_pivot'] = cpr.get('pivot')
            key_levels[f'{label}_r1'] = cpr.get('r1')
            key_levels[f'{label}_s1'] = cpr.get('s1')

    result = {
        'symbol': data.get('symbol'),
        'timeframe': data.get('timeframe'),
        'price': last_close,
        'change_pct': change_pct,
        'consensio': _compact_consensio(consensio),
        'rsi': round(rsi_latest, 1) if rsi_latest is not None else None,
        'key_levels': {k: v for k, v in key_levels.items() if v is not None},
        'candle_count': len(ohlcv) if isinstance(ohlcv, list) else None,
        'tier': data.get('tier'),
    }

    # Add extended indicator snapshot if present
    ext = data.get('indicators_ext', {})
    if ext:
        result['indicators_ext'] = ext

    return result


def compact_scanner(data: dict) -> dict:
    """Scanner results → scores + verdicts only.

    Keeps: per-asset symbol, price, best_verdict, long/short score+verdict.
    Drops: per-criterion evidence strings, confluence zone details.
    """
    compact_results = []
    for r in data.get('results', []):
        item = {
            'symbol': r.get('symbol'),
            'price': r.get('price'),
            'best_verdict': r.get('best_verdict'),
        }
        for direction in ('long', 'short'):
            d = r.get(direction, {})
            item[f'{direction}_score'] = d.get('total')
            item[f'{direction}_verdict'] = d.get('verdict')
        compact_results.append(item)

    return {
        'results': compact_results,
        'macro': data.get('macro', {}),
        'computed_at': data.get('computed_at', ''),
        'event_risk': data.get('event_risk'),
    }


# 6 scored criteria (trend, momentum, rsi, structure, level, macro), 0-2 each.
SCANNER_CONDITION_TOTAL = 12

# Raw engine key -> the descriptive label used on the evidence-first surface.
# "long"/"short" describe a POSITION the reader might take (advice-adjacent);
# "bullish"/"bearish" describe the CONDITIONS observed in the market. The
# non-advice reframe turns on exactly that distinction.
_ALIGNMENT_LABELS = (('long', 'bullish'), ('short', 'bearish'))


def evidence_scanner(data: dict, include_evidence: bool = True) -> dict:
    """Scanner results → evidence-first shape (non-advice framing).

    The v3 reframe made the scanner report *observations and the evidence
    behind them* rather than signals/setups/calls, and the lawyer-reviewed
    envelope (MAR / Reg 2016/958 / MiFID II) rests on that. ``compact_scanner``
    predates it: it keeps the verdict labels the reframe retired and drops the
    per-criterion evidence the reframe made the product. This is the inverse,
    and it is what the MCP/agent surface serves.

    Keeps: symbol, price, per-alignment conditions-met count + the evidence
    string for each criterion, plus any vetoes (themselves observations).
    Drops: ``verdict`` / ``best_verdict`` (evaluative ranking), long/short
    position vocabulary, and ``trade_plan`` — which is INTERNAL ONLY and must
    never reach a user-facing or agent-facing surface.

    ``compact_scanner`` is deliberately left untouched: the HTTP routes that
    feed the web UI and the TUI still consume that shape.

    ``include_evidence=False`` yields the same vocabulary without the
    per-criterion strings — used by the lighter ranked-snapshot tool.
    """
    results = []
    for r in data.get('results', []):
        item = {'symbol': r.get('symbol'), 'price': r.get('price')}
        for raw_key, label in _ALIGNMENT_LABELS:
            d = r.get(raw_key) or {}
            block = {
                'conditions_met': d.get('total'),
                'conditions_total': SCANNER_CONDITION_TOTAL,
            }
            if include_evidence:
                criteria = d.get('criteria') or {}
                evidence = {
                    name: c.get('evidence')
                    for name, c in criteria.items()
                    if isinstance(c, dict) and c.get('evidence')
                }
                if evidence:
                    block['evidence'] = evidence
                if d.get('vetoes'):
                    block['vetoes'] = d['vetoes']
            item[label] = block
        # A row whose history stopped must say so wherever it is
        # served. The MCP screens drop these rows entirely; this keeps the
        # flag visible on any other path through this formatter.
        if r.get('data_stale'):
            item['data_stale'] = r['data_stale']
        results.append(item)

    out = {
        'results': results,
        'macro': data.get('macro', {}),
        'computed_at': data.get('computed_at', ''),
        'event_risk': data.get('event_risk'),
    }
    # Preserve caller-set pagination counters. compact_scanner drops these,
    # which is why market_get_overview's documented result_count /
    # total_available never actually appeared in its responses.
    for key in ('result_count', 'total_available'):
        if key in data:
            out[key] = data[key]
    return out


def compact_sentiment(data: dict) -> dict:
    """Sentiment → key numbers only.

    Keeps: F&G value+label, funding_rate, L/S ratio, MVRV+zone,
    alt_season, regime+score.
    Drops: colors, verbose metadata, FRED sub-fields.
    """
    s = data.get('sentiment', {})
    fg = s.get('fear_greed', {})
    fr = s.get('funding_rate', {})
    mvrv = s.get('mvrv_zscore', {})
    alt = s.get('alt_season', {})
    regime = s.get('regime', {})
    ls = s.get('long_short', {})
    oi = s.get('open_interest', {})

    # Flatten long/short ratios to just ratio numbers
    ls_compact = {}
    for sym, val in ls.items():
        if isinstance(val, dict):
            ls_compact[sym] = val.get('ratio')

    return {
        'status': 'ok',
        'sentiment': {
            'fear_greed': fg.get('value'),
            'fear_greed_label': fg.get('classification'),
            'funding_rate_pct': fr.get('rate_pct'),
            'mvrv_zscore': mvrv.get('zscore'),
            'mvrv_zone': mvrv.get('zone'),
            'alt_season_index': alt.get('index'),
            'alt_season': alt.get('season'),
            'regime': regime.get('regime'),
            'regime_score': regime.get('score'),
            'long_short': ls_compact,
            'open_interest_usd': oi.get('value_usd'),
            'oi_change_pct': oi.get('change_pct'),
        },
    }


def compact_stream(data: dict) -> dict:
    """Stream/confluence → top 3 support + top 3 resistance per asset.

    Drops: full zone list, BB squeeze detail, engulfing per-candle data.
    """
    compact_entries = []
    for entry in data.get('entries', []):
        zones = entry.get('zones', [])
        price = entry.get('price', 0)
        supports = sorted(
            [z for z in zones if z.get('side') == 'support'],
            key=lambda z: z.get('strength', 0), reverse=True
        )[:3]
        resistances = sorted(
            [z for z in zones if z.get('side') == 'resistance'],
            key=lambda z: z.get('strength', 0), reverse=True
        )[:3]

        item = {
            'symbol': entry.get('symbol'),
            'price': price,
            'supports': [
                {'price': z['price'], 'label': z.get('label', ''), 'strength': z.get('strength', 0)}
                for z in supports
            ],
            'resistances': [
                {'price': z['price'], 'label': z.get('label', ''), 'strength': z.get('strength', 0)}
                for z in resistances
            ],
        }

        # Keep signals if present but compact them
        signals = entry.get('signals', [])
        if signals:
            item['signals'] = [
                {'tf': sig.get('tf'), 'type': sig.get('type'), 'direction': sig.get('direction')}
                for sig in signals[:3]
            ]

        transitions = entry.get('transitions', [])
        if transitions:
            item['transitions'] = [
                {'indicator': t.get('indicator'), 'from': t.get('from'), 'to': t.get('to'), 'significance': t.get('significance')}
                for t in transitions[:3]
            ]

        compact_entries.append(item)

    return {
        'entries': compact_entries,
        'computed_at': data.get('computed_at', ''),
    }


def compact_report(data: dict) -> dict:
    """Morning report → summary + key takeaways only.

    Keeps: date, audio_script (key points), summary, report id.
    Drops: full HTML content, metadata, personal sections.
    """
    return {
        'id': data.get('id'),
        'created_at': data.get('created_at'),
        'summary': data.get('summary'),
        'audio_script': data.get('audio_script'),
    }


def compact_plays(data: dict) -> dict:
    """Plays context → already compact, minor trimming.

    Truncates long evidence strings and drops action_plan detail.
    """
    compact_plays_list = []
    for p in data.get('plays', []):
        item = {
            'id': p.get('id'),
            'title': p.get('title'),
            'status': p.get('status'),
            'direction': p.get('direction'),
            'primary_asset': p.get('primary_asset'),
            'conditions_met': p.get('conditions_met'),
            'conditions_logic': p.get('conditions_logic'),
            'timeframe': p.get('timeframe'),
        }
        # Compact conditions: just type + met status
        conditions = p.get('conditions_detail', [])
        if conditions:
            item['conditions'] = [
                {'type': c.get('type'), 'met': c.get('met'), 'label': c.get('label', '')[:80]}
                for c in conditions
            ]
        # Include invalidation status if present
        inv = p.get('invalidation')
        if inv:
            item['invalidated'] = True
        compact_plays_list.append(item)

    return {
        'plays': compact_plays_list,
        'summary': data.get('summary', ''),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compact_consensio(consensio: dict) -> dict:
    """Extract signal direction and position count from consensio dict."""
    if not consensio:
        return {}
    result = {}
    for tf in ('1D', '3D', '1W', '1d', '3d', '1w'):
        val = consensio.get(tf)
        if isinstance(val, dict):
            result[tf.lower()] = val.get('positions', val.get('signal'))
    total = consensio.get('total')
    if isinstance(total, dict):
        result['total'] = total.get('positions', 0)
    elif isinstance(total, (int, float)):
        result['total'] = total
    return result


def is_compact_requested(format_param: object = _UNSET) -> bool:
    """Check if ?format=compact is requested.

    When *format_param* is supplied (including ``None`` for FastAPI callers
    where ?format= was absent from the URL) it is used directly.
    When it is ``_UNSET`` (the default, i.e. Flask callers that omit the
    argument) the function falls back to Flask's ``request.args``.

    This distinction matters: FastAPI passes ``format=None`` when ?format is
    absent.  Using ``None`` as the "no caller arg" sentinel was ambiguous and
    caused a RuntimeError when the FastAPI router called
    ``maybe_compact(payload, compact_scanner, format_param=None)`` — the
    function fell through to ``request.args`` outside a Flask request context.
    """
    if format_param is not _UNSET:
        return format_param == "compact"
    from flask import request
    return request.args.get('format') == 'compact'


def maybe_compact(data: dict, compactor: callable, format_param: object = _UNSET) -> dict:
    """Apply compact formatter if ?format=compact is requested.

    Pass *format_param* when calling from a non-Flask context (e.g. FastAPI)
    so the helper does not attempt to read Flask's ``request`` object.

    Usage in Flask route handlers (unchanged):
        result = maybe_compact(result, compact_chart)
        return jsonify(result)

    Usage in FastAPI route handlers:
        result = maybe_compact(result, compact_chart, format_param=format)
    """
    if is_compact_requested(format_param):
        return compactor(data)
    return data
