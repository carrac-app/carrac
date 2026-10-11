"""
Hyperliquid API client for OHLCV data on coins not listed on Binance.

Mirrors data_sources/binance.py:BinanceClient signatures so OHLCVDatabase
can swap providers without callers caring. REST only; live candles come from hyperliquid_ws.

# WARNING: Hyperliquid candleSnapshot returns at most 5000 bars per request.
#          For HIP-3 listings <30 days old, depth will be limited.
#          Don't use this client to backfill multi-year history — use Binance
#          for any coin Binance has.

# Operational kill switch: set HYPERLIQUID_DISABLED=1 in the env to disable
# this client entirely (e.g. during an HL outage). All fetches will return
# empty DataFrames and the fallback chain will move on to CCXT.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from data_sources.base import BaseDataSource

logger = logging.getLogger('carrac.data_sources.hyperliquid')


class HyperliquidClient(BaseDataSource):
    """Hyperliquid public /info REST client (no auth required)."""

    NAME = 'hyperliquid'
    BASE_URL = "https://api.hyperliquid.xyz/info"
    DEFAULT_TIMEOUT = 30.0

    # Max bars per candleSnapshot request (HL hard limit).
    MAX_BARS_PER_REQUEST = 5000

    # Maps Carrac TF strings -> HL interval strings + step in milliseconds.
    INTERVAL_MS = {
        '1m': 60_000, '3m': 180_000, '5m': 300_000,
        '15m': 900_000, '30m': 1_800_000,
        '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000,
        '8h': 28_800_000, '12h': 43_200_000, '1d': 86_400_000,
    }
    HL_INTERVALS = set(INTERVAL_MS.keys())

    # WARNING: Thin orderbooks on long-tail HL coins can produce wicks that
    #          don't reflect real consensus price. Only add a coin here if its
    #          24h volume is >$1M and you've eyeballed the chart. Do NOT
    #          auto-discover from /info meta.
    #
    # WARNING: Do NOT add a coin to this list if it has a real spot-price source
    #          elsewhere in Carrac (CoinPaprika top-150, Binance, etc.). The
    #          HL perp price can diverge significantly from spot, and users will
    #          see a chart at one price and a ticker at another. XMR was rejected
    #          for exactly this reason — HL XMR perp showed $325 while CoinPaprika
    #          spot Monero showed $118 (3x divergence). Only HL-native and HL-primary
    #          coins belong here.
    #
    # Audited against Hyperliquid metaAndAssetCtxs and Binance exchangeInfo:
    #   - All 14 coins return non-empty candles on HL for the last 7 days
    #   - All 14 coins are NOT currently traded on Binance USDT spot
    #   - Sorted by 24h volume (USD)
    #   - Includes 4 coins from Binance UNAVAILABLE list (FARTCOIN, GRIFFAIN, GRASS, BRETT)
    #   - 4 candidates removed because Binance has since listed them: XPL, PUMP, WLFI, ASTER
    #     (they remain in crypto_map and route to Binance via fallback chain — no special handling)
    #
    # Daily history depth on HL when this list was last reviewed:
    #   KPEPE 1070d, KBONK 872d, BRETT 667d, GRASS 528d, PURR 521d, CHILLGUY 500d,
    #   AERO 491d, HYPE 490d, SPX 485d, FARTCOIN 475d, GRIFFAIN 462d, VVV 436d,
    #   MON 183d, LIT 108d.
    # Indicators requiring >200 daily bars (EMA200, BB200) will be NaN for LIT and MON
    # — they degrade gracefully but expect partial signal until those coins age in.
    HL_COINS = {
        # Tier 1: $10M+ daily volume
        'HYPE',       # Hyperliquid native token, $320M
        'FARTCOIN',   # $227M  (was in BINANCE_UNAVAILABLE)
        'MON',        # $29M
        'LIT',        # $22M
        'KPEPE',      # $11M  (HL k-prefix = 1000x denominated PEPE; stored uppercase
                      # for symbol-lookup consistency, translated to 'kPEPE' at API call)
        # Tier 2: $1M-$10M daily volume
        'VVV',        # $4M
        'GRIFFAIN',   # $4M   (was in BINANCE_UNAVAILABLE)
        'KBONK',      # $2.8M (HL k-prefix = 1000x denominated BONK)
        # 'SPX' removed — conflicts with S&P 500 index symbol used by Stooq/Yahoo
        'CHILLGUY',   # $1.4M
        'AERO',       # $1.0M
        # Tier 3: <$1M but recovered from BINANCE_UNAVAILABLE or culturally relevant
        'GRASS',      # $0.5M (was in BINANCE_UNAVAILABLE)
        'BRETT',      # $0.2M (was in BINANCE_UNAVAILABLE)
        'PURR',       # $0.4M (HL native memecoin, cultural)
    }

    def __init__(self):
        # NOTE: this client is REST-only. Live candles are streamed by the
        #       separate long-running worker in data_sources/hyperliquid_ws.py
        #       (reconnect logic + healthcheck), started from worker.py.
        super().__init__()

    def _check_rate_limit(self, provider=None):
        from api_rate_limiter import can_call
        return can_call('hyperliquid')

    def _record_call(self, count: int = 1, provider=None):
        from api_rate_limiter import record_call
        record_call('hyperliquid', count=count)

    # ========================================
    # Internal HTTP
    # ========================================

    def _post(self, payload: dict, timeout: int = 30) -> Optional[list | dict]:
        """POST to /info with rate limiting. Returns parsed JSON or None on failure."""
        resp = self._request('POST', self.BASE_URL, json=payload, timeout=timeout)
        if resp is None or not resp.ok:
            if resp is not None:
                logger.debug(f"Hyperliquid {payload.get('type')} HTTP {resp.status_code}")
            return None
        try:
            return resp.json()
        except ValueError as e:
            logger.warning(f"Hyperliquid JSON parse failed: {e}")
            return None

    @staticmethod
    def _candles_to_df(candles: list) -> pd.DataFrame:
        """Convert HL candle list to a Carrac-shaped OHLCV DataFrame.

        HL candle keys: t (open ms), T (close ms), s, i, o, c, h, l, v, n
        Carrac expects: DatetimeIndex + columns [open, high, low, close, volume].
        """
        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(candles)
        df['timestamp'] = pd.to_datetime(df['t'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df = df[['o', 'h', 'l', 'c', 'v']].astype(float)
        df.columns = ['open', 'high', 'low', 'close', 'volume']
        df = df[df['close'] > 0]
        df = df[~df.index.duplicated(keep='last')].sort_index()
        return df

    def _fetch_paginated(self, coin: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """Fetch candles in pages of MAX_BARS_PER_REQUEST."""
        if interval not in self.INTERVAL_MS:
            logger.warning(f"Hyperliquid: unsupported interval {interval}")
            return pd.DataFrame()

        step_ms = self.INTERVAL_MS[interval]
        page_window_ms = step_ms * self.MAX_BARS_PER_REQUEST

        all_candles: list = []
        current = start_ms
        while current < end_ms:
            chunk_end = min(current + page_window_ms, end_ms)
            payload = {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval,
                        "startTime": current, "endTime": chunk_end},
            }
            data = self._post(payload)
            if not data:
                break
            all_candles.extend(data)
            # Advance past last candle's open time + 1 step
            if data:
                last_t = int(data[-1]['t'])
                next_start = last_t + step_ms
                if next_start <= current:
                    break  # safety: prevent infinite loop on stuck cursor
                current = next_start
            else:
                break
            time.sleep(0.05)  # gentle pacing

        return self._candles_to_df(all_candles)

    # ========================================
    # Universe / availability
    # ========================================


    def is_supported(self, symbol: str) -> bool:
        """Cheap allowlist check used by database.py before falling back to HL.

        Honors the HYPERLIQUID_DISABLED env var as an operational kill switch:
        if set to a truthy value, this returns False for every coin and the
        fallback chain skips HL entirely.
        """
        if os.environ.get('HYPERLIQUID_DISABLED', '').lower() in ('1', 'true', 'yes'):
            return False
        return symbol.upper() in self.HL_COINS

    # ========================================
    # OHLCV — public surface (mirrors BinanceClient)
    # ========================================

    def fetch_ohlcv(self, symbol: str, days: int = 365) -> pd.DataFrame:
        """Fetch daily OHLCV. Returns empty DataFrame on failure or if coin not in HL_COINS.

        # WARNING: Initial backfill across the full HL_COINS allowlist is ~19 coins
        #          × 4 timeframes = 76 calls one-time, well within the 1200 weight/min
        #          budget. But if smart-money snapshotting (positions for
        #          top-200 wallets), that's 200+ extra calls per cycle — re-evaluate
        #          batching at that point.
        """
        if not self.is_supported(symbol):
            return pd.DataFrame()

        coin = self._normalize_coin(symbol)
        end_ms = int(datetime.now().timestamp() * 1000)
        start_ms = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

        logger.info(f"Fetching {coin} from Hyperliquid ({days} days, daily)...")
        df = self._fetch_paginated(coin, '1d', start_ms, end_ms)
        if df.empty:
            logger.debug(f"Hyperliquid: no daily data for {coin}")
        else:
            logger.info(f"Fetched {len(df)} daily rows for {coin} from Hyperliquid")
        return df

    def fetch_klines(self, symbol: str, interval: str = '15m', bars: int = 2500) -> pd.DataFrame:
        """Fetch klines at any supported interval. Mirrors BinanceClient.fetch_klines.

        # WARNING: Hyperliquid volume is single-venue DEX volume, NOT comparable to
        #          Binance aggregate spot volume. Do not normalize indicators across
        #          venues without per-venue scaling. Volume-based indicators (CVD,
        #          MFI, OBV) may behave differently for HL coins than Binance coins.
        """
        if not self.is_supported(symbol):
            return pd.DataFrame()
        if interval not in self.INTERVAL_MS:
            logger.debug(f"Hyperliquid: interval {interval} not supported")
            return pd.DataFrame()

        coin = self._normalize_coin(symbol)
        step_ms = self.INTERVAL_MS[interval]
        end_ms = int(datetime.now().timestamp() * 1000)
        start_ms = end_ms - (step_ms * bars)

        df = self._fetch_paginated(coin, interval, start_ms, end_ms)
        if not df.empty:
            logger.info(f"Fetched {len(df)} {interval} rows for {coin} from Hyperliquid")
        return df

    def fetch_hourly_ohlcv(self, symbol: str, hours: int = 500) -> pd.DataFrame:
        """Fetch hourly OHLCV. Mirrors BinanceClient.fetch_hourly_ohlcv."""
        if not self.is_supported(symbol):
            return pd.DataFrame()

        coin = self._normalize_coin(symbol)
        end_ms = int(datetime.now().timestamp() * 1000)
        start_ms = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)

        logger.info(f"Fetching {coin} hourly from Hyperliquid ({hours} hours)...")
        df = self._fetch_paginated(coin, '1h', start_ms, end_ms)
        if not df.empty:
            logger.info(f"Downloaded {len(df)} hourly rows for {coin} from Hyperliquid")
        return df

    def fetch_klines_for_range(self, symbol: str, interval: str,
                                start_ms: int, end_ms: int) -> pd.DataFrame:
        """Fetch klines for a specific time range (used by Volume Profile)."""
        if not self.is_supported(symbol):
            return pd.DataFrame()
        if interval not in self.INTERVAL_MS:
            return pd.DataFrame()

        coin = self._normalize_coin(symbol)
        df = self._fetch_paginated(coin, interval, start_ms, end_ms)
        if not df.empty:
            logger.info(f"VP: Fetched {len(df)} {interval} klines for {coin} from Hyperliquid "
                        f"({start_ms}-{end_ms})")
        return df

    # Carrac stores symbols uppercase everywhere, but Hyperliquid uses
    # case-sensitive k-prefix names (kPEPE, kBONK, kSHIB) for 1000x denominated
    # coins. We store the uppercase form (KPEPE, KBONK) in HL_COINS / crypto_map
    # and translate at the API boundary here.
    K_PREFIX_TRANSLATION = {
        'KPEPE': 'kPEPE',
        'KBONK': 'kBONK',
        'KSHIB': 'kSHIB',
        'KFLOKI': 'kFLOKI',
        'KDOGS': 'kDOGS',
    }

    @classmethod
    def _normalize_coin(cls, symbol: str) -> str:
        """Translate a Carrac-uppercase symbol to the Hyperliquid coin name."""
        upper = symbol.upper()
        return cls.K_PREFIX_TRANSLATION.get(upper, upper)
