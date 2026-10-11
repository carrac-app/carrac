"""
Plays Blueprint — Thesis-first market belief tracking.

Read endpoints: list, detail, backtest preview and AI context. Every write
(create, update, status, drawing links, evaluate, custom backtest) is served
by carrac/fastapi_app/routers/plays.py, which reuses the validation and
backtest helpers defined here.
"""

import logging
import re

from flask import Blueprint, g, jsonify, request, session

from carrac.auth.decorators import (
    auth_or_key_required, verify_api_key,
)
from carrac.extensions import limiter
from carrac.openapi.spec_decorator import spec
from carrac.plays.models import (
    PLAY_TYPES, PLAY_APPROACHES, PLAY_DIRECTIONS, PLAY_TIMEFRAMES, get_play, list_plays, play_get_digest,
)

logger = logging.getLogger('carrac.plays')

plays_bp = Blueprint('plays', __name__)


# ── Validation ──

_SAFE_ASSET_RE = re.compile(r'^[A-Z0-9_.]{1,20}$')
_MAX_TITLE_LEN = 200
_MAX_TEXT_LEN = 5000
_MAX_LINKED_DRAWINGS = 20


def _get_user_id():
    """Resolve caller's user_id — per-user API key first, then session.

    Returns None for shared-admin-key callers (no g.api_key_user_id, no
    session); downstream endpoints gate on that explicitly.
    """
    uid = getattr(g, 'api_key_user_id', None)
    if uid is not None:
        return uid
    return session.get('user_id')


def _validate_play_input(data, is_update=False):
    """Validate play create/update input. Returns (cleaned_data, error_msg)."""
    cleaned = {}
    errors = []

    # Title (required on create)
    title = data.get('title')
    if title is not None:
        title = str(title).strip()[:_MAX_TITLE_LEN]
        if not title:
            errors.append('Title is required')
        cleaned['title'] = title
    elif not is_update:
        errors.append('Title is required')

    # Thesis
    thesis = data.get('thesis')
    if thesis is not None:
        cleaned['thesis'] = str(thesis).strip()[:_MAX_TEXT_LEN] or None

    # Type
    play_type = data.get('type')
    if play_type is not None:
        if play_type not in PLAY_TYPES:
            errors.append(f'Invalid type: {play_type}. Must be one of: {", ".join(sorted(PLAY_TYPES))}')
        else:
            cleaned['type'] = play_type

    # Approach
    approach = data.get('approach')
    if approach is not None:
        if approach not in PLAY_APPROACHES:
            errors.append(f'Invalid approach: {approach}. Must be one of: {", ".join(sorted(PLAY_APPROACHES))}')
        else:
            cleaned['approach'] = approach

    # Direction
    direction = data.get('direction')
    if direction is not None:
        if direction not in PLAY_DIRECTIONS:
            errors.append(f'Invalid direction: {direction}')
        else:
            cleaned['direction'] = direction

    # Primary asset
    asset = data.get('primary_asset')
    if asset is not None:
        asset = str(asset).strip().upper()
        if asset and not _SAFE_ASSET_RE.match(asset):
            errors.append('Invalid primary_asset format')
        cleaned['primary_asset'] = asset if asset else None

    # Related assets
    related = data.get('related_assets')
    if related is not None:
        if not isinstance(related, list):
            errors.append('related_assets must be an array')
        else:
            valid = []
            for a in related[:10]:
                a = str(a).strip().upper()
                if a and _SAFE_ASSET_RE.match(a):
                    valid.append(a)
            cleaned['related_assets'] = valid

    # Trigger asset
    trigger = data.get('trigger_asset')
    if trigger is not None:
        trigger = str(trigger).strip().upper()
        if trigger and not _SAFE_ASSET_RE.match(trigger):
            errors.append('Invalid trigger_asset format')
        cleaned['trigger_asset'] = trigger if trigger else None

    # Conditions (validate structure + deep rule validation)
    conditions = data.get('conditions')
    if conditions is not None:
        if not isinstance(conditions, dict):
            errors.append('conditions must be an object')
        else:
            rules = conditions.get('rules', [])
            if not isinstance(rules, list):
                errors.append('conditions.rules must be an array')
            elif len(rules) > 30:
                errors.append('Maximum 30 condition rules')
            logic = conditions.get('logic', 'all')
            if logic not in ('all', 'any', 'custom'):
                errors.append('conditions.logic must be all, any, or custom')
            if not errors:
                from carrac.conditions import validate_conditions, validate_condition
                ok, cond_err = validate_conditions(conditions)
                if not ok:
                    errors.append(f'invalid conditions: {cond_err}')
                else:
                    cleaned['conditions'] = conditions

    # Invalidation (single condition object)
    invalidation = data.get('invalidation')
    if invalidation is not None:
        if invalidation and not isinstance(invalidation, dict):
            errors.append('invalidation must be an object or null')
        else:
            if invalidation and not errors:
                from carrac.conditions import validate_condition
                ok, inv_err = validate_condition(invalidation)
                if not ok:
                    errors.append(f'invalid invalidation: {inv_err}')
                else:
                    cleaned['invalidation'] = invalidation
            else:
                cleaned['invalidation'] = invalidation

    # Timeframe
    timeframe = data.get('timeframe')
    if timeframe is not None:
        if timeframe and timeframe not in PLAY_TIMEFRAMES:
            errors.append(f'Invalid timeframe: {timeframe}')
        else:
            cleaned['timeframe'] = timeframe if timeframe else None

    # Expiry — validate ISO format
    expiry = data.get('expiry')
    if expiry is not None:
        if expiry:
            from datetime import datetime as _dt
            try:
                _dt.fromisoformat(str(expiry).replace('Z', '+00:00'))
                cleaned['expiry'] = expiry
            except (ValueError, TypeError):
                errors.append('Invalid expiry date format (expected ISO 8601)')
        else:
            cleaned['expiry'] = None

    # Action plan
    action_plan = data.get('action_plan')
    if action_plan is not None:
        cleaned['action_plan'] = str(action_plan).strip()[:_MAX_TEXT_LEN] or None

    # Linked drawing IDs
    linked = data.get('linked_drawing_ids')
    if linked is not None:
        if not isinstance(linked, list):
            errors.append('linked_drawing_ids must be an array')
        elif len(linked) > _MAX_LINKED_DRAWINGS:
            errors.append(f'Maximum {_MAX_LINKED_DRAWINGS} linked drawings')
        else:
            try:
                cleaned['linked_drawing_ids'] = [int(x) for x in linked]
            except (ValueError, TypeError):
                errors.append('linked_drawing_ids must contain integers')

    # Report tracking
    report_tracking = data.get('report_tracking')
    if report_tracking is not None:
        cleaned['report_tracking'] = bool(report_tracking)

    # Scorer snapshot (read-only on create, not updatable via general update)
    scorer_snapshot = data.get('scorer_snapshot')
    if scorer_snapshot is not None and not is_update:
        if isinstance(scorer_snapshot, dict):
            cleaned['scorer_snapshot'] = scorer_snapshot

    # Metadata (trade fields, milestones, etc.)
    metadata = data.get('metadata')
    if metadata is not None:
        if not isinstance(metadata, dict):
            errors.append('metadata must be an object')
        else:
            import json as _json
            if len(_json.dumps(metadata)) > 10240:
                errors.append('metadata too large (max 10KB)')
            else:
                cleaned['metadata'] = metadata

    if errors:
        return None, '; '.join(errors)
    return cleaned, None


# ── Endpoints ──

@plays_bp.route('/api/plays', methods=['GET'])
@spec(
    summary='List plays',
    description='Return the caller\'s plays, optionally filtered by status/asset/type.',
    tags=['Plays'],
    responses={200: 'PlayList', 401: 'Error'},
    parameters=[
        {'name': 'status', 'in': 'query', 'schema': {'type': 'string'}},
        {'name': 'asset', 'in': 'query', 'schema': {'type': 'string'}},
        {'name': 'type', 'in': 'query', 'schema': {'type': 'string'}},
        {'name': 'include_resolved', 'in': 'query', 'schema': {'type': 'boolean'}},
        {'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'default': 50}},
        {'name': 'offset', 'in': 'query', 'schema': {'type': 'integer', 'default': 0}},
    ],
    min_tier='member',
)
@auth_or_key_required
def list_plays_endpoint():
    user_id = _get_user_id()
    if user_id is None:
        return jsonify({'error': 'Authentication required'}), 401
    status = request.args.get('status')
    asset = request.args.get('asset')
    play_type = request.args.get('type')
    include_resolved = request.args.get('include_resolved', '').lower() in ('true', '1')
    try:
        limit = min(int(request.args.get('limit', 50)), 200)
        offset = max(int(request.args.get('offset', 0)), 0)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid limit or offset parameter'}), 400

    plays = list_plays(
        user_id,
        status=status,
        asset=asset.upper() if asset else None,
        play_type=play_type,
        include_resolved=include_resolved,
        limit=limit,
        offset=offset,
    )
    return jsonify({'plays': plays, 'count': len(plays)})


@plays_bp.route('/api/plays/<int:play_id>', methods=['GET'])
@spec(
    summary='Get play by id',
    tags=['Plays'],
    responses={200: 'Play', 404: 'Error'},
    min_tier='member',
)
@auth_or_key_required
def get_play_endpoint(play_id):
    user_id = _get_user_id()
    if user_id is None:
        return jsonify({'error': 'Authentication required'}), 401
    play = get_play(play_id, user_id)
    if not play:
        return jsonify({'error': 'Play not found'}), 404
    return jsonify({'play': play})


# ── Backtest ────────────────────────────────────────────────

_BACKTEST_DEFAULT_LOOKBACK = 365
_BACKTEST_DEFAULT_HOLD = 5


def _run_play_backtest(play, lookback_bars, hold_bars):
    """Synthesize play conditions → formula, run flip-signal (Hold-N) backtest.

    Returns a dict suitable for the evidence badge, or {'error': str}.
    Reuses the 6h Redis cache of backtest_indicator_formula.
    """
    from carrac.mcp.tools.backtest import synthesize_play_formula
    from carrac.mcp.tools.indicators import backtest_indicator_formula
    from carrac.mcp.protocol import ToolError

    symbol = (play.get('primary_asset') or '').upper().strip()
    if not symbol:
        return {'error': 'Play has no primary_asset'}

    expression, supported, unsupported = synthesize_play_formula(play.get('conditions'))
    if not expression:
        return {
            'error': 'No backtestable conditions',
            'conditions_skipped': unsupported,
        }

    timeframe = (play.get('timeframe') or '1D').strip()
    if timeframe in {'swing', 'position', 'until_event', 'open_ended'}:
        timeframe = '1D'

    try:
        bt = backtest_indicator_formula({
            'symbol': symbol,
            'expression': expression,
            'timeframe': timeframe,
            'lookback_bars': lookback_bars,
            'hold_bars': hold_bars,
        })
    except ToolError as e:
        return {'error': str(e)}
    except Exception as e:
        logger.warning(f"play backtest failed for play {play.get('id')}: {e}")
        return {'error': 'Backtest failed'}

    return {
        'model': bt.get('model'),
        'symbol': symbol,
        'timeframe': timeframe,
        'expression': expression,
        'conditions_used': supported,
        'conditions_skipped': unsupported,
        'trades': bt.get('trades', 0),
        'hit_rate': bt.get('hit_rate'),
        'mean_return': bt.get('mean_return'),
        'median_return': bt.get('median_return'),
        'sharpe_like': bt.get('sharpe_like'),
        'max_drawdown': bt.get('max_drawdown'),
        'final_equity_multiple': bt.get('final_equity_multiple'),
        'lookback_bars': bt.get('lookback_bars', lookback_bars),
        'hold_bars': bt.get('hold_bars', hold_bars),
        'cached': bt.get('_cached', False),
        'message': bt.get('message'),
    }


@plays_bp.route('/api/plays/<int:play_id>/backtest', methods=['GET'])
@spec(
    summary='Hold-N backtest of play conditions (cached, default params)',
    description='Synthesize the play\'s conditions into a boolean formula and run a '
                'flip-signal (Hold-N) backtest on the primary asset with default '
                '365-bar lookback / 5-bar hold. Any tier. Reuses the 6h Redis cache '
                'of backtest_indicator_formula so repeated reads are cheap.',
    tags=['Plays'],
    min_tier='free',
    operation_id='plays_backtest_get',
)
@limiter.limit("10 per minute")
@auth_or_key_required
def backtest_play_get(play_id):
    user_id = _get_user_id()
    if user_id is None:
        return jsonify({'error': 'Authentication required'}), 401
    play = get_play(play_id, user_id)
    if not play:
        return jsonify({'error': 'Play not found'}), 404
    result = _run_play_backtest(play, _BACKTEST_DEFAULT_LOOKBACK, _BACKTEST_DEFAULT_HOLD)
    if 'error' in result:
        return jsonify(result), (400 if 'No backtestable' in result['error'] else 500)
    return jsonify(result), 200


@plays_bp.route('/api/plays/context', methods=['GET'])
@spec(
    summary='Plays context for AI',
    description='Compact summary of active plays for AI briefing / morning reports.',
    tags=['Plays'],
    responses={200: {'description': 'Compact context object'}},
    min_tier='member',
)
@auth_or_key_required
def plays_context_endpoint():
    user_id = _get_user_id()
    if user_id is None:
        # API key access — return all users' context (for morning engine)
        if not verify_api_key(request.headers.get('X-Internal-Key') or request.headers.get('X-API-Key')):
            return jsonify({'error': 'Authentication required'}), 401
    context = play_get_digest(user_id)
    from carrac.formatters import maybe_compact, compact_plays
    return jsonify(maybe_compact(context, compact_plays))
