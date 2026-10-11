"""
Unit tests for carrac/backtest_sim.py — the Full-Simulation trade engine port. These are deterministic, formula-driven tests that don't need
the JS engine: OHLCV fixtures use exact-equality markers (`close == 100`,
`high == 999`, ...) so a formula fires on one known bar, giving fully
predictable entry/exit timing to hand-check pnl/fill-price math against.

Cross-language parity against static/js/backtester.js lives in
tests/test_backtest_sim_parity.py.
"""

import math

import pytest

from carrac.backtest_sim import ENGINE_VERSION, run_backtest


def flat_ohlcv(n, price=100.0):
    return {
        'open': [price] * n,
        'high': [price + 0.1] * n,
        'low': [price - 0.1] * n,
        'close': [price] * n,
        'volume': [1.0] * n,
    }


# ── Validation / error paths ─────────────────────────────────────────────

def test_missing_entry_formula_errors():
    result = run_backtest({'exit': 'close == 999'}, flat_ohlcv(5))
    assert result['error'] == 'Entry formula is required'
    assert result['trades'] == []
    assert result['stats'] is None


def test_missing_exit_formula_errors():
    result = run_backtest({'entry': 'close == 999'}, flat_ohlcv(5))
    assert result['error'] == 'Exit formula is required'


def test_no_price_data_errors():
    result = run_backtest({'entry': 'close > 0', 'exit': 'close < 0'}, {'close': []})
    assert result['error'] == 'No price data'


def test_start_bar_after_end_bar_errors():
    result = run_backtest(
        {'entry': 'close > 0', 'exit': 'close < 0', 'startBar': 4, 'endBar': 2},
        flat_ohlcv(5),
    )
    assert result['error'] == 'startBar must be <= endBar'


def test_invalid_entry_formula_reports_which_side():
    result = run_backtest({'entry': 'not a valid formula (((', 'exit': 'close < 0'}, flat_ohlcv(5))
    assert result['error'].startswith('Entry: ')


def test_invalid_exit_formula_reports_which_side():
    result = run_backtest({'entry': 'close > 0', 'exit': 'not a valid formula (((' }, flat_ohlcv(5))
    assert result['error'].startswith('Exit: ')


# ── entryMode: default is next_open ────

def test_default_entry_mode_fills_at_next_bar_open():
    # Mirrors tests/test_backtester_js.mjs "unspecified entryMode fills at
    # next bar open". Signal fires on bar 1 (close == 110); next_open must
    # fill at opens[2] = 120, NOT closes[1] = 110.
    ohlcv = {
        'open': [100, 110, 120, 130, 140],
        'high': [101, 111, 121, 131, 141],
        'low': [99, 109, 119, 129, 139],
        'close': [100, 110, 120, 130, 140],
        'volume': [1, 1, 1, 1, 1],
    }
    config = {
        'entry': 'close == 110', 'exit': 'close == 130',
        'direction': 'long', 'initialCapital': 10000, 'sizePercent': 100,
        'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, ohlcv)
    assert result['error'] is None
    assert len(result['trades']) == 1
    trade = result['trades'][0]
    assert trade['entryBar'] == 2
    assert trade['entryPrice'] == pytest.approx(120)


def test_explicit_close_mode_fills_at_signal_bar_close():
    ohlcv = {
        'open': [100, 110, 120, 130, 140],
        'high': [101, 111, 121, 131, 141],
        'low': [99, 109, 119, 129, 139],
        'close': [100, 110, 120, 130, 140],
        'volume': [1, 1, 1, 1, 1],
    }
    config = {
        'entry': 'close == 110', 'exit': 'close == 130',
        'direction': 'long', 'initialCapital': 10000, 'sizePercent': 100,
        'commissionPercent': 0, 'slippageBps': 0, 'entryMode': 'close',
    }
    result = run_backtest(config, ohlcv)
    trade = result['trades'][0]
    assert trade['entryBar'] == 1
    assert trade['entryPrice'] == pytest.approx(110)


def test_next_open_signal_on_last_bar_is_dropped():
    # No next bar exists to fill at -> the signal must be skipped, not
    # silently filled some other way.
    ohlcv = flat_ohlcv(5)
    ohlcv['close'][4] = 999  # entry signal only true on the final bar
    config = {'entry': 'close == 999', 'exit': 'close == -1'}
    result = run_backtest(config, ohlcv)
    assert result['error'] is None
    assert result['trades'] == []


# ── No pyramiding ─────────────────────────────────────────────────────────

def test_no_pyramiding_repeated_entry_signal_dropped():
    n = 8
    ohlcv = flat_ohlcv(n)
    config = {
        'entry': 'close >= 100', 'exit': 'close == -1',  # entry stays true forever
        'entryMode': 'close', 'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, ohlcv)
    assert result['error'] is None
    # Exactly one open position the whole run -> exactly one trade (force-closed
    # at end of data), never re-entered despite the signal staying true.
    assert len(result['trades']) == 1
    assert result['trades'][0]['entryBar'] == 0


# ── Force-close at end of data ────────────────────────────────────────────

def test_force_close_at_end_of_data():
    n = 6
    ohlcv = flat_ohlcv(n)
    config = {
        'entry': 'close == 100', 'exit': 'close == -1',  # exit never fires
        'entryMode': 'close', 'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, ohlcv)
    trade = result['trades'][0]
    assert trade['exitReason'] == 'end_of_data'
    assert trade['exitBar'] == n - 1


# ── Commission is round-trip (2x) ─────────────────────────────────────────

def test_commission_is_round_trip():
    n = 6
    ohlcv = flat_ohlcv(n, price=100)
    ohlcv['low'][0] = 99.5    # one-off entry marker, close stays flat at 100
    ohlcv['high'][4] = 999    # one-off exit marker
    config = {
        'entry': 'low == 99.5', 'exit': 'high == 999',
        'entryMode': 'close', 'commissionPercent': 0.1, 'slippageBps': 0,
        'initialCapital': 10000, 'sizePercent': 100,
    }
    result = run_backtest(config, ohlcv)
    assert len(result['trades']) == 1
    # posValue = 10000, commission = 2 * 0.001 * 10000 = 20. Flat price -> gross pnl 0.
    trade = result['trades'][0]
    assert trade['pnl'] == pytest.approx(-20, abs=0.01)


# ── direction='both' reverses instead of flattening ───────────────────────

def test_direction_both_reverses_on_exit_signal():
    n = 6
    ohlcv = flat_ohlcv(n, price=100)
    ohlcv['high'][3] = 999  # exit/reverse trigger
    config = {
        'entry': 'close == 100', 'exit': 'high == 999',
        'direction': 'both', 'entryMode': 'close',
        'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, ohlcv)
    assert len(result['trades']) == 2
    first, second = result['trades']
    assert first['direction'] == 'long'
    assert first['exitBar'] == 3
    # Reversal opens the opposite side on the SAME bar the long was closed.
    assert second['entryBar'] == 3
    assert second['direction'] == 'short'


# ── Stop beats target on the same bar (if/else-if, not distance-based) ────

def test_stop_beats_target_on_same_bar_collision():
    ohlcv = {
        'open': [100, 100, 100, 100, 100],
        'high': [100.1, 110, 100.1, 100.1, 100.1],   # bar1 high breaches TP (105)
        'low': [99.9, 90, 99.9, 99.9, 99.9],          # bar1 low breaches stop (95)
        'close': [100, 100, 100, 100, 100],
        'volume': [1, 1, 1, 1, 1],
    }
    config = {
        'entry': 'close == 100', 'exit': 'close == -1',
        'entryMode': 'close', 'stopType': 'intrabar',
        'stopLossPercent': 5, 'takeProfitPercent': 5,
        'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, ohlcv)
    trade = result['trades'][0]
    assert trade['exitReason'] == 'stop_intrabar'
    assert trade['exitPrice'] == pytest.approx(95)


# ── Gap-aware fills ─────────────────────────────────
# Faithful (gapAwareFills=False) always fills at the nominal stop/target
# price, matching static/js/backtester.js exactly. Gap-aware (default, True)
# fills a stop at the WORSE of (nominal, bar open) and a target at the
# BETTER of (nominal, bar open).

def _gapped_long_stop_ohlcv():
    return {
        'open': [100, 90, 100, 100, 100],   # bar1 gaps down through the stop (95)
        'high': [100.1, 92, 100.1, 100.1, 100.1],
        'low': [99.9, 85, 99.9, 99.9, 99.9],
        'close': [100, 90, 100, 100, 100],
        'volume': [1, 1, 1, 1, 1],
    }


def test_gap_aware_long_stop_fills_at_worse_of_stop_and_open():
    config = {
        'entry': 'close == 100', 'exit': 'close == -1',
        'entryMode': 'close', 'stopType': 'intrabar', 'stopLossPercent': 5,
        'commissionPercent': 0, 'slippageBps': 0,
    }
    gap_aware = run_backtest(config, _gapped_long_stop_ohlcv())
    faithful = run_backtest({**config, 'gapAwareFills': False}, _gapped_long_stop_ohlcv())

    assert gap_aware['trades'][0]['exitPrice'] == pytest.approx(90)   # open, worse than nominal 95
    assert faithful['trades'][0]['exitPrice'] == pytest.approx(95)    # nominal, JS-faithful
    assert gap_aware['trades'][0]['exitReason'] == 'stop_intrabar'
    assert faithful['trades'][0]['exitReason'] == 'stop_intrabar'


def _gapped_long_target_ohlcv():
    return {
        'open': [100, 110, 100, 100, 100],   # bar1 gaps up through the target (105)
        'high': [100.1, 115, 100.1, 100.1, 100.1],
        'low': [99.9, 108, 99.9, 99.9, 99.9],
        'close': [100, 110, 100, 100, 100],
        'volume': [1, 1, 1, 1, 1],
    }


def test_gap_aware_long_target_fills_at_better_of_target_and_open():
    config = {
        'entry': 'close == 100', 'exit': 'close == -1',
        'entryMode': 'close', 'stopType': 'intrabar', 'takeProfitPercent': 5,
        'commissionPercent': 0, 'slippageBps': 0,
    }
    gap_aware = run_backtest(config, _gapped_long_target_ohlcv())
    faithful = run_backtest({**config, 'gapAwareFills': False}, _gapped_long_target_ohlcv())

    assert gap_aware['trades'][0]['exitPrice'] == pytest.approx(110)  # open, better than nominal 105
    assert faithful['trades'][0]['exitPrice'] == pytest.approx(105)   # nominal, JS-faithful
    assert gap_aware['trades'][0]['exitReason'] == 'tp_intrabar'
    assert faithful['trades'][0]['exitReason'] == 'tp_intrabar'


def _gapped_short_stop_ohlcv():
    return {
        'open': [100, 112, 100, 100, 100],   # bar1 gaps UP through the short stop (105)
        'high': [100.1, 115, 100.1, 100.1, 100.1],
        'low': [99.9, 108, 99.9, 99.9, 99.9],
        'close': [100, 112, 100, 100, 100],
        'volume': [1, 1, 1, 1, 1],
    }


def test_gap_aware_short_stop_fills_at_worse_of_stop_and_open():
    config = {
        'entry': 'close == 100', 'exit': 'close == -1', 'direction': 'short',
        'entryMode': 'close', 'stopType': 'intrabar', 'stopLossPercent': 5,
        'commissionPercent': 0, 'slippageBps': 0,
    }
    gap_aware = run_backtest(config, _gapped_short_stop_ohlcv())
    faithful = run_backtest({**config, 'gapAwareFills': False}, _gapped_short_stop_ohlcv())

    assert gap_aware['trades'][0]['exitPrice'] == pytest.approx(112)  # open, worse (higher) than nominal 105
    assert faithful['trades'][0]['exitPrice'] == pytest.approx(105)


def _gapped_short_target_ohlcv():
    return {
        'open': [100, 85, 100, 100, 100],   # bar1 gaps DOWN through the short target (95)
        'high': [100.1, 92, 100.1, 100.1, 100.1],
        'low': [99.9, 80, 99.9, 99.9, 99.9],
        'close': [100, 85, 100, 100, 100],
        'volume': [1, 1, 1, 1, 1],
    }


def test_gap_aware_short_target_fills_at_better_of_target_and_open():
    config = {
        'entry': 'close == 100', 'exit': 'close == -1', 'direction': 'short',
        'entryMode': 'close', 'stopType': 'intrabar', 'takeProfitPercent': 5,
        'commissionPercent': 0, 'slippageBps': 0,
    }
    gap_aware = run_backtest(config, _gapped_short_target_ohlcv())
    faithful = run_backtest({**config, 'gapAwareFills': False}, _gapped_short_target_ohlcv())

    assert gap_aware['trades'][0]['exitPrice'] == pytest.approx(85)   # open, better (lower) than nominal 95
    assert faithful['trades'][0]['exitPrice'] == pytest.approx(95)


def test_gap_aware_fills_is_default_on():
    # Same config, no explicit gapAwareFills key -> default must match the
    # explicit True case, not the faithful/JS case.
    config = {
        'entry': 'close == 100', 'exit': 'close == -1',
        'entryMode': 'close', 'stopType': 'intrabar', 'stopLossPercent': 5,
        'commissionPercent': 0, 'slippageBps': 0,
    }
    default_result = run_backtest(config, _gapped_long_stop_ohlcv())
    explicit_true = run_backtest({**config, 'gapAwareFills': True}, _gapped_long_stop_ohlcv())
    assert default_result['trades'][0]['exitPrice'] == explicit_true['trades'][0]['exitPrice'] == pytest.approx(90)


def test_no_gap_case_both_modes_agree():
    # When the open does NOT gap through the level, gap-aware and faithful
    # must produce the identical nominal fill (min/max is a no-op).
    ohlcv = {
        'open': [100, 100, 100, 100, 100],  # no gap on bar1
        'high': [100.1, 100.1, 100.1, 100.1, 100.1],
        'low': [99.9, 90, 99.9, 99.9, 99.9],   # low wicks through stop intrabar
        'close': [100, 100, 100, 100, 100],
        'volume': [1, 1, 1, 1, 1],
    }
    config = {
        'entry': 'close == 100', 'exit': 'close == -1',
        'entryMode': 'close', 'stopType': 'intrabar', 'stopLossPercent': 5,
        'commissionPercent': 0, 'slippageBps': 0,
    }
    gap_aware = run_backtest(config, ohlcv)
    faithful = run_backtest({**config, 'gapAwareFills': False}, ohlcv)
    assert gap_aware['trades'][0]['exitPrice'] == faithful['trades'][0]['exitPrice'] == pytest.approx(95)


# ── Funding sign by direction ─────────────────────────────

def _funding_ohlcv(n=10):
    ohlcv = flat_ohlcv(n, price=100)
    ohlcv['high'][8] = 999  # exit marker at bar 8
    ohlcv['funding'] = [0.001] * n  # +0.1% per bar
    return ohlcv


def test_long_pays_funding_when_rate_positive():
    config = {
        'entry': 'close == 100', 'exit': 'high == 999',
        'direction': 'long', 'entryMode': 'close', 'instrument': 'perp',
        'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, _funding_ohlcv())
    assert result['trades'][0]['pnl'] < 0


def test_short_earns_funding_when_rate_positive():
    config = {
        'entry': 'close == 100', 'exit': 'high == 999',
        'entryMode': 'close', 'instrument': 'perp',
        'commissionPercent': 0, 'slippageBps': 0,
    }
    long_result = run_backtest({**config, 'direction': 'long'}, _funding_ohlcv())
    short_result = run_backtest({**config, 'direction': 'short'}, _funding_ohlcv())
    assert short_result['trades'][0]['pnl'] > long_result['trades'][0]['pnl']


def test_no_funding_field_means_no_funding_charge():
    ohlcv = flat_ohlcv(10, price=100)
    ohlcv['high'][8] = 999
    config = {
        'entry': 'close == 100', 'exit': 'high == 999',
        'direction': 'long', 'entryMode': 'close', 'instrument': 'perp',
        'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, ohlcv)  # no 'funding' key in ohlcv
    assert abs(result['trades'][0]['pnl']) < 0.01


# ── Regime tagging + statsRegime split (in JS) ───────────────────────

def test_regime_tagging_and_stats_split():
    # Two one-off entry markers (bar0 = 101, bar3 = 102) so exactly two
    # trades open, deterministically, regardless of how many later bars
    # the (otherwise reusable) entry condition would still match.
    n = 10
    ohlcv = {
        'open':  [101, 100, 100, 102, 100, 100, 100, 100, 100, 100],
        'high':  [101.1, 100.1, 999, 102.1, 100.1, 100.1, 100.1, 100.1, 998, 100.1],
        'low':   [100.9, 99.9, 99.9, 101.9, 99.9, 99.9, 99.9, 99.9, 99.9, 99.9],
        'close': [101, 100, 100, 102, 100, 100, 100, 100, 100, 100],
        'volume': [1] * n,
    }
    config = {
        'entry': 'close == 101 or close == 102', 'exit': 'high == 999 or high == 998',
        'entryMode': 'close', 'commissionPercent': 0, 'slippageBps': 0,
        'regimeSeries': [1, 1, 1, 0, 1, 1, 1, 1, 1, 1],  # entryBar 0 -> favorable, entryBar 3 -> unfavorable
    }
    result = run_backtest(config, ohlcv)
    assert len(result['trades']) == 2
    assert result['trades'][0]['entryBar'] == 0
    assert result['trades'][1]['entryBar'] == 3
    assert result['trades'][0]['regime'] == 1
    assert result['trades'][1]['regime'] == 0
    assert result['statsRegime']['favorable']['totalTrades'] == 1
    assert result['statsRegime']['unfavorable']['totalTrades'] == 1
    assert result['statsRegime']['unknown']['totalTrades'] == 0


# ── Stats block: zero-trade path is all-zero, no NaN/crash ────────────────

def test_zero_trades_stats_are_empty_not_nan():
    ohlcv = flat_ohlcv(10, price=100)
    config = {'entry': 'close == -999', 'exit': 'close == -999'}
    result = run_backtest(config, ohlcv)
    assert result['trades'] == []
    assert result['stats']['totalTrades'] == 0
    assert result['stats']['winRate'] == 0
    assert result['stats']['profitFactor'] == 0
    assert not math.isnan(result['stats']['periodsPerYear'])
    assert result['equity'] == [10000.0] * 10


# ── Equity curve shape ─────────────────────────────────────────────────────

def test_equity_curve_length_matches_bar_count():
    n = 8
    ohlcv = flat_ohlcv(n, price=100)
    ohlcv['high'][5] = 999
    config = {
        'entry': 'close == 100', 'exit': 'high == 999',
        'entryMode': 'close', 'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, ohlcv)
    assert len(result['equity']) == n
    assert result['equity'][0] == pytest.approx(10000)


def test_engine_version_is_a_positive_int():
    assert isinstance(ENGINE_VERSION, int)
    assert ENGINE_VERSION >= 1


# ── ATR-multiple stop / R-multiple target ─────────────────────
#
# Fixture: constant close=100, high=close+1, low=close-1 for bars 0..14
# (range 2 every bar -> Wilder ATR seed/steady-state = 2 exactly, no drift).
# Entry is marked via `volume == 999` (a field ATR doesn't read) so the OHLC
# used for the ATR calc is never disturbed by the entry marker itself.
# Bar index 14 (the 15th bar, 0-based) is the first bar with a valid ATR —
# see _compute_atr14's docstring.

def _atr_warmup_ohlcv(n=20, marker_bar=14):
    ohlcv = {
        'open': [100.0] * n,
        'high': [101.0] * n,
        'low': [99.0] * n,
        'close': [100.0] * n,
        'volume': [1.0] * n,
    }
    ohlcv['volume'][marker_bar] = 999.0
    return ohlcv


def test_atr_stop_long_distance_matches_mult_times_atr():
    config = {
        'entry': 'volume == 999', 'exit': 'close == -1',
        'entryMode': 'close', 'direction': 'long',
        'stopAtrMult': 1.5, 'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, _atr_warmup_ohlcv())
    assert result['error'] is None
    assert len(result['trades']) == 1
    trade = result['trades'][0]
    assert trade['entryBar'] == 14
    assert trade['entryPrice'] == pytest.approx(100)
    # ATR(14) at bar 14 == 2 (constant true range) -> stop distance = 1.5*2 = 3.
    assert trade['stopPrice'] == pytest.approx(97)


def test_atr_stop_short_distance_mirrors_correctly():
    config = {
        'entry': 'volume == 999', 'exit': 'close == -1',
        'entryMode': 'close', 'direction': 'short',
        'stopAtrMult': 1.5, 'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, _atr_warmup_ohlcv())
    trade = result['trades'][0]
    assert trade['entryBar'] == 14
    # Short stop sits ABOVE entry: 100 + 1.5*2 = 103.
    assert trade['stopPrice'] == pytest.approx(103)


def test_r_multiple_target_long_is_multiple_of_actual_stop_distance():
    config = {
        'entry': 'volume == 999', 'exit': 'close == -1',
        'entryMode': 'close', 'direction': 'long',
        'stopAtrMult': 1.5, 'targetRMult': 2, 'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, _atr_warmup_ohlcv())
    trade = result['trades'][0]
    # stop distance = 1.5*2 = 3 -> target = entry + 2*3 = 106, NOT entry*(1+pct).
    assert trade['stopPrice'] == pytest.approx(97)
    assert trade['tpPrice'] == pytest.approx(106)


def test_r_multiple_target_short_mirrors_correctly():
    config = {
        'entry': 'volume == 999', 'exit': 'close == -1',
        'entryMode': 'close', 'direction': 'short',
        'stopAtrMult': 1.5, 'targetRMult': 2, 'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, _atr_warmup_ohlcv())
    trade = result['trades'][0]
    assert trade['stopPrice'] == pytest.approx(103)
    # Short target sits BELOW entry: 100 - 2*3 = 94.
    assert trade['tpPrice'] == pytest.approx(94)


# ── Mutual exclusion / dependency validation ───────────────────────────────

def test_stop_atr_mult_and_stop_loss_percent_are_mutually_exclusive():
    config = {
        'entry': 'volume == 999', 'exit': 'close == -1',
        'stopAtrMult': 1.5, 'stopLossPercent': 5,
    }
    result = run_backtest(config, _atr_warmup_ohlcv())
    assert result['error'] is not None
    assert 'mutually exclusive' in result['error'].lower()
    assert result['trades'] == []


def test_target_r_mult_and_take_profit_percent_are_mutually_exclusive():
    config = {
        'entry': 'volume == 999', 'exit': 'close == -1',
        'stopAtrMult': 1.5, 'targetRMult': 2, 'takeProfitPercent': 5,
    }
    result = run_backtest(config, _atr_warmup_ohlcv())
    assert result['error'] is not None
    assert 'mutually exclusive' in result['error'].lower()


def test_target_r_mult_without_any_stop_errors():
    config = {
        'entry': 'volume == 999', 'exit': 'close == -1',
        'targetRMult': 2,  # neither stopAtrMult nor stopLossPercent -> no R to measure
    }
    result = run_backtest(config, _atr_warmup_ohlcv())
    assert result['error'] is not None
    assert 'stop' in result['error'].lower()


def test_target_r_mult_pairs_with_a_percent_stop():
    """R is the initial risk distance, defined by the stop whatever unit it was
    spelled in — so an R-multiple target pairs with stopLossPercent too, not only
    with stopAtrMult. The alternative (R available only alongside an ATR stop) is a
    conditional rule every caller would have to memorise."""
    ohlcv = _atr_warmup_ohlcv()
    config = {
        'entry': 'close > 0', 'exit': 'close < 0',
        'stopType': 'intrabar', 'stopLossPercent': 4, 'targetRMult': 2,
    }
    result = run_backtest(config, ohlcv)
    assert result['error'] is None, result['error']
    assert result['trades'], 'expected at least one trade to inspect'
    for t in result['trades']:
        entry = t['entryPrice']
        stop_distance = entry * 0.04
        assert t['stopPrice'] == pytest.approx(entry - stop_distance)
        # target sits at 2R = twice the ACTUAL stop distance
        assert t['tpPrice'] == pytest.approx(entry + 2 * stop_distance)


def test_percent_stop_r_target_equals_equivalent_take_profit_percent():
    """With a percent stop R is constant, so 2R must be arithmetically identical to
    takeProfitPercent at twice the stop. Pinning the redundancy deliberately: it is
    the price of keeping the R unit universally available, and if the two ever
    diverge one of the two code paths has drifted."""
    ohlcv = _atr_warmup_ohlcv()
    base = {'entry': 'close > 0', 'exit': 'close < 0',
            'stopType': 'intrabar', 'stopLossPercent': 3}
    via_r = run_backtest({**base, 'targetRMult': 2}, ohlcv)
    via_pct = run_backtest({**base, 'takeProfitPercent': 6}, ohlcv)
    assert via_r['error'] is None and via_pct['error'] is None
    assert len(via_r['trades']) == len(via_pct['trades'])
    for a, b in zip(via_r['trades'], via_pct['trades']):
        assert a['tpPrice'] == pytest.approx(b['tpPrice'])
        assert a['exitReason'] == b['exitReason']
        assert a['pnl'] == pytest.approx(b['pnl'])


def test_negative_stop_atr_mult_errors():
    result = run_backtest(
        {'entry': 'close > 0', 'exit': 'close < 0', 'stopAtrMult': -1},
        flat_ohlcv(20),
    )
    assert result['error'] is not None


def test_negative_target_r_mult_errors():
    result = run_backtest(
        {'entry': 'close > 0', 'exit': 'close < 0', 'targetRMult': -1},
        flat_ohlcv(20),
    )
    assert result['error'] is not None


# ── Warmup-period behaviour: pinned, not incidental ────────────────────────

def test_atr_stop_entry_skipped_during_warmup_then_opens_once_warm():
    """Entry condition is TRUE on every bar (close == 100 always). With
    stopAtrMult active, bars 0..13 have no valid ATR yet -> the entry must be
    SKIPPED on each of those bars (no position, no trade), and only actually
    open once ATR warms up at bar 14 -- exactly the documented decision in
    run_backtest's "ATR stop / R-multiple target" docstring section, not a
    partial/undefined-stop position opened early."""
    n = 20
    ohlcv = {
        'open': [100.0] * n, 'high': [101.0] * n, 'low': [99.0] * n,
        'close': [100.0] * n, 'volume': [1.0] * n,
    }
    config = {
        'entry': 'close == 100', 'exit': 'close == -1',
        'entryMode': 'close', 'stopAtrMult': 1.0,
        'commissionPercent': 0, 'slippageBps': 0,
    }
    result = run_backtest(config, ohlcv)
    assert result['error'] is None
    assert len(result['trades']) == 1
    trade = result['trades'][0]
    assert trade['entryBar'] == 14  # not 0 -- warmup bars were skipped
    assert trade['stopPrice'] == pytest.approx(98)  # 100 - 1.0*2


# ── Acceptance criterion: results MOVE across stopAtrMult values ──────────

def _atr_mult_comparison_ohlcv():
    """Bars 0-14: constant range=2 -> ATR(14) at entry (bar14) == 2 exactly.
    Bars 15-17 each wick progressively lower, breaching a WIDER stop on a
    LATER bar than a tighter one -- built so mult 1.0 / 1.5 / 3.0 each exit
    on a different bar (15 / 16 / 17) with a different fill price, so the
    three runs cannot come out byte-identical (the stopType near-miss bug-class signature)."""
    n = 25
    ohlcv = {
        'open': [100.0] * n, 'high': [101.0] * n, 'low': [99.0] * n,
        'close': [100.0] * n, 'volume': [1.0] * n,
    }
    ohlcv['volume'][14] = 999.0
    ohlcv['low'][15] = 97.5   # breaches mult=1.0's stop (98), not 1.5's (97) or 3.0's (94)
    ohlcv['low'][16] = 96.5   # breaches mult=1.5's stop (97), not 3.0's (94)
    ohlcv['low'][17] = 93.0   # breaches mult=3.0's stop (94)
    return ohlcv


def _run_atr_mult(mult):
    config = {
        'entry': 'volume == 999', 'exit': 'close == -1',
        'entryMode': 'close', 'direction': 'long', 'stopType': 'intrabar',
        'stopAtrMult': mult, 'commissionPercent': 0, 'slippageBps': 0,
    }
    return run_backtest(config, _atr_mult_comparison_ohlcv())


def test_results_move_across_stop_atr_mult_1_0_1_5_3_0():
    r10 = _run_atr_mult(1.0)
    r15 = _run_atr_mult(1.5)
    r30 = _run_atr_mult(3.0)

    for r in (r10, r15, r30):
        assert r['error'] is None
        assert len(r['trades']) == 1
        assert r['trades'][0]['exitReason'] == 'stop_intrabar'

    exit_bars = (r10['trades'][0]['exitBar'], r15['trades'][0]['exitBar'], r30['trades'][0]['exitBar'])
    assert exit_bars == (15, 16, 17), f'expected distinct exit bars per multiple, got {exit_bars}'

    exit_prices = (r10['trades'][0]['exitPrice'], r15['trades'][0]['exitPrice'], r30['trades'][0]['exitPrice'])
    assert exit_prices[0] != pytest.approx(exit_prices[1])
    assert exit_prices[1] != pytest.approx(exit_prices[2])

    returns = (r10['stats']['totalReturnPercent'], r15['stats']['totalReturnPercent'], r30['stats']['totalReturnPercent'])
    assert returns[0] != pytest.approx(returns[1])
    assert returns[1] != pytest.approx(returns[2])
    assert returns[0] != pytest.approx(returns[2])


# ── Additive guarantee: absent/zero stopAtrMult+targetRMult change nothing ─

def test_absent_atr_keys_match_explicit_zero_keys():
    ohlcv = flat_ohlcv(10, price=100)
    ohlcv['low'][0] = 99.5
    ohlcv['high'][4] = 999
    base_config = {
        'entry': 'low == 99.5', 'exit': 'high == 999',
        'entryMode': 'close', 'commissionPercent': 0.1, 'slippageBps': 0,
        'initialCapital': 10000, 'sizePercent': 100,
    }
    without_keys = run_backtest(base_config, ohlcv)
    with_zero_keys = run_backtest({**base_config, 'stopAtrMult': 0, 'targetRMult': 0}, ohlcv)
    assert without_keys['trades'] == with_zero_keys['trades']
    assert without_keys['equity'] == with_zero_keys['equity']
    assert without_keys['stats'] == with_zero_keys['stats']


def test_percent_stop_still_works_unchanged_alongside_new_keys_present_but_zero():
    """The existing percent-stop test (test_stop_beats_target_on_same_bar_collision)
    re-run with stopAtrMult/targetRMult explicitly present at 0 -- must be
    byte-identical to the pre-ATR-multiple percent-only path."""
    ohlcv = {
        'open': [100, 100, 100, 100, 100],
        'high': [100.1, 110, 100.1, 100.1, 100.1],
        'low': [99.9, 90, 99.9, 99.9, 99.9],
        'close': [100, 100, 100, 100, 100],
        'volume': [1, 1, 1, 1, 1],
    }
    base_config = {
        'entry': 'close == 100', 'exit': 'close == -1',
        'entryMode': 'close', 'stopType': 'intrabar',
        'stopLossPercent': 5, 'takeProfitPercent': 5,
        'commissionPercent': 0, 'slippageBps': 0,
    }
    without_keys = run_backtest(base_config, ohlcv)
    with_zero_keys = run_backtest({**base_config, 'stopAtrMult': 0, 'targetRMult': 0}, ohlcv)
    assert without_keys['trades'][0]['exitReason'] == with_zero_keys['trades'][0]['exitReason'] == 'stop_intrabar'
    assert without_keys['trades'][0]['exitPrice'] == pytest.approx(with_zero_keys['trades'][0]['exitPrice'])


# ── modelling assumptions are named, not buried ──────────────


def _same_bar_collision_ohlcv():
    """A bar that touches BOTH a 2% stop and a 2% target from a 100 entry.

    Entry fires on bar 1 (close == 100) and fills at bar 2's open under the
    engine default. Bar 3 spans 97..104, so from a 100 entry a long's 98 stop
    and 102 target are both inside it — the case where the engine has to make
    a choice bar data cannot settle.
    """
    return {
        'open':  [100.0, 100.0, 100.0, 100.0, 100.0],
        'high':  [100.5, 100.5, 100.5, 104.0, 100.5],
        'low':   [ 99.5,  99.5,  99.5,  97.0,  99.5],
        'close': [ 99.0, 100.0, 100.0, 100.0,  99.0],
        'volume': [1.0] * 5,
    }


def _run_collision(**overrides):
    cfg = {
        'entry': 'close == 100',
        'exit': 'close == 12345',  # never — force the stop/target to resolve it
        'direction': 'long',
        'stopType': 'intrabar',
        'stopLossPercent': 2.0,
        'takeProfitPercent': 2.0,
        'gapAwareFills': False,  # isolate precedence from gap adjustment
    }
    cfg.update(overrides)
    return run_backtest(cfg, _same_bar_collision_ohlcv())


def test_same_bar_default_takes_the_stop():
    """The pessimistic reading, and what the JS engine does."""
    out = _run_collision()
    assert not out.get('error'), out.get('error')
    assert out['trades'], 'expected a trade to open and close'
    assert out['trades'][0]['exitReason'] == 'stop_intrabar'


def test_same_bar_target_first_takes_the_target():
    out = _run_collision(sameBarPrecedence='target_first')
    assert not out.get('error'), out.get('error')
    assert out['trades'][0]['exitReason'] == 'tp_intrabar'


def test_same_bar_precedence_changes_the_result():
    """The whole point of naming it — the assumption is worth something."""
    stop_first = _run_collision()['stats']['totalReturnPercent']
    target_first = _run_collision(sameBarPrecedence='target_first')['stats']['totalReturnPercent']
    assert target_first > stop_first, (
        'taking the target instead of the stop must improve the result — '
        f'got stop_first={stop_first}, target_first={target_first}'
    )


def test_same_bar_precedence_explicit_default_matches_omitted():
    assert _run_collision(sameBarPrecedence='stop_first')['stats'] == _run_collision()['stats']


def test_invalid_same_bar_precedence_errors():
    """Fail loud — an unrecognised value must not silently pick a side."""
    out = _run_collision(sameBarPrecedence='nearest')
    assert out.get('error')
    assert 'sameBarPrecedence' in out['error']


def test_precedence_only_matters_when_both_are_touched():
    """A bar that touches only the stop resolves the same either way."""
    ohlcv = _same_bar_collision_ohlcv()
    ohlcv['high'][3] = 100.5  # target no longer reachable
    cfg = {
        'entry': 'close == 100', 'exit': 'close == 12345', 'direction': 'long',
        'stopType': 'intrabar', 'stopLossPercent': 2.0, 'takeProfitPercent': 2.0,
        'gapAwareFills': False,
    }
    a = run_backtest(dict(cfg), ohlcv)
    b = run_backtest(dict(cfg, sameBarPrecedence='target_first'), ohlcv)
    assert a['trades'][0]['exitReason'] == b['trades'][0]['exitReason'] == 'stop_intrabar'


def _gapping_ohlcv():
    """Bar 3 gaps down through anything set on bar 2 — a weekend-style gap.

    24/7 crypto barely exercises these assumptions (the next bar's open is
    effectively the prior close), which is why they are easy to miss. Anything
    session-traded does.
    """
    return {
        'open':  [90.0, 91.0, 100.0, 94.0, 95.0],
        'high':  [90.5, 91.5, 100.5, 94.5, 95.5],
        'low':   [89.5, 90.5,  99.5, 93.0, 94.5],
        'close': [90.0, 91.0, 100.0, 94.0, 95.0],
        'volume': [1.0] * 5,
    }


def test_gap_aware_fills_changes_the_stop_fill_price():
    """A stop the market gapped past cannot fill at its nominal price."""
    ohlcv = {
        'open':  [100.0, 100.0, 100.0, 94.0, 100.0],
        'high':  [100.5, 100.5, 100.5, 94.5, 100.5],
        'low':   [ 99.5,  99.5,  99.5, 93.0,  99.5],
        'close': [ 99.0, 100.0, 100.0, 94.0,  99.0],
        'volume': [1.0] * 5,
    }
    cfg = dict(entry='close == 100', exit='close == 12345', direction='long',
               stopType='intrabar', stopLossPercent=2.0)
    realistic = run_backtest(dict(cfg, gapAwareFills=True), ohlcv)
    nominal = run_backtest(dict(cfg, gapAwareFills=False), ohlcv)

    assert realistic['trades'][0]['exitPrice'] == 94.0, 'should fill at the gapped open'
    assert nominal['trades'][0]['exitPrice'] == 98.0, 'should fill at the nominal stop'
    assert realistic['stats']['totalReturnPercent'] < nominal['stats']['totalReturnPercent'], (
        'nominal fills must look better — that is the bias being disclosed'
    )


def test_entry_mode_close_is_look_ahead_and_moves_the_result():
    """`close` buys at a price only knowable after the bar
    shut — the bias that shipped as the UI default for months."""
    ohlcv = _gapping_ohlcv()
    cfg = dict(entry='close == 100', exit='close == 95', direction='long')
    realistic = run_backtest(dict(cfg, entryMode='next_open'), ohlcv)
    lookahead = run_backtest(dict(cfg, entryMode='close'), ohlcv)

    assert realistic['trades'][0]['entryPrice'] == 94.0
    assert lookahead['trades'][0]['entryPrice'] == 100.0
    assert realistic['stats']['totalReturnPercent'] != lookahead['stats']['totalReturnPercent']


def test_engine_default_entry_mode_is_next_open():
    """Omitting entryMode must give the realistic reading, not look-ahead."""
    ohlcv = _gapping_ohlcv()
    cfg = dict(entry='close == 100', exit='close == 95', direction='long')
    assert (run_backtest(dict(cfg), ohlcv)['stats']
            == run_backtest(dict(cfg, entryMode='next_open'), ohlcv)['stats'])
