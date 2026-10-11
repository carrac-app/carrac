"""MCP tools: Derivatives regime reactivity.

Tools:
  - quant_get_regime_history
  - alert_create_regime_watch
"""

from carrac.compliance.deribit_gate import strip_deribit_columns
import logging

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id

logger = logging.getLogger('carrac.mcp.tools.regime')


VALID_REGIMES = (
    'euphoric_meltup', 'complacent_bull', 'healthy_trend', 'pre_event_tension',
    'distribution_topping', 'sudden_correction', 'full_capitulation',
    'post_crash_stabilization', 'vol_compression', 'bear_grind',
    'short_squeeze_setup', 'structural_carry',
)


# ── quant_get_regime_history ─────────────────────────────────────────

@mcp_tool(
    'quant_get_regime_history',
    'Return the full derivatives regime time series for an asset over the '
    'last N days. Re-classifies every historical row in quant.derivatives '
    'using the live HMM-based classifier (same logic as quant_get_regime). '
    'Returns: samples (count of classified rows), transitions_count, '
    'occupancy_pct (fraction of days spent in each regime — useful for '
    'spotting dominant states), transitions list (date + from_regime + '
    'to_regime + confidence), and sequence (date + regime + confidence for '
    'every row). '
    'Valid regime names: euphoric_meltup, complacent_bull, healthy_trend, '
    'pre_event_tension, distribution_topping, sudden_correction, '
    'full_capitulation, post_crash_stabilization, vol_compression, bear_grind, '
    'short_squeeze_setup, structural_carry. '
    'Call this to map regime context across a lookback window, identify how '
    'many transitions occurred, or check what regime preceded the current one. '
    'For just the current regime snapshot use quant_get_regime. '
    'Max lookback: 730 days.',
    {
        'type': 'object',
        'properties': {
            'asset': {'type': 'string', 'description': 'Asset to classify (default: BTC)'},
            'days': {
                'type': 'integer',
                'description': 'Lookback window in days (default: 90, max: 730)',
            },
        },
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_regime_history(arguments):
    asset = (arguments.get('asset') or 'BTC').upper().strip()
    days = max(7, min(730, int(arguments.get('days') or 90)))

    try:
        from carrac.db_pool import get_quant_conn as get_conn
        # Classify through classify_asset_row, which owns the
        # raw-column → classifier-key mapping (basis_pct→basis_apr, …).
        # Feeding raw column names straight to classify_derivatives_regime
        # would drop basis/put-call/term-slope from the classification.
        from quant.engine.derivatives_regime import classify_asset_row
    except Exception as e:
        raise ToolError(f'Quant DB unavailable: {e}')

    rows = []
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ts, basis_pct, skew_25d, dvol, iv_rv_premium,
                       options_put_call_ratio, term_structure_slope,
                       gex_net, gex_flip_price
                FROM quant.derivatives
                WHERE asset = %s
                  AND ts >= (CURRENT_DATE - INTERVAL '%s days')
                ORDER BY ts ASC
                """,
                (asset, days),
            )
            rows = cur.fetchall()
            # Deribit columns are admin-only.
            rows = [strip_deribit_columns(r) for r in rows]
    except Exception as e:
        raise ToolError(f'Failed to fetch derivatives history: {e}')

    if not rows:
        return {
            'asset': asset,
            'days': days,
            'samples': 0,
            'sequence': [],
            'transitions': [],
        }

    sequence = []
    for row in rows:
        ts = row['ts']
        metrics = {k: float(v) for k, v in row.items()
                   if k != 'ts' and v is not None}
        if not metrics:
            continue
        try:
            cls = classify_asset_row(metrics)
        except Exception:
            continue
        if cls is None:
            continue
        sequence.append({
            'date': ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
            'regime': cls.get('regime', 'unknown'),
            'confidence': round(float(cls.get('confidence', 0)), 3),
        })

    transitions = []
    prev = None
    for s in sequence:
        if prev and s['regime'] != prev['regime']:
            transitions.append({
                'date': s['date'],
                'from_regime': prev['regime'],
                'to_regime': s['regime'],
                'confidence': s['confidence'],
            })
        prev = s

    # Compute regime occupancy stats.
    occupancy = {}
    for s in sequence:
        occupancy[s['regime']] = occupancy.get(s['regime'], 0) + 1
    total = len(sequence) or 1
    occupancy_pct = {k: round(v / total, 3) for k, v in occupancy.items()}

    return {
        'asset': asset,
        'days': days,
        'samples': len(sequence),
        'transitions_count': len(transitions),
        'occupancy_pct': occupancy_pct,
        'transitions': transitions,
        'sequence': sequence,
    }


# ── alert_create_regime_watch ─────────────────────────────────

@mcp_tool(
    'alert_create_regime_watch',
    'Subscribe to HMM derivatives regime transitions for BTC. '
    'Without from_regime/to_regime, fires on ANY regime change. '
    'Optionally filter to a specific flip — e.g. set from_regime=complacent_bull '
    'and to_regime=distribution_topping to get notified only when that exact '
    'transition occurs. Edge-detected: fires only on the bar of transition, '
    'not while a regime persists. The first evaluation after creation just '
    'establishes a baseline (no fire). '
    'frequency=once auto-disables after first fire; '
    'frequency=recurring re-arms after each transition. '
    'SIDE EFFECT: persists an alert row to the database. NOT DESTRUCTIVE. '
    'PREREQUISITE: user must have a notification channel configured '
    '(ntfy / Discord / Telegram / web push) — raises ToolError '
    '"No notification channel configured" if none is set up. '
    'EXAMPLE: alert on any regime change → {} (no params). '
    'Alert only on capitulation → {"to_regime":"full_capitulation","frequency":"recurring"}.',
    {
        'type': 'object',
        'properties': {
            'from_regime': {
                'type': 'string',
                'enum': list(VALID_REGIMES),
                'description': 'Optional: only fire when transitioning FROM this regime.',
            },
            'to_regime': {
                'type': 'string',
                'enum': list(VALID_REGIMES),
                'description': 'Optional: only fire when transitioning TO this regime.',
            },
            'note': {'type': 'string', 'description': 'Freeform note attached to the alert (max 200 chars)'},
            'frequency': {
                'type': 'string',
                'enum': ['once', 'recurring'],
                'description': 'once = fire once then disable; recurring = re-arms after each transition. Default: once.',
            },
        },
    },
    min_tier='member',
    feature='quant_engine',
)
def alert_create_regime_watch(arguments):
    import push_notifications

    user_id = str(get_mcp_user_id())
    if not push_notifications.user_has_notification_channel(user_id):
        raise ToolError(
            'No notification channel configured. Enable ntfy / Discord / '
            'Telegram / web push in Settings first, or call '
            'notification_configure(action="set") in this session.'
        )

    from_regime = arguments.get('from_regime')
    to_regime = arguments.get('to_regime')
    note = (arguments.get('note') or '')[:200]
    frequency = arguments.get('frequency') or 'once'

    try:
        alert = push_notifications.create_derivatives_regime_alert(
            user_id=user_id,
            from_regime=from_regime,
            to_regime=to_regime,
            note=note,
            frequency=frequency,
        )
    except ValueError as e:
        raise ToolError(str(e))

    return {'alert': alert, 'status': 'created'}
