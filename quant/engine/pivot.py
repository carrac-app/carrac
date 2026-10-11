"""
Pivot query engine for the Data Lab tab.

Fetches z-scores for selected canonical metrics and groups/aggregates
them by regime, z-score bucket, or time period.
"""

import logging
from carrac.db_pool import get_quant_conn as get_conn
from quant.engine.config import CANONICAL_METRICS

logger = logging.getLogger('quant.engine.pivot')

MAX_METRICS = 10

AGG_FUNCS = {
    'mean': lambda col: f'AVG(p."{col}")',
    'median': lambda col: f'PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY p."{col}")',
    'count': lambda col: f'COUNT(p."{col}")',
    'p25': lambda col: f'PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY p."{col}")',
    'p75': lambda col: f'PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY p."{col}")',
}


def query_pivot(metrics, group_by=None, agg='mean', from_date=None,
                to_date=None, window=90):
    """Execute a pivot query across canonical metrics."""

    # Validate
    valid = [m for m in metrics if m in CANONICAL_METRICS][:MAX_METRICS]
    if not valid:
        raise ValueError('No valid metrics provided')
    if agg not in AGG_FUNCS:
        raise ValueError(f'Invalid aggregation: {agg}')
    if window not in (30, 90):
        window = 90

    # Build the CTE: one row per date with each metric as a column
    case_cols = []
    case_params = []
    for m in valid:
        conf = CANONICAL_METRICS[m]
        if conf['table'] in ('macro', 'sentiment', 'onchain'):
            zs_key = conf.get('metric', m)
        else:
            zs_key = conf.get('asset', m)
        case_cols.append(
            f'MAX(CASE WHEN (z.metric = %s OR z.asset = %s) '
            f'THEN z.zscore END) AS "{m}"'
        )
        case_params.extend([zs_key, zs_key])

    # WHERE clause
    where_parts = ['z.window_days = %s']
    where_params = [window]
    if from_date:
        where_parts.append('z.ts >= %s')
        where_params.append(from_date)
    if to_date:
        where_parts.append('z.ts <= %s')
        where_params.append(to_date)

    pivot_cte = (
        f'WITH pivot AS ('
        f'  SELECT z.ts, {", ".join(case_cols)}'
        f'  FROM quant.zscores z'
        f'  WHERE {" AND ".join(where_parts)}'
        f'  GROUP BY z.ts'
        f')'
    )
    # Params: case_params first (for SELECT), then where_params (for WHERE)
    all_params = case_params + where_params

    # Aggregation columns
    agg_fn = AGG_FUNCS[agg]
    agg_cols = ', '.join(f'{agg_fn(m)} AS "{m}"' for m in valid)

    # Group query
    if group_by == 'regime':
        outer = (
            f'{pivot_cte}, '
            f'regime AS ('
            f"  SELECT ts, (metadata->>'label') AS state"
            f"  FROM quant.signals"
            f"  WHERE source = 'regime_hmm' AND metric = 'REGIME'"
            f') '
            f'SELECT r.state AS label, COUNT(*) AS n, {agg_cols} '
            f'FROM pivot p JOIN regime r ON p.ts = r.ts '
            f'WHERE r.state IS NOT NULL '
            f'GROUP BY r.state ORDER BY r.state'
        )
    elif group_by == 'monthly':
        outer = (
            f'{pivot_cte} '
            f"SELECT to_char(p.ts, 'YYYY-MM') AS label, COUNT(*) AS n, {agg_cols} "
            f'FROM pivot p GROUP BY label ORDER BY label'
        )
    elif group_by == 'quarterly':
        outer = (
            f'{pivot_cte} '
            f"SELECT to_char(p.ts, 'YYYY-\"Q\"Q') AS label, COUNT(*) AS n, {agg_cols} "
            f'FROM pivot p GROUP BY label ORDER BY label'
        )
    elif group_by == 'zscore_bucket':
        ref = valid[0]
        bucket_expr = (
            f'CASE '
            f'WHEN p."{ref}" < -2 THEN \'<-2\' '
            f'WHEN p."{ref}" >= -2 AND p."{ref}" < -1 THEN \'-2..-1\' '
            f'WHEN p."{ref}" >= -1 AND p."{ref}" < 0 THEN \'-1..0\' '
            f'WHEN p."{ref}" >= 0 AND p."{ref}" < 1 THEN \'0..1\' '
            f'WHEN p."{ref}" >= 1 AND p."{ref}" < 2 THEN \'1..2\' '
            f'WHEN p."{ref}" >= 2 THEN \'>2\' '
            f'ELSE \'N/A\' END'
        )
        outer = (
            f'{pivot_cte} '
            f'SELECT {bucket_expr} AS label, COUNT(*) AS n, {agg_cols} '
            f'FROM pivot p WHERE p."{ref}" IS NOT NULL '
            f'GROUP BY label ORDER BY MIN(p."{ref}")'
        )
    else:
        outer = (
            f'{pivot_cte} '
            f"SELECT 'all' AS label, COUNT(*) AS n, {agg_cols} "
            f'FROM pivot p'
        )

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(outer, all_params)
        rows = cur.fetchall()

    groups = []
    total = 0
    for row in rows:
        d = dict(row)
        for m in valid:
            if d.get(m) is not None:
                d[m] = round(float(d[m]), 4)
        d['n'] = int(d['n'])
        total += d['n']
        groups.append(d)

    return {
        'metrics': valid,
        'group_by': group_by or 'none',
        'agg': agg,
        'groups': groups,
        'total_rows': total,
    }
