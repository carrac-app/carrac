"""SMC pattern primitives."""

import numpy as np
import pytest

from carrac.formula_eval import (
    _gap_up, _gap_down, _swept_low, _swept_high, _displacement,
    _reversal_close_up, _reversal_close_down,
)


# The FVG chart indicator's default threshold — calculateFVG in
# static/js/carrac-indicator-pack.js, atrThresholdPct. The DSL default must
# track this or GAP_UP() and the chart disagree about the same word.
CHART_FVG_ATR_PCT = 0.25


class TestGapUp:
    def test_detects_three_bar_bullish_imbalance(self):
        # bar 2's low (12) is above bar 0's high (10) -> gap of 2.0
        h = np.array([10.0, 11.0, 14.0])
        l = np.array([9.0, 10.5, 12.0])
        c = np.array([9.5, 10.8, 13.0])
        out = _gap_up(h, l, c, 0)
        assert np.isnan(out[0]) and np.isnan(out[1])
        assert out[2] == 1.0

    def test_no_gap_when_low_touches_prior_high(self):
        h = np.array([10.0, 11.0, 14.0])
        l = np.array([9.0, 10.5, 10.0])  # 10.0 is not > 10.0
        c = np.array([9.5, 10.8, 13.0])
        assert _gap_up(h, l, c, 0)[2] == 0.0

    def test_short_series_all_nan(self):
        out = _gap_up(np.array([10.0, 11.0]), np.array([9.0, 10.0]),
                      np.array([9.5, 10.5]), 0)
        assert len(out) == 2 and np.all(np.isnan(out))

    def test_nan_input_propagates(self):
        h = np.array([np.nan, 11.0, 14.0])
        l = np.array([9.0, 10.5, 12.0])
        c = np.array([9.5, 10.8, 13.0])
        assert np.isnan(_gap_up(h, l, c, 0)[2])

    def test_output_length_matches_input(self):
        h = np.arange(50, dtype=np.float64) + 10
        l = np.arange(50, dtype=np.float64)
        c = np.arange(50, dtype=np.float64) + 5
        assert len(_gap_up(h, l, c, 0)) == 50

    def test_atr_filter_rejects_a_gap_smaller_than_threshold(self):
        """A tiny gap in a wide-range market is noise — the chart indicator
        filters it and the DSL must agree."""
        n = 40
        h = np.full(n, 110.0)   # true range 10 -> ATR ~10
        l = np.full(n, 100.0)
        c = np.full(n, 105.0)
        # engineer a 1.0 gap at bar 35: below 0.25 * ATR(=10) = 2.5
        h[33] = 100.5
        l[33] = 100.0
        l[35] = 100.6
        assert _gap_up(h, l, c, 0) [35] == 1.0, 'raw gap should fire unfiltered'
        assert _gap_up(h, l, c, CHART_FVG_ATR_PCT)[35] == 0.0, 'small gap must be filtered'

    def test_default_threshold_matches_chart_indicator(self):
        from carrac.formula_eval import FUNCTION_REGISTRY
        # defaults keys are 0-indexed positions into the FULL types string,
        # so 'dddp' with a 3-series prefix puts the param at position 3.
        assert FUNCTION_REGISTRY['gap_up'][3] == {3: CHART_FVG_ATR_PCT}
        assert FUNCTION_REGISTRY['gap_down'][3] == {3: CHART_FVG_ATR_PCT}


class TestReversalCloseUp:
    def test_two_down_bodies_then_close_above_fires(self):
        o = np.array([10.0, 12.0, 11.0, 9.5])
        c = np.array([11.0, 11.0, 10.0, 12.5])   # closes above max high of bars 1-2 (12.0)
        h = np.array([11.5, 12.0, 11.2, 12.8])
        assert _reversal_close_up(o, c, h, 2)[3] == 1.0

    def test_down_body_means_close_below_open_not_below_prior_close(self):
        """Bars fall bar-over-bar but each body is bullish (close > open),
        so this is NOT a down-close sequence and must not fire."""
        o = np.array([10.0, 11.0, 10.5, 9.0])
        c = np.array([11.0, 11.2, 10.8, 12.0])   # every close > its own open
        h = np.array([11.5, 11.3, 10.9, 12.5])
        assert _reversal_close_up(o, c, h, 2)[3] == 0.0

    def test_close_not_above_window_high_does_not_fire(self):
        o = np.array([10.0, 12.0, 11.0, 9.5])
        c = np.array([11.0, 11.0, 10.0, 11.5])   # 11.5 < 12.0
        h = np.array([11.5, 12.0, 11.2, 11.8])
        assert _reversal_close_up(o, c, h, 2)[3] == 0.0

    def test_invalid_count_all_nan(self):
        o = np.arange(20, dtype=np.float64)
        c = np.arange(20, dtype=np.float64)
        h = np.arange(20, dtype=np.float64)
        assert np.all(np.isnan(_reversal_close_up(o, c, h, 0)))


class TestReversalCloseDown:
    def test_two_up_bodies_then_close_below_fires(self):
        o = np.array([10.0, 10.0, 11.0, 12.5])
        c = np.array([9.5, 11.0, 12.0, 9.0])     # closes below min low of bars 1-2
        l = np.array([9.0, 9.8, 10.8, 8.5])
        assert _reversal_close_down(o, c, l, 2)[3] == 1.0

    def test_close_not_below_window_low_does_not_fire(self):
        o = np.array([10.0, 10.0, 11.0, 12.5])
        c = np.array([9.5, 11.0, 12.0, 10.0])    # 10.0 > 9.8
        l = np.array([9.0, 9.8, 10.8, 9.9])
        assert _reversal_close_down(o, c, l, 2)[3] == 0.0

    def test_up_body_means_close_above_open_not_above_low(self):
        """Mirror of the UP direction's body test, and it earns its place:
        without it, replacing `window_c > window_o` with `window_c > window_l`
        goes undetected, because a close is above its own low on every bar.
        Here the window bars are RED (close < open) so the pattern must stay
        silent, while the broken version would treat them as bullish and fire.
        """
        o = np.array([10.0, 12.0, 13.0, 11.0])
        c = np.array([10.5, 11.0, 12.0, 9.0])    # bars 1,2 close BELOW their opens
        l = np.array([9.5, 10.5, 11.5, 8.5])     # ...but above their own lows
        assert _reversal_close_down(o, c, l, 2)[3] == 0.0


SMC_ALIASES = {
    'fvg_up': 'gap_up',
    'fvg_down': 'gap_down',
    'liquidity_sweep_low': 'swept_low',
    'liquidity_sweep_high': 'swept_high',
    'csd_up': 'reversal_close_up',
    'csd_down': 'reversal_close_down',
}


class TestSmcAliases:
    """SMC vocabulary aliases onto the same callables.

    An alias is legitimate when the DSL computes what Carrac already draws
    (fvg_* — GAP_UP's default threshold matches the FVG chart indicator's
    atrThresholdPct), or when Carrac draws nothing by that name (csd_*,
    liquidity_sweep_*). 'order_block' is deliberately NOT aliased: Carrac
    draws structure-anchored order-block zones with mitigation tracking
    (detectOrderBlocks in smart-money-concepts.js), and no candle-sequence
    formula here is that. Borrowing the name would make a fourth meaning.
    """

    @pytest.mark.parametrize('alias,canonical', sorted(SMC_ALIASES.items()))
    def test_alias_registered(self, alias, canonical):
        from carrac.formula_eval import FUNCTION_REGISTRY
        assert alias in FUNCTION_REGISTRY, f'{alias} not registered'

    @pytest.mark.parametrize('alias,canonical', sorted(SMC_ALIASES.items()))
    def test_alias_shares_implementation(self, alias, canonical):
        """Alias must point at the SAME callable — a copy would drift."""
        from carrac.formula_eval import FUNCTION_REGISTRY
        assert FUNCTION_REGISTRY[alias][0] is FUNCTION_REGISTRY[canonical][0]

    @pytest.mark.parametrize('alias,canonical', sorted(SMC_ALIASES.items()))
    def test_alias_signature_matches(self, alias, canonical):
        from carrac.formula_eval import FUNCTION_REGISTRY
        a, c = FUNCTION_REGISTRY[alias], FUNCTION_REGISTRY[canonical]
        assert a[1] == c[1], 'type_spec differs'
        assert a[2] == c[2], 'prefix differs'
        assert a[3] == c[3], 'defaults differ'

    def test_csd_up_is_not_csd_down(self):
        """Direction is a computation, not an alias."""
        from carrac.formula_eval import FUNCTION_REGISTRY
        assert (FUNCTION_REGISTRY['csd_up'][0]
                is not FUNCTION_REGISTRY['csd_down'][0])

    def test_order_block_is_not_aliased(self):
        """Carrac draws real order-block zones; no formula here is that."""
        from carrac.formula_eval import FUNCTION_REGISTRY
        for name in ('order_block', 'order_block_bull', 'order_block_bear', 'ob'):
            assert name not in FUNCTION_REGISTRY, (
                f"'{name}' must not exist — it would be a fourth meaning of "
                'order block alongside the chart indicator.'
            )


class TestDefaultsAreCallable:
    """Every primitive must evaluate with NO arguments.

    The defaults keys are 0-indexed positions into the full
    `types` string (prefix series included) — `tenkan` is 'ddp' with a
    2-series prefix and defaults {2: 9}. Getting this wrong produces a
    registry entry whose default never matches, so a bare GAP_UP() raises
    instead of using its default. It slipped through initially because every
    golden fixture passed its parameter explicitly, leaving the default path
    unexercised. These tests exercise it.
    """

    @pytest.mark.parametrize('expr', [
        'GAP_UP()', 'GAP_DOWN()',
        'SWEPT_LOW()', 'SWEPT_HIGH()',
        'DISPLACEMENT()',
    ])
    def test_bare_call_evaluates(self, expr):
        from carrac.formula_eval import evaluate_formula
        result = evaluate_formula(expr, 'BTC', '1D')
        assert result['error'] is None, f'{expr} -> {result["error"]}'


class TestGapDown:
    def test_detects_three_bar_bearish_imbalance(self):
        # bar 2's high (8) is below bar 0's low (9) -> gap
        h = np.array([10.0, 9.5, 8.0])
        l = np.array([9.0, 8.5, 7.0])
        c = np.array([9.5, 9.0, 7.5])
        assert _gap_down(h, l, c, 0)[2] == 1.0

    def test_no_gap_when_high_touches_prior_low(self):
        h = np.array([10.0, 9.5, 9.0])  # 9.0 is not < 9.0
        l = np.array([9.0, 8.5, 7.0])
        c = np.array([9.5, 9.0, 7.5])
        assert _gap_down(h, l, c, 0)[2] == 0.0

    def test_short_series_all_nan(self):
        out = _gap_down(np.array([10.0]), np.array([9.0]), np.array([9.5]), 0)
        assert np.all(np.isnan(out))


class TestSweptLow:
    def test_break_then_reclaim_fires(self):
        # prior 3-bar low is 10; bar 3 dips to 9 then closes at 11 (back above)
        l = np.array([10.0, 11.0, 12.0, 9.0])
        c = np.array([10.5, 11.5, 12.5, 11.0])
        assert _swept_low(l, c, 3)[3] == 1.0

    def test_break_without_reclaim_does_not_fire(self):
        l = np.array([10.0, 11.0, 12.0, 9.0])
        c = np.array([10.5, 11.5, 12.5, 9.2])  # closes below the 10.0 low
        assert _swept_low(l, c, 3)[3] == 0.0

    def test_no_break_does_not_fire(self):
        l = np.array([10.0, 11.0, 12.0, 10.5])
        c = np.array([10.5, 11.5, 12.5, 11.0])
        assert _swept_low(l, c, 3)[3] == 0.0

    def test_insufficient_history_is_nan(self):
        l = np.array([10.0, 11.0, 12.0, 9.0])
        c = np.array([10.5, 11.5, 12.5, 11.0])
        out = _swept_low(l, c, 3)
        assert np.all(np.isnan(out[:3]))

    def test_invalid_period_all_nan(self):
        l = np.arange(30, dtype=np.float64)
        c = np.arange(30, dtype=np.float64)
        assert np.all(np.isnan(_swept_low(l, c, 0)))


class TestSweptHigh:
    def test_break_then_reject_fires(self):
        h = np.array([12.0, 11.0, 10.0, 13.0])
        c = np.array([11.5, 10.5, 9.5, 11.0])  # pokes above 12, closes below
        assert _swept_high(h, c, 3)[3] == 1.0

    def test_break_and_hold_does_not_fire(self):
        h = np.array([12.0, 11.0, 10.0, 13.0])
        c = np.array([11.5, 10.5, 9.5, 12.8])  # closes above 12
        assert _swept_high(h, c, 3)[3] == 0.0


class TestDisplacement:
    def _steady_series(self, n=40, rng=1.0):
        """Bars with a constant true range of `rng`, so ATR converges to rng."""
        h = np.full(n, 100.0 + rng)
        l = np.full(n, 100.0)
        c = np.full(n, 100.5)
        return h, l, c

    def test_oversized_bar_fires(self):
        h, l, c = self._steady_series()
        h[35] = 110.0   # range 10 vs ATR ~1
        assert _displacement(h, l, c, 2.0)[35] == 1.0

    def test_normal_bar_does_not_fire(self):
        h, l, c = self._steady_series()
        assert _displacement(h, l, c, 2.0)[35] == 0.0

    def test_uses_previous_bar_atr_not_current(self):
        """A large candle must not inflate the threshold it has to clear.

        The parameters here are chosen so the two implementations DISAGREE —
        an earlier version of this test used mult=3 with a range-4 bar, where
        Wilder smoothing lifts ATR only 1.00 -> 1.21 and BOTH versions fire.
        That test passed against a deliberately broken implementation, which
        makes it decoration rather than a guard.

        With mult=10 and a range-12 bar against a baseline ATR of 1.0:
          previous-bar ATR -> threshold 10.0, and 12 > 10  -> fires (correct)
          current-bar  ATR -> 1.786, threshold 17.9, 12 < 17.9 -> silent (bug)
        """
        h, l, c = self._steady_series()
        h[35] = 112.0   # range 12.0 against a baseline ATR of ~1.0
        assert _displacement(h, l, c, 10.0)[35] == 1.0, (
            'a bar 12x the prior ATR must clear a 10x threshold — if this '
            'fails, DISPLACEMENT is measuring against the current bar and '
            'the candle is raising its own bar'
        )

    def test_invalid_multiplier_all_nan(self):
        h, l, c = self._steady_series()
        assert np.all(np.isnan(_displacement(h, l, c, 0)))
        assert np.all(np.isnan(_displacement(h, l, c, -1)))

    def test_short_series_all_nan(self):
        h, l, c = self._steady_series(n=5)
        assert np.all(np.isnan(_displacement(h, l, c, 2.0)))
