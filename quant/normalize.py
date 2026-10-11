"""
Quant DB normalization engine.

Computes derived metrics from raw collected data:
- Z-scores across all domains (30d, 90d rolling windows)
- Run nightly after all collectors finish.

Usage:
    python -m quant.normalize
    python -m quant.normalize --dry-run
"""

import argparse
import logging
import math

from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger('quant.normalize')

# Each domain query must return: ts, metric, asset, value
# Look-ahead fix: lagged-publication sources (macro=FRED, sentiment=COT) align
# on the knowledge date COALESCE(released_at, ts) so z-scores are attributed to
# when the data was actually published, not its as-of date. NULL released_at
# (real-time series) falls back to ts. Other domains are real-time → plain ts.
DOMAIN_QUERIES = {
    'macro': """
        SELECT COALESCE(released_at::date, ts) AS ts, metric, '_GLOBAL_' as asset, value
        FROM quant.macro
        ORDER BY metric, COALESCE(released_at::date, ts)
    """,
    'sentiment': """
        SELECT COALESCE(released_at::date, ts) AS ts, metric, asset, value
        FROM quant.sentiment
        ORDER BY metric, asset, COALESCE(released_at::date, ts)
    """,
    'derivatives_funding': """
        SELECT ts, 'FUNDING_' || asset as metric, asset,
               funding_rate_avg as value
        FROM quant.derivatives
        WHERE funding_rate_avg IS NOT NULL
        ORDER BY asset, ts
    """,
    'derivatives_ls': """
        SELECT ts, 'LS_RATIO_' || asset as metric, asset,
               long_short_ratio as value
        FROM quant.derivatives
        WHERE long_short_ratio IS NOT NULL
        ORDER BY asset, ts
    """,
    'derivatives_options': """
        SELECT ts, 'OPTIONS_PC_' || asset as metric, asset,
               options_put_call_ratio as value
        FROM quant.derivatives
        WHERE options_put_call_ratio IS NOT NULL
        UNION ALL
        SELECT ts, 'OPTIONS_IV_' || asset as metric, asset,
               options_iv_atm as value
        FROM quant.derivatives
        WHERE options_iv_atm IS NOT NULL
        UNION ALL
        SELECT ts, 'DVOL_' || asset as metric, asset,
               dvol as value
        FROM quant.derivatives
        WHERE dvol IS NOT NULL
        ORDER BY metric, asset, ts
    """,
    'derivatives_advanced': """
        SELECT ts, 'SKEW_25D_' || asset as metric, asset,
               skew_25d as value
        FROM quant.derivatives
        WHERE skew_25d IS NOT NULL
        UNION ALL
        SELECT ts, 'BASIS_' || asset as metric, asset,
               basis_pct as value
        FROM quant.derivatives
        WHERE basis_pct IS NOT NULL
        UNION ALL
        SELECT ts, 'GEX_' || asset as metric, asset,
               gex_net as value
        FROM quant.derivatives
        WHERE gex_net IS NOT NULL
        UNION ALL
        SELECT ts, 'IV_RV_' || asset as metric, asset,
               iv_rv_premium as value
        FROM quant.derivatives
        WHERE iv_rv_premium IS NOT NULL
        ORDER BY metric, asset, ts
    """,
    'onchain': """
        SELECT ts, metric, asset, value
        FROM quant.onchain
        ORDER BY metric, asset, ts
    """,
    # MOVE (ICE BofA Bond VIX) lives in quant.price_daily source=yahoo.
    # It is not in quant.macro so the 'macro' domain query above misses it.
    # Domain 'price_vol' handles raw price-level volatility indices that
    # are collected via yahoo_cross but need z-scoring for the snapshot.
    # Metric name = asset name so downstream consumers can use 'MOVE' directly.
    'price_vol': """
        SELECT ts, asset AS metric, asset, close AS value
        FROM quant.price_daily
        WHERE source = 'yahoo' AND asset IN ('MOVE')
        ORDER BY asset, ts
    """,
}


def _compute_derived_metrics(dry_run: bool = False, latest_only: bool = False):
    """Compute derived metrics from raw collected data.

    NET_LIQUIDITY = FED_BALANCE - TGA - REVERSE_REPO
    Aligns to weekly dates (FED_BALANCE frequency).
    Must run BEFORE z-score computation so derived metrics get z-scored.
    """
    from psycopg2.extras import execute_values

    date_filter = "AND ts >= CURRENT_DATE - INTERVAL '14 days'" if latest_only else ""
    with get_conn() as conn:
        cur = conn.cursor()
        # Fetch source series
        cur.execute(f"""
            SELECT metric, ts, value FROM quant.macro
            WHERE metric IN ('FED_BALANCE', 'TGA', 'REVERSE_REPO')
            {date_filter}
            ORDER BY metric, ts
        """)
        rows = cur.fetchall()

    # Group by metric -> {date: value}
    series = {}
    for r in rows:
        series.setdefault(r['metric'], {})[r['ts']] = float(r['value'])

    fed = series.get('FED_BALANCE', {})
    tga = series.get('TGA', {})
    rrp = series.get('REVERSE_REPO', {})

    if not fed:
        logger.info("  derived: no FED_BALANCE data — skipping NET_LIQUIDITY")
        return

    # REVERSE_REPO is daily; build sorted list for closest-date lookup
    rrp_dates = sorted(rrp.keys())

    def _closest_on_or_before(target, sorted_dates, values):
        """Find the value at the closest date on or before target."""
        best = None
        for d in sorted_dates:
            if d <= target:
                best = d
            else:
                break
        return values[best] if best is not None else None

    derived_rows = []
    fed_dates = sorted(fed.keys())
    if latest_only:
        fed_dates = fed_dates[-1:]  # Only compute latest value
    for dt in fed_dates:
        fed_val = fed[dt]
        tga_val = tga.get(dt)
        rrp_val = _closest_on_or_before(dt, rrp_dates, rrp)
        if tga_val is None or rrp_val is None:
            continue
        net_liq = fed_val - tga_val - rrp_val
        derived_rows.append((str(dt), 'NET_LIQUIDITY', net_liq, None, 0))

    if not derived_rows:
        logger.info("  derived: no aligned data for NET_LIQUIDITY")
        return

    if dry_run:
        logger.info("  derived: DRY RUN — would write %d NET_LIQUIDITY rows", len(derived_rows))
        return

    sql = """
        INSERT INTO quant.macro (ts, metric, value, released_at, revision)
        VALUES %s
        ON CONFLICT (ts, metric)
        DO UPDATE SET value = EXCLUDED.value
    """
    with get_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, sql, derived_rows, page_size=1000)

    logger.info("  derived: %d NET_LIQUIDITY rows upserted", len(derived_rows))


def _compute_altcoin_season(dry_run: bool = False, latest_only: bool = False):
    """Compute Altcoin Season Index from existing Binance daily data.

    Index = % of top altcoins outperforming BTC over a 90-day window.
    Range: 0-100 (100 = full altseason, 0 = full BTC dominance).
    Stored in quant.sentiment as ALTCOIN_SEASON.
    """
    from psycopg2.extras import execute_values

    ALTCOINS = ['ETH', 'SOL', 'XRP', 'BNB', 'DOGE', 'ADA', 'AVAX', 'LINK', 'DOT']
    WINDOW = 90

    all_assets = ['BTC'] + ALTCOINS
    date_filter = "AND ts >= CURRENT_DATE - INTERVAL '91 days'" if latest_only else ""
    with get_conn() as conn:
        cur = conn.cursor()
        placeholders = ','.join(['%s'] * len(all_assets))
        cur.execute(f"""
            SELECT asset, ts, close FROM quant.price_daily
            WHERE source = 'binance' AND asset IN ({placeholders})
            {date_filter}
            ORDER BY asset, ts
        """, all_assets)
        rows = cur.fetchall()

    # Group by asset -> [(ts, close), ...]
    by_asset = {}
    for r in rows:
        by_asset.setdefault(r['asset'], {})[r['ts']] = float(r['close'])

    btc = by_asset.get('BTC', {})
    if not btc:
        logger.info("  altcoin_season: no BTC data — skipping")
        return

    btc_dates = sorted(btc.keys())
    if len(btc_dates) < WINDOW + 1:
        logger.info("  altcoin_season: insufficient BTC data — skipping")
        return

    season_rows = []
    for i in range(WINDOW, len(btc_dates)):
        dt = btc_dates[i]
        dt_past = btc_dates[i - WINDOW]
        btc_ret = (btc[dt] / btc[dt_past]) - 1 if btc[dt_past] > 0 else 0

        outperforming = 0
        counted = 0
        for alt in ALTCOINS:
            alt_data = by_asset.get(alt, {})
            if dt in alt_data and dt_past in alt_data and alt_data[dt_past] > 0:
                alt_ret = (alt_data[dt] / alt_data[dt_past]) - 1
                counted += 1
                if alt_ret > btc_ret:
                    outperforming += 1

        if counted >= 5:  # need at least 5 altcoins for a meaningful index
            index_val = (outperforming / counted) * 100
            season_rows.append((
                str(dt), 'ALTCOIN_SEASON', '_GLOBAL_',
                round(index_val, 2), 'computed',
            ))

    if not season_rows:
        logger.info("  altcoin_season: no data computed")
        return

    if dry_run:
        logger.info("  altcoin_season: DRY RUN — would write %d rows", len(season_rows))
        return

    sql = """
        INSERT INTO quant.sentiment (ts, metric, asset, value, source)
        VALUES %s
        ON CONFLICT (ts, metric, asset)
        DO UPDATE SET value = EXCLUDED.value
    """
    with get_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, sql, season_rows, page_size=1000)

    logger.info("  altcoin_season: %d rows upserted", len(season_rows))


def _compute_cross_sectional_dispersion(dry_run: bool = False,
                                        latest_only: bool = False):
    """Compute daily cross-sectional dispersion of altcoin 30d returns.

    Dispersion = std(30d_return) across 9 altcoins on each date.
    High dispersion = rotation/stock-picking market.
    Low dispersion = macro-driven (everything moves together).
    Stored in quant.sentiment as CROSS_DISPERSION.
    """
    from math import sqrt
    from psycopg2.extras import execute_values

    ALTCOINS = ['ETH', 'SOL', 'XRP', 'BNB', 'DOGE', 'ADA', 'AVAX', 'LINK', 'DOT']
    WINDOW = 30

    date_filter = "AND ts >= CURRENT_DATE - INTERVAL '31 days'" if latest_only else ""
    with get_conn() as conn:
        cur = conn.cursor()
        placeholders = ','.join(['%s'] * len(ALTCOINS))
        cur.execute(f"""
            SELECT asset, ts, close FROM quant.price_daily
            WHERE source = 'binance' AND asset IN ({placeholders})
            {date_filter}
            ORDER BY asset, ts
        """, ALTCOINS)
        rows = cur.fetchall()

    by_asset = {}
    for r in rows:
        by_asset.setdefault(r['asset'], {})[r['ts']] = float(r['close'])

    # Use any alt's dates as reference (they share Binance daily dates)
    all_dates = set()
    for prices in by_asset.values():
        all_dates.update(prices.keys())
    all_dates = sorted(all_dates)

    if len(all_dates) < WINDOW + 1:
        logger.info("  cross_dispersion: insufficient data — skipping")
        return

    dispersion_rows = []
    for i in range(WINDOW, len(all_dates)):
        dt = all_dates[i]
        dt_past = all_dates[i - WINDOW]

        returns = []
        for alt in ALTCOINS:
            alt_data = by_asset.get(alt, {})
            if dt in alt_data and dt_past in alt_data and alt_data[dt_past] > 0:
                returns.append((alt_data[dt] / alt_data[dt_past]) - 1)

        if len(returns) >= 5:
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
            std_ret = sqrt(variance)
            dispersion_rows.append((
                str(dt), 'CROSS_DISPERSION', '_GLOBAL_',
                round(std_ret, 6), 'computed',
            ))

    if not dispersion_rows:
        logger.info("  cross_dispersion: no data computed")
        return

    if dry_run:
        logger.info("  cross_dispersion: DRY RUN — would write %d rows",
                     len(dispersion_rows))
        return

    sql = """
        INSERT INTO quant.sentiment (ts, metric, asset, value, source)
        VALUES %s
        ON CONFLICT (ts, metric, asset)
        DO UPDATE SET value = EXCLUDED.value
    """
    with get_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, sql, dispersion_rows, page_size=1000)

    logger.info("  cross_dispersion: %d rows upserted", len(dispersion_rows))


def _compute_realized_volatility(dry_run: bool = False, latest_only: bool = False):
    """Compute Parkinson realized volatility from daily OHLCV data.

    Parkinson vol = sqrt(mean(ln(high/low)^2) / (4 * ln(2))) * sqrt(252) * 100
    Computed over a 30-day rolling window, annualized, as a percentage.
    Stored in quant.sentiment as REALIZED_VOL_{asset}.
    """
    from psycopg2.extras import execute_values

    ASSETS = ['BTC', 'ETH']
    WINDOW = 30

    date_filter = "AND ts >= CURRENT_DATE - INTERVAL '31 days'" if latest_only else ""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT asset, ts, high, low FROM quant.price_daily
            WHERE source = 'binance' AND asset IN %s
              AND high IS NOT NULL AND low IS NOT NULL
              AND high > 0 AND low > 0
              {date_filter}
            ORDER BY asset, ts
        """, (tuple(ASSETS),))
        rows = cur.fetchall()

    if not rows:
        logger.info("  realized_vol: no OHLCV data")
        return

    by_asset = {}
    for r in rows:
        by_asset.setdefault(r['asset'], []).append(r)

    vol_rows = []
    for asset, price_rows in by_asset.items():
        price_rows.sort(key=lambda x: x['ts'])
        dates = [r['ts'] for r in price_rows]

        # Parkinson intraday range: ln(high/low)^2
        hl_sq = []
        for r in price_rows:
            h, l = float(r['high']), float(r['low'])
            hl_sq.append(math.log(h / l) ** 2 if h > l else 0.0)

        for i in range(WINDOW - 1, len(hl_sq)):
            window_vals = hl_sq[i - WINDOW + 1:i + 1]
            # Parkinson estimator, annualized for crypto.
            # Was sqrt(252) (stocks convention). Crypto trades 24/7,
            # so RV must be annualized over 365 days to match the DVOL index
            # Deribit publishes. Mixing sqrt(252) RV with sqrt(365) IV put a
            # structural +20% bias into the IV-RV premium headline.
            parkinson = math.sqrt(
                sum(window_vals) / len(window_vals) / (4 * math.log(2))
            ) * math.sqrt(365) * 100

            vol_rows.append((
                str(dates[i]), f'REALIZED_VOL_{asset}', asset,
                round(parkinson, 2), 'computed',
            ))

    if not vol_rows:
        logger.info("  realized_vol: no data computed")
        return

    if dry_run:
        logger.info("  realized_vol: DRY RUN — would write %d rows", len(vol_rows))
        return

    sql = """
        INSERT INTO quant.sentiment (ts, metric, asset, value, source)
        VALUES %s
        ON CONFLICT (ts, metric, asset)
        DO UPDATE SET value = EXCLUDED.value
    """
    with get_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, sql, vol_rows, page_size=1000)

    logger.info("  realized_vol: %d rows upserted", len(vol_rows))


def compute_zscores(dry_run: bool = False, latest_only: bool = False):
    """Compute z-scores for all metrics across all domains.

    Args:
        dry_run: Log actions without writing to DB.
        latest_only: Only compute z-scores for the latest date (nightly mode).
                     Reduces writes from ~50K rows to ~50 per domain.
    """
    mode = "latest-only" if latest_only else "full"
    logger.info(f"Computing z-scores ({mode} mode)...")
    _compute_derived_metrics(dry_run, latest_only)
    _compute_altcoin_season(dry_run, latest_only)
    _compute_cross_sectional_dispersion(dry_run, latest_only)
    _compute_realized_volatility(dry_run, latest_only)
    total = 0

    for domain, query in DOMAIN_QUERIES.items():
        try:
            count = _process_domain(domain, query, dry_run, latest_only)
            total += count
        except Exception as e:
            logger.warning(f"Z-score computation failed for {domain}: {e}",
                           exc_info=True)

    logger.info(f"Z-scores complete: {total} total rows written")
    return total


def _process_domain(domain: str, query: str, dry_run: bool,
                    latest_only: bool = False) -> int:
    """Compute z-scores for one domain.

    In latest_only mode, fetches the last 91 OBSERVATIONS per (metric, asset)
    rather than 91 calendar days.  This ensures sparse monthly series (e.g.
    M2) still accumulate enough points (91 obs >> 90-day window) to produce a
    z-score, whereas a plain 91-day date filter yields only ~3 rows and fails
    the ``len(values) > window`` check — causing nightly cron to silently skip
    those metrics forever.

    The full (non-latest-only) path is untouched.
    """
    with get_conn() as conn:
        cur = conn.cursor()
        if latest_only:
            # Wrap the domain query so we keep the last 91 observations per
            # (metric, asset) group.  The ROW_NUMBER() window orders by ts DESC
            # so rn=1 is the most recent row; we keep rn <= 91.
            # The wrapper preserves the exact column names produced by each
            # domain query (ts, metric, asset, value) which the grouping logic
            # below expects unchanged.
            wrapped = (
                "SELECT ts, metric, asset, value "
                "FROM ("
                "  SELECT *, ROW_NUMBER() OVER ("
                "    PARTITION BY metric, asset ORDER BY ts DESC"
                "  ) AS rn "
                f"  FROM ({query}) _inner"
                ") _ranked "
                "WHERE rn <= 91"
            )
            cur.execute(wrapped)
        else:
            cur.execute(query)
        data = cur.fetchall()

    if not data:
        logger.info(f"  {domain}: no data")
        return 0

    # Group by (metric, asset)
    groups = {}
    for row in data:
        key = (row['metric'], row['asset'])
        groups.setdefault(key, []).append((row['ts'], float(row['value'])))

    zscore_rows = []
    for (metric, asset), series in groups.items():
        series.sort(key=lambda x: x[0])

        if latest_only:
            # Only keep last 91 points (enough for 90-day window + 1)
            series = series[-91:]

        dates = [d for d, _ in series]
        values = [v for _, v in series]

        for window in (30, 90):
            if latest_only:
                # Only compute for the very last point
                indices = [len(values) - 1] if len(values) > window else []
            else:
                indices = range(window, len(values))

            for i in indices:
                window_vals = values[i - window:i]

                # Robust z-score: median / MAD (resistant to fat tails)
                sorted_w = sorted(window_vals)
                n = len(sorted_w)
                median = (sorted_w[n // 2] + sorted_w[(n - 1) // 2]) / 2
                abs_devs = sorted(abs(v - median) for v in window_vals)
                mad = (abs_devs[n // 2] + abs_devs[(n - 1) // 2]) / 2
                scaled_mad = mad * 1.4826  # consistency factor for normal equivalence

                if scaled_mad == 0:
                    continue  # constant series — no meaningful z-score

                zscore = (values[i] - median) / scaled_mad

                # Percentile rank: % of window values <= current value
                rank_below = sum(1 for v in window_vals if v <= values[i])
                percentile = round((rank_below / len(window_vals)) * 100, 1)

                zscore_rows.append((
                    str(dates[i]), domain, metric, asset, window,
                    round(zscore, 4),
                    values[i],
                    round(median, 6), round(scaled_mad, 6),
                    percentile,
                ))

    if not zscore_rows:
        logger.info(f"  {domain}: no z-scores to compute")
        return 0

    if dry_run:
        logger.info(f"  {domain}: DRY RUN -- would write {len(zscore_rows)} z-scores")
        return len(zscore_rows)

    # Batch upsert using execute_values for performance
    from psycopg2.extras import execute_values

    sql = """
        INSERT INTO quant.zscores
            (ts, domain, metric, asset, window_days,
             zscore, raw_value, mean, std, percentile)
        VALUES %s
        ON CONFLICT (ts, domain, metric, asset, window_days)
        DO UPDATE SET zscore     = EXCLUDED.zscore,
                      raw_value  = EXCLUDED.raw_value,
                      mean       = EXCLUDED.mean,
                      std        = EXCLUDED.std,
                      percentile = EXCLUDED.percentile
    """
    with get_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, sql, zscore_rows, page_size=1000)
    count = len(zscore_rows)

    logger.info(f"  {domain}: {count} z-scores upserted")
    return count


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(description='Quant DB normalization')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--latest-only', action='store_true',
                        help='Only compute z-scores for latest date (nightly mode)')
    args = parser.parse_args()
    compute_zscores(dry_run=args.dry_run, latest_only=args.latest_only)
