"""
Full-Simulation trade engine — a faithful Python port of static/js/backtester.js.

A standalone, MCP-agnostic module so a
programmatic caller (an automated client, tests, a future MCP tool) can run the same
"Full Simulation" backtest model the UI's chart backtester runs — separate
entry/exit formulas, position sizing, commission, slippage, intrabar
stop/target — instead of only the fixed-hold-bars model on MCP today.

`static/js/backtester.js` (`Backtester.run`) is the one and only prior
implementation of this model's semantics and has never run outside a
browser. This module ports it bar-for-bar.

Wired into MCP as the `backtest_simulate` tool — `carrac/mcp/tools/backtest.py`
imports `run_backtest` from here and exposes it at `min_tier='member'`,
`feature='backtester'`.

Formula evaluation reuses `carrac/formula_eval.py` (the existing Python
mirror of the JS FormulaParser) exactly the way
`carrac/mcp/tools/indicators.py:backtest_indicator_formula` does: tokenize →
parse → Evaluator.eval() against an OHLCV context dict.

Public API
----------
    run_backtest(config: dict, ohlcv: dict) -> dict

See `run_backtest`'s docstring for the config/ohlcv shape and the return
shape (`error`, `trades`, `equity`, `stats`, `statsRegime` — same keys as
the JS engine's return value).

ENGINE_VERSION bump discipline mirrors `carrac/formula_eval.py`: bump
whenever output semantics change, so a saved/cached result can be told
apart from a stale one once this is exposed to callers.
"""

import math
from typing import Any, Dict, List, Optional

import numpy as np

from carrac.formula_eval import Evaluator, Parser, tokenize

# v1: faithful port of static/js/backtester.js + the gap-aware
# intrabar stop/target fill mode (see `gapAwareFills` in run_backtest's
# docstring). v2 adds stopAtrMult/targetRMult (ATR-multiple stop,
# R-multiple target) — additive when absent/0, but output semantics change
# (a warmed-up ATR stop can skip an entry) whenever they're used, so this
# bumps per the module's own bump discipline. Bump when engine semantics
# change.
ENGINE_VERSION = 2

SECONDS_PER_YEAR = 365 * 24 * 3600
ATR_PERIOD = 14


def run_backtest(config: Dict[str, Any], ohlcv: Dict[str, Any]) -> Dict[str, Any]:
    """Run a Full-Simulation backtest. Mirrors `static/js/backtester.js` bar-for-bar.

    config (all optional except entry/exit):
        entry, exit          str  — formula expressions (see formula_eval). REQUIRED.
        direction            'long' | 'short' | 'both'         (default 'long')
        sizePercent          float, % of equity risked per position (default 100)
        commissionPercent    float, % per side — charged round-trip, i.e. 2x (default 0)
        slippageBps          float, flat basis points against the trader (default 0)
        initialCapital       float (default 10000)
        startBar / endBar    int, inclusive bar range (default 0 / n-1)
        stopType             'close' | 'intrabar' (default 'close' — no stop/target at all)
        stopLossPercent      float, % below/above entry price (default 0 = off)
        takeProfitPercent    float, % above/below entry price (default 0 = off)
        stopAtrMult          float, stop distance as a multiple of ATR(14) AT THE ENTRY
                              BAR (default 0 = off). Mutually exclusive with
                              stopLossPercent — supplying both non-zero is an error,
                              never a silent pick of one. Still needs stopType=
                              'intrabar' to actually be checked, exactly like
                              stopLossPercent. See "ATR stop / R-multiple target"
                              below for the warmup-period behaviour.
        targetRMult          float, take-profit distance as a multiple of the ACTUAL
                              stop distance (an R-multiple; default 0 = off), i.e.
                              target = entry +/- targetRMult * stopDistance — the
                              Strategy Lab's 1.5-ATR-stop / 2R-target convention.
                              REQUIRES a stop — stopAtrMult OR stopLossPercent (there
                              is no R without a stop to measure it against); supplying
                              targetRMult with neither is an error. Mutually exclusive
                              with takeProfitPercent.
        entryMode            'next_open' | 'close' (default 'next_open' — the JS engine's
                              default; the UI's own <select> defaults to 'close', which
                              contradicts its engine — this port reproduces the ENGINE
                              default, not the UI's)
        instrument           'perp' — if set AND ohlcv['funding'] is present, funding
                              accrues per bar, signed by direction
        barSeconds           int, bar length in seconds, for annualisation (default 86400)
        regimeSeries         list[int|float], len == n bars; 1=favorable, 0=unfavorable,
                              anything else (incl. NaN) = unknown. Tags each trade by the
                              regime at its entry bar and adds a `statsRegime` breakdown.
        gapAwareFills         bool (default True) — see "Gap-aware fills" below. Set to
                              False to reproduce static/js/backtester.js exactly (this is
                              what tests/test_backtest_sim_parity.py runs under).
        sameBarPrecedence     'stop_first' (default) | 'target_first' — which side wins
                              when one bar touches BOTH the stop and the target. From bar
                              data alone we cannot know which came first, so this is an
                              assumption, not a measurement. 'stop_first' is pessimistic
                              and matches the JS engine; 'target_first' is the optimistic
                              reading and exists so the difference can be measured
                              rather than argued about.

    ohlcv: dict of arrays. `close` is required; `open`/`high`/`low` default to `close`
        if absent (matching the JS engine). `volume` is unused by this engine but
        formulas may reference it. `funding` (per-bar signed rate) is only read when
        `instrument == 'perp'`.

    Returns: {error, trades, equity, stats, statsRegime} — same shape as the JS engine.
        `error` is None on success. `trades` is a list of dicts (entryBar, exitBar,
        entryPrice, exitPrice, direction, pnl, pnlPercent, bars, fundingCost,
        exitReason, regime). `equity` is the per-bar equity curve (list[float],
        length == len(ohlcv['close'])). `stats` / `statsRegime` mirror the JS
        `computeStats` / `computeTradeStats` output.

    Per-bar order, each bar in [startBar, endBar] (matches the bar loop in backtester.js::run):
        1. intrabar stop/target check (only when stopType == 'intrabar')
        2. exit formula
        3. entry formula (dropped if a position is already open — no pyramiding)
    A stop-loss and take-profit hit on the SAME bar is not distance-based: a long
    checks `low <= stopPrice` first and only checks the target `elif` that failed
    — the stop always wins on a same-bar collision, mirroring the JS `if/else if`
    in the bar loop of backtester.js.

    Gap-aware fills (a deliberate divergence from the JS engine)
    -------------------------------------------------------------------------
    The JS engine always fills an intrabar stop/target at its *nominal* price
    (`position.stopPrice` / `position.tpPrice`), even when the bar's open already
    gapped through it. That is a reasonable approximation for a 24/7 crypto perp
    (gaps are small) but systematically overstates exit quality for anything that
    gaps over a weekend or a session close — you cannot actually get filled at a
    stop price the market never traded at.

    When `gapAwareFills` is True (the default here), a stop fills at the WORSE of
    (nominal stop price, the bar's open) and a target fills at the BETTER of
    (nominal target price, the bar's open):
        long stop:    fill = min(stopPrice, open[bar])
        short stop:   fill = max(stopPrice, open[bar])
        long target:  fill = max(tpPrice,   open[bar])
        short target: fill = min(tpPrice,   open[bar])
    The reasoning is order-type microstructure: a stop is economically a
    stop-market order — once triggered it fills at whatever price is available,
    which can only be *worse* than nominal on a gap. A take-profit is economically
    a limit order — it can never fill worse than its limit price, so on a gap
    through it, a real fill would be *better* than nominal. Passing that
    improvement on for targets (rather than clamping to the nominal price) is a
    deliberate, symmetric choice: it is the same "what would actually have
    happened" question the stop side asks, just in the direction that helps
    the trader instead of hurting them.

    Set `gapAwareFills=False` to reproduce the JS engine exactly (no adjustment —
    always fills at the nominal price). This is the mode the parity tests run in.

    ATR stop / R-multiple target
    -------------------------------------------------------------------------
    `stopAtrMult` and `targetRMult` are the volatility-normalised counterparts
    to `stopLossPercent`/`takeProfitPercent` — the unit the Strategy Lab's
    forward simulator (the other repo's `lab/simulator.py`) uses, so both
    lanes can finally express the same exit. ATR(14) is computed ONCE per run
    (Wilder's method — see `_compute_atr14`) over the full high/low/close
    arrays; at entry, `stopPrice = entryPrice -/+ stopAtrMult * atr[entryBar]`
    and, if `targetRMult` is also set, `tpPrice = entryPrice +/-
    targetRMult * abs(entryPrice - stopPrice)`.

    ATR-based and percent-based stops (and, separately, targets) are
    MUTUALLY EXCLUSIVE — supplying both non-zero is a hard error at the top
    of `run_backtest`, not a silent pick of one, matching the "startBar must
    be <= endBar"-style fail-loud validation already in this function.
    `targetRMult` additionally requires SOME stop — `stopAtrMult` or
    `stopLossPercent`. R is the initial risk distance, which the stop itself
    defines regardless of the unit it was expressed in, so an R-multiple
    target pairs with either. Note that with a percent stop R is constant,
    so `targetRMult: 2` is equivalent to `takeProfitPercent` at twice the
    stop percent; that redundancy is accepted deliberately in preference to
    a conditional rule callers would have to memorise. With an ATR stop R
    varies per trade, which is where the unit earns its keep.

    WARMUP DECISION (documented + pinned by
    tests/test_backtest_sim.py::test_atr_stop_entry_skipped_during_warmup):
    Wilder's ATR needs 15 bars of history (14 true ranges) before its first
    value exists — `_compute_atr14` returns NaN for bar indices 0..ATR_PERIOD-1
    (the first 14 bars; index ATR_PERIOD, the 15th bar, is the first valid
    value). If `stopAtrMult` is active and an entry signal fires at a bar
    whose ATR is still NaN, the entry is SKIPPED — no position opens — rather
    than opening one with an undefined stop (which would be exactly the
    accept-and-silently-ignore failure mode this guards against) or
    aborting the whole run (overly disruptive for a transient lead-in window
    a caller may not control, e.g. an automated client scanning many symbols on a shared
    lookback). This mirrors the engine's existing "entryMode='next_open'
    signal on the final bar is dropped" precedent above: a documented,
    tested skip, not a substitution. In practice this rarely bites through
    the MCP tool, which already reserves `_SIM_WARMUP_MARGIN_BARS` (200) bars
    of lead-in before the caller's requested window specifically so
    indicators — including this one — are warm by `startBar`.
    """
    if not config.get('entry'):
        return _err('Entry formula is required')
    if not config.get('exit'):
        return _err('Exit formula is required')

    if 'close' not in ohlcv or ohlcv['close'] is None:
        return _err('No price data')
    closes = np.asarray(ohlcv['close'], dtype=np.float64)
    highs = np.asarray(ohlcv['high'], dtype=np.float64) if ohlcv.get('high') is not None else closes
    lows = np.asarray(ohlcv['low'], dtype=np.float64) if ohlcv.get('low') is not None else closes
    opens = np.asarray(ohlcv['open'], dtype=np.float64) if ohlcv.get('open') is not None else closes
    n = len(closes)
    if not n:
        return _err('No price data')

    direction = config.get('direction') or 'long'
    size_pct = (config['sizePercent'] if config.get('sizePercent') is not None else 100) / 100
    comm_pct = (config.get('commissionPercent') or 0) / 100
    slip_bps = max(0.0, float(config.get('slippageBps') or 0))
    slip = slip_bps / 10000
    capital = config.get('initialCapital') or 10000
    start_bar = max(0, config.get('startBar') or 0)
    end_bar = min(n - 1, config['endBar'] if config.get('endBar') is not None else n - 1)

    if start_bar > end_bar:
        return _err('startBar must be <= endBar')

    stop_type = config.get('stopType') or 'close'
    sl_raw = float(config.get('stopLossPercent') or 0)
    tp_raw = float(config.get('takeProfitPercent') or 0)
    sl_pct = sl_raw / 100 if sl_raw > 0 else 0
    tp_pct = tp_raw / 100 if tp_raw > 0 else 0

    # ATR-multiple stop / R-multiple target — mutually exclusive
    # with the percent pair, fail loud rather than silently pick one.
    stop_atr_mult = float(config.get('stopAtrMult') or 0)
    if stop_atr_mult < 0:
        return _err('stopAtrMult must be >= 0')
    target_r_mult = float(config.get('targetRMult') or 0)
    if target_r_mult < 0:
        return _err('targetRMult must be >= 0')
    if stop_atr_mult > 0 and sl_pct > 0:
        return _err('stopAtrMult and stopLossPercent are mutually exclusive — supply only one stop unit per run.')
    if target_r_mult > 0 and tp_pct > 0:
        return _err('targetRMult and takeProfitPercent are mutually exclusive — supply only one target unit per run.')
    if target_r_mult > 0 and stop_atr_mult <= 0 and sl_pct <= 0:
        return _err('targetRMult requires a stop (stopAtrMult or stopLossPercent) — an R-multiple target has no stop distance to measure R against.')

    atr_series = _compute_atr14(highs, lows, closes) if stop_atr_mult > 0 else None

    # Default fill is the NEXT bar's open,
    # not the same bar's close — evaluating a signal and filling at that same
    # close is a look-ahead bias. Reproduces the JS engine default, not the UI
    # <select>'s default of 'close'.
    entry_mode = config.get('entryMode') or 'next_open'
    gap_aware = config.get('gapAwareFills', True)
    # 'stop_first' is the pessimistic reading and matches the JS
    # engine's if/else-if — keeping it as the default means no existing result
    # moves. 'target_first' exists so the assumption can be inverted and the
    # delta measured, which is the only honest way to show how much it is worth.
    same_bar_precedence = config.get('sameBarPrecedence', 'stop_first')
    if same_bar_precedence not in ('stop_first', 'target_first'):
        return _err("sameBarPrecedence must be 'stop_first' or 'target_first'")

    regime_series = config.get('regimeSeries')
    if not (isinstance(regime_series, (list, tuple)) and len(regime_series) == n):
        regime_series = None

    funding = None
    if config.get('instrument') == 'perp' and ohlcv.get('funding') is not None:
        funding = np.asarray(ohlcv['funding'], dtype=np.float64)

    # ── Evaluate formulas (independent boolean series) ──────────────────
    context = {k: np.asarray(v, dtype=np.float64) for k, v in ohlcv.items()
               if k in ('open', 'high', 'low', 'close', 'volume')}
    try:
        entry_sig = _eval_formula(config['entry'], context, n)
    except Exception as e:
        return _err(f'Entry: {e}')
    try:
        exit_sig = _eval_formula(config['exit'], context, n)
    except Exception as e:
        return _err(f'Exit: {e}')

    # ── Walk bars ─────────────────────────────────────────────────────
    dir_mult = -1 if direction == 'short' else 1
    equity = capital
    position: Optional[Dict[str, Any]] = None
    trades: List[Dict[str, Any]] = []
    equity_curve: List[float] = [0.0] * n

    for i in range(start_bar):
        equity_curve[i] = capital

    for bar in range(start_bar, end_bar + 1):

        # ── Intrabar stop/target check (fires before exit formula) ──────
        if position and stop_type == 'intrabar':
            stop_hit = None  # 'stop' | 'tp' | None
            fill_price = None
            d = position['dir']
            # Which side wins when a bar touches BOTH the stop and
            # the target is a MODELLING ASSUMPTION, not a fact — from bar data
            # alone we cannot know which price came first. The choice is named
            # (not a hardcoded if/elif) so a caller can see it and the
            # response can report it.
            stop_touched = bool(position['stopPrice']) and (
                lows[bar] <= position['stopPrice'] if d > 0
                else highs[bar] >= position['stopPrice'])
            target_touched = bool(position['tpPrice']) and (
                highs[bar] >= position['tpPrice'] if d > 0
                else lows[bar] <= position['tpPrice'])

            if stop_touched and target_touched:
                take_stop = (same_bar_precedence != 'target_first')
            else:
                take_stop = stop_touched

            if take_stop and stop_touched:
                stop_hit = 'stop'
                fill_price = _gap_fill(position['stopPrice'], opens[bar], d, True, gap_aware)
            elif target_touched:
                stop_hit = 'tp'
                fill_price = _gap_fill(position['tpPrice'], opens[bar], d, False, gap_aware)

            if stop_hit:
                slipped_exit = _apply_slippage(fill_price, position['dir'], 'exit', slip)
                pnl, trade = _close_trade(position, slipped_exit, bar, comm_pct, funding, stop_hit + '_intrabar')
                equity += pnl
                trades.append(trade)
                position = None

        # ── Exit formula check ──────────────────────────────────────────
        if position and _is_sig(exit_sig[bar]):
            slipped_exit = _apply_slippage(closes[bar], position['dir'], 'exit', slip)
            pnl, trade = _close_trade(position, slipped_exit, bar, comm_pct, funding, 'signal')
            equity += pnl
            trades.append(trade)

            if direction == 'both':
                reverse_entry_price = _apply_slippage(closes[bar], -position['dir'], 'entry', slip)
                position = _open_position(
                    bar, reverse_entry_price, -position['dir'], equity, size_pct, sl_pct, tp_pct,
                    stop_atr_mult, target_r_mult,
                    atr_series[bar] if atr_series is not None else None,
                )
            else:
                position = None

        # ── Entry formula check ──────────────────────────────────────────
        if not position and _is_sig(entry_sig[bar]):
            if entry_mode == 'next_open' and bar < end_bar:
                open_fill = _apply_slippage(opens[bar + 1], dir_mult, 'entry', slip)
                position = _open_position(
                    bar + 1, open_fill, dir_mult, equity, size_pct, sl_pct, tp_pct,
                    stop_atr_mult, target_r_mult,
                    atr_series[bar + 1] if atr_series is not None else None,
                )
            elif entry_mode != 'next_open':
                close_fill = _apply_slippage(closes[bar], dir_mult, 'entry', slip)
                position = _open_position(
                    bar, close_fill, dir_mult, equity, size_pct, sl_pct, tp_pct,
                    stop_atr_mult, target_r_mult,
                    atr_series[bar] if atr_series is not None else None,
                )
            # else: next_open and bar == endBar → signal skipped (no next bar)
            # else (ATR stop active, atr[entryBar] is NaN): signal skipped —
            # see run_backtest's "ATR stop / R-multiple target" docstring.

        # ── Equity curve (mark to market) ────────────────────────────────
        if position:
            unrealized = (closes[bar] - position['entryPrice']) * position['dir'] * position['units']
            equity_curve[bar] = equity + unrealized
        else:
            equity_curve[bar] = equity

    # ── Close open position at end of data ───────────────────────────────
    if position:
        slipped_exit = _apply_slippage(closes[end_bar], position['dir'], 'exit', slip)
        pnl, trade = _close_trade(position, slipped_exit, end_bar, comm_pct, funding, 'end_of_data')
        equity += pnl
        trades.append(trade)
        equity_curve[end_bar] = equity

    for i in range(end_bar + 1, n):
        equity_curve[i] = equity

    # ── Tag each trade with the regime at its entry bar ──────────────────
    if regime_series:
        for t in trades:
            rv = regime_series[t['entryBar']]
            t['regime'] = rv if rv in (0, 1) else None

    bar_seconds = config.get('barSeconds') or 86400
    stats = _compute_stats(trades, equity_curve, capital, start_bar, end_bar, bar_seconds)

    stats_regime = None
    if regime_series:
        fav = [t for t in trades if t['regime'] == 1]
        unf = [t for t in trades if t['regime'] == 0]
        unk = [t for t in trades if t['regime'] not in (0, 1)]
        stats_regime = {
            'favorable': _compute_trade_stats(fav, capital),
            'unfavorable': _compute_trade_stats(unf, capital),
            'unknown': _compute_trade_stats(unk, capital),
        }

    return {
        'error': None,
        'trades': trades,
        'equity': equity_curve,
        'stats': stats,
        'statsRegime': stats_regime,
    }


# ── Helpers ──────────────────────────────────────────────────────────────

def _err(msg: str) -> Dict[str, Any]:
    return {'error': msg, 'trades': [], 'equity': [], 'stats': None, 'statsRegime': None}


def _is_sig(val: float) -> bool:
    """Mirrors JS `val && !isNaN(val) && val !== 0` (NaN is falsy in JS)."""
    return (not math.isnan(val)) and val != 0


def _eval_formula(expr: str, context: Dict[str, np.ndarray], n: int) -> np.ndarray:
    """Tokenize/parse/eval one formula, same path as
    carrac/mcp/tools/indicators.py:backtest_indicator_formula. Always returns
    an array (a scalar result is broadcast — same convention as
    tests/test_formula_parity.py's `_eval` helper)."""
    tokens = tokenize(expr)
    ast = Parser(tokens).parse()
    result = Evaluator(context, n, symbol='_BACKTEST_SIM_', timeframe='1D').eval(ast)
    if result[0] == 'a':
        return result[1]
    if result[0] == 's':
        return np.full(n, float(result[1]))
    raise ValueError(f"Formula must evaluate to a series or number, got type '{result[0]}'")


def _apply_slippage(price: float, dir_: int, side: str, slip: float) -> float:
    """Slippage moves the fill price against the trader's direction.

    entry long  -> pay more (price up)     | entry short -> sell lower (price down)
    exit  long  -> sell lower (price down) | exit  short -> cover higher (price up)
    """
    if not slip:
        return price
    if side == 'entry':
        sign = 1 if dir_ > 0 else -1
    else:
        sign = -1 if dir_ > 0 else 1
    return price * (1 + sign * slip)


def _gap_fill(nominal_price: float, open_price: float, dir_: int, is_stop: bool, gap_aware: bool) -> float:
    """Resolve the intrabar stop/target fill price.

    `gap_aware=False` reproduces the JS engine exactly: always the nominal
    price, regardless of the bar's open (the stop/target fills in backtester.js).

    `gap_aware=True` is the gap-aware divergence from the JS engine — see run_backtest's
    docstring "Gap-aware fills" section for the reasoning. A stop can only
    be filled at least as badly as nominal on a gap; a target can only be
    filled at least as well as nominal on a gap.
    """
    if not gap_aware:
        return nominal_price
    if is_stop:
        # Stop-market: worse of (nominal, open). Long's "worse" = lower; short's = higher.
        return min(nominal_price, open_price) if dir_ > 0 else max(nominal_price, open_price)
    # Target/limit: better of (nominal, open). Long's "better" = higher; short's = lower.
    return max(nominal_price, open_price) if dir_ > 0 else min(nominal_price, open_price)


def _compute_atr14(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> np.ndarray:
    """Wilder ATR(14) as a per-bar series, NaN until warmed up.

    Independent implementation of the same algorithm as the sibling Strategy
    Lab repo's `lab/simulator.py::atr14` (read for reference, not imported —
    the two repos are only source-aligned by convention). True range for bar
    i (i >= 1): max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-
    close[i-1])); bar 0 has no previous close, so it has no true range.

    atr[i] is NaN for i in [0, ATR_PERIOD) — the first 14 bars, not enough
    true-range history yet. atr[ATR_PERIOD] (index 14, the 15th bar) is the
    first valid value: the simple mean of the first 14 true ranges (Wilder's
    seed). Every bar after that smooths with the standard 1/14 recursion:
        atr[i] = (atr[i-1] * (ATR_PERIOD - 1) + tr[i]) / ATR_PERIOD
    """
    n = len(closes)
    atr = np.full(n, np.nan)
    if n <= ATR_PERIOD:
        return atr
    trs = np.full(n, np.nan)
    for i in range(1, n):
        trs[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    prev = float(np.mean(trs[1:ATR_PERIOD + 1]))
    atr[ATR_PERIOD] = prev
    for i in range(ATR_PERIOD + 1, n):
        prev = (prev * (ATR_PERIOD - 1) + trs[i]) / ATR_PERIOD
        atr[i] = prev
    return atr


def _open_position(bar: int, price: float, dir_: int, equity: float,
                    size_pct: float, sl_pct: float, tp_pct: float,
                    stop_atr_mult: float = 0.0, target_r_mult: float = 0.0,
                    atr_at_entry: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Open a position, or return None if it can't (or shouldn't) be opened.

    Percent-based stop/target (sl_pct/tp_pct) behave exactly as before
    the ATR/R-multiple options existed when stop_atr_mult/target_r_mult are 0 — purely additive.

    When stop_atr_mult > 0, the stop distance comes from
    `stop_atr_mult * atr_at_entry` instead of a percent of price; when
    target_r_mult > 0, the target is `target_r_mult` R-multiples of the
    ACTUAL stop distance — whichever unit that stop was expressed in.
    run_backtest's validation guarantees stop_atr_mult and sl_pct — and
    target_r_mult and tp_pct — are never both non-zero, and that
    target_r_mult > 0 implies SOME stop exists, so the branches below never
    need to reconcile conflicting units.

    R is the initial risk distance, which is defined by the stop itself and
    not by how that stop was spelled — so an R-multiple target pairs with a
    percent stop as readily as with an ATR one. With a percent stop R is
    constant, making `targetRMult: 2` equivalent to `takeProfitPercent`
    at twice the stop percent; that redundancy is deliberate, because a
    conditional rule ("R is only available if your stop is in ATR") is a
    special case every caller would have to memorise.

    WARMUP: if stop_atr_mult > 0 but atr_at_entry is None/NaN (ATR hasn't
    warmed up at this bar yet), the entry is skipped — returns None rather
    than opening a position with an undefined stop. See run_backtest's
    docstring "ATR stop / R-multiple target" for why.
    """
    pos_value = equity * size_pct
    if pos_value <= 0 or price <= 0:
        return None

    stop_price = None
    tp_price = None
    stop_distance = None

    if stop_atr_mult > 0:
        if atr_at_entry is None or (isinstance(atr_at_entry, float) and math.isnan(atr_at_entry)):
            return None  # ATR not warmed up yet — skip this entry, don't guess.
        stop_distance = stop_atr_mult * atr_at_entry
        stop_price = price - stop_distance if dir_ > 0 else price + stop_distance
    elif sl_pct > 0:
        stop_price = price * (1 - sl_pct) if dir_ > 0 else price * (1 + sl_pct)
        stop_distance = price * sl_pct

    if target_r_mult > 0:
        # Validated by run_backtest: target_r_mult > 0 implies a stop exists
        # (ATR or percent), so stop_distance is always set here.
        tp_price = price + target_r_mult * stop_distance if dir_ > 0 else price - target_r_mult * stop_distance
    elif tp_pct > 0:
        tp_price = price * (1 + tp_pct) if dir_ > 0 else price * (1 - tp_pct)

    return {
        'entryBar': bar,
        'entryPrice': price,
        'dir': dir_,
        'units': pos_value / price,
        'posValue': pos_value,
        'stopPrice': stop_price,
        'tpPrice': tp_price,
    }


def _close_trade(pos: Dict[str, Any], exit_price: float, bar: int, comm_pct: float,
                  funding: Optional[np.ndarray], exit_reason: str):
    gross_pnl = (exit_price - pos['entryPrice']) * pos['dir'] * pos['units']
    commission = 2 * comm_pct * pos['posValue']
    # Funding is signed by direction: longs pay when funding > 0
    # and receive when funding < 0; shorts are the opposite.
    funding_cost = 0.0
    if funding is not None:
        for fb in range(pos['entryBar'], bar + 1):
            rate = funding[fb]
            if rate != 0 and not math.isnan(rate):
                funding_cost += (rate if pos['dir'] > 0 else -rate) * pos['posValue']
    net_pnl = gross_pnl - commission - funding_cost
    trade = {
        'entryBar': pos['entryBar'],
        'exitBar': bar,
        'entryPrice': pos['entryPrice'],
        'exitPrice': exit_price,
        'direction': 'long' if pos['dir'] > 0 else 'short',
        'pnl': net_pnl,
        'pnlPercent': (net_pnl / pos['posValue']) * 100 if pos['posValue'] > 0 else 0,
        'bars': bar - pos['entryBar'],
        'fundingCost': funding_cost,
        'exitReason': exit_reason or 'signal',
        'regime': None,
        # The PLANNED stop/target this trade was opened with (None
        # if neither was configured) — additive field, lets a caller compute
        # the realised stop distance regardless of which unit produced it.
        'stopPrice': pos.get('stopPrice'),
        'tpPrice': pos.get('tpPrice'),
    }
    return net_pnl, trade


def _safe_div(a: float, b: float) -> float:
    """a/b that returns +-inf/nan instead of raising on b == 0 (matches JS)."""
    if b == 0:
        return 0.0 if a == 0 else math.copysign(math.inf, a)
    return a / b


# ── Stats ──────────────────────────────────────────────────────────────

def _compute_trade_stats(trades: List[Dict[str, Any]], initial_capital: float) -> Dict[str, Any]:
    """Trade-level-only stats (mirrors JS `computeTradeStats`) — used for regime-split
    blocks where the portfolio equity curve isn't meaningful. Omits
    Sharpe/Sortino/Calmar/Ulcer (equity-curve based)."""
    n_trades = len(trades)
    empty = {
        'totalTrades': 0, 'winners': 0, 'losers': 0, 'winRate': 0, 'profitFactor': 0,
        'avgWin': 0, 'avgLoss': 0, 'avgRR': 0, 'avgBarsInTrade': 0,
        'maxConsecutiveWins': 0, 'maxConsecutiveLosses': 0,
        'totalReturn': 0, 'totalReturnPercent': 0,
        'sumPnlPercent': 0,
    }
    if n_trades == 0:
        return empty

    winners = losers = 0
    win_pnl = loss_pnl = 0.0
    con_wins = con_losses = 0
    max_con_wins = max_con_losses = 0
    total_bars = 0
    sum_pnl = 0.0
    sum_pnl_pct = 0.0

    for t in trades:
        total_bars += t['bars']
        sum_pnl += t['pnl']
        sum_pnl_pct += t.get('pnlPercent') or 0
        if t['pnl'] > 0:
            winners += 1
            win_pnl += t['pnl']
            con_wins += 1
            con_losses = 0
            max_con_wins = max(max_con_wins, con_wins)
        else:
            losers += 1
            loss_pnl += t['pnl']
            con_losses += 1
            con_wins = 0
            max_con_losses = max(max_con_losses, con_losses)

    avg_win = win_pnl / winners if winners > 0 else 0
    avg_loss = loss_pnl / losers if losers > 0 else 0
    return {
        'totalTrades': n_trades,
        'winners': winners,
        'losers': losers,
        'winRate': winners / n_trades,
        'profitFactor': (win_pnl / -loss_pnl) if loss_pnl < 0 else (math.inf if win_pnl > 0 else 0),
        'avgWin': avg_win,
        'avgLoss': avg_loss,
        'avgRR': (avg_win / -avg_loss) if loss_pnl < 0 else 0,
        'avgBarsInTrade': total_bars / n_trades,
        'maxConsecutiveWins': max_con_wins,
        'maxConsecutiveLosses': max_con_losses,
        'totalReturn': sum_pnl,
        'totalReturnPercent': (sum_pnl / initial_capital) * 100 if initial_capital > 0 else 0,
        'sumPnlPercent': sum_pnl_pct,
    }


def _compute_stats(trades: List[Dict[str, Any]], equity_curve: List[float], initial_capital: float,
                    start_bar: int, end_bar: int, bar_seconds: float) -> Dict[str, Any]:
    periods_per_year = SECONDS_PER_YEAR / bar_seconds if bar_seconds > 0 else 365
    n_trades = len(trades)
    empty_stats = {
        'totalTrades': 0, 'winners': 0, 'losers': 0,
        'winRate': 0, 'profitFactor': 0, 'sharpeRatio': 0, 'sortinoRatio': 0,
        'calmarRatio': 0, 'ulcerIndex': 0,
        'maxDrawdown': 0, 'maxDrawdownPercent': 0,
        'avgWin': 0, 'avgLoss': 0, 'avgRR': 0,
        'totalReturn': 0, 'totalReturnPercent': 0,
        'avgBarsInTrade': 0,
        'maxConsecutiveWins': 0, 'maxConsecutiveLosses': 0,
        'periodsPerYear': periods_per_year,
    }
    if n_trades == 0:
        return empty_stats

    # ── Win/loss accounting ──────────────────────────────────────────────
    winners = losers = 0
    win_pnl = loss_pnl = 0.0
    con_wins = con_losses = 0
    max_con_wins = max_con_losses = 0
    total_bars = 0

    for t in trades:
        total_bars += t['bars']
        if t['pnl'] > 0:
            winners += 1
            win_pnl += t['pnl']
            con_wins += 1
            con_losses = 0
            max_con_wins = max(max_con_wins, con_wins)
        else:
            losers += 1
            loss_pnl += t['pnl']
            con_losses += 1
            con_wins = 0
            max_con_losses = max(max_con_losses, con_losses)

    # ── Max drawdown + Ulcer ──────────────────────────────────────────────
    peak = initial_capital
    max_dd = 0.0
    max_dd_pct = 0.0
    ulcer_sumsq = 0.0
    ulcer_n = 0
    for i in range(start_bar, end_bar + 1):
        if equity_curve[i] > peak:
            peak = equity_curve[i]
        dd = peak - equity_curve[i]
        if dd > max_dd:
            max_dd = dd
        dd_pct = dd / peak if peak > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
        ulcer_sumsq += (dd_pct * 100) ** 2
        ulcer_n += 1
    ulcer_index = math.sqrt(ulcer_sumsq / ulcer_n) if ulcer_n > 0 else 0

    # ── Sharpe + Sortino (timeframe-aware) ────────────────────────────────
    sharpe = 0.0
    sortino = 0.0
    n_bars = end_bar - start_bar
    if n_bars > 1:
        returns = []
        for i in range(n_bars):
            prev = equity_curve[start_bar + i]
            curr = equity_curve[start_bar + i + 1]
            returns.append((curr - prev) / prev if prev > 0 else 0.0)
        mean = sum(returns) / n_bars
        # Sharpe (as in the JS engine): uses sample std (/(N-1)) not biased (/N).
        # Sortino downside deviation = sqrt(mean of (max(0, target-r))^2)
        #   over ALL bars (not just negative ones), target = 0.
        target = 0.0
        var_sum = 0.0
        down_var_sum = 0.0
        for r in returns:
            d = r - mean
            var_sum += d * d
            shortfall = target - r
            if shortfall > 0:
                down_var_sum += shortfall * shortfall
        stdev = math.sqrt(var_sum / (n_bars - 1)) if n_bars > 1 else 0
        down_stdev = math.sqrt(down_var_sum / n_bars)
        ann_factor = math.sqrt(periods_per_year)
        if stdev > 0:
            sharpe = (mean / stdev) * ann_factor
        if down_stdev > 0:
            sortino = ((mean - target) / down_stdev) * ann_factor

    # ── Derived stats ──────────────────────────────────────────────────────
    avg_win = win_pnl / winners if winners > 0 else 0
    avg_loss = loss_pnl / losers if losers > 0 else 0
    final_equity = equity_curve[end_bar]
    total_return = final_equity - initial_capital
    # JS divides unguarded here; `_safe_div` avoids a ZeroDivisionError crash
    # on a degenerate initialCapital==0 config while keeping the same
    # inf/nan result JS's silent float semantics would produce.
    total_return_pct = _safe_div(total_return, initial_capital) * 100

    # Calmar = annualised return / |max DD %|
    calmar = 0.0
    if n_bars > 0 and initial_capital > 0 and max_dd_pct > 0:
        years = n_bars / periods_per_year
        if years > 0:
            final_ratio = final_equity / initial_capital
            if final_ratio > 0:
                ann_return = final_ratio ** (1 / years) - 1
                calmar = ann_return / max_dd_pct

    return {
        'totalTrades': n_trades,
        'winners': winners,
        'losers': losers,
        'winRate': winners / n_trades,
        'profitFactor': (win_pnl / -loss_pnl) if loss_pnl < 0 else (math.inf if win_pnl > 0 else 0),
        'sharpeRatio': sharpe,
        'sortinoRatio': sortino,
        'calmarRatio': calmar,
        'ulcerIndex': ulcer_index,
        'maxDrawdown': max_dd,
        'maxDrawdownPercent': max_dd_pct * 100,
        'avgWin': avg_win,
        'avgLoss': avg_loss,
        'avgRR': (avg_win / -avg_loss) if loss_pnl < 0 else 0,
        'totalReturn': total_return,
        'totalReturnPercent': total_return_pct,
        'avgBarsInTrade': total_bars / n_trades,
        'maxConsecutiveWins': max_con_wins,
        'maxConsecutiveLosses': max_con_losses,
        'periodsPerYear': periods_per_year,
    }
