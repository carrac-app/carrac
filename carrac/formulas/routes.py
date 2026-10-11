"""Formula preview endpoint — renders a formula for the chart.

Endpoints:
    GET  /api/formula/preview    — chart rendering. Free-tier session access.
"""

import logging
import math

from flask import Blueprint, jsonify, request

from carrac.auth.decorators import login_required
from carrac.extensions import limiter
from carrac.formula_eval import SUPPORTED_TIMEFRAMES

logger = logging.getLogger('carrac.formulas')

formulas_bp = Blueprint('formulas', __name__, url_prefix='/api/formula')


VALID_TIMEFRAMES = SUPPORTED_TIMEFRAMES
MAX_SYMBOL_LEN = 16


def _validate_inputs(expression, symbol, timeframe):
    """Return None on success, (status_code, jsonified_error) on failure."""
    if not expression or not isinstance(expression, str):
        return 400, jsonify({'error': 'Missing or invalid expression'})
    if len(expression) > 500:
        return 400, jsonify({'error': 'Expression exceeds 500 character limit'})
    if not symbol or not isinstance(symbol, str) or len(symbol) > MAX_SYMBOL_LEN:
        return 400, jsonify({'error': 'Missing or invalid symbol'})
    if timeframe and timeframe not in VALID_TIMEFRAMES:
        return 400, jsonify({
            'error': 'Invalid timeframe',
            'valid_timeframes': list(VALID_TIMEFRAMES)
        })
    return None


def _sanitize_array(values):
    """Convert NaN/Inf to None for JSON transport."""
    out = []
    for v in values:
        if v is None:
            out.append(None)
        else:
            try:
                fv = float(v)
                if math.isnan(fv) or math.isinf(fv):
                    out.append(None)
                else:
                    out.append(fv)
            except (TypeError, ValueError):
                out.append(None)
    return out


@formulas_bp.route('/preview', methods=['GET'])
@login_required
@limiter.limit('60 per minute')
def preview():
    """Return the full output array for chart rendering.

    Query: ?symbol=BTC&tf=1D&expression=rsi(14)

    Free-tier access so users discover the feature; heavy workloads (scanner,
    backtest) remain gated elsewhere.
    """
    expression = (request.args.get('expression') or '').strip()
    symbol = (request.args.get('symbol') or '').strip().upper()
    timeframe = (request.args.get('tf') or '1D').strip()

    err = _validate_inputs(expression, symbol, timeframe)
    if err:
        return err[1], err[0]

    from carrac.formula_eval import (
        load_ohlcv, get_quant_deps, get_deriv_deps, _load_quant_into_context,
        _load_deriv_into_context, tokenize, Parser, Evaluator, ENGINE_VERSION,
    )
    import numpy as np

    ohlcv = load_ohlcv(symbol, timeframe)
    if ohlcv is None:
        return jsonify({
            'engine_version': ENGINE_VERSION,
            'error': f'No OHLCV data for {symbol}/{timeframe}',
        }), 404

    n = len(ohlcv['close'])
    if n == 0:
        return jsonify({'engine_version': ENGINE_VERSION, 'error': 'Empty OHLCV data'}), 404

    context = dict(ohlcv)
    quant_deps = get_quant_deps(expression)
    if quant_deps:
        _load_quant_into_context(context, quant_deps, n)
    deriv_deps = get_deriv_deps(expression)
    if deriv_deps:
        _load_deriv_into_context(context, deriv_deps, symbol, n)

    try:
        tokens = tokenize(expression)
        parser = Parser(tokens)
        ast = parser.parse()
        evaluator = Evaluator(context, n, symbol=symbol, timeframe=timeframe)
        result = evaluator.eval(ast)
    except Exception as e:
        return jsonify({
            'engine_version': ENGINE_VERSION,
            'error': f'Evaluation error: {e}',
        }), 400

    if result[0] == 'a':
        arr = result[1]
    elif result[0] == 's':
        arr = np.full(n, float(result[1]))
    else:
        return jsonify({
            'engine_version': ENGINE_VERSION,
            'error': 'Formula did not return a numeric value',
        }), 400

    # Timestamps (UTC unix seconds) for chart alignment
    timestamps = []
    if '_dates' in ohlcv:
        import pandas as pd
        ts = pd.DatetimeIndex(ohlcv['_dates']).astype('int64') // 10**9
        timestamps = [int(x) for x in ts.tolist()]

    return jsonify({
        'engine_version': ENGINE_VERSION,
        'symbol': symbol,
        'timeframe': timeframe,
        'values': _sanitize_array(arr),
        'timestamps': timestamps,
        'error': None,
    })


