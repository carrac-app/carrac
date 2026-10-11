"""
Quant DB API endpoints.

Flask Blueprint providing read-only access to quant data.
Gated by the `quant_engine` TIER_LIMITS knob (member/tester/premium/admin)
Via @tier_feature_required('quant_engine'), plus internal/morning
bot access via the shared admin API key.

Endpoints:
    GET /api/quant/health        -- Collector status and data freshness
    GET /api/quant/metrics       -- List all available metrics with counts
    GET /api/quant/series        -- Raw time series data
    GET /api/quant/zscores       -- Current z-scores (latest date)
    GET /api/quant/snapshot      -- Overview for morning bot / admin
    GET /api/quant/correlations  -- Cross-domain Spearman correlations
    GET /api/quant/leadlag       -- Lead-lag relationships (what leads what)
    GET /api/quant/regime        -- Current market regime (HMM + changepoints)
    GET /api/quant/conditional   -- Conditional return distributions
    GET /api/quant/analogues     -- Historical analogue search
    GET /api/quant/cot           -- CFTC COT smart-money positioning
"""

import logging
from flask import Blueprint, jsonify, request

from carrac.auth.decorators import tier_feature_required
from carrac.caches import SharedTTLCache
from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger('quant.api')

# QE data updates nightly — 5-min TTL avoids repeated heavy DB queries
_qe_cache = SharedTTLCache('qe', ttl=300, max_local=50)
# COT updates weekly — 1h TTL matches the CFTC cadence
_cot_cache = SharedTTLCache('qe_cot', ttl=3600, max_local=5)

quant_bp = Blueprint('quant', __name__, url_prefix='/api/quant')


def _serialize(rows):
    """Convert date/datetime objects to ISO strings for JSON."""
    result = []
    for row in rows:
        d = dict(row)
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        result.append(d)
    return result


def _strip_gated_chains(chains):
    """Drop transmission chains built on Deribit-backed metrics.

    deribit_gate.py's own docstring flagged this: "the
    ``derivatives_leverage`` transmission chain is built almost entirely from
    them." It is FUNDING_BTC → BASIS_BTC → SKEW_25D_BTC → RVOL_BTC → BTC, so
    two of its four links are barred.

    The whole chain goes, not just the names in it: chain_strength is a
    geometric mean ACROSS those links, so the number is derived from the
    withheld data even when no barred name is printed beside it. Publishing
    the value while hiding the label is the same mistake as gating the
    correlation card while leaving the metric in the JSON.

    Found because the assembled report briefing still carried
    "Chain [derivatives_leverage] … weakest FUNDING_BTC→BASIS_BTC" after the
    other three lists were filtered — the sibling that kept being missed.
    """
    if not chains:
        return chains
    from carrac.compliance.deribit_gate import deribit_visible, leadlag_metric_allowed
    if deribit_visible():
        return chains
    try:
        from quant.engine.config import TRANSMISSION_CHAINS
    except Exception:  # noqa: BLE001 - a broken registry must not fail OPEN
        return []

    def _ok(entry):
        cdef = TRANSMISSION_CHAINS.get(entry.get('chain')) or {}
        for a, b in (cdef.get('links') or []):
            if not leadlag_metric_allowed(a) or not leadlag_metric_allowed(b):
                return False
        wl = entry.get('weakest_link') or {}
        return (leadlag_metric_allowed(wl.get('from') or '')
                and leadlag_metric_allowed(wl.get('to') or ''))

    return [c for c in chains if _ok(c)]


def _strip_gated_anomalies(entries):
    """Drop anomalies on a Deribit-backed metric, unless the caller is admin.

    Anomalies are worse than the correlation lists: each entry
    carries the metric name AND ``value`` (the raw column value), so a single
    unfiltered row publishes the exact number the licence bars, not just the
    fact that we hold it.

    The rule itself is ``deribit_gate.is_deribit_anomaly`` — shared with the
    report-generation path so the two cannot drift apart again. It matches on
    the Deribit-backed DOMAIN, the raw column name, and the asset-agnostic
    metric prefix, because quant.zscores stores the column while the
    correlation tables store the composed form — the same value under two
    spellings, and gating only one spelling is how a filter looks right and
    leaks anyway.
    """
    if not entries:
        return entries
    from carrac.compliance.deribit_gate import deribit_visible, strip_gated_anomalies
    if deribit_visible():
        return entries
    # The rule moved into deribit_gate as
    # is_deribit_anomaly(). The version that lived here gated on a BTC-only
    # lead-lag registry and named only ONE of the two Deribit-backed domains,
    # so SKEW_25D_ETH, GEX_ETH and OPTIONS_IV_ETH were served on the public
    # /brief page with their raw values. One rule, one place, both callers.
    return strip_gated_anomalies(entries)


def _gate_snapshot(result):
    """Apply the Deribit gate to a snapshot payload, per CALLER, on the way out.

    This has to happen AFTER the cache read,
    not before the cache write, and that ordering is the whole point.

    ``build_snapshot`` caches under ``snapshot:{asset}`` — a key with no caller
    identity — in a Redis-backed, cross-worker ``SharedTTLCache`` (ttl 300).
    Gating before the write stores whichever caller happened to MISS first and
    serves that to everyone else for the next five minutes. With an admin
    missing first (a precompute cycle, an MCP call, the operator opening the
    Pulse page) the ungated payload is what the public /brief render then
    picked up. The callers are many — /brief, the editorial gather, pulse.py,
    MCP analysis, precompute — and they share the one entry.

    So the cached artifact is caller-independent and this is the only place
    caller identity is applied. Admin short-circuits to the full payload.
    """
    if not isinstance(result, dict):
        return result
    from carrac.compliance.deribit_gate import deribit_visible
    if deribit_visible():
        return result
    out = dict(result)
    out['anomalies'] = _strip_gated_anomalies(result.get('anomalies'))
    out['anomaly_count'] = len(out['anomalies'] or [])
    out['anomaly_cluster'] = _strip_gated_cluster(result.get('anomaly_cluster'))
    out['top_correlations'] = _strip_gated_metrics(result.get('top_correlations'), 'metric')
    out['leadlag_signals'] = _strip_gated_metrics(result.get('leadlag_signals'), 'leader')
    out['leadlag_signals_weekly'] = _strip_gated_metrics(
        result.get('leadlag_signals_weekly'), 'leader')
    out['chains'] = _strip_gated_chains(result.get('chains'))
    return out


def _strip_gated_cluster(cluster):
    """Drop Deribit-backed rows from the anomaly cluster, unless admin.

    Same rule as _strip_gated_anomalies, applied to the cluster payload and
    its derived headline counts. Kept as its own function so the single
    return site below reads the same way as its gated siblings.
    """
    from carrac.compliance.deribit_gate import (
        deribit_visible, strip_gated_anomaly_cluster)
    if deribit_visible():
        return cluster
    return strip_gated_anomaly_cluster(cluster)


def _strip_gated_metrics(entries, key):
    """Drop entries naming a Deribit-backed metric, unless the caller is admin.

    The snapshot's correlation and lead-lag lists carry raw metric
    names, and the whole thing is serialised into window.__briefData on the
    public /brief page as well as returned by the MCP snapshot tool. A
    correlation computed ON withheld data is still derived from it, so the
    name has to go too — hiding the rendered card while the JSON beside it
    still says "SKEW_25D_BTC" is not a gate.

    Fails closed by construction: leadlag_metric_allowed() resolves the caller
    through the MCP contextvar then the Flask request tier, and anything it
    cannot resolve is not the operator.
    """
    if not entries:
        return entries
    from carrac.compliance.deribit_gate import leadlag_metric_allowed
    return [e for e in entries
            if leadlag_metric_allowed(e.get(key) or '')]


@quant_bp.route('/health')
@tier_feature_required('quant_engine')
def health():
    """Collector health: last run status for each collector."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()

            # Latest run per collector
            cur.execute("""
                SELECT DISTINCT ON (collector)
                    collector, status, started_at, finished_at,
                    rows_upserted, LEFT(error_msg, 200) AS error_msg
                FROM quant.collection_log
                ORDER BY collector, started_at DESC
            """)
            collectors = _serialize(cur.fetchall())

            # Data freshness per domain
            cur.execute("""
                SELECT 'macro' as domain, COUNT(*) as rows, MAX(ts) as latest
                FROM quant.macro
                UNION ALL
                SELECT 'price', COUNT(*), MAX(ts) FROM quant.price_daily
                UNION ALL
                SELECT 'derivatives', COUNT(*), MAX(ts) FROM quant.derivatives
                UNION ALL
                SELECT 'sentiment', COUNT(*), MAX(ts) FROM quant.sentiment
                UNION ALL
                SELECT 'zscores', COUNT(*), MAX(ts) FROM quant.zscores
            """)
            freshness = _serialize(cur.fetchall())

            # Completeness from latest successful runs
            cur.execute("""
                SELECT DISTINCT ON (collector)
                    collector, metadata
                FROM quant.collection_log
                WHERE metadata IS NOT NULL
                  AND status = 'success'
                ORDER BY collector, started_at DESC
            """)
            completeness = {}
            for r in cur.fetchall():
                meta = r['metadata']
                if isinstance(meta, str):
                    import json as _json
                    meta = _json.loads(meta)
                if meta and 'expected' in meta:
                    completeness[r['collector']] = meta

        return jsonify({
            'status': 'ok',
            'collectors': collectors,
            'freshness': freshness,
            'completeness': completeness,
        })
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'status': 'error', 'error': 'Internal server error'}), 500


@quant_bp.route('/metrics')
@tier_feature_required('quant_engine')
def metrics():
    """List all available metrics with row counts and date ranges."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            result = {}

            cur.execute("""
                SELECT metric, COUNT(*) as n, MIN(ts) as first_date,
                       MAX(ts) as last_date
                FROM quant.macro GROUP BY metric ORDER BY metric
            """)
            result['macro'] = _serialize(cur.fetchall())

            cur.execute("""
                SELECT asset, source, COUNT(*) as n, MIN(ts) as first_date,
                       MAX(ts) as last_date
                FROM quant.price_daily GROUP BY asset, source ORDER BY asset
            """)
            result['price'] = _serialize(cur.fetchall())

            cur.execute("""
                SELECT asset, COUNT(*) as n, MIN(ts) as first_date,
                       MAX(ts) as last_date
                FROM quant.derivatives GROUP BY asset ORDER BY asset
            """)
            result['derivatives'] = _serialize(cur.fetchall())

            cur.execute("""
                SELECT metric, asset, COUNT(*) as n, MIN(ts) as first_date,
                       MAX(ts) as last_date
                FROM quant.sentiment GROUP BY metric, asset ORDER BY metric
            """)
            result['sentiment'] = _serialize(cur.fetchall())

        return jsonify(result)
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/series')
@tier_feature_required('quant_engine')
def series():
    """Get raw time series data.

    Query params:
        domain   -- macro|price|derivatives|sentiment|onchain (required)
        metric   -- metric name (macro/sentiment)
        asset    -- asset symbol (price/derivatives)
        from     -- start date YYYY-MM-DD
        to       -- end date YYYY-MM-DD
        limit    -- max rows (default 1000, max 5000)
    """
    domain = request.args.get('domain', '')
    metric = request.args.get('metric', '')
    asset = request.args.get('asset', '')
    date_from = request.args.get('from', '')
    date_to = request.args.get('to', '')
    try:
        limit = min(int(request.args.get('limit', 1000)), 5000)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid limit parameter'}), 400

    if not domain:
        return jsonify({'error': 'domain parameter required'}), 400

    queries = {
        'macro': ("SELECT ts, metric, value FROM quant.macro", 'metric'),
        'price': ("SELECT ts, asset, source, open, high, low, close, volume "
                  "FROM quant.price_daily", 'asset'),
        'derivatives': ("SELECT * FROM quant.derivatives", 'asset'),
        'sentiment': ("SELECT ts, metric, asset, value, source "
                     "FROM quant.sentiment", 'metric'),
        'onchain': ("SELECT ts, metric, asset, value, source "
                   "FROM quant.onchain", 'metric'),
    }

    if domain not in queries:
        return jsonify({'error': f'unknown domain: {domain}'}), 400

    base_sql, filter_col = queries[domain]
    conditions = []
    params = []

    if metric and filter_col == 'metric':
        conditions.append(f"{filter_col} = %s")
        params.append(metric)
    if asset and filter_col == 'asset':
        conditions.append(f"{filter_col} = %s")
        params.append(asset)
    if date_from:
        conditions.append("ts >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("ts <= %s")
        params.append(date_to)

    where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
    sql = f"{base_sql}{where} ORDER BY ts DESC LIMIT %s"
    params.append(limit)

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = _serialize(cur.fetchall())
        return jsonify({'domain': domain, 'count': len(rows), 'data': rows})
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/zscores')
@tier_feature_required('quant_engine')
def zscores():
    """Get current z-scores (latest available date).

    Query params:
        domain  -- filter by domain
        window  -- 30 or 90 (default 90)
        min_abs -- minimum |z-score| to return (default 0)
    """
    domain = request.args.get('domain', '')
    try:
        window = int(request.args.get('window', 90))
        min_abs = float(request.args.get('min_abs', 0))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid window or min_abs parameter'}), 400

    try:
        with get_conn() as conn:
            cur = conn.cursor()

            sql = """
                SELECT z.*
                FROM quant.zscores z
                INNER JOIN (
                    SELECT domain, metric, asset, window_days,
                           MAX(ts) as max_ts
                    FROM quant.zscores
                    WHERE window_days = %s
                    GROUP BY domain, metric, asset, window_days
                ) latest
                    ON z.ts = latest.max_ts
                    AND z.domain = latest.domain
                    AND z.metric = latest.metric
                    AND z.asset = latest.asset
                    AND z.window_days = latest.window_days
                WHERE z.window_days = %s
            """
            params = [window, window]

            if domain:
                sql += " AND z.domain = %s"
                params.append(domain)
            if min_abs > 0:
                sql += " AND ABS(z.zscore) >= %s"
                params.append(min_abs)

            sql += " ORDER BY ABS(z.zscore) DESC"
            cur.execute(sql, params)
            rows = _serialize(cur.fetchall())

        return jsonify({'window': window, 'count': len(rows), 'data': rows})
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/snapshot')
@tier_feature_required('quant_engine')
def snapshot():
    """Current state overview for morning bot / admin dashboard.

    Returns latest values, anomalies, regime, correlations, lead-lag
    signals, anomaly clusters, and top analogues — everything the
    morning bot needs for its analysis in one call.

    Query params:
        asset  — focus asset for top_correlations + leadlag_signals
                 (default 'BTC'). Whitelisted against TARGET_ASSETS to
                 prevent arbitrary strings hitting the DB.
    """
    asset = request.args.get('asset', 'BTC')
    try:
        result = build_snapshot(asset)
        if result is None:
            return jsonify({'error': 'Internal server error'}), 500
        return jsonify(result)
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


def build_snapshot(asset='BTC'):
    """Reusable body of /api/quant/snapshot.

    Returns a dict (not a Response). MCP and morning bot use this directly
    to avoid Flask request plumbing. Whitelists `asset` against TARGET_ASSETS
    to prevent arbitrary strings hitting the DB.
    """
    from quant.engine.config import TARGET_ASSETS
    asset = (asset or 'BTC').upper()
    if asset not in TARGET_ASSETS:
        asset = 'BTC'
    cache_key = 'snapshot:' + asset
    cached = _qe_cache.get(cache_key)
    if cached is not None:
        return _gate_snapshot(cached)
    try:
        with get_conn() as conn:
            cur = conn.cursor()

            # ── Latest macro values ──────────────────────────────────
            cur.execute("""
                SELECT DISTINCT ON (metric) metric, ts, value
                FROM quant.macro ORDER BY metric, ts DESC
            """)
            macro = {}
            for r in cur.fetchall():
                macro[r['metric']] = {
                    'value': float(r['value']),
                    'date': r['ts'].isoformat(),
                }

            # ── Latest sentiment ─────────────────────────────────────
            cur.execute("""
                SELECT DISTINCT ON (metric, asset) metric, asset, ts, value
                FROM quant.sentiment ORDER BY metric, asset, ts DESC
            """)
            sentiment = {}
            for r in cur.fetchall():
                key = r['metric']
                if r['asset'] != '_GLOBAL_':
                    key += f":{r['asset']}"
                sentiment[key] = {
                    'value': float(r['value']),
                    'date': r['ts'].isoformat(),
                }

            # ── Active anomalies (|z| >= 2, 90d window) ─────────────
            cur.execute("""
                SELECT z.domain, z.metric, z.asset, z.zscore,
                       z.raw_value, z.ts, z.percentile
                FROM quant.zscores z
                INNER JOIN (
                    SELECT domain, metric, asset, MAX(ts) as max_ts
                    FROM quant.zscores WHERE window_days = 90
                    GROUP BY domain, metric, asset
                ) latest
                    ON z.ts = latest.max_ts
                    AND z.domain = latest.domain
                    AND z.metric = latest.metric
                    AND z.asset = latest.asset
                WHERE z.window_days = 90 AND ABS(z.zscore) >= 2.0
                ORDER BY ABS(z.zscore) DESC
            """)
            anomalies = []
            for r in cur.fetchall():
                anomalies.append({
                    'domain': r['domain'],
                    'metric': r['metric'],
                    'asset': r['asset'],
                    'zscore': round(float(r['zscore']), 2),
                    'percentile': round(float(r['percentile']), 1) if r.get('percentile') else None,
                    'value': float(r['raw_value']) if r['raw_value'] else None,
                    'date': r['ts'].isoformat(),
                })
            # Drop degenerate (|z|>cap) + dead-feed anomalies before the report
            # slices the top-N (no LIMIT here, so the post-filter is authoritative).
            from quant.anomaly_filter import filter_anomalies
            anomalies = filter_anomalies(anomalies)

            # ── Regime ──────────────────────────────────
            regime = None
            cur.execute("""
                SELECT ts, value, metadata FROM quant.signals
                WHERE source = 'regime_hmm' AND metric = 'REGIME'
                  AND asset = '_GLOBAL_'
                ORDER BY ts DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                meta = row['metadata'] or {}
                # Derive stay_prob from the stored transition matrix so consumers
                # can surface "Regime persistence: X% daily probability of remaining
                # in current state" without a separate DB query.
                _stay_prob = None
                _tm = meta.get('transition_matrix', [])
                _state_idx = int(row['value']) if row['value'] is not None else None
                if _tm and _state_idx in (0, 1):
                    try:
                        _stay_prob = round(float(_tm[_state_idx][_state_idx]), 4)
                    except (IndexError, TypeError, ValueError):
                        _stay_prob = None
                regime = {
                    'state': meta.get('label', 'unknown'),
                    'probability': meta.get('probability', 0),
                    'duration_days': meta.get('duration_days', 0),
                    'since': meta.get('since', ''),
                    'descriptions': meta.get('state_descriptions', {}),
                    'converged': meta.get('converged'),
                    'stay_prob': _stay_prob,
                }

            # ── Regime expected durations, from transition odds ────
            # Approximate from the 2-state HMM transition matrix:
            # expected run = 1 / (1 - p_stay). Close enough for "early/mid/late"
            # context on the regime banner narrative. NOTE: the
            # key name below says "hist_median" but this is the MEAN of a
            # geometric distribution implied by CURRENT transition odds — not
            # historical, not a median. Consumers must display it as "expected
            # duration, from current transition odds", never as a "historical
            # median" (that phrase is reserved for carrac/facts/bands.py's
            # genuinely historical/median regime-clock figure). The key itself
            # is left unrenamed — it has other consumers (carrac/ai/features.py,
            # carrac/mcp/tools/analysis.py, morning_engine/data_assembly.py).
            #
            # Index→label mapping is inferred from the current row: if today's
            # stored `value` is state i and meta.label is 'favorable', then
            # row i of the transition matrix is the favorable state.
            regime_hist_median_days = {}
            if regime and meta.get('transition_matrix') and row is not None:
                tm = meta.get('transition_matrix', [])
                current_state_idx = int(row['value']) if row['value'] is not None else None
                current_label = meta.get('label')
                labels_by_idx = {}
                if current_state_idx in (0, 1) and current_label in ('favorable', 'unfavorable'):
                    labels_by_idx[current_state_idx] = current_label
                    labels_by_idx[1 - current_state_idx] = (
                        'unfavorable' if current_label == 'favorable' else 'favorable')
                try:
                    for i in range(min(len(tm), 2)):
                        p_stay = float(tm[i][i]) if tm[i] and i < len(tm[i]) else 0
                        if 0 < p_stay < 1:
                            expected_run = 1.0 / (1.0 - p_stay)
                            label = labels_by_idx.get(i, f'state_{i}')
                            regime_hist_median_days[label] = round(expected_run)
                except (IndexError, TypeError, ValueError):
                    pass

            # ── Top correlations for focus asset ─
            # Include q_value, ci_low, ci_high, significant so consumers
            # (morning report, MCP) can filter by FDR significance.
            top_correlations = []
            cur.execute("""
                SELECT metric_a, metric_b, correlation, p_value, stability,
                       q_value, ci_low, ci_high, significant
                FROM quant.correlations
                WHERE (metric_a = %s OR metric_b = %s)
                  AND method = 'spearman' AND window_days = 60
                  AND ts = (SELECT MAX(ts) FROM quant.correlations
                            WHERE method = 'spearman')
                  AND ABS(correlation) >= 0.15
                ORDER BY ABS(correlation) DESC
                LIMIT 10
            """, (asset, asset))
            for r in cur.fetchall():
                other = (r['metric_b'] if r['metric_a'] == asset
                         else r['metric_a'])
                entry = {
                    'metric': other,
                    'correlation': round(float(r['correlation']), 3),
                }
                if r.get('stability') is not None:
                    entry['stability'] = round(float(r['stability']), 3)
                if r.get('q_value') is not None:
                    entry['q_value'] = round(float(r['q_value']), 4)
                if r.get('ci_low') is not None:
                    entry['ci_low'] = round(float(r['ci_low']), 3)
                    entry['ci_high'] = round(float(r['ci_high']), 3)
                if r.get('significant') is not None:
                    entry['significant'] = bool(r['significant'])
                top_correlations.append(entry)

            # ── Lead-lag signals for focus asset ────────
            # projected_move_z = r × leader_z — a linear association estimate in
            # target-sigma units (not a predictive interval).
            # projected_ci_z = 95% CI of the *correlation coefficient* scaled by
            # leader_z — an association band, not a forecast interval.
            # best-lag is selected by exploratory scan over many lags; treat as
            # hypothesis. The `persistent` flag marks signals stable across sub-windows.
            leadlag_signals = []
            leadlag_ci_note = (
                "band = 95% CI of the correlation coefficient scaled by leader z "
                "— an association band, not a predictive interval"
            )
            cur.execute("""
                SELECT metric_a, window_days AS lag, correlation, p_value,
                       ci_low, ci_high
                FROM quant.correlations
                WHERE metric_b = %s AND method = 'leadlag'
                  AND ts = (SELECT MAX(ts) FROM quant.correlations
                            WHERE method = 'leadlag')
                ORDER BY metric_a, ABS(correlation) DESC
            """, (asset,))
            rows = cur.fetchall()
            # Group by leader, pick best lag per leader
            from collections import defaultdict
            import json as _snap_json
            by_leader = defaultdict(list)
            for r in rows:
                by_leader[r['metric_a']].append(r)

            # Batch-fetch leader z-scores (90d window) so we don't round-trip per row
            leader_keys = list(by_leader.keys())
            leader_z_by_key = {}
            if leader_keys:
                cur.execute("""
                    SELECT DISTINCT ON (metric) metric, zscore
                    FROM quant.zscores
                    WHERE metric = ANY(%s) AND window_days = 90
                    ORDER BY metric, ts DESC
                """, (leader_keys,))
                for zr in cur.fetchall():
                    leader_z_by_key[zr['metric']] = float(zr['zscore'])

            # Batch-fetch persistence flags + p_corrected + n_lags_tested from
            # leadlag_summary signals (stored by leadlag engine each nightly run).
            leader_persistent = {}
            leader_p_corrected = {}
            leader_n_lags_tested = {}
            if leader_keys:
                try:
                    cur.execute("""
                        SELECT metric, metadata FROM quant.signals
                        WHERE source = 'leadlag_summary'
                          AND metric LIKE %s
                          AND ts = (SELECT MAX(ts) FROM quant.signals
                                    WHERE source = 'leadlag_summary')
                    """, (f'%__{asset}',))
                    for sr in cur.fetchall():
                        leader_name = sr['metric'].split('__')[0]
                        meta = sr['metadata'] if isinstance(sr['metadata'], dict) \
                            else _snap_json.loads(sr['metadata'])
                        leader_persistent[leader_name] = meta.get('persistent')
                        if meta.get('p_corrected') is not None:
                            leader_p_corrected[leader_name] = round(
                                float(meta['p_corrected']), 4)
                        if meta.get('n_lags_tested') is not None:
                            leader_n_lags_tested[leader_name] = int(
                                meta['n_lags_tested'])
                except Exception:
                    pass  # Persistence summaries may not exist yet

            for leader, lrows in by_leader.items():
                best = max(lrows, key=lambda r: abs(float(r['correlation'])))
                if abs(float(best['correlation'])) >= 0.05:
                    r_val = float(best['correlation'])
                    entry = {
                        'leader': leader,
                        'lag_days': best['lag'],
                        'correlation': round(r_val, 3),
                    }
                    persistent_val = leader_persistent.get(leader)
                    if persistent_val is not None:
                        entry['persistent'] = bool(persistent_val)
                    pc = leader_p_corrected.get(leader)
                    if pc is not None:
                        entry['p_corrected'] = pc
                    nl = leader_n_lags_tested.get(leader)
                    if nl is not None:
                        entry['n_lags_tested'] = nl
                    lz = leader_z_by_key.get(leader)
                    if lz is not None:
                        entry['leader_z'] = round(lz, 2)
                        entry['projected_move_z'] = round(r_val * lz, 3)
                        if best.get('ci_low') is not None and best.get('ci_high') is not None:
                            lo, hi = float(best['ci_low']) * lz, float(best['ci_high']) * lz
                            entry['projected_ci_z'] = [round(min(lo, hi), 3), round(max(lo, hi), 3)]
                    leadlag_signals.append(entry)
            leadlag_signals.sort(
                key=lambda d: abs(d['correlation']), reverse=True)

            # ── Weekly lead-lag signals for focus asset ───
            leadlag_signals_weekly = []
            try:
                cur.execute("""
                    SELECT metric_a, window_days AS lag, correlation, p_value
                    FROM quant.correlations
                    WHERE metric_b = %s AND method = 'leadlag_weekly'
                      AND ts = (SELECT MAX(ts) FROM quant.correlations
                                WHERE method = 'leadlag_weekly')
                    ORDER BY metric_a, ABS(correlation) DESC
                """, (asset,))
                wrows = cur.fetchall()
                w_by_leader = defaultdict(list)
                for r in wrows:
                    w_by_leader[r['metric_a']].append(r)
                for leader, lrows in w_by_leader.items():
                    best = max(lrows, key=lambda r: abs(float(r['correlation'])))
                    if abs(float(best['correlation'])) >= 0.05:
                        leadlag_signals_weekly.append({
                            'leader': leader,
                            'lag_days': best['lag'] * 7,  # weeks → days
                            'correlation': round(float(best['correlation']), 3),
                        })
                leadlag_signals_weekly.sort(
                    key=lambda d: abs(d['correlation']), reverse=True)
            except Exception:
                pass  # Weekly data may not exist yet

            # ── Active anomaly cluster ──────────────────
            anomaly_cluster = None
            cur.execute("""
                SELECT ts, zscore, metadata FROM quant.anomalies
                WHERE domain = 'cluster' AND metric = 'MULTI_DOMAIN'
                  AND resolved_at IS NULL
                ORDER BY ts DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                meta = row['metadata'] or {}
                anomaly_cluster = {
                    'date': row['ts'].isoformat(),
                    'extreme_count': int(row['zscore']),
                    'domains_affected': meta.get('domains_affected', 0),
                    'metrics': meta.get('metrics', []),
                }

            # ── Transmission chain summaries ──────────────
            chains = []
            try:
                from quant.engine.config import TRANSMISSION_CHAINS
                import json as _json
                import math as _math

                # Fetch all leadlag summaries at once
                cur.execute("""
                    SELECT metric, value, metadata FROM quant.signals
                    WHERE source = 'leadlag_summary'
                      AND ts = (SELECT MAX(ts) FROM quant.signals
                                WHERE source = 'leadlag_summary')
                """)
                all_summaries = {}
                for r in cur.fetchall():
                    meta = r['metadata'] if isinstance(r['metadata'], dict) \
                        else _json.loads(r['metadata'])
                    all_summaries[r['metric']] = meta.get('best_r', 0)

                for cname, cdef in TRANSMISSION_CHAINS.items():
                    abs_rs = []
                    weakest = {'from': '', 'to': '', 'r': 999}
                    for a, b in cdef['links']:
                        r_val = all_summaries.get(f'{a}__{b}', 0)
                        ar = max(abs(r_val), 0.001)
                        abs_rs.append(ar)
                        if ar < abs(weakest['r']):
                            weakest = {'from': a, 'to': b, 'r': round(r_val, 4)}
                    geo = _math.exp(sum(_math.log(r) for r in abs_rs) / len(abs_rs))
                    chains.append({
                        'chain': cname,
                        'chain_strength': round(geo, 4),
                        'weakest_link': weakest,
                    })
            except Exception:
                pass  # Chain data may not exist yet

            # ── MOVE bond-vol block ──────────────────────────────────
            # MOVE (ICE BofA Bond VIX) lives in quant.price_daily /
            # quant.zscores domain='price_vol'. Updated daily.
            #
            # Label thresholds derived from the full MOVE history
            # (2002–2026, n=5830):
            #   p25 = 63.7   p50 = 80.3   p70 = 99.8
            #   p90 = 126.6  p95 = 142.5  max = 264.6
            # calm      = percentile < 70   (below p70 ≈ 100)
            # elevated  = 70 ≤ percentile < 90  (100–127)
            # stress    = percentile ≥ 90  (above p90 ≈ 127)
            # These align with MOVE's known regimes: <100 = benign
            # pre-COVID world; 100-130 = rate-vol repricing periods;
            # >130 = acute stress (2020 covid, 2023 SVB, 2022 hikes).
            bond_vol = None
            try:
                # Latest MOVE close and 5-day change
                cur.execute("""
                    SELECT ts, close FROM quant.price_daily
                    WHERE asset = 'MOVE' AND source = 'yahoo'
                    ORDER BY ts DESC LIMIT 6
                """)
                move_rows = cur.fetchall()
                if move_rows:
                    latest_move = move_rows[0]
                    move_level = float(latest_move['close'])
                    move_date = (latest_move['ts'].isoformat()
                                 if hasattr(latest_move['ts'], 'isoformat')
                                 else str(latest_move['ts']))
                    # 5-day change: compare today vs 5 trading days ago
                    move_5d_change = None
                    if len(move_rows) >= 6:
                        move_5d_change = round(
                            move_level - float(move_rows[5]['close']), 2)

                    # z-score + percentile from quant.zscores (price_vol domain)
                    cur.execute("""
                        SELECT zscore, percentile
                        FROM quant.zscores
                        WHERE domain = 'price_vol' AND metric = 'MOVE'
                          AND window_days = 90
                        ORDER BY ts DESC LIMIT 1
                    """)
                    zrow = cur.fetchone()
                    move_z = round(float(zrow['zscore']), 4) if zrow else None
                    move_pct = (round(float(zrow['percentile']), 1)
                                if zrow and zrow['percentile'] is not None
                                else None)

                    # Label from percentile (full-history distribution)
                    if move_pct is None:
                        move_label = 'unknown'
                    elif move_pct >= 90.0:
                        move_label = 'stress'
                    elif move_pct >= 70.0:
                        move_label = 'elevated'
                    else:
                        move_label = 'calm'

                    bond_vol = {
                        'level': round(move_level, 2),
                        'zscore': move_z,
                        'percentile': move_pct,
                        '5d_change': move_5d_change,
                        'label': move_label,
                        'date': move_date,
                    }
            except Exception:
                pass  # MOVE data may not be z-scored yet (first run)

        result = {
            'asset': asset,
            'macro': macro,
            'sentiment': sentiment,
            # Anomalies carry the raw column VALUE alongside the
            # metric name, so an ungated row publishes the barred number
            # itself. anomaly_count is derived from the filtered list so the
            # headline cannot advertise rows the caller is not shown.
            # UNGATED here on purpose — see _gate_snapshot. The cache key
            # ('snapshot:BTC') carries no caller identity, so anything gated
            # before the cache write becomes whichever caller happened to miss
            # first, served to everyone else for the next 5 minutes.
            'anomalies': anomalies,
            'anomaly_count': len(anomalies or []),
            'regime': regime,
            'regime_hist_median_days': regime_hist_median_days,
            # These three carry raw metric NAMES (GEX_BTC,
            # SKEW_25D_BTC, ...) and land in two published places — the MCP
            # snapshot, and window.__briefData on the public /brief page, where
            # the whole report object is serialised via `report | tojson`.
            # Gating the rendered correlation CARDS was not enough: the JSON
            # island beside them still named the metrics.
            #
            # Filtered here at the single return site rather than at each
            # append, so a future branch that adds an entry cannot bypass it.
            'top_correlations': top_correlations,
            'leadlag_signals': leadlag_signals,
            'leadlag_ci_note': leadlag_ci_note,
            'leadlag_signals_weekly': leadlag_signals_weekly,
            # derivatives_leverage is built on BASIS_BTC and
            # SKEW_25D_BTC, so both its label and its strength are derived.
            'chains': chains,
            # this sat unfiltered beside the
            # gated siblings above and carried the same metric names — plus
            # SKEW_25D_BTC, which the anomalies filter DID catch. So the gated
            # list would hide a row the cluster beside it published anyway. The helper
            # recomputes extreme_count and domains_affected, since leaving them
            # would advertise the withheld rows.
            'anomaly_cluster': anomaly_cluster,
            'bond_vol': bond_vol,
        }
        _qe_cache.set(cache_key, result)
        return _gate_snapshot(result)
    except Exception as e:
        logger.error(f"[quant] build_snapshot failed: {e}", exc_info=True)
        return None


@quant_bp.route('/correlations')
@tier_feature_required('quant_engine')
def correlations():
    """Get Spearman correlations for a specific metric.

    Shows what other canonical metrics are correlated with the target,
    ranked by absolute correlation strength.

    Query params:
        metric   -- canonical metric name, e.g. BTC, VIX, M2 (required)
        window   -- 60 or 120 days (default 60)
        min_abs  -- minimum |correlation| to return (default 0.3)
        max_p    -- maximum p-value for significance (default 0.05)
        date     -- specific date YYYY-MM-DD (default: latest available)
        regime   -- 'favorable' or 'unfavorable' for regime-dependent correlations
    """
    metric = request.args.get('metric', '')
    try:
        window = int(request.args.get('window', 60))
        min_abs = float(request.args.get('min_abs', 0.3))
        max_p = float(request.args.get('max_p', 0.05))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid numeric parameter'}), 400
    target_date = request.args.get('date', '')
    matrix_mode = request.args.get('matrix', '').lower() == 'true'
    regime = request.args.get('regime', '').lower()
    method = f'spearman_{regime}' if regime in ('favorable', 'unfavorable') else 'spearman'

    # Matrix mode: return all pairs for heatmap (no metric filter)
    if matrix_mode:
        cache_key = f'corr:{method}:{window}'
        cached = _qe_cache.get(cache_key)
        if cached is not None:
            return jsonify(cached)
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT metric_a, metric_b, correlation, p_value,
                           q_value, ci_low, ci_high, significant, n_obs
                    FROM quant.correlations
                    WHERE method = %s AND window_days = %s
                      AND ts = (SELECT MAX(ts) FROM quant.correlations
                                WHERE method = %s)
                    ORDER BY metric_a, metric_b
                """, [method, window, method])
                rows = _serialize(cur.fetchall())
            result = {
                'window': window,
                'count': len(rows),
                'data': rows,
            }
            _qe_cache.set(cache_key, result)
            return jsonify(result)
        except Exception as e:
            logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500

    if not metric:
        # Return list of available canonical metrics
        from quant.engine.config import CANONICAL_METRICS
        return jsonify({
            'error': 'metric parameter required',
            'available': sorted(CANONICAL_METRICS.keys()),
        }), 400

    try:
        with get_conn() as conn:
            cur = conn.cursor()

            params = [metric, metric, window, method, min_abs, max_p]

            if target_date:
                date_filter = "AND c.ts = %s"
                params.append(target_date)
            else:
                date_filter = ("AND c.ts = ("
                               "  SELECT MAX(ts) FROM quant.correlations"
                               "  WHERE method = %s)")
                params.append(method)

            cur.execute(f"""
                SELECT c.ts, c.metric_a, c.metric_b,
                       c.correlation, c.p_value, c.n_obs,
                       c.q_value, c.ci_low, c.ci_high, c.significant
                FROM quant.correlations c
                WHERE (c.metric_a = %s OR c.metric_b = %s)
                  AND c.window_days = %s
                  AND c.method = %s
                  AND ABS(c.correlation) >= %s
                  AND c.p_value <= %s
                  {date_filter}
                ORDER BY ABS(c.correlation) DESC
            """, params)

            rows = []
            for r in cur.fetchall():
                other = (r['metric_b'] if r['metric_a'] == metric
                         else r['metric_a'])
                rows.append({
                    'metric': other,
                    'correlation': round(float(r['correlation']), 4),
                    'p_value': float(r['p_value']),
                    'n_obs': r['n_obs'],
                    'date': r['ts'].isoformat(),
                })

        result = {
            'target': metric,
            'window': window,
            'min_abs': min_abs,
            'count': len(rows),
            'data': rows,
        }
        if regime:
            result['regime'] = regime
        return jsonify(result)
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/leadlag')
@tier_feature_required('quant_engine')
def leadlag():
    """Get lead-lag relationships: what metrics lead a target.

    For each configured focus pair where the target matches, returns
    the lag with the strongest |correlation| from the full
    cross-correlogram.

    Query params:
        target     -- target metric, e.g. BTC, ETH, SPX (required)
        date       -- analysis date YYYY-MM-DD (default: latest)
        frequency  -- 'daily' or 'weekly' (default: daily)
    """
    target = request.args.get('target', '')
    target_date = request.args.get('date', '')
    freq = request.args.get('frequency', 'daily')
    ll_method = 'leadlag_weekly' if freq == 'weekly' else 'leadlag'
    ll_summary = 'leadlag_weekly_summary' if freq == 'weekly' else 'leadlag_summary'

    if not target:
        from quant.engine.config import LEADLAG_PAIRS
        targets = sorted(set(t for _, t, _, _ in LEADLAG_PAIRS))
        return jsonify({
            'error': 'target parameter required',
            'available_targets': targets,
        }), 400

    try:
        from quant.engine.config import LEADLAG_PAIRS

        # Build lookup: (leader, target) -> description, lag_range
        pair_info = {}
        for leader, tgt, lag_range, desc in LEADLAG_PAIRS:
            if tgt == target:
                pair_info[leader] = {'desc': desc, 'lag_range': lag_range}

        if not pair_info:
            return jsonify({
                'error': f'no configured lead-lag pairs for target {target}',
                'available_targets': sorted(set(t for _, t, _, _ in LEADLAG_PAIRS)),
            }), 400

        with get_conn() as conn:
            cur = conn.cursor()

            if target_date:
                date_filter = "AND c.ts = %s"
                date_params = [target_date]
            else:
                date_filter = ("AND c.ts = ("
                               "  SELECT MAX(ts) FROM quant.correlations"
                               "  WHERE method = %s"
                               ")")
                date_params = [ll_method]

            # Fetch all leadlag rows for this target
            cur.execute(f"""
                SELECT c.ts, c.metric_a, c.window_days AS lag,
                       c.correlation, c.p_value, c.n_obs
                FROM quant.correlations c
                WHERE c.metric_b = %s
                  AND c.method = %s
                  AND c.metric_a = ANY(%s)
                  {date_filter}
                ORDER BY c.metric_a, c.window_days
            """, [target, ll_method, list(pair_info.keys())] + date_params)

            rows = cur.fetchall()

            # Fetch persistence summaries
            persistence = {}
            try:
                cur.execute("""
                    SELECT metric, metadata FROM quant.signals
                    WHERE source = %s
                      AND metric LIKE %s
                      AND ts = (SELECT MAX(ts) FROM quant.signals
                                WHERE source = %s)
                """, (ll_summary, f'%__{target}', ll_summary))
                import json as _json
                for r in cur.fetchall():
                    leader_name = r['metric'].split('__')[0]
                    meta = r['metadata'] if isinstance(r['metadata'], dict) else _json.loads(r['metadata'])
                    persistence[leader_name] = meta
            except Exception:
                pass  # Summaries may not exist yet

        # Group by leader, find best lag per leader
        from collections import defaultdict
        by_leader = defaultdict(list)
        for r in rows:
            by_leader[r['metric_a']].append(r)

        result_date = None
        data = []
        for leader, leader_rows in by_leader.items():
            # Find lag with highest |correlation|
            best = max(leader_rows, key=lambda r: abs(float(r['correlation'])))
            info = pair_info.get(leader, {})
            result_date = best['ts'].isoformat()
            pmeta = persistence.get(leader, {})
            # For weekly frequency, convert lag from weeks to days in response
            lag_mult = 7 if freq == 'weekly' else 1
            item = {
                'leader': leader,
                'best_lag': best['lag'] * lag_mult,
                'best_correlation': round(float(best['correlation']), 4),
                'best_p_value': float(best['p_value']),
                'n_obs': best['n_obs'],
                'lag_range': list(info.get('lag_range', [0, 0])),
                'description': info.get('desc', ''),
                'persistent': pmeta.get('persistent', None),
            }
            if freq == 'weekly':
                item['frequency'] = 'weekly'
            if pmeta:
                item['sub_windows'] = {
                    'sub1_lag': pmeta.get('sub1_lag', 0) * lag_mult,
                    'sub1_r': pmeta.get('sub1_r'),
                    'sub2_lag': pmeta.get('sub2_lag', 0) * lag_mult,
                    'sub2_r': pmeta.get('sub2_r'),
                }
            data.append(item)

        # Sort by |correlation| descending
        data.sort(key=lambda d: abs(d['best_correlation']), reverse=True)

        return jsonify({
            'target': target,
            'date': result_date,
            'count': len(data),
            'data': data,
        })
    except ValueError as e:
        logger.warning(f"[quant] {request.path} bad params: {e}")
        return jsonify({'error': 'Invalid request parameters'}), 400
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/leadlag-pair')
@tier_feature_required('quant_engine')
def leadlag_pair():
    """Full cross-correlogram for a single (leader, target) pair.

    Used by the Intelligence chain drill-through: click the
    weakest link in a chain → see r at every lag for that pair.

    Query params:
        leader     — leader metric (required)
        target     — target metric (required)
        frequency  — 'daily' or 'weekly' (default 'daily')
    """
    leader = request.args.get('leader', '').strip()
    target = request.args.get('target', '').strip()
    if not leader or not target:
        return jsonify({'error': 'leader and target parameters required'}), 400
    freq = request.args.get('frequency', 'daily')
    method = 'leadlag_weekly' if freq == 'weekly' else 'leadlag'
    lag_mult = 7 if freq == 'weekly' else 1

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT window_days AS lag, correlation, p_value, n_obs,
                       ci_low, ci_high
                FROM quant.correlations
                WHERE metric_a = %s AND metric_b = %s AND method = %s
                  AND ts = (SELECT MAX(ts) FROM quant.correlations
                            WHERE method = %s)
                ORDER BY window_days
            """, (leader, target, method, method))
            rows = cur.fetchall()
            if not rows:
                return jsonify({
                    'leader': leader,
                    'target': target,
                    'correlogram': [],
                    'best': None,
                }), 200

            correlogram = []
            for r in rows:
                entry = {
                    'lag': int(r['lag']) * lag_mult,
                    'correlation': round(float(r['correlation']), 4),
                }
                if r.get('ci_low') is not None:
                    entry['ci_low'] = round(float(r['ci_low']), 4)
                    entry['ci_high'] = round(float(r['ci_high']), 4)
                correlogram.append(entry)
            best = max(correlogram, key=lambda x: abs(x['correlation']))
        return jsonify({
            'leader': leader,
            'target': target,
            'frequency': freq,
            'correlogram': correlogram,
            'best': best,
        })
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/chain')
@tier_feature_required('quant_engine')
def chain():
    """Transmission chain analysis — see correlation at each link.

    Returns each link's best lag and correlation, plus aggregate
    chain strength (geometric mean of |r|) and the weakest link.

    Query params:
        chain      -- monetary_policy|dollar_liquidity|risk_appetite (required)
        frequency  -- 'daily' or 'weekly' (default: daily)
    """
    from quant.engine.config import TRANSMISSION_CHAINS

    chain_name = request.args.get('chain', '')
    freq = request.args.get('frequency', 'daily')
    summary_source = 'leadlag_weekly_summary' if freq == 'weekly' else 'leadlag_summary'

    if not chain_name or chain_name not in TRANSMISSION_CHAINS:
        return jsonify({
            'error': 'chain parameter required',
            'available': sorted(TRANSMISSION_CHAINS.keys()),
        }), 400

    chain_def = TRANSMISSION_CHAINS[chain_name]

    try:
        with get_conn() as conn:
            cur = conn.cursor()

            # Fetch all lead-lag summaries for the relevant links
            link_metrics = [f'{a}__{b}' for a, b in chain_def['links']]
            cur.execute("""
                SELECT metric, value, metadata FROM quant.signals
                WHERE source = %s
                  AND metric = ANY(%s)
                  AND ts = (SELECT MAX(ts) FROM quant.signals
                            WHERE source = %s)
            """, (summary_source, link_metrics, summary_source))

            import json as _json
            summaries = {}
            for r in cur.fetchall():
                meta = r['metadata'] if isinstance(r['metadata'], dict) \
                    else _json.loads(r['metadata'])
                summaries[r['metric']] = {
                    'lag': int(r['value']),
                    'r': meta.get('best_r', 0),
                    'persistent': meta.get('persistent'),
                    'sub1_r': meta.get('sub1_r'),
                    'sub2_r': meta.get('sub2_r'),
                }

        lag_mult = 7 if freq == 'weekly' else 1
        links = []
        abs_rs = []
        for a, b in chain_def['links']:
            key = f'{a}__{b}'
            s = summaries.get(key, {})
            r_val = s.get('r', 0)
            abs_rs.append(abs(r_val) if r_val else 0.001)
            links.append({
                'from': a,
                'to': b,
                'best_lag': s.get('lag', 0) * lag_mult,
                'correlation': round(r_val, 4) if r_val else None,
                'persistent': s.get('persistent'),
            })

        # Chain strength: geometric mean of |r|
        import math
        geo_mean = math.exp(sum(math.log(r) for r in abs_rs) / len(abs_rs))

        # Weakest link
        weakest = min(links, key=lambda l: abs(l.get('correlation') or 0))

        return jsonify({
            'chain': chain_name,
            'description': chain_def['description'],
            'frequency': freq,
            'chain_strength': round(geo_mean, 4),
            'weakest_link': {
                'from': weakest['from'],
                'to': weakest['to'],
                'r': weakest.get('correlation'),
            },
            'links': links,
        })
    except Exception as e:
        logger.error(f"[quant] chain failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/chain-history')
@tier_feature_required('quant_engine')
def chain_history():
    """Chain strength time series for sparklines and trend analysis.

    Query params:
        chain  -- monetary_policy|dollar_liquidity|risk_appetite (required)
        days   -- number of days to return (default 30, max 365)
    """
    chain_name = request.args.get('chain', '')
    try:
        days = min(int(request.args.get('days', 30)), 365)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid days parameter'}), 400

    valid_chains = ['monetary_policy', 'dollar_liquidity', 'risk_appetite',
                    'derivatives_leverage']
    if chain_name not in valid_chains:
        return jsonify({'error': 'Invalid chain', 'valid': valid_chains}), 400

    cache_key = f'chain_history:{chain_name}:{days}'
    cached = _qe_cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT ts, value, metadata FROM quant.signals
                WHERE source = 'chain_strength' AND metric = %s
                  AND asset = '_GLOBAL_'
                ORDER BY ts DESC LIMIT %s
            """, (chain_name, days))
            rows = cur.fetchall()

        import json as _json
        result = []
        for r in rows:
            meta = r['metadata'] if isinstance(r['metadata'], dict) \
                else _json.loads(r['metadata']) if r['metadata'] else {}
            result.append({
                'date': r['ts'].isoformat() if hasattr(r['ts'], 'isoformat') else str(r['ts']),
                'strength': float(r['value']),
                'weakest_link': meta.get('weakest_link'),
            })

        _qe_cache.set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"[quant] chain-history failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/regime')
@tier_feature_required('quant_engine')
def regime():
    """Current market regime from HMM + PELT changepoints.

    Returns the current state (favorable/unfavorable), probability,
    duration, learned state parameters, and structural changepoints.

    Query params:
        history  -- if 'true', include full state time series (default: false)
    """
    include_history = request.args.get('history', '').lower() == 'true'
    cache_key = 'regime:history' if include_history else 'regime'
    cached = _qe_cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        with get_conn() as conn:
            cur = conn.cursor()

            # Get the latest day's regime signal (has full model info in metadata)
            cur.execute("""
                SELECT ts, value, metadata
                FROM quant.signals
                WHERE source = 'regime_hmm' AND metric = 'REGIME'
                  AND asset = '_GLOBAL_'
                ORDER BY ts DESC LIMIT 1
            """)
            latest = cur.fetchone()

            if not latest:
                return jsonify({'error': 'no regime data available'}), 404

            meta = latest['metadata'] if latest['metadata'] else {}

            result = {
                'current': {
                    'state': meta.get('label', 'unknown'),
                    'probability': meta.get('probability', 0),
                    'duration_days': meta.get('duration_days', 0),
                    'since': meta.get('since', ''),
                },
                'states': meta.get('state_descriptions', {}),
                'state_means': meta.get('state_means', {}),
                'transition_matrix': meta.get('transition_matrix', []),
                'historical_pct': meta.get('historical_pct', {}),
                'changepoints': meta.get('changepoints', []),
                'date': latest['ts'].isoformat(),
            }

            if include_history:
                cur.execute("""
                    SELECT ts, value, metadata
                    FROM quant.signals
                    WHERE source = 'regime_hmm' AND metric = 'REGIME'
                      AND asset = '_GLOBAL_'
                    ORDER BY ts
                """)
                history = []
                for r in cur.fetchall():
                    m = r['metadata'] if r['metadata'] else {}
                    history.append({
                        'date': r['ts'].isoformat(),
                        'state': m.get('label', 'unknown'),
                        'probability': m.get('probability', 0),
                    })
                result['history'] = history

        _qe_cache.set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/derivatives-regime')
@tier_feature_required('quant_engine')
def derivatives_regime():
    """Current derivatives regime classification.

    Returns the classified regime, confidence, metrics snapshot,
    implications, and transition warnings.
    """
    cached = _qe_cache.get('derivatives_regime')
    if cached is not None:
        return jsonify(cached)

    try:
        from quant.engine.derivatives_regime import classify_derivatives_regime

        with get_conn() as conn:
            cur = conn.cursor()

            result = {'by_asset': {}}

            for asset in ('BTC', 'ETH'):
                cur.execute("""
                    SELECT ts, basis_pct, skew_25d, dvol, iv_rv_premium,
                           options_put_call_ratio, options_oi_usd, options_max_pain,
                           options_iv_atm, term_structure_slope, iv_7d, iv_30d, iv_90d,
                           gex_net, gex_flip_price, funding_rate_avg
                    FROM quant.derivatives WHERE asset = %s
                    ORDER BY ts DESC LIMIT 1
                """, (asset,))
                row = cur.fetchone()
                if not row:
                    continue

                metrics = {k: float(v) for k, v in row.items()
                           if v is not None and k != 'ts'}
                metrics_out = {k: v for k, v in metrics.items()}
                metrics_out['ts'] = str(row['ts'])

                result['by_asset'][asset] = metrics_out

            # Classify BTC regime
            btc = result['by_asset'].get('BTC', {})
            if btc:
                regime = classify_derivatives_regime({
                    'basis_apr': btc.get('basis_pct'),
                    'skew_25d': btc.get('skew_25d'),
                    'dvol': btc.get('dvol'),
                    'iv_rv_premium': btc.get('iv_rv_premium'),
                    'put_call_ratio': btc.get('options_put_call_ratio'),
                    'term_slope': btc.get('term_structure_slope'),
                    'gex_net': btc.get('gex_net'),
                    'gex_flip_price': btc.get('gex_flip_price'),
                })
                result['regime'] = regime
            else:
                result['regime'] = {'regime': 'unknown', 'confidence': 0}

        _qe_cache.set('derivatives_regime', result, ttl=300)
        return jsonify(result)

    except Exception as e:
        logger.error("derivatives-regime failed: %s", e, exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/conditional')
@tier_feature_required('quant_engine')
def conditional():
    """Conditional return distributions based on z-score conditions.

    "When conditions X, Y, Z held simultaneously, what was the N-day
    forward return distribution for the target asset?"

    Query params:
        conditions -- comma-separated, format METRIC:op:value
                      ops: gt, lt, gte, lte (applied to 90d z-score)
                      Example: VIX:gt:2,HY_SPREAD:gt:1.5
        target     -- asset for forward returns (default: BTC)
        horizon    -- forward return horizon in days (default: 30)
    """
    conditions_raw = request.args.get('conditions', '')
    target = request.args.get('target', 'BTC')
    try:
        horizon = int(request.args.get('horizon', 30))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid horizon parameter'}), 400

    if not conditions_raw:
        return jsonify({
            'error': 'conditions parameter required',
            'format': 'METRIC:op:value (comma-separated)',
            'example': 'VIX:gt:2,HY_SPREAD:gt:1.5',
            'operators': ['gt', 'lt', 'gte', 'lte'],
        }), 400

    conditions = [c.strip() for c in conditions_raw.split(',') if c.strip()]

    detail = request.args.get('detail', '').lower() == 'true'

    try:
        from quant.engine.conditional import query_conditional
        result = query_conditional(conditions, target=target, horizon=horizon,
                                   include_returns=detail)
        return jsonify(result)
    except ValueError as e:
        logger.warning(f"[quant] {request.path} bad params: {e}")
        return jsonify({'error': 'Invalid request parameters'}), 400
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/analogues')
@tier_feature_required('quant_engine')
def analogues():
    """Historical analogue search: find past dates most similar to today.

    Compares z-score vectors using cosine similarity across 9 core
    metrics spanning macro, sentiment, and derivatives domains.

    Query params:
        target -- asset for forward returns (default: BTC)
        top    -- number of analogues to return (default: 5)
        date   -- reference date YYYY-MM-DD (default: latest)
    """
    target = request.args.get('target', 'BTC')
    try:
        top_n = int(request.args.get('top', 5))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid top parameter'}), 400
    ref_date = request.args.get('date', '')

    cache_key = f'analogues:{target}:{top_n}'
    if not ref_date:
        cached = _qe_cache.get(cache_key)
        if cached is not None:
            return jsonify(cached)

    try:
        from quant.engine.conditional import query_analogues
        result = query_analogues(
            target=target,
            top_n=top_n,
            reference_date=ref_date or None,
        )
        if not ref_date:
            _qe_cache.set(cache_key, result)
        return jsonify(result)
    except ValueError as e:
        logger.warning(f"[quant] {request.path} bad params: {e}")
        return jsonify({'error': 'Invalid request parameters'}), 400
    except Exception as e:
        logger.error(f"[quant] {request.path} failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/events')
@tier_feature_required('quant_engine')
def events():
    """Upcoming and recent macro events from quant.events table.

    Query params:
        from  -- start date YYYY-MM-DD (default: all)
        to    -- end date YYYY-MM-DD (default: all)

    Returns actual + actual_filled_at when filled by fill_event_actuals().
    """
    from_date = request.args.get('from', '')
    to_date = request.args.get('to', '')

    # Don't cache today's events (actuals may be filled mid-day)
    from datetime import datetime as _dt, timezone as _tz
    today_str = _dt.now(_tz.utc).strftime('%Y-%m-%d')
    is_today_query = (from_date == today_str and to_date == today_str)

    cache_key = f'events:{from_date}:{to_date}'
    if not is_today_query:
        cached = _qe_cache.get(cache_key)
        if cached is not None:
            return jsonify(cached)

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            sql = ("SELECT id, ts, event_type, title, impact, "
                   "expected, actual, actual_filled_at FROM quant.events")
            params = []
            clauses = []
            if from_date:
                clauses.append("ts >= %s")
                params.append(from_date)
            if to_date:
                clauses.append("ts <= %s")
                params.append(to_date)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY ts"
            cur.execute(sql, params)
            result = _serialize(cur.fetchall())

        if not is_today_query:
            _qe_cache.set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"[quant] events failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/events/fill-actuals', methods=['POST', 'GET'])
@tier_feature_required('quant_engine')
def fill_actuals():
    """Trigger fill_event_actuals() for a given date.

    Query params / POST body:
        date  -- YYYY-MM-DD (default: today UTC)

    Returns today's events (with actuals) after filling.
    This endpoint is called by the afternoon engine before building
    today_calendar_text so the LLM receives the actual printed values.
    """
    from datetime import datetime as _dt, timezone as _tz

    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        date = body.get('date', '') or request.args.get('date', '')
    else:
        date = request.args.get('date', '')

    if not date:
        date = _dt.now(_tz.utc).strftime('%Y-%m-%d')

    # Validate date format
    try:
        _dt.strptime(date, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Invalid date format; expected YYYY-MM-DD'}), 400

    try:
        from quant.collectors.event_calendar import fill_event_actuals
        filled = fill_event_actuals(date)
    except Exception as e:
        logger.error("[quant] fill-actuals failed: %s", e, exc_info=True)
        return jsonify({'error': 'Internal server error', 'detail': str(e)}), 500

    # Return today's events (with actuals now populated)
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, ts, event_type, title, impact,
                       expected, actual, actual_filled_at
                FROM quant.events
                WHERE ts = %s
                ORDER BY ts
            """, (date,))
            evts = _serialize(cur.fetchall())
        return jsonify({'date': date, 'filled': filled, 'events': evts})
    except Exception as e:
        logger.error("[quant] fill-actuals post-query failed: %s", e, exc_info=True)
        return jsonify({'date': date, 'filled': filled, 'events': [], 'warning': str(e)})


@quant_bp.route('/anomalies')
@tier_feature_required('quant_engine')
def anomaly_history():
    """Historical anomaly clusters with metadata.

    Query params:
        limit  -- max rows (default 30)
    """
    try:
        limit = min(int(request.args.get('limit', 30)), 100)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid limit parameter'}), 400

    cache_key = f'anomalies:{limit}'
    cached = _qe_cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT ts, zscore AS extreme_count, metadata, resolved_at
                FROM quant.anomalies
                WHERE domain = 'cluster'
                ORDER BY ts DESC LIMIT %s
            """, (limit,))
            rows = []
            for r in cur.fetchall():
                meta = r['metadata'] or {}
                if isinstance(meta, str):
                    import json as _json
                    meta = _json.loads(meta)
                rows.append({
                    'date': r['ts'].isoformat(),
                    'extreme_count': int(r['extreme_count']),
                    'domains_affected': meta.get('domains_affected', 0),
                    'metrics': meta.get('metrics', []),
                    'resolved_at': r['resolved_at'].isoformat() if r['resolved_at'] else None,
                })
        result = {'count': len(rows), 'data': rows}
        _qe_cache.set(cache_key, result)
        return jsonify(result)
    except Exception as e:
        logger.error(f"[quant] anomalies failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@quant_bp.route('/pivot')
@tier_feature_required('quant_engine')
def pivot():
    """Pivot query: aggregate z-scores across metrics with grouping.

    Query params:
        metrics   -- comma-separated canonical metric names (required, max 10)
        group_by  -- regime|zscore_bucket|monthly|quarterly (default: none)
        agg       -- mean|median|count|p25|p75 (default: mean)
        from      -- start date YYYY-MM-DD
        to        -- end date YYYY-MM-DD
        window    -- z-score window 30 or 90 (default: 90)
    """
    metrics_raw = request.args.get('metrics', '')
    if not metrics_raw:
        from quant.engine.config import CANONICAL_METRICS
        return jsonify({
            'error': 'metrics parameter required',
            'available': sorted(CANONICAL_METRICS.keys()),
        }), 400

    metrics = [m.strip() for m in metrics_raw.split(',') if m.strip()]
    group_by = request.args.get('group_by', '') or None
    agg = request.args.get('agg', 'mean')
    from_date = request.args.get('from', '') or None
    to_date = request.args.get('to', '') or None
    try:
        window = int(request.args.get('window', 90))
    except (ValueError, TypeError):
        window = 90

    cache_key = f'pivot:{",".join(sorted(metrics))}:{group_by}:{agg}:{from_date}:{to_date}:{window}'
    cached = _qe_cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    try:
        from quant.engine.pivot import query_pivot
        result = query_pivot(
            metrics=metrics,
            group_by=group_by,
            agg=agg,
            from_date=from_date,
            to_date=to_date,
            window=window,
        )
        _qe_cache.set(cache_key, result)
        return jsonify(result)
    except ValueError as e:
        logger.warning(f"[quant] pivot bad params: {e}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"[quant] pivot failed: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


# ── COT: CFTC Commitment of Traders positioning ──────────────────────────────
#
# Metric naming convention in quant.sentiment:
#   COT_{ASSET}_SPEC_NET       — large-speculator (non-commercial) net contracts
#   COT_{ASSET}_COMM_NET       — commercial (hedger) net contracts
#   COT_{ASSET}_SPEC_LONG_PCT  — % of open interest held long by large specs
#
# Z-scores come from quant.zscores domain='sentiment' (computed nightly by
# normalize.py via the 'sentiment' DOMAIN_QUERY which covers all of
# quant.sentiment including COT_* rows).
#
# Stretched flags: |z| >= 2 OR percentile >= 90 / <= 10.
# COT updates weekly (CFTC publishes Fri 3:30 PM ET, as-of Tue +3d).

# Friendly names for display
_COT_ASSET_LABELS = {
    'BTC':   'Bitcoin (CME)',
    'ETH':   'Ethereum (CME)',
    'GOLD':  'Gold (COMEX)',
    'OIL':   'WTI Crude Oil (NYMEX)',
    'SP500': 'S&P 500 e-mini (CME)',
    'DXY':   'US Dollar Index (ICE)',
}

# Metric suffix → human type label
_COT_TYPE_LABELS = {
    'SPEC_NET':      'Large spec net (contracts)',
    'COMM_NET':      'Commercial net (contracts)',
    'SPEC_LONG_PCT': 'Large spec long %',
}

# COT metrics to surface (3 per market × 6 markets = 18 total)
_COT_METRICS = [
    f'COT_{asset}_{suffix}'
    for asset in ('BTC', 'ETH', 'GOLD', 'OIL', 'SP500', 'DXY')
    for suffix in ('SPEC_NET', 'COMM_NET', 'SPEC_LONG_PCT')
]


def _cot_stretched(zscore, percentile):
    """True when positioning is historically extreme (either tail)."""
    if abs(zscore) >= 2.0:
        return True
    if percentile is not None and (percentile >= 90.0 or percentile <= 10.0):
        return True
    return False


def build_cot():
    """Return the current COT picture.

    Reads z-scores from quant.zscores (domain='sentiment', window=90) and
    week-over-week change from the two most recent quant.sentiment rows.
    Returns a dict ready for jsonify(); returns None on DB error.

    COT data updates weekly — cached 1h (3600s) to match that cadence.
    """
    cache_key = 'cot:v1'
    cached = _cot_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        with get_conn() as conn:
            cur = conn.cursor()

            # ── Latest z-scores (90d window) for all COT metrics ─────
            cur.execute("""
                SELECT z.metric, z.ts, z.zscore, z.raw_value, z.percentile
                FROM quant.zscores z
                INNER JOIN (
                    SELECT metric, MAX(ts) AS max_ts
                    FROM quant.zscores
                    WHERE domain = 'sentiment'
                      AND metric = ANY(%s)
                      AND window_days = 90
                    GROUP BY metric
                ) latest
                    ON z.metric = latest.metric
                    AND z.ts    = latest.max_ts
                WHERE z.domain = 'sentiment'
                  AND z.metric = ANY(%s)
                  AND z.window_days = 90
            """, (_COT_METRICS, _COT_METRICS))
            zscore_rows = {r['metric']: r for r in cur.fetchall()}

            # ── Week-over-week change: latest 2 sentiment rows per metric ─
            cur.execute("""
                SELECT metric, ts, value,
                       ROW_NUMBER() OVER (
                           PARTITION BY metric ORDER BY ts DESC
                       ) AS rn
                FROM quant.sentiment
                WHERE metric = ANY(%s)
            """, (_COT_METRICS,))
            wow_latest = {}
            wow_prev = {}
            for r in cur.fetchall():
                if r['rn'] == 1:
                    wow_latest[r['metric']] = float(r['value'])
                elif r['rn'] == 2:
                    wow_prev[r['metric']] = float(r['value'])

        # ── Build per-series result ──────────────────────────────────
        series = []
        for metric in _COT_METRICS:
            # Parse asset + type from metric name (COT_{ASSET}_{SUFFIX})
            # metric always starts with 'COT_'
            parts = metric[4:].split('_', 1)  # strip leading 'COT_'
            if len(parts) != 2:
                continue
            asset_key, type_key = parts[0], parts[1]

            zrow = zscore_rows.get(metric)
            if zrow is None:
                # No z-score yet (insufficient history); still include with nulls
                entry = {
                    'metric': metric,
                    'market': asset_key,
                    'market_label': _COT_ASSET_LABELS.get(asset_key, asset_key),
                    'type': type_key,
                    'type_label': _COT_TYPE_LABELS.get(type_key, type_key),
                    'value': None,
                    'zscore': None,
                    'percentile': None,
                    'wow_change': None,
                    'stretched': False,
                    'date': None,
                }
            else:
                z = float(zrow['zscore'])
                pct = float(zrow['percentile']) if zrow['percentile'] is not None else None
                val = float(zrow['raw_value']) if zrow['raw_value'] is not None else None
                latest_val = wow_latest.get(metric)
                prev_val = wow_prev.get(metric)
                wow = round(latest_val - prev_val, 4) if (
                    latest_val is not None and prev_val is not None
                ) else None
                entry = {
                    'metric': metric,
                    'market': asset_key,
                    'market_label': _COT_ASSET_LABELS.get(asset_key, asset_key),
                    'type': type_key,
                    'type_label': _COT_TYPE_LABELS.get(type_key, type_key),
                    'value': val,
                    'zscore': round(z, 4),
                    'percentile': round(pct, 1) if pct is not None else None,
                    'wow_change': wow,
                    'stretched': _cot_stretched(z, pct),
                    'date': zrow['ts'].isoformat() if hasattr(zrow['ts'], 'isoformat')
                    else str(zrow['ts']),
                }
            series.append(entry)

        # Sort by |z| desc (nulls last)
        series.sort(
            key=lambda d: abs(d['zscore']) if d['zscore'] is not None else -1,
            reverse=True,
        )

        stretched_count = sum(1 for s in series if s['stretched'])
        data_date = series[0]['date'] if series and series[0]['date'] else None

        result = {
            'data_date': data_date,
            'stretched_count': stretched_count,
            'series': series,
        }
        # COT updates weekly — cache for 1 hour
        _cot_cache.set(cache_key, result)
        return result

    except Exception as e:
        logger.error('[quant] build_cot failed: %s', e, exc_info=True)
        return None


@quant_bp.route('/cot')
@tier_feature_required('quant_engine')
def cot():
    """Current CFTC COT positioning picture.

    Returns the 18 COT series (6 markets × 3 metrics each) with latest
    value, 90-day z-score, percentile, week-over-week change, and a
    'stretched' flag (|z|>=2 or percentile>=90/<=10). Sorted by |z| desc.

    Data updates weekly (CFTC publishes Friday 3:30 PM ET for the prior
    Tuesday's snapshot). Cached 1 hour.
    """
    result = build_cot()
    if result is None:
        return jsonify({'error': 'Internal server error'}), 500
    return jsonify(result)
