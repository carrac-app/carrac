"""MCP tools: Backtest run history.

Saves, lists, and retrieves persisted backtest runs for the calling user, and exposes the saved-strategy library the web UI writes to.

All tools are gated on the `backtester_save` TIER_LIMITS knob
(member/tester/premium/admin) — matching the Flask
/api/strategies save/load gate in carrac/dashboard/routes.py, which uses
the same knob.

The SQL lives in carrac/backtests/models.py,
which owns BOTH backtest tables so the web UI and MCP see the same data — the
same arrangement carrac/plays/models.py has for plays. These functions are the
MCP-shaped wrapper over that layer: argument validation, tier gating, and the
response envelope.

Tools:
  - backtest_save_run       — run backtest and persist the result
  - backtest_get_runs       — list saved runs, or retrieve one full run by run_id
                              (run_id present selects the single-run helper,
                              absent the list helper)
  - backtest_manage_strategies — list/save/delete the saved-strategy library
                              shared with the browser's Backtest drawer
"""

import logging

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.tools import get_mcp_user_id
from carrac.mcp.backtest_models import MODEL_HOLD_N

logger = logging.getLogger('carrac.mcp.tools.backtests')


# ── Internal backtest runner — module-level for monkeypatching in tests ───────

def _run_backtest(arguments):
    """Thin proxy to backtest_indicator_formula.

    Defined at module level so tests can monkeypatch this name without
    needing access to the lazy-imported symbol inside backtest_save_run.
    """
    from carrac.mcp.tools.indicators import backtest_indicator_formula
    return backtest_indicator_formula(arguments)


# ── SQLite shim — kept as an alias; the table is owned by backtests.models ────

def _ensure_table(conn):
    """Deprecated alias for carrac.backtests.models.ensure_runs_table.

    Retained because tests and older call sites import this name.
    """
    from carrac.backtests.models import ensure_runs_table
    return ensure_runs_table(conn)


# ── backtest_save_run ──────────────────────────────────────────

@mcp_tool(
    'backtest_save_run',
    'Persist a Hold-N flip-signal backtest so it survives the session and '
    'appears in the user\'s web app under Backtest drawer → History.\n\n'
    'PASS THE `result` YOU ALREADY HAVE. If you ran backtest_run and showed the '
    'user those numbers, hand that same result object back here and it is stored '
    'verbatim. Omit it and this tool RE-RUNS the backtest and saves that instead '
    '— which can differ from what the user approved once the 6h result cache has '
    'expired and fresh bars have arrived. The response tells you '
    'which happened via `result_source`.\n\n'
    'Returns: model (always "Hold-N Signal Test"), run_id (use with '
    'backtest_get_runs), name, symbol, result_source ("caller" | "recomputed"), '
    'and the full Hold-N result object (trades, hit_rate, mean_return, '
    'median_return, stdev_return, sharpe_like, max_drawdown, '
    'final_equity_multiple). '
    'Raises an error if the backtest itself fails (e.g. bad expression, no data).\n\n'
    'When to call: after validating a formula with backtest_run and '
    'wanting to track or compare multiple parameter variations over time. '
    'Use backtest_get_runs (without run_id) to review saved runs; '
    'backtest_get_runs (with run_id) to fetch one.',
    {
        'type': 'object',
        'properties': {
            'symbol': {
                'type': 'string',
                'description': 'Asset symbol, e.g. "BTC"',
            },
            'expression': {
                'type': 'string',
                'description': 'Boolean DSL formula to backtest. Entry fires on '
                               'False→True flip (Hold-N model).',
            },
            'name': {
                'type': 'string',
                'description': 'Optional human-readable label for this run (max 255 chars), '
                               'e.g. "RSI oversold + EMA stack, BTC 1D, 2024 lookback".',
            },
            'timeframe': {
                'type': 'string',
                'description': 'OHLCV timeframe (default: 1D)',
            },
            'lookback_bars': {
                'type': 'integer',
                'description': 'Bars to look back (default: 365, max: 2000, min: 50)',
            },
            'hold_bars': {
                'type': 'integer',
                'description': 'Bars to hold after each entry (default: 5, max: 60)',
            },
            'result': {
                'type': 'object',
                'description': 'The result object from your earlier '
                               'backtest_run call. Supply it to store exactly what '
                               'the user reviewed. Omit it and the backtest is '
                               're-run and that output saved instead, which may '
                               'not be the same numbers.',
            },
        },
        'required': ['symbol', 'expression'],
    },
    min_tier='member',
    feature='backtester_save',
)
def backtest_save_run(arguments):
    from carrac.backtests.models import save_run

    symbol = (arguments.get('symbol') or '').upper().strip()
    expression = (arguments.get('expression') or '').strip()
    name = (arguments.get('name') or '').strip() or None
    timeframe = (arguments.get('timeframe') or '1D').strip()
    lookback_bars = int(arguments.get('lookback_bars') or 365)
    hold_bars = int(arguments.get('hold_bars') or 5)

    if not symbol:
        raise ToolError('symbol is required')
    if not expression:
        raise ToolError('expression is required')

    # Clamp to match backtest_indicator_formula validation
    lookback_bars = max(50, min(2000, lookback_bars))
    hold_bars = max(1, min(60, hold_bars))

    # Persist the result the caller actually reviewed, when they
    # supply it. Always re-running the backtest would mean an agent that
    # ran backtest_run, showed the user the numbers, got approval, then saved,
    # stored a second independent execution. A 6h result cache usually hides
    # that; past that window the save pulls fresh bars and can produce different
    # numbers than the ones that were approved. A "saved run" that can silently
    # differ from the reviewed run is the wrong default for a product whose
    # pitch is honest measurement.
    supplied = arguments.get('result')
    if supplied is not None:
        if not isinstance(supplied, dict):
            raise ToolError('result must be the object returned by backtest_run')
        if supplied.get('error'):
            raise ToolError(f'Refusing to save a failed backtest: {supplied["error"]}')
        bt_result = supplied
        result_source = 'caller'
    else:
        # Imported at call time (not module level) to avoid circular imports;
        # the module-level name _run_backtest is patched in tests.
        bt_result = _run_backtest({
            'symbol': symbol,
            'expression': expression,
            'timeframe': timeframe,
            'lookback_bars': lookback_bars,
            'hold_bars': hold_bars,
        })
        if bt_result.get('error'):
            raise ToolError(f'Backtest failed: {bt_result["error"]}')
        result_source = 'recomputed'

    user_id = get_mcp_user_id()

    run_id = save_run(
        user_id,
        symbol=symbol,
        expression=expression,
        timeframe=timeframe,
        lookback_bars=lookback_bars,
        hold_bars=hold_bars,
        result=bt_result,
        name=name,
        model=MODEL_HOLD_N,
        # Stamp the provenance onto the row itself, so a saved run
        # can always answer "is this what was reviewed?" — not just today, in a
        # year, when nobody remembers how it got there.
        config={'result_source': result_source},
    )

    return {
        'model': MODEL_HOLD_N,
        'run_id': run_id,
        'name': name,
        'symbol': symbol,
        'result': bt_result,
        'result_source': result_source,
        'result_source_note': (
            'Saved exactly what you passed in.' if result_source == 'caller' else
            'No result was supplied, so the backtest was re-run and THAT output '
            'was saved — it may differ from any earlier run you showed the user. '
            'Pass the result from backtest_run to save what was actually reviewed.'
        ),
        # The human can now find this in the browser.
        'visible_in_app': 'Backtest drawer → History',
    }


# ── list_backtest_runs ─────────────────────────────────────────
#
# No longer a standalone MCP tool — absorbed into
# backtest_get_runs (called when run_id is absent). Kept as a plain helper.

def list_backtest_runs(arguments):
    from carrac.backtests.models import list_runs

    runs = list_runs(
        get_mcp_user_id(),
        symbol=(arguments.get('symbol') or '').upper().strip() or None,
        limit=arguments.get('limit') or 20,
        model=arguments.get('model') or None,
    )
    # Runs are no longer all Hold-N, so `model` moved onto each row.
    # `models` names the distinct models present so a caller cannot skim the
    # list and treat a Full-Sim win rate and a Hold-N hit rate as one column.
    return {
        'runs': runs,
        'count': len(runs),
        'models': sorted({r['model'] for r in runs}),
    }


# ── get_backtest_run ───────────────────────────────────────────
#
# No longer a standalone MCP tool — absorbed into
# backtest_get_runs (called when run_id is present). Kept as a plain helper.

def get_backtest_run(arguments):
    from carrac.backtests.models import get_run

    run_id = arguments.get('run_id')
    if run_id is None:
        raise ToolError('run_id is required')

    run = get_run(get_mcp_user_id(), int(run_id))
    if run is None:
        raise ToolError(f'Backtest run {run_id} not found')
    return run


# ── backtest_get_runs ──────────────────────────────

@mcp_tool(
    'backtest_get_runs',
    'List the calling user\'s saved backtest runs, or retrieve one in '
    'full — pass run_id to get a single complete run; omit it to list.\n\n'
    'These are the SAME runs the user sees in the web app under Backtest '
    'drawer → History. A run you save with backtest_save_run appears there, '
    'and a run the user saved in the browser appears here.\n\n'
    'Without run_id: a lightweight summary per run — run_id, name, model, '
    'symbol, expression, timeframe, lookback_bars, hold_bars, created_at — '
    'plus that model\'s headline numbers (Hold-N: hit_rate, mean_return, '
    'trades. Full Simulation: winRate, totalReturnPercent, trades, '
    'maxDrawdownPercent, sharpeRatio). The response also carries `models`, '
    'the distinct models present. CRITICAL: each row states its own model and '
    'the two are NOT comparable — never rank a Hold-N hit_rate against a '
    'Full-Sim win_rate. Optional symbol/model filters, limit (default 20, '
    'max 100).\n\n'
    'With run_id: the complete result object plus run metadata and the config '
    'that produced it. Errors if run_id does not exist or belongs to a '
    'different user.\n\n'
    'Workflow: call without run_id to browse and pick one, then call again '
    'with that run_id for full detail.\n\n'
    'For the user\'s saved strategy CONFIGURATIONS (recipes to re-run, as '
    'opposed to past results) use backtest_manage_strategies instead.',
    {
        'type': 'object',
        'properties': {
            'run_id': {
                'type': 'integer',
                'description': 'Retrieve one full run by id (returned by '
                               'backtest_save_run or a prior no-run_id call). '
                               'Omit to list instead.',
            },
            'symbol': {
                'type': 'string',
                'description': 'Optional (list only): filter to runs for this '
                               'asset symbol (case-insensitive, e.g. "BTC").',
            },
            'model': {
                'type': 'string',
                'enum': ['Hold-N Signal Test', 'Full Simulation'],
                'description': 'Optional (list only): return only runs from '
                               'this backtest model.',
            },
            'limit': {
                'type': 'integer',
                'description': 'Optional (list only): max rows to return '
                               '(default: 20, max: 100)',
            },
        },
    },
    min_tier='member',
    feature='backtester_save',
)
def backtest_get_runs(arguments):
    if arguments.get('run_id') is not None:
        return get_backtest_run(arguments)
    return list_backtest_runs(arguments)


# ── backtest_manage_strategies ─────────────────────
#
# The saved-strategy library is shared: an agent can read what its human
# saved, and the human can see what the agent saved. Same table as the
# Backtest drawer's Save/Load — carrac/backtests/models.py
# owns it, this is the MCP door onto it.

@mcp_tool(
    'backtest_manage_strategies',
    'Read, save and delete the user\'s saved backtest STRATEGIES — the same '
    'library the web app\'s Backtest drawer Save/Load list shows. A strategy '
    'is a reusable recipe (entry formula, exit formula, direction, capital, '
    'size, commission), NOT a past result — for results use backtest_get_runs.\n\n'
    'action="list" (default): every saved strategy with its id, name, formulas '
    'and parameters. Use this to pick up work the user set up in the browser '
    'instead of asking them to retype it.\n'
    'action="save": create or update. Requires name, entry_formula, '
    'exit_formula. Pass id to update an existing one; omit it to create. '
    'Returns the strategy id. What you save here appears in the user\'s '
    'browser immediately.\n'
    'action="delete": requires id.\n\n'
    'A saved strategy carries a `params` object with the rest of the setup — '
    'timeframe, date range, regime filter, instrument, slippage, stop type, '
    'stop/target percentages and entry-fill mode. Use those when '
    're-running it, or you will not reproduce what the user saw. `params` is '
    'null on strategies saved before this existed: in that case only the '
    'formulas and costs were kept, and you should say so rather than assume '
    'defaults were what the user intended.\n\n'
    'To actually RUN one, feed entry_formula/exit_formula into '
    'backtest_simulate (Full Simulation) or backtest_run (Hold-N).',
    {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['list', 'save', 'delete'],
                'description': 'Defaults to "list".',
            },
            'id': {
                'type': 'string',
                'description': 'Strategy id — required for delete, optional '
                               'for save (present = update, absent = create).',
            },
            'name': {
                'type': 'string',
                'description': 'save only: 1-50 characters.',
            },
            'entry_formula': {
                'type': 'string',
                'description': 'save only: boolean expression, when to open. '
                               'Max 2000 characters.',
            },
            'exit_formula': {
                'type': 'string',
                'description': 'save only: boolean expression, when to close. '
                               'Max 2000 characters.',
            },
            'direction': {
                'type': 'string',
                'enum': ['long', 'short', 'both'],
                'description': 'save only: defaults to "long".',
            },
            'capital': {
                'type': 'number',
                'description': 'save only: starting capital, defaults to 10000.',
            },
            'size_percent': {
                'type': 'number',
                'description': 'save only: position size %, defaults to 100.',
            },
            'commission_percent': {
                'type': 'number',
                'description': 'save only: per-side commission %, defaults to 0.1.',
            },
            'timeframe': {'type': 'string', 'description': 'save only: e.g. "1D", "4h".'},
            'start_date': {'type': 'string', 'description': 'save only: YYYY-MM-DD.'},
            'end_date': {'type': 'string', 'description': 'save only: YYYY-MM-DD.'},
            'regime': {
                'type': 'string', 'enum': ['all', 'favorable', 'unfavorable'],
                'description': 'save only: regime filter this strategy is meant for.',
            },
            'instrument': {
                'type': 'string', 'enum': ['spot', 'perp'],
                'description': 'save only: perp applies funding costs.',
            },
            'slippage_bps': {'type': 'number', 'description': 'save only: basis points.'},
            'stop_type': {
                'type': 'string', 'enum': ['close', 'intrabar'],
                'description': 'save only: a stop/target set while this is "close" '
                               'has no effect — store "intrabar" if one is set.',
            },
            'stop_loss_percent': {'type': 'number', 'description': 'save only: 0 = none.'},
            'take_profit_percent': {'type': 'number', 'description': 'save only: 0 = none.'},
            'entry_mode': {
                'type': 'string', 'enum': ['next_open', 'close'],
                'description': 'save only: "close" is look-ahead.',
            },
        },
    },
    min_tier='member',
    feature='backtester_save',
)
def backtest_manage_strategies(arguments):
    from carrac.backtests.models import (
        list_strategies, save_strategy, delete_strategy, count_strategies,
    )
    from carrac.auth.decorators import get_tier_limits
    from carrac.mcp.runtime import get_mcp_caller_tier

    action = (arguments.get('action') or 'list').lower().strip()
    user_id = get_mcp_user_id()

    if action == 'list':
        strategies = list_strategies(user_id)
        return {
            'strategies': strategies,
            'count': len(strategies),
            'note': 'Recipes, not results. Each carries a `params` object with '
                    'the full setup; `params: null` means it was saved before '
                    'that was kept, so only the formulas and costs are real.',
        }

    if action == 'delete':
        sid = (arguments.get('id') or '').strip()
        if not sid:
            raise ToolError('id is required for action="delete"')
        if not delete_strategy(user_id, sid):
            raise ToolError(f'Strategy {sid} not found')
        return {'deleted': sid}

    if action != 'save':
        raise ToolError(f'Unknown action {action!r} — use list, save or delete')

    name = (arguments.get('name') or '').strip()
    entry = (arguments.get('entry_formula') or '').strip()
    exit_f = (arguments.get('exit_formula') or '').strip()
    if not name or len(name) > 50:
        raise ToolError('name must be 1-50 characters')
    if not entry or not exit_f:
        raise ToolError('entry_formula and exit_formula are both required')
    if len(entry) > 2000 or len(exit_f) > 2000:
        raise ToolError('formulas are limited to 2000 characters')

    sid = (arguments.get('id') or '').strip() or None
    if sid is None:
        # Same tier ceiling the Flask route enforces (carrac/dashboard/routes.py).
        limits = get_tier_limits(get_mcp_caller_tier())
        max_strats = limits.get('max_custom_formulas', 3)
        current = count_strategies(user_id)
        if current >= max_strats:
            raise ToolError(
                f'Strategy limit reached ({current}/{max_strats}). '
                f'Delete one or upgrade for more.'
            )

    # Same whitelist the Flask route applies.
    _PARAM_KEYS = (
        'timeframe', 'start_date', 'end_date', 'regime', 'instrument',
        'slippage_bps', 'stop_type', 'stop_loss_percent', 'take_profit_percent',
        'entry_mode',
    )
    params = {k: arguments[k] for k in _PARAM_KEYS if arguments.get(k) not in (None, '')}

    payload = {
        'name': name,
        'entry_formula': entry,
        'exit_formula': exit_f,
        'direction': arguments.get('direction') or 'long',
        'capital': arguments.get('capital', 10000),
        'size_percent': arguments.get('size_percent', 100),
        'commission_percent': arguments.get('commission_percent', 0.1),
        'params': params or None,
    }
    if sid:
        payload['id'] = sid

    new_id = save_strategy(user_id, payload)
    if not new_id:
        raise ToolError('Failed to save strategy')
    return {
        'id': new_id,
        'name': name,
        'updated': bool(sid),
        'visible_in_app': 'Backtest drawer → Load',
    }
