"""MCP tools: consolidated public sharing.

share_manage absorbs the six old share_chart/unshare_chart/share_play/
unshare_play/share_trade/unshare_trade tools — all six were structurally
identical mint-or-revoke-a-slug operations over the same active-share quota,
differing only in which entity type they minted for. entity_type + action
pick the underlying (unchanged) handler; the handlers themselves still live
in charts.py/plays.py/journal.py as plain functions (no longer separately
registered as MCP tools).

Legacy old-name calls (share_chart, unshare_play, etc.) still dispatch
straight to those same handlers via registry.LEGACY_TOOL_ALIASES +
LEGACY_ARG_DEFAULTS, which fill in entity_type/action since the legacy call
never carried them.
"""

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError

_ENTITY_TYPES = ('chart', 'play', 'trade')
_ACTIONS = ('share', 'unshare')


@mcp_tool(
    'share_manage',
    'Mint or revoke a PUBLIC share link for a chart, play, or trade. '
    'entity_type picks WHAT (chart/play/trade), action picks share (mint, or '
    'refresh an existing link\'s toggles) or unshare (revoke). All three '
    'entity types mint a frozen, immutable snapshot at a public URL — no '
    'Carrac account required to view — and count against ONE shared '
    'per-tier active-share quota. Revoking frees a quota slot immediately '
    '(the public URL then 410s); a revoked slug is never reused.\n\n'
    'entity_type=chart: action=share needs symbol (+ optional timeframe, '
    'default 1D) — mints a NEW link every call (a chart share is a moment '
    'in time), with a full-indicator server-side og:image render (falls '
    'back to a plain candlestick card if the render service is down — '
    'never fails because of that). action=unshare needs slug.\n'
    'entity_type=play: action=share needs play_id (+ optional '
    'include_thesis/include_conditions/include_plan/include_outcome '
    'toggles — default thesis/conditions/plan ON, outcome OFF). Calling '
    'share again on the same play returns the existing link unless toggles '
    'are passed, which re-freezes the snapshot. action=unshare needs '
    'play_id.\n'
    'entity_type=trade: action=share needs trade_id, and the trade must '
    'already be CLOSED (trade_close first) — (+ optional include_levels/'
    'include_note toggles — default levels ON, note OFF). PRIVACY: the '
    'public trade card shows percentages and R-multiple only, never '
    'position size, fees, or currency P&L. action=unshare needs trade_id.\n\n'
    'Returns (action=share): slug, url, image_url, view_count, plus '
    'entity-specific fields (chart: symbol/timeframe/change_pct; play: the '
    'resolved include_* toggles; trade: the resolved include_* toggles + '
    'pnl_pct/rr_actual). Returns (action=unshare): the entity id/slug plus '
    'revoked (bool).',
    {
        'type': 'object',
        'properties': {
            'entity_type': {
                'type': 'string',
                'enum': list(_ENTITY_TYPES),
                'description': 'Which kind of share (required).',
            },
            'action': {
                'type': 'string',
                'enum': list(_ACTIONS),
                'description': '"share" to mint/refresh, "unshare" to revoke (required).',
            },
            'symbol': {
                'type': 'string',
                'description': 'entity_type=chart, action=share only: asset symbol '
                               '(e.g. BTC, ETH, SOL), required.',
            },
            'timeframe': {
                'type': 'string',
                'description': 'entity_type=chart, action=share only: timeframe '
                               '(e.g. 1D, 4h, 1W). Default 1D.',
            },
            'slug': {
                'type': 'string',
                'description': 'entity_type=chart, action=unshare only: the slug '
                               'returned by the earlier share call. Required.',
            },
            'play_id': {
                'type': 'integer',
                'description': 'entity_type=play only (both actions): the play ID. Required.',
            },
            'include_thesis': {
                'type': 'boolean',
                'description': 'entity_type=play, action=share only: show the thesis '
                               '(default true).',
            },
            'include_conditions': {
                'type': 'boolean',
                'description': 'entity_type=play, action=share only: show the '
                               'conditions checklist (default true).',
            },
            'include_plan': {
                'type': 'boolean',
                'description': 'entity_type=play, action=share only: show action plan '
                               '+ numeric levels (default true).',
            },
            'include_outcome': {
                'type': 'boolean',
                'description': 'entity_type=play, action=share only: show resolution/'
                               'result once terminal (default false).',
            },
            'trade_id': {
                'type': 'integer',
                'description': 'entity_type=trade only (both actions): the trade ID. '
                               'Required; must be a CLOSED trade for action=share.',
            },
            'include_levels': {
                'type': 'boolean',
                'description': 'entity_type=trade, action=share only: show entry/exit '
                               'prices (default true; off = %-only).',
            },
            'include_note': {
                'type': 'boolean',
                'description': 'entity_type=trade, action=share only: show the close '
                               'note (default false).',
            },
        },
        'required': ['entity_type', 'action'],
    },
    min_tier='free',
)
def share_manage(arguments):
    entity_type = arguments.get('entity_type')
    action = arguments.get('action')

    if entity_type not in _ENTITY_TYPES:
        raise ToolError(f'entity_type is required: {" / ".join(_ENTITY_TYPES)}')
    if action not in _ACTIONS:
        raise ToolError(f'action is required: {" / ".join(_ACTIONS)}')

    if entity_type == 'chart':
        from carrac.mcp.tools.charts import share_chart, unshare_chart
        return share_chart(arguments) if action == 'share' else unshare_chart(arguments)

    if entity_type == 'play':
        from carrac.mcp.tools.plays import share_play, unshare_play
        return share_play(arguments) if action == 'share' else unshare_play(arguments)

    # trade
    from carrac.mcp.tools.journal import share_trade, unshare_trade
    return share_trade(arguments) if action == 'share' else unshare_trade(arguments)
