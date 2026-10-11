"""MCP tools: Plays (8 tools).

The other two Play tools — play_create_from_formula_alert and
play_draft_from_triggered_alerts — live in bridges.py, not here. This header
said 10 because it was not updated when they moved.
"""

from carrac.site_url import site_base_url
from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id


@mcp_tool(
    'play_create',
    'Create a new trading play with thesis, conditions, and invalidation. '
    'Returns the created play with ID. '
    'Supported rule `type` values include: `price_level`, `price_change_pct`, `candle_close`, '
    '`indicator_value`, `indicator_crosses`, `indicator_state` (indicator values: `bb_position` '
    'with states inside/above_upper/below_lower, `bb_squeeze`, `ema_ribbon`, `macd`, `rsi`, '
    '`consensio`, `structure`, `cpr`, `volume`, `adx`, `super_trend`, '
    '`psar` with states bullish/bearish, `ichimoku_tk` with states bullish/bearish/none — '
    'both wired into the Plays evaluator), `indicator_divergence`, '
    '`consensio_threshold`, `scanner_verdict`, `cross_asset`, `formula_expression`, '
    '`derivatives_metric`, `derivatives_regime`, `time_limit`, `manual`, '
    '`engulfing_detected` (params: `direction` bullish/bearish, `timeframe` 1h/4h/D/3D/W), '
    '`bb_breakout_retest_engulfing` (continuation setup: BB breakout → retest middle → engulfing confirmation; '
    'params: `direction` bullish/bearish, `timeframe` 1h/4h/D/3D/W, `lookback_bars` int 5-50, '
    '`proximity_pct` float 0.1-2.0, `require_volume` bool default true), '
    '`bb_fade_engulfing` (fade/reversal setup: price extended past BB band + same-direction engulfing on current bar; '
    'params: `direction` bullish/bearish, `timeframe` 1h/4h/D/3D/W; bullish=fade off lower band, bearish=fade off upper band), '
    '`bb_squeeze_breakout` (TTM Squeeze flavor: BB compressed for N consecutive bars → released → directional close past band; '
    'params: `direction` bullish/bearish, `timeframe` 1h/4h/D/3D/W, `lookback_bars` int 5-30 default 10, '
    '`min_squeeze_bars` int 1-20 default 3, `require_volume` bool default true), '
    '`bb_breakdown_continuation` (breakdown/breakout continuation: band breakout within lookback → confirmed by engulfing OR retest of the broken band; '
    'the only rule besides the composable path that accepts custom BB params, so chart offsets carry into the signal; '
    'params: `direction` bullish/bearish, `timeframe` 1h/4h/D/3D/W, `lookback_bars` int 5-50 default 15, '
    '`confirm_mode` engulfing/band_retest/either default either, `retest_band` broken/middle default broken, '
    '`proximity_pct` float 0.1-2.0 default 0.5, `bb_params` object {period 5-200, std_dev 0.5-5, offset -20..20}). '
    'Use `logic: "all"` or `"any"` to combine multiple rules.',
    {
        'type': 'object',
        'properties': {
            'title': {
                'type': 'string',
                'description': 'Short title for the play (required)',
            },
            'thesis': {
                'type': 'string',
                'description': 'Detailed thesis explaining the trade idea',
            },
            'direction': {
                'type': 'string',
                'enum': ['long', 'short'],
                'description': 'Trade direction (default: long)',
            },
            'primary_asset': {
                'type': 'string',
                'description': 'Main asset symbol (e.g. BTC, ETH)',
            },
            'timeframe': {
                'type': 'string',
                'description': 'Timeframe (e.g. 1D, 4H, 1W)',
            },
            'conditions': {
                'type': 'object',
                'description': 'Conditions object: {rules: [{type, params, ...}], logic: "all"|"any"}',
            },
            'invalidation': {
                'type': 'object',
                'description': 'Invalidation criteria as JSON object',
            },
            'action_plan': {
                'type': 'string',
                'description': 'What to do when conditions are met',
            },
            'type': {
                'type': 'string',
                'enum': ['directional', 'hedge', 'spread', 'macro'],
                'description': 'Play type (default: directional)',
            },
        },
        'required': ['title'],
    },
    min_tier='free',
)
def play_create(arguments):
    from carrac.plays.models import play_create as _create, count_active_plays
    from carrac.mcp.runtime import get_mcp_caller_tier
    from carrac.auth.decorators import get_tier_limits_for_user

    title = arguments.get('title', '').strip()
    if not title:
        raise ToolError('title is required')

    user_id = get_mcp_user_id()
    tier = get_mcp_caller_tier()
    limits = get_tier_limits_for_user(user_id, tier)
    max_plays = limits.get('max_plays', 0)
    current = count_active_plays(user_id)
    if current >= max_plays:
        raise ToolError(
            f'Play limit reached ({current}/{max_plays}). Upgrade your plan.'
        )

    result = _create(
        user_id=user_id,
        title=title,
        thesis=arguments.get('thesis'),
        type=arguments.get('type', 'directional'),
        direction=arguments.get('direction', 'long'),
        primary_asset=arguments.get('primary_asset', '').upper() or None,
        conditions=arguments.get('conditions'),
        invalidation=arguments.get('invalidation'),
        timeframe=arguments.get('timeframe'),
        action_plan=arguments.get('action_plan'),
    )
    return result


@mcp_tool(
    'play_list',
    'Return the user\'s trading plays with live condition evaluation status. '
    'Default (no filters): all non-terminal plays (watching + acting + expired), '
    'sorted acting → watching → expired. '
    'Status filter values: watching (monitoring conditions), acting (conditions met, in trade), '
    'expired (time-limit lapsed, reactivatable), completed (terminal, target reached), '
    'invalidated (terminal, thesis failed). '
    'Set include_resolved=true to include completed + invalidated in the result. '
    'Key fields per play: id, title, primary_asset, direction, status, conditions '
    '(each rule with met + evidence), action_plan, linked_drawing_ids. '
    'Use play_get_digest for a compact AI-optimised digest instead.',
    {
        'type': 'object',
        'properties': {
            'status': {
                'type': 'string',
                'enum': ['watching', 'acting', 'expired', 'completed', 'invalidated'],
                'description': (
                    'Filter to a single status. Omit to get all active plays '
                    '(watching + acting + expired). '
                    'watching=monitoring; acting=conditions met; expired=time-limit lapsed; '
                    'completed/invalidated=terminal.'
                ),
            },
            'asset': {
                'type': 'string',
                'description': 'Filter by primary asset symbol (e.g. BTC)',
            },
            'include_resolved': {
                'type': 'boolean',
                'description': (
                    'Also include completed and invalidated plays. '
                    'Ignored if status filter is already a terminal status. (default: false)'
                ),
            },
        },
    },
    min_tier='free',
)
def play_list(arguments):
    from carrac.plays.models import list_plays

    result = list_plays(
        user_id=get_mcp_user_id(),
        status=arguments.get('status'),
        asset=arguments.get('asset', '').upper() or None,
        include_resolved=arguments.get('include_resolved', False),
        limit=50,
    )
    return {'plays': result, 'count': len(result)}


@mcp_tool(
    'play_evaluate',
    'Read back the current condition evaluation state for a single play. '
    'Returns per-rule met/unmet status and evidence strings as last written by '
    'the background condition monitor (does NOT re-run the monitor live). '
    'Use after play_update to confirm updated conditions took effect, or to '
    'inspect why a play has or has not moved to acting status. '
    'Returns: play_id, title, status, conditions_met (X/N), per-rule met + evidence.',
    {
        'type': 'object',
        'properties': {
            'play_id': {
                'type': 'integer',
                'description': 'Play ID to inspect',
            },
        },
        'required': ['play_id'],
    },
    min_tier='free',
)
def play_evaluate(arguments):
    from carrac.plays.models import get_play

    play_id = arguments.get('play_id')
    if not play_id:
        raise ToolError('play_id is required')

    play = get_play(play_id, user_id=get_mcp_user_id())
    if not play:
        raise ToolError(f'Play {play_id} not found')

    # Return current condition status (conditions are auto-evaluated by the monitor)
    conditions = play.get('conditions') or {'rules': [], 'logic': 'all'}
    rules = conditions.get('rules', [])
    met_count = sum(1 for r in rules if r.get('met'))

    return {
        'play_id': play['id'],
        'title': play['title'],
        'status': play['status'],
        'conditions_met': f'{met_count}/{len(rules)}',
        'conditions': [
            {
                'type': r.get('type'),
                'met': r.get('met', False),
                'evidence': r.get('evidence', ''),
                'label': r.get('label', ''),
            }
            for r in rules
        ],
    }


@mcp_tool(
    'play_get_digest',
    'Return a compact, AI-optimised digest of all active (non-terminal) plays. '
    'Includes per-play condition summaries, met/unmet rule counts, and key metadata — '
    'formatted for minimal token consumption. No filters; covers all watching + acting + '
    'expired plays for the caller. '
    'Use play_list for structured JSON with full rule detail or to filter by status/asset.',
    {
        'type': 'object',
        'properties': {},
    },
    min_tier='free',
)
def play_get_digest(arguments):
    from carrac.plays.models import play_get_digest as _get_context
    from carrac.formatters import compact_plays

    result = _get_context(user_id=get_mcp_user_id())
    return compact_plays(result)


@mcp_tool(
    'play_update',
    'Update one or more fields on an existing play. '
    'Updatable fields: title, thesis, conditions (full replace of the rules block), '
    'invalidation, action_plan, direction (long/short), timeframe. '
    'Plays can be in any status — modify does NOT change status; use play_archive to '
    'transition to completed/invalidated. '
    'SIDE EFFECTS: persists to DB; condition monitor picks up new conditions on its '
    'next evaluation cycle (no immediate re-evaluation — call play_evaluate for that). '
    'EXAMPLE: {"play_id": 12, "thesis": "Updated view", "direction": "short"}.',
    {
        'type': 'object',
        'properties': {
            'play_id': {
                'type': 'integer',
                'description': 'ID of the play to update (required)',
            },
            'title': {
                'type': 'string',
                'description': 'New title for the play',
            },
            'thesis': {
                'type': 'string',
                'description': 'Updated thesis / trade rationale',
            },
            'conditions': {
                'type': 'object',
                'description': (
                    'Full replacement of the conditions block: '
                    '{rules: [{type, params, ...}], logic: "all"|"any"}. '
                    'See play_create for the full rule-type reference.'
                ),
            },
            'invalidation': {
                'type': 'object',
                'description': 'Updated invalidation criteria as JSON object',
            },
            'action_plan': {
                'type': 'string',
                'description': 'What to do when conditions are met',
            },
            'direction': {
                'type': 'string',
                'enum': ['long', 'short'],
                'description': 'Trade direction',
            },
            'timeframe': {
                'type': 'string',
                'description': 'Analysis timeframe (e.g. 1D, 4H)',
            },
        },
        'required': ['play_id'],
    },
    min_tier='free',
)
def play_update(arguments):
    from carrac.plays.models import update_play

    play_id = arguments.get('play_id')
    if not play_id:
        raise ToolError('play_id is required')

    # Extract only updatable fields
    fields = {}
    for key in ('title', 'thesis', 'conditions', 'invalidation',
                'action_plan', 'direction', 'timeframe'):
        if key in arguments and key != 'play_id':
            fields[key] = arguments[key]

    if not fields:
        raise ToolError('No fields to update')

    result = update_play(play_id, get_mcp_user_id(), **fields)
    if not result:
        raise ToolError(f'Play {play_id} not found or not owned by current user')

    return result


@mcp_tool(
    'play_delete',
    'Hard-delete one or more plays. '
    'Pass `play_id` for a single delete or `play_ids` for bulk (max 50). '
    'Use `dry_run` to preview without deleting. '
    'Set `cascade_drawings=true` to also delete the linked chart drawings. '
    'Hard delete — no undo (relies on the daily DB backup for recovery). '
    'Use play_archive for soft-archive (completed/invalidated) instead.',
    {
        'type': 'object',
        'properties': {
            'play_id': {
                'type': 'integer',
                'description': 'Single play ID to delete',
            },
            'play_ids': {
                'type': 'array',
                'items': {'type': 'integer'},
                'description': 'List of play IDs to delete (max 50)',
            },
            'dry_run': {
                'type': 'boolean',
                'description': 'If true, return previews without deleting (default false)',
            },
            'cascade_drawings': {
                'type': 'boolean',
                'description': (
                    'If true, also delete drawings listed in each play\'s linked_drawing_ids '
                    '(ownership-checked; default false)'
                ),
            },
        },
    },
    min_tier='free',  # user owns their own plays — same rationale as trade_delete
)
def play_delete(arguments):
    from carrac.plays.models import get_play, delete_plays_bulk

    BULK_CAP = 50

    raw_id = arguments.get('play_id')
    raw_ids = arguments.get('play_ids') or []
    dry_run = bool(arguments.get('dry_run', False))
    cascade_drawings = bool(arguments.get('cascade_drawings', False))

    ids = []
    if raw_id is not None:
        ids.append(int(raw_id))
    if raw_ids:
        if not isinstance(raw_ids, list):
            raise ToolError('play_ids must be an array of integers')
        try:
            ids.extend(int(i) for i in raw_ids)
        except (TypeError, ValueError):
            raise ToolError('play_ids must contain integers')
    # De-dupe while preserving order
    seen = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]

    if not ids:
        raise ToolError('Provide play_id or play_ids')
    if len(ids) > BULK_CAP:
        raise ToolError(f'Too many ids ({len(ids)}); max {BULK_CAP} per call')

    user_id = get_mcp_user_id()

    # Build previews from the user's own rows (skips foreign IDs silently)
    previews = []
    skipped = []
    for pid in ids:
        play = get_play(pid, user_id)
        if not play:
            skipped.append(pid)
            continue
        preview = {
            'play_id': pid,
            'title': play.get('title'),
            'primary_asset': play.get('primary_asset'),
            'status': play.get('status'),
            'direction': play.get('direction'),
            'created_at': play.get('created_at'),
        }
        if cascade_drawings:
            preview['linked_drawing_ids'] = play.get('linked_drawing_ids') or []
        previews.append(preview)

    if dry_run:
        result = {
            'dry_run': True,
            'would_delete': previews,
            'skipped': skipped,
            'count': len(previews),
        }
        if cascade_drawings:
            result['would_cascade_drawings_count'] = sum(
                len(p.get('linked_drawing_ids', [])) for p in previews
            )
        return result

    owned_ids = [p['play_id'] for p in previews]
    bulk_result = delete_plays_bulk(owned_ids, user_id, cascade_drawings=cascade_drawings)

    result = {
        'dry_run': False,
        'deleted_count': bulk_result['plays_deleted'],
        'deleted': previews,
        'skipped': skipped,
    }
    if cascade_drawings:
        # Report actual deletes from the cascade, not
        # len(linked_drawing_ids), which counts requested-not-deleted.
        result['cascaded_drawings_count'] = bulk_result['drawings_deleted']
    return result


@mcp_tool(
    'play_archive',
    'Transition a single play to a terminal status (completed or invalidated) with an '
    'optional resolution note. '
    'SIDE EFFECTS: persists status change + resolved_at timestamp to DB; play stops '
    'being evaluated by the condition monitor. '
    'DESTRUCTIVE: soft (reversible via play_update if needed, but normal flow is one-way '
    'into terminal state — no built-in undo button). '
    'State machine: watching/acting → completed or invalidated; expired → completed or '
    'invalidated. Already-terminal plays raise a ToolError (invalid transition). '
    'Use play_archive_bulk to archive multiple plays at once. '
    'EXAMPLE: {"play_id": 7, "action": "invalidated", "resolution": "Thesis broke on CPI print"}.',
    {
        'type': 'object',
        'properties': {
            'play_id': {
                'type': 'integer',
                'description': 'ID of the play to archive (required)',
            },
            'action': {
                'type': 'string',
                'enum': ['completed', 'invalidated'],
                'description': (
                    'Terminal status to set. completed = thesis played out as expected; '
                    'invalidated = thesis failed or was abandoned.'
                ),
            },
            'resolution': {
                'type': 'string',
                'description': 'Short note explaining the outcome (stored as resolved_reason)',
            },
        },
        'required': ['play_id', 'action'],
    },
    min_tier='free',
)
def play_archive(arguments):
    from carrac.plays.models import transition_status

    play_id = arguments.get('play_id')
    action = arguments.get('action')
    if not play_id:
        raise ToolError('play_id is required')
    if action not in ('completed', 'invalidated'):
        raise ToolError('action must be "completed" or "invalidated"')

    result = transition_status(
        play_id, get_mcp_user_id(), action,
        resolution=arguments.get('resolution'),
    )
    if not result:
        raise ToolError(f'Play {play_id} not found or invalid status transition')

    return result


# =============================================================================
# play_archive_bulk
# =============================================================================
@mcp_tool(
    'play_archive_bulk',
    'Archive (complete or invalidate) up to 50 plays in a single call. '
    'Each play is transitioned individually — plays that are already terminal, '
    'not owned by the caller, or have an invalid transition are skipped and '
    'reported separately. '
    'SIDE EFFECTS: same as play_archive × N — each archived play gets resolved_at '
    'written and stops being evaluated by the condition monitor. '
    'DESTRUCTIVE: soft (reversible; no built-in undo but DB backup available). '
    'Use dry_run=true to preview which plays would be affected before committing. '
    'Use play_archive for a single play with more explicit error handling.',
    {
        'type': 'object',
        'properties': {
            'play_ids': {
                'type': 'array',
                'items': {'type': 'integer'},
                'description': 'List of play IDs to archive (max 50)',
            },
            'action': {
                'type': 'string',
                'enum': ['completed', 'invalidated'],
                'description': 'Terminal status to set for all plays',
            },
            'resolution': {
                'type': 'string',
                'description': 'Optional resolution note applied to all archived plays',
            },
            'dry_run': {
                'type': 'boolean',
                'description': (
                    'If true, validate which plays would be affected but do not '
                    'write any changes (default: false)'
                ),
            },
        },
        'required': ['play_ids', 'action'],
    },
    min_tier='free',
)
def play_archive_bulk(arguments):
    from carrac.plays.models import transition_status, get_play, TRANSITIONS

    user_id = get_mcp_user_id()

    raw_ids = arguments.get('play_ids')
    if not raw_ids or not isinstance(raw_ids, list):
        raise ToolError('play_ids must be a non-empty array')
    if len(raw_ids) > 50:
        raise ToolError('Maximum 50 play IDs per call')

    try:
        play_ids = [int(pid) for pid in raw_ids]
    except (TypeError, ValueError):
        raise ToolError('play_ids must contain integers')

    # De-duplicate while preserving order
    seen = set()
    play_ids = [pid for pid in play_ids if not (pid in seen or seen.add(pid))]

    action = arguments.get('action')
    if action not in ('completed', 'invalidated'):
        raise ToolError('action must be "completed" or "invalidated"')

    resolution = arguments.get('resolution') or None
    dry_run = bool(arguments.get('dry_run', False))

    results = []
    archived_count = 0
    skipped_count = 0

    for play_id in play_ids:
        play = get_play(play_id, user_id)
        if not play:
            results.append({
                'play_id': play_id,
                'skipped': 'not found or not owned by caller',
            })
            skipped_count += 1
            continue

        current_status = play.get('status', '')
        valid_next = TRANSITIONS.get(current_status, set())
        if action not in valid_next:
            results.append({
                'play_id': play_id,
                'skipped': f'invalid transition: {current_status} → {action}',
            })
            skipped_count += 1
            continue

        if dry_run:
            results.append({
                'play_id': play_id,
                'would_archive': action,
                'current_status': current_status,
            })
            archived_count += 1
            continue

        updated = transition_status(play_id, user_id, action, resolution=resolution)
        if updated:
            results.append({'play_id': play_id, 'status': action})
            archived_count += 1
        else:
            results.append({'play_id': play_id, 'skipped': 'transition failed unexpectedly'})
            skipped_count += 1

    return {
        'requested': len(play_ids),
        'archived': archived_count,
        'skipped': skipped_count,
        'dry_run': dry_run,
        'action': action,
        'results': results,
    }


# No longer a standalone MCP tool — absorbed into share_manage
# (entity_type=play, action=share). Kept as a plain helper, called from
# carrac.mcp.tools.sharing. The legacy 'share_play' tool name still
# dispatches via registry.LEGACY_TOOL_ALIASES + LEGACY_ARG_DEFAULTS.
#
# Mints (or fetches) the PUBLIC share link for one of the user's plays.
# Anyone can open the returned URL with no Carrac account: the play is
# FROZEN into an immutable snapshot at mint time (later edits never leak),
# showing only the opted-in sections. Calling again returns the same active
# link; passing toggles re-freezes the snapshot from the play's CURRENT
# state with those toggles. Toggle defaults: thesis/conditions/plan ON,
# outcome OFF (outcome = resolution note + terminal status, only rendered
# once the play is completed/invalidated). SIDE EFFECTS: creates a public
# web page + og:image card at the returned URL; counts against the per-tier
# active-share quota when minting a NEW link.
def share_play(arguments):

    from carrac.mcp.runtime import get_mcp_caller_tier
    from carrac.plays.shares import (
        count_active_shares,
        create_share,
        get_active_share_for_play,
        share_quota,
    )

    user_id = get_mcp_user_id()
    play_id = arguments.get('play_id')
    if not play_id:
        raise ToolError('play_id is required')

    # Quota only charges NEW mints — re-sharing returns the existing link.
    if get_active_share_for_play(play_id, user_id) is None:
        tier = get_mcp_caller_tier()
        quota = share_quota(tier)
        current = count_active_shares(user_id)
        if current >= quota:
            raise ToolError(
                f'Share limit reached ({current}/{quota} active links on the '
                f'{tier} tier). Revoke an old share or upgrade for more.')

    toggles = {k: arguments[k] for k in (
        'include_thesis', 'include_conditions', 'include_plan', 'include_outcome',
    ) if k in arguments}
    share = create_share(user_id, play_id, **toggles)
    if not share:
        raise ToolError(f'Play {play_id} not found')

    base = site_base_url()
    return {
        'slug': share['slug'],
        'url': f"{base}/s/{share['slug']}",
        'image_url': f"{base}/s/{share['slug']}/card.png",
        'include_thesis': share.get('include_thesis'),
        'include_conditions': share.get('include_conditions'),
        'include_plan': share.get('include_plan'),
        'include_outcome': share.get('include_outcome', False),
        'view_count': share.get('view_count', 0),
    }


# No longer a standalone MCP tool — absorbed into share_manage
# (entity_type=play, action=unshare). Revokes the public share link(s) for
# one of the user's plays. SIDE EFFECTS: the public URL immediately returns
# 410 Gone; the freed slot returns to the per-tier active-share quota. The
# slug is NOT reusable — a later share re-mints a fresh link.
def unshare_play(arguments):
    from carrac.plays.shares import revoke_share

    play_id = arguments.get('play_id')
    if not play_id:
        raise ToolError('play_id is required')

    revoked = revoke_share(play_id, get_mcp_user_id())
    return {'play_id': play_id, 'revoked': revoked}
