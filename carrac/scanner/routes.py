"""Scanner API routes — ranked asset scores for watchlist."""

import os
import time

from flask import Blueprint, jsonify, request, session
from carrac.openapi.spec_decorator import spec
from carrac.auth.decorators import (
    login_required, auth_or_key_required, get_request_tier,
)
from carrac.caches import get_cached_scanner
from carrac.content_db import get_content_db

# Stream (Filter drawer): free tier gets a preview; transitions are Member-only.
FREE_STREAM_ENTRY_LIMIT = 3

# Liquidation spike surfacing. A separate worker maintains ONE
# Redis key, 'mcd:liq:summary':
#   {"updated_at": <unix s>, "symbols": {"BTC": {"m15_long_usd": float,
#    "m15_short_usd": float, "h1_long_usd": float, "h1_short_usd": float,
#    "last_ts": <unix s>}, ...}}
# (long_usd = notional of liquidated LONGS.) get_stream() reads it once per
# request — no import of the worker itself, code purely against the contract.
LIQ_SUMMARY_REDIS_KEY = 'mcd:liq:summary'
LIQ_SUMMARY_STALE_AFTER_S = 120
# Spike rule (see _liquidation_for_symbol): an absolute floor, PLUS either a
# 15m-rate-vs-trailing-hourly-average ratio, or (h1 empty) a doubled floor.
LIQ_SPIKE_MIN_USD = float(os.environ.get('LIQ_SPIKE_MIN_USD', '250000'))
LIQ_SPIKE_RATIO = float(os.environ.get('LIQ_SPIKE_RATIO', '3.0'))

scanner_bp = Blueprint('scanner', __name__)


def _get_liquidation_summary():
    """Read + validate the mcd:liq:summary Redis key.

    Returns the parsed {'updated_at':.., 'symbols': {...}} dict, or None if
    the key is missing, stale (older than LIQ_SUMMARY_STALE_AFTER_S), malformed,
    or Redis is unavailable. Mirrors the ltf_by_symbol read pattern above
    (inline import, swallow-all-errors) — a liquidation-feed hiccup must
    never break /api/stream.
    """
    try:
        from carrac.caches import _get_redis
        import json as _json
        r = _get_redis()
        if not r:
            return None
        raw = r.get(LIQ_SUMMARY_REDIS_KEY)
        if not raw:
            return None
        summary = _json.loads(raw)
        updated_at = summary.get('updated_at')
        if not updated_at:
            return None
        if (time.time() - float(updated_at)) > LIQ_SUMMARY_STALE_AFTER_S:
            return None
        return summary
    except Exception:
        return None


def _liquidation_for_symbol(sym_data):
    """Apply the liquidation spike rule to one symbol's liq-summary row.

    m15_total = m15_long + m15_short must clear an absolute floor
    (LIQ_SPIKE_MIN_USD), AND either:
      - h1_total > 0: m15_total >= LIQ_SPIKE_RATIO * (h1_total / 4) — i.e.
        the 15-min rate is >= 3x the trailing hourly average (h1/4 is the
        equivalent-15m average of the trailing hour); or
      - h1_total <= 0 (no trailing-hour baseline to compare against):
        fall back to a doubled absolute floor (2 * LIQ_SPIKE_MIN_USD).

    Returns the per-entry `liquidation` dict, or None if it doesn't clear
    the bar (or the row is malformed).
    """
    try:
        m15_long = float(sym_data.get('m15_long_usd') or 0)
        m15_short = float(sym_data.get('m15_short_usd') or 0)
        h1_long = float(sym_data.get('h1_long_usd') or 0)
        h1_short = float(sym_data.get('h1_short_usd') or 0)
    except (TypeError, ValueError):
        return None

    m15_total = m15_long + m15_short
    if m15_total < LIQ_SPIKE_MIN_USD:
        return None

    h1_total = h1_long + h1_short
    if h1_total <= 0:
        is_spike = m15_total >= 2 * LIQ_SPIKE_MIN_USD
    else:
        is_spike = m15_total >= LIQ_SPIKE_RATIO * (h1_total / 4.0)
    if not is_spike:
        return None

    dominant = 'long' if m15_long >= m15_short else 'short'
    return {
        'm15_long_usd': m15_long,
        'm15_short_usd': m15_short,
        'dominant': dominant,
        'total_usd': m15_total,
    }


# TF classification by level name prefix
_TF_MAP = {
    'PDH': 'D', 'PDL': 'D', 'SW_H': 'D', 'SW_PH': 'D', 'SW_L': 'D', 'SW_PL': 'D',
    'BB_U': 'D', 'BB_M': 'D', 'BB_L': 'D', 'EMA30': 'D', 'EMA60': 'D',
    'EMA21': 'D', 'EMA50': 'D', 'EMA200': 'D', 'SMA200': 'D',
    'POC': 'D', 'VAH': 'D', 'VAL': 'D',
    'PWH': 'W', 'PWL': 'W',
    'PMH': 'M', 'PML': 'M', 'PIVOT': 'M', 'TC': 'M', 'BC': 'M',
    'S1': 'M', 'S2': 'M', 'S3': 'M', 'R1': 'M', 'R2': 'M', 'R3': 'M',
    'MH': 'M', 'ML': 'M',
}


def _classify_tf(label):
    """Determine timeframe from zone label (e.g. 'PDH+BB_U' → 'D')."""
    parts = label.split('+')
    tfs = set()
    for p in parts:
        tf = _TF_MAP.get(p)
        if tf:
            tfs.add(tf)
    # Highest TF wins: M > W > D
    if 'M' in tfs:
        return 'M'
    if 'W' in tfs:
        return 'W'
    return 'D'


@scanner_bp.route('/api/stream')
@login_required
def get_stream():
    """Return confluence zones near price for watchlist assets."""
    from user_db import user_db

    # Serve the last-known-good snapshot (stale-marked) on cold
    # start so Radar is never blank; only truly-first-boot (no LKG yet) is empty.
    scanner_data = get_cached_scanner(allow_stale=True)
    if not scanner_data:
        return jsonify({'entries': [], 'computed_at': ''})

    # Filter to user watchlist (needed early for on-demand LTF)
    user_id = session.get('user_id')
    watchlist = set(user_db.get_flat_watchlist(user_id)) if user_id else set()

    # Get HTF cache for BB squeeze, PDH/PDL
    try:
        from carrac.precompute import response_cache
        htf_data = response_cache.get('htf')
    except Exception:
        htf_data = None

    htf_by_symbol = {}
    if htf_data:
        for asset in htf_data.get('assets', []):
            htf_by_symbol[asset.get('symbol', '')] = asset

    # Read cached LTF data (never compute on-demand — that's the slow path)
    ltf_by_symbol = {}
    if watchlist:
        try:
            from carrac.caches import _get_redis
            import json as _json
            r = _get_redis()
            if r:
                for sym in watchlist:
                    try:
                        raw = r.get(f'mcd:ltf_asset:{sym}')
                        if raw:
                            ltf_by_symbol[sym] = _json.loads(raw)
                    except Exception:
                        pass
        except Exception:
            pass

    # User's own drawn levels (horizontal lines / zones) near price — surfaced
    # as Radar zone rows. ONE query for the whole request
    # (all watchlist symbols at once); a drawings-layer failure must never
    # break the stream.
    user_drawings_by_symbol = {}
    if user_id:
        try:
            cdb = get_content_db()
            for d in cdb.drawing_list(user_id=user_id):
                if d.get('drawing_type') not in ('horizontal_line', 'zone'):
                    continue
                sym = (d.get('asset') or '').upper()
                if not sym:
                    continue
                user_drawings_by_symbol.setdefault(sym, []).append(d)
        except Exception:
            user_drawings_by_symbol = {}

    entries = []
    for result in scanner_data.get('results', []):
        symbol = result.get('symbol', '')
        if watchlist and symbol not in watchlist:
            continue

        # Collect zones from both directions, deduplicate within 0.1%
        all_zones = []
        seen_prices = []
        for direction in ('long', 'short'):
            dir_data = result.get(direction, {})
            confluences = dir_data.get('confluences', {})
            for zone in confluences.get('all', []):
                zp = zone.get('price', 0)
                if not zp:
                    continue
                # Deduplicate within 0.1%
                dup = False
                for sp in seen_prices:
                    if sp > 0 and abs(zp - sp) / sp <= 0.001:
                        dup = True
                        break
                if not dup:
                    seen_prices.append(zp)
                    all_zones.append({
                        'price': zp,
                        'label': zone.get('label', ''),
                        'sources': zone.get('sources', []),
                        'strength': zone.get('strength', 0),
                        'side': zone.get('side', ''),
                        'count': zone.get('count', 1),
                        'tf': _classify_tf(zone.get('label', '')),
                    })

        # Merge in the user's own drawn levels near price.
        # Not tier-gated — a user's own drawings are their own data.
        try:
            current_price = result.get('price', 0) or 0
            sym_drawings = user_drawings_by_symbol.get(symbol.upper(), [])
            if current_price and sym_drawings:
                for d in sym_drawings:
                    lo = d.get('price_level')
                    hi = d.get('price_level_upper')
                    if lo is None:
                        continue

                    # Rectangle/zone drawings keep their FULL band (a design choice —
                    # collapsing to the midpoint is pointless):
                    # proximity is measured to the nearest edge, and price can
                    # be above / inside / below the band.
                    is_band = hi is not None and hi != lo
                    if is_band:
                        lower, upper = (lo, hi) if lo <= hi else (hi, lo)
                        if not lower or not upper:
                            continue
                        if current_price > upper:
                            edge_dist = (current_price - upper) / current_price
                            band_side = 'support'
                        elif current_price < lower:
                            edge_dist = (lower - current_price) / current_price
                            band_side = 'resistance'
                        else:
                            edge_dist = 0.0
                            band_side = 'inside'
                        if edge_dist > 0.05:
                            continue
                        band_label = (d.get('label') or '').strip() or \
                            f'Your zone {lower:g}–{upper:g}'
                        # No point-dedup for bands: they are ranges, not levels.
                        all_zones.append({
                            'price': (lower + upper) / 2.0,
                            'label': band_label,
                            'sources': ['your_level'],
                            'strength': 1.0,
                            'side': band_side,
                            'count': 1,
                            'tf': 'D',
                            'band': {'lower': lower, 'upper': upper},
                        })
                        continue

                    level_price = lo
                    if not level_price:
                        continue

                    # Same 5% proximity window the confluence zones use.
                    if abs(current_price - level_price) / current_price > 0.05:
                        continue

                    # Dedup within 0.1%, mirroring the confluence dedup above —
                    # merge the marker into the existing zone instead of a
                    # duplicate row. Rebuild (not mutate) 'sources' — it may
                    # alias a list from the shared scanner cache.
                    merge_target = None
                    for sp, existing_zone in zip(seen_prices, all_zones):
                        if sp > 0 and abs(level_price - sp) / sp <= 0.001:
                            merge_target = existing_zone
                            break
                    if merge_target is not None:
                        if 'your_level' not in merge_target['sources']:
                            merge_target['sources'] = merge_target['sources'] + ['your_level']
                        continue

                    seen_prices.append(level_price)
                    label = (d.get('label') or '').strip() or f'Your level @{level_price:g}'
                    all_zones.append({
                        'price': level_price,
                        'label': label,
                        'sources': ['your_level'],
                        'strength': 1.0,
                        'side': 'support' if level_price < current_price else 'resistance',
                        'count': 1,
                        'tf': 'D',
                    })
        except Exception:
            pass

        # Get BB squeeze + PDH/PDL + engulfing signals from HTF cache
        htf_asset = htf_by_symbol.get(symbol, {})
        bb = htf_asset.get('bb') or {}
        kl = htf_asset.get('key_levels') or {}

        # Derive bias primitives from scanner's composite verdict (no recomputation).
        # best='long'/'short' + best_score=0..12; absent → neutral/0.0.
        _best = result.get('best', '')
        _best_score = result.get('best_score', 0) or 0
        if _best == 'long':
            _bias = 'long'
            _bias_score = float(_best_score)
        elif _best == 'short':
            _bias = 'short'
            _bias_score = -float(_best_score)
        else:
            _bias = 'neutral'
            _bias_score = 0.0

        entry = {
            'symbol': symbol,
            'price': result.get('price', 0),
            'zones': all_zones,
            'bb_squeeze_pct': bb.get('squeeze_pct'),
            'pdh': kl.get('pdh'),
            'pdl': kl.get('pdl'),
            'bias': _bias,
            'bias_score': _bias_score,
        }

        # Merge engulfing signals from HTF (D, 3D, W) + LTF (1h, 4h)
        all_signals = list(htf_asset.get('engulfing_signals') or [])
        ltf_asset = ltf_by_symbol.get(symbol, {})
        ltf_signals = ltf_asset.get('engulfing_signals') or []
        if ltf_signals:
            all_signals.extend(ltf_signals)
        if all_signals:
            entry['signals'] = all_signals

        entries.append(entry)

    # Free tier: preview only (first N entries, no transitions). Paid tiers: full feed.
    tier = get_request_tier()
    is_preview = (tier == 'free')

    if is_preview:
        entries = entries[:FREE_STREAM_ENTRY_LIMIT]

    # Liquidation spikes — one Redis read for the whole request,
    # then per-entry attachment. Riding the (already tier-truncated) `entries`
    # list means free-tier truncation applies automatically — no extra gating
    # needed here.
    liq_summary = _get_liquidation_summary()
    if liq_summary:
        liq_by_symbol = liq_summary.get('symbols') or {}
        for entry in entries:
            sym_liq = liq_by_symbol.get(entry['symbol'])
            if not sym_liq:
                continue
            liquidation = _liquidation_for_symbol(sym_liq)
            if liquidation:
                entry['liquidation'] = liquidation

    # Add indicator transitions — Member+ only; free tier gets an upsell instead.
    if not is_preview:
        try:
            from carrac.indicator_state import get_recent_transitions
            all_transitions = get_recent_transitions(limit=200, min_significance=1)
            # Group by symbol and attach to entries
            trans_by_symbol = {}
            for t in all_transitions:
                s = t.get('symbol', '')
                trans_by_symbol.setdefault(s, []).append(t)
            for entry in entries:
                sym_trans = trans_by_symbol.get(entry['symbol'], [])
                if sym_trans:
                    entry['transitions'] = sym_trans[:5]  # max 5 per asset
        except Exception:
            pass

    # User's own open positions surfaced as Radar rows.
    # A position is an implicit watchlist entry — cover EVERY symbol the user
    # holds, not just ones on their explicit watchlist (assemble_live_positions
    # is not filtered to `watchlist` above). No tier gate: this is the user's
    # own data. Cost check for the 30s poll cadence: assemble_live_positions()
    # is cheap — get_open_trades() is one indexed SQLite query that
    # short-circuits to [] (zero further work) when the user has no open
    # trades; get_combined_crypto_prices()/get_cached_scanner() both read the
    # in-memory/Redis caches already kept warm by the background refresh loop
    # (no per-position network fetch); get_active_plays() is one more indexed
    # query. So the full assembler is used rather than hand-rolling a cheaper
    # variant — there's no cheaper correct path available.
    positions = []
    if user_id:
        try:
            from carrac.composite.assemblers import assemble_live_positions
            pos_data = assemble_live_positions(user_id) or {}
            for p in pos_data.get('positions', []):
                direction = p.get('direction', 'long')
                current_price = p.get('current_price')
                stop = p.get('stop_price')
                stop_distance_pct = None
                if current_price and stop:
                    if direction == 'long':
                        stop_distance_pct = round((current_price - stop) / current_price * 100, 2)
                    else:
                        stop_distance_pct = round((stop - current_price) / current_price * 100, 2)
                positions.append({
                    'symbol': p.get('asset', ''),
                    'direction': direction,
                    'risk_status': p.get('risk_status'),
                    'unrealized_pnl_pct': p.get('unrealized_pnl_pct'),
                    'current_rr': p.get('current_rr'),
                    'stop_distance_pct': stop_distance_pct,
                    'entry_price': p.get('entry_price'),
                    'age': p.get('time_in_trade'),
                })
        except Exception:
            positions = []

    # Imminent high-impact macro event (Radar half) — ONE global
    # row, not per-symbol. 72h window is deliberately wider than the Scanner
    # card's own 36h cap (scanner_get_screen() -> data['event_risk']) — the feed can
    # afford to warn earlier than the compact scanner card. Any failure here
    # (DB down, event_risk import error) must never break the stream.
    stream_event = None
    try:
        from carrac.scanner.event_risk import get_imminent_event
        ev = get_imminent_event(72)
        if ev:
            stream_event = {
                'title': ev.get('title'),
                'hours_until': ev.get('hours_until'),
                'impact': ev.get('impact'),
            }
    except Exception:
        stream_event = None

    from carrac.formatters import maybe_compact, compact_stream
    payload = {
        'entries': entries,
        'positions': positions,
        'computed_at': scanner_data.get('computed_at', ''),
        'stream_event': stream_event,
        'stale': bool(scanner_data.get('_stale')),
        'stale_age_sec': scanner_data.get('_stale_age_sec'),
    }
    if is_preview:
        payload['preview_mode'] = True
        payload['preview_tier'] = 'member'
        payload['preview_reason'] = 'Upgrade to Member for the full Filter feed + indicator transitions.'
    result = maybe_compact(payload, compact_stream)
    return jsonify(result)


@scanner_bp.route('/api/transitions')
@auth_or_key_required
def scanner_get_transitions():
    """Recent indicator state transitions across all assets. Member+ only."""
    # Transitions are a Member differentiator — no free preview.
    if get_request_tier() == 'free':
        return jsonify({
            'error': 'Indicator transitions are a Member feature.',
            'upgrade_to': 'member',
        }), 403

    from carrac.indicator_state import get_recent_transitions

    symbol = request.args.get('symbol')
    min_sig = int(request.args.get('min_significance', 0))
    limit = min(int(request.args.get('limit', 50)), 200)

    events = get_recent_transitions(limit=limit, symbol=symbol,
                                    min_significance=min_sig)

    return jsonify({
        'events': events,
        'count': len(events),
    })


@scanner_bp.route('/api/scanner/<symbol>')
@login_required
def get_scanner_detail(symbol):
    """Return detailed score for a single asset (both directions)."""
    data = get_cached_scanner(allow_stale=True)  # stale > empty
    if not data:
        return jsonify({'error': 'Scanner data not yet available'}), 200

    symbol = symbol.upper()
    for r in data.get('results', []):
        if r['symbol'] == symbol:
            return jsonify({
                'result': r,
                'macro': data.get('macro', {}),
                'computed_at': data.get('computed_at', ''),
                'stale': bool(data.get('_stale')),
                'stale_age_sec': data.get('_stale_age_sec'),
            })

    return jsonify({'error': f'{symbol} not found in scanner results'}), 404


@scanner_bp.route('/api/scanner/snapshots/<int:journal_id>')
@login_required
def get_trade_snapshots(journal_id):
    """Get entry + exit snapshots for a specific trade."""
    user_id = session.get('user_id')
    db = get_content_db()
    snaps = db.get_score_snapshots(journal_id)
    # Verify ownership
    snaps = [s for s in snaps if s.get('user_id') == user_id]
    return jsonify({'snapshots': snaps})


@scanner_bp.route('/api/scanner/stats')
@login_required
def get_scanner_stats():
    """Per-criterion accuracy breakdown from completed trades."""
    user_id = session.get('user_id')
    db = get_content_db()

    rows = db.get_all_snapshots_with_outcomes(user_id)
    total = len(rows)

    if total == 0:
        return jsonify({
            'total_trades': 0,
            'overall_win_rate': 0,
            'by_criterion': {},
            'by_verdict': {},
            'sample_sufficient': False,
        })

    wins = sum(1 for r in rows if r.get('win'))
    overall_wr = round(wins / total, 3) if total else 0

    # Per-criterion breakdown
    criteria = ['trend', 'momentum', 'rsi', 'structure', 'level', 'macro']
    by_criterion = {}
    for crit in criteria:
        col = f'score_{crit}'
        buckets = {}
        for score_val in (0, 1, 2):
            matching = [r for r in rows if r.get(col) == score_val]
            count = len(matching)
            wr = round(sum(1 for r in matching if r.get('win')) / count, 3) if count else 0
            buckets[f'score_{score_val}'] = {'count': count, 'win_rate': wr}
        by_criterion[crit] = buckets

    # Per-verdict breakdown
    verdicts = ['STRONG', 'GOOD', 'NEUTRAL', 'WEAK', 'AVOID']
    by_verdict = {}
    for v in verdicts:
        matching = [r for r in rows if r.get('verdict') == v]
        count = len(matching)
        wr = round(sum(1 for r in matching if r.get('win')) / count, 3) if count else 0
        by_verdict[v] = {'count': count, 'win_rate': wr}

    return jsonify({
        'total_trades': total,
        'overall_win_rate': overall_wr,
        'by_criterion': by_criterion,
        'by_verdict': by_verdict,
        'sample_sufficient': total >= 50,
    })


# ── Scanner → Backtester (Hold-N flip signal test across filtered universe) ──

@scanner_bp.route('/api/scanner/backtest', methods=['POST'])
@spec(
    summary='Test a formula against current scanner universe',
    description='Run a Hold-N flip-signal backtest of the given boolean formula against '
                'every asset matching the verdict/direction filter. Returns results ranked '
                'by edge (hit_rate × mean_return). Paid tiers; uses the `backtester` '
                'tier limit (member and up). Max 25 assets per call.',
    tags=['Scanner'],
    # The enforcement below is limits['backtester'], True for member.
    # x-min-tier is docs-only, so 'premium' here advertised a stricter bar than
    # the route actually applies.
    min_tier='member',
    operation_id='scanner_backtest',
)
@login_required
def scanner_backtest_endpoint():
    from carrac.auth.decorators import get_tier_limits
    from carrac.mcp.tools.backtest import scan_and_backtest
    from carrac.mcp.protocol import ToolError

    limits = get_tier_limits(get_request_tier())
    if not limits.get('backtester'):
        return jsonify({
            'error': 'Scanner backtest requires a subscription',
            'tier_gated': True,
        }), 403

    data = request.get_json(silent=True) or {}
    expression = (data.get('expression') or '').strip()
    if not expression:
        return jsonify({'error': 'expression is required'}), 400
    if len(expression) > 500:
        return jsonify({'error': 'expression too long (max 500 chars)'}), 400

    try:
        args = {
            'expression': expression,
            'verdict': data.get('verdict') or 'STRONG,GOOD',
            'direction': data.get('direction') or 'long',
            'timeframe': data.get('timeframe') or '1D',
            'lookback_bars': int(data.get('lookback_bars') or 365),
            'hold_bars': int(data.get('hold_bars') or 5),
            'max_assets': int(data.get('max_assets') or 10),
        }
    except (ValueError, TypeError):
        return jsonify({'error': 'lookback_bars, hold_bars, max_assets must be integers'}), 400

    try:
        return jsonify(scan_and_backtest(args))
    except ToolError as e:
        return jsonify({'error': str(e)}), 400
