"""MCP tool: profile_manage — get or update the user's trading profile.

The trading profile is a structured set of preferences that the AI Copilot
uses to personalise analysis, risk warnings, and commentary. It covers
trading style, timeframes, entry/exit methodology, risk management,
psychology, and AI verbosity preferences.
"""

import logging

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id

logger = logging.getLogger('carrac.mcp.profile')

# ---------------------------------------------------------------------------
# Validation sets (mirrors carrac/ai/routes.py:ai_save_trading_profile)
# ---------------------------------------------------------------------------
_VALID_STYLES = {'hodl', 'swing', 'day', 'scalp'}
_VALID_SETUPS = {'breakout', 'pullback', 'reversal', 'range', 'trend_follow', 'mean_revert'}
_VALID_RISK = {'conservative', 'moderate', 'aggressive'}
_VALID_FOCUS = {'crypto', 'stocks', 'both'}
_VALID_EXP = {'beginner', 'intermediate', 'advanced'}
_VALID_HOLDING = {'minutes', 'hours', 'days_to_weeks', 'weeks_to_months', 'months_plus'}
_VALID_CHECK_FREQ = {'constantly', 'few_times_daily', 'daily', 'few_times_weekly'}
_VALID_TFS = {'1m', '5m', '15m', '30m', '1H', '4H', '1D', '3D', '1W', '1M'}
_VALID_SESSIONS = {'asian', 'london', 'us_morning', 'us_midday', 'us_close', 'all'}
_VALID_ENTRY_CONDITIONS = {
    'htf_trend_aligned', 'key_level_test', 'indicator_confluence',
    'volume_confirmation', 'pullback_to_ma', 'breakout_retest',
    'divergence', 'funding_rate_extreme', 'sentiment_extreme',
    'catalyst_required', 'pattern_completion',
}
_VALID_EXIT_STRATEGY = {
    'fixed_target', 'trailing_stop', 'indicator_signal', 'time_based',
    'scale_out', 'invalidation', 'technical_level_target', 'momentum_exit',
}
_VALID_ENTRY_TIMING = {'early', 'standard', 'late', 'scaling', 'depends'}
_VALID_CATALYST_DEP = {
    'must_have', 'prefer_not_required', 'confirmation_only', 'ignore', 'avoid_news'
}
_VALID_STOP_METHOD = {
    'technical_level', 'percentage', 'atr_multiple', 'structure_invalidation',
    'no_stop', 'varies'
}
_VALID_RR = {'1_to_1', '1.5_to_1', '2_to_1', '3_to_1', '5_plus'}
_VALID_RISK_PER_TRADE = {
    'under_half_pct', 'half_to_1_pct', '1_to_2_pct', '2_to_3_pct',
    '3_to_5_pct', 'over_5_pct', 'no_fixed'
}
_VALID_MAX_DD = {'under_5', '5_to_10', '10_to_20', '20_to_50', 'any'}
_VALID_POS_SIZING = {
    'fixed_pct', 'kelly', 'equal_weight', 'conviction_based',
    'volatility_adjusted', 'gut'
}
_VALID_MAX_POSITIONS = {'1_to_2', '3_to_5', '6_to_10', 'unlimited'}
_VALID_POS_SCALING = {
    'scale_in', 'scale_out', 'add_to_winners', 'reduce_after_loss', 'trail_stops'
}
_VALID_LOSS_RESPONSE = {
    'stop_trading', 'reduce_and_continue', 'continue_normal',
    'get_aggressive', 'take_break', 'review_method'
}
_VALID_INDICATOR_USAGE = {
    'primary_trigger', 'confirmation', 'divergence_detection',
    'trend_filter', 'noise_filter', 'dont_use'
}
_VALID_TRUSTED_PATTERNS = {
    'higher_lows', 'double_bottom', 'head_shoulders', 'bull_flag',
    'ascending_triangle', 'divergence_pattern', 'wyckoff', 'cup_handle',
    'wedge', 'range_sr', 'bollinger_squeeze',
}
_VALID_PATTERN_STRICTNESS = {
    'strict_textbook', 'flexible_with_confluence',
    'aggressive_on_intent', 'case_by_case'
}
_VALID_TREND_APPROACH = {
    'trend_only', 'counter_trend_ranging', 'both_directions',
    'trend_as_filter', 'no_preference'
}
_VALID_VOL_ADJUST = {
    'reduce_size', 'widen_stops', 'reduce_frequency',
    'no_adjustment', 'stop_trading', 'proportional_atr'
}
_VALID_REGIME = {
    'active_adjust', 'recognize_no_adjust', 'aware_not_systematic',
    'setup_focused', 'want_to_learn'
}
_VALID_CORRELATION = {
    'track_portfolio_heat', 'limit_correlated', 'limit_total_positions',
    'no_management', 'depends'
}
_VALID_SCENARIO_STYLE = {'level_based', 'pattern_based', 'catalyst_based', 'conditional_chain'}
_VALID_FUND_BALANCE = {
    'primarily_fundamental', 'balanced', 'primarily_technical',
    'completely_technical', 'context_dependent'
}
_VALID_INSTRUMENT_SEL = {
    'predetermined_list', 'volatility_rotation', 'trend_strength',
    'catalyst_driven', 'scan_universe', 'setup_driven'
}
_VALID_ANALYSIS_DEPTH = {'deep_multi_tf', 'moderate_key_levels', 'minimal_signals', 'alerts_only'}
_VALID_EMOTIONAL = {
    'hesitation_on_setups', 'fomo_early_entry', 'revenge_trading',
    'overconfidence_after_wins', 'overtrading_frustration', 'holding_losers',
    'watching_winners_reverse', 'moving_stops',
}
_VALID_ROUTINE = {'very_structured', 'somewhat_structured', 'minimal', 'no_routine', 'varies'}
_VALID_STRESS = {
    'push_through', 'reduce_rebuild', 'take_break',
    'review_methodology', 'panic', 'maintain_consistent'
}
_VALID_BIAS = {
    'strongly_bearish', 'bearish', 'neutral',
    'cautiously_bullish', 'bullish', 'strongly_bullish'
}
_VALID_BIAS_HORIZON = {'short_term', 'medium_term', 'long_term'}
_VALID_HODL_STRATEGY = {'dca', 'lump_sum', 'buy_dips', 'accumulate_range'}
_VALID_AI_VERBOSITY = {'concise', 'balanced', 'detailed'}
_VALID_REPORT_FOCUS = {
    'macro_context', 'confluence_zones', 'scenario_tracking',
    'devil_advocate', 'pattern_detection', 'regime_analysis',
}

_MAX_MULTI = 15
_MAX_TEXT = 1200


def _multi(data, key, valid):
    return [v for v in data.get(key, []) if v in valid][:_MAX_MULTI]


def _single(data, key, valid, default=''):
    v = data.get(key, '')
    return v if v in valid else default


def _text(data, key, maxlen=_MAX_TEXT):
    v = data.get(key, '')
    return v[:maxlen] if v else ''


def _apply_updates(profile, data):
    """Merge validated fields from data into profile. Only updates keys present in data."""
    # Core Identity
    if 'styles' in data:
        profile['styles'] = _multi(data, 'styles', _VALID_STYLES)
    if 'setups' in data:
        profile['setups'] = _multi(data, 'setups', _VALID_SETUPS)
    if 'risk' in data:
        v = _single(data, 'risk', _VALID_RISK)
        if v:
            profile['risk'] = v
    if 'focus' in data:
        v = _single(data, 'focus', _VALID_FOCUS)
        if v:
            profile['focus'] = v
    if 'experience' in data:
        v = _single(data, 'experience', _VALID_EXP)
        if v:
            profile['experience'] = v
    if 'notes' in data:
        profile['notes'] = _text(data, 'notes')

    # Timeframe & Horizon
    if 'primary_timeframes' in data:
        profile['primary_timeframes'] = [
            t for t in data['primary_timeframes'] if t in _VALID_TFS
        ][:5]
    if 'holding_period' in data:
        v = _single(data, 'holding_period', _VALID_HOLDING)
        if v:
            profile['holding_period'] = v
    if 'check_frequency' in data:
        v = _single(data, 'check_frequency', _VALID_CHECK_FREQ)
        if v:
            profile['check_frequency'] = v
    if 'trading_sessions' in data:
        profile['trading_sessions'] = _multi(data, 'trading_sessions', _VALID_SESSIONS)

    # Entry Methodology
    if 'entry_conditions' in data:
        profile['entry_conditions'] = _multi(data, 'entry_conditions', _VALID_ENTRY_CONDITIONS)
    if 'entry_timing' in data:
        v = _single(data, 'entry_timing', _VALID_ENTRY_TIMING)
        if v:
            profile['entry_timing'] = v
    if 'catalyst_dependency' in data:
        v = _single(data, 'catalyst_dependency', _VALID_CATALYST_DEP)
        if v:
            profile['catalyst_dependency'] = v
    if 'entry_notes' in data:
        profile['entry_notes'] = _text(data, 'entry_notes', 300)

    # Exit & Risk Management
    if 'exit_strategy' in data:
        profile['exit_strategy'] = _multi(data, 'exit_strategy', _VALID_EXIT_STRATEGY)
    if 'stop_loss_method' in data:
        v = _single(data, 'stop_loss_method', _VALID_STOP_METHOD)
        if v:
            profile['stop_loss_method'] = v
    if 'typical_rr' in data:
        v = _single(data, 'typical_rr', _VALID_RR)
        if v:
            profile['typical_rr'] = v
    if 'risk_per_trade' in data:
        v = _single(data, 'risk_per_trade', _VALID_RISK_PER_TRADE)
        if v:
            profile['risk_per_trade'] = v
    if 'max_drawdown_comfort' in data:
        v = _single(data, 'max_drawdown_comfort', _VALID_MAX_DD)
        if v:
            profile['max_drawdown_comfort'] = v
    if 'position_sizing' in data:
        v = _single(data, 'position_sizing', _VALID_POS_SIZING)
        if v:
            profile['position_sizing'] = v
    if 'max_concurrent_positions' in data:
        v = _single(data, 'max_concurrent_positions', _VALID_MAX_POSITIONS)
        if v:
            profile['max_concurrent_positions'] = v
    if 'position_scaling' in data:
        profile['position_scaling'] = _multi(data, 'position_scaling', _VALID_POS_SCALING)
    if 'loss_response' in data:
        v = _single(data, 'loss_response', _VALID_LOSS_RESPONSE)
        if v:
            profile['loss_response'] = v

    # Indicators & Patterns
    if 'trusted_indicators' in data:
        profile['trusted_indicators'] = [
            i for i in data['trusted_indicators'] if isinstance(i, str)
        ][:_MAX_MULTI]
    if 'indicator_usage' in data:
        v = _single(data, 'indicator_usage', _VALID_INDICATOR_USAGE)
        if v:
            profile['indicator_usage'] = v
    if 'trusted_patterns' in data:
        profile['trusted_patterns'] = _multi(data, 'trusted_patterns', _VALID_TRUSTED_PATTERNS)
    if 'pattern_strictness' in data:
        v = _single(data, 'pattern_strictness', _VALID_PATTERN_STRICTNESS)
        if v:
            profile['pattern_strictness'] = v

    # Market Regime & Adaptation
    if 'trend_approach' in data:
        v = _single(data, 'trend_approach', _VALID_TREND_APPROACH)
        if v:
            profile['trend_approach'] = v
    if 'volatility_adjustment' in data:
        v = _single(data, 'volatility_adjustment', _VALID_VOL_ADJUST)
        if v:
            profile['volatility_adjustment'] = v
    if 'regime_awareness' in data:
        v = _single(data, 'regime_awareness', _VALID_REGIME)
        if v:
            profile['regime_awareness'] = v
    if 'correlation_management' in data:
        v = _single(data, 'correlation_management', _VALID_CORRELATION)
        if v:
            profile['correlation_management'] = v

    # Scenario & Analysis Preferences
    if 'scenario_style' in data:
        profile['scenario_style'] = _multi(data, 'scenario_style', _VALID_SCENARIO_STYLE)
    if 'fundamental_balance' in data:
        v = _single(data, 'fundamental_balance', _VALID_FUND_BALANCE)
        if v:
            profile['fundamental_balance'] = v
    if 'instrument_selection' in data:
        v = _single(data, 'instrument_selection', _VALID_INSTRUMENT_SEL)
        if v:
            profile['instrument_selection'] = v
    if 'analysis_depth' in data:
        v = _single(data, 'analysis_depth', _VALID_ANALYSIS_DEPTH)
        if v:
            profile['analysis_depth'] = v

    # Psychology & Self-Awareness
    if 'emotional_patterns' in data:
        profile['emotional_patterns'] = _multi(data, 'emotional_patterns', _VALID_EMOTIONAL)
    if 'pre_trading_routine' in data:
        v = _single(data, 'pre_trading_routine', _VALID_ROUTINE)
        if v:
            profile['pre_trading_routine'] = v
    if 'stress_response' in data:
        v = _single(data, 'stress_response', _VALID_STRESS)
        if v:
            profile['stress_response'] = v

    # Market Views & Biases
    if 'current_bias' in data:
        v = _single(data, 'current_bias', _VALID_BIAS)
        if v:
            profile['current_bias'] = v
    if 'bias_horizon' in data:
        v = _single(data, 'bias_horizon', _VALID_BIAS_HORIZON)
        if v:
            profile['bias_horizon'] = v
    if 'bias_reasoning' in data:
        profile['bias_reasoning'] = _text(data, 'bias_reasoning')

    # HODLer-specific
    if 'hodl_strategy' in data:
        v = _single(data, 'hodl_strategy', _VALID_HODL_STRATEGY)
        if v:
            profile['hodl_strategy'] = v
    if 'hodl_buy_conditions' in data:
        profile['hodl_buy_conditions'] = _text(data, 'hodl_buy_conditions')
    if 'hodl_core_assets' in data:
        profile['hodl_core_assets'] = [
            a for a in data['hodl_core_assets'] if isinstance(a, str)
        ][:20]

    # AI & Report Preferences
    if 'ai_verbosity' in data:
        v = _single(data, 'ai_verbosity', _VALID_AI_VERBOSITY)
        if v:
            profile['ai_verbosity'] = v
    if 'ai_devil_advocate' in data:
        profile['ai_devil_advocate'] = bool(data['ai_devil_advocate'])
    if 'ai_risk_warnings' in data:
        profile['ai_risk_warnings'] = bool(data['ai_risk_warnings'])
    if 'report_focus_areas' in data:
        profile['report_focus_areas'] = _multi(data, 'report_focus_areas', _VALID_REPORT_FOCUS)

    return profile


@mcp_tool(
    'profile_manage',
    'Read or partially update the authenticated user\'s trading profile. '
    'The profile is the structured input the AI Copilot uses to personalise '
    'analysis depth, risk warnings, report focus areas, and devil\'s advocate tone. '
    'Supported actions: '
    'get — return the full current profile (no side effects); '
    'set — partial update: only fields you pass are changed, everything else is '
    'preserved. Invalid enum values are silently dropped (mirrors UI save behaviour). '
    'Returns {ok, profile} after set, or {profile} after get. '
    'SIDE EFFECT: set persists the profile to the user preferences store (DB). '
    'Profile covers ~10 categories: core identity (styles, setups, risk, focus, '
    'experience), timeframe & holding period, entry/exit methodology, '
    'risk management (stop method, R:R, position sizing, max drawdown), '
    'indicators & patterns, market regime awareness, scenario/analysis preferences, '
    'psychology (emotional patterns, stress response), market views/bias, '
    'HODLer-specific fields, and AI verbosity/report focus preferences. '
    'EXAMPLE get: {"action": "get"} '
    'EXAMPLE set: {"action": "set", "risk": "moderate", "ai_verbosity": "concise", '
    '"primary_timeframes": ["4H", "1D"]}',
    {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['get', 'set'],
                'description': 'get = read current profile; set = partial update',
            },
            # Core Identity
            'styles': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_STYLES)},
                'description': 'Trading styles (multi-select)',
            },
            'setups': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_SETUPS)},
                'description': 'Preferred setups (multi-select)',
            },
            'risk': {'type': 'string', 'enum': sorted(_VALID_RISK), 'description': 'Risk tolerance'},
            'focus': {'type': 'string', 'enum': sorted(_VALID_FOCUS), 'description': 'Market focus'},
            'experience': {'type': 'string', 'enum': sorted(_VALID_EXP), 'description': 'Experience level'},
            'notes': {'type': 'string', 'description': 'Free-form notes (max 1200 chars)'},
            # Timeframe & Horizon
            'primary_timeframes': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_TFS)},
                'description': 'Primary chart timeframes (max 5)',
            },
            'holding_period': {'type': 'string', 'enum': sorted(_VALID_HOLDING)},
            'check_frequency': {'type': 'string', 'enum': sorted(_VALID_CHECK_FREQ)},
            'trading_sessions': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_SESSIONS)},
            },
            # Entry
            'entry_conditions': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_ENTRY_CONDITIONS)},
            },
            'entry_timing': {'type': 'string', 'enum': sorted(_VALID_ENTRY_TIMING)},
            'catalyst_dependency': {'type': 'string', 'enum': sorted(_VALID_CATALYST_DEP)},
            'entry_notes': {'type': 'string', 'description': 'Entry notes (max 300 chars)'},
            # Exit & Risk
            'exit_strategy': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_EXIT_STRATEGY)},
            },
            'stop_loss_method': {'type': 'string', 'enum': sorted(_VALID_STOP_METHOD)},
            'typical_rr': {'type': 'string', 'enum': sorted(_VALID_RR)},
            'risk_per_trade': {'type': 'string', 'enum': sorted(_VALID_RISK_PER_TRADE)},
            'max_drawdown_comfort': {'type': 'string', 'enum': sorted(_VALID_MAX_DD)},
            'position_sizing': {'type': 'string', 'enum': sorted(_VALID_POS_SIZING)},
            'max_concurrent_positions': {'type': 'string', 'enum': sorted(_VALID_MAX_POSITIONS)},
            'position_scaling': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_POS_SCALING)},
            },
            'loss_response': {'type': 'string', 'enum': sorted(_VALID_LOSS_RESPONSE)},
            # Indicators
            'trusted_indicators': {
                'type': 'array', 'items': {'type': 'string'},
                'description': 'Indicator names (free-form, max 15)',
            },
            'indicator_usage': {'type': 'string', 'enum': sorted(_VALID_INDICATOR_USAGE)},
            'trusted_patterns': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_TRUSTED_PATTERNS)},
            },
            'pattern_strictness': {'type': 'string', 'enum': sorted(_VALID_PATTERN_STRICTNESS)},
            # Regime
            'trend_approach': {'type': 'string', 'enum': sorted(_VALID_TREND_APPROACH)},
            'volatility_adjustment': {'type': 'string', 'enum': sorted(_VALID_VOL_ADJUST)},
            'regime_awareness': {'type': 'string', 'enum': sorted(_VALID_REGIME)},
            'correlation_management': {'type': 'string', 'enum': sorted(_VALID_CORRELATION)},
            # Scenario & Analysis
            'scenario_style': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_SCENARIO_STYLE)},
            },
            'fundamental_balance': {'type': 'string', 'enum': sorted(_VALID_FUND_BALANCE)},
            'instrument_selection': {'type': 'string', 'enum': sorted(_VALID_INSTRUMENT_SEL)},
            'analysis_depth': {'type': 'string', 'enum': sorted(_VALID_ANALYSIS_DEPTH)},
            # Psychology
            'emotional_patterns': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_EMOTIONAL)},
            },
            'pre_trading_routine': {'type': 'string', 'enum': sorted(_VALID_ROUTINE)},
            'stress_response': {'type': 'string', 'enum': sorted(_VALID_STRESS)},
            # Market Views
            'current_bias': {'type': 'string', 'enum': sorted(_VALID_BIAS)},
            'bias_horizon': {'type': 'string', 'enum': sorted(_VALID_BIAS_HORIZON)},
            'bias_reasoning': {'type': 'string', 'description': 'Bias reasoning (max 1200 chars)'},
            # HODLer
            'hodl_strategy': {'type': 'string', 'enum': sorted(_VALID_HODL_STRATEGY)},
            'hodl_buy_conditions': {'type': 'string'},
            'hodl_core_assets': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Max 20'},
            # AI
            'ai_verbosity': {'type': 'string', 'enum': sorted(_VALID_AI_VERBOSITY)},
            'ai_devil_advocate': {'type': 'boolean'},
            'ai_risk_warnings': {'type': 'boolean'},
            'report_focus_areas': {
                'type': 'array', 'items': {'type': 'string', 'enum': sorted(_VALID_REPORT_FOCUS)},
            },
        },
        'required': ['action'],
    },
    min_tier='free',
)
def profile_manage(arguments):
    from carrac.services import get_services

    user_id = get_mcp_user_id()
    action = arguments.get('action')
    if action not in ('get', 'set'):
        raise ToolError('action must be "get" or "set"')

    user_db = get_services().user_db
    prefs = user_db.get_preferences(user_id) or {}
    profile = dict(prefs.get('trading_profile') or {})

    if action == 'get':
        return {'profile': profile}

    # action == 'set': partial update
    _apply_updates(profile, arguments)

    # Strip empty-string values (mirrors route behaviour)
    profile = {k: v for k, v in profile.items() if v != '' and v != []}

    prefs['trading_profile'] = profile
    user_db.save_preferences(user_id, prefs)

    return {'ok': True, 'profile': profile}
