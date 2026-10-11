"""Scanner-score Information Coefficient (IC) — measurement engine.

Measures whether the 0-12 scanner score actually predicts forward returns: the
**information coefficient** = Spearman rank correlation between the predicted
score and the realized N-day forward return. INTERNAL / diagnostic only — the
numbers are NOT surfaced to users at this stage (a later step, once the
score has earned it). A near-zero or negative IC on today's blended score is the
*expected* baseline — establishing it is the whole point (it's the "before"
photo for archetype scoring, and the map of which of the 6 criteria
carry any signal).

Forward-return source — scanner_history self-join (no external price table):
    every scanner_history row already stores the asset price at capture, so
    price@(date+N) / price@date - 1 is a consistent forward return for the ENTIRE
    scanner universe (~360 symbols), self-sourced. Direction-aligned: for `short`
    rows the return is negated (a higher short score should predict a *lower*
    forward price).

Honesty gate (Grinold & Kahn): |IC| 0.05 = good, 0.10 = great, 0.15 = world-class,
>0.20 = SUSPECT (almost always a leak/look-ahead/overfit, not real skill).

Schedule: nightly ~07:00 UTC (after quant regime.py at 06:55), once enough forward
window has accrued. Reads scanner_history (carrac DB); writes nothing.

Usage:
    python -m carrac.scanner.scorer.ic
    python -m carrac.scanner.scorer.ic --horizons 1,5,10
    python -m carrac.scanner.scorer.ic --dry-run
"""
import argparse
import logging

logger = logging.getLogger('carrac.scanner.scorer.ic')

CRITERIA = ('trend', 'momentum', 'rsi', 'structure', 'level', 'macro')
DEFAULT_HORIZONS = (1, 5, 10)
MIN_OBS = 30  # below this an IC is essentially noise; reported but flagged
SUSPECT, WORLD_CLASS, GREAT, GOOD = 0.20, 0.15, 0.10, 0.05


def fetch_pairs(horizon):
    """Direction-aligned (score, forward-return, per-criterion) rows for a horizon.

    Self-joins scanner_history to itself on (symbol, direction) offset by `horizon`
    calendar days. Postgres-only SQL (JSONB operators + make_interval) — this engine
    runs against the Postgres carrac DB, never SQLite.
    """
    from carrac.db_pool import get_conn, adapt_query
    sql = adapt_query(
        """
        SELECT s.direction,
               s.total,
               CASE WHEN s.direction = 'long'
                    THEN  (f.price / s.price - 1)
                    ELSE -(f.price / s.price - 1) END          AS aligned_return,
               (s.criteria->'trend'->>'score')::numeric        AS c_trend,
               (s.criteria->'momentum'->>'score')::numeric     AS c_momentum,
               (s.criteria->'rsi'->>'score')::numeric          AS c_rsi,
               (s.criteria->'structure'->>'score')::numeric    AS c_structure,
               (s.criteria->'level'->>'score')::numeric        AS c_level,
               (s.criteria->'macro'->>'score')::numeric        AS c_macro
        FROM scanner_history s
        JOIN scanner_history f
          ON f.symbol = s.symbol
         AND f.direction = s.direction
         AND f.captured_date = s.captured_date + make_interval(days => %s)
        WHERE s.price > 0 AND f.price > 0
        """
    )
    with get_conn() as conn:
        rows = conn.execute(sql, (horizon,)).fetchall()
    return [dict(r) for r in rows]


def _ic(xs, ys):
    """Spearman IC + n, guarding degenerate samples (constant input → undefined)."""
    from scipy.stats import spearmanr
    n = len(xs)
    if n < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None, n
    rho, _p = spearmanr(xs, ys)
    return (None if rho != rho else float(rho)), n  # rho != rho catches NaN


def compute_ic(rows):
    """Pure function: rows -> IC report dict. No DB — unit-testable in isolation."""
    rets = [float(r['aligned_return']) for r in rows]
    totals = [float(r['total']) for r in rows]
    report = {
        'n': len(rows),
        'ic_total': _ic(totals, rets),
        'ic_by_criterion': {
            c: _ic([float(r['c_' + c]) for r in rows], rets) for c in CRITERIA
        },
        'buckets': {},
    }
    # Mean forward return by score bucket — monotone rise = the score orders outcomes.
    for label, lo, hi in (('0-2', 0, 2), ('3-5', 3, 5), ('6-8', 6, 8), ('9-12', 9, 12)):
        br = [float(r['aligned_return']) for r in rows if lo <= float(r['total']) <= hi]
        report['buckets'][label] = (sum(br) / len(br) if br else None, len(br))
    return report


def grade(ic):
    """Grinold & Kahn honesty gate as a label."""
    if ic is None:
        return '—'
    a = abs(ic)
    if a > SUSPECT:
        return 'SUSPECT (>0.20 — likely leak/overfit)'
    if a >= WORLD_CLASS:
        return 'world-class'
    if a >= GREAT:
        return 'great'
    if a >= GOOD:
        return 'good'
    return 'noise (<0.05)'


def run(horizons=DEFAULT_HORIZONS, dry_run=False):
    """Fetch, compute, and print an IC report per horizon. Read-only."""
    for h in horizons:
        rows = fetch_pairs(h)
        rep = compute_ic(rows)
        ic, n = rep['ic_total']
        low = '  ⚠ low-n' if n < MIN_OBS else ''
        print(f"\n=== {h}d horizon — n={rep['n']} pairs ===")
        print(f"  Score IC (0-12 total): {'—' if ic is None else round(ic, 4)}  [{grade(ic)}]{low}")
        print("  Per-criterion IC (which sub-score carries the signal?):")
        for c, (cic, _cn) in rep['ic_by_criterion'].items():
            print(f"    {c:10s}: {'—' if cic is None else format(round(cic, 4), '+.4f')}  [{grade(cic)}]")
        print("  Mean direction-aligned forward return by score bucket (monotone = ordering skill):")
        for label, (m, bn) in rep['buckets'].items():
            print(f"    {label:5s}: {'—' if m is None else format(m * 100, '+.2f') + '%'}  (n={bn})")
    return True


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    parser = argparse.ArgumentParser(description='Scanner-score IC baseline')
    parser.add_argument('--horizons', default='1,5,10', help='comma-separated forward-day horizons')
    parser.add_argument('--dry-run', action='store_true', help='no-op (engine is read-only anyway)')
    args = parser.parse_args()
    run(horizons=tuple(int(x) for x in args.horizons.split(',')), dry_run=args.dry_run)
