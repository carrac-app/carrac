"""
Unit tests for the `perp_basis` tracked state added to carrac/indicator_state.py
Perp futures basis (markPrice vs indexPrice) regime.

Covers: extractor threshold behavior, None-tolerance on missing/non-numeric
perp_basis_pct (resolves to the explicit 'none' state — NOT 'normal'),
weight/vocab registration, and the first-cycle (newly-added-key)
transition-storm guard in detect_transitions — same convention as
tests/test_indicator_state_new_states.py.
"""

from carrac.indicator_state import (
    extract_fingerprint,
    detect_transitions,
    TRACKED_STATES,
    _SIGNIFICANCE,
    INDICATOR_ALERT_CONDITIONS,
)


# ---------------------------------------------------------------------------
# Registration sanity
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_in_tracked_states(self):
        tracked_keys = {k for k, _, _ in TRACKED_STATES}
        assert 'perp_basis' in tracked_keys

    def test_has_weight_in_spec_range(self):
        assert 'perp_basis' in _SIGNIFICANCE
        assert 1 <= _SIGNIFICANCE['perp_basis'] <= 3

    def test_weight_matches_spec(self):
        assert _SIGNIFICANCE['perp_basis'] == 2

    def test_has_alert_conditions(self):
        assert 'perp_basis' in INDICATOR_ALERT_CONDITIONS
        assert 'none' in INDICATOR_ALERT_CONDITIONS['perp_basis']

    def test_alert_conditions_match_spec_vocab(self):
        assert set(INDICATOR_ALERT_CONDITIONS['perp_basis']) == {
            'steep_contango', 'backwardation', 'normal', 'none',
        }


# ---------------------------------------------------------------------------
# Threshold behavior
# ---------------------------------------------------------------------------

class TestPerpBasisThresholds:
    def test_steep_contango_at_threshold(self):
        fp = extract_fingerprint({'perp_basis_pct': 0.30})
        assert fp['perp_basis'] == 'steep_contango'

    def test_steep_contango_above_threshold(self):
        fp = extract_fingerprint({'perp_basis_pct': 1.5})
        assert fp['perp_basis'] == 'steep_contango'

    def test_backwardation_at_threshold(self):
        fp = extract_fingerprint({'perp_basis_pct': -0.10})
        assert fp['perp_basis'] == 'backwardation'

    def test_backwardation_below_threshold(self):
        fp = extract_fingerprint({'perp_basis_pct': -1.2})
        assert fp['perp_basis'] == 'backwardation'

    def test_normal_at_zero(self):
        fp = extract_fingerprint({'perp_basis_pct': 0.0})
        assert fp['perp_basis'] == 'normal'

    def test_normal_just_below_contango_threshold(self):
        fp = extract_fingerprint({'perp_basis_pct': 0.29})
        assert fp['perp_basis'] == 'normal'

    def test_normal_just_above_backwardation_threshold(self):
        fp = extract_fingerprint({'perp_basis_pct': -0.09})
        assert fp['perp_basis'] == 'normal'


# ---------------------------------------------------------------------------
# None-tolerance: missing data must NOT masquerade as 'normal'
# ---------------------------------------------------------------------------

class TestPerpBasisNoneTolerance:
    def test_missing_key_is_none_not_normal(self):
        fp = extract_fingerprint({'symbol': 'XYZ'})
        assert fp['perp_basis'] == 'none'
        assert fp['perp_basis'] != 'normal'

    def test_explicit_none_value_is_none(self):
        fp = extract_fingerprint({'perp_basis_pct': None})
        assert fp['perp_basis'] == 'none'

    def test_non_numeric_value_is_none(self):
        fp = extract_fingerprint({'perp_basis_pct': 'n/a'})
        assert fp['perp_basis'] == 'none'

    def test_non_crypto_asset_with_no_basis_key_is_none(self):
        # Non-crypto assets never get perp_basis_pct set in precompute.py.
        fp = extract_fingerprint({'symbol': 'AAPL', 'type': 'stock'})
        assert fp['perp_basis'] == 'none'


# ---------------------------------------------------------------------------
# First-cycle transition-storm guard
# ---------------------------------------------------------------------------

class TestFirstCycleNoStorm:
    """The precompute cycle right after this ships: every stored (old)
    fingerprint in Redis predates the perp_basis key, so previous_fp simply
    won't have it. detect_transitions must not manufacture a spurious event
    for a key that's new-this-cycle (same guard verified for the six
    The earlier states in test_indicator_state_new_states.py).
    """

    def test_new_key_absent_from_old_fp_emits_nothing(self):
        old_fp = {'ema_ribbon': 'bullish', 'macd': 'bullish'}
        new_fp = dict(old_fp)
        new_fp['perp_basis'] = 'steep_contango'
        events = detect_transitions(new_fp, old_fp, 'BTC')
        assert events == []

    def test_new_key_absent_to_explicit_none_emits_nothing(self):
        old_fp = {'ema_ribbon': 'bullish'}
        new_fp = {'ema_ribbon': 'bullish', 'perp_basis': 'none'}
        events = detect_transitions(new_fp, old_fp, 'BTC')
        assert events == []

    def test_real_transition_fires_on_second_cycle(self):
        old_fp = {'ema_ribbon': 'bullish', 'perp_basis': 'none'}
        new_fp = dict(old_fp)
        new_fp['perp_basis'] = 'steep_contango'
        events = detect_transitions(new_fp, old_fp, 'BTC')
        assert len(events) == 1
        ev = events[0]
        assert ev['indicator'] == 'perp_basis'
        assert ev['from'] == 'none'
        assert ev['to'] == 'steep_contango'
        assert ev['significance'] == 2
        assert ev['symbol'] == 'BTC'

    def test_no_change_across_cycles_is_silent(self):
        old_fp = {'perp_basis': 'normal'}
        new_fp = dict(old_fp)
        events = detect_transitions(new_fp, old_fp, 'BTC')
        assert events == []

    def test_end_to_end_via_extract_fingerprint(self):
        """Same guard, but driven through extract_fingerprint on realistic
        asset dicts rather than hand-built fingerprints."""
        asset_before = {'symbol': 'BTC'}  # no perp_basis_pct yet
        old_fp = extract_fingerprint(asset_before)
        old_fp.pop('perp_basis', None)  # simulate pre-deploy Redis blob

        asset_after = {'symbol': 'BTC', 'perp_basis_pct': 0.75}
        new_fp = extract_fingerprint(asset_after)

        events = detect_transitions(new_fp, old_fp, 'BTC')
        assert events == []  # first-cycle: no storm despite a real reading firing
