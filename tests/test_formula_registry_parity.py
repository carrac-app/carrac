"""The Python and JS formula engines must not drift further.

The DSL is implemented twice: carrac/formula_eval.py (Python; MCP + API) and
static/js/formula-parser.js + ta-lib.js (JS; chart + Backtest drawer). Both
hold mirror registries. They can drift with no test comparing them: ADX(14)
evaluates in the browser but returns "Unknown function 'ADX'" from Python, and
BBWIDTH() returns a value from Python but does not exist in JS.

The existing parity tests (tests/parity/formula.parity.test.mjs,
tests/test_formula_parity.py) compare OUTPUTS for a fixed fixture list. A
function present in only one engine has no fixture, so it is never compared.
Output parity is tested; registry completeness was not.

This guard freezes the known drift as allowlists and fails on anything new.
Shrinking the lists is the goal. The lists must never GROW.
"""

import re
from pathlib import Path

from carrac.formula_eval import FUNCTION_REGISTRY

ROOT = Path(__file__).resolve().parent.parent
JS_PARSER = ROOT / 'static' / 'js' / 'formula-parser.js'

# Frozen drift snapshot. Reproduce with:
#   python3 -c "from carrac.formula_eval import FUNCTION_REGISTRY as R; print(sorted(R))"
#   rg -o '^\s*([a-z_][a-z0-9_]*)\s*:\s*\{\s*fn:' static/js/formula-parser.js
FROZEN_PY_ONLY = frozenset({
    'bbwidth', 'bbwidth_pct',
    'monthly_cpr_bc', 'monthly_cpr_p', 'monthly_cpr_r1', 'monthly_cpr_r2',
    'monthly_cpr_r3', 'monthly_cpr_s1', 'monthly_cpr_s2', 'monthly_cpr_s3',
    'monthly_cpr_tc', 'monthly_pivot',
    'ref',
})

FROZEN_JS_ONLY = frozenset({
    'ad', 'adx', 'alma', 'cci', 'clamp', 'cmf', 'kama', 'linreg',
    'linreg_slope', 'median', 'mfi', 'minus_di', 'normalize', 'plus_di',
    'pow', 'psar', 'r_squared', 'realized_vol', 'round', 'skew',
    'stoch_d', 'stoch_k', 'vwap', 'willr',
})


def _py_registry_names():
    return set(FUNCTION_REGISTRY)


def _js_registry_names():
    src = JS_PARSER.read_text()
    return set(re.findall(r'^\s*([a-z_][a-z0-9_]*)\s*:\s*\{\s*fn:', src, re.M))


def test_js_registry_is_parseable():
    """Guard the guard: if the regex stops matching, every other assertion
    here would pass vacuously against an empty set."""
    js = _js_registry_names()
    assert len(js) > 50, f'JS registry regex matched only {len(js)} names — parser format changed?'
    for known in ('ema', 'rsi', 'crossover', 'nr7'):
        assert known in js, f"'{known}' missing — regex is not matching the registry"


def test_no_new_python_only_functions():
    extra = _py_registry_names() - _js_registry_names() - FROZEN_PY_ONLY
    assert not extra, (
        f'Added to Python but not JS: {sorted(extra)}. '
        'Every DSL function must exist in BOTH engines — add it to '
        'static/js/ta-lib.js and static/js/formula-parser.js.'
    )


def test_no_new_js_only_functions():
    extra = _js_registry_names() - _py_registry_names() - FROZEN_JS_ONLY
    assert not extra, (
        f'Added to JS but not Python: {sorted(extra)}. '
        'Every DSL function must exist in BOTH engines — add it to '
        'carrac/formula_eval.py FUNCTION_REGISTRY.'
    )


def test_allowlists_never_grow():
    """A parity failure must be fixed by implementing the function, never by
    appending its name to the allowlist."""
    assert len(FROZEN_PY_ONLY) <= 13
    assert len(FROZEN_JS_ONLY) <= 24


def test_allowlisted_names_are_still_actually_drifted():
    """If a name is ported, it must be REMOVED from the allowlist,
    not left behind as dead weight that hides future drift."""
    py, js = _py_registry_names(), _js_registry_names()
    stale_py = {n for n in FROZEN_PY_ONLY if n in js}
    stale_js = {n for n in FROZEN_JS_ONLY if n in py}
    assert not stale_py, f'Now in both engines, remove from FROZEN_PY_ONLY: {sorted(stale_py)}'
    assert not stale_js, f'Now in both engines, remove from FROZEN_JS_ONLY: {sorted(stale_js)}'
