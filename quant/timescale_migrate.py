"""
TimescaleDB migration script.

Enables the TimescaleDB extension and converts quant tables to hypertables.
Each step runs in its own transaction for resilience.

Core Carrac tables are NOT touched — only quant.* tables.

Usage:
    python -m quant.timescale_migrate
    python -m quant.timescale_migrate --compress
"""

import argparse
import logging

from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger('quant.timescale_migrate')

# Tables to convert: (table_name, time_column, segmentby_columns)
HYPERTABLES = [
    ('quant.price_daily',  'ts', 'asset, source'),
    ('quant.derivatives',  'ts', 'asset'),
    ('quant.macro',        'ts', 'metric'),
    ('quant.sentiment',    'ts', 'metric, asset'),
    ('quant.zscores',      'ts', 'domain, metric, asset'),
]


def _run_sql(sql, params=None, label=""):
    """Run a single SQL statement in its own transaction."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            logger.info(f"  {label}: OK")
            return True
    except Exception as e:
        logger.warning(f"  {label}: {e}")
        return False


def migrate(enable_compression: bool = False):
    """Enable TimescaleDB and convert quant tables to hypertables."""

    # Step 1: Enable extension
    logger.info("Creating TimescaleDB extension...")
    _run_sql(
        "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE",
        label="TimescaleDB extension"
    )

    # Step 2: Convert tables to hypertables (each in own transaction)
    for table, time_col, segmentby in HYPERTABLES:
        table_short = table.split('.')[1]

        # Check if already a hypertable
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*) as cnt FROM timescaledb_information.hypertables
                    WHERE hypertable_schema = 'quant' AND hypertable_name = %s
                """, (table_short,))
                row = cur.fetchone()
                if row and row['cnt'] > 0:
                    logger.info(f"  {table}: already a hypertable, skipping")
                    continue
        except Exception:
            pass  # View might not exist yet if extension just loaded

        _run_sql(
            "SELECT create_hypertable(%s, by_range(%s), migrate_data => true)",
            params=(table, time_col),
            label=f"{table} → hypertable"
        )

    # Step 3: Enable compression (each in own transaction)
    if enable_compression:
        logger.info("Enabling compression policies...")
        for table, time_col, segmentby in HYPERTABLES:
            _run_sql(
                f"ALTER TABLE {table} SET ("
                f"  timescaledb.compress,"
                f"  timescaledb.compress_segmentby = '{segmentby}',"
                f"  timescaledb.compress_orderby = '{time_col}'"
                f")",
                label=f"{table} compress config"
            )
            _run_sql(
                "SELECT add_compression_policy(%s, INTERVAL '30 days', if_not_exists => true)",
                params=(table,),
                label=f"{table} compress policy"
            )

    logger.info("TimescaleDB migration complete")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(description='TimescaleDB migration')
    parser.add_argument('--compress', action='store_true',
                        help='Enable compression policies')
    args = parser.parse_args()
    migrate(enable_compression=args.compress)
