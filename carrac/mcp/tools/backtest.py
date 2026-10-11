"""MCP tools: Backtest composition + Full Simulation.

Synthesizes a play's structured conditions into a boolean formula expression
and runs the same flip-signal backtest used by backtest_indicator_formula.

Tools:
  - backtest_run        (formula, play or scanner-universe backtest,
                          chosen with source=)
  - backtest_simulate   (Full Simulation model)

backtest_play and scan_and_backtest are plain helpers here, not
standalone MCP tools — backtest_run calls them.
"""

import hashlib
import json
import logging

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError, _json_default
from carrac.mcp.tools import get_mcp_user_id
from carrac.mcp.backtest_models import MODEL_HOLD_N, MODEL_FULL_SIMULATION

logger = logging.getLogger('carrac.mcp.tools.backtest')


# ── Condition → formula synthesis ──────────────────────────────

_OP_MAP = {'gt': '>', 'lt': '<', 'gte': '>=', 'lte': '<=', 'eq': '=='}


def _synthesize_indicator_value(params):
    indicator = (params.get('indicator') or '').upper().strip()
    op = _OP_MAP.get(params.get('operator'))
    val = params.get('value')
    if not indicator or not op or val is None:
        return None
    # Map common indicator names to their formula function calls.
    expr_map = {
        'RSI': 'RSI(14)',
        'MACD': 'MACD()',
        'EMA': 'EMA(close, 21)',
        'CONSENSIO': 'CONSENSIO',  # Pre-computed quant var
    }
    indicator_expr = expr_map.get(indicator, indicator)
    return f'({indicator_expr} {op} {val})'


def _synthesize_indicator_crosses(params):
    indicator = (params.get('indicator') or '').upper().strip()
    direction = params.get('direction')
    val = params.get('value')
    if not indicator or direction not in ('above', 'below') or val is None:
        return None
    expr_map = {'RSI': 'RSI(14)', 'EMA': 'EMA(close, 21)'}
    indicator_expr = expr_map.get(indicator, indicator)
    fn = 'CROSSOVER' if direction == 'above' else 'CROSSUNDER'
    return f'{fn}({indicator_expr}, {val})'


def _synthesize_price_level(params):
    level = params.get('level')
    relation = params.get('relation')
    if level is None or relation not in ('above', 'below'):
        return None
    op = '>' if relation == 'above' else '<'
    return f'(close {op} {level})'


def _synthesize_derivatives_metric(params):
    metric = params.get('metric')
    op = _OP_MAP.get(params.get('operator'))
    val = params.get('value')
    if not metric or not op or val is None:
        return None
    return f'({metric} {op} {val})'


def _synthesize_formula_expression(params):
    expr = (params.get('expression') or '').strip()
    return f'({expr})' if expr else None


_SYNTHS = {
    'indicator_value': _synthesize_indicator_value,
    'indicator_crosses': _synthesize_indicator_crosses,
    'price_level': _synthesize_price_level,
    'derivatives_metric': _synthesize_derivatives_metric,
    'formula_expression': _synthesize_formula_expression,
}


def synthesize_play_formula(conditions_obj):
    """Convert a play's conditions block into a single boolean formula.

    Returns: (expression, supported_count, unsupported_types)
    """
    if not conditions_obj or not isinstance(conditions_obj, dict):
        return None, 0, []
    rules = conditions_obj.get('rules') or []
    logic = (conditions_obj.get('logic') or 'all').lower()
    op = ' AND ' if logic == 'all' else ' OR '

    parts = []
    unsupported = []
    for r in rules:
        ctype = r.get('type')
        synth = _SYNTHS.get(ctype)
        if not synth:
            unsupported.append(ctype)
            continue
        expr = synth(r.get('params') or {})
        if expr:
            parts.append(expr)
        else:
            unsupported.append(f'{ctype}(invalid params)')

    if not parts:
        return None, 0, unsupported
    return op.join(parts), len(parts), unsupported


# ── backtest_play ──────────────────────────────────────────────
#
# No longer a standalone MCP tool — absorbed into backtest_run
# (source=play). Kept as a plain helper, called from backtest_run below.
# The legacy 'backtest_play' tool name still dispatches via
# registry.LEGACY_TOOL_ALIASES straight to backtest_run, which infers
# source='play' from the presence of play_id — see backtest_run's docstring.

def backtest_play(arguments):
    from carrac.plays.models import get_play

    play_id = arguments.get('play_id')
    if not play_id:
        raise ToolError('play_id is required')

    play = get_play(play_id, user_id=get_mcp_user_id())
    if not play:
        raise ToolError(f'Play {play_id} not found')

    symbol = (play.get('primary_asset') or '').upper().strip()
    if not symbol:
        raise ToolError(f'Play {play_id} has no primary_asset; cannot backtest')

    expression, supported, unsupported = synthesize_play_formula(play.get('conditions'))
    if not expression:
        raise ToolError(
            f'No supported conditions to backtest. Skipped: {unsupported or "all"}. '
            f'Supported types: {sorted(_SYNTHS.keys())}.'
        )

    timeframe = arguments.get('timeframe') or play.get('timeframe') or '1D'
    # Normalize play.timeframe (swing/position/until_event/open_ended) to a real TF.
    if timeframe in {'swing', 'position', 'until_event', 'open_ended'}:
        timeframe = '1D'

    # Reuse the backtest_indicator_formula handler.
    from carrac.mcp.tools.indicators import backtest_indicator_formula
    bt_args = {
        'symbol': symbol,
        'expression': expression,
        'timeframe': timeframe,
        'lookback_bars': arguments.get('lookback_bars', 365),
        'hold_bars': arguments.get('hold_bars', 5),
    }
    result = backtest_indicator_formula(bt_args)
    result['play_id'] = play_id
    result['play_title'] = play.get('title')
    result['synthesized_expression'] = expression
    result['conditions_used'] = supported
    result['conditions_skipped'] = unsupported
    return result


# ── scan_and_backtest ──────────────────────────────────────────
#
# No longer a standalone MCP tool — absorbed into backtest_run
# (source=scan). Kept as a plain helper, called from backtest_run below.
# The legacy 'scan_and_backtest' tool name still dispatches via
# registry.LEGACY_TOOL_ALIASES straight to backtest_run, which infers
# source='scan' from the absence of `symbol` and `play_id` — see
# backtest_run's docstring.

_VALID_UNIVERSES = ('scanner', 'all')


def scan_and_backtest(arguments):
    from carrac.mcp.tools.scanner import scanner_get_screen
    from carrac.mcp.tools.indicators import backtest_indicator_formula

    expression = (arguments.get('expression') or '').strip()
    if not expression:
        raise ToolError('expression is required')

    # None/absent -> default; but an EXPLICIT blank ('') must hit validation
    # and raise like any other invalid value — `or 'scanner'` would swallow
    # it, and a blank-universe test would then pass only by accident when an
    # empty scanner cache happened to error downstream.
    universe = arguments.get('universe')
    if universe is None:
        universe = 'scanner'
    if universe not in _VALID_UNIVERSES:
        raise ToolError(
            f'Invalid universe "{universe}". Valid values: {", ".join(_VALID_UNIVERSES)}'
        )

    direction = arguments.get('direction') or 'long'
    timeframe = arguments.get('timeframe') or '1D'
    lookback_bars = arguments.get('lookback_bars', 365)
    hold_bars = arguments.get('hold_bars', 5)
    max_assets = max(1, min(25, int(arguments.get('max_assets') or 10)))

    if universe == 'all':
        # Pull all scanner assets — no verdict filter.
        from carrac.caches import get_cached_scanner
        raw = get_cached_scanner()
        if not raw:
            raise ToolError('Scanner data not available — server is warming up')
        all_rows = raw.get('results', [])
        # Still respect direction sort when specified.
        if direction in ('long', 'short'):
            all_rows = sorted(
                all_rows,
                key=lambda r: r.get(direction, {}).get('total', 0),
                reverse=True,
            )
        symbols = []
        for row in all_rows:
            sym = row.get('symbol') or row.get('asset')
            if sym and sym not in symbols:
                symbols.append(sym)
            if len(symbols) >= max_assets:
                break
        verdict = None  # no filter applied
    else:
        verdict = arguments.get('verdict') or 'STRONG,GOOD'
        # Get scanner output via the existing tool (already compacted).
        scanner_out = scanner_get_screen({'verdict': verdict, 'direction': direction})
        rows = scanner_out.get('results') or scanner_out.get('assets') or []
        symbols = []
        for row in rows:
            sym = row.get('symbol') or row.get('asset')
            if sym and sym not in symbols:
                symbols.append(sym)
            if len(symbols) >= max_assets:
                break

    if not symbols:
        return {
            'model': MODEL_HOLD_N,
            'expression': expression,
            'universe': universe,
            'verdict': verdict,
            'direction': direction,
            'count': 0,
            'message': (
                'No assets found in scanner universe.'
                if universe == 'all'
                else 'No assets matched the verdict filter.'
            ),
        }

    results = []
    errors = []
    for sym in symbols:
        try:
            bt = backtest_indicator_formula({
                'symbol': sym,
                'expression': expression,
                'timeframe': timeframe,
                'lookback_bars': lookback_bars,
                'hold_bars': hold_bars,
            })
            results.append(bt)
        except ToolError as e:
            errors.append({'symbol': sym, 'error': str(e)})
        except Exception as e:
            logger.warning(f'scan_and_backtest({sym}) failed: {e}')
            errors.append({'symbol': sym, 'error': str(e)})

    # Rank by edge proxy: hit_rate * mean_return when both exist.
    def _edge(r):
        hr = r.get('hit_rate')
        mr = r.get('mean_return')
        if hr is None or mr is None:
            return -float('inf')
        return hr * mr

    results.sort(key=_edge, reverse=True)

    out = {
        'model': MODEL_HOLD_N,
        'expression': expression,
        'universe': universe,
        'verdict': verdict,
        'direction': direction,
        'timeframe': timeframe,
        'count': len(results),
        'errors': errors,
        'results': results,
    }
    if universe == 'all':
        out['warning'] = (
            'Universe=all — backtest run across the full scanner universe (~50 assets) '
            'with no setup-quality filter. Results not pre-filtered for trade-readiness.'
        )
    return out


# ── backtest_run ────────────────────────────────────
#
# Consolidates backtest_indicator_formula / backtest_play / scan_and_backtest
# — all three run the IDENTICAL Hold-N flip-signal model, differing only in
# where the boolean expression comes from (a raw formula, a play's structured
# conditions, or the scanner's current universe). One tool, `source` picks
# the input. Do NOT fold backtest_simulate (Full Simulation) or
# backtest_get_historical_analog (Historical Analog) in here — those are
# genuinely different backtest models, not different input sources for the
# same model; conflating them would make `model` in the response ambiguous.

@mcp_tool(
    'backtest_run',
    'Run a Hold-N flip-signal backtest. Model: for every bar where the '
    'boolean expression flips False→True (entry signal), hold for hold_bars '
    'bars and record the forward return. Never compare these metrics to '
    'backtest_simulate (Full Simulation) or backtest_get_historical_analog '
    '(Historical Analog) — different models entirely.\n\n'
    'source picks where the expression comes from:\n'
    '  "formula" (default): supply symbol + expression directly.\n'
    '  "play": supply play_id — the play\'s structured conditions are '
    'synthesized into a formula (manual/drawing/scanner_verdict condition '
    'types cannot be synthesized and are skipped; see conditions_skipped in '
    'the result).\n'
    '  "scan": supply expression (no symbol) — the scanner universe (or '
    'verdict-filtered subset) is backtested asset-by-asset, ranked by '
    'hit_rate × mean_return.\n'
    'source is optional and inferred when omitted: play_id present → play; '
    'no symbol → scan; otherwise → formula.\n\n'
    'Returns (Hold-N model metrics, all sources): model (always "Hold-N '
    'Signal Test"), trades, hit_rate, mean_return, median_return, '
    'stdev_return, sharpe_like, max_drawdown, final_equity_multiple, plus '
    'lookback_bars/bars_used/window_start/window_end/history_available_bars/'
    'truncated. source=play additionally returns play_id, play_title, '
    'synthesized_expression, conditions_used, conditions_skipped. source=scan '
    'additionally returns universe, verdict, direction, count, errors, and a '
    'results list (one entry per asset) instead of top-level metrics. Results '
    'cached 6 hours per unique parameter set.\n\n'
    'Workflow: call indicator_get_dsl_reference to learn the DSL → '
    'indicator_run_formula to confirm a condition fires today → backtest_run '
    'for historical edge measurement. To persist a result for later '
    'comparison, call backtest_save_run instead.',
    {
        'type': 'object',
        'properties': {
            'source': {
                'type': 'string',
                'enum': ['formula', 'play', 'scan'],
                'description': 'Which input to backtest (default: inferred — '
                               'see the tool description).',
            },
            'symbol': {
                'type': 'string',
                'description': 'Asset symbol, e.g. "BTC" (source=formula only; '
                               'source=play/scan derive the asset(s) themselves).',
            },
            'expression': {
                'type': 'string',
                'description': 'Boolean DSL formula. Entry fires on the '
                               'False→True transition (flip). Required for '
                               'source=formula and source=scan.',
            },
            'play_id': {
                'type': 'integer',
                'description': 'ID of the play to backtest (source=play, required). '
                               'The play must have a primary_asset and at least one '
                               'supported condition (indicator_value, indicator_crosses, '
                               'price_level, derivatives_metric, formula_expression).',
            },
            'verdict': {
                'type': 'string',
                'description': 'Comma-separated scanner verdict filter (source=scan '
                               'only; default STRONG,GOOD). Only used when universe='
                               '"scanner". Valid values include STRONG, GOOD, NEUTRAL, WEAK.',
            },
            'direction': {
                'type': 'string',
                'enum': ['long', 'short', 'both'],
                'description': 'Direction filter for scanner results (source=scan '
                               'only; default long)',
            },
            'universe': {
                'type': 'string',
                'enum': ['scanner', 'all'],
                'description': '(source=scan only) "scanner" (default): verdict-'
                               'filtered assets only. "all": full scanner universe, '
                               'no verdict filter.',
            },
            'max_assets': {
                'type': 'integer',
                'description': 'Max number of assets to backtest (source=scan only; '
                               'default 10, max 25). Higher values increase latency.',
            },
            'timeframe': {
                'type': 'string',
                'description': 'OHLCV timeframe (e.g. 1h, 4h, 1D). Default: 1D '
                               '(source=play defaults to the play\'s own timeframe).',
            },
            'lookback_bars': {
                'type': 'integer',
                'description': 'How many trailing bars to scan for entry signals '
                               '(default 365, max 2000, min 50).',
            },
            'hold_bars': {
                'type': 'integer',
                'description': 'Bars to hold after each entry before recording '
                               'the return (default 5, max 60).',
            },
        },
        'required': [],
    },
    min_tier='member',
    feature='backtester',
)
def backtest_run(arguments):
    source = arguments.get('source')
    if not source:
        if arguments.get('play_id'):
            source = 'play'
        elif not arguments.get('symbol'):
            source = 'scan'
        else:
            source = 'formula'

    if source == 'play':
        return backtest_play(arguments)
    if source == 'scan':
        return scan_and_backtest(arguments)
    if source == 'formula':
        from carrac.mcp.tools.indicators import backtest_indicator_formula
        return backtest_indicator_formula(arguments)
    raise ToolError(f'Invalid source: {source!r}. Use formula / play / scan.')


# ── backtest_simulate ─────────────────────────
#
# The first MCP-reachable Full Simulation backtest — separate entry/exit
# formulas, position sizing, commission, slippage, an intrabar stop/target,
# a max-hold-bars cap. Everything before this tool (backtest_indicator_formula,
# backtest_play, scan_and_backtest) runs the Hold-N model: fixed calendar
# exit, no stop, no target. This is a thin MCP-arguments -> engine-config
# mapping over carrac/backtest_sim.py:run_backtest — no new simulation logic,
# no abstraction layer.

_SIM_TTL_SEC = 6 * 3600  # 6h — same cadence as backtest_indicator_formula
_SIM_CACHE_KEY_PREFIX = 'mcd:mcp:bt:sim:'
_SIM_WARMUP_MARGIN_BARS = 200  # mirrors indicators.py's _BACKTEST_WARMUP_MARGIN_BARS
_SIM_MAX_TRADES_RETURNED = 500

# Bump whenever this tool's config-mapping or exit-synthesis logic changes.
# Folded into the cache key hash together with carrac.backtest_sim.ENGINE_VERSION
# so either a tool-level fix or an engine-level fix busts stale Redis entries
# automatically — a semantics fix that doesn't change
# the key is invisible.
# v2: added stop_atr_mult/target_r_mult to the cache key payload —
# same precedent, a new config dimension that doesn't change the key would let
# a cached pre-ATR-stop result silently answer an ATR-stop request.
_SIM_CACHE_VERSION = 2


def _sim_cache_key(**kwargs) -> str:
    from carrac.backtest_sim import ENGINE_VERSION
    payload = json.dumps(
        {**kwargs, 'v': _SIM_CACHE_VERSION, 'ev': ENGINE_VERSION},
        sort_keys=True, default=_json_default,
    )
    return _SIM_CACHE_KEY_PREFIX + hashlib.sha256(payload.encode()).hexdigest()


def _hold_bars_exit_expression(entry_expression: str, max_hold_bars: int) -> str:
    """Synthesize an exit condition for max_hold_bars.

    carrac/backtest_sim.py has no native hold-bars-cap config key — only a
    boolean exit *formula*. This builds one from a flip-to-true detector on
    the entry expression (the same False->True idiom backtest_indicator_formula
    uses for its Hold-N entries), NOT a naive BARSSINCE(entry_expression):
    a naive version resets to 0 on every bar a continuously-true entry
    condition (e.g. "close > ema(50)") stays true, so it would silently never
    fire — exactly the silent-substitution failure mode.
    The flip form only resets on an actual False->True transition, so a
    condition that stays true after opening a position doesn't restart the
    clock.

    Exact under the engine's default entryMode='next_open' (a position opens
    the bar AFTER the flip that triggered it): fires when
    barssince(flip) > max_hold_bars, closing the trade exactly
    max_hold_bars bars after it opened (trade['bars'] == max_hold_bars).

    Known limitation — verified by hand, not just theorized: this clause
    latches PERMANENTLY true once it first fires, if the entry expression
    never flips False->True again after the original flip (i.e. it just
    stays continuously true, like a trend filter "close > sma(200)", rather
    than an edge-triggered crossover/breakout that only fires once). If the
    engine then re-opens a position on a later bar (entry_expression is
    still true and no other exit closed it), that new position is force-
    closed on the very next bar (0 bars held), not given its own fresh
    max_hold_bars countdown — there is no position-state signal available to
    a formula (it's computed independent of the engine's stateful position
    tracking), so a hold-cap keyed off the entry expression alone cannot
    re-arm itself per trade. Edge-triggered entry expressions (the common
    case — crossovers, breakouts, one-off level touches) are unaffected:
    they only flip once, so there is only ever one trade to cap. Pair
    max_hold_bars with an exit_expression for continuously-true entry
    conditions if repeated re-entry after the cap fires is undesirable.
    """
    flip = f'(({entry_expression}) and not (({entry_expression})[1]))'
    return f'barssince({flip}) > {max_hold_bars}'


@mcp_tool(
    'backtest_simulate',
    'Run a Full Simulation backtest: separate entry/exit formulas, position '
    'sizing, commission, slippage, and a real stop-loss/take-profit/max-hold '
    'exit — the model the UI chart backtester runs (carrac/backtest_sim.py, '
    'a faithful Python port of static/js/backtester.js). This is the ONLY MCP '
    'tool that can express a stop, a target, or a capped holding period; '
    'backtest_indicator_formula/backtest_play/scan_and_backtest all run the '
    'cheaper Hold-N model (fixed calendar exit, no stop, no target) — never '
    'compare their numbers to this tool\'s.\n\n'
    'At least one exit path is required: exit_expression, stop_percent, '
    'stop_atr_mult, target_percent, or max_hold_bars (an entry with no exit '
    'is not a strategy). Supplying stop_percent/stop_atr_mult or '
    'target_percent/target_r_mult automatically switches the engine to '
    'intrabar stop/target checking — the raw engine defaults stop/target '
    'checking OFF (stopType=\'close\'), so this tool deliberately avoids the '
    'silent-no-op trap of accepting a stop that never actually gets checked. '
    'max_hold_bars is not a native engine parameter; it is synthesized into '
    'the exit formula via a flip-detector on entry_expression. Exact for '
    'edge-triggered entries (crossovers, breakouts — the common case, only '
    'ever one trade to cap). If entry_expression instead stays continuously '
    'true (e.g. a trend filter) after the cap first fires, any position the '
    'engine re-opens afterward is force-closed the very next bar instead of '
    'getting its own fresh countdown — pair max_hold_bars with an '
    'exit_expression in that case.\n\n'
    'Stop/target units: stop_percent/target_percent express distance as a '
    'fixed percent of entry price for the whole run. stop_atr_mult/'
    'target_r_mult instead express it as a multiple of ATR(14) '
    'at the entry bar — the Strategy Lab forward simulator\'s convention '
    '(volatility-normalised, so a stop means the same thing across a calm '
    'and a volatile regime, or across BTC and DXY). The percent pair and the '
    'ATR/R pair are MUTUALLY EXCLUSIVE per side — supplying stop_percent '
    'with stop_atr_mult, or target_percent with target_r_mult, is a clear '
    'error, never a silent pick of one. target_r_mult additionally REQUIRES '
    'a stop — stop_atr_mult or stop_percent — since an R-multiple has no '
    'meaning without a stop distance to measure R against. R is the initial '
    'risk distance, defined by the stop regardless of the unit it was '
    'expressed in, so 2R works with either; note that with a percent stop R '
    'is constant, making target_r_mult redundant with target_percent at '
    'twice the stop, whereas with an ATR stop R varies per trade.\n\n'
    'Exit precedence per bar: intrabar stop -> intrabar target -> exit '
    'formula/max_hold_bars -> end of data. The three modelling '
    'assumptions that move the result are now named parameters — '
    'same_bar_precedence (default stop_first: when one bar touches both the '
    'stop and the target, the stop is taken), entry_mode (default next_open: '
    'a flip on bar N fills at bar N+1\'s open, because a close cannot be acted '
    'on before it happens), and gap_aware_fills (default true: a gapped-through '
    'stop fills at the WORSE of its nominal price and the bar\'s open, a target '
    'at the BETTER). Every response carries an `assumptions` block stating '
    'which were active and which way each biases the numbers — READ IT and '
    'name any non-default when you report a result. Note the UI\'s JS engine '
    'has no gap adjustment, so UI and MCP results can legitimately diverge on '
    'any bar that gaps through a stop or target. With stop_atr_mult, an '
    'entry signal firing before ATR(14) has warmed up (the first 14 bars of '
    'the loaded window) is SKIPPED rather than opened with an undefined '
    'stop — the tool\'s warmup margin (200 bars ahead of lookback_bars) '
    'makes this a non-issue for normal windows.\n\n'
    'Returns: model (always "Full Simulation" — never compare to Hold-N or '
    'Historical Analog metrics), symbol, entry_expression, exit_expression '
    '(as supplied, or null), effective_exit_expression (what was actually '
    'run, including any synthesized max_hold_bars clause), direction, '
    'timeframe, stop_type ("intrabar" or "close"), stop_mode ("atr" | '
    '"percent" | null — which unit was actually active), target_mode ("r_multiple" '
    '| "percent" | null). lookback_bars is the REQUESTED value; bars_used/'
    'window_start/window_end report the window ACTUALLY measured, '
    'history_available_bars is the true stored depth for this symbol/'
    'timeframe, and truncated is true when lookback_bars asked for more '
    'than that — never silently substituted. trade_count is the true '
    'total; trades is capped at 500 entries (most recent) with a '
    'trades_note when capped — each trade includes stopPrice/tpPrice (the '
    'planned levels it was opened with, null if neither was configured). '
    'When stop_mode is "atr", realized_stop_distance reports what '
    'stop_atr_mult actually meant on THIS symbol over THIS window: '
    'atr_mult_requested plus median_percent/min_percent/max_percent/'
    'n_trades_with_stop computed from every trade\'s actual entry-to-stop '
    'distance — so an ATR multiple that silently produced a degenerate '
    'percent distance is visible, not hidden. stats mirrors the JS '
    'engine\'s computeStats: totalTrades, winRate, profitFactor, '
    'sharpeRatio, sortinoRatio, calmarRatio, ulcerIndex, '
    'maxDrawdown(Percent), avgWin/avgLoss/avgRR, totalReturn(Percent), '
    'avgBarsInTrade, maxConsecutiveWins/Losses. Results cached 6 hours per '
    'unique parameter set.',
    {
        'type': 'object',
        'properties': {
            'symbol': {'type': 'string', 'description': 'Asset symbol, e.g. "BTC"'},
            'entry_expression': {
                'type': 'string',
                'description': 'Boolean DSL formula. Entry fires on the False->True '
                               'transition (flip), filled at the NEXT bar\'s open.',
            },
            'exit_expression': {
                'type': 'string',
                'description': 'Optional boolean DSL formula checked every bar a '
                               'position is open; True closes it at that bar\'s close. '
                               'May be combined with stop_percent/target_percent/'
                               'max_hold_bars — exit fires on whichever happens first.',
            },
            'timeframe': {
                'type': 'string',
                'description': 'OHLCV timeframe (e.g. 1h, 4h, 1D). Default: 1D',
            },
            'lookback_bars': {
                'type': 'integer',
                'description': 'How many trailing bars to simulate over '
                               '(default 365, max 2000, min 50).',
            },
            'direction': {
                'type': 'string',
                'enum': ['long', 'short', 'both'],
                'description': '"both" reverses into the opposite side on each exit '
                               'signal instead of going flat (default: long).',
            },
            'stop_percent': {
                'type': 'number',
                'description': 'Stop-loss distance from entry, in percent (e.g. 5 = '
                               '5%). Enables intrabar stop checking automatically. '
                               'Omit to leave the stop disabled.',
            },
            'target_percent': {
                'type': 'number',
                'description': 'Take-profit distance from entry, in percent. Enables '
                               'intrabar target checking automatically. Omit to leave '
                               'the target disabled.',
            },
            'stop_atr_mult': {
                'type': 'number',
                'description': 'Stop-loss distance as a multiple of ATR(14) at the '
                               'entry bar (e.g. 1.5 = 1.5x ATR — the Strategy Lab\'s '
                               'convention). Volatility-normalised, unlike '
                               'stop_percent: the same multiple means a different '
                               'percent distance in a calm regime vs a volatile one, '
                               'which is the point. Mutually exclusive with '
                               'stop_percent — supplying both raises an error rather '
                               'than silently picking one. Enables intrabar stop '
                               'checking automatically, same as stop_percent. An '
                               'entry signal firing before ATR(14) has warmed up '
                               '(the first 14 bars of the loaded window) is SKIPPED, '
                               'not opened with an undefined stop — see '
                               'realized_stop_distance in the response for what this '
                               'multiple actually meant in percent terms here. Omit '
                               'to leave the ATR stop disabled.',
            },
            'target_r_mult': {
                'type': 'number',
                'description': 'Take-profit distance as a multiple of the ACTUAL '
                               'stop distance — an R-multiple (e.g. 2 = 2R, the '
                               'Strategy Lab\'s convention). REQUIRES a stop — '
                               'stop_atr_mult or stop_percent; there is no R without '
                               'a stop distance to measure it against, so supplying '
                               'target_r_mult alone raises an error. With an ATR stop '
                               'R varies per trade; with a percent stop it is constant '
                               '(and so equivalent to target_percent at twice the '
                               'stop). Mutually exclusive with target_percent. '
                               'Enables intrabar target checking automatically. Omit '
                               'to leave the R-multiple target disabled.',
            },
            'max_hold_bars': {
                'type': 'integer',
                'description': 'Force-close a position after this many bars if '
                               'nothing else has exited it yet. Exact for '
                               'edge-triggered entry_expression (crossovers, '
                               'breakouts); for a continuously-true entry_expression '
                               '(e.g. a trend filter), pair with exit_expression too — '
                               'see the tool description.',
            },
            'size_percent': {
                'type': 'number',
                'description': 'Position size as percent of equity per trade '
                               '(default 100).',
            },
            'commission_percent': {
                'type': 'number',
                'description': 'Commission per side, in percent — charged round-trip '
                               '(2x). Default 0.',
            },
            'slippage_bps': {
                'type': 'number',
                'description': 'Flat slippage against the trader, in basis points. '
                               'Default 0.',
            },
            'initial_capital': {
                'type': 'number',
                'description': 'Starting equity for the simulation. Default 10000.',
            },
            'gap_aware_fills': {
                'type': 'boolean',
                'description': 'Modelling assumption. Default true: a stop '
                               'gapped through fills at the WORSE of its price and the '
                               "bar's open, a target at the better — what would really "
                               'have happened. Set false to fill both at their nominal '
                               'price regardless of gaps, which is what the in-app '
                               'backtester does; use that only to reproduce a UI result. '
                               'False is optimistic on stops. Only applies when a stop or '
                               'target is set.',
            },
            'same_bar_precedence': {
                'type': 'string',
                'enum': ['stop_first', 'target_first'],
                'description': 'Modelling assumption. When one bar touches BOTH '
                               'the stop and the target, which is taken. Bar data cannot '
                               'tell us which came first, so this is a choice, not a '
                               'measurement. Default "stop_first" (pessimistic, matches the '
                               'in-app engine). "target_first" is the optimistic reading — '
                               'run both to see what the assumption is worth.',
            },
            'entry_mode': {
                'type': 'string',
                'enum': ['next_open', 'close'],
                'description': 'Modelling assumption. Default "next_open": a '
                               'signal on bar N fills at bar N+1\'s open, because you '
                               'cannot act on a close before it has happened. "close" '
                               'fills on the signal bar itself — that is LOOK-AHEAD BIAS '
                               'and will flatter the result; use it only for signal study, '
                               'and say so when reporting the numbers.',
            },
            'save_as': {
                'type': 'string',
                'description': 'Persist this run under this name so '
                               'it survives the session and appears in the user\'s '
                               'web app under Backtest drawer → History, alongside '
                               'runs they saved in the browser. The response gains '
                               'run_id; retrieve later with backtest_get_runs. '
                               'Requires the backtester_save entitlement. Omit to '
                               'run without persisting. Max 255 characters.',
            },
        },
        'required': ['symbol', 'entry_expression'],
    },
    min_tier='member',
    feature='backtester',
)
def backtest_simulate(arguments):
    from carrac.backtest_sim import run_backtest as run_sim
    from carrac.formula_eval import load_ohlcv, validate_formula, MAX_EXPRESSION_LENGTH
    import pandas as pd

    def _iso(dt64) -> str:
        return pd.Timestamp(dt64).strftime('%Y-%m-%dT%H:%M:%SZ')

    symbol = (arguments.get('symbol') or '').upper().strip()
    if not symbol:
        raise ToolError('symbol is required')

    entry_expression = (arguments.get('entry_expression') or '').strip()
    if not entry_expression:
        raise ToolError('entry_expression is required')
    if len(entry_expression) > MAX_EXPRESSION_LENGTH:
        raise ToolError(f'entry_expression too long (max {MAX_EXPRESSION_LENGTH})')
    valid = validate_formula(entry_expression)
    if not valid['valid']:
        raise ToolError(f'Invalid entry_expression: {valid["error"]}')

    exit_expression = (arguments.get('exit_expression') or '').strip()
    if exit_expression:
        if len(exit_expression) > MAX_EXPRESSION_LENGTH:
            raise ToolError(f'exit_expression too long (max {MAX_EXPRESSION_LENGTH})')
        valid = validate_formula(exit_expression)
        if not valid['valid']:
            raise ToolError(f'Invalid exit_expression: {valid["error"]}')

    # Stop/target/max_hold — validated eagerly and explicitly rejected when
    # invalid (rather than silently treated as absent) so a typo'd 0 or
    # negative value never gets quietly dropped alongside other exit paths.
    stop_percent = arguments.get('stop_percent')
    if stop_percent is not None:
        stop_percent = float(stop_percent)
        if stop_percent <= 0:
            raise ToolError('stop_percent must be > 0 (omit the field to disable the stop)')

    target_percent = arguments.get('target_percent')
    if target_percent is not None:
        target_percent = float(target_percent)
        if target_percent <= 0:
            raise ToolError('target_percent must be > 0 (omit the field to disable the target)')

    # ATR-multiple stop / R-multiple target — the volatility-
    # normalised counterpart to stop_percent/target_percent.
    stop_atr_mult = arguments.get('stop_atr_mult')
    if stop_atr_mult is not None:
        stop_atr_mult = float(stop_atr_mult)
        if stop_atr_mult <= 0:
            raise ToolError('stop_atr_mult must be > 0 (omit the field to disable the ATR stop)')

    target_r_mult = arguments.get('target_r_mult')
    if target_r_mult is not None:
        target_r_mult = float(target_r_mult)
        if target_r_mult <= 0:
            raise ToolError('target_r_mult must be > 0 (omit the field to disable the R-multiple target)')

    # Percent and ATR/R units are mutually exclusive per side — reject at the
    # tool layer too (the engine also rejects this, but a tool-layer error
    # message is more actionable than a bubbled-up engine error string).
    if stop_percent is not None and stop_atr_mult is not None:
        raise ToolError(
            'stop_percent and stop_atr_mult are mutually exclusive — choose one stop unit '
            '(a fixed percent, or a multiple of ATR(14) at the entry bar).'
        )
    if target_percent is not None and target_r_mult is not None:
        raise ToolError(
            'target_percent and target_r_mult are mutually exclusive — choose one target unit '
            '(a fixed percent, or an R-multiple of the actual stop distance).'
        )
    if target_r_mult is not None and stop_atr_mult is None and stop_percent is None:
        raise ToolError(
            'target_r_mult requires a stop — supply stop_atr_mult or stop_percent. An '
            'R-multiple target has no stop distance to measure R against.'
        )

    max_hold_bars = arguments.get('max_hold_bars')
    if max_hold_bars is not None:
        max_hold_bars = int(max_hold_bars)
        if max_hold_bars <= 0:
            raise ToolError('max_hold_bars must be a positive integer')
        max_hold_bars = min(max_hold_bars, 5000)

    if (not exit_expression and stop_percent is None and target_percent is None
            and stop_atr_mult is None and max_hold_bars is None):
        raise ToolError(
            'At least one exit path is required: exit_expression, stop_percent, '
            'stop_atr_mult, target_percent, or max_hold_bars. An entry with no exit '
            'is not a strategy.'
        )

    timeframe = (arguments.get('timeframe') or '1D').strip()
    lookback_bars = int(arguments.get('lookback_bars') or 365)
    lookback_bars = max(50, min(2000, lookback_bars))

    from carrac.mcp.tools.indicators import _require_supported_timeframe
    _require_supported_timeframe(timeframe)

    direction = (arguments.get('direction') or 'long').strip().lower()
    if direction not in ('long', 'short', 'both'):
        raise ToolError(f"Invalid direction '{direction}'; must be long, short, or both")

    size_percent = arguments.get('size_percent')
    size_percent = 100.0 if size_percent is None else max(1.0, min(1000.0, float(size_percent)))

    commission_percent = arguments.get('commission_percent')
    commission_percent = 0.0 if commission_percent is None else max(0.0, min(5.0, float(commission_percent)))

    slippage_bps = arguments.get('slippage_bps')
    slippage_bps = 0.0 if slippage_bps is None else max(0.0, min(1000.0, float(slippage_bps)))

    initial_capital = arguments.get('initial_capital')
    initial_capital = 10000.0 if not initial_capital or float(initial_capital) <= 0 else float(initial_capital)

    # stop/target checking is OFF in the raw engine unless
    # stopType='intrabar' is set explicitly. A caller who supplied
    # stop_percent/target_percent (or stop_atr_mult/
    # target_r_mult) clearly wants them enforced — silently accepting and
    # ignoring them would be exactly the accept-and-ignore bug class,
    # so this tool switches automatically.
    stop_type = 'intrabar' if (
        stop_percent is not None or target_percent is not None
        or stop_atr_mult is not None or target_r_mult is not None
    ) else 'close'
    stop_mode = 'atr' if stop_atr_mult is not None else ('percent' if stop_percent is not None else None)
    target_mode = 'r_multiple' if target_r_mult is not None else ('percent' if target_percent is not None else None)

    # ── modelling assumptions ────────────────────────────────
    # Three choices materially move the result and must be visible to the caller.
    # Defaults are the realistic/pessimistic reading in every case; a caller
    # who wants the flattering one now has to ask for it by name, and the
    # response says which was used either way.
    gap_aware_fills = arguments.get('gap_aware_fills')
    gap_aware_fills = True if gap_aware_fills is None else bool(gap_aware_fills)

    same_bar_precedence = (arguments.get('same_bar_precedence') or 'stop_first').strip()
    if same_bar_precedence not in ('stop_first', 'target_first'):
        raise ToolError("same_bar_precedence must be 'stop_first' or 'target_first'")

    entry_mode = (arguments.get('entry_mode') or 'next_open').strip()
    if entry_mode not in ('next_open', 'close'):
        raise ToolError("entry_mode must be 'next_open' or 'close'")

    # max_hold_bars has no native engine parameter — synthesize it into the
    # exit formula (see _hold_bars_exit_expression's docstring).
    exit_parts = []
    if exit_expression:
        exit_parts.append(f'({exit_expression})')
    if max_hold_bars is not None:
        exit_parts.append(f'({_hold_bars_exit_expression(entry_expression, max_hold_bars)})')
    # No exit_expression/max_hold_bars at all -> stop/target alone (or end-of-data)
    # decide the exit. '0' is a constant-false formula, never fires.
    effective_exit_expression = ' or '.join(exit_parts) if exit_parts else '0'

    # ── Cache check ──────────────────────────────────────────────────
    cache_key = _sim_cache_key(
        symbol=symbol, entry=entry_expression, exit=exit_expression, timeframe=timeframe,
        lookback_bars=lookback_bars, direction=direction,
        stop_percent=stop_percent, target_percent=target_percent, max_hold_bars=max_hold_bars,
        stop_atr_mult=stop_atr_mult, target_r_mult=target_r_mult,
        size_percent=size_percent, commission_percent=commission_percent,
        slippage_bps=slippage_bps, initial_capital=initial_capital,
        # These MUST be in the key. Every one of them changes the
        # result, so omitting them would serve the cached default back to a
        # caller who explicitly asked for a different assumption — the tool
        # would accept the parameter, ignore it, and succeed. That is the
        # silent-substitution class these flags exist to eliminate, and it is
        # exactly what happened on the first run of this feature.
        gap_aware_fills=gap_aware_fills, same_bar_precedence=same_bar_precedence,
        entry_mode=entry_mode,
    )
    r = None
    try:
        from carrac.redis_cache import get_redis
        r = get_redis()
        if r:
            cached = r.get(cache_key)
            if cached:
                return json.loads(cached) | {'_cached': True}
    except Exception as e:
        logger.debug(f'Cache lookup failed: {e}')

    # ── Load OHLCV — requested_bars, same pattern as
    # backtest_indicator_formula: ask for lookback_bars plus warmup margin so
    # indicators with a warmup period have room to stabilize before the
    # simulated window starts. ─────────────────────────────────────────
    ohlcv = load_ohlcv(symbol, timeframe,
                        requested_bars=lookback_bars + _SIM_WARMUP_MARGIN_BARS)
    if ohlcv is None:
        raise ToolError(f'No OHLCV data for {symbol}/{timeframe}')

    n = len(ohlcv['close'])
    if n < 20:
        raise ToolError(f'Not enough data: have {n} bars, need at least 20')

    history_available_bars = ohlcv.get('_total_available', n)
    truncated = lookback_bars > history_available_bars

    start_idx = max(0, n - lookback_bars)
    bars_used = n - start_idx
    window_start = _iso(ohlcv['_dates'][start_idx])
    window_end = _iso(ohlcv['_dates'][n - 1])

    config = {
        'entry': entry_expression,
        'exit': effective_exit_expression,
        'direction': direction,
        'sizePercent': size_percent,
        'commissionPercent': commission_percent,
        'slippageBps': slippage_bps,
        'initialCapital': initial_capital,
        'startBar': start_idx,
        'endBar': n - 1,
        'stopType': stop_type,
        'stopLossPercent': stop_percent or 0,
        'takeProfitPercent': target_percent or 0,
        'stopAtrMult': stop_atr_mult or 0,
        'targetRMult': target_r_mult or 0,
        # The three modelling assumptions, now settable rather than
        # buried. Defaults are unchanged, so an existing caller's numbers do
        # not move — but they are named, and the response reports them.
        'gapAwareFills': gap_aware_fills,
        'sameBarPrecedence': same_bar_precedence,
        'entryMode': entry_mode,
    }
    sim = run_sim(config, ohlcv)
    if sim.get('error'):
        raise ToolError(sim['error'])

    trades = sim['trades']
    trades_truncated = len(trades) > _SIM_MAX_TRADES_RETURNED
    trades_out = trades[-_SIM_MAX_TRADES_RETURNED:] if trades_truncated else trades

    # Report what stop_atr_mult ACTUALLY meant on this symbol
    # over this window, in percent terms — turns the "1.5x ATR" request into
    # a visible, checkable number instead of a black box. Uses the full
    # trade list (not the 500-capped trades_out) so the stat isn't skewed by
    # truncation.
    realized_stop_distance = None
    if stop_mode == 'atr':
        stop_dist_pcts = [
            abs(t['entryPrice'] - t['stopPrice']) / t['entryPrice'] * 100
            for t in trades
            if t.get('stopPrice') is not None and t.get('entryPrice')
        ]
        if stop_dist_pcts:
            stop_dist_pcts.sort()
            n_d = len(stop_dist_pcts)
            median_pct = (stop_dist_pcts[n_d // 2] if n_d % 2 == 1
                          else (stop_dist_pcts[n_d // 2 - 1] + stop_dist_pcts[n_d // 2]) / 2)
            realized_stop_distance = {
                'atr_mult_requested': stop_atr_mult,
                'median_percent': median_pct,
                'min_percent': stop_dist_pcts[0],
                'max_percent': stop_dist_pcts[-1],
                'n_trades_with_stop': n_d,
            }
        else:
            realized_stop_distance = {
                'atr_mult_requested': stop_atr_mult,
                'median_percent': None,
                'min_percent': None,
                'max_percent': None,
                'n_trades_with_stop': 0,
            }

    out = {
        'model': MODEL_FULL_SIMULATION,
        'symbol': symbol,
        'entry_expression': entry_expression,
        'exit_expression': exit_expression or None,
        'effective_exit_expression': effective_exit_expression,
        'direction': direction,
        'timeframe': timeframe,
        'stop_type': stop_type,
        'stop_mode': stop_mode,
        'target_mode': target_mode,
        'lookback_bars': lookback_bars,
        'bars_used': bars_used,
        'window_start': window_start,
        'window_end': window_end,
        'history_available_bars': history_available_bars,
        'truncated': truncated,
        'stop_percent': stop_percent,
        'target_percent': target_percent,
        'stop_atr_mult': stop_atr_mult,
        'target_r_mult': target_r_mult,
        'realized_stop_distance': realized_stop_distance,
        'max_hold_bars': max_hold_bars,
        'size_percent': size_percent,
        'commission_percent': commission_percent,
        'slippage_bps': slippage_bps,
        'initial_capital': initial_capital,
        'trade_count': len(trades),
        'trades': trades_out,
        'stats': sim['stats'],
        # Every result states the assumptions that produced it and
        # which way each one bends the number. An assumption that flatters the
        # result while staying invisible is precisely what this product claims
        # not to do.
        'assumptions': _assumptions_block(
            gap_aware_fills, same_bar_precedence, entry_mode, stop_type,
        ),
    }
    if trades_truncated:
        out['trades_note'] = f'Showing the most recent {_SIM_MAX_TRADES_RETURNED} of {len(trades)} trades.'

    try:
        if r:
            r.setex(cache_key, _SIM_TTL_SEC, json.dumps(out, default=_json_default))
    except Exception:
        pass

    # Full Simulation has no other persistence path — only a
    # 6h Redis cache — so without this a caller could not keep the one model
    # the web UI is shaped around. Saved here rather than to `strategies`, which is
    # a configuration table: this is a result, and results belong in
    # backtest_runs alongside the Hold-N ones, discriminated by `model`.
    save_as = (arguments.get('save_as') or '').strip()
    if save_as:
        out['run_id'] = _persist_simulation(save_as, out)

    return out


def _assumptions_block(gap_aware_fills, same_bar_precedence, entry_mode, stop_type):
    """Name each modelling assumption and its direction of bias.

    `bias` says which way the reported numbers move relative to the alternative,
    so a reader can tell an optimistic setup from a conservative one without
    knowing the engine. `default` flags whether the caller changed it.
    """
    intrabar = stop_type == 'intrabar'
    return {
        'note': 'These are modelling choices, not measurements. Each one moves '
                'the result; the defaults are the conservative reading.',
        'entry_fill': {
            'value': entry_mode,
            'default': entry_mode == 'next_open',
            'means': ("Fills on the next bar's open — you cannot act on a close "
                      'before it has happened.') if entry_mode == 'next_open' else
                     ('Fills on the close of the bar that produced the signal.'),
            'bias': 'neutral — this is the realistic reading' if entry_mode == 'next_open'
                    else 'OPTIMISTIC — look-ahead: it trades on a price it could not have known',
        },
        'same_bar_stop_and_target': {
            'value': same_bar_precedence,
            'default': same_bar_precedence == 'stop_first',
            'applies': intrabar,
            'means': 'When one bar touches both, the '
                     + ('stop' if same_bar_precedence == 'stop_first' else 'target')
                     + ' is taken. Bar data cannot tell us which came first.',
            'bias': 'conservative' if same_bar_precedence == 'stop_first' else 'OPTIMISTIC',
        },
        'gap_aware_fills': {
            'value': gap_aware_fills,
            'default': gap_aware_fills is True,
            'applies': intrabar,
            'means': ('A stop gapped through fills at the worse of its price and the '
                      "bar's open; a target at the better.") if gap_aware_fills else
                     ('Stops and targets always fill at their nominal price, even if the '
                      'market gapped past them. Matches the in-app backtester.'),
            'bias': 'realistic' if gap_aware_fills
                    else 'OPTIMISTIC on stops — assumes fills at prices the market never traded',
        },
    }


def _persist_simulation(name, out):
    """Store a completed Full-Sim run. Returns the run_id, or None if refused.

    Unlike backtest_save_run, this persists exactly the result the
    caller is being handed — the run is not repeated, so what is saved is what
    was returned.
    """
    from carrac.auth.decorators import get_tier_limits
    from carrac.backtests.models import save_run
    from carrac.mcp.backtest_models import MODEL_FULL_SIMULATION
    from carrac.mcp.runtime import get_mcp_caller_tier
    from carrac.mcp.tools import get_mcp_user_id

    if not get_tier_limits(get_mcp_caller_tier()).get('backtester_save'):
        out['save_skipped'] = (
            'Saving backtest runs needs the saved-runs entitlement on this '
            'plan. The run above is complete but was not persisted.'
        )
        return None

    stats = out.get('stats') or {}
    config = {k: out.get(k) for k in (
        'entry_expression', 'exit_expression', 'effective_exit_expression',
        'direction', 'timeframe', 'stop_type', 'stop_mode', 'target_mode',
        'stop_percent', 'target_percent', 'stop_atr_mult', 'target_r_mult',
        'max_hold_bars', 'size_percent', 'commission_percent', 'slippage_bps',
        'initial_capital', 'lookback_bars', 'bars_used',
        'window_start', 'window_end',
    )}
    # The list view lifts these out of `result` for the summary row.
    result = dict(stats)
    result['trades'] = out.get('trade_count')

    try:
        return save_run(
            get_mcp_user_id(),
            symbol=out['symbol'],
            expression=out['entry_expression'],
            timeframe=out.get('timeframe') or '1D',
            lookback_bars=out.get('lookback_bars') or 0,
            hold_bars=None,  # meaningless for Full Simulation
            result=result,
            name=name[:255],
            model=MODEL_FULL_SIMULATION,
            config=config,
        )
    except Exception as e:  # a failed save must not lose the user's result
        logger.warning(f'backtest_simulate save_as failed: {e}')
        out['save_skipped'] = f'Could not save this run: {e}'
        return None
