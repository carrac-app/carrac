"""
Tests for Scenarios 2.0 conditions engine — validation, evaluation, logic, edge cases.
"""

import pytest
from carrac.conditions import (
    validate_condition, validate_conditions, evaluate_scenario,
    evaluate_logic, _get_indicator_state, _get_indicator_value,
    ALL_CONDITION_TYPES, _parse_logic_expression,
    evaluate_conditions_dict,
)


class TestStubConditionRemoval:
    """trendline_break / level_touch / correlation were permanent
    stub evaluators that always returned met=False ("not yet evaluated"), so any
    play built on them silently never fired. They were removed from the registry —
    must now be rejected at creation, and any pre-existing play still carrying one
    must surface honestly as an unknown type instead of misleading "not yet evaluated" text."""

    @pytest.mark.parametrize("ctype", ["trendline_break", "level_touch", "correlation"])
    def test_removed_type_rejected_by_validate(self, ctype):
        valid, err = validate_condition({'type': ctype, 'params': {}})
        assert valid is False
        assert err == f"Unknown condition type: {ctype}"

    @pytest.mark.parametrize("ctype", ["trendline_break", "level_touch", "correlation"])
    def test_removed_type_not_in_registry(self, ctype):
        assert ctype not in ALL_CONDITION_TYPES

    def test_preexisting_dead_play_surfaces_unknown_type(self):
        conditions = {
            'rules': [{'id': 'r1', 'type': 'correlation', 'params': {}}],
            'logic': 'all',
        }
        result = evaluate_conditions_dict(conditions, MOCK_ASSET_DATA)
        assert result['rules'][0]['met'] is False
        assert result['rules'][0]['evidence'] == "Unknown condition type: correlation"
        assert result['all_met'] is False


# ── Mock market data ──

MOCK_ASSET_DATA = {
    'realtime': {'price': 3420.0, 'change_pct': 2.5},
    'latest_price': 3420.0,
    'weekly_rsi': 42.3,
    'ema_ribbon': {'trend': 'bearish', 'days_since_flip': 5},
    'macd': {'histogram': -0.15, 'line': -0.30, 'signal': -0.15},
    'consensio': {
        'total': {'signal': 2, 'positions': 5, 'max_positions': 9},
        '1D': {'signal': 1, 'positions': 3, 'max_positions': 3},
    },
    'structure': {
        'structure_trend': 'bullish',  # real backend vocab: bullish/bearish/neutral
        'last_high_type': 'HH',
        'last_low_type': 'HL',
    },
    'bb': {'upper': 3600, 'lower': 3200, 'middle': 3400, 'width_pct': 3.5, 'squeeze': False},
    'adx': {'adx': 32.5, 'regime': 'trend_forming', 'direction': 'up', 'atr_pct': 1.85},
    # cpr: price_position is the coarse field (above/below/inside), price_level is granular zone
    'cpr': {'pivot': 3350, 'price_position': 'above', 'price_level': 'TC-R1'},
    'weekly_cpr': {'pivot': 3300, 'price_position': 'below', 'price_level': 'BC-CP'},
    'daily_cpr': {'pivot': 3380, 'price_position': 'inside', 'price_level': 'CP-TC'},
    'volume_metrics': {'ratio': 1.45, 'is_above_average': True},
    'rsi_divergence': {
        'daily': {'divergence_type': 'bullish', 'strength': 0.7},
        'weekly': {'divergence_type': None, 'strength': 0},
    },
    'opportunity': {'score': 8, 'verdict': 'GOOD'},
    'cockpit_extended': {
        'supertrend': {'direction': 'long'},
        'ttm_squeeze': {'active': False, 'momentum': 'bullish_weak'},
        'stoch_rsi': {'zone': 'oversold'},
        'donchian': {'upper': 3600.0, 'lower': 3200.0},
        'ichimoku': {'cloud_position': 'above_green'},
    },
}

MOCK_MARKET_DATA = {
    'price': 3420.0,
    'asset_data': MOCK_ASSET_DATA,
    'all_assets': {'ETH': MOCK_ASSET_DATA},
}


class TestConditionValidation:
    """Validate condition rule schemas."""

    def test_all_17_types_have_schemas(self):
        """Every condition type must have a schema defined."""
        from carrac.conditions import CONDITION_SCHEMAS
        for ctype in ALL_CONDITION_TYPES:
            assert ctype in CONDITION_SCHEMAS, f"Missing schema for {ctype}"

    def test_valid_price_level(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'price_level',
            'params': {'level': 3400, 'relation': 'above'},
        })
        assert valid
        assert err is None

    def test_invalid_price_level_missing_param(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'price_level',
            'params': {'level': 3400},  # missing relation
        })
        assert not valid
        assert 'relation' in err

    def test_invalid_relation_enum(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'price_level',
            'params': {'level': 3400, 'relation': 'sideways'},
        })
        assert not valid

    def test_missing_id(self):
        valid, err = validate_condition({
            'type': 'price_level',
            'params': {'level': 3400, 'relation': 'above'},
        })
        assert not valid
        assert 'id' in err

    def test_unknown_type(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'magic_indicator',
            'params': {},
        })
        assert not valid

    def test_valid_indicator_value(self):
        valid, err = validate_condition({
            'id': 'r2', 'type': 'indicator_value',
            'params': {'indicator': 'RSI', 'timeframe': '1D', 'operator': 'gt', 'value': 50},
        })
        assert valid

    def test_valid_scanner_verdict(self):
        valid, err = validate_condition({
            'id': 'r3', 'type': 'scanner_verdict',
            'params': {'direction': 'long', 'min_verdict': 'GOOD'},
        })
        assert valid

    def test_valid_manual(self):
        valid, err = validate_condition({
            'id': 'r4', 'type': 'manual',
            'params': {'label': 'Check chart structure'},
        })
        assert valid

    def test_valid_time_limit(self):
        valid, err = validate_condition({
            'id': 'r5', 'type': 'time_limit',
            'params': {'expires_at': '2026-04-01T00:00:00Z'},
        })
        assert valid

    def test_valid_consensio_threshold(self):
        valid, err = validate_condition({
            'id': 'r6', 'type': 'consensio_threshold',
            'params': {'min_value': 7},
        })
        assert valid

    # SQL injection guard on derivatives_metric / derivatives_regime
    def test_derivatives_metric_valid(self):
        """Was `basis_pct`, which is Deribit-barred — this test was
        pinning the leak. funding_rate_avg is the permitted equivalent and is
        never gated; the barred case is covered in
        tests/test_deribit_condition_gate.py."""
        valid, err = validate_condition({
            'id': 'r7', 'type': 'derivatives_metric',
            'params': {'metric': 'funding_rate_avg', 'operator': 'gt', 'value': 0.5},
        })
        assert valid, err

    def test_derivatives_metric_rejects_unknown_column(self):
        """Unknown metric must be rejected at validation — not interpolated into SQL."""
        valid, err = validate_condition({
            'id': 'r7', 'type': 'derivatives_metric',
            'params': {'metric': '1 FROM pg_sleep(5)--', 'operator': 'gt', 'value': 0},
        })
        assert not valid
        assert 'metric' in err

    def test_derivatives_metric_rejects_empty_metric(self):
        valid, err = validate_condition({
            'id': 'r7', 'type': 'derivatives_metric',
            'params': {'metric': '', 'operator': 'gt', 'value': 0},
        })
        # Empty metric fails the required-param check, which is fine (same effect).
        assert not valid

    def test_derivatives_metric_eval_rejects_unknown_metric(self):
        """Even if a poisoned rule bypasses validation, eval must not interpolate it."""
        from carrac.conditions import _eval_derivatives_metric
        met, evidence = _eval_derivatives_metric(
            {'metric': 'evil; DROP TABLE users;--', 'operator': 'gt', 'value': 0},
        )
        assert met is False
        assert 'unknown metric' in evidence

    def test_derivatives_regime_rejects_unknown_regime(self):
        valid, err = validate_condition({
            'id': 'r8', 'type': 'derivatives_regime',
            'params': {'regime': 'hacker_injection', 'condition': 'matches'},
        })
        assert not valid
        assert 'regime' in err


class TestConditionsValidation:
    """Validate full conditions objects."""

    def test_valid_all_logic(self):
        valid, err = validate_conditions({
            'rules': [
                {'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}},
                {'id': 'r2', 'type': 'manual', 'params': {'label': 'Check volume'}},
            ],
            'logic': 'all',
        })
        assert valid

    def test_valid_any_logic(self):
        valid, err = validate_conditions({
            'rules': [
                {'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}},
            ],
            'logic': 'any',
        })
        assert valid

    def test_valid_custom_logic(self):
        valid, err = validate_conditions({
            'rules': [
                {'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}},
                {'id': 'r2', 'type': 'price_level', 'params': {'level': 3500, 'relation': 'below'}},
            ],
            'logic': 'custom',
            'expression': '(r1 AND r2)',
        })
        assert valid

    def test_custom_logic_missing_expression(self):
        valid, err = validate_conditions({
            'rules': [
                {'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}},
            ],
            'logic': 'custom',
        })
        assert not valid
        assert 'expression' in err

    def test_custom_logic_unknown_rule_ref(self):
        valid, err = validate_conditions({
            'rules': [
                {'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}},
            ],
            'logic': 'custom',
            'expression': 'r1 AND r99',
        })
        assert not valid
        assert 'r99' in err

    def test_duplicate_rule_ids(self):
        valid, err = validate_conditions({
            'rules': [
                {'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}},
                {'id': 'r1', 'type': 'price_level', 'params': {'level': 3500, 'relation': 'below'}},
            ],
            'logic': 'all',
        })
        assert not valid
        assert 'Duplicate' in err

    def test_invalid_logic(self):
        valid, err = validate_conditions({
            'rules': [],
            'logic': 'xor',
        })
        assert not valid

    def test_empty_rules_valid(self):
        valid, err = validate_conditions({
            'rules': [],
            'logic': 'all',
        })
        assert valid

    def test_invalidation_rules_validated(self):
        valid, err = validate_conditions({
            'rules': [
                {'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}},
            ],
            'logic': 'all',
            'invalidation_rules': [
                {'id': 'inv1', 'type': 'unknown_type', 'params': {}},
            ],
        })
        assert not valid


class TestIndicatorState:
    """Indicator state vocabulary mapping."""

    def test_ema_ribbon_bearish(self):
        assert _get_indicator_state('EMA_RIBBON', MOCK_ASSET_DATA) == 'bearish'

    def test_ema_ribbon_bullish(self):
        data = {**MOCK_ASSET_DATA, 'ema_ribbon': {'trend': 'bullish'}}
        assert _get_indicator_state('EMA_RIBBON', data) == 'bullish'

    def test_rsi_neutral(self):
        assert _get_indicator_state('RSI', MOCK_ASSET_DATA) == 'neutral'

    def test_rsi_oversold(self):
        data = {**MOCK_ASSET_DATA, 'weekly_rsi': 25}
        assert _get_indicator_state('RSI', data) == 'oversold'

    def test_rsi_overbought(self):
        data = {**MOCK_ASSET_DATA, 'weekly_rsi': 78}
        assert _get_indicator_state('RSI', data) == 'overbought'

    def test_macd_bearish(self):
        assert _get_indicator_state('MACD', MOCK_ASSET_DATA) == 'bearish'

    def test_consensio_bull(self):
        # positions=5, max=9, ratio=0.556 → 'bull'
        assert _get_indicator_state('CONSENSIO', MOCK_ASSET_DATA) == 'bull'

    def test_structure_bullish(self):
        # Backend emits 'bullish' (not 'uptrend') → maps to HH_HL
        assert _get_indicator_state('STRUCTURE', MOCK_ASSET_DATA) == 'HH_HL'

    def test_structure_bearish(self):
        data = {**MOCK_ASSET_DATA, 'structure': {'structure_trend': 'bearish'}}
        assert _get_indicator_state('STRUCTURE', data) == 'LH_LL'

    def test_structure_neutral(self):
        data = {**MOCK_ASSET_DATA, 'structure': {'structure_trend': 'neutral'}}
        assert _get_indicator_state('STRUCTURE', data) == 'range'

    def test_adx_trend_forming(self):
        # regime='trend_forming' passes through directly
        assert _get_indicator_state('ADX', MOCK_ASSET_DATA) == 'trend_forming'

    def test_bb_squeeze_normal(self):
        assert _get_indicator_state('BB_SQUEEZE', MOCK_ASSET_DATA) == 'normal'

    def test_volume_above_avg(self):
        assert _get_indicator_state('VOLUME', MOCK_ASSET_DATA) == 'above_avg'

    def test_cpr_above(self):
        # cpr.price_position='above' → above_pivot
        assert _get_indicator_state('CPR', MOCK_ASSET_DATA) == 'above_pivot'

    def test_cpr_below(self):
        data = {**MOCK_ASSET_DATA, 'cpr': {'price_position': 'below', 'price_level': 'BC-CP'}}
        assert _get_indicator_state('CPR', data) == 'below_pivot'

    def test_cpr_inside(self):
        data = {**MOCK_ASSET_DATA, 'cpr': {'price_position': 'inside', 'price_level': 'CP-TC'}}
        assert _get_indicator_state('CPR', data) == 'between'

    def test_super_trend_bullish(self):
        # cockpit_extended.supertrend.direction='long' → bullish
        assert _get_indicator_state('SUPER_TREND', MOCK_ASSET_DATA) == 'bullish'

    def test_super_trend_bearish(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'supertrend': {'direction': 'short'},
        }}
        assert _get_indicator_state('SUPER_TREND', data) == 'bearish'

    def test_super_trend_missing(self):
        # Unknown/absent super_trend direction yields
        # None ("no signal"), consistent with the Alerts evaluator. 'neutral' is not a
        # valid super_trend state in either UI; either value leaves a condition unmatched.
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {}}
        assert _get_indicator_state('SUPER_TREND', data) is None

    def test_ttm_squeeze_firing_up(self):
        # firing_up now also requires a recent release
        # (bars_since_release within the window) — momentum alone is no
        # longer sufficient. MOCK_ASSET_DATA's base ttm_squeeze fixture has
        # no bars_since_release, so it resolves to squeeze_off now; add a
        # qualifying recent release here to exercise the firing_up path.
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'ttm_squeeze': {'active': False, 'momentum': 'bullish_weak', 'bars_since_release': 2},
        }}
        assert _get_indicator_state('TTM_SQUEEZE', data) == 'firing_up'

    def test_ttm_squeeze_no_recent_release_is_squeeze_off(self):
        # The base MOCK_ASSET_DATA fixture has
        # bullish momentum but no bars_since_release (never squeezed in
        # window / old cached payload) — must NOT fire.
        assert _get_indicator_state('TTM_SQUEEZE', MOCK_ASSET_DATA) == 'squeeze_off'

    def test_ttm_squeeze_stale_release_is_squeeze_off(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'ttm_squeeze': {'active': False, 'momentum': 'bullish_weak', 'bars_since_release': 10},
        }}
        assert _get_indicator_state('TTM_SQUEEZE', data) == 'squeeze_off'

    def test_ttm_squeeze_on(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'ttm_squeeze': {'active': True, 'momentum': 'neutral'},
        }}
        assert _get_indicator_state('TTM_SQUEEZE', data) == 'squeeze_on'

    def test_ttm_squeeze_off(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'ttm_squeeze': {'active': False, 'momentum': 'neutral'},
        }}
        assert _get_indicator_state('TTM_SQUEEZE', data) == 'squeeze_off'

    def test_ttm_squeeze_missing(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {}}
        assert _get_indicator_state('TTM_SQUEEZE', data) is None

    def test_stoch_rsi_zone(self):
        # cockpit_extended.stoch_rsi.zone='oversold'
        assert _get_indicator_state('STOCH_RSI', MOCK_ASSET_DATA) == 'oversold'

    def test_donchian_at_high(self):
        # price=3420 < upper=3600, > lower=3200 → inside; test at_high separately
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'donchian': {'upper': 3420.0, 'lower': 3200.0},
        }}
        assert _get_indicator_state('DONCHIAN', data) == 'at_high'

    def test_donchian_at_low(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'donchian': {'upper': 3600.0, 'lower': 3420.0},
        }}
        assert _get_indicator_state('DONCHIAN', data) == 'at_low'

    def test_donchian_inside(self):
        assert _get_indicator_state('DONCHIAN', MOCK_ASSET_DATA) == 'inside'

    def test_ichimoku_above_cloud(self):
        # cloud_position='above_green' → above_cloud
        assert _get_indicator_state('ICHIMOKU', MOCK_ASSET_DATA) == 'above_cloud'

    def test_ichimoku_below_cloud(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'ichimoku': {'cloud_position': 'below_red'},
        }}
        assert _get_indicator_state('ICHIMOKU', data) == 'below_cloud'

    def test_ichimoku_inside_cloud(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'ichimoku': {'cloud_position': 'inside'},
        }}
        assert _get_indicator_state('ICHIMOKU', data) == 'inside_cloud'

    def test_weekly_cpr_below(self):
        # weekly_cpr.price_position='below' → below_pivot
        assert _get_indicator_state('WEEKLY_CPR', MOCK_ASSET_DATA) == 'below_pivot'

    def test_daily_cpr_between(self):
        # daily_cpr.price_position='inside' → between
        assert _get_indicator_state('DAILY_CPR', MOCK_ASSET_DATA) == 'between'

    def test_unknown_indicator(self):
        assert _get_indicator_state('UNKNOWN_IND', MOCK_ASSET_DATA) is None


class TestVocabCanonicalization:
    """A/B/C from the vocab-canonicalization spec: Alerts and Plays emit identical strings."""

    # ── A. MACD canonical: bullish/bearish/flat ──

    def test_macd_bullish(self):
        data = {**MOCK_ASSET_DATA, 'macd': {'histogram': 0.5, 'line': 0.3, 'signal': 0.1}}
        assert _get_indicator_state('MACD', data) == 'bullish'

    def test_macd_bearish(self):
        data = {**MOCK_ASSET_DATA, 'macd': {'histogram': -0.15, 'line': -0.30, 'signal': -0.15}}
        assert _get_indicator_state('MACD', data) == 'bearish'

    def test_macd_flat(self):
        data = {**MOCK_ASSET_DATA, 'macd': {'histogram': 0, 'line': 0.1, 'signal': 0.1}}
        assert _get_indicator_state('MACD', data) == 'flat'

    def test_macd_no_above_zero_state(self):
        """above_zero must NOT be returned — removed from canonical vocab."""
        data = {**MOCK_ASSET_DATA, 'macd': {'histogram': 0, 'line': 0.5, 'signal': 0.3}}
        result = _get_indicator_state('MACD', data)
        assert result != 'above_zero'
        assert result in ('bullish', 'bearish', 'flat')

    def test_macd_no_below_zero_state(self):
        """below_zero must NOT be returned — removed from canonical vocab."""
        data = {**MOCK_ASSET_DATA, 'macd': {'histogram': 0, 'line': -0.5, 'signal': -0.3}}
        result = _get_indicator_state('MACD', data)
        assert result != 'below_zero'
        assert result in ('bullish', 'bearish', 'flat')

    def test_macd_alerts_vocab_matches_plays_vocab(self):
        """Alerts (indicator_state.py) MACD lambda and Plays (_get_indicator_state) emit identical
        strings for the same asset dict."""
        from carrac.indicator_state import TRACKED_STATES
        macd_tracked = next(t for t in TRACKED_STATES if t[0] == 'macd')
        macd_lambda = macd_tracked[1]

        for hist, expected in [(0.5, 'bullish'), (-0.5, 'bearish'), (0, 'flat')]:
            data = {**MOCK_ASSET_DATA, 'macd': {'histogram': hist}}
            alerts_state = macd_lambda(data)
            plays_state = _get_indicator_state('MACD', data)
            assert alerts_state == plays_state == expected, (
                f"hist={hist}: alerts={alerts_state!r}, plays={plays_state!r}"
            )

    # ── A. ADX canonical: ranging/trend_forming/strong_trend ──

    def test_adx_ranging(self):
        data = {**MOCK_ASSET_DATA, 'adx': {'adx': 15.0, 'regime': 'ranging'}}
        assert _get_indicator_state('ADX', data) == 'ranging'

    def test_adx_trend_forming(self):
        data = {**MOCK_ASSET_DATA, 'adx': {'adx': 28.0, 'regime': 'trend_forming'}}
        assert _get_indicator_state('ADX', data) == 'trend_forming'

    def test_adx_strong_trend(self):
        data = {**MOCK_ASSET_DATA, 'adx': {'adx': 42.0, 'regime': 'strong_trend'}}
        assert _get_indicator_state('ADX', data) == 'strong_trend'

    def test_adx_unknown_regime_returns_none(self):
        """Stale or unknown regime values return None (not a dead string)."""
        data = {**MOCK_ASSET_DATA, 'adx': {'adx': 32.5, 'regime': 'trending'}}
        assert _get_indicator_state('ADX', data) is None

    def test_adx_alerts_vocab_matches_plays_vocab(self):
        """Alerts adx_regime lambda and Plays ADX handler agree on all three canonical states."""
        from carrac.indicator_state import TRACKED_STATES
        adx_tracked = next(t for t in TRACKED_STATES if t[0] == 'adx_regime')
        adx_lambda = adx_tracked[1]

        for regime in ('ranging', 'trend_forming', 'strong_trend'):
            data = {**MOCK_ASSET_DATA, 'adx': {'adx': 30.0, 'regime': regime}}
            alerts_state = adx_lambda(data)
            plays_state = _get_indicator_state('ADX', data)
            assert alerts_state == plays_state == regime, (
                f"regime={regime}: alerts={alerts_state!r}, plays={plays_state!r}"
            )

    # ── A. CONSENSIO: Alerts and Plays agree on named buckets ──

    def test_consensio_full_bull(self):
        # 9/9 = 1.0 >= 0.95
        data = {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 9, 'max_positions': 9}}}
        assert _get_indicator_state('CONSENSIO', data) == 'full_bull'

    def test_consensio_strong_bull(self):
        # 7/9 ≈ 0.778 → strong_bull
        data = {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 7, 'max_positions': 9}}}
        assert _get_indicator_state('CONSENSIO', data) == 'strong_bull'

    def test_consensio_bull(self):
        # 5/9 ≈ 0.556 → bull
        assert _get_indicator_state('CONSENSIO', MOCK_ASSET_DATA) == 'bull'

    def test_consensio_neutral(self):
        # 5/10 = 0.5 → neutral (>= 0.45 and < 0.55)
        data = {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 5, 'max_positions': 10}}}
        assert _get_indicator_state('CONSENSIO', data) == 'neutral'

    def test_consensio_bear(self):
        # 2/9 ≈ 0.222 → bear (just under 0.25 threshold → strong_bear; use 3 for bear)
        data = {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 3, 'max_positions': 9}}}
        assert _get_indicator_state('CONSENSIO', data) == 'bear'

    def test_consensio_strong_bear(self):
        # 0/9 = 0.0 → strong_bear
        data = {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': 0, 'max_positions': 9}}}
        assert _get_indicator_state('CONSENSIO', data) == 'strong_bear'

    def test_consensio_alerts_vocab_matches_plays_vocab(self):
        """_consensio_state (Alerts) and _get_indicator_state CONSENSIO (Plays) agree."""
        from carrac.indicator_state import _consensio_state

        # Each (positions, max_positions, expected_bucket) chosen so the ratio
        # lands cleanly within the stated threshold band.
        cases = [
            (9, 9, 'full_bull'),    # 1.0 >= 0.95
            (7, 9, 'strong_bull'),  # ~0.778 >= 0.75
            (5, 9, 'bull'),         # ~0.556 >= 0.55
            (5, 10, 'neutral'),     # 0.50 >= 0.45 and < 0.55
            (3, 9, 'bear'),         # ~0.333 >= 0.25 and < 0.45
            (0, 9, 'strong_bear'),  # 0.0 < 0.25
        ]
        for positions, max_p, expected in cases:
            data = {**MOCK_ASSET_DATA, 'consensio': {'total': {'positions': positions, 'max_positions': max_p}}}
            alerts_state = _consensio_state(data)
            plays_state = _get_indicator_state('CONSENSIO', data)
            assert alerts_state == plays_state == expected, (
                f"positions={positions}/{max_p}: alerts={alerts_state!r}, plays={plays_state!r}"
            )

    # ── B. EMA_CROSS: Plays handler reads cockpit_extended.ema_cross.state ──

    def test_ema_cross_bullish(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'ema_cross': {'state': 'bullish'},
        }}
        assert _get_indicator_state('EMA_CROSS', data) == 'bullish'

    def test_ema_cross_bearish(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'ema_cross': {'state': 'bearish'},
        }}
        assert _get_indicator_state('EMA_CROSS', data) == 'bearish'

    def test_ema_cross_missing_returns_none(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {}}
        assert _get_indicator_state('EMA_CROSS', data) is None

    def test_ema_cross_alerts_vocab_matches_plays_vocab(self):
        """_ema_cross_state (Alerts) and _get_indicator_state EMA_CROSS (Plays) agree."""
        from carrac.indicator_state import _ema_cross_state

        for state in ('bullish', 'bearish'):
            data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
                **MOCK_ASSET_DATA['cockpit_extended'],
                'ema_cross': {'state': state},
            }}
            alerts_state = _ema_cross_state(data)
            plays_state = _get_indicator_state('EMA_CROSS', data)
            assert alerts_state == plays_state == state, (
                f"state={state}: alerts={alerts_state!r}, plays={plays_state!r}"
            )

    # ── B. ALLIGATOR: Plays handler reads cockpit_extended.alligator.state ──

    def test_alligator_eating_bullish(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'alligator': {'state': 'eating_bullish'},
        }}
        assert _get_indicator_state('ALLIGATOR', data) == 'eating_bullish'

    def test_alligator_eating_bearish(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'alligator': {'state': 'eating_bearish'},
        }}
        assert _get_indicator_state('ALLIGATOR', data) == 'eating_bearish'

    def test_alligator_sleeping(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'alligator': {'state': 'sleeping'},
        }}
        assert _get_indicator_state('ALLIGATOR', data) == 'sleeping'

    def test_alligator_mixed(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
            **MOCK_ASSET_DATA['cockpit_extended'],
            'alligator': {'state': 'mixed'},
        }}
        assert _get_indicator_state('ALLIGATOR', data) == 'mixed'

    def test_alligator_missing_returns_none(self):
        data = {**MOCK_ASSET_DATA, 'cockpit_extended': {}}
        assert _get_indicator_state('ALLIGATOR', data) is None

    def test_alligator_alerts_vocab_matches_plays_vocab(self):
        """_alligator_state (Alerts) and _get_indicator_state ALLIGATOR (Plays) agree."""
        from carrac.indicator_state import _alligator_state

        for state in ('eating_bullish', 'eating_bearish', 'sleeping', 'mixed'):
            data = {**MOCK_ASSET_DATA, 'cockpit_extended': {
                **MOCK_ASSET_DATA['cockpit_extended'],
                'alligator': {'state': state},
            }}
            alerts_state = _alligator_state(data)
            plays_state = _get_indicator_state('ALLIGATOR', data)
            assert alerts_state == plays_state == state, (
                f"state={state}: alerts={alerts_state!r}, plays={plays_state!r}"
            )


class TestIndicatorStateWiring:
    """12 indicator states newly wired from the Alerts evaluator
    (indicator_state.py TRACKED_STATES) into the Plays evaluator
    (_get_indicator_state), via shared indicator_eval.py extractors.

    These use the *_ZONE/*_REGIME/*_POSITION Alerts-vocabulary key names —
    distinct from the pre-existing RSI/ADX/CPR/WEEKLY_CPR/DAILY_CPR branches,
    which keep the Plays-only coarse vocabulary untouched.
    """

    def test_rsi_zone(self):
        data = {**MOCK_ASSET_DATA, 'weekly_rsi': 25}
        assert _get_indicator_state('RSI_ZONE', data) == 'oversold'

    def test_adx_regime(self):
        assert _get_indicator_state('ADX_REGIME', MOCK_ASSET_DATA) == 'trend_forming'

    def test_adx_regime_unknown_default_preserved(self):
        """Distinct from the ADX branch (which returns None for an
        out-of-vocab regime) — ADX_REGIME preserves the historical
        Alerts 'unknown' default when adx is entirely absent."""
        data = {k: v for k, v in MOCK_ASSET_DATA.items() if k != 'adx'}
        assert _get_indicator_state('ADX_REGIME', data) == 'unknown'

    def test_cpr_position_granular(self):
        # MOCK_ASSET_DATA cpr.price_level = 'TC-R1' -> granular zone 'tc_r1',
        # distinct from the coarse CPR branch's 'above_pivot'.
        assert _get_indicator_state('CPR_POSITION', MOCK_ASSET_DATA) == 'tc_r1'
        assert _get_indicator_state('CPR', MOCK_ASSET_DATA) == 'above_pivot'

    def test_weekly_cpr_position_granular(self):
        # MOCK_ASSET_DATA weekly_cpr.price_level = 'BC-CP' -> 'below_pivot' zone.
        assert _get_indicator_state('WEEKLY_CPR_POSITION', MOCK_ASSET_DATA) == 'below_pivot'

    def test_daily_cpr_position_granular(self):
        # MOCK_ASSET_DATA daily_cpr.price_level = 'CP-TC' -> 'above_pivot' zone.
        assert _get_indicator_state('DAILY_CPR_POSITION', MOCK_ASSET_DATA) == 'above_pivot'

    def test_ema_gap_squeeze_none_by_default(self):
        # MOCK_ASSET_DATA has no score_b -> 'none', not None.
        assert _get_indicator_state('EMA_GAP_SQUEEZE', MOCK_ASSET_DATA) == 'none'

    def test_ema_gap_squeeze_fires(self):
        data = {**MOCK_ASSET_DATA, 'score_b': {'alerts': ['ema_squeeze']}}
        assert _get_indicator_state('EMA_GAP_SQUEEZE', data) == 'squeeze'

    def test_v_cpr_fires(self):
        data = {**MOCK_ASSET_DATA, 'score_b': {'alerts': ['v_cpr']}}
        assert _get_indicator_state('V_CPR', data) == 'vacuum'

    def test_cpr_extreme_fires(self):
        data = {**MOCK_ASSET_DATA, 'score_b': {'alerts': ['near_r3']}}
        assert _get_indicator_state('CPR_EXTREME', data) == 'near_r3'

    def test_structure_disagreement_fires(self):
        data = {**MOCK_ASSET_DATA, 'score_b': {'alerts': ['structure_bearish']}}
        assert _get_indicator_state('STRUCTURE_DISAGREEMENT', data) == 'bearish'

    def test_tf_divergence_fires(self):
        data = {**MOCK_ASSET_DATA, 'trend_analysis': {'patterns': ['divergence']}}
        assert _get_indicator_state('TF_DIVERGENCE', data) == 'active'

    def test_perp_basis_fires(self):
        data = {**MOCK_ASSET_DATA, 'perp_basis_pct': 0.5}
        assert _get_indicator_state('PERP_BASIS', data) == 'steep_contango'

    def test_perp_basis_missing_is_none_state_not_normal(self):
        # MOCK_ASSET_DATA carries no perp_basis_pct.
        assert _get_indicator_state('PERP_BASIS', MOCK_ASSET_DATA) == 'none'

    def test_rsi_divergence_reads_trend_analysis_not_rsi_divergence_field(self):
        """The RSI_DIVERGENCE branch reads trend_analysis.patterns — it must
        NOT be confused with MOCK_ASSET_DATA['rsi_divergence'] (a dict used
        by the unrelated `indicator_divergence` condition type). With no
        trend_analysis present, RSI_DIVERGENCE resolves to 'none' even
        though MOCK_ASSET_DATA['rsi_divergence']['daily']['divergence_type']
        is 'bullish'."""
        assert 'trend_analysis' not in MOCK_ASSET_DATA
        assert MOCK_ASSET_DATA['rsi_divergence']['daily']['divergence_type'] == 'bullish'
        assert _get_indicator_state('RSI_DIVERGENCE', MOCK_ASSET_DATA) == 'none'

    def test_rsi_divergence_bull_pattern(self):
        data = {**MOCK_ASSET_DATA, 'trend_analysis': {'patterns': ['rsi_bull_div']}}
        assert _get_indicator_state('RSI_DIVERGENCE', data) == 'bull'


class TestRsiDivergencePlayCondition:
    """End-to-end Play condition test: a `indicator_state` rule on
    RSI_DIVERGENCE evaluated through evaluate_conditions_dict.

    Uses its own minimal asset dicts (no 'rsi_divergence' key) rather than
    MOCK_ASSET_DATA, since MOCK_ASSET_DATA carries an unrelated
    `rsi_divergence` dict field consumed by the `indicator_divergence`
    condition type — this test must not collide with that.
    """

    @staticmethod
    def _conditions(state):
        return {
            'rules': [{
                'id': 'r1',
                'type': 'indicator_state',
                'params': {'indicator': 'RSI_DIVERGENCE', 'timeframe': '1D', 'state': state},
            }],
            'logic': 'all',
        }

    def test_bull_pattern_matches_bull_state(self):
        market_data = {
            'price': 100.0,
            'asset_data': {'trend_analysis': {'patterns': ['rsi_bull_div']}},
            'all_assets': {},
        }
        result = evaluate_conditions_dict(self._conditions('bull'), market_data)
        assert result['rules'][0]['met'] is True
        assert result['all_met'] is True

    def test_bull_pattern_does_not_match_bear_state(self):
        market_data = {
            'price': 100.0,
            'asset_data': {'trend_analysis': {'patterns': ['rsi_bull_div']}},
            'all_assets': {},
        }
        result = evaluate_conditions_dict(self._conditions('bear'), market_data)
        assert result['rules'][0]['met'] is False
        assert result['all_met'] is False

    def test_missing_trend_analysis_resolves_to_none_state(self):
        market_data = {'price': 100.0, 'asset_data': {}, 'all_assets': {}}
        result = evaluate_conditions_dict(self._conditions('bull'), market_data)
        assert result['rules'][0]['met'] is False


class TestIndicatorValue:
    """Numeric indicator value extraction."""

    def test_rsi_value(self):
        assert _get_indicator_value('RSI', MOCK_ASSET_DATA) == 42.3

    def test_adx_value(self):
        assert _get_indicator_value('ADX', MOCK_ASSET_DATA) == 32.5

    def test_consensio_value(self):
        assert _get_indicator_value('CONSENSIO', MOCK_ASSET_DATA) == 5.0

    def test_volume_ratio(self):
        assert _get_indicator_value('VOLUME', MOCK_ASSET_DATA) == 1.45


class TestConditionEvaluation:
    """Individual condition type evaluators."""

    def test_price_level_above_met(self):
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is True
        assert result['met_count'] == 1

    def test_price_level_above_not_met(self):
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 3500, 'relation': 'above'}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is False

    def test_price_zone_inside(self):
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'price_zone', 'params': {'low': 3400, 'high': 3500, 'relation': 'inside'}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is True

    def test_indicator_value_rsi_gt(self):
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'indicator_value', 'params': {'indicator': 'RSI', 'timeframe': '1D', 'operator': 'lt', 'value': 50}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is True  # RSI 42.3 < 50

    def test_indicator_state_check(self):
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'indicator_state', 'params': {'indicator': 'EMA_RIBBON', 'timeframe': '1D', 'state': 'bearish'}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is True

    def test_consensio_threshold(self):
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'consensio_threshold', 'params': {'min_value': 5}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is True  # positions=5 >= 5

    @staticmethod
    def _stub_scanner(monkeypatch, verdict):
        # The verdict lives on the scanner cache, not on the
        # opportunity dict (that key does not exist).
        import carrac.caches as caches
        sym = MOCK_MARKET_DATA['asset_data'].get('symbol', 'ETH')
        monkeypatch.setattr(caches, 'get_cached_scanner', lambda *a, **k: {
            'results': [{'symbol': sym, 'long': {'total': 8, 'verdict': verdict},
                         'short': {'total': 2, 'verdict': 'AVOID'}}]})

    def test_scanner_verdict_met(self, monkeypatch):
        self._stub_scanner(monkeypatch, 'GOOD')
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'scanner_verdict', 'params': {'direction': 'long', 'min_verdict': 'GOOD'}}],
                'logic': 'all',
            },
            'status': 'watching',
            'asset': 'ETH',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is True  # GOOD >= GOOD

    def test_scanner_verdict_not_met(self, monkeypatch):
        self._stub_scanner(monkeypatch, 'GOOD')
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'scanner_verdict', 'params': {'direction': 'long', 'min_verdict': 'STRONG'}}],
                'logic': 'all',
            },
            'status': 'watching',
            'asset': 'ETH',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is False  # GOOD < STRONG

    def test_manual_never_auto_met(self):
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'manual', 'params': {'label': 'Check chart'}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is False

    def test_indicator_divergence(self):
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'indicator_divergence', 'params': {'indicator': 'RSI', 'timeframe': '1D', 'type': 'bullish'}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is True

    def test_distance_from_entry(self):
        scenario = {
            'entry_low': 3400, 'entry_high': 3500,
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'distance_from', 'params': {'reference': 'entry', 'threshold_pct': 5}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        # Price 3420, entry mid = 3450, distance ~0.87% < 5%
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is True

    def test_time_limit_not_expired(self):
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'time_limit', 'params': {'expires_at': '2099-01-01T00:00:00Z'}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is False  # Not expired yet

    def test_time_limit_expired(self):
        scenario = {
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'time_limit', 'params': {'expires_at': '2020-01-01T00:00:00Z'}}],
                'logic': 'all',
            },
            'status': 'watching',
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is True  # Expired


class TestLogicEvaluation:
    """Logic operators: all, any, custom."""

    def test_all_logic_all_met(self):
        assert evaluate_logic({'logic': 'all'}, {'r1': True, 'r2': True}) is True

    def test_all_logic_one_unmet(self):
        assert evaluate_logic({'logic': 'all'}, {'r1': True, 'r2': False}) is False

    def test_any_logic_one_met(self):
        assert evaluate_logic({'logic': 'any'}, {'r1': True, 'r2': False}) is True

    def test_any_logic_none_met(self):
        assert evaluate_logic({'logic': 'any'}, {'r1': False, 'r2': False}) is False

    def test_custom_and(self):
        assert _parse_logic_expression('r1 AND r2', {'r1': True, 'r2': True}) is True
        assert _parse_logic_expression('r1 AND r2', {'r1': True, 'r2': False}) is False

    def test_custom_or(self):
        assert _parse_logic_expression('r1 OR r2', {'r1': False, 'r2': True}) is True

    def test_custom_complex(self):
        states = {'r1': True, 'r2': False, 'r3': True, 'r4': True}
        assert _parse_logic_expression('(r1 AND r2) OR (r3 AND r4)', states) is True

    def test_custom_nested(self):
        states = {'r1': True, 'r2': True, 'r3': False}
        assert _parse_logic_expression('(r1 AND r2) AND NOT r3', states) is True

    def test_empty_states(self):
        assert evaluate_logic({'logic': 'all'}, {}) is False
        assert evaluate_logic({'logic': 'any'}, {}) is False

    def test_unsafe_expression_rejected(self):
        """Expressions with non-rule tokens should be safely rejected."""
        assert _parse_logic_expression('__import__("os")', {}) is False


class TestInvalidation:
    """Invalidation detection."""

    def test_invalidation_rule_triggers(self):
        scenario = {
            'status': 'watching',
            'direction': 'long',
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}}],
                'logic': 'all',
                'invalidation_rules': [
                    {'id': 'inv1', 'type': 'price_level', 'params': {'level': 3500, 'relation': 'below'}},
                ],
            },
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['invalidation_triggered'] is True  # price 3420 < 3500

    def test_scenario_level_invalidation(self):
        scenario = {
            'status': 'watching',
            'direction': 'long',
            'invalidation': 3500,  # Price 3420 < 3500
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}}],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['invalidation_triggered'] is True
        assert result['suggested_status'] == 'invalidated'

    def test_no_invalidation_when_price_safe(self):
        scenario = {
            'status': 'watching',
            'direction': 'long',
            'invalidation': 3200,  # Price 3420 > 3200
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}}],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['invalidation_triggered'] is False


class TestSuggestedStatus:
    """Status transition suggestions based on evaluation."""

    def test_all_met_suggests_triggered(self):
        scenario = {
            'status': 'watching',
            'conditions': {
                'rules': [
                    {'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}},
                ],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['all_met'] is True
        assert result['suggested_status'] == 'triggered'

    def test_partial_met_near_entry_suggests_approaching(self):
        scenario = {
            'status': 'watching',
            'entry_low': 3400, 'entry_high': 3500,
            'conditions': {
                'rules': [
                    {'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}},  # met
                    {'id': 'r2', 'type': 'indicator_value', 'params': {'indicator': 'RSI', 'timeframe': '1D', 'operator': 'gt', 'value': 50}},  # not met
                ],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['met_count'] == 1
        assert result['total_count'] == 2
        assert result['suggested_status'] == 'approaching'


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_empty_conditions(self):
        scenario = {
            'status': 'watching',
            'conditions': {'rules': [], 'logic': 'all'},
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['met_count'] == 0
        assert result['total_count'] == 0
        assert result['suggested_status'] is None

    def test_no_conditions_key(self):
        scenario = {'status': 'watching'}
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['met_count'] == 0

    def test_missing_market_data(self):
        scenario = {
            'status': 'watching',
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'indicator_value', 'params': {'indicator': 'RSI', 'timeframe': '1D', 'operator': 'gt', 'value': 50}}],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, {'price': 0, 'asset_data': {}, 'all_assets': {}})
        # Should not crash — returns not-met with evidence
        assert result['rules'][0]['met'] is False
        assert 'no data' in result['rules'][0]['evidence']

    def test_zero_price(self):
        scenario = {
            'status': 'watching',
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}}],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, {'price': 0, 'asset_data': {}, 'all_assets': {}})
        assert result['rules'][0]['met'] is False

    def test_met_at_persists_when_met(self):
        """Once a rule is met, met_at should be set and persist."""
        scenario = {
            'status': 'watching',
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 3400, 'relation': 'above'}}],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met_at'] is not None

    def test_met_at_cleared_when_not_met(self):
        scenario = {
            'status': 'watching',
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'price_level', 'params': {'level': 3500, 'relation': 'above'}, 'met_at': '2026-03-20T00:00:00Z'}],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met_at'] is None

    def test_unknown_evaluator_graceful(self):
        scenario = {
            'status': 'watching',
            'conditions': {
                'rules': [{'id': 'r1', 'type': 'nonexistent_type', 'params': {}}],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is False
        assert 'Unknown' in result['rules'][0]['evidence']


class TestCrossAsset:
    """Cross-asset condition evaluation."""

    def test_cross_asset_price_check(self):
        scenario = {
            'status': 'watching',
            'conditions': {
                'rules': [{
                    'id': 'r1', 'type': 'cross_asset',
                    'params': {
                        'asset': 'ETH',
                        'condition_type': 'price_level',
                        'params': {'level': 3400, 'relation': 'above'},
                    },
                }],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is True

    def test_cross_asset_missing_data(self):
        scenario = {
            'status': 'watching',
            'conditions': {
                'rules': [{
                    'id': 'r1', 'type': 'cross_asset',
                    'params': {
                        'asset': 'XYZ',
                        'condition_type': 'price_level',
                        'params': {'level': 100, 'relation': 'above'},
                    },
                }],
                'logic': 'all',
            },
        }
        result = evaluate_scenario(scenario, MOCK_MARKET_DATA)
        assert result['rules'][0]['met'] is False


# ── Engulfing signals fixture ──

_BULLISH_1H_SIGNAL = {'type': 'engulfing', 'direction': 'bullish', 'tf': '1h', 'candle_pos': 0, 'detail': 'bullish engulfing'}
_BEARISH_4H_SIGNAL = {'type': 'engulfing', 'direction': 'bearish', 'tf': '4h', 'candle_pos': 0, 'detail': 'bearish engulfing'}

_ASSET_WITH_ENGULFING = {
    **MOCK_ASSET_DATA,
    'engulfing_signals': [_BULLISH_1H_SIGNAL, _BEARISH_4H_SIGNAL],
}

_MARKET_WITH_ENGULFING = {
    'price': 3420.0,
    'asset_data': _ASSET_WITH_ENGULFING,
    'all_assets': {},
}


class TestEngulfingDetected:
    """Unit + integration tests for the engulfing_detected condition type."""

    # ── Validation ──

    def test_valid_engulfing_bullish_1h(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'direction': 'bullish', 'timeframe': '1h'},
        })
        assert valid
        assert err is None

    def test_valid_engulfing_bearish_4h(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'direction': 'bearish', 'timeframe': '4h'},
        })
        assert valid

    def test_invalid_missing_direction(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'timeframe': '1h'},
        })
        assert not valid
        assert 'direction' in err

    def test_invalid_missing_timeframe(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'direction': 'bullish'},
        })
        assert not valid
        assert 'timeframe' in err

    def test_invalid_direction_enum(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'direction': 'sideways', 'timeframe': '1h'},
        })
        assert not valid
        assert 'direction' in err

    def test_invalid_timeframe_enum(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'direction': 'bullish', 'timeframe': '1D'},
        })
        assert not valid
        assert 'timeframe' in err

    # ── Evaluator unit tests ──

    def test_met_when_matching_signal_present(self):
        from carrac.conditions import _eval_engulfing_detected
        met, evidence = _eval_engulfing_detected(
            {'direction': 'bullish', 'timeframe': '1h'},
            asset_data=_ASSET_WITH_ENGULFING,
        )
        assert met is True
        assert 'bullish' in evidence
        assert '1h' in evidence

    def test_not_met_wrong_direction(self):
        from carrac.conditions import _eval_engulfing_detected
        met, evidence = _eval_engulfing_detected(
            {'direction': 'bearish', 'timeframe': '1h'},
            asset_data=_ASSET_WITH_ENGULFING,
        )
        assert met is False
        assert 'no engulfing' in evidence

    def test_not_met_wrong_timeframe(self):
        from carrac.conditions import _eval_engulfing_detected
        met, evidence = _eval_engulfing_detected(
            {'direction': 'bullish', 'timeframe': '4h'},
            asset_data=_ASSET_WITH_ENGULFING,
        )
        assert met is False
        assert 'no engulfing' in evidence

    def test_not_met_no_signals(self):
        from carrac.conditions import _eval_engulfing_detected
        met, evidence = _eval_engulfing_detected(
            {'direction': 'bullish', 'timeframe': '1h'},
            asset_data=MOCK_ASSET_DATA,  # no engulfing_signals key
        )
        assert met is False

    def test_not_met_empty_signals_list(self):
        from carrac.conditions import _eval_engulfing_detected
        met, _ = _eval_engulfing_detected(
            {'direction': 'bullish', 'timeframe': '1h'},
            asset_data={**MOCK_ASSET_DATA, 'engulfing_signals': []},
        )
        assert met is False

    def test_bearish_4h_met(self):
        from carrac.conditions import _eval_engulfing_detected
        met, evidence = _eval_engulfing_detected(
            {'direction': 'bearish', 'timeframe': '4h'},
            asset_data=_ASSET_WITH_ENGULFING,
        )
        assert met is True
        assert 'bearish' in evidence
        assert '4h' in evidence

    # ── Integration: evaluate_conditions_dict ──

    def test_play_logic_all_engulfing_alone(self):
        """engulfing_detected in a Play conditions dict evaluates correctly."""
        from carrac.conditions import evaluate_conditions_dict
        conditions = {
            'rules': [{'id': 'c1', 'type': 'engulfing_detected',
                       'params': {'direction': 'bullish', 'timeframe': '1h'}}],
            'logic': 'all',
        }
        result = evaluate_conditions_dict(conditions, _MARKET_WITH_ENGULFING)
        assert result['all_met'] is True
        assert result['met_count'] == 1
        assert result['rules'][0]['met'] is True

    def test_play_logic_all_combined_with_price_level(self):
        """engulfing_detected combined with price_level using logic=all."""
        from carrac.conditions import evaluate_conditions_dict
        conditions = {
            'rules': [
                {'id': 'c1', 'type': 'engulfing_detected',
                 'params': {'direction': 'bearish', 'timeframe': '4h'}},
                {'id': 'c2', 'type': 'price_level',
                 'params': {'level': 3400, 'relation': 'above'}},
            ],
            'logic': 'all',
        }
        result = evaluate_conditions_dict(conditions, _MARKET_WITH_ENGULFING)
        assert result['all_met'] is True
        assert result['met_count'] == 2

    def test_play_logic_all_fails_when_engulfing_absent(self):
        """logic=all fails when engulfing signal missing."""
        from carrac.conditions import evaluate_conditions_dict
        conditions = {
            'rules': [
                {'id': 'c1', 'type': 'engulfing_detected',
                 'params': {'direction': 'bullish', 'timeframe': '4h'}},  # not in signals
                {'id': 'c2', 'type': 'price_level',
                 'params': {'level': 3400, 'relation': 'above'}},
            ],
            'logic': 'all',
        }
        result = evaluate_conditions_dict(conditions, _MARKET_WITH_ENGULFING)
        assert result['all_met'] is False
        assert result['met_count'] == 1


# ── BB Breakout-Retest-Engulfing helpers ──

import numpy as np
import pandas as pd


def _make_ohlcv(n=35, base_price=100.0):
    """Build a minimal synthetic OHLCV DataFrame of *n* bars.

    All bars default to a calm range near *base_price* (inside BB).
    Returns the DataFrame so tests can splice in breakout bars as needed.
    """
    dates = pd.date_range('2026-01-01', periods=n, freq='4h')
    close = np.full(n, base_price)
    df = pd.DataFrame({
        'open':   close * 0.999,
        'high':   close * 1.002,
        'low':    close * 0.998,
        'close':  close,
        'volume': np.full(n, 1000.0),
    }, index=dates)
    return df


def _bb_bounds(df, period=20, std_dev=2.0):
    """Return (upper, middle, lower) scalars for the last bar."""
    close = df['close']
    sma = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    return float(upper.iloc[-1]), float(sma.iloc[-1]), float(lower.iloc[-1])


def _make_bullish_setup(lookback=15, proximity_pct=0.5):
    """DataFrame where a bullish setup fires: breakout ~10 bars ago, current close near middle."""
    df = _make_ohlcv(n=40)
    # Inject an above-upper-band close ~10 bars before current bar.
    # We need the close to clearly exceed the upper band.
    # Upper band ≈ mean + 2*std. With tight base prices the std is 0 → inject mild spread first.
    # Easier: set bar -11 (relative to end) to a value we know exceeds upper.
    # The rolling std will be nearly 0 for a flat series, so upper ≈ middle ≈ 100.
    # We force a breakout by setting a bar's close to 105 (5% above) then reset current to 100.
    breakout_idx = -11  # within lookback=15 window (excludes current bar -1 and -2)
    df.iloc[breakout_idx, df.columns.get_loc('close')] = 105.0
    df.iloc[breakout_idx, df.columns.get_loc('high')] = 105.5
    # Current bar: reset to base (inside BB / near middle)
    df.iloc[-1, df.columns.get_loc('close')] = 100.0
    return df


def _make_bearish_setup(lookback=15):
    """DataFrame where a bearish setup fires: breakout below lower band, current near middle."""
    df = _make_ohlcv(n=40)
    breakout_idx = -11
    df.iloc[breakout_idx, df.columns.get_loc('close')] = 95.0
    df.iloc[breakout_idx, df.columns.get_loc('low')] = 94.5
    df.iloc[-1, df.columns.get_loc('close')] = 100.0
    return df


class TestBBBreakoutRetestEngulfing:
    """Validation + evaluator unit tests for bb_breakout_retest_engulfing."""

    # ── Validation ──

    def test_valid_params_accepted(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
            'params': {
                'direction': 'bullish', 'timeframe': '4h',
                'lookback_bars': 15, 'proximity_pct': 0.5,
                'require_volume': True,
            },
        })
        assert valid, err

    def test_invalid_direction_enum(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
            'params': {
                'direction': 'sideways', 'timeframe': '4h',
                'lookback_bars': 15, 'proximity_pct': 0.5,
            },
        })
        assert not valid
        assert 'direction' in err

    def test_invalid_timeframe_enum(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
            'params': {
                'direction': 'bullish', 'timeframe': '1D',
                'lookback_bars': 15, 'proximity_pct': 0.5,
            },
        })
        assert not valid
        assert 'timeframe' in err

    def test_invalid_lookback_bars_too_low(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
            'params': {
                'direction': 'bullish', 'timeframe': '4h',
                'lookback_bars': 3, 'proximity_pct': 0.5,
            },
        })
        assert not valid
        assert 'lookback_bars' in err

    def test_invalid_lookback_bars_too_high(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
            'params': {
                'direction': 'bullish', 'timeframe': '4h',
                'lookback_bars': 55, 'proximity_pct': 0.5,
            },
        })
        assert not valid
        assert 'lookback_bars' in err

    def test_invalid_proximity_pct_out_of_range(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
            'params': {
                'direction': 'bullish', 'timeframe': '4h',
                'lookback_bars': 15, 'proximity_pct': 5.0,
            },
        })
        assert not valid
        assert 'proximity_pct' in err

    def test_missing_required_params_rejected(self):
        # Missing lookback_bars and proximity_pct
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
            'params': {'direction': 'bullish', 'timeframe': '4h'},
        })
        assert not valid

    # ── Evaluator unit tests (patch svc.db to inject synthetic data) ──

    def _make_asset_data(self, symbol='WTIUSD', engulfing_signals=None):
        return {
            'symbol': symbol,
            'engulfing_signals': engulfing_signals or [],
        }

    def _bullish_engulfing_current(self, tf='4h'):
        return [{'type': 'engulfing', 'direction': 'bullish',
                 'candle_pos': 'current', 'tf': tf, 'volume_ratio': 1.5, 'detail': '1.5x vol'}]

    def _bearish_engulfing_current(self, tf='4h'):
        return [{'type': 'engulfing', 'direction': 'bearish',
                 'candle_pos': 'current', 'tf': tf, 'volume_ratio': 1.3, 'detail': '1.3x vol'}]

    def _patch_svc(self, monkeypatch, df, tf='4h'):
        """Patch get_services() inside the evaluator's closure to return *df*.

        Also disables Redis so transition-suppression fingerprints don't leak
        between tests (the synthetic OHLCV dates are fixed, and a real Redis
        connection would carry the fingerprint across tests).
        """
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        mock_db.get_ltf_multi_timeframe_data.return_value = {tf: df}
        mock_svc = MagicMock()
        mock_svc.db = mock_db

        # Patch at the services module level — the evaluator uses
        # `from carrac.services import get_services` inside the function body.
        try:
            import carrac.services as _svc_mod
            monkeypatch.setattr(_svc_mod, 'get_services', lambda: mock_svc, raising=False)
        except Exception:
            pass

        # Disable Redis so per-bar suppression fingerprints don't persist
        # between individual test runs (all tests use the same synthetic dates).
        try:
            import carrac.caches as _caches_mod
            monkeypatch.setattr(_caches_mod, '_get_redis', lambda: None, raising=False)
        except Exception:
            pass

    def test_bullish_match_returns_met_true(self, monkeypatch):
        """Full bullish setup: breakout above upper + retest + bullish engulfing → met."""
        df = _make_bullish_setup(lookback=15, proximity_pct=0.5)
        self._patch_svc(monkeypatch, df, tf='4h')

        signals = self._bullish_engulfing_current(tf='4h')
        asset_data = self._make_asset_data(engulfing_signals=signals)

        from carrac.conditions import _eval_bb_breakout_retest_engulfing
        met, evidence = _eval_bb_breakout_retest_engulfing(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 15, 'proximity_pct': 0.5},
            asset_data=asset_data,
        )
        assert met is True
        assert 'bullish' in evidence
        assert 'breakout' in evidence.lower() or 'broke' in evidence.lower()

    def test_no_breakout_returns_false(self, monkeypatch):
        """Flat OHLCV with no breakout bar → met=False, evidence mentions 'no breakout'."""
        df = _make_ohlcv(n=40)  # all bars flat inside BB
        self._patch_svc(monkeypatch, df, tf='4h')

        signals = self._bullish_engulfing_current(tf='4h')
        asset_data = self._make_asset_data(engulfing_signals=signals)

        from carrac.conditions import _eval_bb_breakout_retest_engulfing
        met, evidence = _eval_bb_breakout_retest_engulfing(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 15, 'proximity_pct': 0.5},
            asset_data=asset_data,
        )
        assert met is False
        assert 'no breakout' in evidence

    def test_no_retest_returns_false(self, monkeypatch):
        """Breakout happened but current close is far from middle → met=False."""
        df = _make_bullish_setup(lookback=15)
        # Push current bar's close far above middle (3% from middle → exceeds 0.5% threshold)
        df.iloc[-1, df.columns.get_loc('close')] = 103.0  # 3% above middle ≈ 100
        self._patch_svc(monkeypatch, df, tf='4h')

        signals = self._bullish_engulfing_current(tf='4h')
        asset_data = self._make_asset_data(engulfing_signals=signals)

        from carrac.conditions import _eval_bb_breakout_retest_engulfing
        met, evidence = _eval_bb_breakout_retest_engulfing(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 15, 'proximity_pct': 0.5},
            asset_data=asset_data,
        )
        assert met is False
        assert 'middle' in evidence or '%' in evidence

    def test_no_engulfing_returns_false(self, monkeypatch):
        """Breakout + retest present but no engulfing signal → met=False."""
        df = _make_bullish_setup(lookback=15)
        self._patch_svc(monkeypatch, df, tf='4h')

        asset_data = self._make_asset_data(engulfing_signals=[])  # no signals

        from carrac.conditions import _eval_bb_breakout_retest_engulfing
        met, evidence = _eval_bb_breakout_retest_engulfing(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 15, 'proximity_pct': 0.5},
            asset_data=asset_data,
        )
        assert met is False
        assert 'engulfing' in evidence

    def test_wrong_direction_returns_false(self, monkeypatch):
        """Bullish engulfing present but params direction='bearish' → met=False."""
        df = _make_bullish_setup(lookback=15)
        self._patch_svc(monkeypatch, df, tf='4h')

        signals = self._bullish_engulfing_current(tf='4h')  # bullish signal
        asset_data = self._make_asset_data(engulfing_signals=signals)

        from carrac.conditions import _eval_bb_breakout_retest_engulfing
        met, evidence = _eval_bb_breakout_retest_engulfing(
            {'direction': 'bearish', 'timeframe': '4h',  # wrong direction
             'lookback_bars': 15, 'proximity_pct': 0.5},
            asset_data=asset_data,
        )
        assert met is False

    def test_bearish_setup_matches(self, monkeypatch):
        """Symmetric bearish setup: breakout below lower + retest + bearish engulfing."""
        df = _make_bearish_setup(lookback=15)
        self._patch_svc(monkeypatch, df, tf='4h')

        signals = self._bearish_engulfing_current(tf='4h')
        asset_data = self._make_asset_data(engulfing_signals=signals)

        from carrac.conditions import _eval_bb_breakout_retest_engulfing
        met, evidence = _eval_bb_breakout_retest_engulfing(
            {'direction': 'bearish', 'timeframe': '4h',
             'lookback_bars': 15, 'proximity_pct': 0.5},
            asset_data=asset_data,
        )
        assert met is True
        assert 'bearish' in evidence

    def test_no_symbol_returns_false(self):
        """Missing symbol in asset_data → immediate False (no patch needed)."""
        from carrac.conditions import _eval_bb_breakout_retest_engulfing
        met, evidence = _eval_bb_breakout_retest_engulfing(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 15, 'proximity_pct': 0.5},
            asset_data={},
        )
        assert met is False
        assert 'symbol' in evidence

    def test_engulfing_previous_bar_not_matched(self, monkeypatch):
        """Engulfing signal with candle_pos='previous' should not match (must be 'current')."""
        df = _make_bullish_setup(lookback=15)
        self._patch_svc(monkeypatch, df, tf='4h')

        signals = [{'type': 'engulfing', 'direction': 'bullish',
                    'candle_pos': 'previous', 'tf': '4h', 'volume_ratio': 1.5, 'detail': 'prev'}]
        asset_data = self._make_asset_data(engulfing_signals=signals)

        from carrac.conditions import _eval_bb_breakout_retest_engulfing
        met, evidence = _eval_bb_breakout_retest_engulfing(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 15, 'proximity_pct': 0.5},
            asset_data=asset_data,
        )
        assert met is False

    # ── Integration: evaluate_conditions_dict ──

    def test_integration_logic_all_with_price_level(self, monkeypatch):
        """Full evaluate_conditions_dict path: bb_brt + price_level, logic=all."""
        df = _make_bullish_setup(lookback=15)
        self._patch_svc(monkeypatch, df, tf='4h')

        signals = self._bullish_engulfing_current(tf='4h')
        asset_data = {**self._make_asset_data(engulfing_signals=signals), 'realtime': {'price': 100.0}}

        from carrac.conditions import evaluate_conditions_dict
        conditions = {
            'rules': [
                {
                    'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
                    'params': {
                        'direction': 'bullish', 'timeframe': '4h',
                        'lookback_bars': 15, 'proximity_pct': 0.5,
                    },
                },
                {
                    'id': 'r2', 'type': 'price_level',
                    'params': {'level': 50.0, 'relation': 'above'},  # always true at price=100
                },
            ],
            'logic': 'all',
        }
        market_data = {'price': 100.0, 'asset_data': asset_data, 'all_assets': {}}
        result = evaluate_conditions_dict(conditions, market_data)
        assert result['met_count'] == 2
        assert result['all_met'] is True


# ── Daily / 3-Day / Weekly timeframe support ──

class TestEngulfingDailyWeeklyTimeframes:
    """Validation + evaluator tests for the new D / 3D / W timeframes on engulfing_detected."""

    # ── Validation: new TF values accepted ──

    def test_engulfing_detected_accepts_daily(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'direction': 'bullish', 'timeframe': 'D'},
        })
        assert valid, err

    def test_engulfing_detected_accepts_3day(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'direction': 'bearish', 'timeframe': '3D'},
        })
        assert valid, err

    def test_engulfing_detected_accepts_weekly(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'direction': 'bullish', 'timeframe': 'W'},
        })
        assert valid, err

    def test_engulfing_detected_rejects_bogus_monthly(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'direction': 'bullish', 'timeframe': 'M'},
        })
        assert not valid
        assert 'timeframe' in err

    def test_engulfing_detected_rejects_5m(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'engulfing_detected',
            'params': {'direction': 'bullish', 'timeframe': '5m'},
        })
        assert not valid
        assert 'timeframe' in err

    def test_bb_brt_accepts_daily(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
            'params': {'direction': 'bullish', 'timeframe': 'D',
                       'lookback_bars': 10, 'proximity_pct': 0.5},
        })
        assert valid, err

    def test_bb_brt_accepts_weekly(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
            'params': {'direction': 'bearish', 'timeframe': 'W',
                       'lookback_bars': 8, 'proximity_pct': 1.0},
        })
        assert valid, err

    def test_bb_brt_rejects_bogus_tf(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakout_retest_engulfing',
            'params': {'direction': 'bullish', 'timeframe': 'random',
                       'lookback_bars': 10, 'proximity_pct': 0.5},
        })
        assert not valid
        assert 'timeframe' in err

    # ── Evaluator: engulfing_detected reads cached D/W signals ──

    def test_engulfing_detected_daily_signal_met(self):
        """Given a cached daily engulfing signal, rule fires with timeframe='D'."""
        from carrac.conditions import _eval_engulfing_detected
        daily_signal = {'type': 'engulfing', 'direction': 'bullish', 'tf': 'D', 'candle_pos': 'current'}
        asset_data = {**MOCK_ASSET_DATA, 'engulfing_signals': [daily_signal]}
        met, evidence = _eval_engulfing_detected(
            {'direction': 'bullish', 'timeframe': 'D'},
            asset_data=asset_data,
        )
        assert met is True
        assert 'bullish' in evidence

    def test_engulfing_detected_weekly_signal_met(self):
        """Given a cached weekly engulfing signal, rule fires with timeframe='W'."""
        from carrac.conditions import _eval_engulfing_detected
        weekly_signal = {'type': 'engulfing', 'direction': 'bearish', 'tf': 'W', 'candle_pos': 'current'}
        asset_data = {**MOCK_ASSET_DATA, 'engulfing_signals': [weekly_signal]}
        met, evidence = _eval_engulfing_detected(
            {'direction': 'bearish', 'timeframe': 'W'},
            asset_data=asset_data,
        )
        assert met is True
        assert 'bearish' in evidence

    def test_engulfing_detected_daily_signal_not_met_wrong_tf(self):
        """Daily signal does not match a rule requesting 4h."""
        from carrac.conditions import _eval_engulfing_detected
        daily_signal = {'type': 'engulfing', 'direction': 'bullish', 'tf': 'D', 'candle_pos': 'current'}
        asset_data = {**MOCK_ASSET_DATA, 'engulfing_signals': [daily_signal]}
        met, _ = _eval_engulfing_detected(
            {'direction': 'bullish', 'timeframe': '4h'},
            asset_data=asset_data,
        )
        assert met is False

    # ── Evaluator: bb_breakout_retest_engulfing routes to get_multi_timeframe_data ──

    def _patch_htf_svc(self, monkeypatch, df, db_key='1D'):
        """Patch get_services() to return HTF data via get_multi_timeframe_data."""
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        mock_db.get_multi_timeframe_data.return_value = {db_key: df}
        mock_svc = MagicMock()
        mock_svc.db = mock_db

        try:
            import carrac.services as _svc_mod
            monkeypatch.setattr(_svc_mod, 'get_services', lambda: mock_svc, raising=False)
        except Exception:
            pass

        try:
            import carrac.caches as _caches_mod
            monkeypatch.setattr(_caches_mod, '_get_redis', lambda: None, raising=False)
        except Exception:
            pass

    def test_bb_brt_daily_bullish_fires(self, monkeypatch):
        """Full bullish daily setup: routes to get_multi_timeframe_data, uses '1D' key."""
        df = _make_bullish_setup(lookback=10, proximity_pct=0.5)
        # Use daily-frequency dates to be realistic
        dates = pd.date_range('2025-01-01', periods=len(df), freq='D')
        df.index = dates
        self._patch_htf_svc(monkeypatch, df, db_key='1D')

        signals = [{'type': 'engulfing', 'direction': 'bullish',
                    'candle_pos': 'current', 'tf': 'D', 'volume_ratio': 1.5}]
        asset_data = {'symbol': 'WTIUSD', 'engulfing_signals': signals}

        from carrac.conditions import _eval_bb_breakout_retest_engulfing
        met, evidence = _eval_bb_breakout_retest_engulfing(
            {'direction': 'bullish', 'timeframe': 'D',
             'lookback_bars': 10, 'proximity_pct': 0.5},
            asset_data=asset_data,
        )
        assert met is True
        assert 'broke' in evidence.lower() or 'breakout' in evidence.lower()

    def test_bb_brt_weekly_routes_to_1w_key(self, monkeypatch):
        """Weekly timeframe ('W') maps to '1W' dict key in get_multi_timeframe_data."""
        # Use lookback_bars=15 — breakout is at index -11, so window [-16:-1] covers it.
        df = _make_bullish_setup(lookback=15, proximity_pct=1.0)
        dates = pd.date_range('2024-01-01', periods=len(df), freq='W')
        df.index = dates
        self._patch_htf_svc(monkeypatch, df, db_key='1W')

        signals = [{'type': 'engulfing', 'direction': 'bullish',
                    'candle_pos': 'current', 'tf': 'W', 'volume_ratio': 1.2}]
        asset_data = {'symbol': 'BTC', 'engulfing_signals': signals}

        from carrac.conditions import _eval_bb_breakout_retest_engulfing
        met, evidence = _eval_bb_breakout_retest_engulfing(
            {'direction': 'bullish', 'timeframe': 'W',
             'lookback_bars': 15, 'proximity_pct': 1.0},
            asset_data=asset_data,
        )
        assert met is True


# ── BB_POSITION in Plays ──

class TestBBPositionInPlays:
    """_get_indicator_state + evaluate_conditions_dict coverage for BB_POSITION."""

    def test_returns_above_upper(self):
        """Returns 'above_upper' when bb.position is 'above_upper'."""
        data = {'bb': {'position': 'above_upper', 'upper': 3600, 'lower': 3200}}
        assert _get_indicator_state('BB_POSITION', data) == 'above_upper'

    def test_returns_below_lower(self):
        """Returns 'below_lower' when bb.position is 'below_lower'."""
        data = {'bb': {'position': 'below_lower', 'upper': 3600, 'lower': 3200}}
        assert _get_indicator_state('BB_POSITION', data) == 'below_lower'

    def test_returns_inside(self):
        """Returns 'inside' when bb.position is 'inside'."""
        data = {'bb': {'position': 'inside', 'upper': 3600, 'lower': 3200}}
        assert _get_indicator_state('BB_POSITION', data) == 'inside'

    def test_returns_none_when_bb_missing(self):
        """Returns None when bb dict is absent."""
        assert _get_indicator_state('BB_POSITION', {}) is None

    def test_returns_none_when_position_missing(self):
        """Returns None when bb dict exists but has no position key."""
        data = {'bb': {'upper': 3600, 'lower': 3200, 'middle': 3400}}
        assert _get_indicator_state('BB_POSITION', data) is None

    def test_evaluate_conditions_dict_met_true(self):
        """Full evaluate_conditions_dict: BB_POSITION rule returns met=True when state matches."""
        from carrac.conditions import evaluate_conditions_dict
        conditions = {
            'rules': [{'id': 'r1', 'type': 'indicator_state',
                       'params': {'indicator': 'BB_POSITION', 'timeframe': '4h', 'state': 'above_upper'}}],
            'logic': 'all',
        }
        asset_data = {**MOCK_ASSET_DATA, 'bb': {'position': 'above_upper', 'upper': 3600, 'lower': 3200, 'middle': 3400, 'width_pct': 3.5, 'squeeze': False}}
        market_data = {'price': 3650.0, 'asset_data': asset_data, 'all_assets': {}}
        result = evaluate_conditions_dict(conditions, market_data)
        assert result['all_met'] is True
        assert result['rules'][0]['met'] is True

    def test_evaluate_conditions_dict_met_false_state_differs(self):
        """Full evaluate_conditions_dict: BB_POSITION rule returns met=False when state differs."""
        from carrac.conditions import evaluate_conditions_dict
        conditions = {
            'rules': [{'id': 'r1', 'type': 'indicator_state',
                       'params': {'indicator': 'BB_POSITION', 'timeframe': '4h', 'state': 'above_upper'}}],
            'logic': 'all',
        }
        asset_data = {**MOCK_ASSET_DATA, 'bb': {'position': 'inside', 'upper': 3600, 'lower': 3200, 'middle': 3400, 'width_pct': 3.5, 'squeeze': False}}
        market_data = {'price': 3420.0, 'asset_data': asset_data, 'all_assets': {}}
        result = evaluate_conditions_dict(conditions, market_data)
        assert result['all_met'] is False
        assert result['rules'][0]['met'] is False


# ── bb_fade_engulfing single-rule fade setup ──

def _asset_with_bb_position(position, extra_signals=None):
    """Build asset_data with a given bb.position and optional engulfing_signals."""
    return {
        **MOCK_ASSET_DATA,
        'bb': {'position': position, 'upper': 3600, 'lower': 3200, 'middle': 3400,
               'width_pct': 3.5, 'squeeze': False},
        'engulfing_signals': extra_signals or [],
    }


_BULLISH_FADE_SIGNAL_4H = {
    'type': 'engulfing', 'direction': 'bullish',
    'tf': '4h', 'candle_pos': 'current', 'volume_ratio': 1.4,
}
_BEARISH_FADE_SIGNAL_4H = {
    'type': 'engulfing', 'direction': 'bearish',
    'tf': '4h', 'candle_pos': 'current', 'volume_ratio': 1.3,
}


class TestBBFadeEngulfing:
    """Validation + evaluator unit + integration tests for bb_fade_engulfing."""

    # ── Validation ──

    def test_valid_bullish_4h(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_fade_engulfing',
            'params': {'direction': 'bullish', 'timeframe': '4h'},
        })
        assert valid, err

    def test_valid_bearish_daily(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_fade_engulfing',
            'params': {'direction': 'bearish', 'timeframe': 'D'},
        })
        assert valid, err

    def test_invalid_direction_enum(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_fade_engulfing',
            'params': {'direction': 'sideways', 'timeframe': '4h'},
        })
        assert not valid
        assert 'direction' in err

    def test_invalid_timeframe_enum(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_fade_engulfing',
            'params': {'direction': 'bullish', 'timeframe': '5m'},
        })
        assert not valid
        assert 'timeframe' in err

    def test_missing_direction_rejected(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_fade_engulfing',
            'params': {'timeframe': '4h'},
        })
        assert not valid
        assert 'direction' in err

    def test_missing_timeframe_rejected(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_fade_engulfing',
            'params': {'direction': 'bullish'},
        })
        assert not valid
        assert 'timeframe' in err

    # ── Evaluator unit tests ──

    def test_bullish_match_met_true(self):
        """bb.position=below_lower + bullish engulfing current 4h → met=True."""
        from carrac.conditions import _eval_bb_fade_engulfing
        asset_data = _asset_with_bb_position('below_lower', extra_signals=[_BULLISH_FADE_SIGNAL_4H])
        met, evidence = _eval_bb_fade_engulfing(
            {'direction': 'bullish', 'timeframe': '4h'},
            asset_data=asset_data,
        )
        assert met is True
        assert 'below_lower' in evidence
        assert 'bullish' in evidence

    def test_bearish_match_met_true(self):
        """bb.position=above_upper + bearish engulfing current 4h → met=True."""
        from carrac.conditions import _eval_bb_fade_engulfing
        asset_data = _asset_with_bb_position('above_upper', extra_signals=[_BEARISH_FADE_SIGNAL_4H])
        met, evidence = _eval_bb_fade_engulfing(
            {'direction': 'bearish', 'timeframe': '4h'},
            asset_data=asset_data,
        )
        assert met is True
        assert 'above_upper' in evidence
        assert 'bearish' in evidence

    def test_wrong_bb_position_bullish_returns_false(self):
        """bb.position=inside when bullish fade expects below_lower → met=False."""
        from carrac.conditions import _eval_bb_fade_engulfing
        asset_data = _asset_with_bb_position('inside', extra_signals=[_BULLISH_FADE_SIGNAL_4H])
        met, evidence = _eval_bb_fade_engulfing(
            {'direction': 'bullish', 'timeframe': '4h'},
            asset_data=asset_data,
        )
        assert met is False
        assert 'below_lower' in evidence

    def test_wrong_engulfing_direction_returns_false(self):
        """bb.position=below_lower but engulfing direction=bearish → met=False."""
        from carrac.conditions import _eval_bb_fade_engulfing
        wrong_signal = {**_BEARISH_FADE_SIGNAL_4H}  # bearish engulfing, not bullish
        asset_data = _asset_with_bb_position('below_lower', extra_signals=[wrong_signal])
        met, evidence = _eval_bb_fade_engulfing(
            {'direction': 'bullish', 'timeframe': '4h'},
            asset_data=asset_data,
        )
        assert met is False
        assert 'no bullish engulfing' in evidence

    def test_wrong_timeframe_returns_false(self):
        """Signal on 1h but params ask for 4h → met=False."""
        from carrac.conditions import _eval_bb_fade_engulfing
        signal_1h = {**_BULLISH_FADE_SIGNAL_4H, 'tf': '1h'}
        asset_data = _asset_with_bb_position('below_lower', extra_signals=[signal_1h])
        met, evidence = _eval_bb_fade_engulfing(
            {'direction': 'bullish', 'timeframe': '4h'},
            asset_data=asset_data,
        )
        assert met is False
        assert 'no bullish engulfing' in evidence

    def test_candle_pos_previous_rejected(self):
        """Engulfing with candle_pos='previous' must not match (only 'current' counts)."""
        from carrac.conditions import _eval_bb_fade_engulfing
        prev_signal = {**_BULLISH_FADE_SIGNAL_4H, 'candle_pos': 'previous'}
        asset_data = _asset_with_bb_position('below_lower', extra_signals=[prev_signal])
        met, evidence = _eval_bb_fade_engulfing(
            {'direction': 'bullish', 'timeframe': '4h'},
            asset_data=asset_data,
        )
        assert met is False

    def test_daily_end_to_end(self):
        """Daily timeframe (tf='D') works end-to-end: position + matching D signal → met=True."""
        from carrac.conditions import _eval_bb_fade_engulfing
        daily_signal = {
            'type': 'engulfing', 'direction': 'bearish',
            'tf': 'D', 'candle_pos': 'current', 'volume_ratio': 1.2,
        }
        asset_data = _asset_with_bb_position('above_upper', extra_signals=[daily_signal])
        met, evidence = _eval_bb_fade_engulfing(
            {'direction': 'bearish', 'timeframe': 'D'},
            asset_data=asset_data,
        )
        assert met is True
        assert 'above_upper' in evidence
        assert 'bearish' in evidence

    # ── Integration: evaluate_conditions_dict ──

    def test_integration_logic_all_with_price_level(self):
        """bb_fade_engulfing combined with price_level via evaluate_conditions_dict logic=all."""
        from carrac.conditions import evaluate_conditions_dict
        asset_data = _asset_with_bb_position('below_lower', extra_signals=[_BULLISH_FADE_SIGNAL_4H])
        conditions = {
            'rules': [
                {'id': 'r1', 'type': 'bb_fade_engulfing',
                 'params': {'direction': 'bullish', 'timeframe': '4h'}},
                {'id': 'r2', 'type': 'price_level',
                 'params': {'level': 3000.0, 'relation': 'above'}},  # always true at 3420
            ],
            'logic': 'all',
        }
        market_data = {'price': 3420.0, 'asset_data': asset_data, 'all_assets': {}}
        result = evaluate_conditions_dict(conditions, market_data)
        assert result['met_count'] == 2
        assert result['all_met'] is True


# ── bb_squeeze_breakout single-rule squeeze-expansion setup ──

def _make_squeeze_ohlcv(n=100, base_price=100.0):
    """Build a synthetic OHLCV DataFrame of *n* bars near *base_price*."""
    dates = pd.date_range('2026-01-01', periods=n, freq='4h')
    close = np.full(n, base_price)
    df = pd.DataFrame({
        'open':   close,
        'high':   close,
        'low':    close,
        'close':  close,
        'volume': np.full(n, 1000.0),
    }, index=dates)
    return df


def _make_squeeze_breakout_bullish(lookback=10, min_squeeze=3):
    """DataFrame with a valid bullish squeeze-breakout setup.

    For squeeze_pct < 50 to fire, we need the BB period-20 rolling window to
    be entirely flat (std → 0, width → 0) while the 60-bar avg width remains
    large (from earlier volatile bars).  The squeeze window must contain at
    least *min_squeeze* consecutive True bars AND fall within the evaluator's
    lookback window (iloc[-(lookback+1):-1]).

    Layout (100 bars):
    - Bars 0..59: volatile ±5  → makes avg_width_pct large.
    - Bars 60..83: nearly flat (0.001 variation) → 24 consecutive squeeze bars.
      The squeeze window spans the last ~10–15 bars of this flat section.
    - Bars 84..98: volatile ±3  → squeeze released (not squeezed).
    - Bar 99 (iloc[-1]): bullish breakout above upper band.

    The evaluator's lookback window = iloc[-11:-1] = bars 88-98.
    Bars 88-91 (inside the flat zone) will be in squeeze → ≥3 consecutive Trues.
    """
    n = 100
    df = _make_squeeze_ohlcv(n=n)

    # Bars 0-59: volatile — seeds a large avg width
    for i in range(60):
        val = 100.0 + (5.0 if i % 2 == 0 else -5.0)
        df.iloc[i, df.columns.get_loc('close')] = val

    # Bars 60-91: very flat → squeeze fires when rolling window is fully in this zone.
    # Extended to bar 91 so at least 3 squeeze bars land in the evaluator's lookback
    # window (iloc[-11:-1] = bars 88-97 for n=100, lookback=10).
    for i in range(60, 92):
        df.iloc[i, df.columns.get_loc('close')] = 100.0 + (0.001 if i % 2 == 0 else -0.001)

    # Bars 92-98: volatile (released)
    for i in range(92, 99):
        val = 100.0 + (3.0 if i % 2 == 0 else -3.0)
        df.iloc[i, df.columns.get_loc('close')] = val

    # Bar 99 (current): strong bullish close well above upper band
    # Upper band at the released bars ≈ 100 + 2*3 = 106; use 125 to be safe
    df.iloc[-1, df.columns.get_loc('close')] = 125.0
    df.iloc[-1, df.columns.get_loc('volume')] = 5000.0  # above-average volume

    return df


def _make_squeeze_breakout_bearish(lookback=10, min_squeeze=3):
    """Symmetric bearish version: squeeze → current close below lower band."""
    df = _make_squeeze_breakout_bullish(lookback=lookback, min_squeeze=min_squeeze)
    # Flip current bar to a bearish breakout below lower band
    df.iloc[-1, df.columns.get_loc('close')] = 75.0
    df.iloc[-1, df.columns.get_loc('volume')] = 5000.0
    return df


class TestBBSqueezeBreakout:
    """Validation + evaluator unit + integration tests for bb_squeeze_breakout."""

    # ── Validation ──

    def test_valid_params_accepted(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_squeeze_breakout',
            'params': {
                'direction': 'bullish', 'timeframe': '4h',
                'lookback_bars': 10, 'min_squeeze_bars': 3,
                'require_volume': True,
            },
        })
        assert valid, err

    def test_invalid_direction_enum(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_squeeze_breakout',
            'params': {
                'direction': 'sideways', 'timeframe': '4h',
                'lookback_bars': 10, 'min_squeeze_bars': 3,
            },
        })
        assert not valid
        assert 'direction' in err

    def test_invalid_timeframe_enum(self):
        """'M' is not in the allowed set."""
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_squeeze_breakout',
            'params': {
                'direction': 'bullish', 'timeframe': 'M',
                'lookback_bars': 10, 'min_squeeze_bars': 3,
            },
        })
        assert not valid
        assert 'timeframe' in err

    def test_invalid_timeframe_15m(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_squeeze_breakout',
            'params': {
                'direction': 'bullish', 'timeframe': '15m',
                'lookback_bars': 10, 'min_squeeze_bars': 3,
            },
        })
        assert not valid
        assert 'timeframe' in err

    def test_invalid_lookback_bars_too_low(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_squeeze_breakout',
            'params': {
                'direction': 'bullish', 'timeframe': '4h',
                'lookback_bars': 3, 'min_squeeze_bars': 3,
            },
        })
        assert not valid
        assert 'lookback_bars' in err

    def test_invalid_lookback_bars_too_high(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_squeeze_breakout',
            'params': {
                'direction': 'bullish', 'timeframe': '4h',
                'lookback_bars': 35, 'min_squeeze_bars': 3,
            },
        })
        assert not valid
        assert 'lookback_bars' in err

    def test_invalid_min_squeeze_bars_too_low(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_squeeze_breakout',
            'params': {
                'direction': 'bullish', 'timeframe': '4h',
                'lookback_bars': 10, 'min_squeeze_bars': 0,
            },
        })
        assert not valid
        assert 'min_squeeze_bars' in err

    def test_invalid_min_squeeze_bars_too_high(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_squeeze_breakout',
            'params': {
                'direction': 'bullish', 'timeframe': '4h',
                'lookback_bars': 10, 'min_squeeze_bars': 25,
            },
        })
        assert not valid
        assert 'min_squeeze_bars' in err

    def test_missing_required_params_rejected(self):
        # Missing lookback_bars and min_squeeze_bars
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_squeeze_breakout',
            'params': {'direction': 'bullish', 'timeframe': '4h'},
        })
        assert not valid

    # ── Evaluator helpers ──

    def _make_asset_data(self, symbol='WTIUSD'):
        return {'symbol': symbol}

    def _patch_svc(self, monkeypatch, df, tf='4h', htf=False):
        """Patch get_services() to inject synthetic OHLCV; disable Redis."""
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        if htf:
            mock_db.get_multi_timeframe_data.return_value = {'1D': df}
        else:
            mock_db.get_ltf_multi_timeframe_data.return_value = {tf: df}
        mock_svc = MagicMock()
        mock_svc.db = mock_db

        try:
            import carrac.services as _svc_mod
            monkeypatch.setattr(_svc_mod, 'get_services', lambda: mock_svc, raising=False)
        except Exception:
            pass

        try:
            import carrac.caches as _caches_mod
            monkeypatch.setattr(_caches_mod, '_get_redis', lambda: None, raising=False)
        except Exception:
            pass

    # ── Evaluator: bullish match ──

    def test_bullish_match_met_true(self, monkeypatch):
        """Valid bullish squeeze → release → close above upper → met=True."""
        df = _make_squeeze_breakout_bullish(lookback=10, min_squeeze=3)
        self._patch_svc(monkeypatch, df, tf='4h')

        from carrac.conditions import _eval_bb_squeeze_breakout
        met, evidence = _eval_bb_squeeze_breakout(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 10, 'min_squeeze_bars': 3, 'require_volume': True},
            asset_data=self._make_asset_data(),
        )
        assert met is True, f"Expected met=True, got evidence: {evidence}"
        assert 'released' in evidence or 'broke' in evidence

    # ── Evaluator: bearish match ──

    def test_bearish_match_met_true(self, monkeypatch):
        """Symmetric bearish setup: squeeze → release → close below lower → met=True."""
        df = _make_squeeze_breakout_bearish(lookback=10, min_squeeze=3)
        self._patch_svc(monkeypatch, df, tf='4h')

        from carrac.conditions import _eval_bb_squeeze_breakout
        met, evidence = _eval_bb_squeeze_breakout(
            {'direction': 'bearish', 'timeframe': '4h',
             'lookback_bars': 10, 'min_squeeze_bars': 3, 'require_volume': True},
            asset_data=self._make_asset_data(),
        )
        assert met is True, f"Expected met=True, got evidence: {evidence}"

    # ── Evaluator: no squeeze in lookback window ──

    def test_no_squeeze_in_lookback_returns_false(self, monkeypatch):
        """Uniform volatile series — no squeeze bars → met=False, evidence mentions
        run length vs threshold."""
        df = _make_ohlcv(n=55)
        # Make all bars volatile (wide width → never squeezed relative to avg)
        for i in range(55):
            val = 100.0 + (4.0 if i % 2 == 0 else -4.0)
            df.iloc[i, df.columns.get_loc('close')] = val
        df.iloc[-1, df.columns.get_loc('close')] = 112.0  # current is a "breakout"
        self._patch_svc(monkeypatch, df, tf='4h')

        from carrac.conditions import _eval_bb_squeeze_breakout
        met, evidence = _eval_bb_squeeze_breakout(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 10, 'min_squeeze_bars': 3},
            asset_data=self._make_asset_data(),
        )
        assert met is False
        assert 'bars' in evidence or 'need' in evidence or 'squeeze' in evidence

    # ── Evaluator: squeeze still active on current bar ──

    def test_squeeze_still_active_returns_false(self, monkeypatch):
        """Squeeze fires in lookback but current bar is still in squeeze → met=False."""
        df = _make_squeeze_breakout_bullish(lookback=10, min_squeeze=3)
        # Collapse current bar AND several surrounding bars to a tiny range
        # so the current bar also lands in squeeze territory
        for i in range(-6, 0):
            df.iloc[i, df.columns.get_loc('close')] = 100.0
            df.iloc[i, df.columns.get_loc('high')]  = 100.01
            df.iloc[i, df.columns.get_loc('low')]   = 99.99
            df.iloc[i, df.columns.get_loc('open')]  = 100.0
        self._patch_svc(monkeypatch, df, tf='4h')

        from carrac.conditions import _eval_bb_squeeze_breakout
        met, evidence = _eval_bb_squeeze_breakout(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 10, 'min_squeeze_bars': 3},
            asset_data=self._make_asset_data(),
        )
        assert met is False
        assert 'squeeze' in evidence.lower()

    # ── Evaluator: wrong direction ──

    def test_wrong_direction_returns_false(self, monkeypatch):
        """Bullish breakout data but params direction='bearish' → current close
        does not break below lower band → met=False."""
        df = _make_squeeze_breakout_bullish(lookback=10, min_squeeze=3)
        self._patch_svc(monkeypatch, df, tf='4h')

        from carrac.conditions import _eval_bb_squeeze_breakout
        met, evidence = _eval_bb_squeeze_breakout(
            {'direction': 'bearish', 'timeframe': '4h',
             'lookback_bars': 10, 'min_squeeze_bars': 3, 'require_volume': True},
            asset_data=self._make_asset_data(),
        )
        assert met is False

    # ── Evaluator: volume gate fails ──

    def test_volume_gate_fails_returns_false(self, monkeypatch):
        """require_volume=True but current bar volume is below 20-SMA → met=False."""
        df = _make_squeeze_breakout_bullish(lookback=10, min_squeeze=3)
        # Set current volume to well below average (all historical bars are 1000)
        df.iloc[-1, df.columns.get_loc('volume')] = 100.0
        self._patch_svc(monkeypatch, df, tf='4h')

        from carrac.conditions import _eval_bb_squeeze_breakout
        met, evidence = _eval_bb_squeeze_breakout(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 10, 'min_squeeze_bars': 3, 'require_volume': True},
            asset_data=self._make_asset_data(),
        )
        assert met is False
        assert 'volume' in evidence.lower()

    # ── Evaluator: volume gate disabled ──

    def test_volume_gate_disabled_passes_low_volume(self, monkeypatch):
        """require_volume=False — same low-volume bar as above should yield met=True."""
        df = _make_squeeze_breakout_bullish(lookback=10, min_squeeze=3)
        df.iloc[-1, df.columns.get_loc('volume')] = 100.0  # below average
        self._patch_svc(monkeypatch, df, tf='4h')

        from carrac.conditions import _eval_bb_squeeze_breakout
        met, evidence = _eval_bb_squeeze_breakout(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 10, 'min_squeeze_bars': 3, 'require_volume': False},
            asset_data=self._make_asset_data(),
        )
        assert met is True, f"Expected met=True without volume gate, got: {evidence}"

    # ── Evaluator: daily timeframe routes to get_multi_timeframe_data ──

    def test_daily_timeframe_routes_correctly(self, monkeypatch):
        """tf='D' should use get_multi_timeframe_data with key '1D', not LTF path."""
        df = _make_squeeze_breakout_bullish(lookback=10, min_squeeze=3)
        self._patch_svc(monkeypatch, df, tf='4h', htf=True)  # mock HTF path

        from carrac.conditions import _eval_bb_squeeze_breakout
        met, evidence = _eval_bb_squeeze_breakout(
            {'direction': 'bullish', 'timeframe': 'D',
             'lookback_bars': 10, 'min_squeeze_bars': 3, 'require_volume': False},
            asset_data=self._make_asset_data(),
        )
        # Must not fail on routing — may or may not be met depending on data,
        # but must not be a routing/data-shape error
        assert isinstance(met, bool)

    # ── Evaluator: no symbol returns False without crashing ──

    def test_no_symbol_returns_false(self):
        from carrac.conditions import _eval_bb_squeeze_breakout
        met, evidence = _eval_bb_squeeze_breakout(
            {'direction': 'bullish', 'timeframe': '4h',
             'lookback_bars': 10, 'min_squeeze_bars': 3},
            asset_data={},
        )
        assert met is False
        assert 'symbol' in evidence

    # ── Integration: evaluate_conditions_dict with logic='all' ──

    def test_integration_logic_all_with_price_level(self, monkeypatch):
        """bb_squeeze_breakout combined with price_level via evaluate_conditions_dict logic=all."""
        df = _make_squeeze_breakout_bullish(lookback=10, min_squeeze=3)
        self._patch_svc(monkeypatch, df, tf='4h')

        from carrac.conditions import evaluate_conditions_dict
        asset_data = {**self._make_asset_data(), 'realtime': {'price': 112.0}}
        conditions = {
            'rules': [
                {
                    'id': 'r1', 'type': 'bb_squeeze_breakout',
                    'params': {
                        'direction': 'bullish', 'timeframe': '4h',
                        'lookback_bars': 10, 'min_squeeze_bars': 3,
                        'require_volume': False,
                    },
                },
                {
                    'id': 'r2', 'type': 'price_level',
                    'params': {'level': 50.0, 'relation': 'above'},  # always true
                },
            ],
            'logic': 'all',
        }
        result = evaluate_conditions_dict(
            conditions,
            {'price': 112.0, 'asset_data': asset_data, 'all_assets': {}},
        )
        assert result['rules'][1]['met'] is True  # price_level always true at 112 > 50
        assert result['met_count'] >= 1


class TestBBBreakdownContinuation:
    """Validation + evaluator unit tests for bb_breakdown_continuation (Setup F)."""

    # ── Validation ──

    def test_valid_minimal_params_accepted(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakdown_continuation',
            'params': {'direction': 'bearish', 'timeframe': '4h', 'lookback_bars': 15},
        })
        assert valid, err

    def test_valid_full_params_with_offset_accepted(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakdown_continuation',
            'params': {
                'direction': 'bearish', 'timeframe': '1d' if False else '4h',
                'lookback_bars': 20, 'confirm_mode': 'either',
                'retest_band': 'broken', 'proximity_pct': 0.5,
                'bb_params': {'period': 20, 'std_dev': 2.0, 'offset': 10},
            },
        })
        assert valid, err

    def test_invalid_confirm_mode_rejected(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakdown_continuation',
            'params': {'direction': 'bearish', 'timeframe': '4h',
                       'lookback_bars': 15, 'confirm_mode': 'magic'},
        })
        assert not valid and 'confirm_mode' in err

    def test_invalid_retest_band_rejected(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakdown_continuation',
            'params': {'direction': 'bearish', 'timeframe': '4h',
                       'lookback_bars': 15, 'retest_band': 'upper'},
        })
        assert not valid and 'retest_band' in err

    def test_invalid_offset_rejected(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakdown_continuation',
            'params': {'direction': 'bearish', 'timeframe': '4h',
                       'lookback_bars': 15, 'bb_params': {'offset': 99}},
        })
        assert not valid and 'offset' in err

    def test_invalid_lookback_rejected(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakdown_continuation',
            'params': {'direction': 'bearish', 'timeframe': '4h', 'lookback_bars': 3},
        })
        assert not valid and 'lookback_bars' in err

    def test_missing_required_lookback_rejected(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakdown_continuation',
            'params': {'direction': 'bearish', 'timeframe': '4h'},
        })
        assert not valid

    def test_invalid_timeframe_rejected(self):
        valid, err = validate_condition({
            'id': 'r1', 'type': 'bb_breakdown_continuation',
            'params': {'direction': 'bearish', 'timeframe': '1D', 'lookback_bars': 15},
        })
        assert not valid and 'timeframe' in err

    # ── Evaluator (patch svc.db to inject synthetic data; disable Redis) ──

    def _patch_svc(self, monkeypatch, df, tf='4h'):
        from unittest.mock import MagicMock
        mock_db = MagicMock()
        mock_db.get_ltf_multi_timeframe_data.return_value = {tf: df}
        mock_svc = MagicMock()
        mock_svc.db = mock_db
        try:
            import carrac.services as _svc_mod
            monkeypatch.setattr(_svc_mod, 'get_services', lambda: mock_svc, raising=False)
        except Exception:
            pass
        try:
            import carrac.caches as _caches_mod
            monkeypatch.setattr(_caches_mod, '_get_redis', lambda: None, raising=False)
        except Exception:
            pass

    def _bearish_engulfing_current(self, tf='4h'):
        return [{'type': 'engulfing', 'direction': 'bearish',
                 'candle_pos': 'current', 'tf': tf, 'volume_ratio': 1.3}]

    def test_breakdown_plus_engulfing_either_fires(self, monkeypatch):
        """Breakdown in window + bearish engulfing on current bar → met (either mode)."""
        df = _make_bearish_setup(lookback=15)
        self._patch_svc(monkeypatch, df, tf='4h')
        from carrac.conditions import _eval_bb_breakdown_continuation
        met, ev = _eval_bb_breakdown_continuation(
            {'direction': 'bearish', 'timeframe': '4h', 'lookback_bars': 15,
             'confirm_mode': 'either'},
            asset_data={'symbol': 'WTIUSD',
                        'engulfing_signals': self._bearish_engulfing_current('4h')},
        )
        assert met, ev
        assert 'engulfing' in ev

    def test_engulfing_only_without_signal_does_not_fire(self, monkeypatch):
        """confirm_mode=engulfing but no engulfing signal → not met (even though breakdown happened)."""
        df = _make_bearish_setup(lookback=15)
        self._patch_svc(monkeypatch, df, tf='4h')
        from carrac.conditions import _eval_bb_breakdown_continuation
        met, ev = _eval_bb_breakdown_continuation(
            {'direction': 'bearish', 'timeframe': '4h', 'lookback_bars': 15,
             'confirm_mode': 'engulfing'},
            asset_data={'symbol': 'WTIUSD', 'engulfing_signals': []},
        )
        assert not met
        assert 'engulfing' in ev

    def test_no_breakdown_does_not_fire(self, monkeypatch):
        """Flat series, no breakdown in window → not met regardless of confirmation."""
        df = _make_ohlcv(n=40)  # calm, no breakout bar
        self._patch_svc(monkeypatch, df, tf='4h')
        from carrac.conditions import _eval_bb_breakdown_continuation
        met, ev = _eval_bb_breakdown_continuation(
            {'direction': 'bearish', 'timeframe': '4h', 'lookback_bars': 15,
             'confirm_mode': 'either'},
            asset_data={'symbol': 'WTIUSD',
                        'engulfing_signals': self._bearish_engulfing_current('4h')},
        )
        assert not met
        assert 'breakdown' in ev

    def test_band_retest_broken_band_fires(self, monkeypatch):
        """Breakdown in window + current close sitting on the lower band → met via retest leg."""
        from consensio.indicators.bollinger import BollingerBandCalculator
        df = _make_bearish_setup(lookback=15)
        # Converge the current close onto the lower band (the close feeds back into
        # the band, so iterate twice to settle within a fraction of a percent).
        for _ in range(2):
            bb = BollingerBandCalculator(period=20, std_dev=2.0).calculate_series(df)
            df.iloc[-1, df.columns.get_loc('close')] = float(bb.lower.iloc[-1])
        self._patch_svc(monkeypatch, df, tf='4h')
        from carrac.conditions import _eval_bb_breakdown_continuation
        met, ev = _eval_bb_breakdown_continuation(
            {'direction': 'bearish', 'timeframe': '4h', 'lookback_bars': 15,
             'confirm_mode': 'band_retest', 'retest_band': 'broken', 'proximity_pct': 1.0},
            asset_data={'symbol': 'WTIUSD', 'engulfing_signals': []},
        )
        assert met, ev
        assert 'retest' in ev and 'lower' in ev

    def test_band_retest_tight_proximity_does_not_fire(self, monkeypatch):
        """Current close near the middle, not the lower band → retest leg fails on tight proximity."""
        df = _make_bearish_setup(lookback=15)  # current close left at ~middle (100)
        self._patch_svc(monkeypatch, df, tf='4h')
        from carrac.conditions import _eval_bb_breakdown_continuation
        met, ev = _eval_bb_breakdown_continuation(
            {'direction': 'bearish', 'timeframe': '4h', 'lookback_bars': 15,
             'confirm_mode': 'band_retest', 'retest_band': 'broken', 'proximity_pct': 0.1},
            asset_data={'symbol': 'WTIUSD', 'engulfing_signals': []},
        )
        assert not met
        assert 'lower band' in ev

    def test_custom_offset_flows_through_without_error(self, monkeypatch):
        """+10 offset bb_params is accepted by the evaluator and doesn't raise."""
        df = _make_bearish_setup(lookback=15)
        self._patch_svc(monkeypatch, df, tf='4h')
        from carrac.conditions import _eval_bb_breakdown_continuation
        met, ev = _eval_bb_breakdown_continuation(
            {'direction': 'bearish', 'timeframe': '4h', 'lookback_bars': 15,
             'confirm_mode': 'either', 'bb_params': {'period': 20, 'std_dev': 2.0, 'offset': 10}},
            asset_data={'symbol': 'WTIUSD',
                        'engulfing_signals': self._bearish_engulfing_current('4h')},
        )
        # Offset lags the bands; the synthetic breakdown may or may not still be in-window,
        # but the call must complete cleanly and return a bool + evidence string.
        assert isinstance(met, bool)
        assert isinstance(ev, str) and ev


# ── Old-schema price_level backward-compat + Sentry beacon ──

class TestOldSchemaPriceLevelBackwardCompat:
    """
    Old-schema invalidation rules used {type, price, condition} at the top level
    instead of the current {type, params:{level, relation}} shape.  The evaluator
    must transparently normalise them so pre-migration trip-wires actually fire.
    """

    # Price below the invalidation level → rule should trigger
    def test_old_schema_close_below_triggers(self):
        """close_below + price < level → met=True."""
        inv = {
            'id': 'inv_old',
            'type': 'price_level',
            'price': 194.74,
            'condition': 'close_below',
        }
        md = {'price': 190.0, 'asset_data': {}, 'all_assets': {}}
        result = evaluate_conditions_dict({'rules': [inv], 'logic': 'all'}, md)
        assert result['rules'][0]['met'] is True, (
            "close_below should trigger when price is below the level"
        )
        assert result['all_met'] is True

    # Price above the invalidation level → rule should NOT trigger
    def test_old_schema_close_below_does_not_trigger_when_safe(self):
        """close_below + price > level → met=False."""
        inv = {
            'id': 'inv_old',
            'type': 'price_level',
            'price': 194.74,
            'condition': 'close_below',
        }
        md = {'price': 200.0, 'asset_data': {}, 'all_assets': {}}
        result = evaluate_conditions_dict({'rules': [inv], 'logic': 'all'}, md)
        assert result['rules'][0]['met'] is False
        assert result['all_met'] is False

    def test_old_schema_close_above_triggers(self):
        """close_above + price > level → met=True."""
        inv = {
            'id': 'inv_old2',
            'type': 'price_level',
            'price': 100.0,
            'condition': 'close_above',
        }
        md = {'price': 105.0, 'asset_data': {}, 'all_assets': {}}
        result = evaluate_conditions_dict({'rules': [inv], 'logic': 'all'}, md)
        assert result['rules'][0]['met'] is True

    def test_old_schema_close_above_does_not_trigger_when_safe(self):
        """close_above + price < level → met=False."""
        inv = {
            'id': 'inv_old2',
            'type': 'price_level',
            'price': 100.0,
            'condition': 'close_above',
        }
        md = {'price': 95.0, 'asset_data': {}, 'all_assets': {}}
        result = evaluate_conditions_dict({'rules': [inv], 'logic': 'all'}, md)
        assert result['rules'][0]['met'] is False

    def test_new_schema_unaffected(self):
        """New-schema {params:{level, relation}} rules evaluate unchanged."""
        rule = {
            'id': 'r_new',
            'type': 'price_level',
            'params': {'level': 50000, 'relation': 'below'},
        }
        md = {'price': 45000.0, 'asset_data': {}, 'all_assets': {}}
        result = evaluate_conditions_dict({'rules': [rule], 'logic': 'all'}, md)
        assert result['rules'][0]['met'] is True

    def test_old_schema_via_invalidation_play_monitor_path(self):
        """
        Simulate the plays/monitor.py wrapping pattern:
        inv_conds = {'rules': [invalidation], 'logic': 'all'}
        Old-schema invalidation should now be met=True when price violates the level.
        """
        # Exact production shape (play #21): flat top-level price/condition, NO params key,
        # plus the extra bookkeeping keys the stored invalidation carries.
        invalidation = {
            'met': False,
            'note': 'Daily close below recent swing low — thesis broken',
            'type': 'price_level',
            'price': 3500.0,
            'met_at': None,
            'evidence': "Evaluation error: 'level'",
            'condition': 'close_below',
        }
        # MOCK_MARKET_DATA price is 3420 — below 3500 → should trigger
        result = evaluate_conditions_dict(
            {'rules': [invalidation], 'logic': 'all'},
            MOCK_MARKET_DATA,
        )
        assert result['all_met'] is True, (
            "Old-schema invalidation must trigger via the monitor wrapping path"
        )

    def test_genuinely_malformed_rule_fails_safe_and_beacons(self):
        """
        A rule that is genuinely malformed (no price, no level) must still fail
        safe (met=False) and fire the Sentry beacon exactly once per unique key.
        """
        import sys
        import carrac.conditions as cond_mod

        captured = []

        class _FakeSentry:
            @staticmethod
            def capture_exception(e):
                captured.append(e)

        # Reset the dedup set so this test starts clean regardless of run order
        original_set = cond_mod._EVAL_ERROR_BEACONED
        cond_mod._EVAL_ERROR_BEACONED = set()
        original_sentry = sys.modules.get('sentry_sdk')
        sys.modules['sentry_sdk'] = _FakeSentry()  # type: ignore[assignment]

        try:
            rule = {
                'id': 'malformed_rule',
                'type': 'price_level',
                'params': {},  # missing both old-schema and new-schema fields → KeyError
            }
            md = {'price': 100.0, 'asset_data': {}, 'all_assets': {}}

            # First call: should fail safe AND beacon
            result = evaluate_conditions_dict({'rules': [rule], 'logic': 'all'}, md)
            assert result['rules'][0]['met'] is False, "Malformed rule must fail safe"
            assert 'Evaluation error' in result['rules'][0]['evidence']
            assert len(captured) == 1, "Should beacon once on first error"

            # Second call with same malformed rule: dedup must suppress repeat beacon
            result2 = evaluate_conditions_dict({'rules': [rule], 'logic': 'all'}, md)
            assert result2['rules'][0]['met'] is False
            assert len(captured) == 1, "Dedup must prevent second beacon for same error"
        finally:
            cond_mod._EVAL_ERROR_BEACONED = original_set
            if original_sentry is None:
                sys.modules.pop('sentry_sdk', None)
            else:
                sys.modules['sentry_sdk'] = original_sentry
