"""MCP tools: Cross-system bridges.

Wire alerts → plays, drawings → alerts, formula → play composition.

Tools:
  - alert_create_from_drawing
  - play_create_from_formula_alert
  - play_draft_from_triggered_alerts

list_formula_alerts is a plain helper here, no longer a
standalone MCP tool — absorbed into alert_manage (action=list, family=formula).
"""

import json
import logging

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id

logger = logging.getLogger('carrac.mcp.tools.bridges')


# ── alert_create_from_drawing ───────────────────────────────────────

def _extract_trendline_points(drawing):
    """Pull p1/p2 (timestamp_ms, price) from a drawing's metadata if present."""
    md = drawing.get('metadata') or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    points = md.get('points') or []
    if isinstance(points, list) and len(points) >= 2:
        try:
            p1 = points[0]
            p2 = points[1]
            return (
                float(p1.get('ts') or p1.get('timestamp')),
                float(p1.get('val') or p1.get('price')),
                float(p2.get('ts') or p2.get('timestamp')),
                float(p2.get('val') or p2.get('price')),
            )
        except (TypeError, ValueError):
            return None
    return None


@mcp_tool(
    'alert_create_from_drawing',
    'Create push-notification alert(s) tied to an existing chart drawing. '
    'horizontal_line/level → two price-cross alerts (above + below the level). '
    'zone → two boundary alerts (above upper + below lower). '
    'trendline → one trendline_break alert that extrapolates the drawn line forward. '
    'SIDE EFFECTS: alert rows written to the notifications DB; alert engine will '
    'fire push/ntfy notifications when price crosses the level. '
    'PREREQUISITES: the drawing must already exist (use drawing_create first) — '
    'checked first, raises ToolError "Drawing <id> not found" if not. '
    'PREREQUISITES: caller must have a configured notification channel (ntfy or Telegram); '
    'checked after the drawing lookup, raises ToolError "No notification channel '
    'configured" (with a pointer to notification_configure) if none is set up. '
    'Unsupported drawing types (annotation, scenario, fibonacci, position) raise ToolError. '
    'Returns the created alert object(s) with their IDs.',
    {
        'type': 'object',
        'properties': {
            'drawing_id': {'type': 'integer', 'description': 'Drawing ID to subscribe to'},
            'note': {'type': 'string', 'description': 'Freeform note'},
            'frequency': {
                'type': 'string',
                'enum': ['once', 'once_per_bar'],
                'description': 'Trigger mode (default once_per_bar)',
            },
        },
        'required': ['drawing_id'],
    },
    min_tier='member',
)
def alert_create_from_drawing(arguments):
    import push_notifications
    from carrac.content_db import get_content_db

    drawing_id = arguments.get('drawing_id')
    if not drawing_id:
        raise ToolError('drawing_id is required')

    user_id = str(get_mcp_user_id())

    db = get_content_db()
    # Scope the lookup to the caller. get_drawing_by_id() defaults
    # to NO owner filter (see its docstring), so an unscoped call would let any
    # member-tier key walk the autoincrement id space and read back another
    # user's asset and price levels in the alerts it created. The miss message is deliberately shared
    # with the absent-id case so this is not an existence oracle either.
    drawing = db.get_drawing_by_id(int(drawing_id), user_id=int(user_id))
    if not drawing:
        raise ToolError(f'Drawing {drawing_id} not found')

    # Validate the referenced drawing BEFORE the notification-
    # channel precondition. Read-only lookup, no side effect either way, but
    # a bad drawing_id must report as a missing drawing, not a channel error
    # that has nothing to do with the actual problem.
    if not push_notifications.user_has_notification_channel(user_id):
        raise ToolError(
            'No notification channel configured. Enable ntfy / Discord / '
            'Telegram / web push in Settings first, or call '
            'notification_configure(action="set") in this session.'
        )

    asset = (drawing.get('asset') or '').upper().strip()
    dtype = (drawing.get('drawing_type') or '').lower()
    note = (arguments.get('note') or f'Auto from drawing #{drawing_id}')[:200]
    frequency = arguments.get('frequency') or 'once_per_bar'

    created = []

    if dtype in ('horizontal_line', 'level', 'support', 'resistance'):
        level = drawing.get('price_level')
        if level is None:
            raise ToolError(f'Drawing {drawing_id} has no price_level')
        # Create both above + below for two-sided coverage; user can disable one.
        for cond in ('above', 'below'):
            alert = push_notifications.create_alert(
                user_id=user_id, symbol=asset,
                condition=cond, price=float(level),
                note=note, frequency=frequency,
                drawing_id=drawing_id,
            )
            created.append(alert)

    elif dtype == 'zone':
        upper = drawing.get('price_level_upper')
        lower = drawing.get('price_level')
        if upper is None or lower is None:
            raise ToolError(f'Zone {drawing_id} missing upper/lower bounds')
        for level, cond, label in (
            (float(upper), 'above', 'top break'),
            (float(lower), 'below', 'bottom break'),
        ):
            alert = push_notifications.create_alert(
                user_id=user_id, symbol=asset,
                condition=cond, price=level,
                note=f'{note} ({label})',
                frequency=frequency,
                drawing_id=drawing_id,
            )
            created.append(alert)

    elif dtype == 'trendline':
        pts = _extract_trendline_points(drawing)
        if not pts:
            raise ToolError(
                f'Trendline {drawing_id} missing point coordinates in metadata'
            )
        p1_ts, p1_val, p2_ts, p2_val = pts
        alert = push_notifications.create_trendline_alert(
            user_id=user_id, symbol=asset, drawing_id=str(drawing_id),
            p1_ts=p1_ts, p1_val=p1_val, p2_ts=p2_ts, p2_val=p2_val,
            note=note, frequency=frequency,
        )
        created.append(alert)

    else:
        raise ToolError(
            f'Drawing type {dtype!r} is not alertable. '
            f'Supported: horizontal_line, level, zone, trendline.'
        )

    return {
        'drawing_id': drawing_id,
        'asset': asset,
        'drawing_type': dtype,
        'alerts_created': created,
        'count': len(created),
    }


# ── list_formula_alerts ────────────────────────────────────────
#
# No longer a standalone MCP tool — absorbed into alert_manage
# (action=list, family=formula), imported from there. The legacy
# 'list_formula_alerts' tool name still dispatches via
# registry.LEGACY_TOOL_ALIASES + LEGACY_ARG_DEFAULTS.

def list_formula_alerts(arguments):
    import push_notifications

    user_id = str(get_mcp_user_id())
    active_only = arguments.get('active_only', True)
    all_alerts = push_notifications.get_user_alerts(user_id, active_only=active_only)
    formula_alerts = [
        {
            'id': a['id'],
            'symbol': a.get('symbol'),
            'expression': a.get('expression'),
            'timeframe': a.get('timeframe', '1h'),
            'frequency': a.get('frequency'),
            'note': a.get('note'),
            'triggered': a.get('triggered'),
            'triggered_at': a.get('triggered_at'),
            'active': a.get('active'),
            'created_at': a.get('created_at'),
        }
        for a in all_alerts if a.get('alert_type') == 'formula'
    ]
    return {'count': len(formula_alerts), 'alerts': formula_alerts}


# ── play_create_from_formula_alert ────────────────────────────────────────

@mcp_tool(
    'play_create_from_formula_alert',
    'Promote an existing formula alert into a new watching-status play. '
    'Reads the expression, symbol, and timeframe from the alert and creates a '
    'play with a single formula_expression condition — no need to re-type the formula. '
    'SIDE EFFECTS: new play row inserted in plays DB (status=watching); '
    'condition monitor will start evaluating the expression on its next cycle. '
    'PREREQUISITES: alert_id must be a formula alert owned by the caller '
    '(use list_formula_alerts to find IDs). '
    'Returns: {play: <full play object>, source_alert_id, status: "created"}.',
    {
        'type': 'object',
        'properties': {
            'alert_id': {'type': 'integer', 'description': 'Formula alert ID'},
            'title': {'type': 'string', 'description': 'Play title (default: derived from formula)'},
            'thesis': {'type': 'string', 'description': 'Optional thesis text'},
            'direction': {
                'type': 'string',
                'enum': ['long', 'short', 'neutral', 'conditional'],
                'description': 'Default long',
            },
        },
        'required': ['alert_id'],
    },
    min_tier='member',
)
def play_create_from_formula_alert(arguments):
    import push_notifications
    from carrac.plays.models import play_create as _create_play, count_active_plays
    from carrac.mcp.runtime import get_mcp_caller_tier
    from carrac.auth.decorators import get_tier_limits_for_user

    alert_id = arguments.get('alert_id')
    if not alert_id:
        raise ToolError('alert_id is required')

    user_id = get_mcp_user_id()

    tier = get_mcp_caller_tier()
    limits = get_tier_limits_for_user(user_id, tier)
    max_plays = limits.get('max_plays', 0)
    current = count_active_plays(user_id)
    if current >= max_plays:
        raise ToolError(
            f'Play limit reached ({current}/{max_plays}). Upgrade your plan.'
        )
    user_alerts = push_notifications.get_user_alerts(str(user_id), active_only=False)
    target = next(
        (a for a in user_alerts
         if a['id'] == alert_id and a.get('alert_type') == 'formula'),
        None,
    )
    if not target:
        raise ToolError(f'Formula alert {alert_id} not found for caller')

    expression = target.get('expression', '')
    if not expression:
        raise ToolError(f'Alert {alert_id} has no expression')

    symbol = (target.get('symbol') or '').upper() or 'BTC'
    timeframe = target.get('timeframe', '1h')
    title = arguments.get('title') or f'Formula play: {expression[:40]}'
    thesis = arguments.get('thesis') or (
        f'Auto-promoted from formula alert #{alert_id}. '
        f'Triggers when: {expression}'
    )
    direction = arguments.get('direction') or 'long'

    conditions = {
        'logic': 'all',
        'rules': [{
            'type': 'formula_expression',
            'params': {'expression': expression, 'timeframe': timeframe},
            'label': f'Alert #{alert_id} formula',
        }],
    }

    play = _create_play(
        user_id=user_id,
        title=title,
        thesis=thesis,
        type='directional',
        approach='single',
        direction=direction,
        primary_asset=symbol,
        conditions=conditions,
        timeframe='swing',
        metadata={'source_alert_id': alert_id, 'source': 'formula_alert'},
    )

    return {'play': play, 'source_alert_id': alert_id, 'status': 'created'}


# ── play_draft_from_triggered_alerts ──────────────────────────────────

_DRAFT_LIMIT = 10


def _alert_to_draft(alert):
    """Build a play DRAFT (not persisted) from a triggered alert."""
    atype = alert.get('alert_type')
    symbol = (alert.get('symbol') or '').upper() or 'BTC'
    rules = []
    title = f'Draft from {atype} alert #{alert.get("id")}'

    if atype == 'price':
        cond = alert.get('condition')
        price = alert.get('price')
        if cond and price is not None:
            rules.append({
                'type': 'price_level',
                'params': {'level': price, 'relation': cond},
                'label': f'Price {cond} {price}',
            })
            title = f'{symbol} reacts to {cond} {price}'

    elif atype == 'formula':
        expr = alert.get('expression', '')
        if expr:
            rules.append({
                'type': 'formula_expression',
                'params': {'expression': expr, 'timeframe': alert.get('timeframe', '1h')},
                'label': 'Formula trigger',
            })
            title = f'{symbol} formula play: {expr[:30]}'

    elif atype == 'indicator':
        indicator = alert.get('indicator')
        target_state = alert.get('target_state')
        if indicator and target_state:
            rules.append({
                'type': 'indicator_state',
                'params': {
                    'indicator': indicator,
                    'timeframe': alert.get('timeframe', '1h'),
                    'state': target_state,
                },
                'label': f'{indicator} → {target_state}',
            })
            title = f'{symbol} on {indicator} {target_state}'

    elif atype == 'derivatives_regime':
        to_regime = alert.get('to_regime')
        if to_regime:
            rules.append({
                'type': 'derivatives_regime',
                'params': {'regime': to_regime, 'condition': 'matches'},
                'label': f'Regime: {to_regime}',
            })
            title = f'Regime play: → {to_regime}'

    return {
        'source_alert_id': alert.get('id'),
        'source_alert_type': atype,
        'title': title,
        'primary_asset': symbol,
        'direction': 'long',
        'conditions': {'logic': 'all', 'rules': rules},
        'has_supported_rules': len(rules) > 0,
    }


@mcp_tool(
    'play_draft_from_triggered_alerts',
    'Build play DRAFTS from recently triggered alerts. Returns the drafts '
    'so the caller can review before persisting (use play_create to save). '
    'Filters: only triggered + active alerts, max 10 drafts. Skips alert '
    'types with no obvious play mapping.',
    {
        'type': 'object',
        'properties': {
            'alert_ids': {
                'type': 'string',
                'description': 'Optional comma-separated alert IDs to filter (default: all triggered)',
            },
            'limit': {
                'type': 'integer',
                'description': f'Max drafts to return (default {_DRAFT_LIMIT})',
            },
        },
    },
    min_tier='premium',
)
def play_draft_from_triggered_alerts(arguments):
    import push_notifications

    user_id = str(get_mcp_user_id())
    limit = max(1, min(_DRAFT_LIMIT, int(arguments.get('limit') or _DRAFT_LIMIT)))

    id_filter = None
    if arguments.get('alert_ids'):
        try:
            id_filter = {int(x.strip()) for x in str(arguments['alert_ids']).split(',') if x.strip()}
        except ValueError:
            raise ToolError('alert_ids must be comma-separated integers')

    user_alerts = push_notifications.get_user_alerts(user_id, active_only=False)
    triggered = [a for a in user_alerts if a.get('triggered')]
    if id_filter is not None:
        triggered = [a for a in triggered if a.get('id') in id_filter]

    # Most-recently-triggered first.
    triggered.sort(
        key=lambda a: (a.get('triggered_at') or ''), reverse=True,
    )

    drafts = []
    for a in triggered[:limit]:
        draft = _alert_to_draft(a)
        if draft['has_supported_rules']:
            drafts.append(draft)

    return {
        'count': len(drafts),
        'drafts': drafts,
        'message': (
            'Drafts are NOT persisted. Use play_create with the conditions '
            'block to save any draft you want to keep.'
        ),
    }
