"""
Multi-exchange funding rate collector for Quant DB via ccxt.

Aggregates funding rates from OKX and Bybit (Binance already collected
separately). Computes cross-exchange average for a more robust signal.

No API keys needed — all public endpoints.

Schedule: Daily (00:45 UTC, after Binance at 00:15).
Backfill: Funding rate history from OKX and Bybit (varies by exchange).

Usage:
    python -m quant.collectors.ccxt_funding
    python -m quant.collectors.ccxt_funding --backfill
    python -m quant.collectors.ccxt_funding --dry-run
"""

import logging
import time
from datetime import datetime, timezone

from carrac.db_pool import get_quant_conn as get_conn
from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.ccxt_funding')

# Assets to collect (must match Binance collector)
ASSETS = ['BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'DOGE', 'ADA', 'AVAX', 'LINK', 'DOT']

# Exchanges to aggregate (Binance is already in binance_daily collector)
EXCHANGES = {
    'okx': {
        'symbol_fmt': '{asset}/USDT:USDT',
    },
    'bybit': {
        'symbol_fmt': '{asset}/USDT:USDT',
    },
}


class CcxtFundingCollector(BaseCollector):
    name = 'ccxt_funding'
    expected_items = list(ASSETS)

    def collect(self):
        try:
            import ccxt
        except ImportError:
            logger.error("ccxt not installed. Run: pip install ccxt")
            raise

        for exchange_id, config in EXCHANGES.items():
            try:
                exchange = getattr(ccxt, exchange_id)()
                self._collect_exchange(exchange, exchange_id, config)
            except Exception as e:
                logger.warning(f"  {exchange_id}: {e}")

        # Compute cross-exchange averages
        self._compute_cross_avg()

    def _collect_exchange(self, exchange, exchange_id, config):
        """Fetch funding rate history from one exchange."""
        for asset in ASSETS:
            symbol = config['symbol_fmt'].format(asset=asset)
            try:
                if self.backfill:
                    rates = exchange.fetch_funding_rate_history(symbol, limit=500)
                else:
                    rates = exchange.fetch_funding_rate_history(symbol, limit=30)

                if not rates:
                    logger.info(f"  {exchange_id} {asset}: no funding data")
                    continue

                # Aggregate to daily average
                daily = {}
                for r in rates:
                    ts = datetime.fromtimestamp(r['timestamp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
                    daily.setdefault(ts, []).append(r['fundingRate'])

                rows = []
                for ts, fr_list in daily.items():
                    rows.append({
                        'ts': ts,
                        'metric': f'FUNDING_{asset}_{exchange_id.upper()}',
                        'asset': asset,
                        'value': sum(fr_list) / len(fr_list),
                        'source': exchange_id,
                    })

                if rows:
                    count = self.upsert_rows(
                        'quant.sentiment', rows,
                        conflict_cols=['ts', 'metric', 'asset'],
                        update_cols=['value', 'source'],
                    )
                    logger.info(f"  {exchange_id} {asset}: {len(rows)} days, {count} upserted")

                time.sleep(0.5)

            except Exception as e:
                logger.warning(f"  {exchange_id} {asset}: {e}")
                time.sleep(1)

    def _compute_cross_avg(self):
        """Compute cross-exchange funding average from all available sources.

        Reads Binance funding from quant.derivatives, OKX/Bybit from quant.sentiment,
        and writes the average to quant.derivatives.funding_rate_cross_avg.
        """
        if self.dry_run:
            logger.info("[ccxt_funding] DRY RUN: skipping cross-avg computation")
            return

        with get_conn() as conn:
            cur = conn.cursor()

            for asset in ASSETS:
                cur.execute("""
                    SELECT ts, AVG(rate) as avg_rate FROM (
                        SELECT ts, funding_rate_avg as rate
                        FROM quant.derivatives
                        WHERE asset = %s AND funding_rate_avg IS NOT NULL
                        UNION ALL
                        SELECT ts, value as rate
                        FROM quant.sentiment
                        WHERE metric LIKE %s AND asset = %s
                    ) all_rates
                    GROUP BY ts
                    ORDER BY ts
                """, (asset, f'FUNDING_{asset}_%', asset))

                rows = cur.fetchall()
                updated = 0
                for row in rows:
                    cur.execute("""
                        INSERT INTO quant.derivatives (ts, asset, funding_rate_cross_avg)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (ts, asset) DO UPDATE
                        SET funding_rate_cross_avg = EXCLUDED.funding_rate_cross_avg
                    """, (row['ts'], asset, row['avg_rate']))
                    updated += cur.rowcount

                if updated:
                    self._rows_upserted += updated
                    logger.info(f"  Cross-avg {asset}: {updated} days updated")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('Multi-exchange funding rate collector (ccxt)')
    collector = CcxtFundingCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
    )
    collector.run()
