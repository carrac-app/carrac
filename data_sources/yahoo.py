"""
Yahoo Finance API client for stocks, ETFs, indices, forex, commodities.

Uses the v8 chart API directly with proper headers (bypasses yfinance library).
Two separate circuit breakers (daily / hourly) are kept per caller expectation
— daily outages often don't correlate with hourly outages on Yahoo's CDN.
"""

import pandas as pd
import logging
from datetime import datetime, timedelta
from typing import Optional

from data_sources.base import BaseDataSource
from data_sources.circuit_breaker import CircuitBreaker

logger = logging.getLogger('carrac.data_sources.yahoo')


class YahooClient(BaseDataSource):
    """Yahoo Finance v8 API client with per-endpoint circuit breakers."""

    NAME = 'yahoo'
    DEFAULT_TIMEOUT = 15.0

    # Canonical Yahoo Finance symbol mapping (indices, forex, commodities)
    SYMBOL_MAP = {
        # Indices (need ^ prefix)
        'SPX': '^GSPC', 'NDX': '^NDX', 'DJI': '^DJI', 'RUT': '^RUT',
        'VIX': '^VIX', 'DAX': '^GDAXI', 'FTSE': '^FTSE', 'N225': '^N225',
        'HSI': '^HSI', 'STOXX50E': '^STOXX50E',
        'NIKKEI': '^N225', 'HSTECH': '^HSTECH',
        'US10Y': '^TNX', 'US30Y': '^TYX', 'US5Y': '^FVX',
        'KOSPI': '^KS11',
        # Forex (use =X suffix)
        'EURUSD': 'EURUSD=X', 'GBPUSD': 'GBPUSD=X', 'USDJPY': 'USDJPY=X',
        'USDCHF': 'USDCHF=X', 'AUDUSD': 'AUDUSD=X', 'USDCAD': 'USDCAD=X',
        'EURGBP': 'EURGBP=X', 'EURJPY': 'EURJPY=X', 'NZDUSD': 'NZDUSD=X',
        'EURHUF': 'EURHUF=X', 'USDHUF': 'USDHUF=X',
        'DXY': 'DX-Y.NYB',
        # Commodities (futures use =F suffix)
        'OIL': 'CL=F', 'NATGAS': 'NG=F', 'COPPER': 'HG=F',
        'PLATINUM': 'PL=F', 'GOLD': 'GC=F', 'SILVER': 'SI=F',
        'CORN': 'ZC=F', 'WHEAT': 'ZW=F', 'SOYBEANS': 'ZS=F',
    }

    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    def __init__(self, daily_breaker: CircuitBreaker, hourly_breaker: CircuitBreaker):
        super().__init__(breaker=daily_breaker)
        self.daily_breaker = daily_breaker
        self.hourly_breaker = hourly_breaker

    def _check_rate_limit(self, provider=None):
        try:
            from api_rate_limiter import can_call_yahoo
            return can_call_yahoo()
        except Exception:
            return True, ''

    def _record_call(self, count: int = 1, provider=None):
        try:
            from api_rate_limiter import record_yahoo_call
            for _ in range(count):
                record_yahoo_call()
        except Exception:
            pass

    def resolve_symbol(self, symbol: str) -> str:
        """Map a symbol to its Yahoo Finance ticker format."""
        return self.SYMBOL_MAP.get(symbol.upper(), symbol.upper())

    def fetch_daily(self, symbol: str, start_date: Optional[datetime] = None) -> pd.DataFrame:
        """Fetch daily OHLCV data from Yahoo Finance v8 API."""
        yf_symbol = self.resolve_symbol(symbol)
        logger.info(f"Fetching {symbol} from Yahoo Finance API...")

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
        response = self._request(
            'GET', url,
            params={'interval': '1d', 'range': '2y'},
            headers=self.HEADERS,
            breaker=self.daily_breaker,
        )

        if response is None:
            return pd.DataFrame()

        # 404 = symbol not found — don't bump breaker (server healthy), return empty.
        if response.status_code == 404:
            logger.debug(f"Yahoo Finance 404 for {symbol} (not found)")
            return pd.DataFrame()

        if not response.ok:
            self.daily_breaker.record_failure()
            logger.warning(f"Yahoo Finance error for {symbol}: HTTP {response.status_code}")
            return pd.DataFrame()

        try:
            data = response.json()
        except Exception as e:
            self.daily_breaker.record_failure()
            logger.warning(f"Yahoo Finance parse error for {symbol}: {e}")
            return pd.DataFrame()

        result = data.get('chart', {}).get('result', [])
        if not result:
            self.daily_breaker.record_failure()
            logger.debug(f"No Yahoo data for {symbol}")
            return pd.DataFrame()

        chart = result[0]
        timestamps = chart.get('timestamp', [])
        quote = chart.get('indicators', {}).get('quote', [{}])[0]

        if not timestamps:
            self.daily_breaker.record_failure()
            return pd.DataFrame()

        df = pd.DataFrame({
            'open': quote.get('open', []),
            'high': quote.get('high', []),
            'low': quote.get('low', []),
            'close': quote.get('close', []),
            'volume': quote.get('volume', [])
        }, index=pd.to_datetime(timestamps, unit='s').normalize())

        df = df.dropna(subset=['close'])
        df = df[~df.index.duplicated(keep='last')]

        # This request is fixed-range — `range=2y` above, every
        # time, whatever `start_date` says — so the row count is expected to be
        # stable and a collapse means the SOURCE changed, not the market. That
        # is the state nothing could see when Yahoo's CL=F served one row for
        # any range asked while OIL's only feed silently stopped extending.
        # Never blocks the fetch: the frame is returned either way.
        try:
            from data_sources.source_health import check_fixed_range_fetch
            check_fixed_range_fetch('yahoo', symbol, 'daily:2y', len(df))
        except Exception:  # noqa: BLE001
            pass

        logger.info(f"Downloaded {len(df)} rows for {symbol} from Yahoo Finance")
        return df

    def fetch_hourly(self, symbol: str, hours: int = 500) -> pd.DataFrame:
        """Fetch hourly OHLCV data from Yahoo Finance API (for non-crypto assets)."""
        yf_symbol = self.resolve_symbol(symbol)

        end_ts = int(datetime.now().timestamp())
        start_ts = int((datetime.now() - timedelta(hours=hours)).timestamp())

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
        logger.info(f"Fetching {symbol} hourly from Yahoo Finance ({hours} hours)...")

        response = self._request(
            'GET', url,
            params={'interval': '1h', 'period1': start_ts, 'period2': end_ts},
            headers=self.HEADERS,
            breaker=self.hourly_breaker,
        )

        if response is None:
            return pd.DataFrame()

        if response.status_code == 404:
            logger.debug(f"Yahoo Finance hourly 404 for {symbol} (not found)")
            return pd.DataFrame()

        if not response.ok:
            self.hourly_breaker.record_failure()
            logger.warning(f"Yahoo Finance hourly error for {symbol}: HTTP {response.status_code}")
            return pd.DataFrame()

        try:
            data = response.json()
        except Exception as e:
            self.hourly_breaker.record_failure()
            logger.warning(f"Yahoo Finance hourly parse error for {symbol}: {e}")
            return pd.DataFrame()

        result = data.get('chart', {}).get('result', [])
        if not result:
            self.hourly_breaker.record_failure()
            logger.debug(f"No Yahoo hourly data for {symbol}")
            return pd.DataFrame()

        chart = result[0]
        timestamps = chart.get('timestamp', [])
        quote = chart.get('indicators', {}).get('quote', [{}])[0]

        if not timestamps:
            self.hourly_breaker.record_failure()
            return pd.DataFrame()

        df = pd.DataFrame({
            'open': quote.get('open', []),
            'high': quote.get('high', []),
            'low': quote.get('low', []),
            'close': quote.get('close', []),
            'volume': quote.get('volume', [])
        }, index=pd.to_datetime(timestamps, unit='s'))

        df = df.dropna(subset=['close'])
        df = df[df['close'] > 0]
        df = df[~df.index.duplicated(keep='last')].sort_index()

        logger.info(f"Downloaded {len(df)} hourly rows for {symbol} from Yahoo Finance")
        return df

    def validate_symbol(self, symbol: str) -> Optional[dict]:
        """Validate a symbol against Yahoo Finance. Returns info dict or None."""
        yahoo_sym = self.resolve_symbol(symbol.upper())
        resp = self._request(
            'GET',
            f'https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}',
            params={'range': '1d', 'interval': '1d'},
            headers=self.HEADERS,
            timeout=5,
            breaker=self.daily_breaker,
        )
        if resp is None or not resp.ok:
            return None
        try:
            result = resp.json().get('chart', {}).get('result')
            if result:
                meta = result[0].get('meta', {})
                name = meta.get('shortName') or meta.get('longName') or symbol.upper()
                asset_type = 'stock'
                instrument_type = meta.get('instrumentType', '').lower()
                if instrument_type in ('etf', 'mutualfund'):
                    asset_type = 'etf'
                elif instrument_type in ('index', 'currency'):
                    asset_type = instrument_type
                return {'symbol': symbol.upper(), 'name': name,
                        'type': asset_type, 'source': 'yahoo', 'tier': 'dynamic'}
        except Exception:
            pass
        return None
