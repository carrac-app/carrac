"""
Widget routes — API, setup page, Scriptable downloads.

Endpoints:
  GET  /widget/setup                    — Setup page (login required)
  GET  /api/widget/tokens               — List user's tokens (login required)
  GET  /api/widget/summary              — Widget data (widget token required)
  GET  /widget/scriptable.js            — Latest Scriptable script (public)
  GET  /widget/scriptable-download      — .scriptable file with token (login required)
  GET  /widget/android/latest           — APK download (public)
  GET  /widget/android/version          — Version JSON (public)

Token create and revoke are served by carrac/fastapi_app/routers/widget.py.
"""

import json
import logging
import os
from datetime import datetime

from flask import (
    jsonify, request, session, send_file,
    current_app, make_response, redirect
)

from carrac.widget import widget_bp
from carrac.widget.auth import (
    hash_token, validate_token_format, widget_token_required,
)
from carrac.auth.decorators import login_required

logger = logging.getLogger('carrac.widget')


ANDROID_VERSION_CODE = 7
ANDROID_VERSION_NAME = '2.3.0'

# Allowed section names for ?sections= filter
ALLOWED_SECTIONS = frozenset({
    'prices', 'signals', 'sentiment', 'alerts', 'positions', 'report'
})


# ==================== Setup Page ====================

@widget_bp.route('/widget/setup')
@login_required
def widget_setup():
    """Redirect to the consolidated Mobile Widget tab in Settings."""
    return redirect('/settings#widget', code=302)


# ==================== Token Management API ====================

@widget_bp.route('/api/widget/tokens', methods=['GET'])
@login_required
def list_tokens():
    """List user's widget tokens (masked)."""
    from user_db import user_db
    tokens = user_db.list_widget_tokens(session['user_id'])
    return jsonify({'tokens': tokens})


# ==================== Widget Data API ====================

@widget_bp.route('/api/widget/summary')
@widget_token_required
def widget_summary():
    """Lightweight JSON for widget rendering. ~2-3 KB."""
    user_id = request.widget_user_id

    # Parse optional filters
    requested_symbols = _parse_symbols(request.args.get('symbols'))
    requested_sections = _parse_sections(request.args.get('sections'))

    result = {
        'ts': int(datetime.now().timestamp()),
        'v': 1,
    }

    # Get user's watchlist if no symbols override
    if not requested_symbols:
        from user_db import user_db
        requested_symbols = user_db.get_flat_watchlist(user_id)

    # Cap at 20 symbols (enough for full watchlist)
    requested_symbols = requested_symbols[:20]

    # Prices + Signals
    if 'prices' in requested_sections or 'signals' in requested_sections:
        prices_data, signals_data = _get_prices_and_signals(requested_symbols)
        if 'prices' in requested_sections:
            result['prices'] = prices_data
        if 'signals' in requested_sections:
            result['signals'] = signals_data

    # Sentiment
    if 'sentiment' in requested_sections:
        result['sentiment'] = _get_sentiment()

    # Alerts
    if 'alerts' in requested_sections:
        result['alerts'] = _get_alerts(user_id)

    # Positions
    if 'positions' in requested_sections:
        result['positions'] = _get_positions(user_id)

    # Morning report snippet
    if 'report' in requested_sections:
        result['report'] = _get_report_snippet()

    return jsonify(result)


# ==================== Scriptable Distribution ====================

_API_BASE_PLACEHOLDER = 'const API_BASE = "https://carrac.example.invalid"'


def _instance_origin():
    """This instance's own public origin: SITE_URL if set, else the request's."""
    base = os.getenv('SITE_URL') or request.url_root
    return base.rstrip('/')


def _render_widget_script(content):
    """Point the script's API_BASE at this instance instead of a fixed domain."""
    return content.replace(
        _API_BASE_PLACEHOLDER,
        'const API_BASE = ' + json.dumps(_instance_origin()),
    )


@widget_bp.route('/widget/scriptable.js')
def scriptable_script():
    """Serve latest Scriptable widget script (public, no auth)."""
    script_path = os.path.join(
        current_app.static_folder, 'widget', 'carrac-widget.js'
    )
    if not os.path.exists(script_path):
        return 'Script not found', 404

    with open(script_path, 'r') as f:
        content = f.read()

    resp = make_response(_render_widget_script(content))
    resp.headers['Content-Type'] = 'application/javascript; charset=utf-8'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    resp.headers['Vary'] = 'Host'
    return resp


@widget_bp.route('/widget/scriptable-download')
@login_required
def scriptable_download():
    """Download .scriptable file with user's token pre-embedded."""
    token_param = request.args.get('token', '').strip()
    if not token_param or not validate_token_format(token_param):
        return jsonify({'error': 'Invalid token'}), 400

    # Verify token belongs to this user
    from user_db import user_db
    token_h = hash_token(token_param)
    token_info = user_db.verify_widget_token(token_h)
    if not token_info or token_info['user_id'] != session['user_id']:
        return jsonify({'error': 'Token not found'}), 404

    # Read the template script
    script_path = os.path.join(
        current_app.static_folder, 'widget', 'carrac-widget.js'
    )
    if not os.path.exists(script_path):
        return jsonify({'error': 'Script not available'}), 500

    with open(script_path, 'r') as f:
        script = f.read()

    script = _render_widget_script(script)

    # Embed the token
    script = script.replace(
        'const PRELOADED_TOKEN = null',
        f'const PRELOADED_TOKEN = "{token_param}"'
    )

    # Build .scriptable JSON envelope
    scriptable_file = json.dumps({
        'always_run_in_app': False,
        'icon': {
            'color': 'green',
            'glyph': 'chart-line',
        },
        'name': 'Carrac Widget',
        'script': script,
        'share_sheet_inputs': [],
    })

    resp = make_response(scriptable_file)
    resp.headers['Content-Type'] = 'application/octet-stream'
    resp.headers['Content-Disposition'] = (
        'attachment; filename="CarracWidget.scriptable"'
    )
    return resp


# ==================== Android Distribution ====================

@widget_bp.route('/widget/android/version')
def android_version():
    """Version check for Android self-update."""
    return jsonify({
        'version_code': ANDROID_VERSION_CODE,
        'version_name': ANDROID_VERSION_NAME,
    })


@widget_bp.route('/widget/android/latest')
def android_apk():
    """Download latest Android APK."""
    apk_path = os.path.join(
        current_app.static_folder, 'widget', 'carrac-widget.apk'
    )
    if not os.path.exists(apk_path):
        return jsonify({'error': 'APK not available yet'}), 404
    return send_file(
        apk_path,
        mimetype='application/vnd.android.package-archive',
        as_attachment=True,
        download_name='carrac-widget.apk',
    )


# ==================== Internal Helpers ====================

def _parse_symbols(raw: str) -> list:
    """Parse ?symbols=BTC,ETH,SPX → ['BTC', 'ETH', 'SPX']."""
    if not raw:
        return []
    symbols = [s.strip().upper() for s in raw.split(',') if s.strip()]
    return symbols[:20]


def _parse_sections(raw: str) -> set:
    """Parse ?sections=prices,signals → set. Default: all."""
    if not raw:
        return ALLOWED_SECTIONS
    parts = {s.strip().lower() for s in raw.split(',') if s.strip()}
    return parts & ALLOWED_SECTIONS or ALLOWED_SECTIONS


def _get_prices_and_signals(symbols: list) -> tuple:
    """Get prices + 24h change and Consensio/RSI from precomputed cache."""
    prices = []
    signals = []

    if not symbols:
        return prices, signals

    # Try precomputed response cache first (already refreshed every 60s)
    try:
        from carrac.precompute import response_cache
        cached = response_cache.get('htf')
    except Exception:
        cached = None

    cached_assets = {}
    if cached:
        for a in cached.get('assets', []):
            cached_assets[a.get('symbol', '')] = a

    # Also get live crypto prices from cache_manager
    try:
        from cache_manager import get_cached_crypto_prices
        crypto_prices = get_cached_crypto_prices() or {}
    except Exception:
        crypto_prices = {}

    for sym in symbols:
        asset = cached_assets.get(sym)
        price_val = None
        change = None

        # Price from crypto cache (most up-to-date for crypto)
        if sym in crypto_prices:
            cp = crypto_prices[sym]
            price_val = cp.get('price') or cp.get('usd')
            change = cp.get('change_pct') or cp.get('change_24h') or cp.get('usd_24h_change')
        # Fallback to precomputed cache
        elif asset:
            price_val = asset.get('latest_price')
            change = asset.get('change_pct') or asset.get('change_24h')

        if price_val is not None:
            import math
            change_val = None
            if change is not None:
                try:
                    cv = float(change)
                    if not math.isnan(cv) and not math.isinf(cv):
                        change_val = round(cv, 2)
                except (TypeError, ValueError):
                    pass

            prices.append({
                's': sym,
                'p': round(float(price_val), _price_decimals(price_val)),
                'c': change_val,
            })

        # Consensio signals + RSI from precomputed cache
        if asset:
            cons = asset.get('consensio', {})
            # Map Consensio positions to 1/-1/0 for compact format
            con_1d = _signal_to_int(cons.get('1d', {}))
            con_3d = _signal_to_int(cons.get('3d', {}))
            con_1w = _signal_to_int(cons.get('1w', {}))

            rsi = asset.get('rsi_14')
            if rsi is not None:
                rsi = round(float(rsi))

            signals.append({
                's': sym,
                'con': [con_1d, con_3d, con_1w],
                'rsi': rsi,
            })

    return prices, signals


def _signal_to_int(tf_data: dict) -> int:
    """Convert Consensio timeframe data to 1 (bullish), -1 (bearish), 0 (neutral)."""
    if not tf_data:
        return 0
    signal = tf_data.get('signal', '')
    if signal == 'bullish':
        return 1
    elif signal == 'bearish':
        return -1
    return 0


def _price_decimals(price) -> int:
    """Determine appropriate decimal places for a price."""
    try:
        p = abs(float(price))
    except (TypeError, ValueError):
        return 2
    if p >= 1000:
        return 2
    if p >= 1:
        return 2
    if p >= 0.01:
        return 4
    return 6


def _get_sentiment() -> dict:
    """Get compact sentiment data."""
    result = {}
    try:
        from carrac.services import get_services
        db = get_services().db
    except Exception:
        return result

    # Fear & Greed
    try:
        from carrac.sentiment.routes import _fetch_fear_greed
        fg = _fetch_fear_greed()
        # value is None when unavailable — do not coerce
        # to 50/Neutral, which is indistinguishable from a measured reading.
        val = fg.get('value')
        result['fg'] = int(val) if val is not None else None
        result['fg_label'] = fg.get('classification')
    except Exception:
        result['fg'] = None
        result['fg_label'] = None

    # Market Regime
    try:
        from carrac.sentiment.regime import gather_regime_inputs, compute_market_regime
        inputs = gather_regime_inputs()
        regime = compute_market_regime(**inputs)
        result['regime'] = regime.get('regime')
        result['regime_score'] = regime.get('score', 0)
    except Exception:
        pass  # Don't send false data — omit regime if computation fails

    # Funding rate (BTC)
    try:
        fr = db.get_funding_rate('BTC')
        result['btc_funding'] = round(float(fr) * 100, 4) if fr else None
    except Exception:
        result['btc_funding'] = None

    # Long/Short ratio (BTC)
    try:
        ls = db.fetch_long_short_ratio(['BTC'])
        btc_ls = ls.get('BTC', {})
        result['ls_ratio'] = round(float(btc_ls.get('ratio', 1.0)), 2) if btc_ls else None
    except Exception:
        result['ls_ratio'] = None

    # Open Interest (BTC)
    try:
        oi = db.fetch_open_interest('BTC')
        if oi:
            result['oi_usd'] = oi.get('value_usd')
            result['oi_change'] = round(float(oi.get('change_pct', 0)), 1) if oi.get('change_pct') is not None else None
        else:
            result['oi_usd'] = None
            result['oi_change'] = None
    except Exception:
        result['oi_usd'] = None
        result['oi_change'] = None

    # MVRV Z-Score
    try:
        mvrv = db.fetch_mvrv_zscore()
        if mvrv:
            result['mvrv'] = round(float(mvrv.get('zscore', 0)), 2)
            result['mvrv_zone'] = mvrv.get('zone', 'neutral')
        else:
            result['mvrv'] = None
            result['mvrv_zone'] = None
    except Exception:
        result['mvrv'] = None
        result['mvrv_zone'] = None

    # Alt Season
    try:
        alt = db.calculate_alt_season_index()
        # index is None when unavailable — do not coerce to 50.
        idx = alt.get('index')
        result['alt_season'] = int(idx) if idx is not None else None
    except Exception:
        result['alt_season'] = None

    return result


def _get_alerts(user_id: int) -> dict:
    """Get compact alert summary for a user."""
    try:
        from push_notifications import get_user_alerts
        all_alerts = get_user_alerts(str(user_id), active_only=False)
        active = [a for a in all_alerts if a.get('active')]
        triggered = [a for a in all_alerts if a.get('triggered_at')]

        result = {
            'active': len(active),
            'total': len(all_alerts),
        }

        # Last triggered
        if triggered:
            last = max(triggered, key=lambda a: a.get('triggered_at', ''))
            result['last_triggered'] = {
                's': last.get('symbol', ''),
                'cond': last.get('condition', ''),
                'p': last.get('price'),
                'at': last.get('triggered_at'),
            }

        # Nearest active alert (by % distance from current price)
        if active:
            nearest = _find_nearest_alert(active)
            if nearest:
                result['nearest'] = nearest

        # Full active alerts list for widget display
        if active:
            try:
                from cache_manager import get_cached_crypto_prices
                crypto_prices = get_cached_crypto_prices() or {}
            except Exception:
                crypto_prices = {}

            alert_list = []
            for a in active:
                sym = a.get('symbol', '')
                target = a.get('price')
                current = None
                if sym in crypto_prices:
                    cp = crypto_prices[sym]
                    current = cp.get('price') or cp.get('usd')
                pct = None
                if current and target:
                    pct = round(abs(float(target) - float(current)) / float(current) * 100, 2)
                alert_list.append({
                    's': sym,
                    'cond': a.get('condition', ''),
                    'p': float(target) if target else None,
                    'pct': pct,
                })
            # Sort by distance (nearest first)
            alert_list.sort(key=lambda x: x.get('pct') or 999)
            result['list'] = alert_list

        return result
    except Exception as e:
        logger.warning(f"Failed to get alerts for widget: {e}")
        return {'active': 0, 'total': 0}


def _find_nearest_alert(active_alerts: list) -> dict:
    """Find the alert closest to being triggered."""
    try:
        from cache_manager import get_cached_crypto_prices
        crypto_prices = get_cached_crypto_prices() or {}
    except Exception:
        return None

    best = None
    best_pct = float('inf')

    for alert in active_alerts:
        sym = alert.get('symbol', '')
        target = alert.get('price')
        if not target:
            continue

        current = None
        if sym in crypto_prices:
            cp = crypto_prices[sym]
            current = cp.get('price') or cp.get('usd')

        if current and target:
            pct = abs(float(target) - float(current)) / float(current) * 100
            if pct < best_pct:
                best_pct = pct
                best = {
                    's': sym,
                    'cond': alert.get('condition', ''),
                    'p': float(target),
                    'pct': round(pct, 2),
                }

    return best


def _get_positions(user_id: int) -> dict:
    """Get open positions with live P&L in R multiples."""
    try:
        from carrac.content_db import get_content_db
        content_db = get_content_db()

        open_trades = content_db.get_open_trades(user_id)
        stats_7d = content_db.get_journal_stats(user_id, days=7)

        # Get current prices for P&L — crypto cache + HTF response cache for stocks
        try:
            from cache_manager import get_cached_crypto_prices
            crypto_prices = get_cached_crypto_prices() or {}
        except Exception:
            crypto_prices = {}

        # Build HTF price map for non-crypto assets (stocks, indices)
        htf_prices = {}
        try:
            from carrac.precompute import response_cache
            cached_htf = response_cache.get('htf')
            if cached_htf:
                for a in cached_htf.get('assets', []):
                    htf_prices[a.get('symbol', '')] = a.get('latest_price')
        except Exception:
            pass

        positions = []
        total_rr = 0.0

        for trade in open_trades:
            entry = float(trade.get('entry_price') or 0)
            target = float(trade.get('target_price') or 0)
            stop = float(trade.get('stop_price') or 0)
            direction = (trade.get('direction') or 'long').lower()
            sym = trade.get('asset', '')

            # Get current price — try crypto cache first, then HTF cache
            current = None
            if sym in crypto_prices:
                cp = crypto_prices[sym]
                current = float(cp.get('price') or cp.get('usd') or 0)
            elif sym in htf_prices and htf_prices[sym]:
                current = float(htf_prices[sym])

            if not current or not entry or not stop or entry == stop:
                rr_now = None
                pct = None
                pct_to_tp = None
                pct_to_sl = None
            else:
                # P&L in R multiples
                if direction == 'long':
                    rr_now = (current - entry) / abs(entry - stop)
                else:  # short
                    rr_now = (entry - current) / abs(stop - entry)

                pct = ((current - entry) / entry) * 100
                if direction == 'short':
                    pct = -pct

                pct_to_tp = abs(target - current) / current * 100 if target and current else None
                pct_to_sl = abs(stop - current) / current * 100 if stop and current else None

                rr_now = round(rr_now, 2)
                pct = round(pct, 2)
                if pct_to_tp is not None:
                    pct_to_tp = round(pct_to_tp, 2)
                if pct_to_sl is not None:
                    pct_to_sl = round(pct_to_sl, 2)

                total_rr += (rr_now or 0)

            rr_planned = None
            if entry and target and stop and entry != stop:
                if direction == 'long':
                    rr_planned = abs(target - entry) / abs(entry - stop)
                else:
                    rr_planned = abs(entry - target) / abs(stop - entry)
                rr_planned = round(rr_planned, 2)

            positions.append({
                'id': trade.get('id'),
                's': sym,
                'dir': direction,
                'entry': entry,
                'tp': target or None,
                'sl': stop or None,
                'now': current,
                'rr_plan': rr_planned,
                'rr_now': rr_now,
                'pct': pct,
                'pct_to_tp': pct_to_tp,
                'pct_to_sl': pct_to_sl,
                'setup': trade.get('setup_type'),
                'opened': trade.get('opened_at'),
            })

        # Build streak from recent closed trades
        streak = _compute_streak(stats_7d)

        return {
            'open': positions,
            'total_rr': round(total_rr, 2),
            'count': len(positions),
            'stats_7d': {
                'trades': stats_7d.get('trades', 0),
                'wins': stats_7d.get('wins', 0),
                'win_rate': stats_7d.get('win_rate', 0),
                'avg_rr': stats_7d.get('avg_rr', 0),
                'streak': streak,
            },
        }
    except Exception as e:
        logger.warning(f"Failed to get positions for widget: {e}")
        return {'open': [], 'total_rr': 0, 'count': 0, 'stats_7d': {}}


def _compute_streak(stats: dict) -> str:
    """Compute win/loss streak string like '3W' or '2L'."""
    wins = stats.get('wins', 0)
    losses = stats.get('losses', 0)
    if wins == 0 and losses == 0:
        return '0'
    # Simple heuristic — proper streak would need trade-by-trade order
    if wins > losses:
        return f'{wins}W'
    return f'{losses}L'


def _get_report_snippet() -> dict:
    """Get latest morning report date + summary snippet."""
    try:
        from carrac.db_pool import get_conn as _pool_get_conn
        with _pool_get_conn() as conn:
            row = conn.execute(
                '''SELECT id, summary, created_at FROM morning_reports
                   ORDER BY created_at DESC LIMIT 1'''
            ).fetchone()
        if row:
            summary = row['summary'] or ''
            # Truncate for widget
            if len(summary) > 100:
                summary = summary[:97] + '...'
            return {
                # str() first: Postgres hands back a real datetime for
                # created_at (RealDictCursor), which isn't subscriptable —
                # SQLite returns TEXT already.
                'date': str(row['created_at'] or '')[:10],
                'snippet': summary,
            }
    except Exception as e:
        logger.debug(f"Failed to get report snippet: {e}")
    return {'date': None, 'snippet': None}
