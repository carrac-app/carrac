"""
Unit tests for the six Tier 1 "sidebar LIVE signal vocabulary" tracked states
added to carrac/indicator_state.py:

    ema_gap_squeeze, v_cpr, cpr_extreme, structure_disagreement,
    tf_divergence, rsi_divergence

Covers: extractor value sets, None-safety on missing score_b/trend_analysis,
weight registration, and the first-cycle (newly-added-key) transition-storm
guard in detect_transitions.
"""

from carrac.indicator_state import (
    extract_fingerprint,
    detect_transitions,
    TRACKED_STATES,
    _SIGNIFICANCE,
    INDICATOR_ALERT_CONDITIONS,
)

NEW_KEYS = [
    'ema_gap_squeeze', 'v_cpr', 'cpr_extreme',
    'structure_disagreement', 'tf_divergence', 'rsi_divergence',
]


def _make_asset(alerts=None, patterns=None, **overrides):
    """Minimal asset dict carrying only what the six new extractors read."""
    asset = {
        'symbol': 'BTC',
        'score_b': {'alerts': alerts if alerts is not None else []},
        'trend_analysis': {'patterns': patterns if patterns is not None else []},
    }
    asset.update(overrides)
    return asset


# ---------------------------------------------------------------------------
# Registration sanity
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_all_six_in_tracked_states(self):
        tracked_keys = {k for k, _, _ in TRACKED_STATES}
        for key in NEW_KEYS:
            assert key in tracked_keys

    def test_all_six_have_weights(self):
        for key in NEW_KEYS:
            assert key in _SIGNIFICANCE
            assert 1 <= _SIGNIFICANCE[key] <= 3

    def test_weights_match_spec(self):
        assert _SIGNIFICANCE['structure_disagreement'] == 3
        assert _SIGNIFICANCE['rsi_divergence'] == 3
        assert _SIGNIFICANCE['v_cpr'] == 2
        assert _SIGNIFICANCE['cpr_extreme'] == 2
        assert _SIGNIFICANCE['ema_gap_squeeze'] == 2
        assert _SIGNIFICANCE['tf_divergence'] == 2

    def test_all_six_have_alert_conditions(self):
        for key in NEW_KEYS:
            assert key in INDICATOR_ALERT_CONDITIONS
            assert 'none' in INDICATOR_ALERT_CONDITIONS[key]


# ---------------------------------------------------------------------------
# ema_gap_squeeze
# ---------------------------------------------------------------------------

class TestEmaGapSqueeze:
    def test_squeeze(self):
        fp = extract_fingerprint(_make_asset(alerts=['ema_squeeze']))
        assert fp['ema_gap_squeeze'] == 'squeeze'

    def test_none_when_absent(self):
        fp = extract_fingerprint(_make_asset(alerts=[]))
        assert fp['ema_gap_squeeze'] == 'none'

    def test_distinct_from_bb_squeeze(self):
        # bb_squeeze is a completely different indicator (Bollinger width) —
        # firing ema_squeeze must not affect it, and vice versa.
        asset = _make_asset(alerts=['ema_squeeze'])
        asset['bb'] = {'squeeze': False, 'width_pct': 8.0}
        fp = extract_fingerprint(asset)
        assert fp['ema_gap_squeeze'] == 'squeeze'
        assert fp['bb_squeeze'] != 'squeezed'

    def test_missing_score_b(self):
        fp = extract_fingerprint({'symbol': 'XYZ'})
        assert fp['ema_gap_squeeze'] == 'none'

    def test_score_b_not_a_dict(self):
        fp = extract_fingerprint({'symbol': 'XYZ', 'score_b': None})
        assert fp['ema_gap_squeeze'] == 'none'

    def test_alerts_not_a_list(self):
        fp = extract_fingerprint({'symbol': 'XYZ', 'score_b': {'alerts': None}})
        assert fp['ema_gap_squeeze'] == 'none'


# ---------------------------------------------------------------------------
# v_cpr
# ---------------------------------------------------------------------------

class TestVCpr:
    def test_vacuum(self):
        fp = extract_fingerprint(_make_asset(alerts=['v_cpr']))
        assert fp['v_cpr'] == 'vacuum'

    def test_none_when_absent(self):
        fp = extract_fingerprint(_make_asset(alerts=[]))
        assert fp['v_cpr'] == 'none'

    def test_missing_score_b(self):
        fp = extract_fingerprint({'symbol': 'XYZ'})
        assert fp['v_cpr'] == 'none'


# ---------------------------------------------------------------------------
# cpr_extreme
# ---------------------------------------------------------------------------

class TestCprExtreme:
    def test_near_s3(self):
        fp = extract_fingerprint(_make_asset(alerts=['near_s3']))
        assert fp['cpr_extreme'] == 'near_s3'

    def test_near_r3(self):
        fp = extract_fingerprint(_make_asset(alerts=['near_r3']))
        assert fp['cpr_extreme'] == 'near_r3'

    def test_none_when_absent(self):
        fp = extract_fingerprint(_make_asset(alerts=[]))
        assert fp['cpr_extreme'] == 'none'

    def test_missing_score_b(self):
        fp = extract_fingerprint({'symbol': 'XYZ'})
        assert fp['cpr_extreme'] == 'none'


# ---------------------------------------------------------------------------
# structure_disagreement
# ---------------------------------------------------------------------------

class TestStructureDisagreement:
    def test_bearish(self):
        fp = extract_fingerprint(_make_asset(alerts=['structure_bearish']))
        assert fp['structure_disagreement'] == 'bearish'

    def test_bullish(self):
        fp = extract_fingerprint(_make_asset(alerts=['structure_bullish']))
        assert fp['structure_disagreement'] == 'bullish'

    def test_none_when_absent(self):
        fp = extract_fingerprint(_make_asset(alerts=[]))
        assert fp['structure_disagreement'] == 'none'

    def test_distinct_from_raw_structure_state(self):
        # The existing `structure` tracked state is a raw structure_trend
        # passthrough — it must be unaffected by the ribbon/structure
        # disagreement alert vocabulary.
        asset = _make_asset(alerts=['structure_bearish'])
        asset['structure'] = {'structure_trend': 'uptrend'}
        fp = extract_fingerprint(asset)
        assert fp['structure_disagreement'] == 'bearish'
        assert fp['structure'] == 'uptrend'

    def test_missing_score_b(self):
        fp = extract_fingerprint({'symbol': 'XYZ'})
        assert fp['structure_disagreement'] == 'none'


# ---------------------------------------------------------------------------
# tf_divergence
# ---------------------------------------------------------------------------

class TestTfDivergence:
    def test_active(self):
        fp = extract_fingerprint(_make_asset(patterns=['divergence']))
        assert fp['tf_divergence'] == 'active'

    def test_none_when_absent(self):
        fp = extract_fingerprint(_make_asset(patterns=['fresh_flip']))
        assert fp['tf_divergence'] == 'none'

    def test_missing_trend_analysis(self):
        fp = extract_fingerprint({'symbol': 'XYZ'})
        assert fp['tf_divergence'] == 'none'

    def test_trend_analysis_not_a_dict(self):
        fp = extract_fingerprint({'symbol': 'XYZ', 'trend_analysis': None})
        assert fp['tf_divergence'] == 'none'

    def test_patterns_not_a_list(self):
        fp = extract_fingerprint({'symbol': 'XYZ', 'trend_analysis': {'patterns': None}})
        assert fp['tf_divergence'] == 'none'


# ---------------------------------------------------------------------------
# rsi_divergence
# ---------------------------------------------------------------------------

class TestRsiDivergencePattern:
    def test_bull(self):
        fp = extract_fingerprint(_make_asset(patterns=['rsi_bull_div']))
        assert fp['rsi_divergence'] == 'bull'

    def test_bear(self):
        fp = extract_fingerprint(_make_asset(patterns=['rsi_bear_div']))
        assert fp['rsi_divergence'] == 'bear'

    def test_hidden_bull(self):
        fp = extract_fingerprint(_make_asset(patterns=['rsi_hidden_bull_div']))
        assert fp['rsi_divergence'] == 'hidden_bull'

    def test_hidden_bear(self):
        fp = extract_fingerprint(_make_asset(patterns=['rsi_hidden_bear_div']))
        assert fp['rsi_divergence'] == 'hidden_bear'

    def test_none_when_absent(self):
        fp = extract_fingerprint(_make_asset(patterns=['fresh_flip']))
        assert fp['rsi_divergence'] == 'none'

    def test_regular_outranks_hidden_same_direction(self):
        # Bullish patterns list can legitimately contain both rsi_bull_div
        # (regular) and rsi_hidden_bull_div (continuation) simultaneously —
        # regular (reversal) wins per the documented tie-break.
        fp = extract_fingerprint(_make_asset(
            patterns=['rsi_hidden_bull_div', 'rsi_bull_div']))
        assert fp['rsi_divergence'] == 'bull'

    def test_bull_checked_before_bear_tie_break(self):
        # In production bull/bear never co-occur (TrendAnalyzer emits only
        # one direction's pattern list), but the extractor must still be
        # deterministic if that assumption is ever violated.
        fp = extract_fingerprint(_make_asset(
            patterns=['rsi_bear_div', 'rsi_bull_div']))
        assert fp['rsi_divergence'] == 'bull'

    def test_missing_trend_analysis(self):
        fp = extract_fingerprint({'symbol': 'XYZ'})
        assert fp['rsi_divergence'] == 'none'


# ---------------------------------------------------------------------------
# First-cycle transition-storm guard
# ---------------------------------------------------------------------------

class TestFirstCycleNoStorm:
    """The precompute cycle right after this deploy: every stored (old)
    fingerprint in Redis predates these six keys, so previous_fp simply
    won't have them. detect_transitions must not manufacture spurious
    events for keys that are new-this-cycle.
    """

    def test_new_keys_absent_from_old_fp_emit_nothing(self):
        # Old fingerprint (pre-deploy) — none of the six new keys present.
        old_fp = {'ema_ribbon': 'bullish', 'macd': 'bullish'}
        # New fingerprint — carries the six new keys, all resolved to real
        # values (not just 'none'), simulating a hot asset on first compute.
        new_fp = dict(old_fp)
        new_fp.update({
            'ema_gap_squeeze': 'squeeze',
            'v_cpr': 'vacuum',
            'cpr_extreme': 'near_r3',
            'structure_disagreement': 'bearish',
            'tf_divergence': 'active',
            'rsi_divergence': 'bull',
        })
        events = detect_transitions(new_fp, old_fp, 'BTC')
        # Only pre-existing tracked keys can transition; ema_ribbon/macd are
        # unchanged here, so no events fire at all — none of the six new
        # keys ever appear despite going from "absent" to a firing state.
        assert events == []
        assert all(e['indicator'] not in NEW_KEYS for e in events)

    def test_new_keys_absent_to_explicit_none_emits_nothing(self):
        # Same first-cycle scenario, but the new keys resolve to the 'none'
        # (quiescent) state rather than a firing state — must also be silent.
        old_fp = {'ema_ribbon': 'bullish'}
        new_fp = {'ema_ribbon': 'bullish', **{k: 'none' for k in NEW_KEYS}}
        events = detect_transitions(new_fp, old_fp, 'BTC')
        assert events == []

    def test_real_transition_fires_on_second_cycle(self):
        # Second cycle: old_fp now HAS the six keys (stored last cycle,
        # all quiescent). A real change on one of them must fire normally.
        old_fp = {'ema_ribbon': 'bullish', **{k: 'none' for k in NEW_KEYS}}
        new_fp = dict(old_fp)
        new_fp['structure_disagreement'] = 'bearish'
        events = detect_transitions(new_fp, old_fp, 'BTC')
        assert len(events) == 1
        ev = events[0]
        assert ev['indicator'] == 'structure_disagreement'
        assert ev['from'] == 'none'
        assert ev['to'] == 'bearish'
        assert ev['significance'] == 3
        assert ev['symbol'] == 'BTC'

    def test_no_change_across_cycles_is_silent(self):
        old_fp = {k: 'none' for k in NEW_KEYS}
        new_fp = dict(old_fp)
        events = detect_transitions(new_fp, old_fp, 'BTC')
        assert events == []

    def test_end_to_end_via_extract_fingerprint(self):
        """Same guard, but driven through extract_fingerprint on realistic
        asset dicts rather than hand-built fingerprints."""
        asset_before = _make_asset(alerts=[], patterns=[])
        # Old fp predates the six keys entirely (simulated: strip them out
        # as if read back from a pre-deploy Redis blob).
        old_fp = extract_fingerprint(asset_before)
        for key in NEW_KEYS:
            old_fp.pop(key, None)

        asset_after = _make_asset(alerts=['v_cpr'], patterns=['rsi_bull_div'])
        new_fp = extract_fingerprint(asset_after)

        events = detect_transitions(new_fp, old_fp, 'BTC')
        assert events == []  # first-cycle: no storm despite v_cpr/rsi firing
