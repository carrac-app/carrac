"""
Drawing Management API

Flask blueprint for reading user chart drawings. Writes are served by
carrac/fastapi_app/routers/drawings.py, which reuses ``validate_drawing_data``
and the limits defined here.
"""

import logging
from flask import Blueprint, g, request, session
from functools import wraps

from carrac.content_db import get_content_db
from carrac.json_response import jsonify_iso
from security import sanitize_for_llm
from carrac.auth.decorators import verify_api_key

logger = logging.getLogger('carrac.drawings')


def _get_asset_price(asset):
    """Get current price from response_cache. Returns float or None."""
    try:
        from carrac.precompute import response_cache
        asset_upper = asset.upper()
        for cache_key in [f'{asset_upper}_latest', f'{asset_upper}_price']:
            if cache_key in response_cache:
                return response_cache[cache_key]
        prices = response_cache.get('combined_crypto_prices') or {}
        p = prices.get(asset_upper, {})
        if isinstance(p, dict):
            return p.get('price')
        return None
    except Exception:
        return None


def _price_dist(current_price, level):
    """Return '(price $X is Y% above/below)' string, or '' if unavailable."""
    if not current_price or not level or level == 0:
        return ''
    pct = (current_price - level) / level * 100
    ab = 'above' if pct > 0 else 'below'
    return f" [price ${current_price:,.2f} is {abs(pct):.1f}% {ab}]"


def _enrich_path(current_price, points):
    """Annotate a scenario path with which waypoint price is near/past."""
    if not current_price or not points:
        return ''
    values = [p.get('value', 0) for p in points if p.get('value')]
    if not values:
        return ''
    # Find the closest waypoint and track progress
    closest_idx = 0
    closest_dist = abs(current_price - values[0])
    passed = []
    for i, v in enumerate(values):
        d = abs(current_price - v)
        if d < closest_dist:
            closest_dist = d
            closest_idx = i
        pct = (current_price - v) / v * 100 if v else 0
        if abs(pct) < 1.5:
            passed.append(f"AT waypoint {i+1} ({v:,.0f})")
        elif (i > 0 and values[i-1] < v and current_price > v) or \
             (i > 0 and values[i-1] > v and current_price < v):
            passed.append(f"PAST waypoint {i+1} ({v:,.0f})")
    pct_nearest = (current_price - values[closest_idx]) / values[closest_idx] * 100 if values[closest_idx] else 0
    ab = 'above' if pct_nearest > 0 else 'below'
    result = f" [nearest waypoint {closest_idx+1}/{len(values)}: ${values[closest_idx]:,.0f}, price is {abs(pct_nearest):.1f}% {ab}]"
    if passed:
        result += f" [{'; '.join(passed)}]"
    return result


def _describe_trendline(d, asset, watched=False):
    """Generate rich description for a trendline including slope, current line value, and distance."""
    meta = d.get('metadata') or {}
    points = meta.get('points', [])
    if len(points) < 2:
        return None
    t1 = points[0].get('timestamp')
    v1 = points[0].get('value')
    t2 = points[1].get('timestamp')
    v2 = points[1].get('value')
    if not all([t1, v1, t2, v2]) or t1 == t2:
        return None
    try:
        from datetime import datetime as dt
        slope_per_ms = (v2 - v1) / (t2 - t1)
        now_ms = dt.now().timestamp() * 1000
        current_line_val = v1 + slope_per_ms * (now_ms - t1)
        if current_line_val <= 0:
            return None
        direction = 'ascending' if v2 > v1 else 'descending'
        d1_str = dt.fromtimestamp(t1 / 1000).strftime('%b %d')
        d2_str = dt.fromtimestamp(t2 / 1000).strftime('%b %d')
        current_price = _get_asset_price(asset)
        dist_str = ''
        if current_price and current_price > 0:
            pct = (current_price - current_line_val) / current_line_val * 100
            above_below = 'above' if pct > 0 else 'below'
            dist_str = f" Price (${current_price:,.2f}) is {abs(pct):.1f}% {above_below}."
        prefix = 'WATCHED ' if watched else ''
        return (
            f"{prefix}{direction} trendline from ${v1:,.2f} ({d1_str}) to ${v2:,.2f} ({d2_str}). "
            f"Current line value: ${current_line_val:,.2f}.{dist_str}"
        )
    except Exception as e:
        logger.debug(f"Error describing trendline: {e}")
        return None


def _describe_parallel_channel(d, asset):
    """Generate rich description for a parallel channel including slope, projected lines, and price position."""
    meta = d.get('metadata') or {}
    points = meta.get('points', [])
    if len(points) < 3:
        return None
    t1 = points[0].get('timestamp')
    v1 = points[0].get('value')
    t2 = points[1].get('timestamp')
    v2 = points[1].get('value')
    t3 = points[2].get('timestamp')
    v3 = points[2].get('value')
    if not all([t1, v1, t2, v2, t3, v3]) or t1 == t2:
        return None
    try:
        from datetime import datetime as dt
        slope_per_ms = (v2 - v1) / (t2 - t1)
        now_ms = dt.now().timestamp() * 1000
        # Line 1 projected to now
        line1_now = v1 + slope_per_ms * (now_ms - t1)
        # Line 2 is parallel through point 3
        line2_now = v3 + slope_per_ms * (now_ms - t3)
        if line1_now <= 0 or line2_now <= 0:
            return None
        upper = max(line1_now, line2_now)
        lower = min(line1_now, line2_now)
        mid = (upper + lower) / 2
        width_pct = (upper - lower) / mid * 100 if mid else 0
        direction = 'ascending' if slope_per_ms > 0 else ('descending' if slope_per_ms < 0 else 'horizontal')
        current_price = _get_asset_price(asset)
        pos_str = ''
        if current_price and current_price > 0:
            if current_price > upper:
                pct = (current_price - upper) / upper * 100
                pos_str = f" Price (${current_price:,.2f}) is {pct:.1f}% above channel."
            elif current_price < lower:
                pct = (lower - current_price) / lower * 100
                pos_str = f" Price (${current_price:,.2f}) is {pct:.1f}% below channel."
            else:
                chan_pos = (current_price - lower) / (upper - lower) * 100 if upper != lower else 50
                pos_str = f" Price (${current_price:,.2f}) at {chan_pos:.0f}% of channel."
        return (
            f"{direction} parallel channel: upper ${upper:,.2f}, mid ${mid:,.2f}, lower ${lower:,.2f} "
            f"(width {width_pct:.1f}%).{pos_str}"
        )
    except Exception as e:
        logger.debug(f"Error describing parallel channel: {e}")
        return None


def _describe_fibonacci(d, asset):
    """Generate rich description for a fibonacci retracement with all ratio levels and price position."""
    meta = d.get('metadata') or {}
    points = meta.get('points', [])
    if len(points) < 2:
        return None
    v1 = points[0].get('value')
    v2 = points[1].get('value')
    if not v1 or not v2 or v1 == v2:
        return None
    try:
        high = max(v1, v2)
        low = min(v1, v2)
        span = high - low
        ratios = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
        # Levels from high to low (retracement measures pullback from the move)
        levels = {r: high - span * r for r in ratios}
        current_price = _get_asset_price(asset)
        level_strs = [f"{r*100:.1f}%=${levels[r]:,.2f}" for r in ratios]
        pos_str = ''
        if current_price and current_price > 0:
            # Find which two levels price is between
            sorted_levels = sorted(levels.items(), key=lambda x: x[1], reverse=True)
            above_level = None
            below_level = None
            for ratio, lv in sorted_levels:
                if lv >= current_price:
                    below_level = (ratio, lv)
                if lv <= current_price and above_level is None:
                    above_level = (ratio, lv)
            if above_level and below_level and above_level != below_level:
                pos_str = f" Price (${current_price:,.2f}) between {above_level[0]*100:.1f}% (${above_level[1]:,.2f}) and {below_level[0]*100:.1f}% (${below_level[1]:,.2f})."
            elif current_price > high:
                pct = (current_price - high) / high * 100
                pos_str = f" Price (${current_price:,.2f}) is {pct:.1f}% above 0% level."
            elif current_price < low:
                pct = (low - current_price) / low * 100
                pos_str = f" Price (${current_price:,.2f}) is {pct:.1f}% below 100% level."
        return f"Fib retracement (${high:,.2f}-${low:,.2f}): {', '.join(level_strs)}.{pos_str}"
    except Exception as e:
        logger.debug(f"Error describing fibonacci: {e}")
        return None


def _describe_pitchfork(d, asset):
    """Generate rich description for a pitchfork with median line and tines projected to current time."""
    meta = d.get('metadata') or {}
    points = meta.get('points', [])
    if len(points) < 3:
        return None
    t0 = points[0].get('timestamp')
    v0 = points[0].get('value')
    t1 = points[1].get('timestamp')
    v1 = points[1].get('value')
    t2 = points[2].get('timestamp')
    v2 = points[2].get('value')
    if not all([t0, v0, t1, v1, t2, v2]):
        return None
    try:
        from datetime import datetime as dt
        # Median line: from anchor (P0) through midpoint of P1-P2
        mid_t = (t1 + t2) / 2
        mid_v = (v1 + v2) / 2
        if t0 == mid_t:
            return None
        median_slope = (mid_v - v0) / (mid_t - t0)
        now_ms = dt.now().timestamp() * 1000
        median_now = v0 + median_slope * (now_ms - t0)
        # Upper tine: parallel to median through P1
        upper_now = v1 + median_slope * (now_ms - t1)
        # Lower tine: parallel to median through P2
        lower_now = v2 + median_slope * (now_ms - t2)
        if median_now <= 0:
            return None
        # Ensure upper > lower
        if upper_now < lower_now:
            upper_now, lower_now = lower_now, upper_now
        direction = 'ascending' if median_slope > 0 else ('descending' if median_slope < 0 else 'horizontal')
        current_price = _get_asset_price(asset)
        pos_str = ''
        if current_price and current_price > 0:
            if current_price > median_now:
                pos_str = f" Price (${current_price:,.2f}) is above median."
            else:
                pos_str = f" Price (${current_price:,.2f}) is below median."
        return (
            f"{direction} pitchfork: median ${median_now:,.2f}, upper ${upper_now:,.2f}, "
            f"lower ${lower_now:,.2f}.{pos_str}"
        )
    except Exception as e:
        logger.debug(f"Error describing pitchfork: {e}")
        return None

drawings_bp = Blueprint('drawings', __name__, url_prefix='/api/drawings')

# =============================================================================
# Security Constants
# =============================================================================
ALLOWED_DRAWING_TYPES = {'horizontal_line', 'zone', 'annotation', 'trendline', 'fibonacci', 'position', 'long_position', 'short_position', 'spot', 'scenario', 'liquidation_zone'}
MAX_LABEL_LENGTH = 500
MAX_ASSET_LENGTH = 20
MAX_TIMEFRAME_LENGTH = 10
MAX_DRAWINGS_PER_ASSET_TIMEFRAME = 100
ALLOWED_LINE_STYLES = {'solid', 'dashed', 'dotted'}
MAX_LINE_WIDTH = 10
MIN_LINE_WIDTH = 1


# =============================================================================
# Decorators
# =============================================================================
def login_required_api(f):
    """Require a session login or an API key in the X-Internal-Key or
    X-API-Key header. Answers 401 JSON instead of redirecting."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_id'):
            api_key = request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')
            if not verify_api_key(api_key):
                logger.warning(f"Unauthorized API access attempt: {request.path}")
                return jsonify_iso({
                    "success": False,
                    "error": "Authentication required"
                }), 401
        return f(*args, **kwargs)
    return decorated_function


def get_request_user_id() -> int:
    """Resolve caller's user_id for drawing scoping.

    Priority:
      1. Session-authenticated user → ``session['user_id']``.
      2. Per-user API key (``cvx_k_``) → ``g.api_key_user_id`` (set by
         ``verify_api_key`` for per-user keys, never for the shared admin key).
      3. Anything else (shared admin key, unauthenticated) → ``0``.

    Per-user keys must resolve to the bound user: returning ``0`` for all
    API-key auth would propagate to ``db.drawing_list(user_id=None)`` (handlers
    use ``get_request_user_id() if session.get('user_id') else None``) and dump
    every user's drawings.
    """
    if session.get('user_id'):
        return session['user_id']
    api_uid = getattr(g, 'api_key_user_id', None)
    if api_uid is not None:
        return api_uid
    return 0  # shared admin key / unauthenticated → no user scope


def handle_errors(f):
    """Decorator to handle exceptions and return JSON errors."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.exception(f"Error in {f.__name__}: {e}")
            return jsonify_iso({
                "success": False,
                "error": "Internal server error"
            }), 500
    return decorated_function


# =============================================================================
# Input Validation
# =============================================================================
def validate_drawing_data(data, is_update=False):
    """
    Validate drawing input data.

    Args:
        data: Request JSON data
        is_update: If True, fields are optional

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not is_update:
        # Required fields for create
        required_fields = ['asset', 'timeframe', 'drawing_type']
        for field in required_fields:
            if field not in data:
                return False, f"Missing required field: {field}"

    # Validate asset
    if 'asset' in data:
        if not isinstance(data['asset'], str):
            return False, "Asset must be a string"
        if len(data['asset']) > MAX_ASSET_LENGTH:
            return False, f"Asset name too long (max {MAX_ASSET_LENGTH} chars)"
        if not data['asset'].strip():
            return False, "Asset cannot be empty"

    # Validate timeframe
    if 'timeframe' in data:
        if not isinstance(data['timeframe'], str):
            return False, "Timeframe must be a string"
        if len(data['timeframe']) > MAX_TIMEFRAME_LENGTH:
            return False, f"Timeframe too long (max {MAX_TIMEFRAME_LENGTH} chars)"

    # Validate drawing_type
    if 'drawing_type' in data:
        if data['drawing_type'] not in ALLOWED_DRAWING_TYPES:
            return False, f"Invalid drawing_type. Must be one of: {sorted(ALLOWED_DRAWING_TYPES)}"

    # Validate label
    if 'label' in data and data['label']:
        if not isinstance(data['label'], str):
            return False, "Label must be a string"
        if len(data['label']) > MAX_LABEL_LENGTH:
            return False, f"Label too long (max {MAX_LABEL_LENGTH} chars)"

    # Validate price_level
    if 'price_level' in data and data['price_level'] is not None:
        try:
            price = float(data['price_level'])
            if price < 0:
                return False, "Price level cannot be negative"
        except (TypeError, ValueError):
            return False, "Price level must be a number"

    # Validate price_level_upper (for zones)
    if 'price_level_upper' in data and data['price_level_upper'] is not None:
        try:
            price_upper = float(data['price_level_upper'])
            if price_upper < 0:
                return False, "Upper price level cannot be negative"
        except (TypeError, ValueError):
            return False, "Upper price level must be a number"

    # Validate line_style
    if 'line_style' in data and data['line_style']:
        if data['line_style'] not in ALLOWED_LINE_STYLES:
            return False, f"Invalid line_style. Must be one of: {sorted(ALLOWED_LINE_STYLES)}"

    # Validate line_width
    if 'line_width' in data and data['line_width'] is not None:
        try:
            width = int(data['line_width'])
            if width < MIN_LINE_WIDTH or width > MAX_LINE_WIDTH:
                return False, f"Line width must be between {MIN_LINE_WIDTH} and {MAX_LINE_WIDTH}"
        except (TypeError, ValueError):
            return False, "Line width must be an integer"

    # Validate color (basic hex check)
    if 'color' in data and data['color']:
        color = data['color']
        if not isinstance(color, str):
            return False, "Color must be a string"
        if not (color.startswith('#') and len(color) in (4, 7, 9)):
            return False, "Color must be a valid hex color (e.g., #ff0000)"

    return True, None


@drawings_bp.route('', methods=['GET'])
@handle_errors
@login_required_api
def drawing_list():
    """
    Get drawings, optionally filtered by asset and/or timeframe.

    GET /api/drawings
    GET /api/drawings?asset=BTCUSDT
    GET /api/drawings?asset=BTCUSDT&timeframe=1D
    GET /api/drawings?asset=BTCUSDT&timeframe=4h  (returns 4h + ALL)
    GET /api/drawings?drawing_type=scenario
    """
    asset = request.args.get('asset')
    timeframe = request.args.get('timeframe')
    drawing_type = request.args.get('drawing_type')

    db = get_content_db()

    # Determine user scope: session user sees own + system(0), API key sees system(0) only
    # Never call db.drawing_list(user_id=None) — that returns all
    # users' drawings. Per-user keys + sessions resolve to a real user id;
    # shared admin key has no per-user binding → refuse to leak.
    user_id = get_request_user_id()
    if not user_id or user_id == 0:
        return jsonify_iso({
            "success": False,
            "error": "Per-user authentication required (session or per-user API key)"
        }), 401

    # When timeframe is specified, also include 'ALL' drawings (cross-TF)
    if timeframe:
        drawings = db.get_drawings_with_all(asset=asset, timeframe=timeframe, drawing_type=drawing_type, user_id=user_id)
    else:
        drawings = db.drawing_list(asset=asset, drawing_type=drawing_type, user_id=user_id)

    return jsonify_iso({
        "success": True,
        "count": len(drawings),
        "drawings": drawings
    })


@drawings_bp.route('/all', methods=['GET'])
@handle_errors
@login_required_api
def get_all_drawings_grouped():
    """
    Get all drawings grouped by asset and timeframe.

    GET /api/drawings/all

    Returns: {
        "BTCUSDT": {
            "1D": [...drawings...],
            "4H": [...drawings...]
        },
        "SPX": {...}
    }
    """
    db = get_content_db()
    # Never call db.drawing_list(user_id=None) — that returns all
    # users' drawings. Per-user keys + sessions resolve to a real user id;
    # shared admin key has no per-user binding → refuse to leak.
    user_id = get_request_user_id()
    if not user_id or user_id == 0:
        return jsonify_iso({
            "success": False,
            "error": "Per-user authentication required (session or per-user API key)"
        }), 401
    grouped = db.get_all_drawings_grouped(user_id=user_id)

    # Count total
    total = sum(
        len(drawings)
        for asset_data in grouped.values()
        for drawings in asset_data.values()
    )

    return jsonify_iso({
        "success": True,
        "total_drawings": total,
        "drawings": grouped
    })


@drawings_bp.route('/<int:drawing_id>', methods=['GET'])
@handle_errors
@login_required_api
def get_drawing(drawing_id: int):
    """
    Get a single drawing by ID.

    GET /api/drawings/123
    """
    db = get_content_db()
    drawing = db.get_drawing_by_id(drawing_id)

    if drawing is None:
        return jsonify_iso({
            "success": False,
            "error": "Drawing not found"
        }), 404

    # Ownership check — prevent IDOR
    if drawing.get('user_id') and drawing['user_id'] != get_request_user_id():
        return jsonify_iso({
            "success": False,
            "error": "Drawing not found"
        }), 404

    return jsonify_iso({
        "success": True,
        "drawing": drawing
    })


@drawings_bp.route('/scenario/<int:scenario_id>/children', methods=['GET'])
@handle_errors
@login_required_api
def drawing_get_scenario_children(scenario_id: int):
    """
    Get all child drawings for a scenario.

    GET /api/drawings/scenario/123/children
    """
    db = get_content_db()

    parent = db.get_drawing_by_id(scenario_id)
    if not parent or parent.get('drawing_type') != 'scenario':
        return jsonify_iso({"success": False, "error": "Scenario not found"}), 404

    # Ownership check — avoid leaking other users' scenario children.
    # user_id == 0 means admin / system request (full access, same pattern
    # as sibling endpoints).
    user_id = get_request_user_id()
    if user_id != 0 and parent.get('user_id', 0) != user_id:
        return jsonify_iso({"success": False, "error": "Not your scenario"}), 403

    children = db.drawing_get_scenario_children(scenario_id, user_id=parent.get('user_id'))

    return jsonify_iso({
        "success": True,
        "scenario_id": scenario_id,
        "count": len(children),
        "drawings": children
    })


@drawings_bp.route('/context', methods=['GET'])
@handle_errors
@login_required_api
def get_drawings_context():
    """
    Get human-readable drawing context for the morning analysis bot.

    GET /api/drawings/context
    GET /api/drawings/context?assets=BTC,ETH,SPX
    GET /api/drawings/context?assets=all&enrich=1

    With enrich=1: annotates every level/zone/position with current price
    and % distance. Scenario paths get waypoint tracking.

    Returns text like:
    "BTC 1D: Level at 94,000.00 (label: 'Support') [price $91,234 is 2.9% below]"
    """
    asset_filter = request.args.get('assets', '')
    assets = [a.strip().upper() for a in asset_filter.split(',') if a.strip()] if asset_filter else None
    enrich = request.args.get('enrich', '0') == '1'

    db = get_content_db()
    # Never call db.drawing_list(user_id=None) — that returns all
    # users' drawings. Per-user keys + sessions resolve to a real user id;
    # shared admin key has no per-user binding → refuse to leak.
    user_id = get_request_user_id()
    if not user_id or user_id == 0:
        return jsonify_iso({
            "success": False,
            "error": "Per-user authentication required (session or per-user API key)"
        }), 401
    grouped = db.get_all_drawings_grouped(user_id=user_id)

    # Pre-fetch prices for all assets when enriching
    price_cache = {}
    if enrich:
        for a in grouped.keys():
            if assets and a not in assets:
                continue
            p = _get_asset_price(a)
            if p:
                price_cache[a] = p

    lines = []
    scenario_lines = []
    scenario_assets = set()
    for asset in sorted(grouped.keys()):
        if assets and asset not in assets:
            continue
        cp = price_cache.get(asset) if enrich else None
        for tf in sorted(grouped[asset].keys()):
            for d in grouped[asset][tf]:
                meta = d.get('metadata') or {}
                # Handle scenarios separately
                if d['drawing_type'] == 'scenario':
                    if meta.get('archived'):
                        continue  # Skip archived scenarios
                    scenario_assets.add(asset)
                    name = sanitize_for_llm(str(meta.get('name', d.get('label', 'Unnamed'))), max_length=200, source="scenario_name")
                    created = d.get('created_at', '')[:10]

                    thesis = sanitize_for_llm(str(meta.get('thesis', '')), max_length=1000, source="scenario_thesis")

                    # v2 scenarios (drawing groups): fetch children
                    if meta.get('is_group'):
                        children = db.drawing_get_scenario_children(d['id'])
                        if not children and not thesis:
                            scenario_lines.append(
                                f'SCENARIO "{name}" on {asset} {tf} (drawn {created}): empty (no drawings yet)'
                            )
                            continue
                        child_descs = []
                        for c in children:
                            c_meta = c.get('metadata') or {}
                            if c['drawing_type'] == 'horizontal_line':
                                lv = c.get('price_level', 0)
                                cd = f"Level at {lv:,.2f}" if lv else "Level"
                                if enrich and cp and lv:
                                    cd += _price_dist(cp, lv)
                            elif c['drawing_type'] == 'zone':
                                c_kline = c_meta.get('kline_name', '')
                                c_channel_types = ('parallelChannel', 'boundedChannel', 'parallelStraightLine', 'priceChannelLine')
                                if c_kline in c_channel_types:
                                    ch_desc = _describe_parallel_channel(c, asset)
                                    cd = ch_desc if ch_desc else f"Zone {c.get('price_level', 0):,.2f}-{c.get('price_level_upper', 0):,.2f}"
                                elif c_kline == 'pitchfork':
                                    pf_desc = _describe_pitchfork(c, asset)
                                    cd = pf_desc if pf_desc else f"Zone {c.get('price_level', 0):,.2f}-{c.get('price_level_upper', 0):,.2f}"
                                else:
                                    zl = c.get('price_level', 0)
                                    zu = c.get('price_level_upper', zl)
                                    cd = f"Zone {zl:,.2f}-{zu:,.2f}"
                                    if enrich and cp and zl and zu:
                                        if cp > zu:
                                            pct = (cp - zu) / zu * 100
                                            cd += f" [price ${cp:,.2f} is {pct:.1f}% above zone]"
                                        elif cp < zl:
                                            pct = (zl - cp) / zl * 100
                                            cd += f" [price ${cp:,.2f} is {pct:.1f}% below zone]"
                                        else:
                                            cd += f" [price ${cp:,.2f} is INSIDE zone]"
                            elif c['drawing_type'] == 'fibonacci':
                                fib_desc = _describe_fibonacci(c, asset)
                                cd = fib_desc if fib_desc else f"fibonacci at {c.get('price_level', 'N/A')}"
                            elif c['drawing_type'] == 'annotation':
                                text = sanitize_for_llm(str(c.get('label') or c_meta.get('text', '')), max_length=200, source="annotation_text")
                                cd = f'Text: "{text[:80]}"' if text else "Text annotation"
                            elif c['drawing_type'] in ('position', 'long_position', 'short_position'):
                                points = c_meta.get('points', [])
                                if len(points) >= 3:
                                    entry_v = points[0].get('value', 0)
                                    sl_v = points[1].get('value', 0)
                                    tp_v = points[2].get('value', 0)
                                    direction = 'Long' if sl_v < entry_v else 'Short'
                                    cd = f"{direction} position: Entry {entry_v:,.0f}, SL {sl_v:,.0f}, TP {tp_v:,.0f}"
                                    if enrich and cp and entry_v:
                                        pnl = (cp - entry_v) / entry_v * 100
                                        if direction == 'Short':
                                            pnl = -pnl
                                        cd += f" [P&L: {pnl:+.1f}%]"
                                        cd += _price_dist(cp, entry_v)
                                else:
                                    cd = "Position"
                            elif c['drawing_type'] == 'trendline':
                                c_ext = c_meta.get('extend_data', {})
                                is_watched = bool(c_ext.get('watch'))
                                tl_desc = _describe_trendline(c, asset, watched=is_watched)
                                cd = tl_desc if tl_desc else "Trendline"
                            elif c['drawing_type'] == 'scenario':
                                # scenarioPath within a group
                                pts = c_meta.get('points', [])
                                if pts:
                                    cd = 'Path: ' + ' → '.join(f"{p.get('value', 0):,.0f}" for p in pts)
                                    if enrich and cp:
                                        cd += _enrich_path(cp, pts)
                                else:
                                    cd = 'Path'
                            else:
                                cd = f"{c['drawing_type']}"
                            if c.get('label') and c['drawing_type'] != 'annotation':
                                label_text = sanitize_for_llm(str(c['label']), max_length=200, source="drawing_label")
                                cd += f" (label: '{label_text}')"
                            child_descs.append(f"  - {cd}")

                        # Scenario header with price context
                        header = f'SCENARIO "{name}" on {asset} {tf} (drawn {created})'
                        if enrich and cp:
                            header += f' — current price: ${cp:,.2f}'
                        thesis_line = f'  THESIS: {thesis}\n' if thesis else ''
                        child_section = '\n'.join(child_descs) + '\n' if child_descs else ''
                        scenario_lines.append(
                            header + ':\n'
                            + thesis_line
                            + child_section
                            + '  Analyze current price action against this scenario. '
                            + ('Call out explicitly if price action deviates from the thesis.' if thesis else 'Analyze current price action against this annotated scenario.')
                        )
                    else:
                        # v1 scenarios (scenarioPath polyline)
                        direction = meta.get('direction', 'unknown')
                        points = meta.get('points', [])
                        pts_str = ' → '.join(f"{p.get('value', 0):,.0f}" for p in points) if points else 'no points'
                        if enrich and cp and points:
                            pts_str += _enrich_path(cp, points)
                        thesis_line = f'  THESIS: {thesis}\n' if thesis else ''
                        header = f'SCENARIO "{name}" on {asset} {tf} (drawn {created})'
                        if enrich and cp:
                            header += f' — current price: ${cp:,.2f}'
                        scenario_lines.append(
                            header + ':\n'
                            + thesis_line +
                            f'  {pts_str}. Direction: {direction}.\n'
                            f'  Compare current price action against this expected path.'
                            + (' Call out explicitly if price action deviates from the thesis.' if thesis else '')
                        )
                    continue

                desc = f"{asset}: " if tf == 'ALL' else f"{asset} {tf}: "
                if d['drawing_type'] == 'horizontal_line':
                    lv = d.get('price_level', 0)
                    desc += f"Level at {lv:,.2f}"
                    if enrich and cp and lv:
                        desc += _price_dist(cp, lv)
                elif d['drawing_type'] == 'zone':
                    kline_name = meta.get('kline_name', '')
                    channel_types = ('parallelChannel', 'boundedChannel', 'parallelStraightLine', 'priceChannelLine')
                    if kline_name in channel_types:
                        ch_desc = _describe_parallel_channel(d, asset)
                        if ch_desc:
                            desc += ch_desc
                        else:
                            zl = d.get('price_level', 0)
                            zu = d.get('price_level_upper', zl)
                            desc += f"Zone {zl:,.2f}-{zu:,.2f}"
                    elif kline_name == 'pitchfork':
                        pf_desc = _describe_pitchfork(d, asset)
                        if pf_desc:
                            desc += pf_desc
                        else:
                            zl = d.get('price_level', 0)
                            zu = d.get('price_level_upper', zl)
                            desc += f"Zone {zl:,.2f}-{zu:,.2f}"
                    else:
                        zl = d.get('price_level', 0)
                        zu = d.get('price_level_upper', zl)
                        desc += f"Zone {zl:,.2f}-{zu:,.2f}"
                        if enrich and cp and zl and zu:
                            if cp > zu:
                                pct = (cp - zu) / zu * 100
                                desc += f" [price ${cp:,.2f} is {pct:.1f}% above zone]"
                            elif cp < zl:
                                pct = (zl - cp) / zl * 100
                                desc += f" [price ${cp:,.2f} is {pct:.1f}% below zone]"
                            else:
                                desc += f" [price ${cp:,.2f} is INSIDE zone]"
                elif d['drawing_type'] == 'fibonacci':
                    fib_desc = _describe_fibonacci(d, asset)
                    desc += fib_desc if fib_desc else f"fibonacci at {d.get('price_level', 'N/A')}"
                elif d['drawing_type'] in ('position', 'long_position', 'short_position'):
                    points = meta.get('points', [])
                    if len(points) >= 3:
                        entry_v = points[0].get('value', 0)
                        sl_v = points[1].get('value', 0)
                        tp_v = points[2].get('value', 0)
                        direction = 'Long' if sl_v < entry_v else 'Short'
                        desc += f"{direction} position: Entry {entry_v:,.2f}, SL {sl_v:,.2f}, TP {tp_v:,.2f}"
                        if enrich and cp and entry_v:
                            pnl = (cp - entry_v) / entry_v * 100
                            if direction == 'Short':
                                pnl = -pnl
                            desc += f" [P&L: {pnl:+.1f}%]"
                    else:
                        desc += "Position (incomplete)"
                elif d['drawing_type'] == 'trendline':
                    ext_data = meta.get('extend_data', {})
                    is_watched = bool(ext_data.get('watch'))
                    tl_desc = _describe_trendline(d, asset, watched=is_watched)
                    desc += tl_desc if tl_desc else f"trendline at {d.get('price_level', 'N/A')}"
                else:
                    desc += f"{d['drawing_type']} at {d.get('price_level', 'N/A')}"
                if d.get('label'):
                    label_text = sanitize_for_llm(str(d['label']), max_length=200, source="drawing_label")
                    desc += f" (label: '{label_text}')"
                lines.append(desc)

    # Append scenario section if any exist
    if scenario_lines:
        lines.append('\n=== SCENARIOS (drawings) ===')
        lines.extend(scenario_lines)

    # Append structured scenarios v2 data
    try:
        from carrac.scenarios.models import get_active_scenarios as _get_active_v2
        v2_scenarios = _get_active_v2(user_id)
        if v2_scenarios:
            lines.append('\n=== SCENARIOS (structured) ===')
            for s in v2_scenarios:
                name = s.get('label', 'Unnamed')
                status = s.get('status', '?').upper()
                thesis = s.get('thesis', '')
                conds = s.get('conditions') or {}
                rules = conds.get('rules', [])
                met_count = sum(1 for r in rules if r.get('met'))
                total = len(rules)
                asset_s = s.get('asset', '?')
                direction = s.get('direction', '')
                conv = s.get('conviction')
                entry_l = s.get('entry_low')
                entry_h = s.get('entry_high')
                inv = s.get('invalidation')

                header = f'SCENARIO "{name}" ({asset_s}) [{status}]'
                if total > 0:
                    header += f' — {met_count}/{total} conditions met'
                if conv:
                    header += f', conviction {conv}/5'
                if direction:
                    header += f', {direction}'
                lines.append(header)
                if thesis:
                    lines.append(f'  Thesis: {thesis}')
                if entry_l and entry_h:
                    lines.append(f'  Entry zone: ${entry_l:,.0f} - ${entry_h:,.0f}')
                if inv:
                    lines.append(f'  Invalidation: ${inv:,.0f}')
                targets = s.get('targets', [])
                if targets:
                    tp_str = ', '.join(f"${t.get('price', 0):,.0f}" for t in targets[:3] if isinstance(t, dict))
                    if tp_str:
                        lines.append(f'  Targets: {tp_str}')
                # Met/pending conditions
                for r in rules:
                    if r.get('met'):
                        lines.append(f'  [MET] {r.get("evidence", r.get("type", "?"))}')
                    else:
                        lines.append(f'  [PENDING] {r.get("evidence", r.get("type", "?"))}')
                # Milestone playbook
                milestones = s.get('milestones') or []
                if milestones:
                    ms_stages = {}
                    for ms in milestones:
                        ms_stages.setdefault(ms.get('stage', 1), []).append(ms)
                    active_stage = None
                    for sn in sorted(ms_stages.keys()):
                        has_winner = any(ms.get('status') in ('triggered', 'completed') for ms in ms_stages[sn])
                        all_dead = all(ms.get('status') == 'skipped' for ms in ms_stages[sn])
                        if not has_winner and not all_dead:
                            active_stage = sn
                            break
                    stage_label = f' (Stage {active_stage} active)' if active_stage else ''
                    lines.append(f'  PLAYBOOK{stage_label}:')
                    for sn in sorted(ms_stages.keys()):
                        sms = ms_stages[sn]
                        st_status = 'DONE' if all(ms.get('status') in ('completed', 'skipped') for ms in sms) else \
                                    'ACTIVE' if any(ms.get('status') in ('triggered', 'watching') for ms in sms) else 'WAITING'
                        lines.append(f'    Stage {sn} [{st_status}]:')
                        for ms in sms:
                            ms_label = ms.get('label', '?')
                            ms_status = ms.get('status', 'dormant')
                            ms_action = ms.get('action_type', '?')
                            ms_rules = (ms.get('conditions') or {}).get('rules', [])
                            ms_met = sum(1 for r in ms_rules if r.get('met'))
                            detail = f'{ms_action}'
                            if ms_rules:
                                detail += f', {ms_met}/{len(ms_rules)} conditions met'
                            review_by = ms.get('review_by')
                            if review_by and ms_status in ('watching', 'dormant'):
                                try:
                                    from datetime import datetime, timezone
                                    rb = datetime.fromisoformat(review_by.replace('Z', '+00:00'))
                                    days_left = (rb - datetime.now(timezone.utc)).days
                                    if days_left < 0:
                                        detail += f', OVERDUE by {abs(days_left)}d'
                                    else:
                                        detail += f', review in {days_left}d'
                                except Exception:
                                    pass
                            status_mark = '✓' if ms_status == 'completed' else '✗' if ms_status == 'skipped' else '→' if ms_status == 'triggered' else '·'
                            lines.append(f'      {status_mark} {ms_label} [{ms_status}] — {detail}')
                scenario_assets.add(asset_s)
    except Exception as v2_err:
        logger.debug(f"Scenarios v2 context error: {v2_err}")

    return jsonify_iso({
        "success": True,
        "context": "\n".join(lines),
        "count": len(lines),
        "scenario_assets": sorted(scenario_assets) if enrich else [],
    })


@drawings_bp.route('/scenarios/assets', methods=['GET'])
@handle_errors
@login_required_api
def get_scenario_assets():
    """
    Get list of assets that have at least one active (non-archived) scenario.

    GET /api/drawings/scenarios/assets

    Returns: {"success": true, "assets": ["BTC", "ETH"], "count": 2}
    """
    db = get_content_db()
    # Never call db.drawing_list(user_id=None) — that returns all
    # users' drawings. Per-user keys + sessions resolve to a real user id;
    # shared admin key has no per-user binding → refuse to leak.
    user_id = get_request_user_id()
    if not user_id or user_id == 0:
        return jsonify_iso({
            "success": False,
            "error": "Per-user authentication required (session or per-user API key)"
        }), 401
    drawings = db.drawing_list(drawing_type='scenario', user_id=user_id)
    assets = set()
    for d in drawings:
        meta = d.get('metadata') or {}
        if meta.get('archived'):
            continue
        asset = d.get('asset', '').strip()
        if asset:
            assets.add(asset)
    return jsonify_iso({
        "success": True,
        "assets": sorted(assets),
        "count": len(assets)
    })


@drawings_bp.route('/positions', methods=['GET'])
@handle_errors
@login_required_api
def trade_get_positions():
    """
    Get all active position drawings with entry/SL/TP data.

    GET /api/drawings/positions

    Returns structured position data for bot consumption.
    """
    import json as json_mod
    db = get_content_db()
    # Never call db.drawing_list(user_id=None) — that returns all
    # users' drawings. Per-user keys + sessions resolve to a real user id;
    # shared admin key has no per-user binding → refuse to leak.
    user_id = get_request_user_id()
    if not user_id or user_id == 0:
        return jsonify_iso({
            "success": False,
            "error": "Per-user authentication required (session or per-user API key)"
        }), 401
    positions = []
    for dtype in ('position', 'long_position', 'short_position'):
        drawings = db.drawing_list(drawing_type=dtype, user_id=user_id)
        for d in drawings:
            meta = d.get('metadata') or {}
            if isinstance(meta, str):
                try:
                    meta = json_mod.loads(meta)
                except (ValueError, TypeError):
                    meta = {}
            if meta.get('archived'):
                continue
            points = meta.get('points', [])
            if len(points) < 3:
                continue
            entry_v = points[0].get('value', 0)
            sl_v = points[1].get('value', 0)
            tp_v = points[2].get('value', 0)
            direction = 'long' if sl_v < entry_v else 'short'
            risk = abs(entry_v - sl_v)
            reward = abs(tp_v - entry_v)
            rr = round(reward / risk, 2) if risk > 0 else 0
            positions.append({
                "id": d.get('id'),
                "asset": d.get('asset', ''),
                "timeframe": d.get('timeframe', ''),
                "direction": direction,
                "entry": round(entry_v, 2),
                "stop_loss": round(sl_v, 2),
                "take_profit": round(tp_v, 2),
                "risk_reward": rr,
                "label": d.get('label', ''),
                "created_at": d.get('created_at', ''),
            })
    return jsonify_iso({
        "success": True,
        "positions": positions,
        "count": len(positions)
    })
