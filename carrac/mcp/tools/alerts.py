"""MCP tools: Alerts.

alert_manage covers price, indicator, formula, AND the
quant-engine alert types (create/list also reachable through the legacy
create_quant_alert / list_quant_alerts / list_regime_alerts /
list_drawing_alerts / list_formula_alerts names, which exist as plain helper
functions here and in bridges.py/quant.py and are not registered as MCP tools).
The `list` action returns ALL alert families by default; pass family= to
filter. The `delete` action supports bulk + dry_run for ANY alert type.

Quant-engine alert types (z-score / regime / chain-strength / anomaly-cluster
/ conditional) are implemented in create_quant_alert in tools/quant.py, which
remains quant_engine-feature-gated even when reached through alert_manage —
see _require_quant_engine() below.

Alert types available here (create):
  - price:     symbol + condition (above/below) + price
  - indicator: indicator + target_state (uses INDICATOR_ALERT_CONDITIONS)
                Supported indicators include bb_position (states: inside /
                above_upper / below_lower) and bb_squeeze (states: squeezed /
                normal / expanded). For bb_position alerts, pass optional
                bb_params {period, std_dev, offset} to snapshot custom BB
                parameters at creation time.
  - formula:   expression (boolean DSL), fires when the expression flips true

Alert families (list / delete apply to all):
  - drawing:  trendline_break, horizontal_line-derived (created via alert_create_from_drawing)
  - quant:    zscore, regime_transition, chain_strength_breach,
              anomaly_cluster_active, conditional_query_match
  - regime:   derivatives_regime (created via alert_create_regime_watch)
"""

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id

_BULK_CAP = 50

# Alert types that belong to each named family — used in list_*_alerts tools.
_QUANT_ALERT_TYPES = frozenset({
    'zscore', 'regime_transition', 'chain_strength_breach',
    'anomaly_cluster_active', 'conditional_query_match',
})
_DRAWING_ALERT_TYPES = frozenset({'trendline_break'})
_REGIME_ALERT_TYPES = frozenset({'derivatives_regime'})

# alert_type values handled by the quant create path (legacy create_quant_alert).
# That path (and list_quant_alerts/list_regime_alerts) requires the
# quant_engine TIER_LIMITS knob, not just plain min_tier='member' — this
# tool's own registry entry is min_tier='member' with no feature knob, so
# these branches must enforce quant_engine themselves or a plain member
# would silently gain quant-engine access through alert_manage.
_QUANT_CREATE_TYPES = frozenset({
    'zscore', 'regime', 'chain_strength', 'anomaly_cluster', 'conditional',
})


def _require_quant_engine():
    from carrac.auth.decorators import get_tier_limits
    from carrac.mcp.runtime import get_mcp_caller_tier
    if not get_tier_limits(get_mcp_caller_tier()).get('quant_engine', False):
        raise ToolError(
            'This requires the quant_engine feature. This needs a higher plan. An admin can change plans under Admin → Users.'
        )


@mcp_tool(
    'alert_manage',
    'Create, list, update, toggle, or delete alerts across every alert family: '
    'price / indicator / formula (this tool\'s own types) plus the quant-engine '
    'types zscore / regime / chain_strength / anomaly_cluster / conditional '
    '(absorbed from the old create_quant_alert tool — same params, '
    'passed straight through, see their descriptions below). '
    'SIDE EFFECTS (action=create): writes a new alert row and arms the evaluator. '
    'SIDE EFFECTS (action=delete): hard-removes the alert row — use dry_run=true '
    'to preview before committing. Bulk delete via alert_ids (max 50). '
    'PREREQUISITE (action=create): the caller must have at least one notification '
    'channel configured (ntfy, Discord, Telegram, or email) — raises ToolError '
    '("No notification channel configured") if none. '
    'Call notification_configure action=set to set one up first. '
    'Alert types available for create: '
    '"price" (symbol + above/below + price), '
    '"indicator" (symbol + indicator + target_state — supports bb_position, '
    'bb_squeeze, consensio, rsi, ema_ribbon, volprofile_zone and others), '
    '"formula" (boolean DSL expression, fires on flip-to-true), '
    '"zscore" (metric + condition + threshold), '
    '"regime" (fires on any derivatives regime transition — same as '
    'alert_create_regime_watch but from within this tool), '
    '"chain_strength" (chain + condition + threshold), '
    '"anomaly_cluster" (min_domains + min_metrics), '
    '"conditional" (conditions list, 1-5 DSL-style strings like "VIX:gt:2"). '
    'The list action returns ALL alert families by default (price, indicator, '
    'formula, quant, drawing, regime) — pass family to filter to one: '
    '"quant" (zscore/regime/chain_strength/anomaly_cluster/conditional, with '
    'their type-specific params), "drawing" (trendline_break + drawing-linked '
    'price alerts), "regime" (derivatives_regime alerts only), "formula" '
    '(alert_type=formula only). Filtered families return a shaped subset of '
    'fields specific to that family; family="all" (default) returns the raw '
    'alert rows unfiltered.',
    {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['create', 'list', 'delete', 'update', 'toggle'],
                'description': 'Action to perform (required)',
            },
            'alert_type': {
                'type': 'string',
                'enum': [
                    'price', 'indicator', 'formula',
                    'zscore', 'regime', 'chain_strength', 'anomaly_cluster', 'conditional',
                ],
                'description': 'Alert type (create only, default: price). The last five '
                               '(zscore/regime/chain_strength/anomaly_cluster/conditional) '
                               'are quant-engine types — see metric/threshold/chain/'
                               'min_domains/min_metrics/conditions/target/horizon below.',
            },
            'family': {
                'type': 'string',
                'enum': ['all', 'quant', 'drawing', 'regime', 'formula'],
                'description': 'Filter for action=list (default: all — every family, '
                               'unfiltered shape).',
            },
            'metric': {
                'type': 'string',
                'description': 'Metric name (alert_type=zscore only, e.g. VIX, M2)',
            },
            'threshold': {
                'type': 'number',
                'description': 'Threshold value (alert_type=zscore sigma or '
                               'chain_strength 0-1)',
            },
            'window': {
                'type': 'integer',
                'description': 'Z-score window in days (alert_type=zscore only; default 90)',
            },
            'chain': {
                'type': 'string',
                'enum': ['monetary_policy', 'dollar_liquidity', 'risk_appetite'],
                'description': 'Chain name (alert_type=chain_strength only)',
            },
            'min_domains': {
                'type': 'integer',
                'description': 'Minimum domains in cluster (alert_type=anomaly_cluster only; 1-6)',
            },
            'min_metrics': {
                'type': 'integer',
                'description': 'Minimum extreme metrics in cluster (alert_type=anomaly_cluster only; 1-20)',
            },
            'conditions': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Condition strings (alert_type=conditional only; 1-5 items like "VIX:gt:2")',
            },
            'target': {
                'type': 'string',
                'description': 'Target asset (alert_type=conditional only; default BTC)',
            },
            'horizon': {
                'type': 'integer',
                'description': 'Horizon days (alert_type=conditional only; default 30)',
            },
            'symbol': {
                'type': 'string',
                'description': 'Asset symbol (required for price/indicator; '
                               'optional for formula)',
            },
            'condition': {
                'type': 'string',
                'enum': ['above', 'below'],
                'description': 'Price condition (price alerts only)',
            },
            'price': {
                'type': 'number',
                'description': 'Trigger price (price alerts only)',
            },
            'indicator': {
                'type': 'string',
                'description': (
                    'Indicator name (indicator alerts only). Common values: '
                    '"bb_position" (with target_state inside/above_upper/below_lower), '
                    '"bb_squeeze" (squeezed/normal/expanded), '
                    '"consensio", "rsi", "ema_ribbon", "volprofile_zone". '
                    'Run with action="create" + invalid indicator to see the full list.'
                ),
            },
            'target_state': {
                'type': 'string',
                'description': 'Target state the indicator should transition '
                               'into (indicator alerts only, e.g. "bullish")',
            },
            'expression': {
                'type': 'string',
                'description': 'Boolean formula expression '
                               '(formula alerts only, max 500 chars)',
            },
            'timeframe': {
                'type': 'string',
                'description': 'Timeframe for indicator/formula alerts '
                               '(e.g. "1h", "4h", "1d"; default 1h)',
            },
            'bb_params': {
                'type': 'object',
                'description': (
                    'BB parameters snapshot for bb_position alerts only '
                    '(period/std_dev/offset). Defaults to '
                    '{period: 20, std_dev: 2.0, offset: 0} if omitted. '
                    'The alert evaluator uses these exact params at fire time '
                    'so chart-side BB tweaks do not affect the alert.'
                ),
                'properties': {
                    'period':  {'type': 'integer', 'minimum': 5, 'maximum': 200},
                    'std_dev': {'type': 'number',  'minimum': 0.5, 'maximum': 5.0},
                    'offset':  {'type': 'integer', 'minimum': -20, 'maximum': 20},
                },
            },
            'frequency': {
                'type': 'string',
                'enum': ['once', 'once_per_bar', 'recurring'],
                'description': 'Trigger mode. price/indicator/formula default '
                               'once_per_bar; quant alert_types (zscore/regime/'
                               'chain_strength/anomaly_cluster/conditional) default '
                               'once, or recurring for conditional.',
            },
            'note': {
                'type': 'string',
                'description': 'Freeform note (max 200 chars)',
            },
            'alert_id': {
                'type': 'integer',
                'description': 'Single alert ID (delete / update / toggle). '
                               'For bulk delete use alert_ids instead.',
            },
            'alert_ids': {
                'type': 'array',
                'items': {'type': 'integer'},
                'description': 'List of alert IDs to delete in bulk (max 50). '
                               'Works for ANY alert type.',
            },
            'dry_run': {
                'type': 'boolean',
                'description': 'Preview deletion without committing (delete action only). '
                               'Returns would_delete + skipped counts.',
            },
            'updates': {
                'type': 'object',
                'description': 'Fields to update (update action only). '
                               'Editable: price, note, frequency, condition.',
            },
            'active_only': {
                'type': 'boolean',
                'description': 'List only active alerts (default: true). '
                               'All alert families are included.',
            },
        },
        'required': ['action'],
    },
    min_tier='member',
)
def alert_manage(arguments):
    import push_notifications

    action = arguments.get('action')
    user_id = str(get_mcp_user_id())

    if action == 'list':
        family = arguments.get('family') or 'all'
        active_only = arguments.get('active_only', True)

        if family == 'quant':
            _require_quant_engine()
            return list_quant_alerts(arguments)
        if family == 'regime':
            _require_quant_engine()
            return list_regime_alerts(arguments)
        if family == 'drawing':
            return list_drawing_alerts(arguments)
        if family == 'formula':
            from carrac.mcp.tools.bridges import list_formula_alerts
            return list_formula_alerts(arguments)
        if family != 'all':
            raise ToolError(
                f'Unknown family: {family!r}. Use all / quant / drawing / regime / formula.'
            )

        alerts = push_notifications.get_user_alerts(user_id, active_only=active_only)
        return {'alerts': alerts, 'count': len(alerts)}

    if action == 'create':
        alert_type = (arguments.get('alert_type') or 'price').lower()

        if alert_type in _QUANT_CREATE_TYPES:
            # Legacy create_quant_alert path —
            # delegate wholesale, including its own notification-channel and
            # per-type validation (not duplicated here).
            _require_quant_engine()
            from carrac.mcp.tools.quant import create_quant_alert
            return create_quant_alert({**arguments, 'type': alert_type})

        if not push_notifications.user_has_notification_channel(user_id):
            raise ToolError(
                'No notification channel configured. Enable ntfy / Discord / '
                'Telegram / web push in Settings first, or call '
                'notification_configure(action="set") in this session.'
            )

        if alert_type == 'price':
            symbol = (arguments.get('symbol') or '').upper().strip()
            condition = arguments.get('condition')
            price = arguments.get('price')
            if not symbol:
                raise ToolError('symbol is required for price alerts')
            if condition not in ('above', 'below'):
                raise ToolError('condition must be "above" or "below"')
            if not price or price <= 0:
                raise ToolError('price must be a positive number')

            alert = push_notifications.create_alert(
                user_id=user_id,
                symbol=symbol,
                condition=condition,
                price=float(price),
                note=arguments.get('note', ''),
                frequency=arguments.get('frequency', 'once_per_bar'),
            )
            return {'alert': alert, 'status': 'created'}

        if alert_type == 'indicator':
            from carrac.indicator_state import INDICATOR_ALERT_CONDITIONS
            symbol = (arguments.get('symbol') or '').upper().strip()
            indicator = arguments.get('indicator')
            target_state = arguments.get('target_state')
            if not symbol:
                raise ToolError('symbol is required for indicator alerts')
            if not indicator or indicator not in INDICATOR_ALERT_CONDITIONS:
                raise ToolError(
                    f'Invalid indicator. Valid: {", ".join(INDICATOR_ALERT_CONDITIONS.keys())}'
                )
            valid_states = INDICATOR_ALERT_CONDITIONS[indicator]
            if target_state not in valid_states:
                raise ToolError(
                    f'Invalid target_state for {indicator}. Valid: {valid_states}'
                )

            alert = {
                'user_id': user_id,
                'symbol': symbol,
                'alert_type': 'indicator',
                'indicator': indicator,
                'target_state': target_state,
                'timeframe': arguments.get('timeframe', '1h'),
                'note': (arguments.get('note') or '')[:200],
                'frequency': arguments.get('frequency', 'once_per_bar'),
                'last_triggered_bar': None,
                'last_state': None,
                'triggered': False,
                'triggered_at': None,
                'active': True,
            }
            # Snapshot custom BB params for bb_position alerts.
            if indicator == 'bb_position' and 'bb_params' in arguments:
                raw_bb = arguments['bb_params']
                if not isinstance(raw_bb, dict):
                    raise ToolError('bb_params must be an object with period/std_dev/offset fields')
                period = raw_bb.get('period', 20)
                std_dev = raw_bb.get('std_dev', 2.0)
                offset = raw_bb.get('offset', 0)
                if not isinstance(period, int) or not (5 <= period <= 200):
                    raise ToolError('bb_params.period must be an integer between 5 and 200')
                if not isinstance(std_dev, (int, float)) or not (0.5 <= std_dev <= 5.0):
                    raise ToolError('bb_params.std_dev must be a number between 0.5 and 5.0')
                if not isinstance(offset, int) or not (-20 <= offset <= 20):
                    raise ToolError('bb_params.offset must be an integer between -20 and 20')
                alert['bb_params'] = {
                    'period': period,
                    'std_dev': float(std_dev),
                    'offset': offset,
                }

            alert = push_notifications.insert_alert(alert)
            return {'alert': alert, 'status': 'created'}

        if alert_type == 'formula':
            from carrac.formula_eval import validate_formula
            expression = arguments.get('expression', '').strip()
            if not expression:
                raise ToolError('expression is required for formula alerts')
            if len(expression) > 500:
                raise ToolError('expression must be 500 chars or fewer')
            validation = validate_formula(expression)
            if not validation.get('valid'):
                raise ToolError(
                    f"Invalid formula: {validation.get('error', 'parse error')}"
                )

            alert = {
                'user_id': user_id,
                'symbol': (arguments.get('symbol') or '').upper().strip() or 'BTC',
                'alert_type': 'formula',
                'expression': expression,
                'timeframe': arguments.get('timeframe', '1h'),
                'note': (arguments.get('note') or '')[:200],
                'frequency': arguments.get('frequency', 'once_per_bar'),
                'last_triggered_bar': None,
                'last_value': None,
                'triggered': False,
                'triggered_at': None,
                'active': True,
            }
            alert = push_notifications.insert_alert(alert)
            return {'alert': alert, 'status': 'created'}

        raise ToolError(
            f'Unknown alert_type: {alert_type}. Use price / indicator / formula / '
            f'zscore / regime / chain_strength / anomaly_cluster / conditional.'
        )

    if action == 'delete':
        raw_id = arguments.get('alert_id')
        raw_ids = arguments.get('alert_ids') or []
        dry_run = bool(arguments.get('dry_run', False))

        ids = []
        if raw_id is not None:
            ids.append(int(raw_id))
        if raw_ids:
            if not isinstance(raw_ids, list):
                raise ToolError('alert_ids must be an array of integers')
            try:
                ids.extend(int(i) for i in raw_ids)
            except (TypeError, ValueError):
                raise ToolError('alert_ids must contain integers')
        # De-dupe while preserving order.
        seen = set()
        ids = [i for i in ids if not (i in seen or seen.add(i))]

        if not ids:
            raise ToolError('Provide alert_id or alert_ids for delete')
        if len(ids) > _BULK_CAP:
            raise ToolError(f'Too many ids ({len(ids)}); max {_BULK_CAP} per call')

        # Dry-run: resolve previews without committing.
        if dry_run:
            all_alerts = push_notifications.get_user_alerts(user_id, active_only=False)
            by_id = {a['id']: a for a in all_alerts}
            would_delete = []
            skipped = []
            for aid in ids:
                a = by_id.get(aid)
                if a:
                    would_delete.append({
                        'alert_id': aid,
                        'alert_type': a.get('alert_type'),
                        'symbol': a.get('symbol'),
                        'note': a.get('note', ''),
                        'active': a.get('active'),
                        'created_at': a.get('created_at'),
                    })
                else:
                    skipped.append(aid)
            return {
                'dry_run': True,
                'would_delete': would_delete,
                'skipped': skipped,
                'count': len(would_delete),
            }

        # Single-id path (legacy behaviour, backward-compatible).
        if len(ids) == 1:
            ok = push_notifications.delete_alert(user_id, ids[0])
            if not ok:
                raise ToolError(f'Alert {ids[0]} not found or not owned by caller')
            return {'dry_run': False, 'deleted_count': 1,
                    'deleted': [ids[0]], 'skipped': []}

        # Bulk path.
        deleted, skipped = push_notifications.delete_alerts_bulk(user_id, ids)
        return {
            'dry_run': False,
            'deleted_count': len(deleted),
            'deleted': deleted,
            'skipped': skipped,
        }

    if action == 'toggle':
        alert_id = arguments.get('alert_id')
        if not alert_id:
            raise ToolError('alert_id is required for toggle')
        alert = push_notifications.toggle_alert(user_id, int(alert_id))
        if not alert:
            raise ToolError(f'Alert {alert_id} not found or not owned by caller')
        return {'alert': alert, 'status': 'toggled'}

    if action == 'update':
        alert_id = arguments.get('alert_id')
        updates = arguments.get('updates') or {}
        if not alert_id:
            raise ToolError('alert_id is required for update')
        if not isinstance(updates, dict) or not updates:
            raise ToolError('updates must be a non-empty object')
        alert = push_notifications.update_alert(user_id, int(alert_id), updates)
        if not alert:
            raise ToolError(f'Alert {alert_id} not found or not owned by caller')
        return {'alert': alert, 'status': 'updated'}

    raise ToolError(f'Unknown action: {action}')


# Not a standalone MCP tool — reached through alert_manage
# (action=list, family=quant). A plain helper function, called from
# alert_manage and from the legacy 'list_quant_alerts' alias dispatch
# (registry.LEGACY_ARG_DEFAULTS forces action=list/family=quant for that
# alias since legacy callers never pass those params).
def list_quant_alerts(arguments):
    import push_notifications

    user_id = str(get_mcp_user_id())
    active_only = arguments.get('active_only', True)
    all_alerts = push_notifications.get_user_alerts(user_id, active_only=active_only)
    quant_alerts = [
        {
            'id': a['id'],
            'alert_type': a.get('alert_type'),
            'metric': a.get('metric'),
            'condition': a.get('condition'),
            'threshold': a.get('threshold'),
            'window': a.get('window'),
            'chain': a.get('chain'),
            'min_domains': a.get('min_domains'),
            'min_metrics': a.get('min_metrics'),
            'conditions': a.get('conditions'),
            'target': a.get('target'),
            'note': a.get('note', ''),
            'frequency': a.get('frequency'),
            'triggered': a.get('triggered'),
            'triggered_at': a.get('triggered_at'),
            'active': a.get('active'),
            'created_at': a.get('created_at'),
        }
        for a in all_alerts if a.get('alert_type') in _QUANT_ALERT_TYPES
    ]
    return {'count': len(quant_alerts), 'alerts': quant_alerts}


# No longer a standalone MCP tool — absorbed into alert_manage
# (action=list, family=drawing). Kept as a plain helper (see list_quant_alerts
# above for the legacy-alias mechanics — identical here).
def list_drawing_alerts(arguments):
    import push_notifications

    user_id = str(get_mcp_user_id())
    active_only = arguments.get('active_only', True)
    all_alerts = push_notifications.get_user_alerts(user_id, active_only=active_only)

    # Drawing alerts: trendline_break type + any price alert that carries a
    # drawing_id in its metadata (created by alert_create_from_drawing for
    # horizontal_line / zone drawing types).
    drawing_alerts = []
    for a in all_alerts:
        atype = a.get('alert_type', '')
        if atype in _DRAWING_ALERT_TYPES or a.get('drawing_id'):
            drawing_alerts.append({
                'id': a['id'],
                'alert_type': atype,
                'symbol': a.get('symbol'),
                'drawing_id': a.get('drawing_id'),
                'condition': a.get('condition'),
                'price': a.get('price'),
                'note': a.get('note', ''),
                'frequency': a.get('frequency'),
                'triggered': a.get('triggered'),
                'triggered_at': a.get('triggered_at'),
                'active': a.get('active'),
                'created_at': a.get('created_at'),
            })
    return {'count': len(drawing_alerts), 'alerts': drawing_alerts}


# No longer a standalone MCP tool — absorbed into alert_manage
# (action=list, family=regime). Kept as a plain helper (see list_quant_alerts
# above for the legacy-alias mechanics — identical here).
def list_regime_alerts(arguments):
    import push_notifications

    user_id = str(get_mcp_user_id())
    active_only = arguments.get('active_only', True)
    all_alerts = push_notifications.get_user_alerts(user_id, active_only=active_only)
    regime_alerts = [
        {
            'id': a['id'],
            'alert_type': a.get('alert_type'),
            'from_regime': a.get('from_regime'),
            'to_regime': a.get('to_regime'),
            'note': a.get('note', ''),
            'frequency': a.get('frequency'),
            'last_deriv_regime': a.get('last_deriv_regime'),
            'triggered': a.get('triggered'),
            'triggered_at': a.get('triggered_at'),
            'active': a.get('active'),
            'created_at': a.get('created_at'),
        }
        for a in all_alerts if a.get('alert_type') in _REGIME_ALERT_TYPES
    ]
    return {'count': len(regime_alerts), 'alerts': regime_alerts}


@mcp_tool(
    'alert_list_triggered',
    'Returns all alerts that have fired for the caller, across every alert family '
    '(price, indicator, formula, quant, drawing, regime). '
    'Each item includes: id, alert_type, symbol, condition, price (threshold set), '
    'triggered_price (price at fire time), triggered_at (ISO timestamp), note. '
    'Use the since parameter (ISO timestamp) to retrieve only alerts triggered '
    'after a known point — useful for polling in automated workflows. '
    'Freshness: reads live alert storage; re-call to check for new fires. '
    'To clean up fired alerts use alert_manage action=delete with the returned id.',
    {
        'type': 'object',
        'properties': {
            'since': {
                'type': 'string',
                'description': 'ISO timestamp to filter from (optional, e.g. "2026-04-01T00:00:00")',
            },
        },
    },
    min_tier='member',
)
def alert_list_triggered(arguments):
    import push_notifications

    since = arguments.get('since', '')
    alerts = push_notifications.get_user_alerts(str(get_mcp_user_id()), active_only=False)

    triggered = []
    for a in alerts:
        ts = a.get('triggered_at')
        if not ts:
            continue
        if since and ts <= since:
            continue
        triggered.append({
            'id': a.get('id'),
            'alert_type': a.get('alert_type', 'price'),
            'symbol': a.get('symbol'),
            'condition': a.get('condition', ''),
            'price': a.get('price'),
            'triggered_price': a.get('triggered_price'),
            'triggered_at': ts,
            'note': a.get('note', ''),
        })

    return {'triggered': triggered, 'count': len(triggered)}


@mcp_tool(
    'notification_configure',
    'Get or set push notification channel settings for the authenticated user. '
    'Supported channels: ntfy topic URL, Discord webhook, Telegram bot + chat_id, '
    'email. '
    'SIDE EFFECTS (action=set): persists channel config to user preferences; '
    'enables the channels for all future alert fires. '
    'PREREQUISITE for alert_manage action=create: at least one channel must be '
    'enabled here first — call action=get to check current state, '
    'then action=set to configure a channel before creating alerts. '
    'action=get returns current config with the Telegram bot token masked '
    '(first 4 chars + asterisks). '
    'action=set merges supplied fields into existing config; omitted channels '
    'are left unchanged. '
    'Note: Telegram and Discord delivery requires Member or Premium subscription.',
    {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['get', 'set'],
                'description': 'Action to perform (required)',
            },
            'ntfy_url': {
                'type': 'string',
                'description': (
                    'ntfy topic URL for set action '
                    '(e.g. https://ntfy.sh/my-topic). '
                    'Omit to leave unchanged.'
                ),
            },
            'ntfy_enabled': {
                'type': 'boolean',
                'description': 'Enable/disable ntfy notifications',
            },
            'discord_webhook': {
                'type': 'string',
                'description': (
                    'Discord webhook URL for set action '
                    '(must be https://discord.com/api/webhooks/...). '
                    'Omit to leave unchanged.'
                ),
            },
            'discord_enabled': {
                'type': 'boolean',
                'description': 'Enable/disable Discord notifications',
            },
            'telegram_chat_id': {
                'type': 'string',
                'description': 'Telegram chat ID (numeric, may be negative for groups)',
            },
            'telegram_bot_token': {
                'type': 'string',
                'description': (
                    'Telegram bot token (format: 123456:ABC-DEF...). '
                    'Stored encrypted. Not returned in plaintext by get.'
                ),
            },
            'telegram_enabled': {
                'type': 'boolean',
                'description': 'Enable/disable Telegram notifications',
            },
            'email_enabled': {
                'type': 'boolean',
                'description': 'Enable/disable email notifications',
            },
        },
        'required': ['action'],
    },
    min_tier='member',
)
def notification_configure(arguments):
    from user_db import user_db as _user_db
    from carrac.secret_box import decrypt_token, encrypt_token

    user_id = get_mcp_user_id()
    action = (arguments.get('action') or '').lower()

    _NOTIF_DEFAULTS = {
        'ntfy': {'enabled': False, 'url': ''},
        'discord': {'enabled': False, 'webhook_url': ''},
        'telegram': {'enabled': False, 'bot_token': '', 'chat_id': ''},
        'email': {'enabled': False},
    }

    def _get_current():
        prefs = _user_db.get_preferences(user_id)
        notif = prefs.get('notifications', _NOTIF_DEFAULTS)
        for ch, defaults in _NOTIF_DEFAULTS.items():
            if ch not in notif:
                notif[ch] = dict(defaults)
        # Mask Telegram token
        tg = dict(notif.get('telegram', {}))
        raw_token = tg.get('bot_token', '')
        if raw_token:
            plain = decrypt_token(raw_token)
            tg['bot_token'] = (plain[:4] + '****') if plain else '****'
        else:
            tg['bot_token'] = ''
        notif = dict(notif)
        notif['telegram'] = tg
        return notif

    if action == 'get':
        return _get_current()

    if action == 'set':
        # Load existing prefs and merge changes
        prefs = _user_db.get_preferences(user_id)
        existing_notif = prefs.get('notifications', {})
        for ch, defaults in _NOTIF_DEFAULTS.items():
            if ch not in existing_notif:
                existing_notif[ch] = dict(defaults)

        import copy
        notif = copy.deepcopy(existing_notif)

        warnings = []

        # ntfy — validate before persisting (SSRF hardening, mirrors alerts/routes.py).
        if 'ntfy_url' in arguments:
            ntfy_url = str(arguments['ntfy_url'] or '').strip()[:500]
            if ntfy_url:
                from security import validate_outbound_url
                from carrac.alerts.routes import _ntfy_allow_hosts
                ok, reason = validate_outbound_url(
                    ntfy_url,
                    allowed_schemes=('https',),
                    allow_hosts=_ntfy_allow_hosts(),
                )
                if not ok:
                    warnings.append(f'ntfy URL rejected and not saved: {reason}')
                    ntfy_url = notif['ntfy'].get('url', '')  # keep existing value
            notif['ntfy']['url'] = ntfy_url
        if 'ntfy_enabled' in arguments:
            notif['ntfy']['enabled'] = bool(arguments['ntfy_enabled'])

        # discord — validate before persisting (SSRF hardening, mirrors alerts/routes.py).
        if 'discord_webhook' in arguments:
            import re as _re
            discord_webhook = str(arguments['discord_webhook'] or '').strip()[:500]
            if discord_webhook:
                if not _re.match(r'https://discord\.com/api/webhooks/\d+/.+', discord_webhook):
                    warnings.append(
                        'Discord webhook URL rejected and not saved: '
                        'must match https://discord.com/api/webhooks/<id>/<token>'
                    )
                    discord_webhook = notif['discord'].get('webhook_url', '')  # keep existing
            notif['discord']['webhook_url'] = discord_webhook
        if 'discord_enabled' in arguments:
            notif['discord']['enabled'] = bool(arguments['discord_enabled'])

        # telegram
        if 'telegram_bot_token' in arguments and arguments['telegram_bot_token']:
            raw_token = str(arguments['telegram_bot_token']).strip()
            import re as _re
            if not _re.match(r'\d+:[A-Za-z0-9_-]+$', raw_token):
                warnings.append(
                    'telegram_bot_token looks malformed '
                    '(expected format: 123456:ABCdef...). Saved anyway.'
                )
            notif['telegram']['bot_token'] = encrypt_token(raw_token)
        if 'telegram_chat_id' in arguments:
            notif['telegram']['chat_id'] = str(arguments['telegram_chat_id'] or '').strip()
        if 'telegram_enabled' in arguments:
            notif['telegram']['enabled'] = bool(arguments['telegram_enabled'])

        # email
        if 'email_enabled' in arguments:
            notif['email']['enabled'] = bool(arguments['email_enabled'])

        prefs['notifications'] = notif
        _user_db.save_preferences(user_id, prefs)

        result = _get_current()
        if warnings:
            result['warnings'] = warnings
        return result

    raise ToolError(f'Unknown action: {action!r}. Use get or set')
