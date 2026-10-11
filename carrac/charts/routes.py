"""
Chart routes: OHLCV data, headless chart page, indicator prefs, consensio, volume profile.

``get_chart_data`` and the headless pages are also called by the FastAPI
front doors (fastapi_app/routers/chart_public.py, screenshot_public.py).
Preference writes are served by fastapi_app/routers/preferences.py.
"""

import json
import logging
import os
import re
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from flask import Blueprint, current_app, g, jsonify, request, session, render_template

from carrac.auth.decorators import login_required, auth_or_key_required, verify_api_key
from carrac.extensions import limiter
from carrac.charts.ratios import _handle_ratio_chart
from carrac.charts.service import (
    _calculate_cpr_history,
    _calculate_daily_cpr_history,
    _calculate_weekly_cpr_history,
    _cpr_round,
    _cpr_supports_with_logging,
)
from carrac.charts.indicators import compute_macd_series, compute_sma_lines, _ema_pine_compat
from carrac.charts.indicators import compute_chart_indicators  # noqa: F401 — patched by a test
from carrac.charts.sessions import compute_session_data, compute_future_sessions
from carrac.caches import (
    record_chart_miss as _record_chart_miss,
    get_cached_derivatives as _get_cached_derivatives,
    set_cached_derivatives as _set_cached_derivatives,
    get_cached_chart as _get_cached_chart,
    set_cached_chart as _set_cached_chart,
    get_cached_subhourly as _get_cached_subhourly,
    set_cached_subhourly as _set_cached_subhourly,
    get_cached_vp as _get_cached_vp,
    set_cached_vp as _set_cached_vp,
    SharedTTLCache,
)
# Imported from here, next to _compute_rs_rankings, by the relative-strength
# readers (fastapi_app/routers/scanner.py, reports/routes.py).
from carrac.caches import get_cached_rs as _get_cached_rs  # noqa: F401
from carrac.caches import set_cached_rs as _set_cached_rs  # noqa: F401
from carrac.analysis import calculate_mansfield_rs
from carrac.services import get_services

logger = logging.getLogger('carrac.charts')

charts_bp = Blueprint('charts', __name__)

# ETF proxies for stock indices that don't have a directly-traded volume.
# Volume Profile for these indices is computed from the proxy ETF's volume
# (same approach as TradingView / ThinkOrSwim).
VP_INDEX_ETF_PROXY = {
    'SPX': 'SPY',
    'NDX': 'QQQ',
    'DJI': 'DIA',
}


def _fetch_yahoo_range_for_vp(yahoo_client, symbol, interval, start_ms, end_ms):
    """Fetch OHLCV for VP from Yahoo, sliced to [start_ms, end_ms].

    Yahoo's fetch_hourly takes ``hours`` (rolling from now); we approximate
    the window then slice to the requested range.  ``interval`` is the
    source_interval ('1h', '4h', '1d') — for sub-daily we always use Yahoo
    1h bars and let compute_volume_profile bin from there.
    """
    now_ms = int(_time.time() * 1000)
    # Derive how many hours of history we need (clamped to Yahoo's ~730-day max)
    hours = max(24, int((now_ms - start_ms) / 3_600_000))
    hours = min(hours, 730 * 24)

    df = yahoo_client.fetch_hourly(symbol, hours=hours)
    if df.empty:
        return df

    # Slice to requested range — fetch_hourly returns rolling-from-now,
    # so we trim the extra leading bars.  Index is tz-naive UTC datetime.
    start_dt = pd.to_datetime(start_ms, unit='ms')
    end_dt = pd.to_datetime(end_ms, unit='ms')

    # Yahoo may return tz-aware index; normalise to tz-naive for comparison.
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    df = df[(df.index >= start_dt) & (df.index <= end_dt)]
    return df


def _get_db():
    return get_services().db


def _determine_ws_venue(db, symbol_upper, is_crypto, is_dynamic):
    """Determine the WebSocket venue for a symbol. Used by both chart endpoints.

    Returns (has_live_ws, ws_venue, ws_exchange) where ws_exchange is the
    specific exchange ID for CCXT coins (e.g. 'bybit', 'mexc', 'kucoin', 'gateio').
    """
    if is_crypto and symbol_upper in db.binance_map:
        return True, 'binance', None
    elif is_crypto and db.hyperliquid.is_supported(symbol_upper):
        return True, 'hyperliquid', None
    elif is_crypto and db.ccxt.is_supported(symbol_upper):
        exchange_id = db.ccxt.get_venue_for_symbol(symbol_upper)
        return True, 'ccxt', exchange_id
    elif not is_crypto and (symbol_upper in db.stock_map or symbol_upper in db.etf_map):
        return True, 'finnhub', None
    elif not is_dynamic:
        return True, 'binance', None
    return False, None, None


def _get_consensio():
    return get_services().consensio


def _get_rsi_calc():
    return get_services().rsi_calc


def _get_cpr_calc():
    return get_services().cpr_calc


# ============================================================
# Indicator Preferences (per-user in DB, legacy disk fallback)
# ============================================================

# One-time migration map: old indicator key → new key (EMA Ribbon rename)
_INDICATOR_KEY_MIGRATIONS = {
    'LARSSON': 'EMA_RIBBON',
    'MONEY_NOODLE': 'KELTNER_TIGHT',
}
# SMC split: if old SMC key exists, enable all 4 sub-indicators
_SMC_SPLIT_KEYS = ['MKT_STRUCTURE', 'ORDER_BLOCKS', 'EQ_HL', 'VALUE_ZONES']
_PARAM_KEY_MIGRATIONS = {
    'larsson': 'emaRibbon',
    'moneyNoodle': 'keltnerTight',
}

def _migrate_indicator_prefs(prefs_dict):
    """Migrate old indicator keys in-place. Returns True if any changes were made."""
    changed = False
    indicators = prefs_dict.get('indicators', {})
    for old, new in _INDICATOR_KEY_MIGRATIONS.items():
        if old in indicators and new not in indicators:
            indicators[new] = indicators.pop(old)
            changed = True
        elif old in indicators:
            del indicators[old]
            changed = True
    # SMC split: if old SMC was enabled, enable all 4 sub-indicators
    if 'SMC' in indicators:
        smc_val = indicators.pop('SMC')
        for sub_key in _SMC_SPLIT_KEYS:
            if sub_key not in indicators:
                indicators[sub_key] = smc_val
        changed = True
    # Migrate favourites list
    favourites = prefs_dict.get('favourites', [])
    for i, fav in enumerate(favourites):
        if fav in _INDICATOR_KEY_MIGRATIONS:
            favourites[i] = _INDICATOR_KEY_MIGRATIONS[fav]
            changed = True
    # Migrate param keys
    params = prefs_dict.get('params', {})
    for old, new in _PARAM_KEY_MIGRATIONS.items():
        if old in params and new not in params:
            params[new] = params.pop(old)
            changed = True
        elif old in params:
            del params[old]
            changed = True
    # Migrate keys inside groups
    groups = prefs_dict.get('groups', [])
    for group in groups:
        if isinstance(group, dict) and 'indicators' in group:
            group_inds = group['indicators']
            for old, new in _INDICATOR_KEY_MIGRATIONS.items():
                if old in group_inds and new not in group_inds:
                    group_inds[new] = group_inds.pop(old)
                    changed = True
                elif old in group_inds:
                    del group_inds[old]
                    changed = True
    return changed


# ============================================================
# Routes
# ============================================================

@charts_bp.route('/chart-headless/<asset>/<timeframe>')
def chart_headless(asset: str, timeframe: str):
    """
    Minimal chart page for headless Playwright screenshots.
    No login required — accessed only by server-side screenshot endpoint.

    GET /chart-headless/BTC/1D?indicators=CPR,EMA_RIBBON,WEEKLY_MA with an
    X-Internal-Key or X-API-Key header.
    """
    key = request.headers.get('X-Internal-Key', '') or request.headers.get('X-API-Key', '')
    if not verify_api_key(key):
        return 'Unauthorized', 401
    return render_template('chart_headless.html')


# Slug format shared with the public share loader (carrac/shares/routes.py
# _SLUG_RE): 8-char Crockford-ish alphabet at mint time, 6-12 accepted here
# to be lenient on any future length tweak — mirrors the sibling regex.
_HEADLESS_SHARE_SLUG_RE = re.compile(r'^[a-z0-9]{6,12}$')


@charts_bp.route('/chart-headless/share/<slug>')
def chart_headless_share(slug: str):
    """Slug-scoped headless render for a chart share.

    The regular ``/chart-headless/<asset>/<timeframe>`` page fetches live
    drawings from ``/api/drawings``, which requires per-user auth (session
    or a ``cvx_k_`` per-user API key) — the Playwright render context only
    ever sends the shared internal/admin key, so that fetch 401s and drawings
    silently render as ``[]`` (the pre-existing bug this route fixes).

    We deliberately do NOT fix that by aligning header names: under an
    internal/admin key the resolved user is ambiguous, so granting this page
    live drawings-API access would risk leaking another user's drawings.

    Instead: this route renders the share's OWN frozen, PII-stripped
    drawings — ``_freeze_drawings`` output, already resolved into
    ``snapshot['drawings']`` at mint time — injected server-side. The slug
    is resolved via the SAME public loader ``/s/<slug>`` uses
    (``get_share_by_slug`` — public data only, no auth/session context),
    with matching 404/410/expiry semantics.

    Still gated behind the internal/API key (same as the sibling route)
    — not because the frozen snapshot is sensitive (it's already public via
    ``/s/<slug>``), but because this remains an expensive Playwright render
    surface and the key gate here is a resource/abuse control, not a
    data-confidentiality one.
    """
    key = request.headers.get('X-Internal-Key', '') or request.headers.get('X-API-Key', '')
    if not verify_api_key(key):
        return 'Unauthorized', 401

    if not _HEADLESS_SHARE_SLUG_RE.match(slug or ''):
        return 'Not Found', 404

    from carrac.plays.shares import get_share_by_slug
    share = get_share_by_slug(slug)
    if not share or (share.get('kind') or 'chart') != 'chart':
        return 'Not Found', 404
    if share.get('revoked_at'):
        return 'Gone', 410
    exp = share.get('expires_at')
    if exp:
        try:
            when = datetime.fromisoformat(str(exp).replace('Z', '+00:00'))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < datetime.now(timezone.utc):
                return 'Not Found', 404
        except ValueError:
            pass

    snap = share.get('snapshot') or {}
    symbol = snap.get('symbol')
    timeframe = snap.get('timeframe')
    if not symbol or not timeframe:
        return 'Not Found', 404

    return render_template(
        'chart_headless.html',
        headless_share={
            'symbol': symbol,
            'timeframe': timeframe,
            'drawings': snap.get('drawings') or [],
        },
    )


def _load_legacy_indicator_prefs():
    """Load indicator prefs from the legacy disk file (pre-multi-user migration)."""
    prefs_path = os.path.join('data', 'indicator_prefs.json')
    try:
        with open(prefs_path, 'r') as f:
            raw = json.load(f)
            if isinstance(raw, dict) and 'indicators' in raw:
                result = {'indicators': raw['indicators'], 'favourites': raw.get('favourites', []),
                          'groups': raw.get('groups', []), 'params': raw.get('params', {})}
            elif isinstance(raw, dict):
                legacy_inds = {k: v for k, v in raw.items() if k not in ('screenshot_defaults', 'favourites', 'groups', 'params')}
                result = {'indicators': legacy_inds, 'favourites': raw.get('favourites', []),
                          'groups': raw.get('groups', []), 'params': raw.get('params', {})}
            else:
                return None
            _migrate_indicator_prefs(result)
            return result
    except Exception:
        return None


@charts_bp.route('/api/indicator-prefs', methods=['GET'])
def indicator_prefs():
    """Return per-user indicator preferences (writes are served by FastAPI)."""
    from user_db import PreferencesWriteError, user_db

    user_id = session.get('user_id')
    if not user_id:
        # API-key access (morning engine, scripts). A per-user key resolves to
        # its own owner; only the shared key, which carries no user binding,
        # falls back to the admin account.
        key = request.headers.get('X-Internal-Key', '') or request.headers.get('X-API-Key', '')
        if not verify_api_key(key):
            return jsonify({'error': 'Unauthorized'}), 401
        user_id = getattr(g, 'api_key_user_id', None)
        if not user_id:
            admin_email = os.getenv('ADMIN_EMAIL', '')
            if admin_email:
                admin = user_db.get_user_by_email(admin_email)
                if admin:
                    user_id = admin['id']
        if not user_id:
            return jsonify({'error': 'Admin user not found'}), 500

    all_prefs = user_db.get_preferences(user_id)
    ind_prefs = all_prefs.get('indicator_prefs', {})

    # A user with no saved prefs yet gets the legacy disk file, copied into
    # the database once so later reads come from there.
    if not ind_prefs.get('indicators'):
        legacy = _load_legacy_indicator_prefs()
        if legacy:
            ind_prefs = legacy
            all_prefs['indicator_prefs'] = ind_prefs
            try:
                user_db.save_preferences(user_id, all_prefs)
            except PreferencesWriteError as exc:
                # Still served from the file; the copy is retried next read.
                logger.error('legacy indicator prefs not copied for user %s: %s', user_id, exc)

    _migrate_indicator_prefs(ind_prefs)
    return jsonify({
        'indicators': ind_prefs.get('indicators', {}),
        'favourites': ind_prefs.get('favourites', []),
        'groups': ind_prefs.get('groups', []),
        'params': ind_prefs.get('params', {})
    })


@charts_bp.route('/api/preferences/all', methods=['GET'])
@login_required
def preferences_all():
    """Return all synced preferences for the current user."""
    from user_db import user_db

    user_id = session.get('user_id')
    all_prefs = user_db.get_preferences(user_id)

    # Return only the syncable keys
    syncable = {}
    for key in ('display_options', 'chart_prefs', 'favourite_assets',
                'watchlist_order', 'alert_prefs', 'scenario_panel',
                'indicator_prefs', 'morning_report_prefs',
                'report_indicators', 'report_assets', 'column_prefs',
                'ticker_prefs', 'news_panel_prefs', 'tf_favourites',
                'report_schedule'):
        if key in all_prefs:
            syncable[key] = all_prefs[key]

    return jsonify(syncable)


@charts_bp.route('/api/consensio/<symbol>')
@login_required
def get_consensio(symbol: str):
    """
    Get Consensio values for a symbol.

    Args:
        symbol: Asset symbol (e.g., 'BTC', 'TSLA', 'GOLD')

    Returns:
        JSON with Consensio data
    """
    db = _get_db()
    consensio = _get_consensio()

    try:
        # Get multi-timeframe data from database
        data = db.get_multi_timeframe_data(symbol.upper())

        # Check if we have data
        if data['1D'].empty:
            return jsonify({
                'error': f'No data found for {symbol}',
                'symbol': symbol.upper()
            }), 404

        # Calculate Consensio
        result = consensio.calculate_multi_timeframe(
            data['1D'],
            data['3D'],
            data['1W']
        )

        # Add symbol to result
        result['symbol'] = symbol.upper()

        # Add latest price from database
        if not data['1D'].empty:
            result['latest_price'] = float(data['1D']['close'].iloc[-1])

        return jsonify(result)

    except Exception as e:
        logger.error(f"Chart internal error for {symbol}: {e}")
        return jsonify({
            'error': 'Failed to load chart data',
            'symbol': symbol.upper()
        }), 500


@charts_bp.route('/api/volume-profile/<symbol>')
@auth_or_key_required
def chart_get_volume_profile(symbol: str):
    """Volume Profile: returns price-level volume distribution for chart overlay."""
    from volume_profile import compute_volume_profile, get_source_interval

    db = _get_db()

    tf = request.args.get('tf', '1D')
    # Tier-based TF gating
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    tf_check = tf if tf in ('1D','3D','1W','1M') else tf.lower()
    blocked = get_tier_limits(get_request_tier()).get('blocked_tfs', frozenset())
    if tf_check in blocked:
        tf = '4h'
    start_ms = request.args.get('start', type=int)
    end_ms = request.args.get('end', type=int)

    if not start_ms or not end_ms or end_ms <= start_ms:
        return jsonify({'error': 'start and end timestamps required'}), 400

    # Validate timeframe
    valid_tfs = {'1m', '3m', '5m', '15m', '30m', '1h', '4h', '1D', '3D', '1W', '1M'}
    if tf not in valid_tfs:
        return jsonify({'error': 'invalid timeframe'}), 400

    # num_bins: clamp instead of 400 — matches the tier-gating style of this
    # route, which degrades gracefully rather than rejecting the request.
    num_bins = request.args.get('num_bins', default=200, type=int)
    num_bins = max(20, min(500, num_bins))

    # Round timestamps to improve cache hit rate (round to nearest hour)
    round_ms = 3600000
    start_rounded = (start_ms // round_ms) * round_ms
    end_rounded = ((end_ms // round_ms) + 1) * round_ms

    sym = symbol.upper()
    proxy_symbol = VP_INDEX_ETF_PROXY.get(sym)

    # Cache key includes proxy so future proxy-map changes don't serve stale data.
    # Always append num_bins so different bin counts don't cross-serve.
    cache_key_sym = f"{sym}::{proxy_symbol}" if proxy_symbol else sym
    cache_key = f"vp:{cache_key_sym}:{tf}:{start_rounded}:{end_rounded}:{num_bins}"
    cached = _get_cached_vp(cache_key)
    if cached is not None:
        return jsonify(cached)

    # Determine source interval
    source_interval = get_source_interval(tf)
    if not source_interval:
        # Sub-hourly TFs or unknown — VP not available
        return jsonify({'bins': [], 'poc': 0, 'vah': 0, 'val': 0})

    # Route to the correct data source.
    if proxy_symbol:
        # Index → ETF proxy path. Use Yahoo Finance hourly OHLCV+volume.
        df = _fetch_yahoo_range_for_vp(db.yahoo, proxy_symbol, source_interval, start_rounded, end_rounded)
    elif sym in db.crypto_map or sym in db.BINANCE_SYMBOL_MAP:
        df = db.fetch_klines_for_range(sym, source_interval, start_rounded, end_rounded)
    else:
        # VP not available for this asset class yet.
        return jsonify({'bins': [], 'poc': 0, 'vah': 0, 'val': 0, 'unavailable': True})

    if df is None or df.empty:
        return jsonify({'bins': [], 'poc': 0, 'vah': 0, 'val': 0})

    result = compute_volume_profile(df, num_bins=num_bins)
    if result is None:
        return jsonify({'bins': [], 'poc': 0, 'vah': 0, 'val': 0})

    if proxy_symbol:
        result['proxy'] = proxy_symbol
        result['proxy_source'] = 'yahoo'

    result['source_interval'] = source_interval
    result['bar_count'] = len(df)
    result['num_bins'] = num_bins

    _set_cached_vp(cache_key, result)
    return jsonify(result)


# Naked-POC cache — same SharedTTLCache constructor pattern as
# the vp/derivatives/etc. caches in carrac/caches.py, kept local here.
_npoc_cache = SharedTTLCache('npoc', ttl=300, max_local=50)


def _get_cached_npoc(key):
    return _npoc_cache.get(key)


def _set_cached_npoc(key, data):
    _npoc_cache.set(key, data)


@charts_bp.route('/api/naked-pocs/<symbol>')
@auth_or_key_required
def get_naked_pocs(symbol: str):
    """Naked (virgin) POCs: completed daily sessions whose point-of-control
    price has never been revisited by later price action."""
    from volume_profile import compute_naked_pocs

    db = _get_db()
    sym = symbol.upper()

    # Crypto only — no ETF-proxy/index or stock path for this feature yet.
    if sym not in db.crypto_map and sym not in db.BINANCE_SYMBOL_MAP:
        return jsonify({'levels': [], 'unavailable': True})

    # lookback: clamp instead of 400 — matches the volume-profile route's
    # num_bins clamp, which degrades gracefully rather than rejecting.
    lookback = request.args.get('lookback', default=90, type=int)
    lookback = max(10, min(365, lookback))
    include_touched = request.args.get('include_touched', default='0') == '1'

    now_ms = int(_time.time() * 1000)
    day_ms = 86_400_000
    today_midnight_ms = (now_ms // day_ms) * day_ms
    start_ms = today_midnight_ms - (lookback + 1) * day_ms
    end_ms = now_ms

    # Round "now" to the hour for the cache key — bounds key cardinality
    # while the 300s TTL keeps data fresh regardless.
    hour_ms = 3_600_000
    now_hour = (now_ms // hour_ms) * hour_ms
    cache_key = f"npoc:{sym}:{lookback}:{1 if include_touched else 0}:{now_hour}"
    cached = _get_cached_npoc(cache_key)
    if cached is not None:
        return jsonify(cached)

    df = db.fetch_klines_for_range(sym, '15m', start_ms, end_ms)
    if df is None or df.empty:
        return jsonify({'levels': []})

    if not isinstance(df.index, pd.DatetimeIndex):
        # Some venue paths (e.g. Binance's fetch_klines_for_range, used by
        # the volume-profile route too) return raw OHLCV without a
        # timestamp index since VP itself doesn't need one. Naked-POC does
        # need per-bar timestamps to group by UTC day, so rebuild one here
        # assuming uniform 15m spacing from the requested window start.
        # Best-effort: correct as long as the source has no gaps.
        df = df.copy()
        df.index = pd.date_range(
            start=pd.to_datetime(start_ms, unit='ms'), periods=len(df), freq='15min'
        )

    levels = compute_naked_pocs(df, lookback_days=lookback, num_bins=50)

    if not include_touched:
        levels = [lv for lv in levels if lv['naked']][:15]

    result = {
        'levels': levels,
        'lookback_days': lookback,
        'source_interval': '15m',
    }

    _set_cached_npoc(cache_key, result)
    return jsonify(result)


def _to_columnar(result):
    """Convert array-of-objects fields to columnar format for smaller JSON."""
    ohlcv = result.get('ohlcv', [])
    if ohlcv and isinstance(ohlcv, list):
        result['ohlcv'] = {
            't': [c['time'] for c in ohlcv],
            'o': [c['open'] for c in ohlcv],
            'h': [c['high'] for c in ohlcv],
            'l': [c['low'] for c in ohlcv],
            'c': [c['close'] for c in ohlcv],
        }
    vol = result.get('volume', [])
    if vol and isinstance(vol, list):
        result['volume'] = {
            't': [v['time'] for v in vol],
            'v': [v['value'] for v in vol],
            'd': [1 if v.get('color', '').startswith('rgba(38') else 0 for v in vol],
        }
    rsi = result.get('rsi', [])
    if rsi and isinstance(rsi, list):
        result['rsi'] = {
            't': [r['time'] for r in rsi],
            'v': [r['value'] for r in rsi],
            'ma': [r.get('ma') for r in rsi],
        }
    cvd = result.get('cvd_spot', [])
    if cvd and isinstance(cvd, list):
        result['cvd_spot'] = {
            't': [c['time'] for c in cvd],
            'cvd': [c['cvd'] for c in cvd],
            'delta': [c['delta'] for c in cvd],
        }
    return result


_FORMING_INTERVAL_MS = {
    '1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000,
    '30m': 1_800_000, '1h': 3_600_000, '4h': 14_400_000,
}

# Case-canonical timeframe spellings. The frontend emits both '1H' (keyboard
# shortcuts, some toolbars) and '1h' (TF lists) for the same timeframe;
# unnormalised they produce distinct cache keys, wrong per-TF TTLs, and warm
# keys nobody reads. '1m'/'1M' are ABSENT on purpose — minute vs month differ
# only by case and must pass through untouched.
_TF_CANON = {
    '1H': '1h', '4H': '4h',
    '3M': '3m', '5M': '5m', '15M': '15m', '30M': '30m',
    '1d': '1D', '3d': '3D', '1w': '1W',
}


PRELOAD_HINT_HEADER = 'X-Carrac-Preload'


def _cold_build_verdict() -> str:
    """Classify a cold chart build for the timing log and the miss ledger.

    ``refresh`` — SWR revalidate / worker warm pass (tier-bypass flag set);
    nobody waited. ``preload`` — the frontend's background timeframe preload
    (``X-Carrac-Preload: 1`` from main.js ``preloadKlineData``); nobody waited
    either. ``miss`` — a real request blocked on the build. Only ``miss``
    feeds the blocking-miss share (gold-check greps hit|stale|miss) and the
    ``mcd:chartmiss`` ledger the warm tier is sized from: before
    this split, the preload of every TF of the open symbol every 30 minutes
    was 37% of the ledger and read as users waiting.
    """
    if _is_tier_bypass_active():
        return 'refresh'
    if request.headers.get(PRELOAD_HINT_HEADER) == '1':
        return 'preload'
    return 'miss'


def build_chart_cache_suffix(tf, columnar, quant_param=None, now_ms=None):
    """The cache-key suffix a chart request appends to its TF — the ONE
    source of truth.

    The worker's warm path (precompute.py::_chart_warm_lookup_tf) builds its
    keys through this function too; a hand-synced copy would risk warm
    poisoning: one drift and the warmer populates keys no request ever reads,
    silently.
    tests/test_chart_cache_key_parity.py pins the parity.

    Components, in order:
    - ``:col`` when the payload is columnar (the frontend always sends
      fmt=col; warm requests mirror that).
    - ``:q<sorted series>`` — quant overlay is baked into the cached payload,
      so it must be part of the key or a request without ?quant could be
      served a payload some OTHER caller built with an overlay attached.
      Sorted so param order cannot fragment the key.
    - ``:b<epoch_bucket>`` for intraday TFs, ALWAYS LAST — a payload built in
      one candle period must never be served in the next (it lacks the new
      forming candle and re-trips the client's 30s boundary-retry loop), and
      caches.py strips exactly this trailing component for the SWR
      bucket-agnostic alias. HTF/daily keys carry no bucket (live bar is
      handled by _inject_live_candle).
    """
    suffix = ':col' if columnar else ''
    if quant_param:
        quant_sorted = sorted(s.strip() for s in quant_param.split(',') if s.strip())
        suffix += ':q' + ','.join(quant_sorted)
    bucket_ms = _FORMING_INTERVAL_MS.get(str(tf).lower())
    if bucket_ms:
        ms = int(_time.time() * 1000) if now_ms is None else now_ms
        suffix += f":b{ms // bucket_ms}"
    return suffix


def _ensure_forming_candle(df, timeframe):
    """Intraday charts: guarantee the CURRENT (forming) candle is present.

    Tier-1 LTF/sub-hourly chart data is precomputed/cached and stops at the last
    CLOSED candle, so the client's candle-close check (`lastBar + interval <= now`)
    is ~always true → an endless 30s `boundary-retry` reload loop (flicker + jump).
    Daily/HTF already carry the live bar via `_inject_live_candle`; this does the
    same for intraday: append a placeholder forming bar at the current period's
    open (O/H/L/C = last close, volume 0) when the last bar's period has just
    ended. The live WS tick fills it in client-side; server indicators treat it as
    the (incomplete) current bar, which is correct.

    Gap-safe: only injects when the data is EXACTLY one period behind (the normal
    case — every intraday cache TTL is shorter than its candle period). If the data
    is >1 period stale (a cache/precompute malfunction) it injects nothing, leaving
    the normal reload to refetch rather than drawing a candle over a gap.
    """
    iv_ms = _FORMING_INTERVAL_MS.get(str(timeframe).lower())
    if not iv_ms or df is None or df.empty:
        return df
    try:
        now_ms = int(_time.time() * 1000)
        cur_open_ms = (now_ms // iv_ms) * iv_ms
        last_ms = int(df.index[-1].value // 1_000_000)  # pandas Timestamp ns → ms
        if last_ms >= cur_open_ms:
            return df  # forming candle already present
        if last_ms < cur_open_ms - iv_ms:
            return df  # >1 period stale → gap risk, let the normal reload refetch
        last_close = float(df['close'].iloc[-1])
        row = {col: (0.0 if col in ('volume', 'taker_buy_base') else last_close)
               for col in df.columns}
        forming = pd.DataFrame([row], index=[pd.Timestamp(cur_open_ms, unit='ms')])
        return pd.concat([df, forming])
    except Exception as e:
        logger.debug("forming-candle inject skipped (%s): %s", timeframe, e)
        return df


def _chart_browser_ttl(tf):
    """Browser max-age for a chart response. HTF: 60s. Intraday: 30s, clamped so
    the cached copy never outlives the current candle period — a payload reused
    by the browser past the close lacks the next forming candle and costs one
    spurious stale-retry per close."""
    if tf in ('1D', '3D', '1W', '1M'):
        return 60
    iv_ms = _FORMING_INTERVAL_MS.get(str(tf).lower())
    if not iv_ms:
        return 30
    remaining_s = (iv_ms - (int(_time.time() * 1000) % iv_ms)) // 1000
    return max(1, min(30, int(remaining_s)))


# ============================================================
# Chart SWR background refresh
#
# A stale cache hit is served immediately; the rebuild that would otherwise
# have blocked the request runs here instead. Bounded on two axes:
#   - ThreadPoolExecutor(max_workers=2): caps CPU cost per gunicorn worker.
#   - Redis SET-NX single-flight lock: caps it to ONE rebuild per cache key
#     across all 4 gunicorn workers (they don't share the ThreadPoolExecutor).
# ============================================================
_CHART_REFRESH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='chart-refresh')
_CHART_REFRESH_LOCK_TTL = 180   # seconds — Redis single-flight guard
_CHART_REFRESH_GUARD_S = 30     # in-process floor when Redis is unreachable
_chart_refresh_last = {}        # cache_key -> last-spawned monotonic ts (Redis-down fallback only)
_chart_refresh_last_lock = threading.Lock()

# Set (per-thread) by the background-refresh runner so the replayed request
# bypasses the tier-gating rewrites below — the replay has no session, so
# get_request_tier() would see 'free' and rewrite/strip params, poisoning the
# rebuilt cache entry (it must reproduce the ORIGINAL caller's already-gated
# params exactly). NOT settable via any request parameter — no HTTP surface.
_chart_refresh_bypass_tier = threading.local()

# Set (per-thread) by the FastAPI front door
# (fastapi_app/routers/chart_public.py) around its replay of THIS view, so
# the inner @limiter.limit("120 per minute") below is skipped for that one
# call. The FastAPI side already applied the SAME 120/minute limit against
# the caller's real identity (session/API-key) before spawning the replay;
# re-checking it here would either double-count that hit or — worse — merge
# EVERY migrated caller into one bucket, since the replay's remote_addr is
# always 127.0.0.1 inside test_request_context (a 429-storm above 120 total
# chart requests/minute system-wide). NOT settable via any request
# parameter — no HTTP surface, set/cleared only by the FastAPI wrapper
# around the view call, in the same thread. The SWR
# background-refresh replay above is a DIFFERENT call path and is
# deliberately NOT exempted here — it keeps consuming inner-limiter hits
# exactly as it does today.
_chart_via_asgi_replay = threading.local()


def _is_asgi_replay_active() -> bool:
    return getattr(_chart_via_asgi_replay, 'active', False)


def _is_tier_bypass_active() -> bool:
    return getattr(_chart_refresh_bypass_tier, 'active', False)


def _try_acquire_chart_refresh_lock(cache_key: str) -> bool:
    """Cross-worker single-flight guard for one background rebuild per key.

    Best-effort in-process fallback (timestamp dict) when Redis is down —
    bounds re-spawns to at most one per _CHART_REFRESH_GUARD_S per process,
    which is weaker than the Redis lock (per-process, not cross-worker) but
    still prevents a hot stale key from flooding the local thread pool.
    """
    from carrac.redis_cache import get_redis
    r = get_redis()
    if r:
        try:
            return bool(r.set(f'mcd:c:chartrefresh:{cache_key}', '1', nx=True, ex=_CHART_REFRESH_LOCK_TTL))
        except Exception as e:
            logger.debug(f"chart refresh lock (redis) failed, using in-process guard: {e}")
    now = _time.time()
    with _chart_refresh_last_lock:
        last = _chart_refresh_last.get(cache_key, 0)
        if now - last < _CHART_REFRESH_GUARD_S:
            return False
        _chart_refresh_last[cache_key] = now
    return True


def _release_chart_refresh_lock(cache_key: str) -> None:
    from carrac.redis_cache import get_redis
    r = get_redis()
    if r:
        try:
            r.delete(f'mcd:c:chartrefresh:{cache_key}')
        except Exception as e:
            logger.debug(f"chart refresh lock release failed: {e}")


def _build_chart_refresh_query(tf_for_cache, mode, lite, columnar, quant_param, days_raw):
    """Replay query string for the background rebuild: the SAME post-tier-
    gating params the stale-serving request resolved to, plus fresh=1 (bypass
    the cache lookup so the miss/rebuild path always runs)."""
    parts = [f'timeframe={tf_for_cache}', f'mode={mode}', 'fresh=1']
    if lite:
        parts.append('lite=1')
    if columnar:
        parts.append('fmt=col')
    if quant_param:
        parts.append('quant=' + quant_param)
    if days_raw is not None:
        parts.append(f'days={days_raw}')
    return '&'.join(parts)


def _run_chart_refresh(app, symbol, query, cache_key):
    """Background job body (runs on _CHART_REFRESH_POOL): rebuild ONE stale
    chart cache entry via a synthetic request context (no real HTTP round
    trip), then release the single-flight lock. Best-effort — any failure is
    logged, not raised; the caller that got the stale payload is unaffected
    either way, and the next stale hit will just retry.
    """
    _chart_refresh_bypass_tier.active = True
    try:
        # env first — auth's verify path compares against os.getenv (decorators.py
        # _verify), and prod never copies the key into app.config.
        internal_key = os.getenv('SCREENSHOT_API_KEY', '') or app.config.get('SCREENSHOT_API_KEY', '')
        headers = {'X-Internal-Key': internal_key} if internal_key else {}
        with app.test_request_context(f'/api/chart/{symbol}?{query}', headers=headers):
            get_chart_data(symbol)
    except Exception as e:
        logger.warning(f"chart background refresh failed for {cache_key}: {e}")
    finally:
        _chart_refresh_bypass_tier.active = False
        _release_chart_refresh_lock(cache_key)


def _spawn_chart_refresh(symbol, tf_for_cache, mode, lite, columnar, quant_param, days_raw, cache_key):
    """Fire a bounded background rebuild for a stale chart cache entry.

    No-op (silently) if the single-flight lock can't be acquired — another
    worker/thread is already rebuilding this key — or if there's no live app
    object to replay against.
    """
    if not _try_acquire_chart_refresh_lock(cache_key):
        return
    try:
        app = current_app._get_current_object()
    except Exception:
        _release_chart_refresh_lock(cache_key)
        return
    query = _build_chart_refresh_query(tf_for_cache, mode, lite, columnar, quant_param, days_raw)
    _CHART_REFRESH_POOL.submit(_run_chart_refresh, app, symbol, query, cache_key)


@dataclass
class _ChartRequest:
    """The /api/chart query, after timeframe canonicalisation and tier gating."""
    symbol: str              # upper-cased
    mode: str                # 'htf' | 'ltf'
    requested_tf: str | None  # canonical case
    lite: bool
    fresh: bool
    columnar: bool
    derivatives_param: str   # e.g. "oi,cvd_usdt,cvd_coin"
    quant_param: str         # e.g. "M2,NET_LIQUIDITY,FEAR_GREED"
    want_macd: bool
    is_dynamic: bool
    tf_for_cache: str
    cache_suffix: str


@dataclass
class _ChartFrames:
    """The OHLCV frames one chart is built from."""
    df: pd.DataFrame             # the frame being charted, before trimming
    timeframe: str | None        # the timeframe actually served
    tf_normalized: str | None    # requested timeframe, case-normalised
    days: int                    # bars to serve
    tf_data: dict | None         # HTF frames by timeframe (daily at least)
    ltf_data: dict | None        # LTF frames by timeframe
    daily_df: pd.DataFrame | None  # dynamic symbols: the on-demand daily frame


@dataclass
class _ChartSeries:
    """The per-bar series every chart response carries."""
    ts_list: list
    ohlcv: list
    volume: list
    cvd_spot: list
    rsi: list


_HTF_TIMEFRAMES = ('1D', '3D', '1W', '1M')
_LTF_TIMEFRAMES = ('1m', '3m', '5m', '15m', '30m', '1h', '4h')


def _fire_first_chart_view(symbol):
    """first_chart_view funnel event. Idempotent — flips a per-user flag in
    users.flags on first hit so later loads don't re-fire. Best-effort."""
    try:
        uid = session.get('user_id')
        if uid:
            from carrac.integrations import fire_first_event
            fire_first_event(uid, 'first_chart_view', {'symbol': (symbol or '').upper()})
    except Exception:
        pass


def _parse_chart_request(db, symbol):
    """Read the query string and apply the tier gates."""
    mode = request.args.get('mode', 'htf')
    requested_tf = request.args.get('timeframe', None)
    # Canonicalise the timeframe's case BEFORE anything keys off it. Real
    # traffic sends BOTH '1H' (keyboard shortcuts) and '1h' (TF lists) for
    # the same timeframe — case-sensitive cache keys fragmented the chart
    # cache per spelling, broke the per-TF TTL lookup, and made the
    # Warm job (lowercase keys) invisible to uppercase readers.
    # '1m' (minute) vs '1M' (month) differ ONLY by case — never touch them.
    if requested_tf:
        requested_tf = _TF_CANON.get(requested_tf, requested_tf)
    # Tier-based TF gating: free users blocked from sub-4H. Skipped during
    # a background refresh replay (see _is_tier_bypass_active)
    # — that request has no session, so get_request_tier() would see
    # 'free' and rewrite the TF, poisoning the rebuilt cache entry (it
    # must reproduce the ORIGINAL requester's already-gated tf exactly).
    if requested_tf and not _is_tier_bypass_active():
        from carrac.auth.decorators import get_request_tier, get_tier_limits
        tf_check = requested_tf if requested_tf in _HTF_TIMEFRAMES else requested_tf.lower()
        blocked = get_tier_limits(get_request_tier()).get('blocked_tfs', frozenset())
        if tf_check in blocked:
            requested_tf = '4h'
    lite = request.args.get('lite', '0') == '1'
    fresh = request.args.get('fresh', '0') == '1'
    columnar = request.args.get('fmt') == 'col'
    derivatives_param = request.args.get('derivatives', '')
    # Strip premium derivatives for free-tier users (same replay bypass as above)
    if derivatives_param and not _is_tier_bypass_active():
        from carrac.auth.decorators import get_request_tier, get_tier_limits, PREMIUM_DERIVATIVES
        tier = get_request_tier()
        if not get_tier_limits(tier)['derivatives']:
            parts = [d.strip() for d in derivatives_param.split(',')]
            parts = [d for d in parts if d not in PREMIUM_DERIVATIVES]
            derivatives_param = ','.join(parts)
    quant_param = request.args.get('quant', '')
    # All 14 macro/quant chart series (M2, Net Liquidity, DXY,
    # Fear & Greed, Regime, ...) are premiumOnly on the frontend — gated
    # behind window._hasPremiumFeatures (premium/admin/tester, NOT
    # member — a stricter bar than the plain 'derivatives' tier limit
    # above). Strip the whole param for anyone below that bar, same
    # replay bypass as derivatives so the SWR rebuild
    # reproduces the original requester's already-gated params exactly.
    if quant_param and not _is_tier_bypass_active():
        from carrac.auth.decorators import get_request_tier
        from carrac.tiers import TESTER_OR_HIGHER
        if get_request_tier() not in TESTER_OR_HIGHER:
            quant_param = ''
    # Optional top-level MACD(12,26,9) overlay — same series /api/chart-internal
    # ships. Opt-in so the default response stays byte-identical for existing
    # consumers (web app, curl bridge, MCP). When requested we bypass the
    # shared chart cache (read + write) so the cached payload never carries
    # macd and macd is never served to a caller that didn't ask for it.
    want_macd = request.args.get('macd', '0').lower() in ('1', 'true')
    symbol_upper = symbol.upper()

    # Curated vs dynamic (Tier 2). A dynamic symbol is validated only on a
    # cache miss — see get_chart_data.
    is_dynamic = not db.is_curated(symbol_upper) and '_' not in symbol_upper

    tf_for_cache = requested_tf or ('4h' if mode == 'ltf' else '1D')
    return _ChartRequest(
        symbol=symbol_upper, mode=mode, requested_tf=requested_tf, lite=lite,
        fresh=fresh, columnar=columnar, derivatives_param=derivatives_param,
        quant_param=quant_param, want_macd=want_macd, is_dynamic=is_dynamic,
        tf_for_cache=tf_for_cache,
        cache_suffix=build_chart_cache_suffix(tf_for_cache, columnar, quant_param),
    )


def _serve_cached_chart(req, t0):
    """The chart response cache (90s TTL, SWR-aware).

    Returns the response when the cache can answer, else None. Derivatives
    requests (rare, need fresh data), fresh=1 (manual refresh of a stale
    dynamic asset) and macd requests always rebuild. A stale entry is served
    at once with stale-marker headers while a background rebuild runs.
    """
    if req.derivatives_param or req.fresh or req.want_macd:
        return None
    _cached = _get_cached_chart(req.symbol, req.tf_for_cache + req.cache_suffix, req.lite)
    if _cached is None:
        return None
    cached_chart, is_fresh = _cached
    cached_chart['_server_ts'] = int(_time.time())
    if not is_fresh:
        # Compute age BEFORE maybe_compact — compact_chart drops most
        # top-level keys, including _cached_at.
        _age_s = max(0, int(_time.time() - (cached_chart.get('_cached_at') or _time.time())))
    from carrac.formatters import maybe_compact, compact_chart
    cached_chart = maybe_compact(cached_chart, compact_chart)
    resp = jsonify(cached_chart)
    if is_fresh:
        _browser_ttl = _chart_browser_ttl(req.tf_for_cache)
        resp.headers['Cache-Control'] = f'private, max-age={_browser_ttl}'
        resp.headers.pop('Pragma', None)
        resp.headers.pop('Expires', None)
        logger.info("chart %s tf=%s hit %dms", req.symbol, req.tf_for_cache, int((_time.perf_counter() - t0) * 1000))
    else:
        # Mirror the SAME stale-marker headers the service worker sets
        # on its own offline fallback (the fetch handler in static/service-worker.js)
        # so the client's existing staleness-badge / auto-refresh
        # machinery (the staleness handling in static/js/chart-loading.js) fires identically for a
        # server-served stale response.
        resp.headers['X-Carrac-Stale'] = '1'
        resp.headers['X-Carrac-Age'] = str(_age_s)
        # Browser must NOT cache this itself — next load should re-hit
        # the server so it gets the (hopefully by-then-fresh) rebuild
        # instead of replaying its own stale copy.
        resp.headers['Cache-Control'] = 'no-store'
        resp.headers.pop('Pragma', None)
        resp.headers.pop('Expires', None)
        logger.info("chart %s tf=%s stale %dms age=%ds", req.symbol, req.tf_for_cache, int((_time.perf_counter() - t0) * 1000), _age_s)
        _cache_key = f"{req.symbol}:{req.tf_for_cache}{req.cache_suffix}:{1 if req.lite else 0}"
        _spawn_chart_refresh(req.symbol, req.tf_for_cache, req.mode, req.lite, req.columnar,
                             req.quant_param, request.args.get('days'), _cache_key)
    return resp


def _load_dynamic_frames(db, symbol_upper, is_crypto, tf_normalized, days):
    """Frames for a dynamic (Tier 2) symbol: fetched on demand, no precompute.
    HTFs are resampled from the daily frame. Returns
    (df, timeframe, tf_data, daily_df); df is empty when there is no data."""
    daily_df = db.get_dynamic_ohlcv(symbol_upper)
    tf_data = {'1D': daily_df}
    if daily_df.empty:
        return daily_df, None, tf_data, daily_df
    if len(daily_df) >= 3:
        tf_data['3D'] = db.resample_to_timeframe(daily_df, '3D')
    if len(daily_df) >= 7:
        tf_data['1W'] = db.resample_to_timeframe(daily_df, '1W')
    if len(daily_df) >= 30:
        tf_data['1M'] = db.resample_to_timeframe(daily_df, '1M')
    # Dynamic crypto can have LTF via Binance klines (or Hyperliquid for HL-only coins)
    if is_crypto and tf_normalized and tf_normalized in _LTF_TIMEFRAMES:
        # NOTE: fetch_klines_for_chart routes to Hyperliquid for HL-only coins.
        # Never mix venues for the same symbol — see _inject_live_candle invariant.
        df = db.fetch_klines_for_chart(symbol_upper, interval=tf_normalized, bars=days) if hasattr(db, 'fetch_klines_for_chart') else pd.DataFrame()
        if df.empty:
            return daily_df, '1D', tf_data, daily_df
        return df, tf_normalized, tf_data, daily_df
    target_tf = tf_normalized if tf_normalized in _HTF_TIMEFRAMES else '1D'
    return tf_data.get(target_tf, daily_df), target_tf, tf_data, daily_df


def _load_curated_frames(db, symbol_upper, req, is_crypto, tf_normalized, days):
    """Frames for a curated symbol. Returns (df, timeframe, tf_data, ltf_data);
    tf_data / ltf_data are None when this timeframe did not need them."""
    tf_data = None
    ltf_data = None
    if req.requested_tf:
        if tf_normalized in ['1m', '3m', '5m', '15m', '30m']:
            if is_crypto:
                df = _get_cached_subhourly(symbol_upper, tf_normalized)
                if df is None:
                    # NOTE: fetch_klines_for_chart routes to Hyperliquid for HL-only coins.
                    df = db.fetch_klines_for_chart(symbol_upper, interval=tf_normalized, bars=days)
                    if not df.empty:
                        _set_cached_subhourly(symbol_upper, tf_normalized, df)
                timeframe = tf_normalized
            else:
                tf_data = db.get_multi_timeframe_data(symbol_upper)
                df = tf_data.get('1D', pd.DataFrame())
                timeframe = '1D'
        elif tf_normalized in ['1h', '4h']:
            # Lower timeframes — crypto via Binance, non-crypto via Yahoo
            ltf_data = db.get_ltf_multi_timeframe_data(symbol_upper)
            df = ltf_data.get(tf_normalized, pd.DataFrame())
            timeframe = tf_normalized
            # Also fetch HTF data for weekly indicators (Bull Market Band, Weekly MAs)
            tf_data = db.get_multi_timeframe_data(symbol_upper)
        else:
            # Higher timeframes: 1D, 3D, 1W
            tf_data = db.get_multi_timeframe_data(symbol_upper)
            df = tf_data.get(tf_normalized, pd.DataFrame())
            timeframe = tf_normalized
    elif req.mode == 'ltf' and is_crypto:
        # Legacy LTF mode: use 4H data for crypto
        ltf_data = db.get_ltf_multi_timeframe_data(symbol_upper)
        df = ltf_data.get('4h', pd.DataFrame())
        timeframe = '4H'
    else:
        # Legacy HTF mode: daily data
        tf_data = db.get_multi_timeframe_data(symbol_upper)
        df = tf_data.get('1D', pd.DataFrame())
        timeframe = '1D'
    return df, timeframe, tf_data, ltf_data


def _load_chart_frames(db, req, is_crypto):
    """Fetch the multi-timeframe data ONCE; it is reused for the chart, the
    analysis and the CPR levels. ``frames.df`` is empty when there is no data."""
    symbol_upper = req.symbol
    # Max bars: 25000 for LTF, 5000 for HTF (extended by backfill)
    tf_normalized = None
    if req.requested_tf:
        tf_normalized = req.requested_tf if req.requested_tf in _HTF_TIMEFRAMES else req.requested_tf.lower()
    is_ltf = tf_normalized in _LTF_TIMEFRAMES if tf_normalized else (req.mode == 'ltf')
    max_bars = 25000 if is_ltf else 5000
    days = min(request.args.get('days', max_bars, type=int), max_bars)

    daily_df = None
    ltf_data = None
    if req.is_dynamic:
        df, timeframe, tf_data, daily_df = _load_dynamic_frames(
            db, symbol_upper, is_crypto, tf_normalized, days)
    else:
        df, timeframe, tf_data, ltf_data = _load_curated_frames(
            db, symbol_upper, req, is_crypto, tf_normalized, days)

    # Sub-daily charts still need daily data for the weekly MAs and CPR.
    if tf_data is None:
        tf_data = db.get_multi_timeframe_data(symbol_upper)

    return _ChartFrames(df=df, timeframe=timeframe, tf_normalized=tf_normalized,
                        days=days, tf_data=tf_data, ltf_data=ltf_data,
                        daily_df=daily_df)


def _ohlcv_series(df):
    """(timestamps, OHLCV, volume) in lightweight-charts shape (vectorized)."""
    timestamps = df.index.astype('datetime64[s]').astype(int)
    ts_list = timestamps.tolist()
    opens = df['open'].astype(float).tolist()
    highs = df['high'].astype(float).tolist()
    lows = df['low'].astype(float).tolist()
    closes = df['close'].astype(float).tolist()

    # Always array-of-objects here (the marker code reads it); converted to
    # columnar at the end when requested.
    ohlcv = [
        {'time': ts_list[i], 'open': opens[i], 'high': highs[i], 'low': lows[i], 'close': closes[i]}
        for i in range(len(ts_list))
    ]

    # Volume coloured by candle direction
    volume = []
    if 'volume' in df.columns:
        vol_vals = df['volume'].astype(float).tolist()
        vol_notna = df['volume'].notna().tolist()
        up_color = 'rgba(38, 166, 154, 0.5)'
        down_color = 'rgba(239, 83, 80, 0.5)'
        volume = [
            {'time': ts_list[i], 'value': vol_vals[i], 'color': up_color if closes[i] >= opens[i] else down_color}
            for i in range(len(ts_list)) if vol_notna[i]
        ]

    return ts_list, ohlcv, volume


def _spot_cvd_series(df):
    """Spot CVD from taker_buy_base, detrended (CVD minus its EMA-50) into an
    oscillator around zero that shows momentum shifts. [] without taker data."""
    if not ('taker_buy_base' in df.columns and df['taker_buy_base'].notna().any()):
        return []
    mask = df['taker_buy_base'].notna() & df['volume'].notna() & (df['volume'] > 0)
    taker_buy = df.loc[mask, 'taker_buy_base'].astype(float)
    total_vol = df.loc[mask, 'volume'].astype(float)
    deltas = 2 * taker_buy - total_vol  # buy - sell = taker_buy - (total - taker_buy)
    cumulative = deltas.cumsum()
    cvd_ema = cumulative.ewm(span=50, adjust=False).mean()
    detrended = cumulative - cvd_ema
    cvd_ts = df.loc[mask].index.astype('datetime64[s]').astype(int).tolist()
    deltas_list = deltas.round(4).tolist()
    detrended_list = detrended.round(4).tolist()
    return [
        {'time': cvd_ts[i], 'cvd': detrended_list[i], 'delta': deltas_list[i]}
        for i in range(len(cvd_ts))
    ]


def _rsi_with_ma_series(df, rsi_calc):
    """RSI(14) series with its 14-bar moving average (Wilder's smoothing)."""
    rsi_data = []
    if len(df) < 15:
        return rsi_data
    rsi_series = rsi_calc.calculate_series(df)
    if rsi_series is not None and len(rsi_series) > 0:
        rsi_ma = rsi_series.rolling(window=14, min_periods=14).mean()
        rsi_notna = rsi_series.dropna()
        if len(rsi_notna) > 0:
            rsi_ts = rsi_notna.index.astype('datetime64[s]').astype(int).tolist()
            rsi_vals = rsi_notna.round(2).astype(float).tolist()
            rsi_ma_aligned = rsi_ma.reindex(rsi_notna.index)
            for i in range(len(rsi_ts)):
                pt = {'time': rsi_ts[i], 'value': rsi_vals[i]}
                ma_v = rsi_ma_aligned.iloc[i]
                if pd.notna(ma_v):
                    pt['ma'] = round(float(ma_v), 2)
                rsi_data.append(pt)
    return rsi_data


def _fetch_chart_derivatives(db, req, is_crypto, frames):
    """Futures OI and CVD series, only when the client asked for them (the
    indicators are enabled) and never for lite requests.
    Returns (oi_hist, cvd_usdt, cvd_coin)."""
    oi_hist_data = []
    cvd_usdt_data = []
    cvd_coin_data = []
    want_oi = 'oi' in req.derivatives_param
    want_cvd_usdt = 'cvd_usdt' in req.derivatives_param
    want_cvd_coin = 'cvd_coin' in req.derivatives_param
    if not (is_crypto and not req.lite and (want_oi or want_cvd_usdt or want_cvd_coin)):
        return oi_hist_data, cvd_usdt_data, cvd_coin_data
    symbol_upper = req.symbol
    deriv_period_map = {
        '1m': '5m', '3m': '5m', '5m': '5m', '15m': '15m', '30m': '30m',
        '1h': '1h', '4h': '4h', '1H': '1h', '4H': '4h',
        '1D': '1d', '3D': '1d', '1W': '1d', '1M': '1d'
    }
    deriv_period = deriv_period_map.get(frames.tf_normalized or frames.timeframe, '1h')

    if want_oi:
        oi_cache_key = f"oi:{symbol_upper}:{deriv_period}"
        oi_hist_data = _get_cached_derivatives(oi_cache_key)
        if oi_hist_data is None:
            oi_raw = db.fetch_open_interest_hist(symbol_upper, period=deriv_period, limit=500)
            oi_hist_data = oi_raw or []
            _set_cached_derivatives(oi_cache_key, oi_hist_data)

    if want_cvd_usdt:
        cvd_usdt_key = f"cvd:{symbol_upper}:{deriv_period}:usdt"
        cvd_usdt_data = _get_cached_derivatives(cvd_usdt_key)
        if cvd_usdt_data is None:
            cvd_usdt_data = db.fetch_cvd_futures(symbol_upper, period=deriv_period, limit=500, margin_type='usdt') or []
            _set_cached_derivatives(cvd_usdt_key, cvd_usdt_data)

    if want_cvd_coin:
        cvd_coin_key = f"cvd:{symbol_upper}:{deriv_period}:coin"
        cvd_coin_data = _get_cached_derivatives(cvd_coin_key)
        if cvd_coin_data is None:
            cvd_coin_data = db.fetch_cvd_futures(symbol_upper, period=deriv_period, limit=500, margin_type='coin') or []
            _set_cached_derivatives(cvd_coin_key, cvd_coin_data)
    return oi_hist_data, cvd_usdt_data, cvd_coin_data


def _consensio_analysis(consensio, mode, is_crypto, frames):
    """Multi-timeframe Consensio analysis, or {} when the frames it needs are
    missing. calculate_multi_timeframe needs all three HTFs non-empty; a
    sparse dynamic symbol (daily history < 7 bars) only has '1D' (and maybe
    '3D'), and indexing the missing ones raised KeyError '3D'."""
    ltf_data = frames.ltf_data
    tf_data = frames.tf_data
    if mode == 'ltf' and is_crypto and ltf_data:
        return consensio.calculate_multi_timeframe(
            ltf_data['1h'], ltf_data['2h'], ltf_data['4h']
        )
    if (tf_data
            and not tf_data.get('1D', pd.DataFrame()).empty
            and not tf_data.get('3D', pd.DataFrame()).empty
            and not tf_data.get('1W', pd.DataFrame()).empty):
        return consensio.calculate_multi_timeframe(
            tf_data['1D'], tf_data['3D'], tf_data['1W']
        )
    return {}


def _consensio_table(analysis):
    """1D/3D/1W Consensio positions for the chart's table."""
    d1 = analysis.get('1D', {})
    d3 = analysis.get('3D', {})
    w1 = analysis.get('1W', {})
    consensio_data = {
        '1D': {'signal': d1.get('signal', 0), 'position': d1.get('positions', max(0, d1.get('signal', 0)))},
        '3D': {'signal': d3.get('signal', 0), 'position': d3.get('positions', max(0, d3.get('signal', 0)))},
        '1W': {'signal': w1.get('signal', 0), 'position': w1.get('positions', max(0, w1.get('signal', 0)))},
    }
    total = analysis.get('total', {})
    consensio_data['total'] = total.get('positions', consensio_data['1D']['position'] + consensio_data['3D']['position'] + consensio_data['1W']['position'])
    return consensio_data


def _monthly_cpr_levels(symbol_upper, daily_df):
    """Monthly CPR levels from the previous calendar month's daily bars, or
    None when that month has no bars. The caller checks there are >= 32 bars."""
    last_date = daily_df.index[-1]
    if not hasattr(last_date, 'month'):
        last_date = pd.Timestamp(last_date)
    cur_year, cur_month = last_date.year, last_date.month
    if cur_month == 1:
        prev_year, prev_month = cur_year - 1, 12
    else:
        prev_year, prev_month = cur_year, cur_month - 1

    prev_bars = daily_df[
        (daily_df.index.year == prev_year) &
        (daily_df.index.month == prev_month)
    ]
    if len(prev_bars) == 0:
        return None
    prev_high = float(prev_bars['high'].max())
    prev_low = float(prev_bars['low'].min())
    prev_close = float(prev_bars.iloc[-1]['close'])

    pivot = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2
    tc = pivot * 2 - bc
    r1 = pivot * 2 - prev_low
    s1 = pivot * 2 - prev_high
    r2 = pivot + (prev_high - prev_low)
    s2 = pivot - (prev_high - prev_low)
    # cumulative-range convention (matches CPRCalculator + frontend), not
    # classic floor-pivot R3 = 2P + H - 2L.
    r3 = r2 + (prev_high - prev_low)
    s3 = s2 - (prev_high - prev_low)

    # Current period start timestamp
    cur_bars = daily_df[
        (daily_df.index.year == cur_year) &
        (daily_df.index.month == cur_month)
    ]
    period_start = int(cur_bars.index[0].timestamp()) if len(cur_bars) > 0 else None

    def _r(v):
        return _cpr_round(v, prev_close)

    _supports = _cpr_supports_with_logging(
        symbol_upper, _r(s1), _r(s2), _r(s3),
        prev_high=prev_high, prev_low=prev_low,
        prev_close=prev_close, pivot=pivot,
    )
    return {
        'pivot': _r(pivot), 'tc': _r(tc), 'bc': _r(bc),
        'r1': _r(r1), 'r2': _r(r2), 'r3': _r(r3),
        's1': _supports['s1'],
        's2': _supports['s2'],
        's3': _supports['s3'],
        'prevHigh': _r(prev_high), 'prevLow': _r(prev_low),
        'prevClose': _r(prev_close),
        'periodStart': period_start
    }


def _weekly_and_daily_cpr(daily_df):
    """(weekly CPR, daily CPR), each with its history. Best-effort: None for
    whichever could not be computed."""
    weekly_cpr = None
    daily_cpr = None
    try:
        if not daily_df.empty:
            from consensio.indicators.cpr import CPRCalculator
            calc = CPRCalculator()
            weekly = calc.calculate_weekly_cpr(daily_df)
            if weekly:
                weekly['history'] = _calculate_weekly_cpr_history(daily_df, 52)
                weekly_cpr = weekly
            daily = calc.calculate_daily_cpr(daily_df)
            if daily:
                daily['history'] = _calculate_daily_cpr_history(daily_df, 60)
                daily_cpr = daily
    except Exception:
        pass
    return weekly_cpr, daily_cpr


def _build_lite_chart(req, db, consensio, frames, series, is_crypto, dynamic_info):
    """Lite payload (preloader): OHLCV, volume, RSI, Consensio and the basic
    CPR levels. No markers, patterns, divergences or heavy indicators."""
    tf_data = frames.tf_data
    indicators = {}
    # Consensio is needed for the TF badge scores in preloaded data.
    analysis = _consensio_analysis(consensio, req.mode, is_crypto, frames)
    if analysis:
        indicators['consensio'] = _consensio_table(analysis)

    # Basic monthly CPR (no history)
    cpr_data = None
    try:
        daily_df = None
        if tf_data and not tf_data.get('1D', pd.DataFrame()).empty:
            daily_df = tf_data['1D']
        if daily_df is not None and not daily_df.empty and len(daily_df) >= 32:
            cpr_data = _monthly_cpr_levels(req.symbol, daily_df)
    except Exception:
        pass

    weekly_cpr, daily_cpr = _weekly_and_daily_cpr(
        tf_data.get('1D', pd.DataFrame()) if tf_data else pd.DataFrame())

    has_live_ws, ws_venue, ws_exchange = _determine_ws_venue(db, req.symbol, is_crypto, req.is_dynamic)
    result = {
        'symbol': req.symbol,
        'timeframe': frames.timeframe,
        'ohlcv': series.ohlcv,
        'volume': series.volume,
        'rsi': series.rsi,
        'markers': [],
        'priceLines': [],
        'indicators': indicators,
        'cpr': cpr_data,
        'weekly_cpr': weekly_cpr,
        'daily_cpr': daily_cpr,
        '_lite': True,
        'tier': 'dynamic' if req.is_dynamic else 'curated',
        'live_ws': has_live_ws,
        'ws_venue': ws_venue,
        'ws_exchange': ws_exchange,
    }
    if req.is_dynamic and dynamic_info:
        result['asset_name'] = dynamic_info.get('name', req.symbol)
        result['asset_type'] = dynamic_info.get('type', 'stock')
    if series.cvd_spot:
        result['cvd_spot'] = series.cvd_spot
    return result


# Marker colours, matching the dashboard badge colours.
_SIGNAL_COLORS = {
    # Divergences
    'rsi_bullish': '#26a69a',
    'rsi_bearish': '#ef5350',
    'macd_bullish': '#26a69a',
    'macd_bearish': '#ef5350',
    'obv_bullish': '#26a69a',
    'obv_bearish': '#ef5350',
    # Alerts
    'ema_squeeze': '#f97316',      # Orange (SQZ)
    'v_cpr': '#a855f7',            # Purple (V-CPR)
    'near_s3': '#22c55e',          # Green (S3)
    'near_r3': '#ef4444',          # Red (R3)
    # RSI levels
    'rsi_oversold': '#22c55e',     # Green
    'rsi_overbought': '#ef4444',   # Red
}


def _signal_markers(analysis, ohlcv, df, rsi_calc):
    """Last-bar markers for Consensio divergences, the EMA squeeze and an
    RSI extreme."""
    markers = []
    if analysis:
        for div_type, abbr in [('rsi_divergence', 'RSI'), ('macd_divergence', 'MCD'), ('obv_divergence', 'OBV')]:
            div = analysis.get(div_type, {})
            if div and div.get('signal') and ohlcv:
                is_bullish = div['signal'] == 'bullish'
                color_key = div_type.split('_')[0] + ('_bullish' if is_bullish else '_bearish')
                markers.append({
                    'time': ohlcv[-1]['time'],
                    'position': 'belowBar' if is_bullish else 'aboveBar',
                    'color': _SIGNAL_COLORS.get(color_key, '#888888'),
                    'shape': 'arrowUp' if is_bullish else 'arrowDown',
                    'text': abbr
                })

        alerts = analysis.get('alerts', {})
        if alerts.get('ema_squeeze') and ohlcv:
            markers.append({
                'time': ohlcv[-1]['time'],
                'position': 'aboveBar',
                'color': _SIGNAL_COLORS['ema_squeeze'],
                'shape': 'circle',
                'text': 'SQZ'
            })

    if len(df) >= 14 and ohlcv:
        # calculate() may return an array or a single value
        latest_rsi = rsi_calc.calculate(df)
        if hasattr(latest_rsi, '__len__') and len(latest_rsi) > 0:
            latest_rsi = float(latest_rsi[-1])
        else:
            latest_rsi = float(latest_rsi) if latest_rsi is not None else None

        if latest_rsi is not None and not pd.isna(latest_rsi):
            if latest_rsi < 30:
                markers.append({
                    'time': ohlcv[-1]['time'],
                    'position': 'belowBar',
                    'color': _SIGNAL_COLORS['rsi_oversold'],
                    'shape': 'arrowUp',
                    'text': f'RSI:{latest_rsi:.0f}'
                })
            elif latest_rsi > 70:
                markers.append({
                    'time': ohlcv[-1]['time'],
                    'position': 'aboveBar',
                    'color': _SIGNAL_COLORS['rsi_overbought'],
                    'shape': 'arrowDown',
                    'text': f'RSI:{latest_rsi:.0f}'
                })
    return markers


def _cpr_price_lines(cpr_calc, tf_data, ohlcv):
    """Monthly CPR support/resistance price lines, plus last-bar markers for a
    V-CPR and for price within 2% of S3 or R3. Returns (price_lines, markers)."""
    price_lines = []
    markers = []
    if not (tf_data and not tf_data['1D'].empty):
        return price_lines, markers
    cpr_data = cpr_calc.calculate_monthly_cpr(tf_data['1D'])
    if not cpr_data:
        return price_lines, markers
    levels = [
        ('pivot', cpr_data.get('pivot'), '#888888', 'P'),
        ('s1', cpr_data.get('s1'), '#4caf50', 'S1'),
        ('s2', cpr_data.get('s2'), '#2e7d32', 'S2'),
        ('s3', cpr_data.get('s3'), '#1b5e20', 'S3'),
        ('r1', cpr_data.get('r1'), '#f44336', 'R1'),
        ('r2', cpr_data.get('r2'), '#c62828', 'R2'),
        ('r3', cpr_data.get('r3'), '#b71c1c', 'R3'),
    ]
    for key, value, color, label in levels:
        if value is not None:
            price_lines.append({
                'price': float(value),
                'color': color,
                'title': label,
                'lineWidth': 1,
                'lineStyle': 2
            })

    if ohlcv and cpr_data.get('is_v_cpr'):
        markers.append({
            'time': ohlcv[-1]['time'],
            'position': 'aboveBar',
            'color': _SIGNAL_COLORS['v_cpr'],
            'shape': 'circle',
            'text': 'V-CPR'
        })

    last_close = ohlcv[-1]['close'] if ohlcv else None
    if last_close:
        s3 = cpr_data.get('s3')
        r3 = cpr_data.get('r3')
        if s3 and abs(last_close - s3) / s3 < 0.02:
            markers.append({
                'time': ohlcv[-1]['time'],
                'position': 'belowBar',
                'color': _SIGNAL_COLORS['near_s3'],
                'shape': 'arrowUp',
                'text': 'S3'
            })
        if r3 and abs(last_close - r3) / r3 < 0.02:
            markers.append({
                'time': ohlcv[-1]['time'],
                'position': 'aboveBar',
                'color': _SIGNAL_COLORS['near_r3'],
                'shape': 'arrowDown',
                'text': 'R3'
            })
    return price_lines, markers


def _ema_ribbon(df, ts_list):
    """30/60 EMA ribbon coloured by trend (an ATR margin decides bull/bear/neutral)."""
    close = df['close']
    # SMA-seeded EMA (Pine-compatible).
    ema_fast = _ema_pine_compat(close, 30)
    ema_slow = _ema_pine_compat(close, 60)

    tr1 = df['high'] - df['low']
    tr2 = abs(df['high'] - close.shift(1))
    tr3 = abs(df['low'] - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=60).mean()
    margin = 0.30 * atr

    ema_diff = ema_fast - ema_slow
    mask_valid = ema_fast.notna() & ema_slow.notna()
    trend_arr = np.where(ema_diff > margin.fillna(0), 'bull',
                np.where(ema_diff < -margin.fillna(0), 'bear', 'neutral'))
    color_map = {'bull': '#7cb518', 'bear': '#fb6107', 'neutral': '#f3de2c'}

    fast_vals = ema_fast.astype(float).tolist()
    slow_vals = ema_slow.astype(float).tolist()
    valid_mask = mask_valid.tolist()

    ema_ribbon_fast = []
    ema_ribbon_slow = []
    ema_ribbon_fill = []
    for i in range(len(ts_list)):
        if valid_mask[i]:
            c = color_map[trend_arr[i]]
            ema_ribbon_fast.append({'time': ts_list[i], 'value': fast_vals[i], 'color': c})
            ema_ribbon_slow.append({'time': ts_list[i], 'value': slow_vals[i], 'color': c})
            ema_ribbon_fill.append({'time': ts_list[i], 'trend': trend_arr[i]})

    return {
        'fast': ema_ribbon_fast,
        'slow': ema_ribbon_slow,
        'fill': ema_ribbon_fill
    }


def _line_points(series):
    """[{'time', 'value'}] for the non-NaN points of a series."""
    s_notna = series.dropna()
    if len(s_notna) == 0:
        return []
    s_ts = s_notna.index.astype('datetime64[s]').astype(int).tolist()
    s_vals = s_notna.astype(float).tolist()
    return [{'time': s_ts[i], 'value': s_vals[i]} for i in range(len(s_ts))]


def _weekly_overlays(indicators, df_weekly):
    """Weekly 50/100/200 MAs (with 200+ weekly bars) and the Bull Market
    Support Band, 21W EMA + 20W SMA (with 21+), added to ``indicators``."""
    if len(df_weekly) >= 200:
        weekly_close = df_weekly['close']
        indicators['weeklyMAs'] = {
            'wma50': _line_points(weekly_close.rolling(window=50).mean()),
            'wma100': _line_points(weekly_close.rolling(window=100).mean()),
            'wma200': _line_points(weekly_close.rolling(window=200).mean()),
        }
    if len(df_weekly) >= 21:
        weekly_close_b = df_weekly['close']
        indicators['bullMarketBand'] = {
            'ema21w': _line_points(weekly_close_b.ewm(span=21).mean()),
            'sma20w': _line_points(weekly_close_b.rolling(window=20).mean()),
        }


def _ttm_squeeze_markers(df, ts_list):
    """TTM Squeeze (Bollinger Bands inside the Keltner Channel). Pine Script
    params: BB(30, 2.0), KC(30, 2.5); a bar is marked after 3+ consecutive
    squeeze bars."""
    sqz_len = 30
    bb_mult = 2.0
    kc_mult = 2.5

    sqz_close = df['close']
    sqz_basis = sqz_close.rolling(window=sqz_len).mean()
    sqz_std = sqz_close.rolling(window=sqz_len).std()
    sqz_upper_bb = sqz_basis + bb_mult * sqz_std
    sqz_lower_bb = sqz_basis - bb_mult * sqz_std

    sqz_tr = pd.concat([
        df['high'] - df['low'],
        abs(df['high'] - df['close'].shift(1)),
        abs(df['low'] - df['close'].shift(1))
    ], axis=1).max(axis=1)
    sqz_atr = sqz_tr.rolling(window=sqz_len).mean()
    sqz_upper_kc = sqz_basis + kc_mult * sqz_atr
    sqz_lower_kc = sqz_basis - kc_mult * sqz_atr

    squeeze_on = (sqz_lower_bb > sqz_lower_kc) & (sqz_upper_bb < sqz_upper_kc)

    squeeze_duration = []
    duration_count = 0
    for val in squeeze_on:
        if val:
            duration_count += 1
        else:
            duration_count = 0
        squeeze_duration.append(duration_count)

    sqz_mask = np.array(squeeze_duration) >= 3
    if not sqz_mask.any():
        return []
    return [{'time': t, 'squeeze': True} for t in np.array(ts_list)[sqz_mask].tolist()]


def _overlay_indicators(df, ts_list, tf_data, analysis, symbol):
    """Line-series overlays for the full chart: EMA ribbon, weekly MAs, Bull
    Market Band, TTM Squeeze, SMA 20/200 and the Consensio table."""
    indicators = {}
    if len(df) >= 60:
        indicators['ema_ribbon'] = _ema_ribbon(df, ts_list)
    if tf_data and '1W' in tf_data and not tf_data['1W'].empty:
        _weekly_overlays(indicators, tf_data['1W'])
    if len(df) >= 30:
        indicators['ttmSqueeze'] = _ttm_squeeze_markers(df, ts_list)
    if len(df) >= 220:
        try:
            indicators['smaLines'] = compute_sma_lines(df, [20, 200])
        except Exception as e:
            logger.warning(f"SMA line error for {symbol}: {e}")
    if analysis:
        indicators['consensio'] = _consensio_table(analysis)
    return indicators


def _full_monthly_cpr(db, symbol, symbol_upper, tf_data, daily_df):
    """Monthly CPR with five months of history, always from daily data.

    Returns (cpr_data, daily_df): the daily frame it resolved (fetched when
    tf_data has none, e.g. an LTF chart), or the ``daily_df`` passed in when
    resolving it failed. A failure is logged and leaves cpr_data None.
    """
    cpr_data = None
    try:
        if tf_data and not tf_data.get('1D', pd.DataFrame()).empty:
            daily_df = tf_data['1D']
        else:
            daily_tf = db.get_multi_timeframe_data(symbol_upper)
            daily_df = daily_tf.get('1D', pd.DataFrame()) if daily_tf else pd.DataFrame()

        if not daily_df.empty and len(daily_df) >= 32:
            cpr_data = _monthly_cpr_levels(symbol_upper, daily_df)
            if cpr_data is not None:
                cpr_history = _calculate_cpr_history(daily_df, 48)
                if cpr_history:
                    cpr_data['history'] = cpr_history
    except Exception as cpr_err:
        logger.warning(f"CPR calculation error for {symbol}: {cpr_err}")
    return cpr_data, daily_df


def _build_full_chart(symbol, req, db, consensio, rsi_calc, cpr_calc, frames, df, series,
                      is_crypto, dynamic_info, derivatives):
    """Full payload: the lite fields plus markers, CPR price lines, overlay
    indicators, CPR history, requested futures series, the quant overlay and
    market sessions. ``df`` is the trimmed frame being charted."""
    tf_data = frames.tf_data
    analysis = _consensio_analysis(consensio, req.mode, is_crypto, frames)

    markers = _signal_markers(analysis, series.ohlcv, df, rsi_calc)
    if tf_data is None:
        tf_data = db.get_multi_timeframe_data(req.symbol)
    price_lines, cpr_markers = _cpr_price_lines(cpr_calc, tf_data, series.ohlcv)
    markers.extend(cpr_markers)
    # lightweight-charts requires markers sorted by time
    markers.sort(key=lambda m: m['time'])
    if markers:
        logger.debug(f"Chart markers for {req.symbol}: {[m['text'] for m in markers]}")

    indicators = _overlay_indicators(df, series.ts_list, tf_data, analysis, symbol)
    cpr_data, daily_df = _full_monthly_cpr(db, symbol, req.symbol, tf_data, frames.daily_df)
    weekly_cpr, daily_cpr = _weekly_and_daily_cpr(daily_df)

    has_live_ws, ws_venue, ws_exchange = _determine_ws_venue(db, req.symbol, is_crypto, req.is_dynamic)
    result = {
        'symbol': req.symbol,
        'timeframe': frames.timeframe,
        'ohlcv': series.ohlcv,
        'volume': series.volume,
        'rsi': series.rsi,
        'markers': markers,
        'priceLines': price_lines,
        'indicators': indicators,
        'cpr': cpr_data,
        'weekly_cpr': weekly_cpr,
        'daily_cpr': daily_cpr,
        'tier': 'dynamic' if req.is_dynamic else 'curated',
        'live_ws': has_live_ws,
        'ws_venue': ws_venue,
    }
    if req.is_dynamic and dynamic_info:
        result['asset_name'] = dynamic_info.get('name', req.symbol)
        result['asset_type'] = dynamic_info.get('type', 'stock')
    oi_hist_data, cvd_usdt_data, cvd_coin_data = derivatives
    if series.cvd_spot:
        result['cvd_spot'] = series.cvd_spot
    if oi_hist_data:
        result['oi_hist'] = oi_hist_data
    if cvd_usdt_data:
        result['cvd_usdt'] = cvd_usdt_data
    if cvd_coin_data:
        result['cvd_coin'] = cvd_coin_data

    # Quant data overlay (macro, sentiment, regime, cross-asset prices)
    if req.quant_param:
        try:
            from quant.bridge import get_aligned_series
            quant_series = [s.strip() for s in req.quant_param.split(',') if s.strip()]
            quant_data = get_aligned_series(quant_series, series.ts_list)
            if quant_data:
                result['quant'] = quant_data
        except Exception as e:
            logger.warning(f"[chart] Quant bridge failed for {req.symbol}: {e}")

    # Market sessions (only for timeframes <= 4H), projected 50 bars ahead
    tf_for_sessions = frames.tf_normalized or frames.timeframe
    session_data = compute_session_data(series.ts_list, tf_for_sessions)
    if session_data:
        result['sessions'] = session_data
        future_sessions = compute_future_sessions(series.ts_list[-1], tf_for_sessions, count=50)
        if future_sessions:
            result['futureSessions'] = future_sessions
    return result


def _chart_response(result, req, df, t0, cacheable):
    """Columnar conversion, cache write, MACD overlay, the JSON response with
    its browser-cache headers, and the timing log line."""
    if req.columnar:
        _to_columnar(result)
    # The cached copy never carries macd (it stays byte-identical for
    # non-macd consumers). _cache_suffix already carries ':q<series>' when
    # quant was requested, so a quant payload never lands under the plain key.
    if cacheable:
        _set_cached_chart(req.symbol, req.tf_for_cache + req.cache_suffix, req.lite, result)
    # Optional MACD overlay — same dict shape /api/chart-internal ships.
    # Attached after columnar/cache so the macd sub-shape is identical
    # regardless of ?fmt=col and never leaks into the cache.
    if req.want_macd:
        result['macd'] = compute_macd_series(df, symbol=req.symbol)
    result['_server_ts'] = int(_time.time())
    from carrac.formatters import maybe_compact, compact_chart
    result = maybe_compact(result, compact_chart)
    resp = jsonify(result)
    _browser_ttl = _chart_browser_ttl(req.tf_for_cache)
    resp.headers['Cache-Control'] = f'private, max-age={_browser_ttl}'
    resp.headers.pop('Pragma', None)
    resp.headers.pop('Expires', None)
    # Background rebuilds (SWR revalidate + worker warm pass — the tier-bypass
    # flag is set exactly and only on those replay paths) log as `refresh`,
    # not `miss`: nobody waited on them, and counting them as misses made the
    # cache look 28% cold when real blocking misses were 0.9%.
    _verdict = _cold_build_verdict()
    if _verdict == 'miss':
        _record_chart_miss(req.symbol, req.tf_for_cache)
    logger.info("chart %s tf=%s %s %dms", req.symbol, req.tf_for_cache,
                _verdict, int((_time.perf_counter() - t0) * 1000))
    return resp


@charts_bp.route('/api/chart/<symbol>')
@limiter.limit("120 per minute", exempt_when=_is_asgi_replay_active)
@auth_or_key_required
def get_chart_data(symbol: str):
    """
    Get OHLCV chart data for a symbol (used for chart preview).

    Args:
        symbol: Asset symbol (e.g., 'BTC', 'TSLA')

    Query params:
        days: Number of days/bars of data (default 90, max 365)
        mode: 'htf' (1D candles) or 'ltf' (4H candles for crypto)
        lite: '1' to skip derivatives (OI/CVD futures) for background preloading
        macd: '1'/'true' to include a top-level `macd` series
              (line/signal/histogram) — same shape /api/chart-internal ships.
              Absent → response unchanged. macd requests bypass the chart cache.

    Returns:
        JSON with OHLCV, volume, RSI data and pattern markers for lightweight-charts
    """
    db = _get_db()
    consensio = _get_consensio()
    rsi_calc = _get_rsi_calc()
    cpr_calc = _get_cpr_calc()
    _fire_first_chart_view(symbol)

    try:
        t0 = _time.perf_counter()
        req = _parse_chart_request(db, symbol)

        cached = _serve_cached_chart(req, t0)
        if cached is not None:
            return cached

        # Ratio symbols (e.g. BTC_SPX)
        if '_' in req.symbol:
            parts = req.symbol.split('_', 1)
            if len(parts) == 2:
                return _handle_ratio_chart(parts[0], parts[1], req.requested_tf or '1D', request.args.get('days', 90, type=int), include_macd=req.want_macd)

        # Validation runs only on a cache miss: for a dynamic symbol it can
        # mean a live upstream call, and a warm cached chart must never gate
        # on one (running it first would 404 charts sitting warm in cache
        # during the post-restart rate-limit window). An unknown symbol has no cache
        # entry, so it always reaches this check. validate_symbol caches
        # positives in Redis for 24h.
        dynamic_info = None
        if req.is_dynamic:
            dynamic_info = db.validate_symbol(req.symbol)
            if not dynamic_info:
                return jsonify({'error': f'Unknown symbol: {symbol}', 'symbol': req.symbol}), 404

        is_crypto = db._is_crypto(req.symbol) or (dynamic_info and dynamic_info.get('type') == 'crypto')

        frames = _load_chart_frames(db, req, is_crypto)
        if frames.df.empty:
            return jsonify({
                'error': f'No data found for {symbol}',
                'symbol': req.symbol
            }), 404

        df = frames.df.tail(frames.days)
        # Drop rows with NaN OHLC values (prevents invalid JSON with NaN literals)
        df = df.dropna(subset=['open', 'high', 'low', 'close'])
        # Intraday: ensure the current forming candle is present so the client's
        # candle-close check isn't permanently tripped → no 30s boundary-retry loop.
        # No-op for daily/HTF (they already carry the live bar via _inject_live_candle).
        df = _ensure_forming_candle(df, frames.tf_normalized or frames.timeframe)

        ts_list, ohlcv, volume = _ohlcv_series(df)
        cvd_spot = _spot_cvd_series(df)
        derivatives = _fetch_chart_derivatives(db, req, is_crypto, frames)
        series = _ChartSeries(ts_list=ts_list, ohlcv=ohlcv, volume=volume, cvd_spot=cvd_spot,
                              rsi=_rsi_with_ma_series(df, rsi_calc))

        if req.lite:
            result = _build_lite_chart(req, db, consensio, frames, series, is_crypto, dynamic_info)
            return _chart_response(result, req, df, t0, cacheable=not req.want_macd)

        result = _build_full_chart(symbol, req, db, consensio, rsi_calc, cpr_calc, frames, df,
                                   series, is_crypto, dynamic_info, derivatives)
        # Derivatives payloads are never cached either: they carry the
        # requester's indicator state.
        return _chart_response(result, req, df, t0,
                               cacheable=not req.derivatives_param and not req.want_macd)

    except Exception as e:
        logger.error(f"Chart data error for {symbol}: {e}", exc_info=True)
        return jsonify({
            'error': 'Failed to load chart data',
            'symbol': symbol.upper()
        }), 500


@charts_bp.route('/api/backtest/quant-context')
@login_required
def api_backtest_quant_context():
    """Cross-validate backtest entry conditions against the quant conditional engine.

    Maps formula quant conditions to z-score conditions and queries historical
    forward return distributions.

    Query params:
        conditions: Comma-separated quant conditions (e.g., 'FEAR_GREED:lt:-1.5,REGIME:eq:1')
        target: Asset for forward returns (default: current chart symbol or BTC)
        horizons: Comma-separated horizons in days (default: '7,30,90')
    """
    conditions_param = request.args.get('conditions', '')
    target = request.args.get('target', 'BTC').upper()
    horizons_param = request.args.get('horizons', '7,30,90')

    if not conditions_param:
        return jsonify({'error': 'conditions parameter required'}), 400

    conditions = [c.strip() for c in conditions_param.split(',') if c.strip()]
    try:
        horizons = [int(h.strip()) for h in horizons_param.split(',') if h.strip()]
    except ValueError:
        return jsonify({'error': 'Invalid horizons parameter'}), 400

    try:
        from quant.engine.conditional import query_conditional
        results = {}
        for h in horizons:
            r = query_conditional(conditions, target=target, horizon=h)
            if r and not r.get('error'):
                results[f'{h}d'] = {
                    'instances': r.get('instances', 0),
                    'median_return': r.get('forward_return', {}).get('median'),
                    'p25': r.get('forward_return', {}).get('p25'),
                    'p75': r.get('forward_return', {}).get('p75'),
                    'current_match': r.get('current_match', False),
                }
        return jsonify({
            'conditions': conditions,
            'target': target,
            'horizons': results,
        })
    except Exception as e:
        logger.error(f"[backtest/quant-context] Error: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@charts_bp.route('/api/quant/chart-series')
@login_required
def api_quant_chart_series():
    """Fetch quant series aligned to a chart's time axis.

    Used by the formula parser to lazy-load macro/quant data when a formula
    references variables like M2, FEAR_GREED, REGIME, etc.

    Query params:
        symbol: Asset symbol (for cache key context)
        tf: Timeframe (e.g., '1D', '4h')
        series: Comma-separated series names (e.g., 'M2,FEAR_GREED,REGIME')
        timestamps: JSON array of Unix epoch seconds (chart bar timestamps)
    """
    # Custom indicators/formulas (the only caller — see
    # static/js/custom-indicators.js) are gated on the frontend behind
    # window._hasPremiumFeatures = ['premium','admin','tester'].includes(tier),
    # i.e. exactly TESTER_OR_HIGHER — mirror that bar server-side so a non-
    # premium caller can't hit this route directly for the same macro/quant
    # series /api/chart's ?quant= param already gates. Same replay bypass as
    # that fix (this endpoint doesn't currently run under the SWR
    # background-refresh replay, but respecting it keeps the pattern
    # consistent and safe if that ever changes).
    if not _is_tier_bypass_active():
        from carrac.auth.decorators import get_request_tier
        from carrac.tiers import TESTER_OR_HIGHER
        if get_request_tier() not in TESTER_OR_HIGHER:
            return jsonify({'error': 'Premium access required', 'upgrade_required': True}), 403

    series_param = request.args.get('series', '')
    timestamps_json = request.args.get('timestamps', '')

    if not series_param or not timestamps_json:
        return jsonify({'error': 'series and timestamps parameters required'}), 400

    import json
    try:
        timestamps = json.loads(timestamps_json)
        if not isinstance(timestamps, list) or not timestamps:
            return jsonify({'error': 'timestamps must be a non-empty JSON array'}), 400
    except (json.JSONDecodeError, TypeError):
        return jsonify({'error': 'Invalid timestamps JSON'}), 400

    series_list = [s.strip() for s in series_param.split(',') if s.strip()]
    if len(series_list) > 5:
        return jsonify({'error': 'Maximum 5 series per request'}), 400

    try:
        from quant.bridge import get_aligned_series
        data = get_aligned_series(series_list, timestamps)
        return jsonify({'series': data})
    except Exception as e:
        logger.error(f"[quant/chart-series] Error: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@charts_bp.route('/api/weekly-signal')
@login_required
def api_weekly_signal():
    """Weekly cycle signal using on-chain metrics (MVRV, Mayer, Puell). BTC only."""
    try:
        symbol = request.args.get('symbol', 'BTC')
        if symbol != 'BTC':
            return jsonify({
                'status': 'ok', 'signal': 'N/A', 'confidence': 'none',
                'reason': 'Weekly signal only available for BTC', 'symbol': symbol
            })
        result = get_services().weekly_signal_engine.calculate(symbol)
        return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat(), 'symbol': symbol, **result})
    except Exception as e:
        logger.error(f"Error in weekly signal endpoint: {e}")
        return jsonify({'status': 'error', 'signal': 'ERROR', 'error': 'Failed to compute weekly signal'}), 500


# ------------------------------------------------------------------
# Relative Strength (Mansfield RS with RRG quadrants)
# ------------------------------------------------------------------

# Benchmark mapping: asset → benchmark symbol
_RS_BENCHMARKS = {
    # Crypto vs BTC
    'ETH': 'BTC', 'XRP': 'BTC', 'TOTAL': 'BTC',
    # Equities vs SPX
    'QQQ': 'SPX', 'MAGS': 'SPX',
    # Commodities vs GOLD
    'SILVER': 'GOLD', 'OIL': 'GOLD', 'COPPER': 'GOLD',
    'NATGAS': 'GOLD', 'XLE': 'GOLD',
    'CORN': 'GOLD', 'WHEAT': 'GOLD', 'SOYBEANS': 'GOLD',
    # Benchmarks themselves — absolute RS percentile (vs self 4wk ago)
    'BTC': None, 'SPX': None, 'GOLD': None, 'DXY': None,
}

# RS computation period (weekly bars)
_RS_PERIOD = 52
# Momentum lookback (4 weeks of daily bars ≈ 20 trading days)
_RS_MOMENTUM_LOOKBACK = 20


def _compute_rs_rankings():
    """Compute relative strength for all tracked assets. Returns list of dicts."""
    db = _get_db()
    rankings = []

    for symbol, benchmark in _RS_BENCHMARKS.items():
        try:
            asset_data = db.get_multi_timeframe_data(symbol)
            if not asset_data or asset_data.get('1D') is None or asset_data['1D'].empty:
                continue
            asset_daily = asset_data['1D']

            if benchmark:
                bench_data = db.get_multi_timeframe_data(benchmark)
                if not bench_data or bench_data.get('1D') is None or bench_data['1D'].empty:
                    continue
                bench_daily = bench_data['1D']

                # Align on common dates
                common = asset_daily.index.intersection(bench_daily.index)
                if len(common) < _RS_PERIOD:
                    continue
                asset_close = asset_daily.loc[common, 'close']
                bench_close = bench_daily.loc[common, 'close']

                rs_result = calculate_mansfield_rs(asset_close, bench_close, period=_RS_PERIOD)
                if not rs_result:
                    continue

                rs_score = rs_result['rs_normalized']

                # Compute RS momentum: current score minus score from 4 weeks ago
                if len(common) >= _RS_PERIOD + _RS_MOMENTUM_LOOKBACK:
                    older_asset = asset_close.iloc[:-_RS_MOMENTUM_LOOKBACK]
                    older_bench = bench_close.iloc[:-_RS_MOMENTUM_LOOKBACK]
                    older_rs = calculate_mansfield_rs(older_asset, older_bench, period=_RS_PERIOD)
                    rs_momentum = round(rs_score - (older_rs['rs_normalized'] if older_rs else 50), 1)
                else:
                    rs_momentum = 0.0
            else:
                # Benchmark asset — use absolute returns as RS proxy
                close = asset_daily['close']
                if len(close) < _RS_PERIOD:
                    continue
                recent = close.iloc[-_RS_PERIOD:]
                period_min = recent.min()
                period_max = recent.max()
                current = close.iloc[-1]
                rs_score = round(((current - period_min) / (period_max - period_min) * 100)
                                 if period_max > period_min else 50.0, 1)
                rs_result = {'pct_vs_benchmark': 0.0, 'interpretation': 'benchmark'}

                # Momentum: percentile now vs percentile 4 weeks ago
                if len(close) >= _RS_PERIOD + _RS_MOMENTUM_LOOKBACK:
                    older = close.iloc[-_RS_PERIOD - _RS_MOMENTUM_LOOKBACK:-_RS_MOMENTUM_LOOKBACK]
                    older_min = older.min()
                    older_max = older.max()
                    older_current = close.iloc[-1 - _RS_MOMENTUM_LOOKBACK]
                    older_score = ((older_current - older_min) / (older_max - older_min) * 100) \
                        if older_max > older_min else 50.0
                    rs_momentum = round(rs_score - older_score, 1)
                else:
                    rs_momentum = 0.0
                benchmark = symbol  # Show self as benchmark

            # RRG Quadrant
            if rs_score >= 50 and rs_momentum >= 0:
                quadrant = 'leading'
            elif rs_score >= 50 and rs_momentum < 0:
                quadrant = 'weakening'
            elif rs_score < 50 and rs_momentum < 0:
                quadrant = 'lagging'
            else:
                quadrant = 'improving'

            # Returns
            close_series = asset_daily['close']
            ret_7d = round((close_series.iloc[-1] / close_series.iloc[-min(7, len(close_series))] - 1) * 100, 2) \
                if len(close_series) >= 2 else 0.0
            ret_30d = round((close_series.iloc[-1] / close_series.iloc[-min(30, len(close_series))] - 1) * 100, 2) \
                if len(close_series) >= 2 else 0.0

            # RSI 14 — canonical Wilder, NOT a local SMA reimplementation.
            #
            # A hand-rolled `rolling(14).mean()` is a simple moving average,
            # a different indicator from the Wilder RSI the charts and facts
            # block render (consensio/indicators/rsi.py, "matches TradingView
            # exactly"). It is published as `rsi_14` in the RS-rankings
            # payload, which /api/relative-strength serves straight into the
            # shared reports — so one asset could carry two different RSIs on
            # one page depending on which section the reader looked at, enough
            # to sit on opposite sides of the 30 line.
            rsi_14 = None
            if len(close_series) >= 15:
                try:
                    from consensio.indicators.rsi import RSICalculator
                    rsi_14 = RSICalculator(14).calculate(
                        pd.DataFrame({'close': close_series})
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f'RS rankings RSI unavailable for {symbol}: {e}')
                    rsi_14 = None

            rankings.append({
                'symbol': symbol,
                'benchmark': benchmark,
                'rs_score': rs_score,
                'rs_momentum': rs_momentum,
                'quadrant': quadrant,
                'interpretation': rs_result.get('interpretation', 'neutral'),
                'ret_7d': ret_7d,
                'ret_30d': ret_30d,
                'rsi_14': rsi_14,
                'pct_vs_benchmark': rs_result.get('pct_vs_benchmark', 0.0),
            })

        except Exception as e:
            logger.debug(f"RS calc failed for {symbol}: {e}")
            continue

    # Sort by rs_score descending
    rankings.sort(key=lambda x: x['rs_score'], reverse=True)
    return rankings

