"""
Assets routes: /api/assets, /api/watchlist, /api/providers/usage.
"""

import logging
from datetime import datetime

from flask import Blueprint, g, jsonify, request, session, current_app

from carrac.auth.decorators import login_required, verify_api_key, auth_or_key_required
from user_db import user_db

logger = logging.getLogger('carrac.assets')

assets_bp = Blueprint('assets', __name__)


@assets_bp.before_request
def _enforce_assets_csrf():
    """Enforce CSRF for session-authenticated mutating requests.

    API-key callers are exempt (they cannot be targeted by CSRF attacks).
    Session callers on mutation methods (POST/PUT/DELETE) must pass a valid
    CSRF token.
    """
    if not current_app.config.get('WTF_CSRF_ENABLED', True):
        return None
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    api_key = request.headers.get('X-Internal-Key', '') or request.headers.get('X-API-Key', '')
    if api_key and verify_api_key(api_key):
        return None
    if session.get('user_id'):
        from flask_wtf.csrf import validate_csrf
        try:
            csrf_token = request.headers.get('X-CSRFToken') or request.form.get('csrf_token')
            validate_csrf(csrf_token)
        except Exception:
            return jsonify({'error': 'CSRF token missing or invalid'}), 403
    return None


# ==================== /api/assets — precompute consumer ====================

@assets_bp.route('/api/assets')
@login_required
def get_assets():
    """Serve the pre-computed multi-asset Consensio snapshot.

    HTF mode is warmed by the precompute loop every ~60s; LTF is computed
    on-demand per-asset. On a cache miss this handler computes the full
    response inline (slow: ~60s). Clients receive an HTTP 202 with
    ``retry_after`` while another worker is computing.
    """
    from carrac.precompute import (
        response_cache,
        _compute_assets_response,
    )

    try:
        timeframe_mode = request.args.get('mode', 'htf')
        if timeframe_mode not in ('htf', 'ltf'):
            timeframe_mode = 'htf'

        cached_data = response_cache.get(timeframe_mode)
        if cached_data:
            cache_age = cached_data.get('_cache_age', 0)
            logger.debug(f"Cache hit for {timeframe_mode}: age={cache_age}s")
            if cache_age < 60:
                cached_data['fetch_stats'] = f"⚡ Instant! Data from {cache_age}s ago"
            else:
                cached_data['fetch_stats'] = f"⚡ Fast! Data from {cache_age // 60}m ago"
            return jsonify(cached_data)

        if response_cache.is_computing(timeframe_mode):
            return jsonify({
                'status': 'computing',
                'message': 'Data is being computed, please wait...',
                'retry_after': 5
            }), 202

        logger.info(f"Cache miss for {timeframe_mode} — computing on-demand")
        response_cache.set_computing(timeframe_mode, True)

        try:
            start_time = datetime.now()
            data = _compute_assets_response(timeframe_mode)
            elapsed = (datetime.now() - start_time).total_seconds()

            response_cache.set(timeframe_mode, data)
            response_cache.set_computing(timeframe_mode, False)

            data['fetch_stats'] = f"The wizard gathered data in {elapsed:.1f}s (first load)"
            logger.info(
                f"On-demand compute {timeframe_mode}: "
                f"{len(data.get('assets', []))} assets in {elapsed:.1f}s"
            )
            return jsonify(data)

        except Exception as e:
            response_cache.set_computing(timeframe_mode, False)
            raise e

    except Exception as e:
        logger.error(f"Error in get_assets: {e}")
        return jsonify({'error': 'Failed to load asset data'}), 500


# ==================== Watchlist Endpoints ====================

@assets_bp.route('/api/watchlist', methods=['GET'])
@auth_or_key_required
def get_user_watchlist():
    """Get current user's watchlist as a flat ordered array.

    Accepts a session login OR a per-user API key (``X-API-Key`` /
    ``X-Internal-Key``) so headless clients (e.g. the Carrac TUI) can sync the
    caller's OWN watchlist. Resolves the key's user via ``g.api_key_user_id``
    first, falling back to the session.
    """
    user_id = getattr(g, 'api_key_user_id', None)
    if user_id is None:
        user_id = session.get('user_id')
    if user_id is None:
        return jsonify({'error': 'authentication required'}), 401
    fmt = request.args.get('format', 'flat')
    if fmt == 'raw':
        # Legacy: return raw stored format
        return jsonify(user_db.get_watchlist(user_id))
    if fmt == 'groups':
        return jsonify({'groups': user_db.get_watchlist_groups(user_id)})
    # Default: flat ordered array
    symbols = user_db.get_flat_watchlist(user_id)
    return jsonify(symbols)


# POST /api/watchlist, /api/watchlist/add, /api/watchlist/remove,
# /api/watchlist/reorder — deleted; all four are in MIGRATED_WRITE_PATHS
# (asgi.py) and served by FastAPI (carrac/fastapi_app/routers/watchlist.py).

# ==================== User Preferences ====================

@assets_bp.route('/api/user/me', methods=['GET', 'POST'])
@login_required
def user_me():
    """Get or update current user info including onboarding status."""
    user_id = session['user_id']
    if request.method == 'POST':
        data = request.get_json()
        if data:
            prefs = user_db.get_preferences(user_id)
            prefs.update(data)
            user_db.save_preferences(user_id, prefs)
            return jsonify({'success': True})
        return jsonify({'error': 'No data'}), 400
    prefs = user_db.get_preferences(user_id)
    return jsonify({
        'user_id': user_id,
        'onboarding_completed': prefs.get('onboarding_completed', False),
        'onboarding_version': prefs.get('onboarding_version', 1),
        'profile': prefs.get('profile', None),
        'profile_completeness': user_db.profile_completeness(user_id),
    })


# ==================== Cache & Usage ====================

@assets_bp.route('/api/providers/usage')
@login_required
def api_providers_usage():
    """Upstream data-source rate-limit stats (Binance, crypto market data, etc.).

    Not /api/usage — that path serves per-user
    API consumption (see carrac/usage_routes.py).
    """
    try:
        from api_rate_limiter import get_usage_stats
        stats = get_usage_stats()
        return jsonify({'status': 'ok', 'providers': stats})
    except Exception as e:
        logger.error(f"Error getting API usage stats: {e}")
        return jsonify({'status': 'error', 'error': 'Failed to fetch usage stats'}), 500
