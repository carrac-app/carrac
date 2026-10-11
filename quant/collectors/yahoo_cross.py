"""
Yahoo Finance cross-asset collector for Quant DB.

Fetches daily OHLCV for traditional market assets: DXY, VIX, gold, oil,
indices, yields, copper, silver.

Backfill default: 2000-01-01 (Yahoo has deep history for majors).
Daily mode: last 7 days.

Note: Yahoo v8 API is unofficial. We add polite delays (2s per ticker)
to avoid throttling.

Usage:
    python -m quant.collectors.yahoo_cross
    python -m quant.collectors.yahoo_cross --backfill
"""

import logging
import time
import requests
from datetime import datetime, timedelta, timezone

from quant.collectors.base import BaseCollector

logger = logging.getLogger('quant.collectors.yahoo_cross')

# asset_name -> (yahoo_ticker, description)
YAHOO_ASSETS = {
    # US Indices
    'DXY':      ('DX-Y.NYB',   'US Dollar Index'),
    'VIX':      ('^VIX',        'CBOE Volatility Index'),
    'SPX':      ('^GSPC',       'S&P 500'),
    'NASDAQ':   ('^IXIC',       'NASDAQ Composite'),
    # Global Indices
    'STOXX50':  ('^STOXX50E',   'Euro Stoxx 50'),
    'FTSE':     ('^FTSE',       'FTSE 100'),
    'NIKKEI':   ('^N225',       'Nikkei 225'),
    'SHANGHAI': ('000001.SS',   'Shanghai Composite'),
    'HSI':      ('^HSI',        'Hang Seng Index'),
    # Commodities
    'GOLD_F':   ('GC=F',        'Gold Futures'),
    'OIL_F':    ('CL=F',        'WTI Crude Oil Futures'),
    'SILVER_F': ('SI=F',        'Silver Futures'),
    'COPPER_F': ('HG=F',        'Copper Futures'),
    # Bonds & Yields
    'US10Y':    ('^TNX',        '10-Year Treasury Yield'),
    'TLT':      ('TLT',         '20+ Year Treasury Bond ETF'),
    'HYG':      ('HYG',         'High Yield Corporate Bond ETF'),
    'EU_BOND':  ('IBGL.L',      'iShares Euro Govt Bond'),
    'MOVE':     ('^MOVE',       'ICE BofA MOVE Index (bond VIX)'),
    # FX
    'EURUSD':   ('EURUSD=X',    'EUR/USD'),
    'GBPUSD':   ('GBPUSD=X',    'GBP/USD'),
    'USDJPY':   ('USDJPY=X',    'USD/JPY'),
    'USDCNY':   ('USDCNH=X',    'USD/CNH (offshore yuan)'),
    # Sector ETFs
    'XLF':      ('XLF',         'Financial Select Sector SPDR'),
    'XLK':      ('XLK',         'Technology Select Sector SPDR'),
    'XLE':      ('XLE',         'Energy Select Sector SPDR'),
    'XLV':      ('XLV',         'Health Care Select Sector SPDR'),
    # Emerging Markets & Real Estate
    'EEM':      ('EEM',         'iShares MSCI Emerging Markets'),
    'FXI':      ('FXI',         'iShares China Large-Cap'),
    'IYR':      ('IYR',         'iShares US Real Estate'),
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}


class YahooCrossCollector(BaseCollector):
    name = 'yahoo_cross'
    expected_items = list(YAHOO_ASSETS.keys())

    def collect(self):
        if self.backfill:
            start_str = self.backfill_start or '2000-01-01'
            period1 = int(datetime.strptime(start_str, '%Y-%m-%d').timestamp())
            logger.info(f"[yahoo_cross] Backfilling from {start_str}")
        else:
            period1 = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
            logger.info("[yahoo_cross] Daily update (last 7 days)")

        period2 = int(datetime.now(timezone.utc).timestamp())

        for asset_name, (ticker, desc) in YAHOO_ASSETS.items():
            try:
                self._fetch_ticker(asset_name, ticker, period1, period2)
            except Exception as e:
                logger.warning(f"  {asset_name} ({ticker}): {e}")
            time.sleep(2)  # Be very polite with unofficial API

    def _fetch_ticker(self, asset_name: str, ticker: str,
                      period1: int, period2: int):
        """Fetch daily data for one Yahoo ticker."""
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
        resp = requests.get(url, params={
            'interval': '1d',
            'period1': period1,
            'period2': period2,
        }, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        result = resp.json().get('chart', {}).get('result', [])
        if not result:
            logger.warning(f"  {asset_name}: no data")
            return

        chart = result[0]
        timestamps = chart.get('timestamp', [])
        quote = chart.get('indicators', {}).get('quote', [{}])[0]

        if not timestamps:
            return

        n = len(timestamps)
        opens = quote.get('open', [None] * n)
        highs = quote.get('high', [None] * n)
        lows = quote.get('low', [None] * n)
        closes = quote.get('close', [None] * n)
        volumes = quote.get('volume', [None] * n)

        rows = []
        for i, ts_epoch in enumerate(timestamps):
            close = closes[i]
            if close is None:
                continue
            ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime('%Y-%m-%d')
            rows.append({
                'ts': ts,
                'asset': asset_name,
                'source': 'yahoo',
                'open': opens[i],
                'high': highs[i],
                'low': lows[i],
                'close': close,
                'volume': volumes[i],
            })

        if rows:
            count = self.upsert_rows(
                'quant.price_daily', rows,
                conflict_cols=['ts', 'asset', 'source'],
                update_cols=['open', 'high', 'low', 'close', 'volume'],
            )
            logger.info(f"  {asset_name}: {len(rows)} days, {count} upserted")


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    args = BaseCollector.parse_args('Yahoo Finance cross-asset collector')
    collector = YahooCrossCollector(
        dry_run=args.dry_run,
        backfill=args.backfill,
        backfill_start=args.backfill_start,
    )
    collector.run()
