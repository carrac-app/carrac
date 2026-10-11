"""MCP tools: Analysis & Quant (4 tools)."""

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.mcp.backtest_models import MODEL_HISTORICAL_ANALOG


@mcp_tool(
    'report_get_compact',
    'Return a compact, LLM-ready summary of the latest market report — distinct '
    'from report_get_raw, which returns the full raw HTML/metadata. This '
    'tool returns {id, created_at, summary, audio_script} via compact_report(), '
    'making it cheaper for downstream synthesis, plus requested_type/is_fallback/'
    'is_stale/age_hours so a caller can tell what it actually got. '
    'SECURITY/PRIVACY: the operator-personal "morning" report (positions/alerts '
    'woven into its prose) is ADMIN-ONLY — every other caller, paid included, '
    'transparently resolves "morning" to the shared, operator-neutral "general" '
    'report (same gating as report_get_raw). As defense-in-depth, any '
    'operator-flavored report text that still reaches a non-admin caller (e.g. '
    'via the existence fallback below) is run through the same operator-neutral '
    'serving filter report_get_raw uses before being compacted; narration '
    '(audio_script) is withheld from non-operator callers. '
    'FRESHNESS FALLBACK: if the resolved type has no row, or its latest row is '
    'stale (generating pipeline stalled), this substitutes the freshest available '
    'SHARED report (general/afternoon/weekly) instead of silently returning a '
    'dead pipeline\'s last output; "morning" is never itself a fallback target. '
    'Use report_get_raw instead if you need the full HTML, section list, '
    'or assets_included metadata. Use report_get_section if you only need '
    'one section of the report by name. '
    'min_tier=member — free tier should use report_get_raw (resolves to general).',
    {
        'type': 'object',
        'properties': {
            'type': {
                'type': 'string',
                'enum': ['morning', 'weekly'],
                'description': 'Report type requested: morning = operator-personal daily '
                               'analysis (resolves to the shared general report for all '
                               'non-admin callers); weekly = prior-week digest. Default: morning.',
            },
        },
    },
    min_tier='member',
)
def report_get_compact(arguments):
    from carrac.formatters import compact_report
    # Reuse the exact tier-gating, freshness-fallback, and sanitization logic
    # report_get_raw/report_get_section already implement. Calling
    # db.get_latest_report() directly would let a member-tier caller request
    # type="morning" and receive the operator's own personal report
    # (positions/alerts) verbatim.
    from carrac.mcp.tools.reports import (
        _caller_is_free,
        _fetch_report_with_fallback,
        _report_row_for_caller,
        _is_stale,
        _report_age_hours,
        _resolve_report_type_for_caller,
    )

    requested_type = arguments.get('type') or 'morning'
    rtype = requested_type
    if rtype not in ('morning', 'weekly'):
        raise ToolError(f'Unknown report type: {rtype}')

    is_free = _caller_is_free()
    rtype = _resolve_report_type_for_caller(rtype)

    try:
        from carrac.content_db import get_content_db
        db = get_content_db()
        # Free callers must never fall back to the operator 'morning' report.
        report, rtype = _fetch_report_with_fallback(
            db, rtype, allow_morning_fallback=not is_free)
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f'Failed to fetch report: {e}')

    if not report:
        raise ToolError(f'No {rtype} report found')

    age_hours = _report_age_hours(report)
    is_stale = _is_stale(report, rtype)

    # compact_report() only trims the row; the serving policy does the
    # filtering: non-operator callers get a filtered summary and no narration.
    sanitized = _report_row_for_caller(report, rtype)

    out = compact_report(sanitized)
    # Make the substitution unambiguous: what was asked for vs what was
    # served, and whether even what was served is itself stale. Never
    # fabricate freshness — these reflect the real row returned.
    out['requested_type'] = requested_type
    out['is_fallback'] = rtype != requested_type
    out['is_stale'] = is_stale
    out['age_hours'] = round(age_hours, 1) if age_hours is not None else None
    return out


@mcp_tool(
    'quant_get_data',
    'Fetch quantitative macro-crypto data from the Carrac Quant DB. '
    'Four query types — choose via query_type: '
    'snapshot (default) = full /api/quant/snapshot for a focus asset (BTC/ETH/SOL), '
    'including transmission chains, regime_hist_median_days, leader_z, '
    'projected_move_z, and anomaly_cluster — the richest single-call view; '
    'zscores = latest z-score rankings across all metrics for a domain '
    '(macro/price/derivatives/sentiment), sorted by |z|, top 50 returned. '
    'NOTE on domain coverage for zscores: \'macro\' and \'sentiment\' are '
    'populated; \'derivatives\' aggregates four underlying sub-domains '
    '(funding, long/short ratio, options, advanced-derivatives) and is '
    'populated; \'price\' currently has NO z-scored metrics in this table '
    '(asset price-return series are not written to quant.zscores) — a '
    'price-domain query returns an explicit no-data message, not a bare '
    'empty list, so it is never mistaken for "nothing anomalous". '
    'series = historical time series for a specific metric (requires metric + domain); '
    'regime = current market regime label + scores, either from cache or freshly computed. '
    'Freshness: snapshot/zscores are updated on the quant worker schedule (typically hourly); '
    'series returns up to the last 100 rows. '
    'SIDE EFFECT: none — read-only. '
    'Use quant_get_correlations to follow up on transmission channels surfaced in snapshot. '
    'Use backtest_get_historical_analog to test whether a regime or z-score threshold has '
    'historically predicted forward returns.',
    {
        'type': 'object',
        'properties': {
            'query_type': {
                'type': 'string',
                'enum': ['snapshot', 'zscores', 'series', 'regime'],
                'description': 'Type of quant data to fetch (default: snapshot)',
            },
            'asset': {
                'type': 'string',
                'description': 'Focus asset for snapshot (BTC/ETH/SOL). Default: BTC. '
                               'Threads through correlations/leadlag queries.',
            },
            'domain': {
                'type': 'string',
                'enum': ['macro', 'price', 'derivatives', 'sentiment'],
                'description': 'Data domain filter (for zscores and series). For zscores: '
                               '\'derivatives\' aggregates all derivatives_* sub-domains; '
                               '\'price\' has no z-scored rows today (returns an explicit '
                               'no-data message rather than an empty list).',
            },
            'metric': {
                'type': 'string',
                'description': 'Specific metric name (for series query, e.g. VIX, M2)',
            },
            'window': {
                'type': 'integer',
                'enum': [30, 90],
                'description': 'Lookback window in days for z-scores. Must be 30 or 90 (the two pre-computed windows). Default: 90.',
            },
        },
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_data(arguments):
    query_type = arguments.get('query_type', 'snapshot')
    asset = (arguments.get('asset') or 'BTC').upper()

    # build_snapshot() whitelists against TARGET_ASSETS for SQL
    # safety and then SILENTLY falls back to BTC. On the HTTP route a default is
    # defensible; on the agent surface it means a caller that asked for PEPE
    # gets BTC's correlations and lead-lag back and may narrate them as PEPE.
    # Only an EXPLICIT unknown asset is an error — omitting it keeps the
    # documented BTC default, so existing callers are unaffected.
    if arguments.get('asset'):
        try:
            from quant.engine.config import TARGET_ASSETS
            covered = sorted(TARGET_ASSETS)
        except Exception:  # noqa: BLE001 - a broken registry must not 500 the tool
            covered = []
        if covered and asset not in covered:
            raise ToolError(
                f'Asset {asset!r} is not covered by the quant engine. '
                f'Covered assets: {", ".join(covered)}. '
                f'The engine computes z-scores, correlations and lead-lag on '
                f'this fixed set only — it does not fall back to another asset.'
            )

    try:
        from carrac.db_pool import get_quant_conn as get_conn

        if query_type == 'regime':
            from carrac.caches import get_cached_regime
            regime = get_cached_regime()
            if regime:
                return regime
            # Compute fresh
            from carrac.sentiment.regime import gather_regime_inputs, compute_market_regime
            inputs = gather_regime_inputs()
            return compute_market_regime(**inputs)

        if query_type == 'snapshot':
            # Use the shared build_snapshot() helper so MCP returns the full
            # snapshot shape (chains, regime_hist_median_days,
            # projected_move_z/leader_z, anomaly_cluster, q_value/CI on corrs).
            from quant.api.routes import build_snapshot
            snap = build_snapshot(asset)
            if snap is None:
                raise ToolError('Quant snapshot build failed')
            return snap

        if query_type == 'zscores':
            domain = arguments.get('domain')
            window = arguments.get('window', 90)
            # Only the two pre-computed window sizes are stored; reject others
            # with a clear error so callers know what to pass.
            if window not in (30, 90):
                raise ToolError(
                    f'window must be 30 or 90 (the two pre-computed z-score windows). '
                    f'Got: {window}'
                )
            with get_conn() as conn:
                # Not `ts = (SELECT MAX(ts) FROM quant.zscores)`: that is a
                # SINGLE global latest timestamp across every domain/metric. Domains that
                # update less frequently than the most-frequently-updated one
                # (e.g. macro, which is refreshed on FRED/macro release
                # schedules) never share that exact global-max ts, so they
                # silently returned zero rows even though the domain has real
                # data — indistinguishable from "nothing anomalous". Join
                # against the latest ts PER (domain, metric, asset) instead,
                # as quant_get_anomalies does.
                sql = (
                    'SELECT z.ts, z.domain, z.metric, z.asset, z.zscore, z.raw_value '
                    'FROM quant.zscores z '
                    'INNER JOIN ('
                    '    SELECT domain, metric, asset, MAX(ts) AS max_ts '
                    '    FROM quant.zscores WHERE window_days = %s '
                    '    GROUP BY domain, metric, asset'
                    ') latest '
                    '  ON z.ts = latest.max_ts AND z.domain = latest.domain '
                    ' AND z.metric = latest.metric AND z.asset = latest.asset '
                    'WHERE z.window_days = %s'
                )
                params = [window, window]
                if domain == 'derivatives':
                    # The 'derivatives' domain value is a friendly umbrella —
                    # quant.zscores actually stores finer-grained domains
                    # (derivatives_funding, derivatives_ls, derivatives_options,
                    # derivatives_advanced; see quant/normalize.py
                    # DOMAIN_QUERIES). Aggregate across all of them so the
                    # documented 'derivatives' filter actually returns data.
                    sql += " AND z.domain LIKE 'derivatives%%'"
                elif domain:
                    sql += ' AND z.domain = %s'
                    params.append(domain)
                sql += ' ORDER BY ABS(z.zscore) DESC LIMIT 50'
                cur = conn.cursor()
                cur.execute(sql, params)
                rows = cur.fetchall()
                data = [
                    {
                        'domain': r['domain'], 'metric': r['metric'], 'asset': r['asset'],
                        'zscore': round(float(r['zscore']), 2),
                        'value': float(r['raw_value']) if r.get('raw_value') is not None else None,
                    }
                    for r in rows
                ]
                result = {
                    'type': 'zscores',
                    'domain': domain,
                    'window': window,
                    'data': data,
                }
                if domain and not data:
                    # Don't let a bare empty list read as "nothing is
                    # anomalous" when it may just mean this domain has no
                    # z-scored metrics at all — say so explicitly.
                    result['message'] = (
                        f"No z-score data available for domain '{domain}' "
                        f'(window={window}d). This is not the same as '
                        f'"nothing anomalous" — this domain currently has no '
                        f'rows in quant.zscores. Known-populated domains: '
                        f'macro, sentiment, derivatives (aggregates '
                        f'derivatives_funding/ls/options/advanced). The '
                        f"'price' domain has no dedicated z-scored metrics "
                        f'in this table today (asset return series are not '
                        f'z-scored here) — use query_type=series for raw '
                        f'price-metric history instead.'
                    )
                return result

        if query_type == 'series':
            metric = (arguments.get('metric') or '').strip().upper()
            if not metric:
                raise ToolError('metric is required for series query')

            from quant.engine.config import CANONICAL_METRICS
            if metric not in CANONICAL_METRICS:
                valid = sorted(CANONICAL_METRICS.keys())
                raise ToolError(
                    f'Unknown metric: {metric}. '
                    f'Valid metric names: {", ".join(valid)}'
                )

            cfg = CANONICAL_METRICS[metric]
            table = cfg['table']

            # Route query to the correct table based on CANONICAL_METRICS config.
            # Cap at 365 most-recent points to prevent bulk raw-data export.
            # This returns computed z-score inputs (transformed series values),
            # not raw OHLCV or other bulk data export.
            with get_conn() as conn:
                cur = conn.cursor()

                if table == 'price_daily':
                    asset = cfg['asset']
                    source = cfg.get('source', 'binance')
                    cur.execute(
                        'SELECT ts, close AS value FROM quant.price_daily '
                        'WHERE asset = %s AND source = %s '
                        'ORDER BY ts DESC LIMIT 365',
                        (asset, source),
                    )
                elif table == 'macro':
                    db_metric = cfg.get('metric', metric)
                    cur.execute(
                        'SELECT ts, value FROM quant.macro '
                        'WHERE metric = %s '
                        'ORDER BY ts DESC LIMIT 365',
                        (db_metric,),
                    )
                elif table == 'sentiment':
                    db_metric = cfg.get('metric', metric)
                    asset = cfg.get('asset', '_GLOBAL_')
                    cur.execute(
                        'SELECT ts, value FROM quant.sentiment '
                        'WHERE metric = %s AND asset = %s '
                        'ORDER BY ts DESC LIMIT 365',
                        (db_metric, asset),
                    )
                elif table == 'derivatives':
                    asset = cfg['asset']
                    column = cfg.get('column', 'funding_rate_avg')
                    cur.execute(
                        f'SELECT ts, {column} AS value FROM quant.derivatives '  # noqa: S608
                        'WHERE asset = %s AND {column} IS NOT NULL '
                        'ORDER BY ts DESC LIMIT 365'.replace('{column}', column),
                        (asset,),
                    )
                elif table == 'onchain':
                    db_metric = cfg.get('metric', metric)
                    asset = cfg.get('asset', 'BTC')
                    cur.execute(
                        'SELECT ts, value FROM quant.onchain '
                        'WHERE metric = %s AND asset = %s '
                        'ORDER BY ts DESC LIMIT 365',
                        (db_metric, asset),
                    )
                elif table == 'signals':
                    source = cfg['source']
                    db_metric = cfg.get('metric', metric)
                    asset = cfg.get('asset', '_GLOBAL_')
                    cur.execute(
                        'SELECT ts, value FROM quant.signals '
                        'WHERE source = %s AND metric = %s AND asset = %s '
                        'ORDER BY ts DESC LIMIT 365',
                        (source, db_metric, asset),
                    )
                else:
                    raise ToolError(f'Unsupported table type: {table}')

                rows = cur.fetchall()
                return {
                    'type': 'series',
                    'metric': metric,
                    'table': table,
                    'data': [
                        {'date': str(r['ts']), 'value': float(r['value'])}
                        for r in rows if r['value'] is not None
                    ],
                    'count': len(rows),
                    'cap': 365,
                }

        raise ToolError(f'Unknown query_type: {query_type}')

    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f'Quant data query failed: {e}')


@mcp_tool(
    'quant_get_correlations',
    'Return the top cross-asset/macro correlations with a target asset or metric '
    'from the Carrac Quant DB. Results are pre-computed on a rolling window and '
    'include FDR-adjusted q_value, 95% CI (ci_low/ci_high), and a boolean '
    'significant flag alongside the raw correlation and p_value. '
    'Returns up to 30 entries sorted by |correlation| descending, one row per '
    'metric (the unconditional full-window correlation; regime-split and '
    'lead-lag variants stored under the same asset are excluded here). '
    'Freshness: updated on the quant worker schedule (typically hourly). '
    'NOT for live tick correlation — this is macro regime-level analysis. '
    'Use quant_get_data(query_type=snapshot) to see transmission chains already '
    'assembled; use this tool when you want the raw ranked list to filter or '
    'reason about specific macro drivers. '
    'target must be one of the canonical metric names (see CANONICAL_METRICS, '
    'e.g. BTC, ETH, VIX, M2, DXY) — an unrecognised target raises a fast '
    'ToolError listing valid targets rather than querying the DB. '
    'window must be one of the pre-computed correlation windows (60 or 120 days) '
    '— an unsupported value raises a ToolError naming the supported windows '
    'rather than silently returning an empty result set.',
    {
        'type': 'object',
        'properties': {
            'target': {
                'type': 'string',
                'description': 'Target asset/metric (default: BTC). Must be a canonical '
                               'metric name — see quant_get_anomalies or quant_get_leadlag_ranking for examples.',
            },
            'window': {
                'type': 'integer',
                'enum': [60, 120],
                'description': 'Rolling window in days. Must be 60 or 120 (the two '
                               'pre-computed correlation windows). Default: 60.',
            },
            'min_abs': {
                'type': 'number',
                'description': 'Minimum absolute correlation (default: 0.3)',
            },
        },
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_correlations(arguments):
    target = (arguments.get('target') or 'BTC').strip().upper()
    window = arguments.get('window', 60)
    min_abs = arguments.get('min_abs', 0.3)

    try:
        # Fail fast on an unknown target instead of hitting the DB — an
        # unrecognised metric_a would fall through to a query that hangs
        # to the full MCP timeout instead of erroring cleanly (unlike
        # quant_get_leadlag_ranking, which validates against its own pair config up
        # front). CANONICAL_METRICS is the same universe the correlation
        # engine writes metric_a/metric_b from, so it's a safe local
        # whitelist — no DB round trip needed to validate.
        from quant.engine.config import CANONICAL_METRICS
        if target not in CANONICAL_METRICS:
            valid = sorted(CANONICAL_METRICS.keys())
            raise ToolError(
                f'Unknown target: {target}. Valid targets: {", ".join(valid)}'
            )

        # Only window_days in CORRELATION_WINDOWS (quant/engine/
        # correlations.py) is ever computed and written by the engine. An
        # unsupported window (e.g. 90, 20, 9999, 0) would fall through to a
        # query that legitimately finds zero rows and echo the caller's own
        # number back in an otherwise-normal-looking
        # {"window": 20, "count": 0} — an LLM reads that as "no correlations
        # on a 20-day window", a false statement about the market. The truth
        # is "20 is not a window Carrac computes." Mirrors quant_get_data's
        # window check exactly (quant_get_data in this module).
        from quant.engine.config import CORRELATION_WINDOWS
        if window not in CORRELATION_WINDOWS:
            valid_windows = ' or '.join(str(w) for w in CORRELATION_WINDOWS)
            raise ToolError(
                f'window must be {valid_windows} (the pre-computed correlation '
                f'windows). Got: {window}'
            )

        from carrac.db_pool import get_quant_conn as get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            # method='spearman' is the unconditional, full-window
            # correlation. Without this filter the same (metric_a, metric_b,
            # window_days, ts) also matches the regime-split rows the engine
            # writes under method='spearman_favorable'/'spearman_unfavorable'
            # (quant/engine/correlations.py::compute_rolling_correlations, n_obs = count of days in
            # that regime within the window, e.g. 36) and, for metric_a values
            # that are also leadlag leaders, method='leadlag'/'leadlag_weekly'
            # rows (quant/engine/leadlag.py) — all sharing the same primary key
            # except `method` (schema: quant.correlations_pkey = ts, metric_a,
            # metric_b, window_days, method). That is exactly the observed bug:
            # the same metric appearing twice with different n_obs (36 vs 60)
            # and different correlation values. The HTTP route
            # (quant/api/routes.py::build_snapshot) already filters to method='spearman'
            # for this same "top correlations for an asset" use case — this
            # copies that idiom so MCP and HTTP agree, and guarantees at most
            # one row per metric_b (method is part of the primary key, so
            # fixing it to a single value makes metric_b unique per ts/window).
            cur.execute(
                'SELECT metric_a, metric_b, correlation, p_value, n_obs, ts, '
                '       q_value, ci_low, ci_high, significant '
                'FROM quant.correlations '
                "WHERE metric_a = %s AND window_days = %s AND method = 'spearman' "
                'AND ABS(correlation) >= %s '
                'AND ts = (SELECT MAX(ts) FROM quant.correlations '
                "           WHERE metric_a = %s AND window_days = %s AND method = 'spearman') "
                'ORDER BY ABS(correlation) DESC LIMIT 30',
                (target, window, min_abs, target, window),
            )
            rows = cur.fetchall()
            data = []
            for r in rows:
                entry = {
                    'metric': r['metric_b'],
                    'correlation': round(float(r['correlation']), 3),
                    'p_value': round(float(r['p_value']), 4) if r.get('p_value') else None,
                    'n_obs': r['n_obs'],
                    'date': str(r['ts']),
                }
                # Surface FDR-adjusted q-value, 95% CI, and the
                # pre-computed significance flag so callers can filter.
                if r.get('q_value') is not None:
                    entry['q_value'] = round(float(r['q_value']), 4)
                if r.get('ci_low') is not None:
                    entry['ci_low'] = round(float(r['ci_low']), 3)
                    entry['ci_high'] = round(float(r['ci_high']), 3)
                if r.get('significant') is not None:
                    entry['significant'] = bool(r['significant'])
                data.append(entry)
            return {
                'target': target,
                'window': window,
                'data': data,
                'count': len(data),
            }

    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f'Correlation query failed: {e}')


@mcp_tool(
    'backtest_get_historical_analog',
    'Test a set of quant conditions against Carrac\'s historical data and get '
    'forward return distributions for those past instances. This is the '
    '"Historical Analog" model — a conditional query, not a trade simulation; '
    'do not compare its numbers to Hold-N or Full Simulation metrics. '
    'conditions is a comma-separated list of METRIC:op:threshold clauses '
    '(e.g. "FEAR_GREED:lt:-1.5,REGIME:eq:1"). '
    'Returns model (always "Historical Analog") plus per-horizon stats: '
    'instances (how many historical hits), '
    'median_return, p25/p75, and current_match (whether conditions are met right now). '
    'Supports multiple horizons simultaneously (default 7,30,90 days). '
    'Use quant_get_data(query_type=zscores) to discover candidate metrics and '
    'thresholds before calling this. '
    'SIDE EFFECT: none — read-only. min_tier=member (quant_engine feature).',
    {
        'type': 'object',
        'properties': {
            'conditions': {
                'type': 'string',
                'description': 'Comma-separated conditions (e.g. "FEAR_GREED:lt:-1.5,REGIME:eq:1")',
            },
            'target': {
                'type': 'string',
                'description': 'Target asset for forward returns (default: BTC)',
            },
            'horizons': {
                'type': 'string',
                'description': 'Comma-separated horizons in days (default: "7,30,90")',
            },
        },
        'required': ['conditions'],
    },
    min_tier='member',
    feature='quant_engine',
)
def backtest_get_historical_analog(arguments):
    conditions_str = arguments.get('conditions', '')
    if not conditions_str:
        raise ToolError('conditions is required')

    target = arguments.get('target', 'BTC')
    horizons_str = arguments.get('horizons', '7,30,90')

    try:
        horizons = [int(h.strip()) for h in horizons_str.split(',')]
    except ValueError:
        raise ToolError('horizons must be comma-separated integers')

    conditions = [c.strip() for c in conditions_str.split(',') if c.strip()]

    try:
        from quant.engine.conditional import query_conditional

        results = {}
        for horizon in horizons:
            data = query_conditional(conditions, target=target, horizon=horizon)
            results[f'{horizon}d'] = {
                'instances': data.get('instances', 0),
                'median_return': data.get('forward_return', {}).get('median'),
                'p25': data.get('forward_return', {}).get('p25'),
                'p75': data.get('forward_return', {}).get('p75'),
                'current_match': data.get('current_match', False),
            }

        return {
            'model': MODEL_HISTORICAL_ANALOG,
            'conditions': conditions,
            'target': target,
            'horizons': results,
        }

    except Exception as e:
        raise ToolError(f'Backtest query failed: {e}')
