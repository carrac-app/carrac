"""Pin the Python formula evaluator against golden outputs.

Drift here means the engine semantics changed — if intentional, regenerate
with `python tests/golden/formula/regenerate.py` and bump ENGINE_VERSION.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from carrac.formula_eval import (
    tokenize, Parser, Evaluator, ENGINE_VERSION,
)

GOLDEN_DIR = Path(__file__).parent / 'golden' / 'formula'

# This was `range(40)`, hardcoded. expected.json had already grown
# past 40 results, so the tail was never compared — a golden fixture could be
# wrong and this test would still pass. Derive the count from the file so
# every result is checked and adding fixtures cannot silently outrun coverage.
_GOLDEN_RESULT_COUNT = len(
    json.loads((GOLDEN_DIR / 'expected.json').read_text())['results']
)


@pytest.fixture(autouse=True)
def _no_wallclock_budget(request, monkeypatch):
    """Take the evaluator's 200ms wall-clock budget out of play for the parity
    tests.

    EVAL_TIMEOUT_MS is a production LATENCY guard — it stops a user's
    pathological formula from pegging a request thread. It is not a property
    of the numbers this file checks. Under `make test`'s xdist runner (16
    workers on 12 cores) a worker can be descheduled for longer than 200ms
    mid-evaluation, so `crossover(sma(close, 5), sma(close, 20))` over 120
    bars — a few microseconds of actual work — raised TimeoutError and failed
    the suite. That blocks deploys and looks exactly like
    a test-order flake without being one: same test, same
    parameter, same worker, only ever under parallel load.

    Measuring wall-clock inside a parallel test run measures the scheduler,
    not the code. The guard keeps its own dedicated coverage in
    TestEvalTimeoutGuard below, where it is exercised deliberately instead of
    accidentally.
    """
    if request.cls is not None and request.cls.__name__ == 'TestEvalTimeoutGuard':
        return  # that class owns the budget; it must see the real value
    import carrac.formula_eval as fe
    monkeypatch.setattr(fe, 'EVAL_TIMEOUT_MS', 60_000)


def _load():
    fixtures = json.loads((GOLDEN_DIR / 'fixtures.json').read_text())
    expected = json.loads((GOLDEN_DIR / 'expected.json').read_text())
    return fixtures, expected


def _eval(expr, ohlcv):
    context = {k: np.asarray(v, dtype=np.float64) for k, v in ohlcv.items()
               if k in ('open', 'high', 'low', 'close', 'volume')}
    n = len(context['close'])
    ast = Parser(tokenize(expr)).parse()
    result = Evaluator(context, n, symbol='_FIXTURE_', timeframe='1D').eval(ast)
    if result[0] == 'a':
        return result[1]
    if result[0] == 's':
        return np.full(n, float(result[1]))
    raise AssertionError(f'Unexpected result type: {result[0]}')


def test_engine_version_matches_golden():
    _, expected = _load()
    assert expected['engine_version'] == ENGINE_VERSION, (
        f"Golden engine_version {expected['engine_version']} != runtime {ENGINE_VERSION}. "
        f"Regenerate with `python tests/golden/formula/regenerate.py` after "
        f"semantics changes."
    )


def test_golden_coverage_is_complete():
    """Every golden result must be parametrized — guards the guard."""
    _, expected = _load()
    assert _GOLDEN_RESULT_COUNT == len(expected['results'])
    assert _GOLDEN_RESULT_COUNT >= 40, 'golden set shrank unexpectedly'


@pytest.mark.parametrize('result_idx', range(_GOLDEN_RESULT_COUNT))
def test_formula_matches_golden(result_idx):
    fixtures, expected = _load()
    ohlcv_by_name = {f['name']: f for f in fixtures['ohlcv_fixtures']}
    case = expected['results'][result_idx]

    if case['error']:
        pytest.skip(f"Golden marks this as error: {case['error']}")

    ohlcv = ohlcv_by_name[case['fixture']]
    actual = _eval(case['expr'], ohlcv).tolist()
    actual_clean = []
    for v in actual:
        if v is None:
            actual_clean.append(None)
            continue
        fv = float(v)
        if np.isnan(fv) or np.isinf(fv):
            actual_clean.append(None)
        else:
            actual_clean.append(round(fv, 6))

    expected_vals = case['values']
    assert len(actual_clean) == len(expected_vals), (
        f"Length mismatch on {case['fixture']}/{case['expr_id']}: "
        f"got {len(actual_clean)} vs golden {len(expected_vals)}"
    )
    for i, (a, e) in enumerate(zip(actual_clean, expected_vals)):
        if a is None and e is None:
            continue
        assert a is not None and e is not None, (
            f"{case['fixture']}/{case['expr_id']} bar {i}: NaN mismatch (got {a}, golden {e})"
        )
        assert abs(a - e) < 1e-5, (
            f"{case['fixture']}/{case['expr_id']} bar {i}: got {a} vs golden {e}"
        )


class TestEvalTimeoutGuard:
    """The 200ms budget had NO test at all — found when it flaked the deploy
    gate. It is real DoS protection for a user-supplied formula field, so it
    gets exercised deliberately here rather than by accident under load."""

    def test_the_budget_is_still_200ms(self):
        """A silent bump would widen the DoS window; a silent drop would flake
        every slow box. Either deserves to be a decision, not a diff."""
        import carrac.formula_eval as fe
        assert fe.EVAL_TIMEOUT_MS == 200

    def test_an_over_budget_evaluation_is_stopped(self, monkeypatch):
        """Show me it failing: squeeze the budget to zero and the guard must
        fire on a formula that is otherwise trivial."""
        import carrac.formula_eval as fe
        monkeypatch.setattr(fe, 'EVAL_TIMEOUT_MS', -1)
        fixtures, expected = _load()
        ohlcv = fixtures['ohlcv_fixtures'][0]
        with pytest.raises(TimeoutError):
            _eval('sma(close, 5)', ohlcv)

    def test_a_within_budget_evaluation_is_not_stopped(self, monkeypatch):
        """And the guard must not fire on ordinary work, or it is a bug rather
        than a protection."""
        import carrac.formula_eval as fe
        monkeypatch.setattr(fe, 'EVAL_TIMEOUT_MS', 60_000)
        fixtures, expected = _load()
        ohlcv = fixtures['ohlcv_fixtures'][0]
        out = _eval('sma(close, 5)', ohlcv)
        assert len(out) == len(ohlcv['close'])
