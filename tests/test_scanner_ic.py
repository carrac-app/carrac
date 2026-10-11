"""Unit tests for the scanner-score IC engine (pure math, no DB)."""
from carrac.scanner.scorer.ic import compute_ic, grade


def _mk(total, ret):
    row = {'total': total, 'aligned_return': ret}
    for c in ('trend', 'momentum', 'rsi', 'structure', 'level', 'macro'):
        row['c_' + c] = total / 2.0
    return row


def test_perfect_predictor_high_positive_ic():
    rows = [_mk(i % 13, (i % 13) * 0.01 + i * 1e-6) for i in range(130)]
    ic, n = compute_ic(rows)['ic_total']
    assert n == 130
    assert ic is not None and ic > 0.9


def test_inverse_predictor_negative_ic():
    rows = [_mk(i % 13, -(i % 13) * 0.01 - i * 1e-6) for i in range(130)]
    ic, _ = compute_ic(rows)['ic_total']
    assert ic is not None and ic < -0.9


def test_constant_score_is_degenerate_none():
    # A score with no variance cannot correlate with anything → None, not a crash.
    rows = [_mk(6, (i % 7) * 0.01) for i in range(40)]
    assert compute_ic(rows)['ic_total'][0] is None


def test_buckets_monotone_for_perfect_predictor():
    rows = [_mk(i % 13, (i % 13) * 0.01) for i in range(130)]
    means = [compute_ic(rows)['buckets'][k][0] for k in ('0-2', '3-5', '6-8', '9-12')]
    assert all(a < b for a, b in zip(means, means[1:]))


def test_grade_honesty_gate():
    assert grade(0.02) == 'noise (<0.05)'
    assert grade(0.07) == 'good'
    assert grade(0.12) == 'great'
    assert grade(0.17) == 'world-class'
    assert 'SUSPECT' in grade(0.25)
    assert grade(None) == '—'
