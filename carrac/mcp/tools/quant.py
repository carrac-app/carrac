"""MCP tools: Quant Engine — derivatives regime, lead-lag, chains, analogues."""

from carrac.mcp.registry import mcp_tool
from carrac.mcp.protocol import ToolError
from carrac.compliance.deribit_gate import (
    deribit_visible, leadlag_metric_allowed, strip_deribit_columns)
from carrac.mcp.tools import get_mcp_user_id


@mcp_tool(
    'quant_get_regime',
    'Return the current derivatives regime classification for BTC and/or ETH. '
    'Reads the latest row from quant.derivatives and runs it through the live '
    'HMM-based classifier. Returns: (1) raw metrics — funding_rate_avg, oi_usd, '
    'long_short_ratio and the ratios derived from them; (2) classified regime — one of '
    'euphoric_meltup / complacent_bull / healthy_trend / pre_event_tension / '
    'distribution_topping / sudden_correction / full_capitulation / '
    'post_crash_stabilization / vol_compression / bear_grind / short_squeeze_setup '
    '/ structural_carry — plus confidence score (0–1). '
    'Call this for a point-in-time regime label before sizing a trade or before '
    'creating a regime_change alert. For the full historical sequence and '
    'transition log use quant_get_regime_history instead. Freshness: latest ingested '
    'derivatives row (typically daily).',
    {
        'type': 'object',
        'properties': {
            'asset': {
                'type': 'string',
                'enum': ['BTC', 'ETH', 'all'],
                'description': 'Asset to classify. "all" returns both BTC and ETH metrics but classifies BTC regime only. Default: all.',
            },
        },
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_regime(arguments):
    asset_filter = arguments.get('asset', 'all').upper()

    try:
        from quant.engine.derivatives_regime import (
            classify_asset_row, fetch_latest_derivatives_row)

        assets = ['BTC', 'ETH'] if asset_filter == 'ALL' else [asset_filter]
        result = {}

        for asset in assets:
            row = fetch_latest_derivatives_row(asset)
            if not row:
                continue

            # Deribit-derived columns are admin-only. Stripped
            # BEFORE classification, not just before serialisation — a regime
            # label computed from withheld data is still derived from it.
            row = strip_deribit_columns(row)

            metrics = {}
            for k, v in row.items():
                if k == 'ts':
                    continue
                if v is not None:
                    metrics[k] = round(float(v), 4)

            result[asset] = {
                'ts': str(row['ts']),
                'metrics': metrics,
            }

        # Classify from the REQUESTED asset's own row — this used
        # to hardcode a 'BTC' lookup here regardless of asset_filter, so an
        # ETH-only request never got its own regime. 'all' keeps classifying
        # BTC only (documented, back-compat primary regime); a single-asset
        # request (BTC or ETH) classifies THAT asset's own metrics.
        primary_asset = 'BTC' if asset_filter == 'ALL' else asset_filter
        primary = result.get(primary_asset)
        regime = classify_asset_row(primary['metrics']) if primary else None

        if regime is None and asset_filter != 'ALL':
            # No derivatives row at all, or a row with none of the
            # options/GEX/skew/dvol fields needed to classify (that
            # collection — quant/collectors/deribit_options.py — is BTC/ETH
            # only; other assets may still have a funding-only row). Say so
            # plainly instead of silently classifying off another asset.
            # Keep whatever metrics we DID resolve. Without the
            # options complex the classifier cannot produce a regime, but the
            # permitted Binance columns (funding, OI, long/short) are still
            # real data the caller asked for — dropping them would turn a
            # licensing boundary into a silent feature outage.
            out = dict(result)
            out.update({
                'asset': asset_filter,
                'regime': {'regime': 'unknown', 'confidence': 0},
                'message': f'No regime classification for {asset_filter}. '
                           f'Available for BTC/ETH only.'
                           + ('' if deribit_visible() else
                              ' The options-based inputs it needs (implied '
                              'volatility, skew, gamma exposure) come from a '
                              'venue whose terms do not permit redistribution, '
                              'so they are excluded. Funding, open interest and '
                              'long/short balance are unaffected.'),
            })
            return out

        result['regime'] = regime or {'regime': 'unknown', 'confidence': 0}
        return result

    except Exception as e:
        raise ToolError(f'Derivatives regime query failed: {e}')


@mcp_tool(
    'quant_get_leadlag_ranking',
    'Return the full lead-lag ranking for a target asset — all configured '
    'leaders sorted by peak |correlation|. For each leader the response '
    'includes: lag_days (optimal lag in calendar days), correlation (Spearman r '
    'at that lag), p_value (per-lag, NOT corrected for multiple comparisons), '
    'p_corrected (permutation-based p-value for the max-statistic over all '
    'tested lags — accounts for the fact that ~181 lags are scanned and the '
    'best one is reported; prefer this over p_value for significance decisions), '
    'n_lags_tested (how many lags were evaluated), data_as_of (ISO date the '
    'correlation row was computed), ci_low/ci_high (95% CI of the correlation '
    'coefficient itself using effective sample size — corrected for forward-fill '
    'ties and serial autocorrelation, so wider than naive Fisher-z for sparse '
    'monthly/weekly series; NOT of any projected move), n_obs (number of '
    'observations used), description (economic rationale), and persistent '
    '(bool — whether the relationship is stable across rolling sub-windows). '
    'NOTE: ci_low/ci_high are the 95% CI of the correlation coefficient itself, '
    'not of any projected move. '
    'Configured leader→target pairs include: M2→BTC (10-week lag), '
    'NET_LIQUIDITY→BTC, DXY→BTC/ETH, VIX→BTC, HY_SPREAD→BTC, '
    'STABLECOIN_SUPPLY→BTC, FUNDING_BTC→BTC, ETF_BTC_FLOW→BTC, and chain '
    'links (FED_FUNDS→YIELD_2Y→…→BTC). '
    'Call this for a broad "what is leading BTC right now" sweep. For a '
    'single pair with the full correlogram (r at every lag + 95% CI) use '
    'quant_get_leadlag_pair. For structured chain analysis use quant_get_transmission_chains. '
    'Freshness: updated each time the daily leadlag job runs.',
    {
        'type': 'object',
        'properties': {
            'target': {
                'type': 'string',
                'description': 'Target asset — must match a configured pair (e.g. BTC, ETH, SPX). Default: BTC.',
            },
            'frequency': {
                'type': 'string',
                'enum': ['daily', 'weekly'],
                'description': 'Analysis frequency. Weekly lags are expressed in calendar days (lag×7). Default: daily.',
            },
        },
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_leadlag_ranking(arguments):
    target = arguments.get('target', 'BTC').upper()
    freq = arguments.get('frequency', 'daily')
    ll_method = 'leadlag_weekly' if freq == 'weekly' else 'leadlag'
    ll_summary = 'leadlag_weekly_summary' if freq == 'weekly' else 'leadlag_summary'

    try:
        from carrac.db_pool import get_quant_conn as get_conn
        from quant.engine.config import LEADLAG_PAIRS
        from collections import defaultdict
        import json as _json

        # Build lookup for configured pairs
        pair_info = {}
        for leader, tgt, lag_range, desc in LEADLAG_PAIRS:
            if tgt == target:
                pair_info[leader] = {'desc': desc, 'lag_range': lag_range}

        if not pair_info:
            targets = sorted(set(t for _, t, _, _ in LEADLAG_PAIRS))
            raise ToolError(
                f'No configured lead-lag pairs for {target}. '
                f'Available targets: {", ".join(targets)}'
            )

        with get_conn() as conn:
            cur = conn.cursor()

            cur.execute("""
                SELECT c.metric_a, c.window_days AS lag,
                       c.correlation, c.p_value, c.ts, c.ci_low, c.ci_high, c.n_obs
                FROM quant.correlations c
                WHERE c.metric_b = %s
                  AND c.method = %s
                  AND c.metric_a = ANY(%s)
                  AND c.ts = (SELECT MAX(ts) FROM quant.correlations
                              WHERE method = %s)
                ORDER BY c.metric_a, c.window_days
            """, [target, ll_method, list(pair_info.keys()), ll_method])
            rows = cur.fetchall()

            # Fetch persistence summaries
            persistence = {}
            cur.execute("""
                SELECT metric, metadata FROM quant.signals
                WHERE source = %s AND metric LIKE %s
                  AND ts = (SELECT MAX(ts) FROM quant.signals
                            WHERE source = %s)
            """, (ll_summary, f'%__{target}', ll_summary))
            for r in cur.fetchall():
                leader_name = r['metric'].split('__')[0]
                meta = r['metadata'] if isinstance(r['metadata'], dict) \
                    else _json.loads(r['metadata'])
                persistence[leader_name] = meta

        # Group by leader, find best lag
        by_leader = defaultdict(list)
        for r in rows:
            by_leader[r['metric_a']].append(r)

        lag_mult = 7 if freq == 'weekly' else 1
        data = []
        for leader, leader_rows in by_leader.items():
            best = max(leader_rows, key=lambda r: abs(float(r['correlation'])))
            info = pair_info.get(leader, {})
            pmeta = persistence.get(leader, {})
            item = {
                'leader': leader,
                'lag_days': best['lag'] * lag_mult,
                'correlation': round(float(best['correlation']), 4),
                'p_value': round(float(best['p_value']), 4),
                # p_corrected: permutation null for max-statistic (multiple-comparison
                # corrected); prefer this over p_value for significance decisions.
                'p_corrected': round(float(pmeta['p_corrected']), 4) if pmeta.get('p_corrected') is not None else None,
                'n_lags_tested': pmeta.get('n_lags_tested'),
                'description': info.get('desc', ''),
                'persistent': pmeta.get('persistent'),
                'data_as_of': str(best['ts']) if best.get('ts') is not None else None,
                'n_obs': best['n_obs'] if best.get('n_obs') is not None else None,
                'ci_low': round(float(best['ci_low']), 4) if best.get('ci_low') is not None else None,
                'ci_high': round(float(best['ci_high']), 4) if best.get('ci_high') is not None else None,
            }
            data.append(item)

        data.sort(key=lambda d: abs(d['correlation']), reverse=True)

        return {
            'target': target,
            'frequency': freq,
            'signals': data,
            'count': len(data),
        }

    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f'Lead-lag query failed: {e}')


@mcp_tool(
    'quant_get_transmission_chains',
    'Return the macro-to-crypto transmission chain analysis for one or all '
    'three named chains: '
    'monetary_policy (FED_FUNDS→YIELD_2Y→YIELD_10Y→HY_SPREAD→SPX→BTC), '
    'dollar_liquidity (M2→NET_LIQUIDITY→DXY→GOLD→BTC), '
    'risk_appetite (VIX→HY_SPREAD→SPX→BTC). '
    'For each chain returns: description, strength (geometric mean of |r| '
    'across all links, 0–1), weakest_link (the from→to pair with lowest |r|), '
    'data_as_of (ISO date of the quant.signals row used — signals freshness), '
    'and per-link detail (lag_days, correlation, persistent). '
    'Call this to understand where macro signal breaks down on the way to BTC, '
    'or to compare chain strength across regimes. For trend in chain strength '
    'over time use quant_get_chain_history. For the underlying pair-level correlogram '
    'use quant_get_leadlag_pair.',
    {
        'type': 'object',
        'properties': {
            'chain': {
                'type': 'string',
                'enum': ['monetary_policy', 'dollar_liquidity', 'risk_appetite', 'all'],
                'description': 'Which chain to analyze. Default: all.',
            },
            'frequency': {
                'type': 'string',
                'enum': ['daily', 'weekly'],
                'description': 'Analysis frequency. Default: daily.',
            },
        },
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_transmission_chains(arguments):
    chain_filter = arguments.get('chain', 'all')
    freq = arguments.get('frequency', 'daily')
    summary_source = 'leadlag_weekly_summary' if freq == 'weekly' else 'leadlag_summary'

    try:
        import json as _json
        import math
        from carrac.db_pool import get_quant_conn as get_conn
        from quant.engine.config import TRANSMISSION_CHAINS

        # A chain whose links are Deribit metrics is withheld
        # whole — publishing its strength still publishes derived data.
        _gated_chains = {
            name for name, spec in TRANSMISSION_CHAINS.items()
            if any(not leadlag_metric_allowed(m)
                   for link in spec.get('links', ()) for m in link)
        }
        if chain_filter in _gated_chains:
            raise ToolError(
                f'Chain {chain_filter} is not available — it is built on '
                f'options-venue metrics that cannot be redistributed.')

        if chain_filter != 'all' and chain_filter not in TRANSMISSION_CHAINS:
            raise ToolError(
                f'Unknown chain: {chain_filter}. '
                f'Available: {", ".join(TRANSMISSION_CHAINS.keys())}'
            )

        chains_to_query = (
            TRANSMISSION_CHAINS if chain_filter == 'all'
            else {chain_filter: TRANSMISSION_CHAINS[chain_filter]}
        )

        # Collect all link metrics needed
        all_link_metrics = []
        for cdef in chains_to_query.values():
            for a, b in cdef['links']:
                all_link_metrics.append(f'{a}__{b}')

        signals_ts = None
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT ts, metric, value, metadata FROM quant.signals
                WHERE source = %s AND metric = ANY(%s)
                  AND ts = (SELECT MAX(ts) FROM quant.signals
                            WHERE source = %s)
            """, (summary_source, all_link_metrics, summary_source))

            summaries = {}
            for r in cur.fetchall():
                meta = r['metadata'] if isinstance(r['metadata'], dict) \
                    else _json.loads(r['metadata'])
                summaries[r['metric']] = {
                    'lag': int(r['value']),
                    'r': meta.get('best_r', 0),
                    'persistent': meta.get('persistent'),
                }
                if signals_ts is None and r.get('ts') is not None:
                    signals_ts = str(r['ts'])

        lag_mult = 7 if freq == 'weekly' else 1
        result = []
        for cname, cdef in chains_to_query.items():
            links = []
            abs_rs = []
            for a, b in cdef['links']:
                key = f'{a}__{b}'
                s = summaries.get(key, {})
                r_val = s.get('r', 0)
                abs_rs.append(abs(r_val) if r_val else 0.001)
                links.append({
                    'from': a,
                    'to': b,
                    'lag_days': s.get('lag', 0) * lag_mult,
                    'correlation': round(r_val, 4) if r_val else None,
                    'persistent': s.get('persistent'),
                })

            geo_mean = math.exp(
                sum(math.log(r) for r in abs_rs) / len(abs_rs)
            )
            weakest = min(links, key=lambda l: abs(l.get('correlation') or 0))

            result.append({
                'chain': cname,
                'description': cdef['description'],
                'strength': round(geo_mean, 4),
                'weakest_link': f"{weakest['from']} → {weakest['to']}",
                'data_as_of': signals_ts,
                'links': links,
            })

        return {'chains': result, 'frequency': freq}

    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f'Transmission chain query failed: {e}')


@mcp_tool(
    'quant_get_analogues',
    'Find the historical dates whose macro+sentiment+derivatives z-score '
    'profile is most similar to today (or a given reference date). Uses '
    'cosine similarity on 90-day z-score vectors augmented with 7-day '
    'momentum deltas; dates within 90 days of the reference are excluded '
    '(overlapping windows). '
    'IMPORTANT — varying similarity basis: each analogue\'s cosine similarity '
    'is computed only over the metrics available on BOTH the reference date '
    'and that historical date, so different analogues can rest on different '
    'metric subsets (e.g. one entry 90% similar on 6 metrics, another 90% '
    'similar on 9). Top-level similarity_basis discloses analogue_metrics_total, '
    'reference_metrics_available, and min_common_required (5); each analogue '
    'entry\'s basis (e.g. "6/10") and n_common_metrics disclose its own subset '
    'size. Anyone quoting a similarity percentage should state the basis '
    'alongside it (e.g. "89% similar, basis 6/10") rather than the bare number. '
    'Returns: reference_date, current_vector (metric→z-score map), '
    'similarity_basis, and a ranked analogues list — each entry has: date, '
    'similarity (cosine, 0–1), vector (per-metric z-scores on that historical '
    'date), forward_return.7d/30d/90d (actual price return after that date), '
    'n_common_metrics, basis. '
    'Call this when building a probabilistic return outlook or stress-testing '
    'a thesis ("does today resemble any historical setup?"). '
    'Not suitable for real-time signals — use quant_get_anomalies for '
    'current outliers, or quant_get_regime for a live regime label.',
    {
        'type': 'object',
        'properties': {
            'target': {
                'type': 'string',
                'description': 'Asset for forward returns (default: BTC)',
            },
            'top': {
                'type': 'integer',
                'description': 'Number of analogues to return (default: 5, max: 10)',
            },
            'date': {
                'type': 'string',
                'description': 'Reference date YYYY-MM-DD. Default: latest date with ≥5 metrics available.',
            },
        },
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_analogues(arguments):
    target = arguments.get('target', 'BTC').upper()
    top_n = min(arguments.get('top', 5), 10)
    ref_date = arguments.get('date') or None

    try:
        from quant.engine.conditional import query_analogues

        result = query_analogues(
            target=target,
            top_n=top_n,
            reference_date=ref_date,
        )
        return result

    except Exception as e:
        raise ToolError(f'Analogue search failed: {e}')


@mcp_tool(
    'quant_get_leadlag_pair',
    'Return the full lead-lag correlogram for a single (leader, target) pair — '
    'Pearson r at every tested lag plus 95% confidence interval bounds. '
    'Also returns best: the lag with the highest |r| including its ci_low/ci_high. '
    'Use this to drill into one specific hypothesis (e.g. M2→BTC, '
    'FED_FUNDS→YIELD_2Y) and confirm the optimal lag and CI width before '
    'acting on it. For a broad ranking of all leaders for a target asset use '
    'quant_get_leadlag_ranking instead. For chain-level aggregation use quant_get_transmission_chains. '
    'leader and target must be canonical metric names (e.g. M2, VIX, DXY, BTC, '
    'YIELD_2Y, HY_SPREAD, SPX, GOLD, FUNDING_BTC, OI_BTC).',
    {
        'type': 'object',
        'properties': {
            'leader': {
                'type': 'string',
                'description': 'Leader metric (e.g. M2, FED_FUNDS, VIX). Required.',
            },
            'target': {
                'type': 'string',
                'description': 'Target metric (e.g. BTC, YIELD_2Y, SPX). Required.',
            },
            'frequency': {
                'type': 'string',
                'enum': ['daily', 'weekly'],
                'description': 'Analysis frequency. Default: daily.',
            },
        },
        'required': ['leader', 'target'],
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_leadlag_pair(arguments):
    leader = (arguments.get('leader') or '').strip().upper()
    target = (arguments.get('target') or '').strip().upper()
    if not leader or not target:
        raise ToolError('leader and target are required')
    # Correlations computed ON Deribit metrics are derived data.
    for _m in (leader, target):
        if not leadlag_metric_allowed(_m):
            raise ToolError(
                f'{_m} is not available. It derives from an options venue whose '
                f'terms do not permit redistribution. Perpetual-futures metrics '
                f'(FUNDING_BTC, OI_BTC, LONG_SHORT_BTC) are available.')
    freq = arguments.get('frequency', 'daily')
    method = 'leadlag_weekly' if freq == 'weekly' else 'leadlag'
    lag_mult = 7 if freq == 'weekly' else 1

    try:
        from carrac.db_pool import get_quant_conn as get_conn
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
            return {
                'leader': leader,
                'target': target,
                'frequency': freq,
                'correlogram': [],
                'best': None,
            }

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
        return {
            'leader': leader,
            'target': target,
            'frequency': freq,
            'correlogram': correlogram,
            'best': best,
        }
    except Exception as e:
        raise ToolError(f'Lead-lag pair query failed: {e}')


@mcp_tool(
    'quant_get_chain_history',
    'Return the daily time series of transmission chain strength for a named '
    'chain (monetary_policy, dollar_liquidity, risk_appetite, or '
    'derivatives_leverage). '
    'Chain strength is the geometric mean of |r| across all links (0–1; '
    'higher = tighter macro→BTC coupling). Each data point includes: '
    'date, strength (0–1), weakest_link (the from→to pair that dragged the '
    'geometric mean down on that day). '
    'derivatives_leverage chain: perpetual-futures positioning propagation. '
    'Call this to determine whether a chain is strengthening or breaking down '
    'over time — useful before relying on a macro signal for a trade decision. '
    'For the current snapshot + all-chain comparison use quant_get_transmission_chains. '
    'Max lookback: 365 days.',
    {
        'type': 'object',
        'properties': {
            'chain': {
                'type': 'string',
                'enum': ['monetary_policy', 'dollar_liquidity', 'risk_appetite',
                         'derivatives_leverage'],
                'description': 'Which chain to query (required)',
            },
            'days': {
                'type': 'integer',
                'description': 'Number of days of history to return (default: 30, max: 365)',
            },
        },
        'required': ['chain'],
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_chain_history(arguments):
    chain_name = (arguments.get('chain') or '').strip()
    valid = ('monetary_policy', 'dollar_liquidity', 'risk_appetite', 'derivatives_leverage')
    if chain_name not in valid:
        raise ToolError(f'chain must be one of {", ".join(valid)}')
    try:
        days = min(int(arguments.get('days', 30) or 30), 365)
    except (ValueError, TypeError):
        raise ToolError('days must be a positive integer')

    try:
        import json as _json
        from carrac.db_pool import get_quant_conn as get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT ts, value, metadata FROM quant.signals
                WHERE source = 'chain_strength' AND metric = %s
                  AND asset = '_GLOBAL_'
                  AND ts >= NOW() - INTERVAL '%s days'
                ORDER BY ts DESC
            """, (chain_name, days))
            rows = cur.fetchall()
        history = []
        for r in rows:
            meta = r['metadata'] or {}
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta)
                except ValueError:
                    meta = {}
            history.append({
                'date': r['ts'].isoformat() if r['ts'] else None,
                'strength': round(float(r['value']), 4),
                'weakest_link': meta.get('weakest_link'),
            })
        return {
            'chain': chain_name,
            'days': days,
            'count': len(history),
            'history': history,
        }
    except Exception as e:
        raise ToolError(f'Chain history query failed: {e}')


# No longer a standalone MCP tool — absorbed into alert_manage
# (action=create, alert_type=zscore|regime|chain_strength|anomaly_cluster|
# conditional). alert_manage.py's dispatch calls this directly with
# arguments['type'] set from alert_type, and re-enforces the quant_engine
# feature gate itself (this function has no @mcp_tool registration anymore,
# so the registry's automatic tier/feature gate no longer covers it — see
# alerts.py:_require_quant_engine). The legacy 'create_quant_alert' tool name
# still dispatches via registry.LEGACY_TOOL_ALIASES + LEGACY_ARG_DEFAULTS
# (forces action=create; this function reads type= same as before).
def create_quant_alert(arguments):
    import push_notifications
    atype = (arguments.get('type') or '').strip()
    user_id = str(get_mcp_user_id())

    if not push_notifications.user_has_notification_channel(user_id):
        raise ToolError(
            'No notification channel configured. Enable ntfy / Discord / Telegram / '
            'web push in your settings before creating quant alerts, or call '
            'notification_configure(action="set") in this session.')

    note = (arguments.get('note') or '')[:200]
    freq = arguments.get('frequency')

    try:
        if atype == 'zscore':
            metric = (arguments.get('metric') or '').strip().upper()
            condition = arguments.get('condition', '')
            threshold = arguments.get('threshold')
            if not metric:
                raise ToolError('metric is required for zscore alerts')
            if condition not in ('above', 'below'):
                raise ToolError('condition must be "above" or "below"')
            if threshold is None:
                raise ToolError('threshold is required for zscore alerts')
            try:
                from quant.engine.config import CANONICAL_METRICS
                if metric not in CANONICAL_METRICS:
                    raise ToolError(f'Unknown metric: {metric}')
            except ImportError:
                pass
            alert = push_notifications.create_zscore_alert(
                user_id=user_id,
                metric=metric,
                condition=condition,
                threshold=float(threshold),
                window=int(arguments.get('window', 90)),
                note=note,
                frequency=freq or 'once',
            )
            return {'alert': alert, 'status': 'created'}

        if atype == 'regime':
            alert = push_notifications.create_regime_alert(
                user_id=user_id,
                note=note,
                frequency=freq or 'once',
            )
            return {'alert': alert, 'status': 'created'}

        if atype == 'chain_strength':
            chain = (arguments.get('chain') or '').strip()
            condition = arguments.get('condition', 'below')
            threshold = arguments.get('threshold', 0.15)
            if chain not in ('monetary_policy', 'dollar_liquidity', 'risk_appetite'):
                raise ToolError('chain must be monetary_policy, dollar_liquidity, or risk_appetite')
            if condition not in ('above', 'below'):
                raise ToolError('condition must be "above" or "below"')
            try:
                alert = push_notifications.create_chain_strength_alert(
                    user_id=user_id,
                    chain=chain,
                    condition=condition,
                    threshold=float(threshold),
                    frequency=freq or 'once',
                    note=note,
                )
            except ValueError as e:
                raise ToolError(str(e))
            return {'alert': alert, 'status': 'created'}

        if atype == 'anomaly_cluster':
            min_domains = int(arguments.get('min_domains', 2))
            min_metrics = int(arguments.get('min_metrics', 3))
            try:
                alert = push_notifications.create_anomaly_cluster_alert(
                    user_id=user_id,
                    min_domains=min_domains,
                    min_metrics=min_metrics,
                    frequency=freq or 'once',
                    note=note,
                )
            except ValueError as e:
                raise ToolError(str(e))
            return {'alert': alert, 'status': 'created'}

        if atype == 'conditional':
            conditions = arguments.get('conditions') or []
            if not isinstance(conditions, list) or not (1 <= len(conditions) <= 5):
                raise ToolError('conditions must be a list of 1-5 strings')
            target = (arguments.get('target') or 'BTC').upper()
            horizon = int(arguments.get('horizon', 30) or 30)
            try:
                alert = push_notifications.create_conditional_alert(
                    user_id=user_id,
                    conditions=[str(c) for c in conditions],
                    target=target,
                    horizon=horizon,
                    frequency=freq or 'recurring',
                    note=note,
                )
            except ValueError as e:
                raise ToolError(str(e))
            return {'alert': alert, 'status': 'created'}

        raise ToolError(f'Unknown alert type: {atype}. '
                        f'Valid: zscore, regime, chain_strength, anomaly_cluster, conditional')

    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f'Quant alert creation failed: {e}')


_VALID_ANOMALY_DOMAINS = frozenset({
    'macro', 'sentiment', 'derivatives_options', 'derivatives_funding', 'price', 'all'
})


@mcp_tool(
    'quant_get_anomalies',
    'Return current z-score anomalies from the Quant Engine — metrics where '
    '|z-score| (90-day window) exceeds a threshold, ranked by severity '
    '(highest |z| first). '
    'Each result row includes: domain, metric, asset, zscore, percentile '
    '(0–100 historical rank), value (raw metric value), date. '
    'Domains: macro (VIX, M2, DXY, yields, credit), sentiment (fear/greed, '
    'funding, stablecoin flows, COT), derivatives_options (skew, IV, GEX), '
    'derivatives_funding (funding rate, basis), price (OHLCV-derived). '
    'Call this first when building a market context summary or scanning for '
    'unusual conditions. Default min_abs_zscore=2.0 returns 2-sigma+ outliers; '
    'raise to 3.0 for extreme-only. For a structured chain view use '
    'quant_get_transmission_chains; for the regime label use quant_get_regime.',
    {
        'type': 'object',
        'properties': {
            'min_abs_zscore': {
                'type': 'number',
                'description': 'Minimum |z-score| to include (default 2.0)',
            },
            'domain': {
                'type': 'string',
                'enum': ['macro', 'sentiment', 'derivatives_options',
                         'derivatives_funding', 'price', 'all'],
                'description': 'Domain filter (default: all)',
            },
            'top': {
                'type': 'integer',
                'description': 'Max results to return (default 25, max 100)',
            },
        },
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_anomalies(arguments):
    min_z = arguments.get('min_abs_zscore', 2.0)
    try:
        min_z = float(min_z)
    except (TypeError, ValueError):
        raise ToolError('min_abs_zscore must be a number')
    if min_z < 0:
        raise ToolError('min_abs_zscore must be >= 0')

    domain = (arguments.get('domain') or 'all').lower()
    if domain not in _VALID_ANOMALY_DOMAINS:
        raise ToolError(
            f'domain must be one of: {sorted(_VALID_ANOMALY_DOMAINS)}'
        )

    top = arguments.get('top', 25)
    try:
        top = int(top)
    except (TypeError, ValueError):
        raise ToolError('top must be an integer')
    top = max(1, min(top, 100))

    try:
        from carrac.db_pool import get_quant_conn as get_conn
        from quant.anomaly_filter import filter_anomalies, MAX_ABS_Z

        with get_conn() as conn:
            cur = conn.cursor()
            sql = """
                SELECT z.domain, z.metric, z.asset, z.zscore,
                       z.raw_value, z.ts, z.percentile
                FROM quant.zscores z
                INNER JOIN (
                    SELECT domain, metric, asset, MAX(ts) AS max_ts
                    FROM quant.zscores WHERE window_days = 90
                    GROUP BY domain, metric, asset
                ) latest
                    ON z.ts = latest.max_ts
                    AND z.domain = latest.domain
                    AND z.metric = latest.metric
                    AND z.asset = latest.asset
                WHERE z.window_days = 90
                  AND ABS(z.zscore) >= %s
                  AND ABS(z.zscore) <= %s
            """
            params = [min_z, MAX_ABS_Z]

            if domain != 'all':
                sql += ' AND z.domain = %s'
                params.append(domain)

            sql += ' ORDER BY ABS(z.zscore) DESC LIMIT %s'
            params.append(top)

            cur.execute(sql, params)
            rows = cur.fetchall()

            anomalies = []
            for r in rows:
                anomalies.append({
                    'domain': r['domain'],
                    'metric': r['metric'],
                    'asset': r['asset'],
                    'zscore': round(float(r['zscore']), 2),
                    'percentile': (
                        round(float(r['percentile']), 1)
                        if r.get('percentile') is not None else None
                    ),
                    'value': float(r['raw_value']) if r.get('raw_value') is not None else None,
                    'date': r['ts'].isoformat() if r.get('ts') else None,
                })

        anomalies = filter_anomalies(anomalies)
        return {
            'anomalies': anomalies,
            'count': len(anomalies),
            'filter': {
                'min_abs_zscore': min_z,
                'domain': domain,
                'top': top,
            },
        }

    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f'Quant anomalies query failed: {e}')


@mcp_tool(
    'quant_get_cot',
    'Return the current CFTC Commitment of Traders (COT) positioning picture. '
    'Reports the 18 COT series across 6 markets — Bitcoin (CME), Ethereum (CME), '
    'Gold (COMEX), WTI Crude Oil (NYMEX), S&P 500 e-mini (CME), and US Dollar '
    'Index (ICE) — with three metrics per market: large-speculator net contracts '
    '(SPEC_NET, the "smart money" proxy), commercial (hedger) net contracts '
    '(COMM_NET, typically contrarian), and large-spec long % of open interest '
    '(SPEC_LONG_PCT). '
    'Each series includes: value (raw contracts / %), zscore (90-day robust '
    'z-score), percentile (0–100 vs 90-day history), wow_change (week-over-week '
    'delta), stretched (bool — True when |z|>=2 or percentile>=90/<=10), and '
    'the data_date (CFTC publish date = as-of Tuesday + 3 days). '
    'Results are sorted by |z| descending so the most extreme positioning is first. '
    'COT data updates weekly (CFTC publishes Friday 3:30 PM ET). '
    'Use this to identify extreme speculative crowding or hedger capitulation '
    'before sizing a macro trade. A stretched SPEC_NET at the 90th+ percentile '
    'historically precedes mean-reversion; a stretched COMM_NET squeeze marks '
    'structural demand. Do not confuse SPEC_NET direction with price direction — '
    'commercials are systematically short gold/oil as production hedges. '
    'Returns: data_date, stretched_count, series list.',
    {
        'type': 'object',
        'properties': {
            'market': {
                'type': 'string',
                'enum': ['BTC', 'ETH', 'GOLD', 'OIL', 'SP500', 'DXY', 'all'],
                'description': 'Filter to a single market. Default: all.',
            },
        },
    },
    min_tier='member',
    feature='quant_engine',
)
def quant_get_cot(arguments):
    market_filter = (arguments.get('market') or 'all').upper()

    try:
        from quant.api.routes import build_cot

        result = build_cot()
        if result is None:
            raise ToolError('COT data unavailable')

        if market_filter != 'ALL':
            result = dict(result)
            result['series'] = [
                s for s in result['series'] if s['market'] == market_filter
            ]
            if not result['series']:
                raise ToolError(
                    f'No COT data for market {market_filter}. '
                    f'Available: BTC, ETH, GOLD, OIL, SP500, DXY'
                )
            result['stretched_count'] = sum(
                1 for s in result['series'] if s['stretched']
            )

        return result

    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f'COT query failed: {e}')
