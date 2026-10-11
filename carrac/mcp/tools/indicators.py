"""MCP tools: Indicators & Formulas.

Exposes the formula engine and indicator registry to MCP clients so agents
can run one-off formulas, discover indicators, and backtest simple signal
expressions without going through the chart UI.

Tools:
  - indicator_run_formula
  - indicator_list
  - indicator_get_definition
  - indicator_get_dsl_reference

backtest_indicator_formula is a plain helper here, no longer a
standalone MCP tool — absorbed into backtest_run (source=formula) in
tools/backtest.py.
"""

import hashlib
import json
import logging

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.backtest_models import MODEL_HOLD_N

logger = logging.getLogger('carrac.mcp.tools.indicators')


def _require_supported_timeframe(timeframe):
    """Reject a timeframe the OHLCV layer cannot actually serve.

    Without this, an unsupported timeframe (`15m`, `5m`, or a
    typo) would fall through to daily bars and return a plausible-looking result
    an agent had no way to tell apart from a real one. Fail loudly instead —
    a wrong number is worse than an error, and this surface feeds agents.
    """
    from carrac.formula_eval import canonical_timeframe, SUPPORTED_TIMEFRAMES

    if canonical_timeframe(timeframe) is None:
        raise ToolError(
            f'Unsupported timeframe {timeframe!r}. Carrac stores daily and 1h '
            f'bars only; supported timeframes are: '
            f'{", ".join(SUPPORTED_TIMEFRAMES)}. Sub-hourly (1m/5m/15m) data '
            f'is not collected, so no intraday backtest is possible.'
        )


# ── indicator_run_formula ─────────────────────────────────────────

@mcp_tool(
    'indicator_run_formula',
    'Evaluate a DSL formula against the latest bar of a symbol/timeframe. '
    'Returns: value (float, last non-NaN bar), met (bool, true when value > 0), '
    'evidence (human-readable "expr = N.NNNN"), error (string or null). '
    'Use for one-shot "is this condition true right now?" checks — e.g. confirming '
    'a setup before logging a trade. NOT for historical analysis; call '
    'backtest_indicator_formula for that.\n\n'
    'DSL supports RSI/MACD/EMA/BB/ATR/CROSSOVER/CPR and quant macro vars '
    '(m2, dxy, fear_greed) plus per-asset derivatives (funding, open interest). '
    'Call indicator_get_dsl_reference first to see all available functions and operators.',
    {
        'type': 'object',
        'properties': {
            'symbol': {'type': 'string', 'description': 'Asset symbol, e.g. "BTC"'},
            'expression': {
                'type': 'string',
                'description': 'Formula expression (max 500 chars), '
                               'e.g. "RSI(14) < 30 AND CROSSOVER(EMA(9), EMA(21))"',
            },
            'timeframe': {
                'type': 'string',
                'description': 'OHLCV timeframe (e.g. 1h, 4h, 1D). Default: 1D',
            },
        },
        'required': ['symbol', 'expression'],
    },
    min_tier='premium',
)
def indicator_run_formula(arguments):
    from carrac.formula_eval import evaluate_formula, validate_formula

    symbol = (arguments.get('symbol') or '').upper().strip()
    expression = (arguments.get('expression') or '').strip()
    timeframe = (arguments.get('timeframe') or '1D').strip()

    if not symbol:
        raise ToolError('symbol is required')
    if not expression:
        raise ToolError('expression is required')
    _require_supported_timeframe(timeframe)

    # Parse-only validation first — gives a cleaner error for malformed inputs.
    parse_check = validate_formula(expression)
    if not parse_check['valid']:
        return {
            'symbol': symbol,
            'expression': expression,
            'timeframe': timeframe,
            'value': None,
            'met': False,
            'error': parse_check['error'],
        }

    result = evaluate_formula(expression, symbol, timeframe)
    return {
        'symbol': symbol,
        'expression': expression,
        'timeframe': timeframe,
        **result,
    }


# ── indicator_list ────────────────────────────────────────────

@mcp_tool(
    'indicator_list',
    'List all indicators available in the Carrac chart (oscillators, trend, '
    'levels, volume, structure, macro, custom). Returns lightweight metadata '
    'per indicator: name (registry key), label (display name), category, type, '
    'and has_states (whether discrete alert states exist). Also returns the '
    'full list of categories.\n\n'
    'When to call: discovering what indicators exist, or checking the canonical '
    'key to pass to indicator_get_definition or create_quant_alert. '
    'When NOT to call: if you already know the indicator name — call '
    'indicator_get_definition directly for the full definition and states.',
    {
        'type': 'object',
        'properties': {
            'category': {
                'type': 'string',
                'description': 'Optional filter: oscillators | trend | '
                               'levels | volume | structure | macro | custom. '
                               'Omit to list all categories.',
            },
        },
    },
    min_tier='free',
)
def indicator_list(arguments):
    from carrac.mcp.indicator_registry_parser import (
        get_all_indicators, get_categories, get_registry_diagnostics,
    )

    category = (arguments.get('category') or '').strip().lower() or None
    all_ind = get_all_indicators()
    cats = get_categories()

    if category and cats:
        valid_ids = {c['id'] for c in cats}
        if category not in valid_ids:
            raise ToolError(
                f'Unknown category: {category}. '
                f'Valid categories: {", ".join(sorted(valid_ids))}'
            )

    items = []
    for name, entry in sorted(all_ind.items()):
        if category and entry.get('category') != category:
            continue
        items.append({
            'name': name,
            'label': entry.get('label', name),
            'category': entry.get('category'),
            'type': entry.get('type'),
            'has_states': bool(entry.get('states')),
        })

    result = {
        'count': len(items),
        'categories': cats,
        'indicators': items,
    }

    if len(all_ind) == 0:
        diag = get_registry_diagnostics()
        result['warning'] = (
            'Indicator registry returned 0 entries. The server may be warming up '
            'or the registry file is missing. See server logs for details.'
        )
        result['diagnostics'] = {
            'path': diag['path'],
            'exists': diag['exists'],
            'mtime': diag['mtime'],
        }

    return result


# ── indicator_get_definition ───────────────────────────────────

@mcp_tool(
    'indicator_get_definition',
    'Return the full definition for one indicator: label, category, type '
    '(builtin_pane / builtin_overlay / pack_overlay / pack_pane / special), '
    'default calculation parameters, possible discrete states (for indicator '
    'alerts), and a one-paragraph trading description. '
    'Pass either the canonical key (e.g. "VOL", "ATR_PCT", "BOLL") or a '
    'natural-language label (e.g. "Volume", "Bollinger Bands"). '
    'Returns suggestions on miss.',
    {
        'type': 'object',
        'properties': {
            'name': {
                'type': 'string',
                'description': (
                    'Indicator name or label (case-insensitive). '
                    'Accepts canonical keys (e.g. "VOL", "ATR_PCT", "BOLL") or '
                    'natural-language labels (e.g. "Volume", "Bollinger Bands"). '
                    'Call indicator_list to see all available names.'
                ),
            },
        },
        'required': ['name'],
    },
    min_tier='free',
)
def indicator_get_definition(arguments):
    from carrac.mcp.indicator_registry_parser import get_indicator_fuzzy

    name = (arguments.get('name') or '').strip()
    if not name:
        raise ToolError('name is required')

    result = get_indicator_fuzzy(name)
    # Fuzzy lookup returns either a full entry dict or {"error": ..., "suggestions": [...]}
    if 'error' in result:
        return result  # Return error + suggestions so the caller can pick

    return result


# ── backtest_indicator_formula ─────────────────────────────────

_BACKTEST_TTL_SEC = 6 * 3600  # 6h
_BACKTEST_KEY_PREFIX = 'mcd:mcp:bt:formula:'

# Bump whenever the backtest's measurement semantics change (not
# just the DSL/ENGINE_VERSION). Version 2 made lookback_bars honour the
# requested window instead of clamping to OHLCV_BARS, so entries cached
# under version 1 were measured over a different window than their key
# claims. Folding the version into the cache key hash busts those entries
# automatically — no manual Redis flush needed.
_BACKTEST_CACHE_VERSION = 2

# Extra trailing bars requested beyond lookback_bars so indicators with a
# warmup period (SMA/EMA/BB percentile, etc.) have room to stabilize before
# start_idx, instead of the window's first bars scanning on NaN warmup output.
# Applied uniformly regardless of lookback_bars.
_BACKTEST_WARMUP_MARGIN_BARS = 200


def _backtest_cache_key(symbol, expression, timeframe, lookback_bars, hold_bars):
    payload = json.dumps(
        {
            's': symbol, 'e': expression, 't': timeframe,
            'l': lookback_bars, 'h': hold_bars,
            'v': _BACKTEST_CACHE_VERSION,
        },
        sort_keys=True,
    )
    return _BACKTEST_KEY_PREFIX + hashlib.sha256(payload.encode()).hexdigest()


# No longer a standalone MCP tool — absorbed into backtest_run
# (source=formula, the default). Kept as a plain helper, called directly by
# backtest_run in tools/backtest.py and by backtest_play/scan_and_backtest
# (also plain helpers there). The legacy 'backtest_indicator_formula' tool
# name still dispatches via registry.LEGACY_TOOL_ALIASES straight to
# backtest_run, which infers source='formula' from the presence of `symbol`
# and absence of `play_id` — see backtest_run's docstring.
def backtest_indicator_formula(arguments):
    import math

    from carrac.formula_eval import (
        Evaluator, Parser, get_quant_deps, get_deriv_deps,
        load_ohlcv, _load_quant_into_context, _load_deriv_into_context,
        tokenize, validate_formula, MAX_EXPRESSION_LENGTH,
        btc_reference_warnings,
    )
    import numpy as np
    import pandas as pd

    def _iso(dt64) -> str:
        return pd.Timestamp(dt64).strftime('%Y-%m-%dT%H:%M:%SZ')

    symbol = (arguments.get('symbol') or '').upper().strip()
    expression = (arguments.get('expression') or '').strip()
    timeframe = (arguments.get('timeframe') or '1D').strip()
    lookback_bars = int(arguments.get('lookback_bars') or 365)
    hold_bars = int(arguments.get('hold_bars') or 5)

    if not symbol or not expression:
        raise ToolError('symbol and expression are required')
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ToolError(f'Expression too long (max {MAX_EXPRESSION_LENGTH})')
    _require_supported_timeframe(timeframe)
    lookback_bars = max(50, min(2000, lookback_bars))
    hold_bars = max(1, min(60, hold_bars))

    valid = validate_formula(expression)
    if not valid['valid']:
        raise ToolError(f'Invalid formula: {valid["error"]}')

    # Cache check
    cache_key = _backtest_cache_key(symbol, expression, timeframe, lookback_bars, hold_bars)
    try:
        from carrac.redis_cache import get_redis
        r = get_redis()
        if r:
            cached = r.get(cache_key)
            if cached:
                return json.loads(cached) | {'_cached': True}
    except Exception as e:
        logger.debug(f'Cache lookup failed: {e}')

    # Load OHLCV — ask for lookback_bars plus warmup margin,
    # rather than the flat OHLCV_BARS=500 every other caller gets. Only this
    # tool needs deep history; /api/formula/* and HTF alignment keep loading
    # exactly 500 bars (their default, unchanged).
    ohlcv = load_ohlcv(symbol, timeframe,
                        requested_bars=lookback_bars + _BACKTEST_WARMUP_MARGIN_BARS)
    if ohlcv is None:
        raise ToolError(f'No OHLCV data for {symbol}/{timeframe}')

    n = len(ohlcv['close'])
    if n < hold_bars + 20:
        raise ToolError(f'Not enough data: have {n} bars, need at least {hold_bars + 20}')

    # The TRUE depth this symbol/timeframe has on disk, independent
    # of how many bars we asked for or how many survived the load. Used to
    # flag truncation honestly rather than silently substituting a shorter
    # window.
    history_available_bars = ohlcv.get('_total_available', n)
    truncated = lookback_bars > history_available_bars

    # Restrict to lookback window from end of data.
    start_idx = max(0, n - lookback_bars)
    bars_used = n - start_idx
    window_start = _iso(ohlcv['_dates'][start_idx])
    window_end = _iso(ohlcv['_dates'][n - 1])

    context = dict(ohlcv)
    quant_deps = get_quant_deps(expression)
    if quant_deps:
        _load_quant_into_context(context, quant_deps, n)
    deriv_deps = get_deriv_deps(expression)
    if deriv_deps:
        try:
            _load_deriv_into_context(context, deriv_deps, symbol, n)
        except ValueError as e:
            # per-symbol var with no rows for this symbol —
            # refuse loudly rather than backtest a silently absent series.
            raise ToolError(str(e))

    # Same BTC-reference warning evaluate_formula gives.
    _warnings = btc_reference_warnings(quant_deps, symbol)

    tokens = tokenize(expression)
    parser = Parser(tokens)
    ast = parser.parse()
    evaluator = Evaluator(context, n, symbol=symbol, timeframe=timeframe)
    result = evaluator.eval(ast)

    if result[0] != 'a':
        # Constant — no signal series possible.
        return {
            'symbol': symbol,
            'expression': expression,
            'timeframe': timeframe,
            'error': 'Formula resolves to a constant; cannot backtest as signal series.',
        }

    series = result[1]
    closes = np.asarray(ohlcv['close'], dtype=float)

    # Detect flip-to-true transitions in [start_idx..n-hold_bars-1]
    entries = []
    prev = False
    for i in range(start_idx, n - hold_bars):
        v = series[i]
        cur = (not np.isnan(v)) and v > 0
        if cur and not prev:
            entries.append(i)
        prev = cur

    if not entries:
        out = {
            'model': MODEL_HOLD_N,
            'symbol': symbol,
            'expression': expression,
            'timeframe': timeframe,
            'lookback_bars': lookback_bars,
            'hold_bars': hold_bars,
            'bars_used': bars_used,
            'window_start': window_start,
            'window_end': window_end,
            'history_available_bars': history_available_bars,
            'truncated': truncated,
            'trades': 0,
            'message': 'No signal flips in the lookback window.',
        }
        if _warnings:
            out['warnings'] = _warnings
        try:
            if r:
                r.setex(cache_key, _BACKTEST_TTL_SEC, json.dumps(out))
        except Exception:
            pass
        return out

    rets = []
    wins = 0
    for i in entries:
        entry_px = closes[i]
        exit_px = closes[i + hold_bars]
        if not (math.isfinite(entry_px) and math.isfinite(exit_px) and entry_px > 0):
            continue
        ret = (exit_px - entry_px) / entry_px
        rets.append(ret)
        if ret > 0:
            wins += 1

    if not rets:
        out = {
            'model': MODEL_HOLD_N,
            'symbol': symbol, 'expression': expression, 'timeframe': timeframe,
            'lookback_bars': lookback_bars, 'hold_bars': hold_bars,
            'bars_used': bars_used,
            'window_start': window_start,
            'window_end': window_end,
            'history_available_bars': history_available_bars,
            'truncated': truncated,
            'trades': 0, 'message': 'No valid trades after price filtering.',
        }
        if _warnings:
            out['warnings'] = _warnings
        return out

    rets_arr = np.asarray(rets)
    mean = float(np.mean(rets_arr))
    median = float(np.median(rets_arr))
    std = float(np.std(rets_arr)) if len(rets_arr) > 1 else 0.0
    sharpe_like = float(mean / std) if std > 0 else None

    # Equity curve drawdown (compounded)
    equity = np.cumprod(1.0 + rets_arr)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    max_dd = float(np.min(dd)) if len(dd) else 0.0

    out = {
        'model': MODEL_HOLD_N,
        'symbol': symbol,
        'expression': expression,
        'timeframe': timeframe,
        'lookback_bars': lookback_bars,
        'hold_bars': hold_bars,
        'bars_used': bars_used,
        'window_start': window_start,
        'window_end': window_end,
        'history_available_bars': history_available_bars,
        'truncated': truncated,
        'trades': len(rets),
        'hit_rate': round(wins / len(rets), 4),
        'mean_return': round(mean, 6),
        'median_return': round(median, 6),
        'stdev_return': round(std, 6),
        'sharpe_like': round(sharpe_like, 4) if sharpe_like is not None else None,
        'max_drawdown': round(max_dd, 6),
        'final_equity_multiple': round(float(equity[-1]), 6),
    }
    if _warnings:
        out['warnings'] = _warnings
    try:
        if r:
            r.setex(cache_key, _BACKTEST_TTL_SEC, json.dumps(out))
    except Exception:
        pass
    return out


# ── indicator_get_dsl_reference ──────────────────────────────────────────

# Hardcoded category groupings for FUNCTION_REGISTRY keys.
# Extend if new functions are added to formula_eval.py.
_DSL_CATEGORIES: dict = {
    'moving_averages': ['sma', 'ema', 'wma', 'hma', 'kama', 'alma', 'dema', 'tema'],
    'oscillators': ['rsi', 'macd_line', 'macd_signal', 'macd_hist',
                    'stoch_k', 'stoch_d', 'willr', 'cci', 'mfi'],
    'volatility': ['atr', 'atr_pct', 'stdev', 'bb_upper', 'bb_lower', 'bb_mid',
                   'bbwidth', 'bbwidth_pct', 'bbw_percentile', 'keltner_upper', 'keltner_lower'],
    'bar_reference': ['highest', 'lowest', 'highest_bars', 'lowest_bars',
                      'offset', 'ref', 'change', 'roc'],
    'logic': ['crossover', 'crossunder', 'barssince', 'valuewhen',
              'count', 'streak', 'squeeze_on'],
    'math': ['abs', 'sqrt', 'log', 'exp', 'max_val', 'min_val', 'zscore', 'percentile', 'rank'],
    'special': ['htf', 'mtf', 'divergence', 'if'],
}

_CPR_FUNCTIONS = [
    # Rolling daily CPR — each bar uses the previous bar's HLC (intraday-useful)
    'cpr_pivot', 'cpr_r1', 'cpr_r2', 'cpr_r3',
    'cpr_s1', 'cpr_s2', 'cpr_s3',
    # Monthly anchored CPR — matches the chart's flagship CPR indicator exactly.
    # Values are anchored to the previous calendar month's H/L/C and step once per month.
    'monthly_cpr_p', 'monthly_pivot',       # pivot point (monthly_pivot is an alias)
    'monthly_cpr_tc', 'monthly_cpr_bc',     # top / bottom camarilla
    'monthly_cpr_r1', 'monthly_cpr_r2', 'monthly_cpr_r3',  # resistance levels
    'monthly_cpr_s1', 'monthly_cpr_s2', 'monthly_cpr_s3',  # support levels
]

_OHLCV_SERIES = ['open', 'high', 'low', 'close', 'volume', 'hl2', 'hlc3', 'ohlc4', 'tr']

_OPERATORS = ['+', '-', '*', '/', '>', '<', '>=', '<=', '==', '!=', 'AND', 'OR', 'NOT', '[n]']

_EXAMPLES = [
    'RSI(14) < 30 AND CROSSOVER(EMA(9), EMA(21))',
    "HTF('1W', RSI(14)) < 50 AND RSI(14) < 40",
    'BBWIDTH() < SMA(BBWIDTH(), 125) * 0.5',
    'CLOSE > EMA(8) AND EMA(8) > EMA(21) AND VOLUME > SMA(VOLUME, 20) * 1.3',
    # Monthly CPR anchored-breakout — matches chart CPR indicator values
    "CLOSE > MONTHLY_CPR_P() AND CLOSE > MONTHLY_CPR_TC() AND EMA(8) > EMA(21)",  # Above monthly pivot + TC with bullish short-term EMA stack — anchored-CPR breakout
    # Monthly timeframe filter via HTF resampling
    "HTF('1M', CLOSE) > HTF('1M', EMA(8))",  # Monthly close above monthly 8-EMA — classic monthly trend filter
]


@mcp_tool(
    'indicator_get_dsl_reference',
    'Return the full DSL grammar — OHLCV series, all available functions grouped by category, '
    'quant variables, derivatives variables, operators, let-syntax, and concrete examples. '
    'Call this before writing any custom formula to avoid trial-and-error.',
    {
        'type': 'object',
        'properties': {},
        'required': [],
    },
    min_tier='free',
)
def indicator_get_dsl_reference(arguments: dict) -> dict:  # noqa: ARG001
    """Return full DSL grammar reference for the formula engine."""
    # Lazy import to avoid circular imports at module load time.
    from carrac.formula_eval import (
        ENGINE_VERSION,
        FUNCTION_REGISTRY,
        QUANT_VARS,
        DERIV_VARS,
        VAR_SCOPE,
        MAX_EXPRESSION_LENGTH,
        MAX_FUNCTION_CALLS,
        EVAL_TIMEOUT_MS,
    )

    # Build the categorized functions dict, using only keys actually in FUNCTION_REGISTRY.
    all_known: set = set()
    functions: dict = {}
    for category, names in _DSL_CATEGORIES.items():
        present = [n for n in names if n in FUNCTION_REGISTRY or n in ('htf', 'mtf', 'divergence', 'if')]
        functions[category] = present
        all_known.update(present)

    # Detect any FUNCTION_REGISTRY keys not covered by a category.
    uncategorized = sorted(
        k for k in FUNCTION_REGISTRY.keys()
        if k not in all_known
        and k not in _CPR_FUNCTIONS
        and k not in ('tr',)  # 'tr' is exposed as an OHLCV series, not a function
    )
    if uncategorized:
        functions['uncategorized'] = uncategorized

    return {
        'engine_version': ENGINE_VERSION,
        'ohlcv_series': _OHLCV_SERIES,
        'functions': functions,
        'cpr_functions': _CPR_FUNCTIONS,
        'quant_vars': sorted(QUANT_VARS.keys()),
        'derivatives_vars': sorted(DERIV_VARS.keys()),
        # symbol-resolution scope per variable.
        #   per_symbol    — resolves to the formula's own symbol
        #   global        — one market-wide series regardless of symbol
        #   btc_reference — a single BTC-anchored series; non-BTC use adds
        #                   a warning to evaluate/backtest results
        'var_scopes': {name: VAR_SCOPE[name]
                       for name in list(QUANT_VARS) + list(DERIV_VARS)},
        'operators': _OPERATORS,
        'let_syntax': 'let x = EMA(14); x > SMA(20)',
        'examples': _EXAMPLES,
        'limits': {
            'max_expression_length': MAX_EXPRESSION_LENGTH,
            'max_function_calls': MAX_FUNCTION_CALLS,
            'eval_timeout_ms': EVAL_TIMEOUT_MS,
        },
        'discovery_hint': (
            'All function names case-insensitive. '
            'Bracket syntax x[n] = OFFSET(x, n) = REF(x, n). '
            'CPR functions come in two variants: rolling daily (cpr_pivot, cpr_r1..s3 — each bar uses '
            'the prior bar\'s HLC, useful for intraday) and monthly-anchored '
            '(monthly_cpr_p, monthly_cpr_tc, monthly_cpr_bc, monthly_cpr_r1..r3, monthly_cpr_s1..s3 — '
            'anchored to the previous calendar month\'s H/L/C, matching the chart\'s flagship CPR indicator exactly). '
            'Call indicator_get_definition for any indicator\'s chart-level meta.'
        ),
    }
