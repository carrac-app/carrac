"""MCP tools: Drawing CRUD + play-drawing linking + scenario helpers.

Note: drawing_list (text-context) and drawing_delete (bulk) already exist
in context.py. This module adds drawing tools:
  drawing_create              — create any drawing type with convenience helpers
  drawing_update              — patch updatable fields with optimistic-concurrency
  drawing_link_to_play        — link/unlink drawings to/from plays
  drawing_get_scenario_children       — list children of a scenario drawing
  drawing_create_group        — atomic parent + children creation (
                                 formerly drawing_create_scenario, kept as a
                                 registry.LEGACY_TOOL_ALIASES shim)
"""

import re
import logging

import pandas as pd

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id

logger = logging.getLogger('carrac.mcp.drawings')

# ── Constants (mirrors carrac/drawings/routes.py) ───────────────────────
ALLOWED_DRAWING_TYPES = {
    'horizontal_line', 'zone', 'annotation', 'trendline', 'fibonacci',
    'position', 'long_position', 'short_position', 'spot', 'scenario',
    'liquidation_zone',
}
ALLOWED_LINE_STYLES = {'solid', 'dashed', 'dotted'}
MAX_LABEL_LENGTH = 500
MAX_LINE_WIDTH = 10
MIN_LINE_WIDTH = 1
_COLOR_RE = re.compile(r'^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')

# Bar timestamp placeholder (milliseconds) — used when callers don't supply ts
_BAR_TS_PLACEHOLDER = 0


def _validate_color(color):
    """Return (ok, error). ok=True means valid or empty."""
    if not color:
        return True, None
    if not isinstance(color, str) or not _COLOR_RE.match(color):
        return False, 'color must be a hex string like #rgb, #rrggbb, or #rrggbbaa'
    return True, None


def _validate_line_style(style):
    if style and style not in ALLOWED_LINE_STYLES:
        return False, f'line_style must be one of: {sorted(ALLOWED_LINE_STYLES)}'
    return True, None


def _validate_line_width(width):
    if width is not None:
        try:
            w = int(width)
        except (TypeError, ValueError):
            return False, 'line_width must be an integer'
        if w < MIN_LINE_WIDTH or w > MAX_LINE_WIDTH:
            return False, f'line_width must be between {MIN_LINE_WIDTH} and {MAX_LINE_WIDTH}'
    return True, None


def _publish_sync(user_id, asset):
    """Best-effort drawing sync event for open browser tabs."""
    try:
        from carrac.sync.events import publish_drawing_event
        publish_drawing_event(user_id, [asset], session_id='mcp')
    except Exception as exc:
        logger.debug('publish_drawing_event failed (non-fatal): %s', exc)


# =============================================================================
# drawing_create
# =============================================================================
@mcp_tool(
    'drawing_create',
    'Create a chart drawing (level, zone, trendline, fibonacci, annotation, position, scenario, etc.) '
    'and persist it to the drawing DB. '
    'SIDE EFFECTS: 1) row inserted in drawing DB; 2) Redis SSE drawing-sync event fired — '
    'all the user\'s open browser tabs receive the new drawing immediately. '
    'Convenience shortcuts: pass `price` for horizontal_line; `price_low`+`price_high` '
    'for zone; `entry`+`stop`+`target` for position; `from_price`+`to_price` for trendline; '
    '`high_price`+`low_price` for fibonacci; `text`+`price` for annotation; `name` for scenario. '
    'Convenience fields take precedence over raw price_level/metadata when both are supplied. '
    'Pass ISO dates via from_date/to_date/at_date — auto-resolved to bar-open epoch ms. '
    'Use kline_name to override the KLineChart overlay name. Canonical names: '
    'horizontal_line: horizontalStraightLine, horizontalRayLine, horizontalSegment, priceLine; '
    'trendline: segment (default), rayLine, straightLine; '
    'zone: rect (default), boundedChannel, pitchfork, priceChannelLine, gannBox; '
    'fibonacci: fibonacciRetrace (default), fibonacciExtension, fibonacciInfinite, '
    'fibonacciLine, fibonacciSegment, fibonacciCircle, fibonacciSpiral, fibonacciSpeedResistanceFan; '
    'annotation: simpleAnnotation, simpleTag, textAnnotation, arrow, elliottWave, anchoredVwap, alertLine, '
    'arrowUpMarker, arrowDownMarker, flagPin, circleMarker, callout, verticalStraightLine; '
    'position: position (default), longPosition, shortPosition. '
    'Returns the full saved drawing row including assigned id.',
    {
        'type': 'object',
        'properties': {
            'asset': {
                'type': 'string',
                'description': 'Asset symbol, e.g. BTC, ETH (required, max 20 chars)',
            },
            'timeframe': {
                'type': 'string',
                'description': (
                    'Timeframe, e.g. 1D, 4h, 1h, 1W, 15m, ALL (required, max 10 chars)'
                ),
            },
            'drawing_type': {
                'type': 'string',
                'enum': sorted(ALLOWED_DRAWING_TYPES),
                'description': 'Drawing type (required)',
            },
            # ── Convenience: horizontal_line ──────────────────────────────────
            'price': {
                'type': 'number',
                'description': (
                    'Convenience for horizontal_line/annotation: maps to price_level'
                ),
            },
            # ── Convenience: zone ────────────────────────────────────────────
            'price_low': {
                'type': 'number',
                'description': 'Convenience for zone: lower bound → price_level',
            },
            'price_high': {
                'type': 'number',
                'description': 'Convenience for zone: upper bound → price_level_upper',
            },
            'style': {
                'type': 'string',
                'enum': ['rectangle', 'channel', 'pitchfork'],
                'description': (
                    'Convenience for zone: maps to kline_name. '
                    'rectangle→rect, channel→boundedChannel, pitchfork→pitchfork. '
                    'Overridden by explicit kline_name if both are supplied.'
                ),
            },
            # ── Convenience: position ─────────────────────────────────────────
            'entry': {
                'type': 'number',
                'description': 'Convenience for position drawing: entry price',
            },
            'stop': {
                'type': 'number',
                'description': 'Convenience for position drawing: stop-loss price',
            },
            'target': {
                'type': 'number',
                'description': 'Convenience for position drawing: primary target price',
            },
            'additional_targets': {
                'type': 'array',
                'items': {'type': 'number'},
                'description': (
                    'Convenience for position drawing: extra take-profit levels '
                    'stored in metadata.extend_data.tps'
                ),
            },
            # ── Convenience: trendline ────────────────────────────────────────
            'from_price': {
                'type': 'number',
                'description': 'Convenience for trendline: start price',
            },
            'to_price': {
                'type': 'number',
                'description': 'Convenience for trendline: end price',
            },
            'from_ts': {
                'type': 'integer',
                'description': (
                    'Convenience for trendline: start bar timestamp (ms epoch); '
                    'defaults to 0 if omitted. Use from_date for ISO date input.'
                ),
            },
            'to_ts': {
                'type': 'integer',
                'description': (
                    'Convenience for trendline: end bar timestamp (ms epoch); '
                    'defaults to 0 if omitted. Use to_date for ISO date input.'
                ),
            },
            # ── Date convenience (auto-resolves to bar epoch ms) ──────────
            'from_date': {
                'type': 'string',
                'description': (
                    'ISO date for start timestamp, e.g. "2026-05-01" or "2026-05-01T16:00:00Z". '
                    'Also accepts "now"/"latest" or "-N" (N bars ago). '
                    'Resolved to bar epoch ms. Overrides from_ts if both supplied.'
                ),
            },
            'to_date': {
                'type': 'string',
                'description': (
                    'ISO date for end timestamp. Same format as from_date. '
                    'Overrides to_ts if both supplied.'
                ),
            },
            'at_date': {
                'type': 'string',
                'description': (
                    'ISO date for single-point drawings (annotation, horizontal_line). '
                    'Same format as from_date. Overrides timestamp if both supplied.'
                ),
            },
            # ── Convenience: fibonacci ────────────────────────────────────────
            'high_price': {
                'type': 'number',
                'description': 'Convenience for fibonacci: high of the swing',
            },
            'low_price': {
                'type': 'number',
                'description': 'Convenience for fibonacci: low of the swing',
            },
            # ── Convenience: annotation ───────────────────────────────────────
            'text': {
                'type': 'string',
                'description': 'Convenience for annotation: alias for label',
            },
            'timestamp': {
                'type': 'integer',
                'description': (
                    'Convenience for annotation/trendline: bar timestamp (ms epoch). '
                    'Use at_date for ISO date input.'
                ),
            },
            # ── Convenience: scenario ─────────────────────────────────────────
            'name': {
                'type': 'string',
                'description': (
                    'Convenience for scenario: scenario name (also used as label)'
                ),
            },
            'thesis': {
                'type': 'string',
                'description': 'Convenience for scenario: thesis text (stored in metadata)',
            },
            'direction': {
                'type': 'string',
                'enum': ['bullish', 'bearish', 'neutral'],
                'description': (
                    'Convenience for scenario: directional bias stored in metadata'
                ),
            },
            # ── Raw / canonical fields ────────────────────────────────────────
            'kline_name': {
                'type': 'string',
                'description': (
                    'Override the KLineChart overlay name. When supplied, takes precedence '
                    'over any default or style-mapped name. See tool description for canonical names.'
                ),
            },
            'price_level': {
                'type': 'number',
                'description': 'Raw: primary price level (overridden by convenience fields)',
            },
            'price_level_upper': {
                'type': 'number',
                'description': 'Raw: upper price level for zones',
            },
            'label': {
                'type': 'string',
                'description': 'Drawing label / annotation text (max 500 chars)',
            },
            'color': {
                'type': 'string',
                'description': 'Hex colour: #rgb / #rrggbb / #rrggbbaa (default #3fb950)',
            },
            'line_style': {
                'type': 'string',
                'enum': ['solid', 'dashed', 'dotted'],
                'description': 'Line style (default solid)',
            },
            'line_width': {
                'type': 'integer',
                'description': 'Line width 1–10 (default 2)',
            },
            'metadata': {
                'type': 'object',
                'description': (
                    'Raw metadata dict for kline_name, points, extend_data, etc. '
                    'Merged with (and overridden by) convenience-field-generated metadata.'
                ),
            },
            'scenario_id': {
                'type': 'integer',
                'description': 'Parent scenario drawing ID (for child drawings)',
            },
        },
        'required': ['asset', 'timeframe', 'drawing_type'],
    },
    min_tier='free',
)
def drawing_create(arguments):
    from carrac.content_db import get_content_db

    user_id = get_mcp_user_id()

    # ── Required fields ──────────────────────────────────────────────────────
    asset = (arguments.get('asset') or '').strip().upper()
    timeframe = (arguments.get('timeframe') or '').strip().upper()
    drawing_type = (arguments.get('drawing_type') or '').strip().lower()

    if not asset:
        raise ToolError('asset is required')
    if len(asset) > 20:
        raise ToolError('asset name too long (max 20 chars)')
    if not timeframe:
        raise ToolError('timeframe is required')
    if len(timeframe) > 10:
        raise ToolError('timeframe too long (max 10 chars)')
    if drawing_type not in ALLOWED_DRAWING_TYPES:
        raise ToolError(
            f'drawing_type must be one of: {sorted(ALLOWED_DRAWING_TYPES)}'
        )

    # ── Optional common fields (raw) ─────────────────────────────────────────
    price_level = arguments.get('price_level')
    price_level_upper = arguments.get('price_level_upper')
    label = (arguments.get('label') or '').strip() or None
    color = arguments.get('color') or '#3fb950'
    line_style = arguments.get('line_style') or 'solid'
    line_width = arguments.get('line_width', 2)
    metadata = dict(arguments.get('metadata') or {})
    scenario_id = arguments.get('scenario_id')

    # ── Validation ───────────────────────────────────────────────────────────
    ok, err = _validate_color(color)
    if not ok:
        raise ToolError(err)
    ok, err = _validate_line_style(line_style)
    if not ok:
        raise ToolError(err)
    ok, err = _validate_line_width(line_width)
    if not ok:
        raise ToolError(err)
    line_width = int(line_width)
    if price_level is not None and float(price_level) < 0:
        raise ToolError('price_level cannot be negative')
    if price_level_upper is not None and float(price_level_upper) < 0:
        raise ToolError('price_level_upper cannot be negative')
    if label and len(label) > MAX_LABEL_LENGTH:
        raise ToolError(f'label too long (max {MAX_LABEL_LENGTH} chars)')

    # ── kline_name override: explicit param wins over all defaults ───────
    explicit_kline_name = arguments.get('kline_name')
    if explicit_kline_name and 'kline_name' not in metadata:
        metadata['kline_name'] = explicit_kline_name

    # ── style convenience: maps zone style names to kline_name ──────────
    _STYLE_MAP = {'rectangle': 'rect', 'channel': 'boundedChannel', 'pitchfork': 'pitchfork'}
    style_param = arguments.get('style')
    if style_param and style_param in _STYLE_MAP:
        # Only apply if kline_name not already set (explicit kline_name takes precedence)
        if 'kline_name' not in metadata:
            metadata['kline_name'] = _STYLE_MAP[style_param]

    # ── Date-to-timestamp resolution ────────────────────────────────────
    def _resolve_date_to_ms(when_str):
        """Resolve ISO date / 'now' / '-N' to bar open ms epoch. Returns int or None."""
        if not when_str:
            return None
        try:
            from database import OHLCVDatabase
            db_inst = OHLCVDatabase()
            tf_lower = timeframe.lower()
            _BARS_NEEDED = {
                '1d': 400, '1w': 200, '1m': 60,
                '4h': 2400, '1h': 9000, '15m': 35000,
                '3d': 150, 'all': 400,
            }
            bars_needed = _BARS_NEEDED.get(tf_lower, 1000)

            def _coerce_dt(d):
                if d is None or d.empty:
                    return d
                if not pd.api.types.is_datetime64_any_dtype(d.index):
                    d.index = pd.to_datetime(d.index, unit='ms', utc=True)
                elif d.index.tz is None:
                    d.index = d.index.tz_localize('UTC')
                return d

            def _fetch(bars):
                # HTF: prefer cached multi-tf data (Binance daily fetches occasionally fail)
                if timeframe.upper() in {'1D', '3D', '1W', '1M'}:
                    try:
                        mt = db_inst.get_multi_timeframe_data(asset)
                        key = timeframe.upper() if timeframe.upper() in mt else timeframe
                        if key in mt and mt[key] is not None and not mt[key].empty:
                            return _coerce_dt(mt[key].tail(bars).copy())
                    except Exception:
                        pass
                # LTF cached
                if timeframe in {'15m', '1h', '4h'}:
                    try:
                        ltf = db_inst.get_ltf_multi_timeframe_data(asset)
                        if timeframe in ltf and ltf[timeframe] is not None and not ltf[timeframe].empty:
                            return _coerce_dt(ltf[timeframe].tail(bars).copy())
                    except Exception:
                        pass
                return _coerce_dt(db_inst.fetch_klines_for_chart(asset, interval=timeframe, bars=bars))

            if when_str in ('now', 'latest'):
                df = _fetch(2)
                if df is None or df.empty:
                    raise ToolError(f'No kline data available for {asset}/{timeframe}')
                return int(df.index[-1].timestamp() * 1000)
            elif when_str.startswith('-') and when_str[1:].isdigit():
                n = int(when_str[1:])
                df = _fetch(n + 2)
                if df is None or df.empty or len(df) < 1:
                    raise ToolError(f'No kline data for {asset}/{timeframe}')
                idx = max(0, len(df) - 1 - n)
                return int(df.index[idx].timestamp() * 1000)
            else:
                target = pd.Timestamp(when_str, tz='UTC')
                df = _fetch(bars_needed)
                if df is None or df.empty:
                    raise ToolError(f'No kline data for {asset}/{timeframe}')
                if target < df.index[0]:
                    raise ToolError(
                        f'Target date {when_str} is before earliest cached bar '
                        f'({df.index[0].isoformat()})'
                    )
                bar_ts = df.index.asof(target)
                if pd.isna(bar_ts):
                    bar_ts = df.index[-1]
                return int(bar_ts.timestamp() * 1000)
        except ToolError:
            raise
        except Exception as exc:
            logger.warning('Date resolution failed for %s: %s', when_str, exc)
            return None

    # Apply date overrides (from_date > from_ts > 0)
    from_date = arguments.get('from_date')
    to_date = arguments.get('to_date')
    at_date = arguments.get('at_date')

    if from_date:
        resolved = _resolve_date_to_ms(from_date)
        if resolved is not None:
            arguments = dict(arguments)
            arguments['from_ts'] = resolved
    if to_date:
        resolved = _resolve_date_to_ms(to_date)
        if resolved is not None:
            arguments = dict(arguments)
            arguments['to_ts'] = resolved
    if at_date:
        resolved = _resolve_date_to_ms(at_date)
        if resolved is not None:
            arguments = dict(arguments)
            arguments['timestamp'] = resolved

    # ── Convenience field resolution ─────────────────────────────────────────
    inferred_direction = None

    if drawing_type == 'horizontal_line':
        if arguments.get('price') is not None:
            price_level = float(arguments['price'])
        if 'kline_name' not in metadata:
            metadata['kline_name'] = 'horizontalStraightLine'
        if 'points' not in metadata:
            ts = arguments.get('timestamp', _BAR_TS_PLACEHOLDER)
            metadata['points'] = [{'timestamp': ts, 'value': price_level or 0}]

    elif drawing_type in ('zone', 'liquidation_zone'):
        if arguments.get('price_low') is not None:
            price_level = float(arguments['price_low'])
        if arguments.get('price_high') is not None:
            price_level_upper = float(arguments['price_high'])
        if 'kline_name' not in metadata:
            metadata['kline_name'] = 'rect'

    elif drawing_type in ('position', 'long_position', 'short_position'):
        entry = arguments.get('entry')
        stop = arguments.get('stop')
        target = arguments.get('target')
        if entry is not None and stop is not None and target is not None:
            entry = float(entry)
            stop = float(stop)
            target = float(target)
            # Infer direction
            if stop < entry:
                inferred_direction = 'long'
                if target <= entry:
                    raise ToolError(
                        'Long position: target must be above entry (target > entry > stop)'
                    )
            elif stop > entry:
                inferred_direction = 'short'
                if target >= entry:
                    raise ToolError(
                        'Short position: target must be below entry (target < entry < stop)'
                    )
            else:
                raise ToolError('stop cannot equal entry')

            ts0 = arguments.get('from_ts', _BAR_TS_PLACEHOLDER)
            ts1 = arguments.get('to_ts', _BAR_TS_PLACEHOLDER)
            metadata['points'] = [
                {'timestamp': ts0, 'value': entry},
                {'timestamp': ts0, 'value': stop},
                {'timestamp': ts1, 'value': target},
            ]
            extra_targets = arguments.get('additional_targets')
            if extra_targets:
                if not isinstance(extra_targets, list):
                    raise ToolError('additional_targets must be a list of numbers')
                extend_data = metadata.get('extend_data') or {}
                extend_data['tps'] = [float(t) for t in extra_targets]
                metadata['extend_data'] = extend_data
            price_level = entry
        elif any(x is not None for x in (entry, stop, target)):
            raise ToolError(
                'position drawing requires all of: entry, stop, target'
            )

    elif drawing_type == 'trendline':
        from_price = arguments.get('from_price')
        to_price = arguments.get('to_price')
        pts_raw = arguments.get('points')
        if pts_raw is not None:
            # points: [[ts1, price1], [ts2, price2]]
            if (not isinstance(pts_raw, list) or len(pts_raw) < 2
                    or not all(isinstance(p, (list, tuple)) and len(p) == 2
                               for p in pts_raw)):
                raise ToolError(
                    'trendline points must be [[ts1, price1], [ts2, price2]]'
                )
            metadata['points'] = [
                {'timestamp': int(pts_raw[0][0]), 'value': float(pts_raw[0][1])},
                {'timestamp': int(pts_raw[1][0]), 'value': float(pts_raw[1][1])},
            ]
            price_level = float(pts_raw[0][1])
        elif from_price is not None and to_price is not None:
            ts0 = arguments.get('from_ts', _BAR_TS_PLACEHOLDER)
            ts1 = arguments.get('to_ts', _BAR_TS_PLACEHOLDER)
            metadata['points'] = [
                {'timestamp': ts0, 'value': float(from_price)},
                {'timestamp': ts1, 'value': float(to_price)},
            ]
            price_level = float(from_price)
        if 'kline_name' not in metadata:
            metadata['kline_name'] = 'segment'

    elif drawing_type == 'fibonacci':
        high_price = arguments.get('high_price')
        low_price = arguments.get('low_price')
        if high_price is not None and low_price is not None:
            high_price = float(high_price)
            low_price = float(low_price)
            ts0 = arguments.get('from_ts', _BAR_TS_PLACEHOLDER)
            ts1 = arguments.get('to_ts', _BAR_TS_PLACEHOLDER)
            metadata['points'] = [
                {'timestamp': ts0, 'value': high_price},
                {'timestamp': ts1, 'value': low_price},
            ]
            price_level = high_price
            price_level_upper = low_price
        if 'kline_name' not in metadata:
            metadata['kline_name'] = 'fibonacciRetrace'

    elif drawing_type == 'annotation':
        if arguments.get('text'):
            label = arguments['text'][:MAX_LABEL_LENGTH]
        if arguments.get('price') is not None:
            price_level = float(arguments['price'])
        ts = arguments.get('timestamp', _BAR_TS_PLACEHOLDER)
        if 'points' not in metadata and price_level is not None:
            metadata['points'] = [{'timestamp': ts, 'value': price_level}]
        if 'kline_name' not in metadata:
            metadata['kline_name'] = 'simpleAnnotation'

    elif drawing_type == 'scenario':
        name = arguments.get('name') or label
        if name:
            label = name[:MAX_LABEL_LENGTH]
        scenario_thesis = arguments.get('thesis')
        if scenario_thesis:
            metadata['thesis'] = scenario_thesis
        scen_dir = arguments.get('direction')
        if scen_dir:
            metadata['direction'] = scen_dir
        # Mark as v2 drawing-group parent
        metadata['is_group'] = True

    # ── Spot / remaining types use raw fields as-is ──────────────────────────

    # ── Persist ──────────────────────────────────────────────────────────────
    db = get_content_db()
    if not db:
        raise ToolError('Drawing database not available')

    drawing_id = db.save_drawing(
        asset=asset,
        timeframe=timeframe,
        drawing_type=drawing_type,
        price_level=float(price_level) if price_level is not None else None,
        price_level_upper=float(price_level_upper) if price_level_upper is not None else None,
        label=label,
        color=color,
        line_style=line_style,
        line_width=line_width,
        metadata=metadata if metadata else None,
        scenario_id=int(scenario_id) if scenario_id is not None else None,
        user_id=user_id,
    )

    # Re-fetch to return the full saved row
    saved = db.get_drawing_by_id(drawing_id)
    if not saved:
        raise ToolError(f'Drawing saved (id={drawing_id}) but could not be re-fetched')

    _publish_sync(user_id, asset)

    result = dict(saved)
    if inferred_direction:
        result['inferred_direction'] = inferred_direction
    return result


# =============================================================================
# drawing_update
# =============================================================================
@mcp_tool(
    'drawing_update',
    'Patch one or more fields on an existing drawing. '
    'Updatable fields: price_level, price_level_upper, label, color, line_style, '
    'line_width, metadata (full replace) or metadata_patch (shallow merge). '
    'SIDE EFFECTS: 1) row updated in drawing DB (version counter incremented); '
    '2) Redis SSE drawing-sync event fired — all the user\'s open browser tabs '
    'receive the update immediately. '
    'Use if_version (pass the version from the prior get/create response) to detect '
    'mid-air collisions — returns ToolError 409 with the current version on mismatch. '
    'EXAMPLE: {"drawing_id": 42, "price_level": 68500, "label": "Key resistance"}.',
    {
        'type': 'object',
        'properties': {
            'drawing_id': {
                'type': 'integer',
                'description': 'Drawing ID to update (required)',
            },
            'price_level': {
                'type': 'number',
                'description': 'New primary price level',
            },
            'price_level_upper': {
                'type': 'number',
                'description': 'New upper price level (zones)',
            },
            'label': {
                'type': 'string',
                'description': 'New label text (max 500 chars)',
            },
            'color': {
                'type': 'string',
                'description': 'New hex colour: #rgb / #rrggbb / #rrggbbaa',
            },
            'line_style': {
                'type': 'string',
                'enum': ['solid', 'dashed', 'dotted'],
                'description': 'New line style',
            },
            'line_width': {
                'type': 'integer',
                'description': 'New line width 1–10',
            },
            'metadata': {
                'type': 'object',
                'description': 'Replace the entire metadata dict',
            },
            'metadata_patch': {
                'type': 'object',
                'description': 'Shallow-merge into existing metadata (keys not listed are preserved)',
            },
            'if_version': {
                'type': 'integer',
                'description': (
                    'If provided, update only when the stored version equals this '
                    'value; returns a 409 ToolError with current version on mismatch'
                ),
            },
        },
        'required': ['drawing_id'],
    },
    min_tier='free',
)
def drawing_update(arguments):
    from carrac.content_db import get_content_db

    user_id = get_mcp_user_id()
    drawing_id = arguments.get('drawing_id')
    if drawing_id is None:
        raise ToolError('drawing_id is required')
    try:
        drawing_id = int(drawing_id)
    except (TypeError, ValueError):
        raise ToolError('drawing_id must be an integer')

    db = get_content_db()
    if not db:
        raise ToolError('Drawing database not available')

    # Verify existence + ownership before mutating
    existing = db.get_drawing_by_id(drawing_id)
    if not existing:
        raise ToolError(f'Drawing {drawing_id} not found')
    if not existing or existing.get('user_id') != user_id:
        raise ToolError(f'Drawing {drawing_id} not found')  # don't leak existence

    # ── Build updates dict ───────────────────────────────────────────────────
    updates = {}

    if 'price_level' in arguments and arguments['price_level'] is not None:
        pl = float(arguments['price_level'])
        if pl < 0:
            raise ToolError('price_level cannot be negative')
        updates['price_level'] = pl

    if 'price_level_upper' in arguments and arguments['price_level_upper'] is not None:
        plu = float(arguments['price_level_upper'])
        if plu < 0:
            raise ToolError('price_level_upper cannot be negative')
        updates['price_level_upper'] = plu

    if 'label' in arguments:
        lbl = (arguments['label'] or '').strip()
        if lbl and len(lbl) > MAX_LABEL_LENGTH:
            raise ToolError(f'label too long (max {MAX_LABEL_LENGTH} chars)')
        updates['label'] = lbl or None

    if 'color' in arguments:
        ok, err = _validate_color(arguments['color'])
        if not ok:
            raise ToolError(err)
        updates['color'] = arguments['color']

    if 'line_style' in arguments:
        ok, err = _validate_line_style(arguments['line_style'])
        if not ok:
            raise ToolError(err)
        updates['line_style'] = arguments['line_style']

    if 'line_width' in arguments:
        ok, err = _validate_line_width(arguments['line_width'])
        if not ok:
            raise ToolError(err)
        updates['line_width'] = int(arguments['line_width'])

    # metadata (full replace) vs metadata_patch (shallow merge)
    meta_replace = arguments.get('metadata')
    meta_patch = arguments.get('metadata_patch')
    if meta_replace is not None and not isinstance(meta_replace, dict):
        raise ToolError('metadata must be an object')
    if meta_patch is not None and not isinstance(meta_patch, dict):
        raise ToolError('metadata_patch must be an object')

    if meta_replace is not None:
        updates['metadata'] = meta_replace
    elif meta_patch is not None:
        # Shallow merge: start from existing metadata
        existing_meta = existing.get('metadata') or {}
        if isinstance(existing_meta, str):
            import json as _json
            try:
                existing_meta = _json.loads(existing_meta)
            except Exception:
                existing_meta = {}
        merged = dict(existing_meta)
        merged.update(meta_patch)
        updates['metadata'] = merged

    if not updates:
        raise ToolError('No updatable fields provided')

    # ── Call the DB layer ────────────────────────────────────────────────────
    if_version = arguments.get('if_version')
    status, new_version = db.drawing_update(
        drawing_id,
        user_id=user_id,
        expected_version=if_version,
        **updates,
    )

    if status == db.UPDATE_CONFLICT:
        raise ToolError(
            f'Drawing was modified by another session, current version: {new_version}'
        )
    if status == db.UPDATE_NOT_FOUND:
        raise ToolError(f'Drawing {drawing_id} not found or not owned by caller')

    # Return the updated drawing
    updated = db.get_drawing_by_id(drawing_id)
    if not updated:
        raise ToolError(f'Drawing updated (id={drawing_id}) but could not be re-fetched')

    _publish_sync(user_id, updated.get('asset', ''))
    return dict(updated)


# =============================================================================
# drawing_link_to_play
# =============================================================================
@mcp_tool(
    'drawing_link_to_play',
    'Link or unlink chart drawings to a trading play (updates the play\'s '
    'linked_drawing_ids list). '
    'Pass drawing_id for a single drawing or drawing_ids for up to 20 at once. '
    'SIDE EFFECTS: play row updated in DB (linked_drawing_ids column). '
    'NO SSE event fired — browser tabs do not update in real-time (use play_list to confirm). '
    'Ownership of both the play and every drawing is verified; foreign IDs raise ToolError. '
    'PREREQUISITES: play and drawings must already exist (play_create + drawing_create first). '
    'EXAMPLE: {"play_id": 5, "drawing_ids": [101, 102], "action": "link"}.',
    {
        'type': 'object',
        'properties': {
            'play_id': {
                'type': 'integer',
                'description': 'Play ID (required)',
            },
            'drawing_id': {
                'type': 'integer',
                'description': 'Single drawing ID to link/unlink',
            },
            'drawing_ids': {
                'type': 'array',
                'items': {'type': 'integer'},
                'description': 'Bulk drawing IDs to link/unlink (max 20)',
            },
            'action': {
                'type': 'string',
                'enum': ['link', 'unlink'],
                'description': 'Whether to link or unlink (default: link)',
            },
        },
        'required': ['play_id'],
    },
    min_tier='free',
)
def drawing_link_to_play(arguments):
    from carrac.content_db import get_content_db

    user_id = get_mcp_user_id()
    play_id = arguments.get('play_id')
    if play_id is None:
        raise ToolError('play_id is required')
    try:
        play_id = int(play_id)
    except (TypeError, ValueError):
        raise ToolError('play_id must be an integer')

    action = arguments.get('action', 'link')
    if action not in ('link', 'unlink'):
        raise ToolError('action must be "link" or "unlink"')

    raw_id = arguments.get('drawing_id')
    raw_ids = arguments.get('drawing_ids') or []

    ids = []
    if raw_id is not None:
        ids.append(int(raw_id))
    if raw_ids:
        if not isinstance(raw_ids, list):
            raise ToolError('drawing_ids must be an array of integers')
        try:
            ids.extend(int(i) for i in raw_ids)
        except (TypeError, ValueError):
            raise ToolError('drawing_ids must contain integers')
    # De-dupe
    seen = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]

    if not ids:
        raise ToolError('Provide drawing_id or drawing_ids')
    if len(ids) > 20:
        raise ToolError('Too many drawing_ids; max 20 per call')

    # Verify drawing ownership
    db = get_content_db()
    if not db:
        raise ToolError('Drawing database not available')

    not_found = []
    for did in ids:
        d = db.get_drawing_by_id(did)
        if not d or d.get('user_id') != user_id:
            not_found.append(did)
    if not_found:
        raise ToolError(
            f'Drawing(s) not found or not owned by caller: {not_found}'
        )

    # Call the play model functions one by one (they handle de-dup + ownership)
    from carrac.plays.models import link_drawing, unlink_drawing, get_play

    # Verify play ownership first
    play = get_play(play_id, user_id)
    if not play:
        raise ToolError(f'Play {play_id} not found or not owned by caller')

    fn = link_drawing if action == 'link' else unlink_drawing
    result_play = None
    for did in ids:
        result_play = fn(play_id, user_id, did)
        if result_play is None:
            raise ToolError(f'Failed to {action} drawing {did} to play {play_id}')

    return {
        'action': action,
        'play_id': play_id,
        'drawing_ids': ids,
        'linked_drawing_ids': (result_play or {}).get('linked_drawing_ids', []),
    }


# =============================================================================
# drawing_get_scenario_children
# =============================================================================
@mcp_tool(
    'drawing_get_scenario_children',
    'Return all child drawings that belong to a scenario (drawing group). '
    'Caller must own the scenario; ownership is verified before returning data. '
    'Key fields per child: id, drawing_type, price_level, label, color, metadata. '
    'Use after drawing_create_group to confirm all children were saved, '
    'or to inspect/edit children before calling drawing_update on individual ones.',
    {
        'type': 'object',
        'properties': {
            'scenario_id': {
                'type': 'integer',
                'description': 'ID of the parent scenario drawing',
            },
        },
        'required': ['scenario_id'],
    },
    min_tier='free',
)
def drawing_get_scenario_children(arguments):
    from carrac.content_db import get_content_db

    user_id = get_mcp_user_id()
    scenario_id = arguments.get('scenario_id')
    if scenario_id is None:
        raise ToolError('scenario_id is required')
    try:
        scenario_id = int(scenario_id)
    except (TypeError, ValueError):
        raise ToolError('scenario_id must be an integer')

    db = get_content_db()
    if not db:
        raise ToolError('Drawing database not available')

    # Verify scenario exists and is owned by caller
    parent = db.get_drawing_by_id(scenario_id)
    if not parent:
        raise ToolError(f'Scenario {scenario_id} not found')
    if not parent or parent.get('user_id') != user_id:
        raise ToolError(f'Scenario {scenario_id} not found')  # don't leak existence
    if parent.get('drawing_type') != 'scenario':
        raise ToolError(f'Drawing {scenario_id} is not a scenario (type={parent.get("drawing_type")})')

    children = db.drawing_get_scenario_children(scenario_id)
    return {
        'scenario_id': scenario_id,
        'count': len(children),
        'children': children,
    }


# =============================================================================
# drawing_create_group (atomic) — formerly drawing_create_scenario.
# The legacy name is deprecated, not removed: it dispatches here via
# registry.LEGACY_TOOL_ALIASES so every published/external caller (incl. the
# Strategy Lab) keeps working.
# =============================================================================
@mcp_tool(
    'drawing_create_group',
    'Atomically create a drawing group (parent + linked children, formerly '
    'called a "scenario") in one call. '
    'Parent + all children are saved in a single DB transaction — all succeed or none are saved. '
    'Each child inherits asset and timeframe from the parent; max 50 children per call. '
    'SIDE EFFECTS: 1) parent scenario row + N child rows inserted in drawing DB; '
    '2) Redis SSE drawing-sync event fired once — all the user\'s open browser tabs '
    'receive all new drawings immediately. '
    'PREREQUISITES: none beyond valid asset + timeframe + name. '
    'Returns: scenario_id, full parent drawing, list of created child drawings, child_count.',
    {
        'type': 'object',
        'properties': {
            'asset': {
                'type': 'string',
                'description': 'Asset symbol for the scenario and all children',
            },
            'timeframe': {
                'type': 'string',
                'description': 'Timeframe for the scenario and all children',
            },
            'name': {
                'type': 'string',
                'description': 'Scenario name (required, max 500 chars)',
            },
            'thesis': {
                'type': 'string',
                'description': 'Optional thesis text stored in metadata',
            },
            'direction': {
                'type': 'string',
                'enum': ['bullish', 'bearish', 'neutral'],
                'description': 'Directional bias',
            },
            'color': {
                'type': 'string',
                'description': 'Hex colour for the parent scenario (default #3fb950)',
            },
            'children': {
                'type': 'array',
                'maxItems': 50,
                'description': 'Child drawings to create under the scenario',
                'items': {
                    'type': 'object',
                    'properties': {
                        'drawing_type': {
                            'type': 'string',
                            'description': 'Child drawing type (required)',
                        },
                        'price_level': {
                            'type': 'number',
                            'description': 'Primary price level',
                        },
                        'price_level_upper': {
                            'type': 'number',
                            'description': 'Upper price level (zones)',
                        },
                        'label': {
                            'type': 'string',
                            'description': 'Child label (max 500 chars)',
                        },
                        'color': {
                            'type': 'string',
                            'description': 'Hex colour for this child',
                        },
                        'line_style': {
                            'type': 'string',
                            'enum': ['solid', 'dashed', 'dotted'],
                        },
                        'line_width': {
                            'type': 'integer',
                            'description': 'Line width 1–10',
                        },
                        'metadata': {
                            'type': 'object',
                            'description': 'Raw metadata (kline_name, points, etc.)',
                        },
                    },
                    'required': ['drawing_type'],
                },
            },
        },
        'required': ['asset', 'timeframe', 'name'],
    },
    min_tier='free',
)
def drawing_create_group(arguments):
    from carrac.content_db import get_content_db
    from carrac.mcp.runtime import get_mcp_caller_tier
    from carrac.auth.decorators import get_tier_limits_for_user

    user_id = get_mcp_user_id()

    asset = (arguments.get('asset') or '').strip().upper()
    timeframe = (arguments.get('timeframe') or '').strip().upper()
    name = (arguments.get('name') or '').strip()

    if not asset:
        raise ToolError('asset is required')
    if len(asset) > 20:
        raise ToolError('asset name too long (max 20 chars)')
    if not timeframe:
        raise ToolError('timeframe is required')
    if not name:
        raise ToolError('name is required')
    if len(name) > MAX_LABEL_LENGTH:
        raise ToolError(f'name too long (max {MAX_LABEL_LENGTH} chars)')

    # Scenario quota check (mirrors FastAPI drawings router)
    tier = get_mcp_caller_tier()
    limits = get_tier_limits_for_user(user_id, tier)
    max_scenarios = limits.get('max_scenarios', 0)
    db_check = get_content_db()
    if db_check is None:
        # fail closed: an unavailable DB must not skip quota enforcement
        raise ToolError('Drawing storage unavailable — try again shortly.')
    current_scenarios = db_check.get_active_scenario_count(user_id)
    if current_scenarios >= max_scenarios:
        raise ToolError(
            f'Scenario limit reached ({current_scenarios}/{max_scenarios}). Upgrade for more.'
        )

    color = arguments.get('color') or '#3fb950'
    ok, err = _validate_color(color)
    if not ok:
        raise ToolError(err)

    children_raw = arguments.get('children') or []
    if not isinstance(children_raw, list):
        raise ToolError('children must be an array')
    if len(children_raw) > 50:
        raise ToolError('Maximum 50 children per call')

    # Validate children before touching the DB
    valid_children = []
    for i, child in enumerate(children_raw):
        if not isinstance(child, dict):
            raise ToolError(f'children[{i}] must be an object')
        child_type = (child.get('drawing_type') or '').strip().lower()
        if not child_type or child_type not in ALLOWED_DRAWING_TYPES:
            raise ToolError(
                f'children[{i}].drawing_type must be one of: {sorted(ALLOWED_DRAWING_TYPES)}'
            )
        child_color = child.get('color')
        if child_color:
            ok, err = _validate_color(child_color)
            if not ok:
                raise ToolError(f'children[{i}].color: {err}')
        child_style = child.get('line_style')
        if child_style:
            ok, err = _validate_line_style(child_style)
            if not ok:
                raise ToolError(f'children[{i}].line_style: {err}')
        child_width = child.get('line_width')
        if child_width is not None:
            ok, err = _validate_line_width(child_width)
            if not ok:
                raise ToolError(f'children[{i}].line_width: {err}')
        child_label = (child.get('label') or '').strip() or None
        if child_label and len(child_label) > MAX_LABEL_LENGTH:
            raise ToolError(f'children[{i}].label too long (max {MAX_LABEL_LENGTH} chars)')

        valid_children.append({
            'drawing_type': child_type,
            'price_level': float(child['price_level']) if child.get('price_level') is not None else None,
            'price_level_upper': float(child['price_level_upper']) if child.get('price_level_upper') is not None else None,
            'label': child_label,
            'color': child_color or color,
            'line_style': child_style or 'solid',
            'line_width': int(child_width) if child_width is not None else 2,
            'metadata': dict(child.get('metadata') or {}),
        })

    db = get_content_db()
    if not db:
        raise ToolError('Drawing database not available')

    # 1. Create parent scenario
    parent_metadata = {
        'is_group': True,
        'name': name,
    }
    if arguments.get('thesis'):
        parent_metadata['thesis'] = arguments['thesis']
    if arguments.get('direction'):
        parent_metadata['direction'] = arguments['direction']

    scenario_id = db.save_drawing(
        asset=asset,
        timeframe=timeframe,
        drawing_type='scenario',
        price_level=None,
        price_level_upper=None,
        label=name,
        color=color,
        line_style='solid',
        line_width=2,
        metadata=parent_metadata,
        scenario_id=None,
        user_id=user_id,
    )

    if not scenario_id:
        raise ToolError('Failed to create parent scenario')

    # 2. Atomically replace children (delete any existing + insert new)
    db.replace_scenario_children(scenario_id, valid_children)

    # 3. Publish sync event once
    _publish_sync(user_id, asset)

    # 4. Return full parent + children
    parent = db.get_drawing_by_id(scenario_id)
    children_out = db.drawing_get_scenario_children(scenario_id)

    return {
        'scenario_id': scenario_id,
        'parent': dict(parent) if parent else {'id': scenario_id},
        'children': children_out,
        'child_count': len(children_out),
    }
