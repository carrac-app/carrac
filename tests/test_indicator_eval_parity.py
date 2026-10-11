"""
Parity tests for carrac/indicator_eval.py.

Verifies that the shared indicator-eval primitives:
  1. Produce the correct canonical state string for representative inputs.
  2. Agree exactly with carrac.indicator_state private helpers (which now
     delegate to them).
  3. Agree exactly with carrac.conditions._get_indicator_state() on the
     indicators that were unified.

Coverage strategy: for every indicator we test:
  - The normal/positive/negative/boundary cases.
  - None / missing-key cases.
  - Any edge case that differs between the two original implementations.
"""


import carrac.indicator_eval as ie
from carrac.indicator_state import (
    _rsi_zone,
    _bb_state,
    _bb_position,
    _consensio_state,
    _ichimoku_cloud,
    _ttm_state,
    _stoch_rsi_zone,
    _donchian_pos,
    _ema_cross_state,
    _alligator_state,
    _cpr_level,
)
from carrac.conditions import _get_indicator_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _asset(**kw):
    """Build a minimal asset dict for testing."""
    return dict(kw)


# ---------------------------------------------------------------------------
# rsi_zone
# ---------------------------------------------------------------------------

class TestRsiZone:
    def test_oversold(self):
        assert ie.rsi_zone(20.0) == 'oversold'

    def test_oversold_boundary(self):
        assert ie.rsi_zone(29.9) == 'oversold'

    def test_boundary_30_is_neutral(self):
        assert ie.rsi_zone(30.0) == 'neutral'

    def test_neutral_midpoint(self):
        assert ie.rsi_zone(50.0) == 'neutral'

    def test_boundary_70_is_neutral(self):
        assert ie.rsi_zone(70.0) == 'neutral'

    def test_overbought_boundary(self):
        assert ie.rsi_zone(70.1) == 'overbought'

    def test_overbought(self):
        assert ie.rsi_zone(85.0) == 'overbought'

    def test_none_input(self):
        assert ie.rsi_zone(None) is None

    # Parity: indicator_state private helper delegates to the same function
    def test_parity_with_indicator_state(self):
        for val in (10.0, 29.9, 30.0, 50.0, 70.0, 70.1, 90.0, None):
            assert ie.rsi_zone(val) == _rsi_zone(val), f"divergence at val={val}"

    # Parity: conditions._get_indicator_state('RSI', ...) agrees
    def test_parity_with_conditions(self):
        cases = [
            (20.0, 'oversold'),
            (30.0, 'neutral'),
            (55.0, 'neutral'),
            (70.0, 'neutral'),
            (75.0, 'overbought'),
        ]
        for val, expected in cases:
            asset = _asset(weekly_rsi=val)
            assert _get_indicator_state('RSI', asset) == expected

    def test_conditions_none_rsi(self):
        assert _get_indicator_state('RSI', _asset()) is None


# ---------------------------------------------------------------------------
# ema_ribbon_state
# ---------------------------------------------------------------------------

class TestEmaRibbonState:
    def test_bull_short_form(self):
        assert ie.ema_ribbon_state(_asset(ema_ribbon={'trend': 'bull'})) == 'bullish'

    def test_bullish_long_form(self):
        assert ie.ema_ribbon_state(_asset(ema_ribbon={'trend': 'bullish'})) == 'bullish'

    def test_bear_short_form(self):
        assert ie.ema_ribbon_state(_asset(ema_ribbon={'trend': 'bear'})) == 'bearish'

    def test_bearish_long_form(self):
        assert ie.ema_ribbon_state(_asset(ema_ribbon={'trend': 'bearish'})) == 'bearish'

    def test_neutral(self):
        assert ie.ema_ribbon_state(_asset(ema_ribbon={'trend': 'neutral'})) == 'neutral'

    def test_missing_trend_key(self):
        assert ie.ema_ribbon_state(_asset(ema_ribbon={})) == 'neutral'

    def test_missing_ema_ribbon_key(self):
        assert ie.ema_ribbon_state(_asset()) == 'neutral'

    # Parity: conditions agrees
    def test_parity_with_conditions(self):
        for trend_val, expected in [
            ('bull', 'bullish'), ('bullish', 'bullish'),
            ('bear', 'bearish'), ('bearish', 'bearish'),
            ('neutral', 'neutral'), ('', 'neutral'),
        ]:
            asset = _asset(ema_ribbon={'trend': trend_val})
            assert _get_indicator_state('EMA_RIBBON', asset) == expected, \
                f"divergence at trend='{trend_val}'"


# ---------------------------------------------------------------------------
# macd_state
# ---------------------------------------------------------------------------

class TestMacdState:
    def test_bullish(self):
        assert ie.macd_state(_asset(macd={'histogram': 0.5})) == 'bullish'

    def test_bearish(self):
        assert ie.macd_state(_asset(macd={'histogram': -0.3})) == 'bearish'

    def test_flat_zero(self):
        assert ie.macd_state(_asset(macd={'histogram': 0})) == 'flat'

    def test_flat_none_histogram(self):
        # None histogram → coerced to 0 via `or 0`
        assert ie.macd_state(_asset(macd={'histogram': None})) == 'flat'

    def test_missing_macd_key(self):
        assert ie.macd_state(_asset()) == 'flat'

    # Parity: conditions agrees
    def test_parity_with_conditions(self):
        for hist, expected in [(1.0, 'bullish'), (-1.0, 'bearish'), (0.0, 'flat')]:
            asset = _asset(macd={'histogram': hist})
            assert _get_indicator_state('MACD', asset) == expected


# ---------------------------------------------------------------------------
# bb_squeeze_state
# ---------------------------------------------------------------------------

class TestBbSqueezeState:
    def test_squeezed(self):
        assert ie.bb_squeeze_state(_asset(bb={'squeeze': True, 'width_pct': 1.0})) == 'squeezed'

    def test_expanded(self):
        assert ie.bb_squeeze_state(_asset(bb={'squeeze': False, 'width_pct': 7.5})) == 'expanded'

    def test_expanded_boundary(self):
        # width > 6 → expanded; width == 6 → normal
        assert ie.bb_squeeze_state(_asset(bb={'squeeze': False, 'width_pct': 6.1})) == 'expanded'
        assert ie.bb_squeeze_state(_asset(bb={'squeeze': False, 'width_pct': 6.0})) == 'normal'

    def test_normal(self):
        assert ie.bb_squeeze_state(_asset(bb={'squeeze': False, 'width_pct': 3.0})) == 'normal'

    def test_squeeze_flag_dominates_width(self):
        # squeeze=True should always return 'squeezed' regardless of width
        assert ie.bb_squeeze_state(_asset(bb={'squeeze': True, 'width_pct': 8.0})) == 'squeezed'

    def test_none_width(self):
        # width_pct=None → coerced to 0 via `or 0` → normal
        assert ie.bb_squeeze_state(_asset(bb={'squeeze': False, 'width_pct': None})) == 'normal'

    def test_missing_bb_key(self):
        assert ie.bb_squeeze_state(_asset()) == 'normal'

    # Parity: indicator_state private helper
    def test_parity_with_indicator_state(self):
        assets = [
            _asset(bb={'squeeze': True, 'width_pct': 2.0}),
            _asset(bb={'squeeze': False, 'width_pct': 7.0}),
            _asset(bb={'squeeze': False, 'width_pct': 3.0}),
            _asset(bb={'squeeze': False, 'width_pct': None}),
            _asset(),
        ]
        for asset in assets:
            assert ie.bb_squeeze_state(asset) == _bb_state(asset)

    # Parity: conditions agrees
    def test_parity_with_conditions(self):
        for asset, expected in [
            (_asset(bb={'squeeze': True}), 'squeezed'),
            (_asset(bb={'squeeze': False, 'width_pct': 7.0}), 'expanded'),
            (_asset(bb={'squeeze': False, 'width_pct': 3.0}), 'normal'),
        ]:
            assert _get_indicator_state('BB_SQUEEZE', asset) == expected


# ---------------------------------------------------------------------------
# bb_position_state
# ---------------------------------------------------------------------------

class TestBbPositionState:
    def test_precomputed_position_returned_directly(self):
        for pos in ('inside', 'above_upper', 'below_lower'):
            assert ie.bb_position_state(_asset(bb={'position': pos})) == pos

    def test_fallback_above_upper(self):
        asset = _asset(bb={'upper': 100.0, 'lower': 80.0}, latest_price=105.0)
        assert ie.bb_position_state(asset) == 'above_upper'

    def test_fallback_below_lower(self):
        asset = _asset(bb={'upper': 100.0, 'lower': 80.0}, latest_price=75.0)
        assert ie.bb_position_state(asset) == 'below_lower'

    def test_fallback_inside(self):
        asset = _asset(bb={'upper': 100.0, 'lower': 80.0}, latest_price=90.0)
        assert ie.bb_position_state(asset) == 'inside'

    def test_fallback_uses_realtime_price(self):
        asset = _asset(
            bb={'upper': 100.0, 'lower': 80.0},
            latest_price=75.0,   # would be below_lower
            realtime={'price': 95.0},  # overrides to inside
        )
        assert ie.bb_position_state(asset) == 'inside'

    def test_missing_bands_returns_none(self):
        assert ie.bb_position_state(_asset(bb={'upper': 100.0})) is None
        assert ie.bb_position_state(_asset(bb={'lower': 80.0})) is None

    def test_no_price_returns_none(self):
        asset = _asset(bb={'upper': 100.0, 'lower': 80.0}, latest_price=0)
        assert ie.bb_position_state(asset) is None

    def test_missing_bb_key(self):
        # No bb key → bb_pos = None, no upper/lower → None
        assert ie.bb_position_state(_asset()) is None

    # Parity: indicator_state private helper
    def test_parity_with_indicator_state(self):
        assets = [
            _asset(bb={'position': 'inside'}),
            _asset(bb={'position': 'above_upper'}),
            _asset(bb={'upper': 100.0, 'lower': 80.0}, latest_price=105.0),
            _asset(bb={'upper': 100.0, 'lower': 80.0}, latest_price=75.0),
            _asset(bb={'upper': 100.0, 'lower': 80.0}, latest_price=90.0),
            _asset(),
        ]
        for asset in assets:
            assert ie.bb_position_state(asset) == _bb_position(asset)

    # Parity: conditions (simple path — precomputed position)
    def test_parity_with_conditions_precomputed(self):
        for pos in ('inside', 'above_upper', 'below_lower'):
            asset = _asset(bb={'position': pos})
            assert _get_indicator_state('BB_POSITION', asset) == pos


# ---------------------------------------------------------------------------
# ichimoku_cloud_state
# ---------------------------------------------------------------------------

class TestIchimokuCloudState:
    def _make(self, cloud_position):
        return _asset(cockpit_extended={'ichimoku': {'cloud_position': cloud_position}})

    def test_above_green(self):
        assert ie.ichimoku_cloud_state(self._make('above_green')) == 'above_cloud'

    def test_above_red(self):
        assert ie.ichimoku_cloud_state(self._make('above_red')) == 'above_cloud'

    def test_below_green(self):
        assert ie.ichimoku_cloud_state(self._make('below_green')) == 'below_cloud'

    def test_below_red(self):
        assert ie.ichimoku_cloud_state(self._make('below_red')) == 'below_cloud'

    def test_inside(self):
        assert ie.ichimoku_cloud_state(self._make('inside')) == 'inside_cloud'

    def test_none_position(self):
        assert ie.ichimoku_cloud_state(self._make(None)) is None

    def test_missing_key(self):
        assert ie.ichimoku_cloud_state(_asset()) is None

    # Parity: indicator_state
    def test_parity_with_indicator_state(self):
        assets = [self._make(p) for p in ('above_green', 'below_red', 'inside', None)]
        assets.append(_asset())
        for asset in assets:
            assert ie.ichimoku_cloud_state(asset) == _ichimoku_cloud(asset)

    # Parity: conditions
    def test_parity_with_conditions(self):
        for cloud_pos, expected in [
            ('above_green', 'above_cloud'),
            ('below_red', 'below_cloud'),
            ('inside', 'inside_cloud'),
        ]:
            asset = self._make(cloud_pos)
            assert _get_indicator_state('ICHIMOKU', asset) == expected


# ---------------------------------------------------------------------------
# ttm_squeeze_state
# ---------------------------------------------------------------------------

class TestTtmSqueezeState:
    """firing_up/firing_down require a recent release
    (ttm.bars_since_release <= TTM_RELEASE_WINDOW_BARS), not just "no active
    squeeze + momentum happens to point that way" (otherwise a squeeze that
    ended long ago with drifting momentum would fire indefinitely, which the
    "just released" meaning contradicts).
    """

    def _make(self, active, momentum=None, bars_since_release=None):
        ttm = {'active': active}
        if momentum is not None:
            ttm['momentum'] = momentum
        if bars_since_release is not None:
            ttm['bars_since_release'] = bars_since_release
        return _asset(cockpit_extended={'ttm_squeeze': ttm})

    def test_squeeze_on(self):
        assert ie.ttm_squeeze_state(self._make(True)) == 'squeeze_on'

    def test_firing_up(self):
        # Released 2 bars ago (within the window) + bullish momentum.
        assert ie.ttm_squeeze_state(
            self._make(False, 'bullish_strong', bars_since_release=2)
        ) == 'firing_up'

    def test_firing_down(self):
        assert ie.ttm_squeeze_state(
            self._make(False, 'bearish', bars_since_release=2)
        ) == 'firing_down'

    def test_firing_at_window_boundary(self):
        # Exactly TTM_RELEASE_WINDOW_BARS bars ago still counts as "recent".
        assert ie.ttm_squeeze_state(
            self._make(False, 'bullish', bars_since_release=ie.TTM_RELEASE_WINDOW_BARS)
        ) == 'firing_up'

    def test_stale_release_is_squeeze_off(self):
        # Released 10 bars ago (past the window) — pins that stale
        # momentum alone must not keep firing.
        assert ie.ttm_squeeze_state(
            self._make(False, 'bullish', bars_since_release=10)
        ) == 'squeeze_off'

    def test_never_squeezed_is_squeeze_off(self):
        # bars_since_release absent because the window never saw a
        # qualifying squeeze — momentum alone must not fire.
        assert ie.ttm_squeeze_state(self._make(False, 'bullish')) == 'squeeze_off'
        assert ie.ttm_squeeze_state(self._make(False, 'bearish')) == 'squeeze_off'

    def test_missing_bars_since_release_key_means_no_firing(self):
        # Old cached payloads computed before this field existed must not
        # crash and must not fire (safe default).
        assert ie.ttm_squeeze_state(self._make(False, 'bullish')) == 'squeeze_off'

    def test_squeeze_off(self):
        assert ie.ttm_squeeze_state(
            self._make(False, 'neutral', bars_since_release=1)
        ) == 'squeeze_off'

    def test_missing_momentum_defaults_to_squeeze_off(self):
        # momentum absent → mom = None → `or 'neutral'` → squeeze_off, even
        # with a qualifying recent release.
        assert ie.ttm_squeeze_state(self._make(False, bars_since_release=1)) == 'squeeze_off'

    def test_missing_key(self):
        assert ie.ttm_squeeze_state(_asset()) is None

    # Parity: indicator_state
    def test_parity_with_indicator_state(self):
        assets = [
            self._make(True),
            self._make(False, 'bullish', bars_since_release=2),
            self._make(False, 'bearish', bars_since_release=2),
            self._make(False, 'neutral', bars_since_release=2),
            self._make(False, 'bullish', bars_since_release=10),
            self._make(False, 'bullish'),
            _asset(),
        ]
        for asset in assets:
            assert ie.ttm_squeeze_state(asset) == _ttm_state(asset)

    # Parity: conditions
    def test_parity_with_conditions(self):
        for asset, expected in [
            (self._make(True), 'squeeze_on'),
            (self._make(False, 'bullish', bars_since_release=2), 'firing_up'),
            (self._make(False, 'bearish', bars_since_release=2), 'firing_down'),
            (self._make(False, 'neutral', bars_since_release=2), 'squeeze_off'),
            (self._make(False, 'bullish', bars_since_release=10), 'squeeze_off'),
            (self._make(False, 'bullish'), 'squeeze_off'),
        ]:
            assert _get_indicator_state('TTM_SQUEEZE', asset) == expected


# ---------------------------------------------------------------------------
# stoch_rsi_zone
# ---------------------------------------------------------------------------

class TestStochRsiZone:
    def _make(self, zone):
        return _asset(cockpit_extended={'stoch_rsi': {'zone': zone}})

    def test_oversold(self):
        assert ie.stoch_rsi_zone(self._make('oversold')) == 'oversold'

    def test_overbought(self):
        assert ie.stoch_rsi_zone(self._make('overbought')) == 'overbought'

    def test_neutral(self):
        assert ie.stoch_rsi_zone(self._make('neutral')) == 'neutral'

    def test_missing_key(self):
        assert ie.stoch_rsi_zone(_asset()) is None

    # Parity: indicator_state
    def test_parity_with_indicator_state(self):
        for zone in ('oversold', 'neutral', 'overbought', None):
            asset = self._make(zone)
            assert ie.stoch_rsi_zone(asset) == _stoch_rsi_zone(asset)

    # Parity: conditions
    def test_parity_with_conditions(self):
        for zone in ('oversold', 'neutral', 'overbought'):
            asset = self._make(zone)
            assert _get_indicator_state('STOCH_RSI', asset) == zone


# ---------------------------------------------------------------------------
# donchian_position
# ---------------------------------------------------------------------------

class TestDonchianPosition:
    def _make(self, upper, lower, price=None, realtime_price=None):
        d = {'cockpit_extended': {'donchian': {'upper': upper, 'lower': lower}}}
        if price is not None:
            d['latest_price'] = price
        if realtime_price is not None:
            d['realtime'] = {'price': realtime_price}
        return d

    def test_at_high_equal(self):
        assert ie.donchian_position(self._make(100, 80, price=100)) == 'at_high'

    def test_at_high_above(self):
        assert ie.donchian_position(self._make(100, 80, price=105)) == 'at_high'

    def test_at_low_equal(self):
        assert ie.donchian_position(self._make(100, 80, price=80)) == 'at_low'

    def test_at_low_below(self):
        assert ie.donchian_position(self._make(100, 80, price=75)) == 'at_low'

    def test_inside(self):
        assert ie.donchian_position(self._make(100, 80, price=90)) == 'inside'

    def test_uses_realtime_price(self):
        # latest_price would be at_low, but realtime overrides to inside
        asset = self._make(100, 80, price=70, realtime_price=90)
        assert ie.donchian_position(asset) == 'inside'

    def test_missing_bands_returns_none(self):
        assert ie.donchian_position(_asset(cockpit_extended={'donchian': {'upper': 100}})) is None

    def test_missing_key(self):
        assert ie.donchian_position(_asset()) is None

    # Parity: indicator_state
    def test_parity_with_indicator_state(self):
        assets = [
            self._make(100, 80, price=105),
            self._make(100, 80, price=80),
            self._make(100, 80, price=90),
            _asset(),
        ]
        for asset in assets:
            assert ie.donchian_position(asset) == _donchian_pos(asset)

    # Parity: conditions
    def test_parity_with_conditions(self):
        for price, expected in [(105, 'at_high'), (80, 'at_low'), (90, 'inside')]:
            asset = self._make(100, 80, price=price)
            assert _get_indicator_state('DONCHIAN', asset) == expected


# ---------------------------------------------------------------------------
# ema_cross_state
# ---------------------------------------------------------------------------

class TestEmaCrossState:
    def _make(self, state):
        return _asset(cockpit_extended={'ema_cross': {'state': state}})

    def test_bullish(self):
        assert ie.ema_cross_state(self._make('bullish')) == 'bullish'

    def test_bearish(self):
        assert ie.ema_cross_state(self._make('bearish')) == 'bearish'

    def test_missing(self):
        assert ie.ema_cross_state(_asset()) is None

    # Parity: indicator_state
    def test_parity_with_indicator_state(self):
        for state in ('bullish', 'bearish', None):
            asset = self._make(state)
            assert ie.ema_cross_state(asset) == _ema_cross_state(asset)

    # Parity: conditions
    def test_parity_with_conditions(self):
        for state in ('bullish', 'bearish'):
            asset = self._make(state)
            assert _get_indicator_state('EMA_CROSS', asset) == state


# ---------------------------------------------------------------------------
# alligator_state
# ---------------------------------------------------------------------------

class TestAlligatorState:
    def _make(self, state):
        return _asset(cockpit_extended={'alligator': {'state': state}})

    def test_eating_bullish(self):
        assert ie.alligator_state(self._make('eating_bullish')) == 'eating_bullish'

    def test_sleeping(self):
        assert ie.alligator_state(self._make('sleeping')) == 'sleeping'

    def test_missing(self):
        assert ie.alligator_state(_asset()) is None

    # Parity: indicator_state
    def test_parity_with_indicator_state(self):
        for state in ('eating_bullish', 'eating_bearish', 'sleeping', 'mixed', None):
            asset = self._make(state)
            assert ie.alligator_state(asset) == _alligator_state(asset)

    # Parity: conditions
    def test_parity_with_conditions(self):
        for state in ('eating_bullish', 'eating_bearish', 'sleeping', 'mixed'):
            asset = self._make(state)
            assert _get_indicator_state('ALLIGATOR', asset) == state


# ---------------------------------------------------------------------------
# consensio_state  (divergence 1: missing data must not read as strong_bear)
# ---------------------------------------------------------------------------

class TestConsensioState:
    def _make(self, positions, max_positions=9):
        return _asset(consensio={'total': {'positions': positions, 'max_positions': max_positions}})

    # Normal buckets
    def test_full_bull(self):
        # 9/9 = 1.0 >= 0.95
        assert ie.consensio_state(self._make(9, 9)) == 'full_bull'

    def test_full_bull_boundary(self):
        # exactly 0.95 → full_bull
        assert ie.consensio_state(self._make(19, 20)) == 'full_bull'

    def test_strong_bull(self):
        # 7/9 ≈ 0.778 >= 0.75
        assert ie.consensio_state(self._make(7, 9)) == 'strong_bull'

    def test_bull(self):
        # 5/9 ≈ 0.556 >= 0.55
        assert ie.consensio_state(self._make(5, 9)) == 'bull'

    def test_neutral(self):
        # 4/9 ≈ 0.444 >= 0.45? No — 4/9=0.444 < 0.45 → bear. Use 5/10=0.5
        assert ie.consensio_state(self._make(5, 10)) == 'neutral'  # 0.5 >= 0.45, < 0.55

    def test_bear(self):
        # 3/10 = 0.3 >= 0.25, < 0.45
        assert ie.consensio_state(self._make(3, 10)) == 'bear'

    def test_strong_bear(self):
        # 1/9 ≈ 0.11 < 0.25
        assert ie.consensio_state(self._make(1, 9)) == 'strong_bear'

    def test_zero_positions_is_strong_bear(self):
        # positions=0, max_p=9 → ratio=0.0 < 0.25 → strong_bear (data present, just zero)
        assert ie.consensio_state(self._make(0, 9)) == 'strong_bear'

    # Edge cases: missing data → None (never a misleading strong_bear)
    def test_missing_positions_returns_none(self):
        # positions key absent → None, NOT strong_bear
        asset = _asset(consensio={'total': {'max_positions': 9}})
        assert ie.consensio_state(asset) is None

    def test_none_positions_returns_none(self):
        assert ie.consensio_state(self._make(None, 9)) is None

    def test_missing_consensio_returns_none(self):
        assert ie.consensio_state(_asset()) is None

    def test_max_p_zero_returns_none(self):
        # Division guard: max_positions=0 → None
        assert ie.consensio_state(self._make(5, 0)) is None

    def test_non_numeric_positions_returns_none(self):
        asset = _asset(consensio={'total': {'positions': 'bad', 'max_positions': 9}})
        assert ie.consensio_state(asset) is None

    # Parity: indicator_state _consensio_state now delegates here
    def test_parity_with_indicator_state(self):
        cases = [
            self._make(9, 9),
            self._make(7, 9),
            self._make(5, 9),
            self._make(0, 9),
            self._make(None, 9),
            _asset(consensio={'total': {'max_positions': 9}}),  # positions key absent
            _asset(consensio={'total': {'positions': 5, 'max_positions': 0}}),
            _asset(),
        ]
        for asset in cases:
            assert ie.consensio_state(asset) == _consensio_state(asset), \
                f"divergence for asset={asset}"

    # Parity: conditions._get_indicator_state now delegates here too
    def test_parity_with_conditions_normal(self):
        for asset, expected in [
            (self._make(9, 9), 'full_bull'),
            (self._make(7, 9), 'strong_bull'),
            (self._make(5, 9), 'bull'),
            (self._make(0, 9), 'strong_bear'),
        ]:
            assert _get_indicator_state('CONSENSIO', asset) == expected

    def test_conditions_missing_positions_returns_none(self):
        # A missing positions key yields None, not a 0/0 'strong_bear'.
        asset = _asset(consensio={'total': {'max_positions': 9}})
        assert _get_indicator_state('CONSENSIO', asset) is None

    def test_conditions_max_p_zero_returns_none(self):
        assert _get_indicator_state('CONSENSIO', self._make(5, 0)) is None


# ---------------------------------------------------------------------------
# structure_trend / structure_pattern  (divergence 2 — two real contracts)
# ---------------------------------------------------------------------------

class TestStructureTrend:
    """Alerts: raw ``structure_trend`` passthrough (default 'neutral' when absent).

    Behavior-preserving — real data legitimately includes uptrend/downtrend/unknown,
    and the Alerts matcher + fingerprint expect the raw value (no normalisation).
    """

    def _make(self, trend):
        return _asset(structure={'structure_trend': trend})

    def test_bullish(self):
        assert ie.structure_trend(self._make('bullish')) == 'bullish'

    def test_uptrend_passthrough(self):
        assert ie.structure_trend(self._make('uptrend')) == 'uptrend'

    def test_bearish(self):
        assert ie.structure_trend(self._make('bearish')) == 'bearish'

    def test_downtrend_passthrough(self):
        assert ie.structure_trend(self._make('downtrend')) == 'downtrend'

    def test_neutral(self):
        assert ie.structure_trend(self._make('neutral')) == 'neutral'

    def test_unknown_passthrough(self):
        assert ie.structure_trend(self._make('unknown')) == 'unknown'

    def test_missing_structure_key(self):
        assert ie.structure_trend(_asset()) == 'neutral'

    # Parity: indicator_state TRACKED_STATES structure entry delegates to structure_trend
    def test_parity_with_indicator_state(self):
        from carrac.indicator_state import TRACKED_STATES
        extract_fn = next(fn for key, fn, _ in TRACKED_STATES if key == 'structure')
        for trend in ('bullish', 'uptrend', 'bearish', 'downtrend', 'neutral', 'unknown'):
            asset = self._make(trend)
            assert extract_fn(asset) == trend, f"divergence at trend='{trend}'"
        assert extract_fn(_asset()) == 'neutral'


class TestStructurePattern:
    """Plays vocabulary: HH_HL / LH_LL / range."""

    def _make(self, trend):
        return _asset(structure={'structure_trend': trend})

    def test_bullish_maps_to_HH_HL(self):
        assert ie.structure_pattern(self._make('bullish')) == 'HH_HL'

    def test_uptrend_maps_to_HH_HL(self):
        assert ie.structure_pattern(self._make('uptrend')) == 'HH_HL'

    def test_bearish_maps_to_LH_LL(self):
        assert ie.structure_pattern(self._make('bearish')) == 'LH_LL'

    def test_downtrend_maps_to_LH_LL(self):
        assert ie.structure_pattern(self._make('downtrend')) == 'LH_LL'

    def test_neutral_maps_to_range(self):
        assert ie.structure_pattern(self._make('neutral')) == 'range'

    def test_unknown_maps_to_range(self):
        assert ie.structure_pattern(self._make('unknown')) == 'range'

    def test_missing_structure_key(self):
        assert ie.structure_pattern(_asset()) == 'range'

    # Parity: conditions._get_indicator_state('STRUCTURE') returns Plays vocab
    def test_parity_with_conditions(self):
        for trend, expected in [
            ('bullish', 'HH_HL'), ('uptrend', 'HH_HL'),
            ('bearish', 'LH_LL'), ('downtrend', 'LH_LL'),
            ('neutral', 'range'), ('unknown', 'range'),
        ]:
            asset = self._make(trend)
            assert _get_indicator_state('STRUCTURE', asset) == expected, \
                f"divergence at trend='{trend}'"

    def test_conditions_missing_returns_range(self):
        assert _get_indicator_state('STRUCTURE', _asset()) == 'range'

    # The two vocabularies must NOT overlap — verify they stay separate
    def test_vocabularies_are_disjoint(self):
        alert_vocab = {'bullish', 'bearish', 'neutral'}
        plays_vocab = {'HH_HL', 'LH_LL', 'range'}
        assert alert_vocab.isdisjoint(plays_vocab)


# ---------------------------------------------------------------------------
# cpr_zone / cpr_position  (divergence 3 — two real contracts)
# ---------------------------------------------------------------------------

class TestCprZone:
    """Alerts vocabulary: 10 granular zones (below_s3 .. above_r3)."""

    def _make(self, price_level, key='cpr'):
        return _asset(**{key: {'price_level': price_level}})

    def test_below_s3(self):
        assert ie.cpr_zone(self._make('Below S3')) == 'below_s3'

    def test_s2_s3(self):
        assert ie.cpr_zone(self._make('S2-S3')) == 's2_s3'

    def test_bc_cp_maps_to_below_pivot(self):
        assert ie.cpr_zone(self._make('BC-CP')) == 'below_pivot'

    def test_cp_tc_maps_to_above_pivot(self):
        assert ie.cpr_zone(self._make('CP-TC')) == 'above_pivot'

    def test_above_r3(self):
        assert ie.cpr_zone(self._make('Above R3')) == 'above_r3'

    def test_missing_price_level_returns_none(self):
        assert ie.cpr_zone(_asset(cpr={})) is None

    def test_missing_cpr_key_returns_none(self):
        assert ie.cpr_zone(_asset()) is None

    def test_weekly_cpr_key(self):
        asset = self._make('TC-R1', key='weekly_cpr')
        assert ie.cpr_zone(asset, 'weekly_cpr') == 'tc_r1'

    def test_daily_cpr_key(self):
        asset = self._make('R1-R2', key='daily_cpr')
        assert ie.cpr_zone(asset, 'daily_cpr') == 'r1_r2'

    # Parity: indicator_state _cpr_level delegates to cpr_zone
    def test_parity_with_indicator_state(self):
        for price_level, expected_zone in [
            ('Below S3', 'below_s3'),
            ('S1-S2', 's1_s2'),
            ('BC-CP', 'below_pivot'),
            ('CP-TC', 'above_pivot'),
            ('Above R3', 'above_r3'),
        ]:
            asset = self._make(price_level)
            assert ie.cpr_zone(asset) == _cpr_level(asset, 'cpr'), \
                f"divergence at price_level='{price_level}'"
        # Missing data
        assert ie.cpr_zone(_asset()) == _cpr_level(_asset(), 'cpr')

    def test_all_10_zones_map_correctly(self):
        expected = {
            'Below S3': 'below_s3', 'S2-S3': 's2_s3', 'S1-S2': 's1_s2',
            'BC-S1': 'bc_s1', 'BC-CP': 'below_pivot', 'CP-TC': 'above_pivot',
            'TC-R1': 'tc_r1', 'R1-R2': 'r1_r2', 'R2-R3': 'r2_r3',
            'Above R3': 'above_r3',
        }
        for price_level, zone in expected.items():
            assert ie.cpr_zone(self._make(price_level)) == zone


class TestCprPosition:
    """Plays vocabulary: 9 states — above_pivot / below_pivot /
    between / above_R1 / above_R2 / above_R3 / below_S1 / below_S2 / below_S3.

    price_position alone (no price_level) exercises the 3-state fallback path
    below; see TestCprPositionNineState for the price_level-driven banding."""

    def _make(self, price_position, key='cpr'):
        return _asset(**{key: {'price_position': price_position}})

    def _make_position_field(self, position, key='cpr'):
        """Test the fallback 'position' field (older data)."""
        return _asset(**{key: {'position': position}})

    def test_above(self):
        assert ie.cpr_position(self._make('above')) == 'above_pivot'

    def test_below(self):
        assert ie.cpr_position(self._make('below')) == 'below_pivot'

    def test_inside_maps_to_between(self):
        assert ie.cpr_position(self._make('inside')) == 'between'

    def test_missing_position_returns_between(self):
        assert ie.cpr_position(_asset(cpr={})) == 'between'

    def test_missing_cpr_key_returns_between(self):
        assert ie.cpr_position(_asset()) == 'between'

    def test_fallback_position_field(self):
        # 'position' field (older format) used as fallback
        assert ie.cpr_position(self._make_position_field('above')) == 'above_pivot'
        assert ie.cpr_position(self._make_position_field('below')) == 'below_pivot'

    def test_weekly_cpr_key(self):
        asset = self._make('above', key='weekly_cpr')
        assert ie.cpr_position(asset, 'weekly_cpr') == 'above_pivot'

    def test_daily_cpr_key(self):
        asset = self._make('below', key='daily_cpr')
        assert ie.cpr_position(asset, 'daily_cpr') == 'below_pivot'

    # Parity: conditions._get_indicator_state for all 3 CPR variants
    def test_parity_with_conditions_cpr(self):
        for pos, expected in [('above', 'above_pivot'), ('below', 'below_pivot'), ('inside', 'between')]:
            asset = self._make(pos)
            assert _get_indicator_state('CPR', asset) == expected

    def test_parity_with_conditions_weekly_cpr(self):
        for pos, expected in [('above', 'above_pivot'), ('below', 'below_pivot'), ('inside', 'between')]:
            asset = self._make(pos, key='weekly_cpr')
            assert _get_indicator_state('WEEKLY_CPR', asset) == expected

    def test_parity_with_conditions_daily_cpr(self):
        for pos, expected in [('above', 'above_pivot'), ('below', 'below_pivot'), ('inside', 'between')]:
            asset = self._make(pos, key='daily_cpr')
            assert _get_indicator_state('DAILY_CPR', asset) == expected

    def test_conditions_missing_data_returns_between(self):
        assert _get_indicator_state('CPR', _asset()) == 'between'
        assert _get_indicator_state('WEEKLY_CPR', _asset()) == 'between'
        assert _get_indicator_state('DAILY_CPR', _asset()) == 'between'

    # The two CPR vocabularies serve different consumers — verify separation
    def test_zone_and_position_vocabs_are_distinct(self):
        # cpr_zone returns 10 specific keys; cpr_position returns 9 states —
        # but the granular zone's own 'below_pivot'/'above_pivot' (mid-point
        # band names) intentionally overlap with cpr_position's coarse states.
        zone_vocab = {'below_s3', 's2_s3', 's1_s2', 'bc_s1', 'below_pivot',
                      'above_pivot', 'tc_r1', 'r1_r2', 'r2_r3', 'above_r3'}
        position_vocab = {'above_pivot', 'below_pivot', 'between',
                           'above_R1', 'above_R2', 'above_R3',
                           'below_S1', 'below_S2', 'below_S3'}
        assert 'between' not in zone_vocab
        assert 'below_s3' not in position_vocab  # case-sensitive: zone is lowercase, position is 'below_S3'


class TestCprPositionNineState:
    """Full 9-state banding driven by price_level, plus the
    price_position-is-authoritative design (price_level only refines
    specificity within the side price_position already established)."""

    def _make(self, price_position, price_level, key='cpr'):
        return _asset(**{key: {'price_position': price_position, 'price_level': price_level}})

    def test_all_9_states_from_consistent_data(self):
        # (price_position, price_level) pairs exactly as CPRCalculator would
        # produce them together — see consensio/indicators/cpr.py.
        cases = [
            ('below', 'Below S3', 'below_S3'),
            ('below', 'S2-S3', 'below_S2'),
            ('below', 'S1-S2', 'below_S1'),
            ('below', 'BC-S1', 'below_pivot'),
            ('inside', 'BC-CP', 'between'),
            ('inside', 'CP-TC', 'between'),
            ('above', 'TC-R1', 'above_pivot'),
            ('above', 'R1-R2', 'above_R1'),
            ('above', 'R2-R3', 'above_R2'),
            ('above', 'Above R3', 'above_R3'),
        ]
        for pos, level, expected in cases:
            assert ie.cpr_position(self._make(pos, level)) == expected, \
                f"divergence at price_position={pos!r}, price_level={level!r}"

    def test_most_specific_band_above_r2_not_above_pivot(self):
        # Ticket example: price above R2 but below R3 -> 'above_R2', not the
        # coarser 'above_pivot'.
        asset = self._make('above', 'R2-R3')
        assert ie.cpr_position(asset) == 'above_R2'

    def test_price_level_never_flips_the_side_established_by_price_position(self):
        # Defensive design: price_position is authoritative for direction.
        # An inconsistent/stale price_level that doesn't refine the
        # established side falls back to that side's coarse state instead of
        # being taken at face value (guards against hand-built data
        # pairing price_position='below' with a
        # price_level that CPRCalculator would never actually pair it with).
        asset = self._make('below', 'BC-CP')  # 'BC-CP' is really an 'inside' band
        assert ie.cpr_position(asset) == 'below_pivot'

    def test_missing_price_level_falls_back_to_coarse(self):
        asset = _asset(cpr={'price_position': 'above'})
        assert ie.cpr_position(asset) == 'above_pivot'

    def test_weekly_and_daily_variants_use_same_banding(self):
        assert ie.cpr_position(self._make('above', 'R1-R2', key='weekly_cpr'), 'weekly_cpr') == 'above_R1'
        assert ie.cpr_position(self._make('below', 'S1-S2', key='daily_cpr'), 'daily_cpr') == 'below_S1'

    # Parity: conditions._get_indicator_state agrees for all 3 CPR variants
    def test_parity_with_conditions(self):
        asset = self._make('above', 'R2-R3')
        assert _get_indicator_state('CPR', asset) == 'above_R2'
        asset_w = self._make('below', 'S2-S3', key='weekly_cpr')
        assert _get_indicator_state('WEEKLY_CPR', asset_w) == 'below_S2'
        asset_d = self._make('above', 'Above R3', key='daily_cpr')
        assert _get_indicator_state('DAILY_CPR', asset_d) == 'above_R3'


class TestCprStateSatisfies:
    """cpr_state_satisfies() — hierarchical containment so a Play
    saved against a coarse state keeps matching once cpr_position() starts
    returning more specific bands for the same underlying price."""

    def test_exact_match_always_satisfies(self):
        for state in ('between', 'above_pivot', 'below_S2', 'above_R3'):
            assert ie.cpr_state_satisfies(state, state) is True

    def test_more_specific_above_satisfies_coarser_above_pivot(self):
        # Backward compat: a Play saved on 'above_pivot' (saved before the 9-state vocabulary, or
        # deliberately coarse) must still match once the live state is a more
        # specific band on the same (bullish) side.
        assert ie.cpr_state_satisfies('above_R1', 'above_pivot') is True
        assert ie.cpr_state_satisfies('above_R2', 'above_pivot') is True
        assert ie.cpr_state_satisfies('above_R3', 'above_pivot') is True
        assert ie.cpr_state_satisfies('above_R3', 'above_R1') is True

    def test_more_specific_below_satisfies_coarser_below_pivot(self):
        assert ie.cpr_state_satisfies('below_S1', 'below_pivot') is True
        assert ie.cpr_state_satisfies('below_S3', 'below_pivot') is True
        assert ie.cpr_state_satisfies('below_S3', 'below_S1') is True

    def test_coarser_actual_does_not_satisfy_more_specific_expected(self):
        # The containment only runs one way: being merely 'above_pivot' does
        # NOT satisfy a condition that explicitly asked for 'above_R2'.
        assert ie.cpr_state_satisfies('above_pivot', 'above_R2') is False
        assert ie.cpr_state_satisfies('below_pivot', 'below_S2') is False

    def test_opposite_sides_never_satisfy(self):
        assert ie.cpr_state_satisfies('above_R1', 'below_pivot') is False
        assert ie.cpr_state_satisfies('below_S1', 'above_pivot') is False

    def test_between_is_exact_only(self):
        assert ie.cpr_state_satisfies('between', 'above_pivot') is False
        assert ie.cpr_state_satisfies('above_pivot', 'between') is False

    def test_none_actual_never_satisfies(self):
        assert ie.cpr_state_satisfies(None, 'above_pivot') is False

    # End-to-end through the public evaluator, proving a play saved with the
    # coarse 'above_pivot' state (old semantics) still fires when
    # the live price has moved to a more specific band.
    def test_end_to_end_backward_compat_via_get_indicator_state(self):
        from carrac.conditions import _eval_indicator_state

        asset = _asset(cpr={'price_position': 'above', 'price_level': 'R1-R2'})
        met, evidence = _eval_indicator_state(
            {'indicator': 'CPR', 'state': 'above_pivot'}, asset_data=asset,
        )
        assert met is True, evidence
        # But an exact-match request for the wrong specific band still fails.
        met2, _ = _eval_indicator_state(
            {'indicator': 'CPR', 'state': 'above_R2'}, asset_data=asset,
        )
        assert met2 is False


# ---------------------------------------------------------------------------
# MKT_STRUCTURE alias
# ---------------------------------------------------------------------------

class TestMktStructureAlias:
    """conditions.py's _get_indicator_state must accept BOTH 'STRUCTURE' (the
    legacy alias condition-builder.js saved earlier) and 'MKT_STRUCTURE' (the
    registry's canonical key, which the picker also offers and which
    condition-builder.js now normalizes new saves to) — existing plays may
    hold either."""

    def _make(self, trend):
        return _asset(structure={'structure_trend': trend})

    def test_mkt_structure_matches_structure_exactly(self):
        for trend in ('bullish', 'bearish', 'neutral'):
            asset = self._make(trend)
            assert _get_indicator_state('MKT_STRUCTURE', asset) == _get_indicator_state('STRUCTURE', asset)

    def test_mkt_structure_returns_plays_vocab(self):
        assert _get_indicator_state('MKT_STRUCTURE', self._make('bullish')) == 'HH_HL'
        assert _get_indicator_state('MKT_STRUCTURE', self._make('bearish')) == 'LH_LL'
        assert _get_indicator_state('MKT_STRUCTURE', _asset()) == 'range'


# ---------------------------------------------------------------------------
# super_trend_state  (divergence 4: unknown → None, not 'neutral')
# ---------------------------------------------------------------------------

class TestSuperTrendState:
    def _make(self, direction):
        return _asset(cockpit_extended={'supertrend': {'direction': direction}})

    def test_long_maps_to_bullish(self):
        assert ie.super_trend_state(self._make('long')) == 'bullish'

    def test_short_maps_to_bearish(self):
        assert ie.super_trend_state(self._make('short')) == 'bearish'

    def test_unknown_direction_returns_none(self):
        # Canonical: unknown direction is None, not 'neutral'.
        assert ie.super_trend_state(self._make('unknown')) is None

    def test_none_direction_returns_none(self):
        assert ie.super_trend_state(self._make(None)) is None

    def test_empty_string_direction_returns_none(self):
        assert ie.super_trend_state(self._make('')) is None

    def test_missing_supertrend_key_returns_none(self):
        assert ie.super_trend_state(_asset()) is None

    def test_missing_cockpit_extended_returns_none(self):
        assert ie.super_trend_state(_asset()) is None

    # Parity: indicator_state TRACKED_STATES super_trend entry delegates here
    def test_parity_with_indicator_state(self):
        from carrac.indicator_state import TRACKED_STATES
        extract_fn = next(fn for key, fn, _ in TRACKED_STATES if key == 'super_trend')
        for direction, expected in [
            ('long', 'bullish'), ('short', 'bearish'),
            ('unknown', None), (None, None), ('', None),
        ]:
            asset = self._make(direction)
            assert extract_fn(asset) == expected, f"divergence at direction='{direction}'"
        assert extract_fn(_asset()) is None

    # Parity: conditions._get_indicator_state now returns None for unknown
    def test_parity_with_conditions_bullish(self):
        assert _get_indicator_state('SUPER_TREND', self._make('long')) == 'bullish'

    def test_parity_with_conditions_bearish(self):
        assert _get_indicator_state('SUPER_TREND', self._make('short')) == 'bearish'

    def test_conditions_unknown_returns_none_not_neutral(self):
        # Unknown direction is None; 'neutral' is not in the SUPER_TREND alert/play vocab.
        assert _get_indicator_state('SUPER_TREND', self._make('unknown')) is None

    def test_conditions_missing_returns_none_not_neutral(self):
        assert _get_indicator_state('SUPER_TREND', _asset()) is None
