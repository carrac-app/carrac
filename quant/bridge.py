"""Quant ↔ Chart Bridge: align quant time series to chart bar timestamps.

Serves quant data (macro, sentiment, derivatives, regime, cross-asset prices)
pre-aligned to the chart's OHLCV array length for direct overlay rendering
and formula parser consumption.
"""

import logging
from datetime import datetime, timedelta, timezone

from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger(__name__)

# ── Series Catalog ──────────────────────────────────────────────────────
# Maps user-facing series names to DB queries.
# Each entry: (domain, query_template, value_column)
# Query template gets {date_filter} placeholder for WHERE clause.

SERIES_CATALOG = {
    # Macro overlays
    'M2': ('macro', "SELECT ts, value FROM quant.macro WHERE metric = 'M2'{date_filter} ORDER BY ts", 'value'),
    'NET_LIQUIDITY': ('macro', "SELECT ts, value FROM quant.macro WHERE metric = 'NET_LIQUIDITY'{date_filter} ORDER BY ts", 'value'),
    'DXY': ('price', "SELECT ts, close AS value FROM quant.price_daily WHERE asset = 'DXY'{date_filter} ORDER BY ts", 'value'),
    'HY_SPREAD': ('macro', "SELECT ts, value FROM quant.macro WHERE metric = 'HY_SPREAD'{date_filter} ORDER BY ts", 'value'),
    'YIELD_CURVE': ('macro', "SELECT ts, value FROM quant.macro WHERE metric = 'YIELD_10Y2Y'{date_filter} ORDER BY ts", 'value'),

    # Sentiment
    'FEAR_GREED': ('sentiment', "SELECT ts, value FROM quant.sentiment WHERE metric = 'FEAR_GREED'{date_filter} ORDER BY ts", 'value'),
    'ALT_SEASON': ('sentiment', "SELECT ts, value FROM quant.sentiment WHERE metric = 'ALTCOIN_SEASON' AND asset = '_GLOBAL_'{date_filter} ORDER BY ts", 'value'),
    'STABLECOIN_USDT_SUPPLY': ('sentiment', "SELECT ts, value FROM quant.sentiment WHERE metric = 'STABLECOIN_USDT_SUPPLY'{date_filter} ORDER BY ts", 'value'),

    # Derivatives
    'FUNDING': ('derivatives', "SELECT ts, funding_rate_avg AS value FROM quant.derivatives WHERE asset = 'BTC'{date_filter} ORDER BY ts", 'value'),
    'DVOL': ('derivatives', "SELECT ts, dvol AS value FROM quant.derivatives WHERE asset = 'BTC' AND dvol IS NOT NULL{date_filter} ORDER BY ts", 'value'),
    'BASIS_APR': ('derivatives', "SELECT ts, basis_pct AS value FROM quant.derivatives WHERE asset = 'BTC' AND basis_pct IS NOT NULL{date_filter} ORDER BY ts", 'value'),
    'SKEW_25D': ('derivatives', "SELECT ts, skew_25d AS value FROM quant.derivatives WHERE asset = 'BTC' AND skew_25d IS NOT NULL{date_filter} ORDER BY ts", 'value'),
    'GEX': ('derivatives', "SELECT ts, gex_net AS value FROM quant.derivatives WHERE asset = 'BTC' AND gex_net IS NOT NULL{date_filter} ORDER BY ts", 'value'),
    'IV_RV_PREMIUM': ('derivatives', "SELECT ts, iv_rv_premium AS value FROM quant.derivatives WHERE asset = 'BTC' AND iv_rv_premium IS NOT NULL{date_filter} ORDER BY ts", 'value'),
    'ETF_FLOW': ('onchain', "SELECT ts, value FROM quant.onchain WHERE metric = 'ETF_BTC_NET_FLOW'{date_filter} ORDER BY ts", 'value'),

    # Regime (from quant engine signals). Map label → 1 (favorable) / 0 (unfavorable)
    # so the chart renderer is stable across HMM retrainings (state indices can flip).
    'REGIME': ('signals', "SELECT ts, CASE metadata->>'label' WHEN 'favorable' THEN 1 WHEN 'unfavorable' THEN 0 ELSE NULL END AS value FROM quant.signals WHERE source = 'regime_hmm' AND metric = 'REGIME' AND asset = '_GLOBAL_'{date_filter} ORDER BY ts", 'value'),

    # Cross-asset prices
    'BTC': ('price', "SELECT ts, close AS value FROM quant.price_daily WHERE asset = 'BTC' AND source = 'binance'{date_filter} ORDER BY ts", 'value'),
    'ETH': ('price', "SELECT ts, close AS value FROM quant.price_daily WHERE asset = 'ETH' AND source = 'binance'{date_filter} ORDER BY ts", 'value'),
    'SOL': ('price', "SELECT ts, close AS value FROM quant.price_daily WHERE asset = 'SOL' AND source = 'binance'{date_filter} ORDER BY ts", 'value'),
    'SPX': ('price', "SELECT ts, close AS value FROM quant.price_daily WHERE asset = 'SPX'{date_filter} ORDER BY ts", 'value'),
    'GOLD_F': ('price', "SELECT ts, close AS value FROM quant.price_daily WHERE asset = 'GOLD_F'{date_filter} ORDER BY ts", 'value'),
    'VIX': ('price', "SELECT ts, close AS value FROM quant.price_daily WHERE asset = 'VIX'{date_filter} ORDER BY ts", 'value'),

    # PCA composite factors
    'RISK_APPETITE': ('sentiment', "SELECT ts, value FROM quant.sentiment WHERE metric = 'FACTOR_RISK_APPETITE' AND asset = '_GLOBAL_'{date_filter} ORDER BY ts", 'value'),
    'LIQUIDITY_FACTOR': ('sentiment', "SELECT ts, value FROM quant.sentiment WHERE metric = 'FACTOR_LIQUIDITY' AND asset = '_GLOBAL_'{date_filter} ORDER BY ts", 'value'),
    'CREDIT_STRESS_FACTOR': ('sentiment', "SELECT ts, value FROM quant.sentiment WHERE metric = 'FACTOR_CREDIT_STRESS' AND asset = '_GLOBAL_'{date_filter} ORDER BY ts", 'value'),

    # Transmission chain strengths — written nightly by leadlag engine
    'CHAIN_MONETARY_POLICY': ('signals', "SELECT ts, value FROM quant.signals WHERE source = 'chain_strength' AND metric = 'monetary_policy' AND asset = '_GLOBAL_'{date_filter} ORDER BY ts", 'value'),
    'CHAIN_DOLLAR_LIQUIDITY': ('signals', "SELECT ts, value FROM quant.signals WHERE source = 'chain_strength' AND metric = 'dollar_liquidity' AND asset = '_GLOBAL_'{date_filter} ORDER BY ts", 'value'),
    'CHAIN_RISK_APPETITE': ('signals', "SELECT ts, value FROM quant.signals WHERE source = 'chain_strength' AND metric = 'risk_appetite' AND asset = '_GLOBAL_'{date_filter} ORDER BY ts", 'value'),
}

# Display metadata per series
SERIES_META = {
    # WM2NS observations are dated weekly, but FRED only PUBLISHES
    # them via the H.6 release (~monthly, batches 4-5 weeks at once) — so new
    # points actually land on a monthly cadence, not weekly. Labelled 'monthly'
    # here since that is what matters for a user's/monitor's staleness
    # expectations.
    'M2':                    {'label': 'M2 Money Supply ($B)', 'frequency': 'monthly', 'axis': 'right'},
    'NET_LIQUIDITY':         {'label': 'Net Liquidity ($B)', 'frequency': 'weekly', 'axis': 'right'},
    'DXY':                   {'label': 'DXY (Dollar Index)', 'frequency': 'daily', 'axis': 'right'},
    'HY_SPREAD':             {'label': 'HY Spread (%)', 'frequency': 'daily', 'axis': 'own_pane'},
    'YIELD_CURVE':           {'label': '10Y-2Y Spread (%)', 'frequency': 'daily', 'axis': 'own_pane'},
    'FEAR_GREED':            {'label': 'Fear & Greed', 'frequency': 'daily', 'axis': 'own_pane', 'range': [0, 100]},
    'ALT_SEASON':            {'label': 'Altcoin Season', 'frequency': 'daily', 'axis': 'own_pane', 'range': [0, 100]},
    'STABLECOIN_USDT_SUPPLY':{'label': 'USDT Supply ($B)', 'frequency': 'daily', 'axis': 'right'},
    'FUNDING':               {'label': 'BTC Funding Rate (3-exch)', 'frequency': 'daily', 'axis': 'own_pane'},
    'DVOL':                  {'label': 'Deribit Vol Index', 'frequency': '6h', 'axis': 'own_pane'},
    'BASIS_APR':             {'label': 'Futures Basis APR %', 'frequency': '6h', 'axis': 'own_pane'},
    'SKEW_25D':              {'label': '25-Delta Skew %', 'frequency': '6h', 'axis': 'own_pane'},
    'GEX':                   {'label': 'Gamma Exposure (USD)', 'frequency': '6h', 'axis': 'own_pane'},
    'IV_RV_PREMIUM':         {'label': 'IV-RV Premium %', 'frequency': '6h', 'axis': 'own_pane'},
    'ETF_FLOW':              {'label': 'BTC ETF Net Flow ($M)', 'frequency': 'daily', 'axis': 'own_pane'},
    'REGIME':                {'label': 'Market Regime', 'frequency': 'daily', 'axis': 'background'},
    'BTC':                   {'label': 'BTC Price', 'frequency': 'daily', 'axis': 'right'},
    'ETH':                   {'label': 'ETH Price', 'frequency': 'daily', 'axis': 'right'},
    'SOL':                   {'label': 'SOL Price', 'frequency': 'daily', 'axis': 'right'},
    'SPX':                   {'label': 'S&P 500', 'frequency': 'daily', 'axis': 'right'},
    'GOLD_F':                {'label': 'Gold', 'frequency': 'daily', 'axis': 'right'},
    'VIX':                   {'label': 'VIX', 'frequency': 'daily', 'axis': 'own_pane'},
    # PCA composite factors
    'RISK_APPETITE':         {'label': 'Risk Appetite Factor', 'frequency': 'daily', 'axis': 'own_pane', 'range': [-3, 3]},
    'LIQUIDITY_FACTOR':      {'label': 'Liquidity Factor', 'frequency': 'daily', 'axis': 'own_pane', 'range': [-3, 3]},
    'CREDIT_STRESS_FACTOR':  {'label': 'Credit Stress Factor', 'frequency': 'daily', 'axis': 'own_pane', 'range': [-3, 3]},
    # Transmission chain strengths
    'CHAIN_MONETARY_POLICY':  {'label': 'Chain: Monetary Policy',  'frequency': 'daily', 'axis': 'own_pane', 'range': [0, 1]},
    'CHAIN_DOLLAR_LIQUIDITY': {'label': 'Chain: Dollar Liquidity', 'frequency': 'daily', 'axis': 'own_pane', 'range': [0, 1]},
    'CHAIN_RISK_APPETITE':    {'label': 'Chain: Risk Appetite',    'frequency': 'daily', 'axis': 'own_pane', 'range': [0, 1]},
}


def _fetch_series(series_name, start_date=None, end_date=None):
    """Fetch raw quant series from DB. Returns list of (date, value) tuples."""
    if series_name not in SERIES_CATALOG:
        return []

    # Deribit-backed series are admin-only. This is the narrowest
    # chokepoint for the DATA — it covers the formula engine
    # (formula_eval._load_quant_series_cached) and GET /api/quant/chart-series
    # in one place, so a new consumer of the bridge cannot leak them by
    # forgetting to ask. The formula layer ALSO refuses to recognise the
    # variable, so a user gets an unknown-variable error rather than a formula
    # that silently evaluates against an empty series.
    from carrac.compliance.deribit_gate import is_gated_series
    if is_gated_series(series_name):
        logger.info(
            "bridge: %s withheld — Deribit-derived, admin only",
            series_name,
        )
        return []

    _domain, query_tpl, _val_col = SERIES_CATALOG[series_name]

    # Build date filter
    date_parts = []
    params = []
    if start_date:
        date_parts.append("ts >= %s")
        params.append(start_date)
    if end_date:
        date_parts.append("ts <= %s")
        params.append(end_date)
    date_filter = (' AND ' + ' AND '.join(date_parts)) if date_parts else ''
    sql = query_tpl.format(date_filter=date_filter)

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [(row['ts'], float(row['value'])) for row in rows if row['value'] is not None]
    except Exception as e:
        logger.error(f"[bridge] Failed to fetch {series_name}: {e}")
        return []


def align_to_chart(quant_ts, chart_timestamps):
    """Align quant time series to chart bar timestamps using forward-fill.

    Args:
        quant_ts: list of (date, float) — sorted by date ascending
        chart_timestamps: list of int — Unix epoch seconds, sorted ascending

    Returns:
        list of (float | None) — same length as chart_timestamps
    """
    n = len(chart_timestamps)
    if not quant_ts or n == 0:
        return [None] * n

    out = [None] * n
    qi = 0
    qn = len(quant_ts)
    last_val = None

    for ci in range(n):
        # Convert chart timestamp to date
        chart_date = datetime.fromtimestamp(chart_timestamps[ci], tz=timezone.utc).date()

        # Advance quant pointer to the last value on or before this chart date
        while qi < qn and quant_ts[qi][0] <= chart_date:
            last_val = quant_ts[qi][1]
            qi += 1

        out[ci] = last_val

    return out


def get_aligned_series(requested_series, chart_timestamps):
    """Fetch and align multiple quant series to chart timestamps.

    Args:
        requested_series: list of series names (e.g., ['M2', 'FEAR_GREED', 'REGIME'])
        chart_timestamps: list of int — Unix epoch seconds from OHLCV

    Returns:
        dict: {series_name: {values: [...], label: str, frequency: str, axis: str, ...}}
    """
    if not chart_timestamps or not requested_series:
        return {}

    # Determine date range from chart timestamps (with buffer for forward-fill)
    start_ts = chart_timestamps[0]
    end_ts = chart_timestamps[-1]
    start_date = datetime.fromtimestamp(start_ts, tz=timezone.utc).date() - timedelta(days=90)
    end_date = datetime.fromtimestamp(end_ts, tz=timezone.utc).date()

    result = {}
    for name in requested_series:
        name_upper = name.upper().strip()
        if name_upper not in SERIES_CATALOG:
            continue

        raw = _fetch_series(name_upper, start_date=start_date, end_date=end_date)
        if not raw:
            continue

        values = align_to_chart(raw, chart_timestamps)
        meta = SERIES_META.get(name_upper, {})

        # Find latest non-None value's date
        latest_date = None
        if raw:
            latest_date = str(raw[-1][0])

        result[name_upper] = {
            'values': values,
            'label': meta.get('label', name_upper),
            'frequency': meta.get('frequency', 'daily'),
            'axis': meta.get('axis', 'right'),
            'latest_date': latest_date,
        }
        if 'range' in meta:
            result[name_upper]['range'] = meta['range']

    return result
