"""
Quant DB schema management.

Creates the quant schema and all tables. Safe to run multiple times
(all statements use IF NOT EXISTS).

Usage:
    python -m quant.schema          # Apply schema
    python -m quant.schema --drop   # Drop and recreate (DESTROYS DATA)
"""

import logging

from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger('quant.schema')

SCHEMA_SQL = """
-- ==========================================================
-- Quant DB Schema v1
-- Separate schema for market intelligence data.
-- Kill switch: DROP SCHEMA quant CASCADE
-- ==========================================================

CREATE SCHEMA IF NOT EXISTS quant;

-- ----------------------------------------------------------
-- Price & Volume (daily OHLCV from multiple sources)
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.price_daily (
    ts      DATE        NOT NULL,
    asset   TEXT        NOT NULL,
    source  TEXT        NOT NULL DEFAULT 'binance',
    open    NUMERIC,
    high    NUMERIC,
    low     NUMERIC,
    close   NUMERIC,
    volume  NUMERIC,
    PRIMARY KEY (ts, asset, source)
);
CREATE INDEX IF NOT EXISTS idx_price_daily_asset
    ON quant.price_daily(asset, ts);

-- ----------------------------------------------------------
-- Derivatives (funding, OI, long/short, liquidations)
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.derivatives (
    ts                      DATE    NOT NULL,
    asset                   TEXT    NOT NULL,
    funding_rate_avg        NUMERIC,
    oi_usd                  NUMERIC,
    oi_change_pct           NUMERIC,
    long_short_ratio        NUMERIC,
    liquidations_long       NUMERIC,
    liquidations_short      NUMERIC,
    basis_pct               NUMERIC,
    -- Options (Phase 2, Deribit)
    options_put_call_ratio  NUMERIC,
    options_oi_usd          NUMERIC,
    options_max_pain        NUMERIC,
    options_iv_atm          NUMERIC,
    dvol                    NUMERIC,
    -- Advanced derivatives (Deribit computed)
    skew_25d                NUMERIC,
    iv_7d                   NUMERIC,
    iv_30d                  NUMERIC,
    iv_90d                  NUMERIC,
    term_structure_slope    NUMERIC,
    gex_net                 NUMERIC,
    gex_flip_price          NUMERIC,
    iv_rv_premium           NUMERIC,
    -- Multi-exchange funding (Phase 2, ccxt)
    funding_rate_cross_avg  NUMERIC,
    PRIMARY KEY (ts, asset)
);

-- ----------------------------------------------------------
-- Macro indicators (FRED + other sources)
-- released_at tracks when data was actually published
-- (prevents look-ahead bias in backtests)
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.macro (
    ts          DATE        NOT NULL,
    metric      TEXT        NOT NULL,
    value       NUMERIC     NOT NULL,
    released_at TIMESTAMPTZ,
    revision    INTEGER     DEFAULT 0,
    PRIMARY KEY (ts, metric)
);
CREATE INDEX IF NOT EXISTS idx_macro_metric
    ON quant.macro(metric, ts);

-- ----------------------------------------------------------
-- On-chain data (exchange flows, MVRV, etc.)
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.onchain (
    ts      DATE    NOT NULL,
    asset   TEXT    NOT NULL,
    metric  TEXT    NOT NULL,
    value   NUMERIC NOT NULL,
    source  TEXT,
    PRIMARY KEY (ts, asset, metric)
);

-- ----------------------------------------------------------
-- Sentiment (Fear & Greed, COT, stablecoin supply, etc.)
-- asset = '_GLOBAL_' for broad market metrics
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.sentiment (
    ts          DATE    NOT NULL,
    metric      TEXT    NOT NULL,
    asset       TEXT    NOT NULL DEFAULT '_GLOBAL_',
    value       NUMERIC NOT NULL,
    source      TEXT,
    -- released_at: actual publish date for lagged sources (COT = reported
    -- Tuesday, published Friday). NULL for real-time sentiment; analysis
    -- COALESCEs back to ts. Prevents look-ahead bias.
    released_at TIMESTAMPTZ,
    PRIMARY KEY (ts, metric, asset)
);

-- ----------------------------------------------------------
-- Flexible signals (experimental / new metrics)
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.signals (
    ts       DATE    NOT NULL,
    source   TEXT    NOT NULL,
    metric   TEXT    NOT NULL,
    asset    TEXT    NOT NULL DEFAULT '_GLOBAL_',
    value    NUMERIC NOT NULL,
    metadata JSONB,
    PRIMARY KEY (ts, source, metric, asset)
);
CREATE INDEX IF NOT EXISTS idx_signals_lookup
    ON quant.signals(source, metric, asset, ts DESC);

-- ----------------------------------------------------------
-- Calendar events (FOMC, CPI, expiry, halving, etc.)
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.events (
    id          SERIAL  PRIMARY KEY,
    ts          DATE    NOT NULL,
    event_type  TEXT    NOT NULL,
    title       TEXT    NOT NULL,
    description TEXT,
    impact      TEXT,
    expected TEXT,
    metadata    JSONB,
    -- actual: formatted printed value filled by fill_event_actuals()
    --         after FRED publishes (e.g. "+0.2% MoM / 3.4% YoY").
    --         NULL until filled. Safe to query on IS NOT NULL.
    actual           TEXT,
    actual_filled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_events_ts
    ON quant.events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type
    ON quant.events(event_type, ts);

-- Migration: add actual columns to pre-existing events table (idempotent).
-- ALTER IF NOT EXISTS requires PG 9.6+; production runs PG 14+.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'quant' AND table_name = 'events'
          AND column_name = 'actual'
    ) THEN
        ALTER TABLE quant.events ADD COLUMN actual TEXT;
        ALTER TABLE quant.events ADD COLUMN actual_filled_at TIMESTAMPTZ;
    END IF;
END $$;

-- Migration: add expected column to pre-existing events table (idempotent).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'quant' AND table_name = 'events'
          AND column_name = 'expected'
    ) THEN
        ALTER TABLE quant.events ADD COLUMN expected TEXT;
    END IF;
END $$;

-- ----------------------------------------------------------
-- Z-scores (refreshed nightly by normalize.py)
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.zscores (
    ts          DATE    NOT NULL,
    domain      TEXT    NOT NULL,
    metric      TEXT    NOT NULL,
    asset       TEXT    NOT NULL DEFAULT '_GLOBAL_',
    window_days INTEGER NOT NULL,
    zscore      NUMERIC NOT NULL,
    raw_value   NUMERIC,
    mean        NUMERIC,
    std         NUMERIC,
    percentile  NUMERIC,
    PRIMARY KEY (ts, domain, metric, asset, window_days)
);
CREATE INDEX IF NOT EXISTS idx_zscores_lookup
    ON quant.zscores(domain, metric, asset, window_days, ts DESC);

-- ----------------------------------------------------------
-- Collector health log
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.collection_log (
    id            SERIAL      PRIMARY KEY,
    collector     TEXT        NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    status        TEXT        NOT NULL DEFAULT 'running',
    rows_upserted INTEGER     DEFAULT 0,
    error_msg     TEXT,
    metadata      JSONB
);
CREATE INDEX IF NOT EXISTS idx_clog_collector
    ON quant.collection_log(collector, started_at DESC);

-- ----------------------------------------------------------
-- Phase 3: Correlations (pre-created for forward compat)
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.correlations (
    ts          DATE    NOT NULL,
    metric_a    TEXT    NOT NULL,
    metric_b    TEXT    NOT NULL,
    window_days INTEGER NOT NULL,
    correlation NUMERIC NOT NULL,
    p_value     NUMERIC,
    n_obs       INTEGER,
    method      TEXT    NOT NULL DEFAULT 'pearson',
    stability   NUMERIC,
    q_value     NUMERIC,  -- BH-FDR corrected p-value, per (ts, method, window)
    ci_low      NUMERIC,  -- 95% CI lower bound (Fisher z-transform)
    ci_high     NUMERIC,  -- 95% CI upper bound
    significant BOOLEAN,  -- q<0.05 AND n_obs>=30 AND |r|>=0.1
    PRIMARY KEY (ts, metric_a, metric_b, window_days, method)
);
CREATE INDEX IF NOT EXISTS idx_correlations_metric_b
    ON quant.correlations(metric_b, ts);

-- ----------------------------------------------------------
-- Phase 3: Anomalies
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS quant.anomalies (
    id          SERIAL  PRIMARY KEY,
    ts          DATE    NOT NULL,
    domain      TEXT    NOT NULL,
    metric      TEXT    NOT NULL,
    asset       TEXT,
    zscore      NUMERIC NOT NULL,
    raw_value   NUMERIC,
    window_days INTEGER NOT NULL,
    resolved_at DATE,
    metadata    JSONB
);
CREATE INDEX IF NOT EXISTS idx_anomalies_active
    ON quant.anomalies(ts DESC) WHERE resolved_at IS NULL;

-- ----------------------------------------------------------
-- Liquidation heatmap
-- ----------------------------------------------------------

-- Ring-buffer of individual Binance forced-liquidation events. The live WS
-- feed (data_sources/binance_liq_ws.py) only ever kept 60-minute in-memory
-- rolling sums + daily aggregate totals -- no per-event history existed
-- anywhere, and Binance has no historical REST endpoint for past
-- liquidation orders (unlike OI/klines/long-short-ratio, which do). This
-- table is that missing per-event history, persisted so the validation
-- harness (scripts/validate_liq_heatmap.py) has something to replay against.
-- Capped by periodic pruning in binance_liq_ws.py (not a DB-level cap) --
-- see BinanceLiqWSWorker.EVENTS_RETENTION_DAYS.
CREATE TABLE IF NOT EXISTS quant.liquidation_events (
    id           SERIAL      PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,
    asset        TEXT        NOT NULL,
    side         TEXT        NOT NULL,  -- 'long' or 'short' (side liquidated)
    price        NUMERIC     NOT NULL,
    qty          NUMERIC,
    notional_usd NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_liquidation_events_asset_ts
    ON quant.liquidation_events(asset, ts DESC);

-- One row per (symbol, compute cycle) -- the modeled liquidation heatmap is a
-- TIME x PRICE grid (owner design addendum), so the API serves a *series* of
-- snapshots, not just the latest. `bands` is the sparse, threshold-filtered
-- list of {price_low, price_high, intensity_long, intensity_short} (weak
-- buckets below HEATMAP_PAINT_QUANTILE are dropped before storage -- see
-- carrac/liq_heatmap.py filter_bands_by_threshold()). Capped retention via
-- periodic pruning in carrac/liq_heatmap.py (SNAPSHOT_RETENTION_DAYS).
CREATE TABLE IF NOT EXISTS quant.liq_heatmap_snapshots (
    symbol    TEXT        NOT NULL,
    cycle_ts  TIMESTAMPTZ NOT NULL,
    price     NUMERIC,
    band_low  NUMERIC,
    band_high NUMERIC,
    bands     JSONB       NOT NULL,
    PRIMARY KEY (symbol, cycle_ts)
);
CREATE INDEX IF NOT EXISTS idx_liq_heatmap_snapshots_lookup
    ON quant.liq_heatmap_snapshots(symbol, cycle_ts DESC);
"""


def apply_schema():
    """Create the quant schema and all tables."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(SCHEMA_SQL)
        logger.info("Quant DB schema applied successfully")


def drop_schema():
    """Drop the entire quant schema. DESTROYS ALL QUANT DATA."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DROP SCHEMA IF EXISTS quant CASCADE")
        logger.info("Quant schema DROPPED")


if __name__ == '__main__':
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    if '--drop' in sys.argv:
        drop_schema()
    apply_schema()
