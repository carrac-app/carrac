"""
AI assistant routes: /api/ai/balance, /api/ai/journal, /api/ai/pattern-match,
recipes, conversations, trading-profile.

Migration note: /api/ai/chat, /api/ai/weekly-review,
/api/ai/execute-action and /api/ai/context-preview are served by the FastAPI
sub-app (carrac/fastapi_app/routers/ai.py + ai_aux.py). The constants
below (VALID_FEATURES / VALID_TF / ASSET_RE / _rate_limit_key_by_user)
remain here because the FastAPI router imports them as the canonical
definition.
"""

import json
import logging
import re

from flask import Blueprint, jsonify, request, session

from flask import session as _flask_session
from carrac.auth.decorators import login_required, auth_or_key_required
from carrac.journal.routes import caller_id, journal_auth_required
from carrac.json_response import jsonify_iso
from flask_limiter.util import get_remote_address

logger = logging.getLogger('carrac.ai')


def _rate_limit_key_by_user() -> str:
    """Key the AI rate limit on user_id, not remote IP.

    Mobile carriers (and many NATs) multiplex thousands of users
    behind a single IP, causing legitimate users to trip each other's limits.
    All /api/ai/* endpoints require login, so user_id is always present.
    """
    uid = _flask_session.get('user_id')
    if uid:
        return f"user:{uid}"
    # Fallback to IP for any unauthenticated edge-case (shouldn't reach here
    # since @login_required fires first, but belt-and-suspenders).
    return get_remote_address()

ai_bp = Blueprint('ai', __name__)


VALID_FEATURES = frozenset({
    'chat', 'viewport', 'validate', 'challenge', 'checklist', 'confluence',
    'trade_design', 'report_followup', 'news_analysis',
})
VALID_TF = frozenset({
    '1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h',
    '1D', '3D', '1W', '1M',
})
ASSET_RE = re.compile(r'^[A-Z0-9_]{1,20}$')



@ai_bp.route('/api/ai/balance')
@login_required
def ai_balance():
    """Return user's credit balance."""
    from user_db import user_db
    balance = user_db.get_ai_balance(session['user_id'])
    return jsonify({
        'balance_cents': balance,
        'balance': f'${balance / 100:.2f}',
    })


@ai_bp.route('/api/ai/assets')
@login_required
def ai_assets():
    """Return available assets for the UI selectors."""
    from carrac.ai.context_builder import get_asset_list
    assets = get_asset_list()
    return jsonify({'assets': assets, 'scenarios': []})


@ai_bp.route('/api/ai/conversations', methods=['GET'])
@login_required
def list_conversations():
    """List user's recent AI conversations."""
    from user_db import user_db
    convs = user_db.get_conversations(session['user_id'], limit=20)
    return jsonify({'conversations': convs})


@ai_bp.route('/api/ai/conversations', methods=['POST'])
@login_required
def create_conversation():
    """Create a new AI conversation with optional metadata."""
    from user_db import user_db
    data = request.get_json() or {}
    primary_asset = (data.get('primary_asset') or '')[:20] or None
    timeframe = (data.get('timeframe') or '')[:10] or None
    page_context = (data.get('page_context') or '')[:30] or None
    conv_id = user_db.create_conversation(
        session['user_id'], primary_asset=primary_asset,
        timeframe=timeframe, page_context=page_context,
    )
    return jsonify({'id': conv_id})


@ai_bp.route('/api/ai/conversations/<int:conv_id>/messages', methods=['GET'])
@login_required
def get_conversation_messages(conv_id):
    """Get messages for a conversation."""
    from user_db import user_db
    msgs = user_db.get_messages(conv_id, session['user_id'], limit=50)
    return jsonify({'messages': msgs})


@ai_bp.route('/api/ai/conversations/<int:conv_id>', methods=['DELETE'])
@login_required
def delete_conversation(conv_id):
    """Delete a conversation."""
    from user_db import user_db
    ok = user_db.delete_conversation(conv_id, session['user_id'])
    return jsonify({'ok': ok})


@ai_bp.route('/api/ai/quick-score')
@login_required
def ai_quick_score():
    """Return deterministic score for an asset (no LLM, no billing)."""
    from carrac.scanner.scorer import get_quick_score

    asset = (request.args.get('asset') or '').upper()
    if not asset or not ASSET_RE.match(asset):
        return jsonify({'error': 'Valid asset symbol required'}), 400

    direction = request.args.get('direction')
    if direction and direction not in ('long', 'short'):
        direction = None

    result = get_quick_score(asset, direction)
    if not result:
        return jsonify({'error': 'Asset not found in cache'}), 404

    return jsonify(result)



@ai_bp.route('/api/ai/pattern-match', methods=['POST'])
@login_required
def ai_pattern_match():
    """Find similar historical patterns for the current chart."""
    from carrac.ai.billing import check_and_reserve, settle
    from carrac.ai.pattern_match import find_similar_patterns
    from carrac.auth.decorators import get_request_tier, get_tier_limits

    user_id = session.get('user_id')

    # ── Tier & daily cap checks ──
    limits = get_tier_limits(get_request_tier())
    daily_limit = limits.get('pattern_match_daily', 0)
    if daily_limit == 0:
        return jsonify({'error': 'Pattern match requires a subscription',
                        'upgrade_required': True}), 403
    if daily_limit < 99:
        from user_db import user_db as _udb
        today_count = _udb.get_pattern_match_count_today(user_id)
        if today_count >= daily_limit:
            return jsonify({'error': f'Daily pattern match limit reached ({today_count}/{daily_limit})',
                            'limit_type': 'pattern_daily'}), 429

    data = request.get_json() or {}

    asset = (data.get('asset') or '').upper()
    if not asset or not ASSET_RE.match(asset):
        return jsonify({'error': 'Asset is required'}), 400

    timeframe = data.get('timeframe', '1D')
    if timeframe not in VALID_TF:
        timeframe = '1D'

    window_size = min(max(int(data.get('window_size', 20)), 10), 60)

    # Pattern match is cheap (no LLM) — charge 2 cents
    if not check_and_reserve(user_id, 2):
        return jsonify({'error': 'Insufficient credits', 'need_credits': True}), 402

    from user_db import user_db
    try:
        result = find_similar_patterns(
            asset=asset, timeframe=timeframe, window_size=window_size,
        )
    except Exception as e:
        logger.error(f"Pattern match failed for {asset}: {e}")
        user_db.refund_ai_credits(user_id, 2)
        return jsonify({'error': 'Pattern match failed', 'balance': user_db.get_ai_balance(user_id)}), 500

    # Settle at fixed 2 cents (no LLM tokens)
    settle(user_id, 2, 0, 0, feature='pattern_match', asset=asset, model='local')

    result['balance'] = user_db.get_ai_balance(user_id)
    return jsonify(result)


@ai_bp.route('/api/ai/journal', methods=['GET'])
@journal_auth_required
def ai_journal():
    """DEPRECATED — proxies to /api/journal/trades. Use new endpoint."""
    from carrac.content_db import get_content_db

    user_id = caller_id()
    db = get_content_db()

    try:
        days = min(int(request.args.get('days', 30)), 365)
    except (ValueError, TypeError):
        days = 30
    open_trades = db.get_open_trades(user_id)
    history = db.get_trade_history(user_id, days=days)
    stats = db.get_journal_stats(user_id, days=days)

    return jsonify_iso({
        'open': open_trades,
        'history': history,
        'stats': stats,
    })


@ai_bp.route('/api/ai/journal/asset-history', methods=['GET'])
@journal_auth_required
def ai_journal_asset_history():
    """Get trades for a specific asset (open + closed)."""
    from carrac.content_db import get_content_db

    user_id = caller_id()
    asset = request.args.get('asset', '').strip().upper()
    if not asset or not asset.isalnum():
        return jsonify_iso({'trades': []})

    db = get_content_db()
    try:
        days = min(int(request.args.get('days', 365)), 3650)
    except (ValueError, TypeError):
        days = 365
    try:
        limit = min(int(request.args.get('limit', 10)), 50)
    except (ValueError, TypeError):
        limit = 10

    open_trades = db.get_open_trades(user_id, asset=asset)
    closed_trades = db.get_trade_history(user_id, days=days, limit=limit, asset=asset)

    return jsonify_iso({'trades': open_trades + closed_trades})


@ai_bp.route('/api/ai/trading-profile', methods=['GET'])
@login_required
def ai_get_trading_profile():
    """Get user's trading profile."""
    from user_db import user_db
    prefs = user_db.get_preferences(session['user_id'])
    return jsonify(prefs.get('trading_profile', {}))


@ai_bp.route('/api/ai/trading-profile', methods=['POST'])
@login_required
def ai_save_trading_profile():
    """Save user's trading profile (v2 — extended fields)."""
    from user_db import user_db
    user_id = session['user_id']
    data = request.get_json() or {}

    # --- Validation sets ---
    VALID_STYLES = {'hodl', 'swing', 'day', 'scalp'}
    VALID_SETUPS = {'breakout', 'pullback', 'reversal', 'range', 'trend_follow', 'mean_revert'}
    VALID_RISK = {'conservative', 'moderate', 'aggressive'}
    VALID_FOCUS = {'crypto', 'stocks', 'both'}
    VALID_EXP = {'beginner', 'intermediate', 'advanced'}
    VALID_HOLDING = {'minutes', 'hours', 'days_to_weeks', 'weeks_to_months', 'months_plus'}
    VALID_CHECK_FREQ = {'constantly', 'few_times_daily', 'daily', 'few_times_weekly'}
    VALID_ENTRY_CONDITIONS = {
        'htf_trend_aligned', 'key_level_test', 'indicator_confluence',
        'volume_confirmation', 'pullback_to_ma', 'breakout_retest',
        'divergence', 'funding_rate_extreme', 'sentiment_extreme',
        'catalyst_required', 'pattern_completion',
    }
    VALID_EXIT_STRATEGY = {
        'fixed_target', 'trailing_stop', 'indicator_signal', 'time_based',
        'scale_out', 'invalidation', 'technical_level_target', 'momentum_exit',
    }
    VALID_ENTRY_TIMING = {'early', 'standard', 'late', 'scaling', 'depends'}
    VALID_CATALYST_DEP = {'must_have', 'prefer_not_required', 'confirmation_only', 'ignore', 'avoid_news'}
    VALID_STOP_METHOD = {'technical_level', 'percentage', 'atr_multiple', 'structure_invalidation', 'no_stop', 'varies'}
    VALID_RR = {'1_to_1', '1.5_to_1', '2_to_1', '3_to_1', '5_plus'}
    VALID_RISK_PER_TRADE = {'under_half_pct', 'half_to_1_pct', '1_to_2_pct', '2_to_3_pct', '3_to_5_pct', 'over_5_pct', 'no_fixed'}
    VALID_MAX_DD = {'under_5', '5_to_10', '10_to_20', '20_to_50', 'any'}
    VALID_POS_SIZING = {'fixed_pct', 'kelly', 'equal_weight', 'conviction_based', 'volatility_adjusted', 'gut'}
    VALID_MAX_POSITIONS = {'1_to_2', '3_to_5', '6_to_10', 'unlimited'}
    VALID_POS_SCALING = {'scale_in', 'scale_out', 'add_to_winners', 'reduce_after_loss', 'trail_stops'}
    VALID_LOSS_RESPONSE = {'stop_trading', 'reduce_and_continue', 'continue_normal', 'get_aggressive', 'take_break', 'review_method'}
    VALID_INDICATOR_USAGE = {'primary_trigger', 'confirmation', 'divergence_detection', 'trend_filter', 'noise_filter', 'dont_use'}
    VALID_TRUSTED_PATTERNS = {
        'higher_lows', 'double_bottom', 'head_shoulders', 'bull_flag',
        'ascending_triangle', 'divergence_pattern', 'wyckoff', 'cup_handle',
        'wedge', 'range_sr', 'bollinger_squeeze',
    }
    VALID_PATTERN_STRICTNESS = {'strict_textbook', 'flexible_with_confluence', 'aggressive_on_intent', 'case_by_case'}
    VALID_TREND_APPROACH = {'trend_only', 'counter_trend_ranging', 'both_directions', 'trend_as_filter', 'no_preference'}
    VALID_VOL_ADJUST = {'reduce_size', 'widen_stops', 'reduce_frequency', 'no_adjustment', 'stop_trading', 'proportional_atr'}
    VALID_REGIME = {'active_adjust', 'recognize_no_adjust', 'aware_not_systematic', 'setup_focused', 'want_to_learn'}
    VALID_CORRELATION = {'track_portfolio_heat', 'limit_correlated', 'limit_total_positions', 'no_management', 'depends'}
    VALID_SCENARIO_STYLE = {'level_based', 'pattern_based', 'catalyst_based', 'conditional_chain'}
    VALID_FUND_BALANCE = {'primarily_fundamental', 'balanced', 'primarily_technical', 'completely_technical', 'context_dependent'}
    VALID_INSTRUMENT_SEL = {'predetermined_list', 'volatility_rotation', 'trend_strength', 'catalyst_driven', 'scan_universe', 'setup_driven'}
    VALID_ANALYSIS_DEPTH = {'deep_multi_tf', 'moderate_key_levels', 'minimal_signals', 'alerts_only'}
    VALID_EMOTIONAL = {
        'hesitation_on_setups', 'fomo_early_entry', 'revenge_trading',
        'overconfidence_after_wins', 'overtrading_frustration', 'holding_losers',
        'watching_winners_reverse', 'moving_stops',
    }
    VALID_ROUTINE = {'very_structured', 'somewhat_structured', 'minimal', 'no_routine', 'varies'}
    VALID_STRESS = {'push_through', 'reduce_rebuild', 'take_break', 'review_methodology', 'panic', 'maintain_consistent'}
    VALID_BIAS = {'strongly_bearish', 'bearish', 'neutral', 'cautiously_bullish', 'bullish', 'strongly_bullish'}
    VALID_BIAS_HORIZON = {'short_term', 'medium_term', 'long_term'}
    VALID_HODL_STRATEGY = {'dca', 'lump_sum', 'buy_dips', 'accumulate_range'}
    VALID_AI_VERBOSITY = {'concise', 'balanced', 'detailed'}
    VALID_REPORT_FOCUS = {
        'macro_context', 'confluence_zones', 'scenario_tracking',
        'devil_advocate', 'pattern_detection', 'regime_analysis',
    }
    VALID_SESSIONS = {'asian', 'london', 'us_morning', 'us_midday', 'us_close', 'all'}

    MAX_MULTI = 15  # sanity cap on multi-select fields
    MAX_TEXT = 1200

    def _multi(key, valid):
        return [v for v in data.get(key, []) if v in valid][:MAX_MULTI]

    def _single(key, valid, default=''):
        v = data.get(key, '')
        return v if v in valid else default

    def _text(key, maxlen=MAX_TEXT):
        v = data.get(key, '')
        return v[:maxlen] if v else ''

    # --- Build profile (v1 fields + v2 extensions) ---
    profile = {}

    # Section 1: Core Identity (v1)
    profile['styles'] = _multi('styles', VALID_STYLES)
    profile['setups'] = _multi('setups', VALID_SETUPS)
    profile['risk'] = _single('risk', VALID_RISK, 'moderate')
    profile['focus'] = _single('focus', VALID_FOCUS, 'both')
    profile['experience'] = _single('experience', VALID_EXP, 'intermediate')
    profile['notes'] = _text('notes')

    # Section 2: Timeframe & Horizon
    valid_tfs = {'1m', '5m', '15m', '30m', '1H', '4H', '1D', '3D', '1W', '1M'}
    profile['primary_timeframes'] = [t for t in data.get('primary_timeframes', []) if t in valid_tfs][:5]
    profile['holding_period'] = _single('holding_period', VALID_HOLDING)
    profile['check_frequency'] = _single('check_frequency', VALID_CHECK_FREQ)
    profile['trading_sessions'] = _multi('trading_sessions', VALID_SESSIONS)

    # Section 3: Entry Methodology
    profile['entry_conditions'] = _multi('entry_conditions', VALID_ENTRY_CONDITIONS)
    profile['entry_timing'] = _single('entry_timing', VALID_ENTRY_TIMING)
    profile['catalyst_dependency'] = _single('catalyst_dependency', VALID_CATALYST_DEP)
    profile['entry_notes'] = _text('entry_notes', 300)

    # Section 4: Exit & Risk Management
    profile['exit_strategy'] = _multi('exit_strategy', VALID_EXIT_STRATEGY)
    profile['stop_loss_method'] = _single('stop_loss_method', VALID_STOP_METHOD)
    profile['typical_rr'] = _single('typical_rr', VALID_RR)
    profile['risk_per_trade'] = _single('risk_per_trade', VALID_RISK_PER_TRADE)
    profile['max_drawdown_comfort'] = _single('max_drawdown_comfort', VALID_MAX_DD)
    profile['position_sizing'] = _single('position_sizing', VALID_POS_SIZING)
    profile['max_concurrent_positions'] = _single('max_concurrent_positions', VALID_MAX_POSITIONS)
    profile['position_scaling'] = _multi('position_scaling', VALID_POS_SCALING)
    profile['loss_response'] = _single('loss_response', VALID_LOSS_RESPONSE)

    # Section 5: Indicators & Patterns
    profile['trusted_indicators'] = [i for i in data.get('trusted_indicators', []) if isinstance(i, str)][:MAX_MULTI]
    profile['indicator_usage'] = _single('indicator_usage', VALID_INDICATOR_USAGE)
    profile['trusted_patterns'] = _multi('trusted_patterns', VALID_TRUSTED_PATTERNS)
    profile['pattern_strictness'] = _single('pattern_strictness', VALID_PATTERN_STRICTNESS)

    # Section 6: Market Regime & Adaptation
    profile['trend_approach'] = _single('trend_approach', VALID_TREND_APPROACH)
    profile['volatility_adjustment'] = _single('volatility_adjustment', VALID_VOL_ADJUST)
    profile['regime_awareness'] = _single('regime_awareness', VALID_REGIME)
    profile['correlation_management'] = _single('correlation_management', VALID_CORRELATION)

    # Section 7: Scenario & Analysis Preferences
    profile['scenario_style'] = _multi('scenario_style', VALID_SCENARIO_STYLE)
    profile['fundamental_balance'] = _single('fundamental_balance', VALID_FUND_BALANCE)
    profile['instrument_selection'] = _single('instrument_selection', VALID_INSTRUMENT_SEL)
    profile['analysis_depth'] = _single('analysis_depth', VALID_ANALYSIS_DEPTH)

    # Section 8: Psychology & Self-Awareness
    profile['emotional_patterns'] = _multi('emotional_patterns', VALID_EMOTIONAL)
    profile['pre_trading_routine'] = _single('pre_trading_routine', VALID_ROUTINE)
    profile['stress_response'] = _single('stress_response', VALID_STRESS)

    # Section 9: Market Views & Biases
    profile['current_bias'] = _single('current_bias', VALID_BIAS)
    profile['bias_horizon'] = _single('bias_horizon', VALID_BIAS_HORIZON)
    profile['bias_reasoning'] = _text('bias_reasoning')

    # Section 10: HODLer-specific
    profile['hodl_strategy'] = _single('hodl_strategy', VALID_HODL_STRATEGY)
    profile['hodl_buy_conditions'] = _text('hodl_buy_conditions')
    profile['hodl_core_assets'] = [a for a in data.get('hodl_core_assets', []) if isinstance(a, str)][:20]

    # Section 11: AI & Report Preferences
    profile['ai_verbosity'] = _single('ai_verbosity', VALID_AI_VERBOSITY)
    profile['ai_devil_advocate'] = bool(data.get('ai_devil_advocate', True))
    profile['ai_risk_warnings'] = bool(data.get('ai_risk_warnings', True))
    profile['report_focus_areas'] = _multi('report_focus_areas', VALID_REPORT_FOCUS)

    # Strip empty string values to keep storage lean
    profile = {k: v for k, v in profile.items() if v != '' and v != []}

    prefs = user_db.get_preferences(user_id)
    prefs['trading_profile'] = profile
    user_db.save_preferences(user_id, prefs)
    return jsonify({'ok': True, 'profile': profile})


@ai_bp.route('/api/ai/journal/analytics')
@journal_auth_required
def ai_journal_analytics():
    """DEPRECATED — use /api/journal/analytics."""
    from carrac.content_db import get_content_db

    user_id = caller_id()
    days = min(int(request.args.get('days', 90)), 365)
    db = get_content_db()
    analytics = db.get_journal_analytics(user_id, days=days)
    return jsonify_iso(analytics)


@ai_bp.route('/api/ai/journal/open', methods=['POST'])
@journal_auth_required
def ai_journal_open():
    """Manually open a journal entry (auto-capture also calls this internally)."""
    from carrac.content_db import get_content_db

    user_id = caller_id()
    data = request.get_json() or {}

    asset = (data.get('asset') or '').upper()
    if not asset or not ASSET_RE.match(asset):
        return jsonify_iso({'error': 'Asset is required'}), 400

    db = get_content_db()
    # Tier gate — journal_trades_monthly cap (this path must
    # enforce the cap too, or imports bypass it).
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    tier = get_request_tier()
    limits = get_tier_limits(tier)
    monthly_cap = limits.get('journal_trades_monthly', 0)
    trades_this_month = db.count_trades_this_month(user_id)
    if trades_this_month >= monthly_cap:
        return jsonify_iso({
            'error': f"Monthly journal limit reached ({trades_this_month}/{monthly_cap} "
                     "trades logged this month). Upgrade for unlimited journal trades.",
            'limit_type': 'journal_trade',
            'current': trades_this_month,
            'max': monthly_cap,
            'tier': tier,
        }), 403

    entry_id = db.log_trade_open(
        user_id=user_id,
        asset=asset,
        timeframe=data.get('timeframe', '1D'),
        direction=data.get('direction', 'long'),
        entry_price=float(data.get('entry_price', 0)),
        target_price=float(data.get('target_price', 0)) if data.get('target_price') else None,
        stop_price=float(data.get('stop_price', 0)) if data.get('stop_price') else None,
        setup_type=data.get('setup_type'),
        drawing_id=data.get('drawing_id'),
        scenario_id=data.get('scenario_id'),
    )

    # PostHog + EspoCRM integration
    try:
        from carrac.integrations import on_trade_opened
        entry_data = db.get_journal_entry(entry_id)
        if entry_data:
            on_trade_opened(user_id, session.get('email', ''), entry_data)
    except Exception as e:
        logger.warning(f"Trade open integration failed for trade {entry_id}: {e}")

    return jsonify_iso({'id': entry_id})


@ai_bp.route('/api/ai/journal/close', methods=['POST'])
@journal_auth_required
def ai_journal_close():
    """Close a journal entry."""
    from carrac.content_db import get_content_db

    user_id = caller_id()
    data = request.get_json() or {}

    journal_id = data.get('id')
    if not journal_id:
        return jsonify_iso({'error': 'Journal entry ID required'}), 400

    db = get_content_db()
    # Verify ownership
    entry = db.get_journal_entry(journal_id)
    if not entry or entry['user_id'] != user_id:
        return jsonify_iso({'error': 'Not found'}), 404

    success = db.log_trade_close(
        journal_id=int(journal_id),
        exit_price=float(data.get('exit_price', 0)),
        exit_reason=data.get('exit_reason', 'manual'),
        notes=data.get('notes'),
    )

    # PostHog + EspoCRM integration
    try:
        from carrac.integrations import on_trade_closed
        closed_entry = db.get_journal_entry(journal_id)
        stats = db.get_journal_stats(user_id, days=365)
        on_trade_closed(user_id, session.get('email', ''), closed_entry, stats)
    except Exception as e:
        logger.warning(f"Trade close integration failed for trade {journal_id}: {e}")

    return jsonify_iso({'ok': success})


@ai_bp.route('/api/ai/journal/<int:journal_id>', methods=['PATCH'])
@journal_auth_required
def ai_journal_update(journal_id):
    """Edit any field on a journal entry (open or closed)."""
    from carrac.content_db import get_content_db

    user_id = caller_id()
    db = get_content_db()

    row = db.get_journal_entry(journal_id)
    if not row or row['user_id'] != user_id:
        return jsonify_iso({'error': 'Not found'}), 404

    data = request.get_json() or {}
    updates = {}
    for field in ('entry_price', 'target_price', 'stop_price'):
        if field in data:
            try:
                updates[field] = float(data[field])
            except (ValueError, TypeError):
                pass
    for field in ('direction', 'setup_type', 'notes', 'timeframe'):
        if field in data:
            updates[field] = str(data[field])[:500] if data[field] else None
    if 'scenario_id' in data:
        try:
            updates['scenario_id'] = int(data['scenario_id']) if data['scenario_id'] else None
        except (ValueError, TypeError):
            pass

    if not updates:
        return jsonify_iso({'error': 'No valid fields to update'}), 400

    success = db.trade_update(journal_id, **updates)
    entry = db.get_journal_entry(journal_id)
    return jsonify_iso({'ok': success, 'entry': entry})


@ai_bp.route('/api/ai/journal/<int:journal_id>/reopen', methods=['POST'])
@journal_auth_required
def ai_journal_reopen(journal_id):
    """Reopen a closed trade."""
    from carrac.content_db import get_content_db

    user_id = caller_id()
    db = get_content_db()

    row = db.get_journal_entry(journal_id)
    if not row or row['user_id'] != user_id:
        return jsonify_iso({'error': 'Not found'}), 404

    success = db.reopen_trade(journal_id)
    return jsonify_iso({'ok': success})


@ai_bp.route('/api/ai/journal/import', methods=['POST'])
@journal_auth_required
def ai_journal_import():
    """Scan position drawings and create journal entries for unlinked ones."""
    from carrac.content_db import get_content_db

    user_id = caller_id()
    db = get_content_db()

    drawings = db.drawing_list(user_id=user_id)
    imported = 0

    # Tier gate — journal_trades_monthly cap applies to bulk import too
    # (checked per iteration).
    from carrac.auth.decorators import get_request_tier, get_tier_limits
    limits = get_tier_limits(get_request_tier())
    monthly_cap = limits.get('journal_trades_monthly', 0)
    base_count = db.count_trades_this_month(user_id)
    capped = False

    for d in drawings:
        if (base_count + imported) >= monthly_cap:
            capped = True
            break
        dtype = d.get('drawing_type', '')
        if dtype not in ('position', 'long_position', 'short_position'):
            continue

        # Already linked?
        existing = db.find_journal_by_drawing(d['id'])
        if existing:
            continue

        meta = d.get('metadata') or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}

        points = meta.get('points', [])
        if len(points) < 2:
            continue

        entry_price = points[0].get('value', 0)
        # Position drawings: point[0]=entry, point[1]=TP (or SL depending on direction)
        # Determine direction from drawing type or price comparison
        direction = 'long'
        if dtype == 'short_position':
            direction = 'short'
        elif dtype == 'long_position':
            direction = 'long'
        elif len(points) >= 2:
            direction = 'long' if points[1].get('value', 0) > entry_price else 'short'

        target_price = None
        stop_price = None
        if len(points) >= 3:
            # 3-point position: entry, TP, SL
            target_price = points[1].get('value')
            stop_price = points[2].get('value')
        elif len(points) == 2:
            target_price = points[1].get('value')

        db.log_trade_open(
            user_id=user_id,
            asset=d.get('asset', ''),
            timeframe=d.get('timeframe', '1D'),
            direction=direction,
            entry_price=entry_price,
            target_price=target_price,
            stop_price=stop_price,
            drawing_id=d['id'],
            scenario_id=d.get('scenario_id'),
        )
        imported += 1

    return jsonify_iso({'imported': imported, 'capped': capped})



# ==================== Context Composer API ====================


@ai_bp.route('/api/ai/context-blocks')
@auth_or_key_required
def ai_context_blocks():
    """List available context blocks (with tier gates)."""
    from carrac.ai.context_blocks import get_available_blocks
    from carrac.auth.decorators import get_request_tier
    user_tier = get_request_tier()
    blocks = get_available_blocks(user_tier)
    return jsonify({'blocks': blocks})


@ai_bp.route('/api/ai/recipes', methods=['GET'])
@login_required
def ai_list_recipes():
    """List user's saved context recipes."""
    from carrac.ai.context_composer import list_recipes
    recipes = list_recipes(session['user_id'])
    return jsonify({'recipes': recipes})


@ai_bp.route('/api/ai/recipes', methods=['POST'])
@login_required
def ai_create_recipe():
    """Create a new context recipe (or clone from template)."""
    from carrac.ai.context_composer import create_recipe
    from carrac.auth.decorators import get_request_tier, get_tier_limits

    # ── Tier gate: context composer ──
    limits = get_tier_limits(get_request_tier())
    if not limits.get('context_composer'):
        return jsonify({'error': 'Context Composer requires a subscription',
                        'upgrade_required': True}), 403

    data = request.get_json() or {}

    name = (data.get('name') or '')[:100]
    blocks = data.get('blocks', [])
    is_default = bool(data.get('is_default', False))
    template_id = data.get('template_id')

    # Custom recipes (not from template) require premium
    if not template_id and not limits.get('custom_recipes'):
        return jsonify({'error': 'Custom recipes require Premium subscription',
                        'upgrade_required': True}), 403

    if not template_id and not blocks:
        return jsonify({'error': 'Blocks or template_id required'}), 400

    recipe_id = create_recipe(
        session['user_id'], name, blocks,
        is_default=is_default, template_id=template_id,
    )
    if recipe_id is None:
        return jsonify({'error': 'Failed to create recipe'}), 500

    return jsonify({'id': recipe_id}), 201


@ai_bp.route('/api/ai/recipes/<int:rid>', methods=['PUT'])
@login_required
def ai_update_recipe(rid):
    """Update an existing context recipe."""
    from carrac.ai.context_composer import update_recipe
    data = request.get_json() or {}

    ok = update_recipe(
        session['user_id'], rid,
        name=data.get('name'),
        blocks=data.get('blocks'),
        is_default=data.get('is_default'),
    )
    if not ok:
        return jsonify({'error': 'Recipe not found'}), 404
    return jsonify({'ok': True})


@ai_bp.route('/api/ai/recipes/<int:rid>', methods=['DELETE'])
@login_required
def ai_delete_recipe(rid):
    """Delete a context recipe."""
    from carrac.ai.context_composer import delete_recipe
    ok = delete_recipe(session['user_id'], rid)
    return jsonify({'ok': ok})


@ai_bp.route('/api/ai/recipes/templates')
@login_required
def ai_recipe_templates():
    """List built-in recipe templates."""
    from carrac.ai.context_composer import get_templates
    return jsonify({'templates': get_templates()})


def _build_scenario_context(scenario_id: int, user_id: int) -> str:
    """Build context text for a specific scenario and its child drawings."""
    try:
        import json as _json
        from carrac.content_db import get_content_db
        db = get_content_db()

        # Get parent scenario
        scenario = db.get_drawing_by_id(scenario_id)
        if not scenario or scenario.get('user_id') != user_id:
            return ''

        meta = {}
        if isinstance(scenario.get('metadata'), str):
            try:
                meta = _json.loads(scenario['metadata'])
            except (ValueError, TypeError):
                pass

        # Scenario label / meta.name / child labels are user-authored
        # free text. escape_user_text wraps in <scenario_label>/<drawing_label>
        # so a poisoned name cannot break out and steer the model.
        from carrac.grounding import escape_user_text as _esc
        raw_label = scenario.get('label', '') or meta.get('name', f"Scenario #{scenario_id}")
        label = _esc(str(raw_label), 'scenario_label', max_chars=120)
        lines = [f"Scenario: {label} ({scenario.get('asset', '')} {scenario.get('timeframe', '')})"]

        children = db.drawing_get_scenario_children(scenario_id)
        from carrac.ai.context_builder import _format_position_drawing
        for c in children:
            dtype = c.get('drawing_type', '?')
            # Position drawings: extract full entry/SL/TP
            if dtype in ('position', 'long_position', 'short_position',
                          'longPosition', 'shortPosition'):
                pos_desc = _format_position_drawing(c)
                if pos_desc:
                    lines.append('  ' + pos_desc.lstrip('- '))
                    continue
            clabel = c.get('label', '')
            cprice = c.get('price_level')
            desc = f"  {dtype}"
            if clabel:
                desc += f" {_esc(str(clabel), 'drawing_label', max_chars=120)}"
            if cprice is not None:
                desc += f" @ ${cprice:,.2f}"
            lines.append(desc)

        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"Scenario context error: {e}")
        return ''


def _build_context_summary(feature, asset, timeframe, shared_ctx, viewport_ctx, scenario_ctx, trader_context=''):
    """Build a human-readable context summary for the collapsible viewer."""
    parts = [f"Feature: {feature}"]
    if asset:
        parts.append(f"Asset: {asset} / {timeframe}")
    if trader_context:
        parts.append(f"\n--- Trader Profile ---\n{trader_context}")
    if shared_ctx:
        parts.append(f"\n--- Shared Market Context ---\n{shared_ctx}")
    if viewport_ctx:
        parts.append(f"\n--- Viewport Context ---\n{viewport_ctx}")
    if scenario_ctx:
        parts.append(f"\n--- Scenario Context ---\n{scenario_ctx}")
    return '\n'.join(parts)
