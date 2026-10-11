"""
Parity tests for 12 indicator states understood by the Alerts evaluator
(indicator_state.py TRACKED_STATES) and also wired into the Plays/conditions
evaluator (conditions.py
_get_indicator_state), delegating to the shared classification logic in
carrac/indicator_eval.py.

States covered: rsi_zone, adx_regime, cpr_position, weekly_cpr_position,
daily_cpr_position, ema_gap_squeeze, v_cpr, cpr_extreme,
structure_disagreement, tf_divergence, rsi_divergence, perp_basis.

Mirrors the pattern in tests/test_indicator_state_parity.py (a `_tracked_fn`
helper pulls the extractor from TRACKED_STATES; assert extractor(asset) ==
_get_indicator_state('KEY', asset) across vocab values INCLUDING missing data).
Kept in its own file so the original parity file stays small.
"""

import pytest
from carrac.conditions import _get_indicator_state
from carrac.indicator_state import TRACKED_STATES


# ── TRACKED_STATES lookup helper (same as test_indicator_state_parity.py) ──

def _tracked_fn(key):
    """Return the extraction function registered for `key` in TRACKED_STATES."""
    for k, fn, _ in TRACKED_STATES:
        if k == key:
            return fn
    raise KeyError(f"'{key}' not found in TRACKED_STATES")


# ── Asset data factories ──

def _rsi_asset(val):
    return {'weekly_rsi': val} if val is not None else {}


def _adx_asset(regime, present=True):
    if not present:
        return {}
    d = {'adx': {}}
    if regime is not None:
        d['adx']['regime'] = regime
    return d


def _cpr_asset(key, price_level):
    """Build a minimal asset dict with the given CPR variant's price_level."""
    d = {}
    if price_level is not None:
        d[key] = {'price_level': price_level}
    return d


def _score_b_asset(alerts):
    """Build an asset dict carrying score_b.alerts (None-safe upstream)."""
    if alerts is None:
        return {}
    return {'score_b': {'alerts': alerts}}


def _trend_asset(patterns):
    """Build an asset dict carrying trend_analysis.patterns (None-safe upstream)."""
    if patterns is None:
        return {}
    return {'trend_analysis': {'patterns': patterns}}


def _perp_basis_asset(basis_pct):
    return {'perp_basis_pct': basis_pct} if basis_pct is not None else {}


# ══════════════════════════════════════════════════════════════════════════════
# RSI_ZONE
# ══════════════════════════════════════════════════════════════════════════════

class TestRsiZoneParity:
    def setup_method(self):
        self.fn = _tracked_fn('rsi_zone')

    @pytest.mark.parametrize('val,expected', [
        (25, 'oversold'),
        (75, 'overbought'),
        (50, 'neutral'),
        (None, None),
    ])
    def test_parity(self, val, expected):
        asset = _rsi_asset(val)
        assert self.fn(asset) == expected
        assert _get_indicator_state('RSI_ZONE', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('RSI_ZONE', asset)


# ══════════════════════════════════════════════════════════════════════════════
# ADX_REGIME
# ══════════════════════════════════════════════════════════════════════════════

class TestAdxRegimeParity:
    def setup_method(self):
        self.fn = _tracked_fn('adx_regime')

    @pytest.mark.parametrize('regime,expected', [
        ('ranging', 'ranging'),
        ('trend_forming', 'trend_forming'),
        ('strong_trend', 'strong_trend'),
        (None, 'unknown'),  # regime key present but None -> 'unknown' default
    ])
    def test_parity(self, regime, expected):
        asset = _adx_asset(regime)
        assert self.fn(asset) == expected
        assert _get_indicator_state('ADX_REGIME', asset) == expected

    def test_parity_missing_adx_dict(self):
        """adx entirely absent -> 'unknown' default, not None (load-bearing)."""
        asset = _adx_asset(None, present=False)
        assert self.fn(asset) == 'unknown'
        assert _get_indicator_state('ADX_REGIME', asset) == 'unknown'
        assert self.fn(asset) == _get_indicator_state('ADX_REGIME', asset)


# ══════════════════════════════════════════════════════════════════════════════
# CPR_POSITION / WEEKLY_CPR_POSITION / DAILY_CPR_POSITION (granular 10-zone)
# ══════════════════════════════════════════════════════════════════════════════

class TestCprPositionParity:
    def setup_method(self):
        self.fn = _tracked_fn('cpr_position')

    @pytest.mark.parametrize('price_level,expected', [
        ('Below S3', 'below_s3'),
        ('TC-R1', 'tc_r1'),
        ('Above R3', 'above_r3'),
        (None, None),
    ])
    def test_parity(self, price_level, expected):
        asset = _cpr_asset('cpr', price_level)
        assert self.fn(asset) == expected
        assert _get_indicator_state('CPR_POSITION', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('CPR_POSITION', asset)


class TestWeeklyCprPositionParity:
    def setup_method(self):
        self.fn = _tracked_fn('weekly_cpr_position')

    @pytest.mark.parametrize('price_level,expected', [
        ('S2-S3', 's2_s3'),
        ('BC-CP', 'below_pivot'),
        (None, None),
    ])
    def test_parity(self, price_level, expected):
        asset = _cpr_asset('weekly_cpr', price_level)
        assert self.fn(asset) == expected
        assert _get_indicator_state('WEEKLY_CPR_POSITION', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('WEEKLY_CPR_POSITION', asset)

    def test_parity_wrong_key_not_used(self):
        """weekly_cpr must read the 'weekly_cpr' asset key, not 'cpr' or 'daily_cpr'."""
        asset = {'cpr': {'price_level': 'Above R3'}, 'daily_cpr': {'price_level': 'Below S3'}}
        assert self.fn(asset) is None
        assert _get_indicator_state('WEEKLY_CPR_POSITION', asset) is None


class TestDailyCprPositionParity:
    def setup_method(self):
        self.fn = _tracked_fn('daily_cpr_position')

    @pytest.mark.parametrize('price_level,expected', [
        ('CP-TC', 'above_pivot'),
        ('R2-R3', 'r2_r3'),
        (None, None),
    ])
    def test_parity(self, price_level, expected):
        asset = _cpr_asset('daily_cpr', price_level)
        assert self.fn(asset) == expected
        assert _get_indicator_state('DAILY_CPR_POSITION', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('DAILY_CPR_POSITION', asset)


# ══════════════════════════════════════════════════════════════════════════════
# EMA_GAP_SQUEEZE / V_CPR / CPR_EXTREME / STRUCTURE_DISAGREEMENT (score_b.alerts)
# ══════════════════════════════════════════════════════════════════════════════

class TestEmaGapSqueezeParity:
    def setup_method(self):
        self.fn = _tracked_fn('ema_gap_squeeze')

    @pytest.mark.parametrize('alerts,expected', [
        (['ema_squeeze'], 'squeeze'),
        ([], 'none'),
        (None, 'none'),  # missing score_b -> 'none', not None
    ])
    def test_parity(self, alerts, expected):
        asset = _score_b_asset(alerts)
        assert self.fn(asset) == expected
        assert _get_indicator_state('EMA_GAP_SQUEEZE', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('EMA_GAP_SQUEEZE', asset)

    def test_parity_alerts_not_a_list(self):
        asset = {'score_b': {'alerts': None}}
        assert self.fn(asset) == _get_indicator_state('EMA_GAP_SQUEEZE', asset) == 'none'


class TestVCprParity:
    def setup_method(self):
        self.fn = _tracked_fn('v_cpr')

    @pytest.mark.parametrize('alerts,expected', [
        (['v_cpr'], 'vacuum'),
        ([], 'none'),
        (None, 'none'),
    ])
    def test_parity(self, alerts, expected):
        asset = _score_b_asset(alerts)
        assert self.fn(asset) == expected
        assert _get_indicator_state('V_CPR', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('V_CPR', asset)


class TestCprExtremeParity:
    def setup_method(self):
        self.fn = _tracked_fn('cpr_extreme')

    @pytest.mark.parametrize('alerts,expected', [
        (['near_s3'], 'near_s3'),
        (['near_r3'], 'near_r3'),
        ([], 'none'),
        (None, 'none'),
    ])
    def test_parity(self, alerts, expected):
        asset = _score_b_asset(alerts)
        assert self.fn(asset) == expected
        assert _get_indicator_state('CPR_EXTREME', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('CPR_EXTREME', asset)


class TestStructureDisagreementParity:
    def setup_method(self):
        self.fn = _tracked_fn('structure_disagreement')

    @pytest.mark.parametrize('alerts,expected', [
        (['structure_bearish'], 'bearish'),
        (['structure_bullish'], 'bullish'),
        ([], 'none'),
        (None, 'none'),
    ])
    def test_parity(self, alerts, expected):
        asset = _score_b_asset(alerts)
        assert self.fn(asset) == expected
        assert _get_indicator_state('STRUCTURE_DISAGREEMENT', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('STRUCTURE_DISAGREEMENT', asset)


# ══════════════════════════════════════════════════════════════════════════════
# TF_DIVERGENCE / RSI_DIVERGENCE (trend_analysis.patterns)
# ══════════════════════════════════════════════════════════════════════════════

class TestTfDivergenceParity:
    def setup_method(self):
        self.fn = _tracked_fn('tf_divergence')

    @pytest.mark.parametrize('patterns,expected', [
        (['divergence'], 'active'),
        (['fresh_flip'], 'none'),
        ([], 'none'),
        (None, 'none'),
    ])
    def test_parity(self, patterns, expected):
        asset = _trend_asset(patterns)
        assert self.fn(asset) == expected
        assert _get_indicator_state('TF_DIVERGENCE', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('TF_DIVERGENCE', asset)


class TestRsiDivergenceParity:
    """This branch is DISTINCT from the pre-existing `indicator_divergence`
    condition type / asset_data['rsi_divergence'] dict field — it reads
    trend_analysis.patterns instead. Uses its own minimal asset dicts (no
    'rsi_divergence' key at all) to avoid any collision with that unrelated
    field."""

    def setup_method(self):
        self.fn = _tracked_fn('rsi_divergence')

    @pytest.mark.parametrize('patterns,expected', [
        (['rsi_bull_div'], 'bull'),
        (['rsi_bear_div'], 'bear'),
        (['rsi_hidden_bull_div'], 'hidden_bull'),
        (['rsi_hidden_bear_div'], 'hidden_bear'),
        (['fresh_flip'], 'none'),
        ([], 'none'),
        (None, 'none'),
    ])
    def test_parity(self, patterns, expected):
        asset = _trend_asset(patterns)
        assert self.fn(asset) == expected
        assert _get_indicator_state('RSI_DIVERGENCE', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('RSI_DIVERGENCE', asset)

    def test_no_collision_with_indicator_divergence_field(self):
        """A populated asset_data['rsi_divergence'] dict field (used by the
        unrelated `indicator_divergence` condition type) must not affect the
        RSI_DIVERGENCE indicator_state branch, which reads trend_analysis only."""
        asset = {
            'rsi_divergence': {
                'daily': {'divergence_type': 'bullish', 'strength': 0.7},
                'weekly': {'divergence_type': None, 'strength': 0},
            },
            'trend_analysis': {'patterns': ['rsi_bear_div']},
        }
        assert self.fn(asset) == 'bear'
        assert _get_indicator_state('RSI_DIVERGENCE', asset) == 'bear'


# ══════════════════════════════════════════════════════════════════════════════
# PERP_BASIS
# ══════════════════════════════════════════════════════════════════════════════

class TestPerpBasisParity:
    def setup_method(self):
        self.fn = _tracked_fn('perp_basis')

    @pytest.mark.parametrize('basis_pct,expected', [
        (0.30, 'steep_contango'),
        (1.5, 'steep_contango'),
        (-0.10, 'backwardation'),
        (-1.2, 'backwardation'),
        (0.0, 'normal'),
        (0.29, 'normal'),
        (None, 'none'),
    ])
    def test_parity(self, basis_pct, expected):
        asset = _perp_basis_asset(basis_pct)
        assert self.fn(asset) == expected
        assert _get_indicator_state('PERP_BASIS', asset) == expected

    def test_parity_missing_data(self):
        asset = {}
        assert self.fn(asset) == _get_indicator_state('PERP_BASIS', asset)

    def test_parity_non_numeric_value(self):
        asset = {'perp_basis_pct': 'n/a'}
        assert self.fn(asset) == _get_indicator_state('PERP_BASIS', asset) == 'none'
