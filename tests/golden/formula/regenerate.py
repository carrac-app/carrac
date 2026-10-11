"""Regenerate expected.json from the current Python formula_eval implementation.

Run this ONLY when intentionally changing evaluator semantics. In normal CI,
`test_formula_parity.py` verifies outputs match the pinned expected.json.
"""

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent.parent))

from carrac.formula_eval import (  # noqa: E402
    tokenize, Parser, Evaluator, ENGINE_VERSION,
)


def _sanitize(arr):
    out = []
    for v in arr:
        if v is None:
            out.append(None)
            continue
        fv = float(v)
        if np.isnan(fv) or np.isinf(fv):
            out.append(None)
        else:
            # Round to 6 decimals for stable comparisons
            out.append(round(fv, 6))
    return out


def _eval(expr, ohlcv):
    context = {k: np.asarray(v, dtype=np.float64) for k, v in ohlcv.items()
               if k in ('open', 'high', 'low', 'close', 'volume')}
    n = len(context['close'])
    tokens = tokenize(expr)
    ast = Parser(tokens).parse()
    ev = Evaluator(context, n, symbol='_FIXTURE_', timeframe='1D')
    result = ev.eval(ast)
    if result[0] == 'a':
        return result[1]
    if result[0] == 's':
        return np.full(n, float(result[1]))
    raise ValueError(f'Unexpected result type: {result[0]}')


def main():
    fixtures = json.loads((HERE / 'fixtures.json').read_text())
    expected = {'engine_version': ENGINE_VERSION, 'results': []}

    for ohlcv_fx in fixtures['ohlcv_fixtures']:
        for expr_fx in fixtures['expressions']:
            try:
                out = _eval(expr_fx['expr'], ohlcv_fx)
                values = _sanitize(out.tolist())
                expected['results'].append({
                    'fixture': ohlcv_fx['name'],
                    'expr_id': expr_fx['id'],
                    'expr': expr_fx['expr'],
                    'values': values,
                    'error': None,
                })
            except Exception as e:
                expected['results'].append({
                    'fixture': ohlcv_fx['name'],
                    'expr_id': expr_fx['id'],
                    'expr': expr_fx['expr'],
                    'values': None,
                    'error': str(e),
                })

    (HERE / 'expected.json').write_text(json.dumps(expected, indent=2))
    print(f'Regenerated expected.json — {len(expected["results"])} results, engine v{ENGINE_VERSION}')


if __name__ == '__main__':
    main()
