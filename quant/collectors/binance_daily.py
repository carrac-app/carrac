"""
Binance daily data collector for Quant DB.

Fetches daily OHLCV (spot) + funding rates + OI + long/short ratio
for top crypto assets.

Backfill default: 2022-01-01 to present.
Daily mode: last 7 days (overlap for gap-filling).

Usage:
    python -m quant.collectors.binance_daily
    python -m quant.collectors.binance_daily --backfill
    python -m quant.collectors.binance_daily --dry-run
"""

import logging
import time
import requests
from datetime import datetime, timedelta, timezone

from carrac.db_pool import get_quant_conn as get_conn
from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.binance_daily')

# Tracked assets (top 10 by futures volume)
ASSETS = ['BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'DOGE', 'ADA', 'AVAX', 'LINK', 'DOT']

SPOT_KLINES = 'https://data-api.binance.vision/api/v3/klines'
FUNDING_RATE = 'https://fapi.binance.com/fapi/v1/fundingRate'
OPEN_INTEREST = 'https://fapi.binance.com/fapi/v1/openInterest'
LONG_SHORT = 'https://fapi.binance.com/futures/data/globalLongShortAccountRatio'


class BinanceDailyCollector(BaseCollector):
    name = 'binance_daily'
    expected_items = list(ASSETS)

    def collect(self):
        if self.backfill:
            start_str = self.backfill_start or '2022-01-01'
            start_ms = int(datetime.strptime(start_str, '%Y-%m-%d').timestamp() * 1000)
            logger.info(f"[binance_daily] Backfilling from {start_str}")
        else:
            start_ms = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)
            logger.info("[binance_daily] Daily update (last 7 days)")

        for asset in ASSETS:
            symbol = f'{asset}USDT'
            try:
                self._fetch_ohlcv(asset, symbol, start_ms)
                time.sleep(0.2)
            except Exception as e:
                logger.warning(f"  {asset} OHLCV: {e}")

            try:
                self._fetch_funding(asset, symbol, start_ms)
                time.sleep(0.2)
            except Exception as e:
                logger.warning(f"  {asset} funding: {e}")

            try:
                self._fetch_oi(asset, symbol)
                time.sleep(0.2)
            except Exception as e:
                logger.warning(f"  {asset} OI: {e}")

            try:
                self._fetch_long_short(asset, symbol)
                time.sleep(0.2)
            except Exception as e:
                logger.warning(f"  {asset} L/S: {e}")

    def _fetch_ohlcv(self, asset: str, symbol: str, start_ms: int):
        """Fetch daily spot OHLCV with pagination."""
        all_rows = []
        current_start = start_ms

        while True:
            resp = requests.get(SPOT_KLINES, params={
                'symbol': symbol,
                'interval': '1d',
                'startTime': current_start,
                'limit': 1000,
            }, timeout=15)
            resp.raise_for_status()
            candles = resp.json()

            if not candles:
                break

            for c in candles:
                ts = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
                all_rows.append({
                    'ts': ts,
                    'asset': asset,
                    'source': 'binance',
                    'open': float(c[1]),
                    'high': float(c[2]),
                    'low': float(c[3]),
                    'close': float(c[4]),
                    'volume': float(c[5]),
                })

            if len(candles) < 1000:
                break
            current_start = candles[-1][0] + 1
            time.sleep(0.1)

        if all_rows:
            count = self.upsert_rows(
                'quant.price_daily', all_rows,
                conflict_cols=['ts', 'asset', 'source'],
                update_cols=['open', 'high', 'low', 'close', 'volume'],
            )
            logger.info(f"  {asset} OHLCV: {len(all_rows)} candles, {count} upserted")

    def _fetch_funding(self, asset: str, symbol: str, start_ms: int):
        """Fetch funding rates, aggregate to daily average."""
        all_rates = []
        current_start = start_ms

        while True:
            resp = requests.get(FUNDING_RATE, params={
                'symbol': symbol,
                'startTime': current_start,
                'limit': 1000,
            }, timeout=15)
            resp.raise_for_status()
            rates = resp.json()

            if not rates:
                break

            all_rates.extend(rates)
            if len(rates) < 1000:
                break
            current_start = rates[-1]['fundingTime'] + 1
            time.sleep(0.1)

        # Aggregate to daily average
        daily = {}
        for r in all_rates:
            ts = datetime.fromtimestamp(r['fundingTime'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
            daily.setdefault(ts, []).append(float(r['fundingRate']))

        if daily:
            self._upsert_derivatives(
                [{
                    'ts': ts,
                    'asset': asset,
                    'funding_rate_avg': sum(vals) / len(vals),
                } for ts, vals in daily.items()],
                update_cols=['funding_rate_avg'],
            )
            logger.info(f"  {asset} funding: {len(daily)} days")

    def _fetch_oi(self, asset: str, symbol: str):
        """Fetch current open interest snapshot (converted to USD)."""
        resp = requests.get(OPEN_INTEREST,
                            params={'symbol': symbol}, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        oi_contracts = float(data.get('openInterest', 0))
        if oi_contracts > 0:
            # OI from API is in base asset units (e.g. BTC), convert to USD
            price_resp = requests.get(
                'https://fapi.binance.com/fapi/v1/ticker/price',
                params={'symbol': symbol}, timeout=10)
            price_resp.raise_for_status()
            mark_price = float(price_resp.json().get('price', 0))
            oi_usd = oi_contracts * mark_price if mark_price > 0 else oi_contracts
            self._upsert_derivatives(
                [{'ts': ts, 'asset': asset, 'oi_usd': oi_usd}],
                update_cols=['oi_usd'],
            )
            logger.info(f"  {asset} OI: ${oi_usd:,.0f} ({oi_contracts:,.2f} contracts)")

    def _fetch_long_short(self, asset: str, symbol: str):
        """Fetch global long/short ratio (last 30 days)."""
        resp = requests.get(LONG_SHORT, params={
            'symbol': symbol,
            'period': '1d',
            'limit': 30,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        rows = []
        for d in data:
            ts = datetime.fromtimestamp(d['timestamp'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d')
            rows.append({
                'ts': ts,
                'asset': asset,
                'long_short_ratio': float(d.get('longShortRatio', 0)),
            })

        if rows:
            self._upsert_derivatives(rows, update_cols=['long_short_ratio'])
            logger.info(f"  {asset} L/S: {len(rows)} days")

    def _upsert_derivatives(self, rows, update_cols):
        """Partial upsert into quant.derivatives.

        Only sets columns we have data for, leaves others unchanged.
        """
        if not rows or self.dry_run:
            if self.dry_run and rows:
                logger.info(f"[{self.name}] DRY RUN: {len(rows)} derivatives rows")
            return

        with get_conn() as conn:
            cur = conn.cursor()
            for row in rows:
                self._collected_items.add(row['asset'])  # Track for completeness
                cols = ['ts', 'asset'] + update_cols
                vals = [row['ts'], row['asset']] + [row.get(c) for c in update_cols]
                placeholders = ', '.join(['%s'] * len(cols))
                col_str = ', '.join(cols)
                set_str = ', '.join(f'{c} = EXCLUDED.{c}' for c in update_cols)

                cur.execute(f"""
                    INSERT INTO quant.derivatives ({col_str})
                    VALUES ({placeholders})
                    ON CONFLICT (ts, asset) DO UPDATE SET {set_str}
                """, vals)
                self._rows_upserted += cur.rowcount


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('Binance daily data collector')
    collector = BinanceDailyCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
    )
    collector.run()
