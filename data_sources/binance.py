"""
Binance API client for spot + futures data.

Handles: OHLCV klines, exchange info, 24hr tickers,
funding rates, open interest, CVD, long/short ratio.

Rate-limit accounting is routed via `provider` per-call:
  - 'binance' for spot/exchangeInfo
  - 'binance_futures' for futures endpoints
"""

import pandas as pd
import time
import threading
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

from data_sources.base import BaseDataSource

logger = logging.getLogger('carrac.data_sources.binance')


class BinanceClient(BaseDataSource):
    """Binance spot + futures API client with rate limiting."""

    NAME = 'binance'
    DEFAULT_TIMEOUT = 30.0

    # Symbol overrides for non-standard Binance tickers
    SYMBOL_MAP = {
        'IOTA': 'IOTA', 'MIOTA': 'IOTA',
    }

    # Symbols NOT available on Binance (served by Hyperliquid or CCXT instead)
    UNAVAILABLE = {
        'XMR', 'CC', 'FARTCOIN',
        'AKT', 'ARC', 'BRETT', 'BUTTHOLE', 'CHEX', 'COLLAT',
        'DEEP', 'DSYNC', 'GRASS', 'GRIFFAIN', 'KIP', 'MLC', 'PIPPIN',
        'PRIME', 'RNDR',
        # Binance delisting batch — frozen tickers with dead
        # order books (bidPrice=askPrice=0) still show up in the raw
        # /api/v3/ticker/24hr dump; route these through the fallback instead.
        'OCEAN', 'BEAM', 'MKR', 'NTRN', 'RDNT', 'TON', 'EOS',
    }

    # Dashboard symbol -> Binance perpetual contract symbol
    PERP_MAP = {
        'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'SOL': 'SOLUSDT',
        'BNB': 'BNBUSDT', 'XRP': 'XRPUSDT', 'ADA': 'ADAUSDT',
        'AVAX': 'AVAXUSDT', 'DOGE': 'DOGEUSDT', 'DOT': 'DOTUSDT',
        'LINK': 'LINKUSDT', 'POL': 'POLUSDT', 'UNI': 'UNIUSDT',
        'ATOM': 'ATOMUSDT', 'LTC': 'LTCUSDT', 'ETC': 'ETCUSDT',
        'XLM': 'XLMUSDT', 'ALGO': 'ALGOUSDT', 'VET': 'VETUSDT',
        'FIL': 'FILUSDT', 'AAVE': 'AAVEUSDT', 'MKR': 'MKRUSDT',
        'NEAR': 'NEARUSDT', 'APT': 'APTUSDT', 'ARB': 'ARBUSDT',
        'OP': 'OPUSDT', 'SUI': 'SUIUSDT', 'SEI': 'SEIUSDT',
        'INJ': 'INJUSDT', 'TIA': 'TIAUSDT', 'PEPE': 'PEPEUSDT',
        'WIF': 'WIFUSDT', 'BONK': 'BONKUSDT', 'RENDER': 'RENDERUSDT',
        'FET': 'FETUSDT', 'TAO': 'TAOUSDT', 'RUNE': 'RUNEUSDT',
        'TRX': 'TRXUSDT', 'TON': 'TONUSDT', 'ICP': 'ICPUSDT',
        'STX': 'STXUSDT', 'IMX': 'IMXUSDT', 'HBAR': 'HBARUSDT',
    }

    def __init__(self):
        super().__init__()
        # Spot symbol mapping (symbol -> USDT pair)
        self.spot_map = {
            # Major coins
            'BTC': 'BTCUSDT', 'ETH': 'ETHUSDT', 'SOL': 'SOLUSDT',
            'ADA': 'ADAUSDT', 'XRP': 'XRPUSDT', 'DOT': 'DOTUSDT',
            'LINK': 'LINKUSDT', 'AVAX': 'AVAXUSDT', 'POL': 'POLUSDT',
            'UNI': 'UNIUSDT', 'ATOM': 'ATOMUSDT', 'LTC': 'LTCUSDT',
            'DOGE': 'DOGEUSDT', 'XLM': 'XLMUSDT', 'ALGO': 'ALGOUSDT',
            'VET': 'VETUSDT', 'FIL': 'FILUSDT', 'AAVE': 'AAVEUSDT',
            'EOS': 'EOSUSDT', 'XTZ': 'XTZUSDT', 'BNB': 'BNBUSDT',
            'TRX': 'TRXUSDT', 'SHIB': 'SHIBUSDT',
            'NEAR': 'NEARUSDT', 'SUI': 'SUIUSDT', 'APT': 'APTUSDT',
            # Layer 2s
            'ARB': 'ARBUSDT', 'OP': 'OPUSDT', 'IMX': 'IMXUSDT',
            'ZK': 'ZKUSDT', 'METIS': 'METISUSDT', 'LRC': 'LRCUSDT',
            'SKL': 'SKLUSDT',
            # AI & Computing
            'FET': 'FETUSDT', 'RENDER': 'RENDERUSDT', 'TAO': 'TAOUSDT',
            'OCEAN': 'OCEANUSDT', 'AR': 'ARUSDT',
            'IO': 'IOUSDT', 'AI': 'AIUSDT', 'CGPT': 'CGPTUSDT',
            # DeFi
            'CRV': 'CRVUSDT', 'SNX': 'SNXUSDT', 'COMP': 'COMPUSDT',
            'LDO': 'LDOUSDT', 'MKR': 'MKRUSDT', 'RUNE': 'RUNEUSDT',
            'JUP': 'JUPUSDT', 'ENA': 'ENAUSDT', 'PENDLE': 'PENDLEUSDT',
            'CVX': 'CVXUSDT', 'RPL': 'RPLUSDT', 'COW': 'COWUSDT',
            'MORPHO': 'MORPHOUSDT', 'DODO': 'DODOUSDT', 'USUAL': 'USUALUSDT',
            'FRAX': 'FRAXUSDT',
            # Gaming & Metaverse
            'GALA': 'GALAUSDT', 'SAND': 'SANDUSDT', 'MANA': 'MANAUSDT',
            'AXS': 'AXSUSDT', 'ENJ': 'ENJUSDT', 'BEAM': 'BEAMUSDT',
            'YGG': 'YGGUSDT', 'SUPER': 'SUPERUSDT', 'GMT': 'GMTUSDT',
            'ALICE': 'ALICEUSDT', 'RONIN': 'RONINUSDT',
            # Infrastructure
            'ICP': 'ICPUSDT', 'INJ': 'INJUSDT', 'HBAR': 'HBARUSDT',
            'FLOW': 'FLOWUSDT', 'SEI': 'SEIUSDT',
            'TIA': 'TIAUSDT', 'STX': 'STXUSDT', 'THETA': 'THETAUSDT',
            'QNT': 'QNTUSDT', 'EGLD': 'EGLDUSDT',
            'IOTA': 'IOTAUSDT', 'KSM': 'KSMUSDT', 'ZIL': 'ZILUSDT',
            'MINA': 'MINAUSDT', 'CKB': 'CKBUSDT', 'ONE': 'ONEUSDT',
            'CELO': 'CELOUSDT', 'ZEN': 'ZENUSDT', 'ANKR': 'ANKRUSDT',
            'ICX': 'ICXUSDT', 'ASTR': 'ASTRUSDT', 'AXL': 'AXLUSDT',
            'SC': 'SCUSDT', 'STORJ': 'STORJUSDT', 'GLM': 'GLMUSDT',
            'FLUX': 'FLUXUSDT', 'RVN': 'RVNUSDT',
            # Memecoins
            'PEPE': 'PEPEUSDT', 'BONK': 'BONKUSDT', 'WIF': 'WIFUSDT',
            'FLOKI': 'FLOKIUSDT', 'TURBO': 'TURBOUSDT',
            'BOME': 'BOMEUSDT', 'PEOPLE': 'PEOPLEUSDT', 'HMSTR': 'HMSTRUSDT',
            'LUNC': 'LUNCUSDT',
            # Other
            'TON': 'TONUSDT', 'ONDO': 'ONDOUSDT', 'PYTH': 'PYTHUSDT',
            'JTO': 'JTOUSDT', 'W': 'WUSDT',
            '1INCH': '1INCHUSDT', 'BIO': 'BIOUSDT', 'CETUS': 'CETUSUSDT',
            'COTI': 'COTIUSDT', 'GMX': 'GMXUSDT', 'MANTA': 'MANTAUSDT',
            'PENGU': 'PENGUUSDT', 'S': 'SUSDT', 'STRK': 'STRKUSDT',
            'BAND': 'BANDUSDT', 'NMR': 'NMRUSDT', 'BAT': 'BATUSDT',
            'DASH': 'DASHUSDT', 'DCR': 'DCRUSDT', 'PAXG': 'PAXGUSDT',
            'NEXO': 'NEXOUSDT', 'FTT': 'FTTUSDT',
            # Solana ecosystem
            'ORCA': 'ORCAUSDT', 'KMNO': 'KMNOUSDT', 'TNSR': 'TNSRUSDT',
            'FIDA': 'FIDAUSDT',
            # Major additions
            'BCH': 'BCHUSDT', 'ETC': 'ETCUSDT', 'NEO': 'NEOUSDT', 'ZEC': 'ZECUSDT',
            # DeFi (extended)
            'DYDX': 'DYDXUSDT', 'ENS': 'ENSUSDT', 'GRT': 'GRTUSDT',
            'SUSHI': 'SUSHIUSDT', 'CAKE': 'CAKEUSDT', 'LQTY': 'LQTYUSDT',
            'LISTA': 'LISTAUSDT', 'RDNT': 'RDNTUSDT', 'RSR': 'RSRUSDT',
            # Gaming (extended)
            'APE': 'APEUSDT', 'PIXEL': 'PIXELUSDT', 'PORTAL': 'PORTALUSDT',
            'BIGTIME': 'BIGTIMEUSDT', 'MAGIC': 'MAGICUSDT', 'ILV': 'ILVUSDT',
            # AI (extended)
            'AIXBT': 'AIXBTUSDT', 'WLD': 'WLDUSDT', 'ARKM': 'ARKMUSDT',
            # Infrastructure (extended)
            'JASMY': 'JASMYUSDT', 'CHZ': 'CHZUSDT', 'ROSE': 'ROSEUSDT',
            'CFX': 'CFXUSDT', 'KAVA': 'KAVAUSDT', 'OSMO': 'OSMOUSDT',
            'LUNA': 'LUNAUSDT', 'ZRO': 'ZROUSDT', 'SAGA': 'SAGAUSDT',
            'DYM': 'DYMUSDT', 'NTRN': 'NTRNUSDT', 'RAY': 'RAYUSDT',
            'ACH': 'ACHUSDT', 'BLUR': 'BLURUSDT', 'SSV': 'SSVUSDT',
            'ETHFI': 'ETHFIUSDT', 'MOVE': 'MOVEUSDT',
            # New L1/L2
            'BERA': 'BERAUSDT', 'KAITO': 'KAITOUSDT',
            'LAYER': 'LAYERUSDT', 'HYPER': 'HYPERUSDT', 'INIT': 'INITUSDT',
            'PLUME': 'PLUMEUSDT', 'FORM': 'FORMUSDT',
            # Memecoins (extended)
            'ORDI': 'ORDIUSDT', 'NOT': 'NOTUSDT', 'DOGS': 'DOGSUSDT',
            'NEIRO': 'NEIROUSDT', 'PNUT': 'PNUTUSDT', 'TRUMP': 'TRUMPUSDT',
            'VIRTUAL': 'VIRTUALUSDT', 'ANIME': 'ANIMEUSDT',
            # Narrative
            'EIGEN': 'EIGENUSDT',
            'SHELL': 'SHELLUSDT', 'KERNEL': 'KERNELUSDT', 'SIGN': 'SIGNUSDT',
            'HUMA': 'HUMAUSDT', 'WAL': 'WALUSDT',
        }
        self.reverse_map = {v: k for k, v in self.spot_map.items()}

        # Exchange info cache (all USDT trading pairs)
        self._exchange_info: set = set()
        self._exchange_info_time: Optional[datetime] = None
        self._exchange_info_lock = threading.Lock()

        # Funding rate cache
        self._funding_cache: Dict[str, float] = {}
        self._funding_cache_time: Optional[datetime] = None
        self.FUNDING_CACHE_TTL = 3600  # 1 hour

        # Perp basis cache (markPrice vs indexPrice, %) — populated in the
        # same premiumIndex fetch as funding rates.
        self._basis_cache: Dict[str, Optional[float]] = {}

        # Open interest cache
        self._oi_cache: Dict[str, Dict] = {}
        self._oi_cache_time: Optional[datetime] = None
        self.OI_CACHE_TTL = 300  # 5 minutes

        # Long/short ratio cache
        self._ls_cache: Optional[Dict[str, Dict]] = None
        self._ls_cache_time: Optional[datetime] = None
        self.LS_CACHE_TTL = 300  # 5 minutes

    def _check_rate_limit(self, provider=None):
        from api_rate_limiter import can_call
        return can_call(provider or 'binance')

    def _record_call(self, count: int = 1, provider=None):
        from api_rate_limiter import record_call
        record_call(provider or 'binance', count=count)

    # ========================================
    # Exchange Info
    # ========================================

    def refresh_exchange_info(self):
        """Fetch and cache all USDT trading pairs from Binance exchangeInfo."""
        resp = self._request(
            'GET', 'https://data-api.binance.vision/api/v3/exchangeInfo',
            params={'permissions': 'SPOT'},
        )
        if resp is None or not resp.ok:
            return
        try:
            data = resp.json()
            pairs = set()
            for s in data.get('symbols', []):
                if s.get('quoteAsset') == 'USDT' and s.get('status') == 'TRADING':
                    pairs.add(s['baseAsset'].upper())
            with self._exchange_info_lock:
                self._exchange_info = pairs
                self._exchange_info_time = datetime.now()
            logger.info(f"Binance exchangeInfo cached: {len(pairs)} USDT pairs")
        except Exception as e:
            logger.warning(f"Failed to parse Binance exchangeInfo: {e}")

    def get_exchange_info(self) -> set:
        """Get cached Binance exchangeInfo, refreshing if stale (>24h)."""
        with self._exchange_info_lock:
            if (self._exchange_info_time is None or
                    (datetime.now() - self._exchange_info_time).total_seconds() > 86400):
                pass
            else:
                return self._exchange_info
        self.refresh_exchange_info()
        return self._exchange_info

    # ========================================
    # Spot OHLCV
    # ========================================

    def _resolve_pair(self, symbol: str) -> str:
        """Resolve symbol to Binance USDT pair."""
        symbol_upper = symbol.upper()
        binance_symbol = self.SYMBOL_MAP.get(symbol_upper, symbol_upper)
        return f"{binance_symbol}USDT"

    def _klines_paginated(self, pair: str, interval: str, start_time: int,
                          end_time: int, binance_symbol: str,
                          step_ms: int) -> list:
        """Common pagination loop for klines. Tries USDT pair first; on 400,
        retries with USD pair. Returns all candles or empty list on failure.
        """
        url = "https://data-api.binance.vision/api/v3/klines"
        all_candles: list = []
        current_start = start_time
        current_pair = pair

        while current_start < end_time:
            params = {
                'symbol': current_pair, 'interval': interval,
                'startTime': current_start, 'endTime': end_time, 'limit': 1000,
            }
            resp = self._request('GET', url, params=params)
            if resp is None:
                return []

            if resp.status_code == 400 and current_pair.endswith('USDT'):
                # Try USD pair
                current_pair = f"{binance_symbol}USD"
                params['symbol'] = current_pair
                resp = self._request('GET', url, params=params)
                if resp is None:
                    return []

            if not resp.ok:
                logger.warning(
                    "Binance klines %s %s: HTTP %s%s", current_pair, interval,
                    resp.status_code,
                    ' (Binance refuses this region)' if resp.status_code == 451 else '')
                return []

            try:
                candles = resp.json()
            except Exception:
                return []

            if not candles:
                break

            all_candles.extend(candles)
            current_start = candles[-1][0] + step_ms
            time.sleep(0.05)

        return all_candles

    def fetch_ohlcv(self, symbol: str, days: int = 365) -> pd.DataFrame:
        """Fetch daily OHLCV data from Binance Klines API."""
        symbol_upper = symbol.upper()
        if symbol_upper in self.UNAVAILABLE:
            logger.debug(f"{symbol} not available on Binance, using fallback")
            return pd.DataFrame()

        binance_symbol = self.SYMBOL_MAP.get(symbol_upper, symbol_upper)
        pair = f"{binance_symbol}USDT"
        logger.info(f"Fetching {symbol} from Binance ({days} days)...")

        end_time = int(datetime.now().timestamp() * 1000)
        start_time = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

        all_candles = self._klines_paginated(pair, '1d', start_time, end_time,
                                              binance_symbol, step_ms=86400000)
        if not all_candles:
            logger.debug(f"No Binance data for {symbol}")
            return pd.DataFrame()

        return self._parse_klines(all_candles, symbol)

    def fetch_pair_ohlcv(self, pair: str, days: int = 365) -> pd.DataFrame:
        """Fetch daily OHLCV for an explicit Binance pair (e.g. 'ETHBTC').

        fetch_ohlcv() always quotes in USDT; cross pairs like ETHBTC/XRPBTC are
        real markets that must be requested by their own symbol.
        """
        pair_upper = pair.upper()
        logger.info(f"Fetching {pair_upper} pair from Binance ({days} days)...")

        end_time = int(datetime.now().timestamp() * 1000)
        start_time = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

        all_candles = self._klines_paginated(pair_upper, '1d', start_time, end_time,
                                             pair_upper, step_ms=86400000)
        if not all_candles:
            logger.debug(f"No Binance data for pair {pair_upper}")
            return pd.DataFrame()

        return self._parse_klines(all_candles, pair_upper)

    def fetch_klines(self, symbol: str, interval: str = '15m', bars: int = 2500) -> pd.DataFrame:
        """Fetch kline data at any supported interval."""
        interval_ms = {
            '1m': 60000, '3m': 180000, '5m': 300000, '15m': 900000, '30m': 1800000,
            '1h': 3600000, '2h': 7200000, '4h': 14400000,
        }
        step_ms = interval_ms.get(interval, 3600000)
        total_ms = step_ms * bars
        return self._fetch_interval(symbol, interval, total_ms, step_ms)

    def _fetch_interval(self, symbol: str, interval: str, range_ms: int,
                         step_ms: int) -> pd.DataFrame:
        """Fetch klines for a given interval and time range."""
        symbol_upper = symbol.upper()
        if symbol_upper in self.UNAVAILABLE:
            return pd.DataFrame()

        binance_symbol = self.SYMBOL_MAP.get(symbol_upper, symbol_upper)
        pair = f"{binance_symbol}USDT"

        end_time = int(datetime.now().timestamp() * 1000)
        start_time = end_time - range_ms

        all_candles = self._klines_paginated(pair, interval, start_time, end_time,
                                              binance_symbol, step_ms=1)
        if not all_candles:
            return pd.DataFrame()

        df = pd.DataFrame(all_candles, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df = df[['open', 'high', 'low', 'close', 'volume', 'taker_buy_base']].astype(float)
        df = df[df['close'] > 0]
        df = df[~df.index.duplicated(keep='last')].sort_index()

        logger.info(f"Fetched {len(df)} {interval} rows for {symbol} from Binance")
        return df

    def fetch_klines_for_range(self, symbol: str, interval: str,
                               start_ms: int, end_ms: int) -> pd.DataFrame:
        """Fetch klines for a specific time range (used by Volume Profile)."""
        symbol_upper = symbol.upper()
        if symbol_upper in self.UNAVAILABLE:
            return pd.DataFrame()

        binance_symbol = self.SYMBOL_MAP.get(symbol_upper, symbol_upper)
        pair = f"{binance_symbol}USDT"

        all_candles = self._klines_paginated(pair, interval, start_ms, end_ms,
                                              binance_symbol, step_ms=1)
        if not all_candles:
            return pd.DataFrame()

        df = pd.DataFrame(all_candles, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        df = df[['open', 'high', 'low', 'close', 'volume', 'taker_buy_base']].astype(float)
        df = df[df['close'] > 0]

        logger.info(f"VP: Fetched {len(df)} {interval} klines for {symbol} ({start_ms}-{end_ms})")
        return df

    def fetch_hourly_ohlcv(self, symbol: str, hours: int = 500) -> pd.DataFrame:
        """Fetch hourly OHLCV data (500 hours = ~20 days)."""
        symbol_upper = symbol.upper()
        if symbol_upper in self.UNAVAILABLE:
            logger.debug(f"{symbol} not available on Binance hourly")
            return pd.DataFrame()

        binance_symbol = self.SYMBOL_MAP.get(symbol_upper, symbol_upper)
        pair = f"{binance_symbol}USDT"
        logger.info(f"Fetching {symbol} hourly from Binance ({hours} hours)...")

        end_time = int(datetime.now().timestamp() * 1000)
        start_time = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)

        all_candles = self._klines_paginated(pair, '1h', start_time, end_time,
                                              binance_symbol, step_ms=3600000)
        if not all_candles:
            logger.debug(f"No Binance hourly data for {symbol}")
            return pd.DataFrame()

        df = pd.DataFrame(all_candles, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df = df[['open', 'high', 'low', 'close', 'volume', 'taker_buy_base']].astype(float)
        df = df[df['close'] > 0]
        df = df[~df.index.duplicated(keep='last')].sort_index()

        logger.info(f"Downloaded {len(df)} hourly rows for {symbol} from Binance")
        return df

    def fetch_24hr_tickers(self) -> Dict[str, Dict[str, Any]]:
        """Fetch 24hr ticker data (direct fallback, normally uses cache_manager)."""
        resp = self._request(
            'GET', "https://data-api.binance.vision/api/v3/ticker/24hr", timeout=10,
        )
        if resp is None or not resp.ok:
            return {}

        try:
            all_tickers = resp.json()
        except Exception as e:
            logger.warning(f"Fallback Binance 24hr parse failed: {e}")
            return {}

        result = {}
        for ticker in all_tickers:
            binance_symbol = ticker.get('symbol', '')
            if binance_symbol in self.reverse_map:
                our_symbol = self.reverse_map[binance_symbol]
                result[our_symbol] = {
                    'price': float(ticker.get('lastPrice', 0)),
                    'open': float(ticker.get('openPrice', 0)),
                    'change_pct': float(ticker.get('priceChangePercent', 0)),
                    'high_24h': float(ticker.get('highPrice', 0)),
                    'low_24h': float(ticker.get('lowPrice', 0)),
                    'volume_24h': float(ticker.get('volume', 0)),
                    'source': 'binance',
                }
        return result

    # ========================================
    # Futures: Funding Rates
    # ========================================

    def fetch_funding_rates(self) -> Dict[str, float]:
        """Fetch perpetual futures funding rates."""
        if (self._funding_cache_time and
                (datetime.now() - self._funding_cache_time).total_seconds() < self.FUNDING_CACHE_TTL and
                self._funding_cache):
            return self._funding_cache

        resp = self._request(
            'GET', "https://fapi.binance.com/fapi/v1/premiumIndex",
            timeout=10, provider='binance',
        )
        if resp is None or not resp.ok:
            return self._funding_cache if self._funding_cache else {}

        try:
            data = resp.json()
        except Exception as e:
            logger.warning(f"Binance funding rates parse failed: {e}")
            return self._funding_cache if self._funding_cache else {}

        reverse_perp = {v: k for k, v in self.PERP_MAP.items()}
        rates = {}
        basis = {}
        for item in data:
            binance_symbol = item.get('symbol', '')
            if binance_symbol in reverse_perp:
                our_symbol = reverse_perp[binance_symbol]
                rates[our_symbol] = float(item.get('lastFundingRate', 0))
                basis[our_symbol] = self._compute_basis_pct(
                    item.get('markPrice'), item.get('indexPrice')
                )

        self._funding_cache = rates
        self._basis_cache = basis
        self._funding_cache_time = datetime.now()

        logger.info(f"Fetched funding rates for {len(rates)} symbols from Binance")
        return rates

    @staticmethod
    def _compute_basis_pct(mark_price: Any, index_price: Any) -> Optional[float]:
        """Perp basis (markPrice vs indexPrice) as a percentage.

        basis_pct = (markPrice - indexPrice) / indexPrice * 100

        Returns None if either input is missing/non-numeric or indexPrice is
        zero — a data gap must surface as None, never as a fabricated 0.0.
        """
        if mark_price is None or index_price is None:
            return None
        try:
            mark_f = float(mark_price)
            index_f = float(index_price)
        except (TypeError, ValueError):
            return None
        if index_f == 0:
            return None
        return (mark_f - index_f) / index_f * 100

    def fetch_perp_basis(self) -> Dict[str, Optional[float]]:
        """Fetch perpetual futures basis (markPrice vs indexPrice, %).

        Shares the premiumIndex fetch (and its TTL cache) with
        fetch_funding_rates() — calling this triggers/refreshes that same
        request rather than issuing a second one.
        """
        self.fetch_funding_rates()
        return self._basis_cache

    # ========================================
    # Futures: Open Interest
    # ========================================

    def fetch_open_interest(self, symbol: str) -> Dict:
        """Fetch open interest with 24h change."""
        symbol_upper = symbol.upper()
        cache_key = symbol_upper
        empty = {'value': None, 'value_usd': None, 'change_pct': None}
        if (self._oi_cache_time and
                (datetime.now() - self._oi_cache_time).total_seconds() < self.OI_CACHE_TTL and
                cache_key in self._oi_cache):
            return self._oi_cache[cache_key]

        binance_symbol = self.PERP_MAP.get(symbol_upper, f"{symbol_upper}USDT")
        url = f"https://fapi.binance.com/futures/data/openInterestHist?symbol={binance_symbol}&period=1d&limit=2"

        resp = self._request('GET', url, timeout=10, provider='binance_futures')
        if resp is None or not resp.ok:
            return self._oi_cache.get(cache_key, empty)

        try:
            data = resp.json()
        except Exception:
            return self._oi_cache.get(cache_key, empty)

        if not data or len(data) < 1:
            return empty

        latest = data[-1]
        oi_value = float(latest.get('sumOpenInterest', 0))
        oi_usd = float(latest.get('sumOpenInterestValue', 0))

        change_pct = None
        if len(data) >= 2:
            prev_usd = float(data[-2].get('sumOpenInterestValue', 0))
            if prev_usd > 0:
                change_pct = ((oi_usd - prev_usd) / prev_usd) * 100

        result = {
            'value': oi_value,
            'value_usd': oi_usd,
            'change_pct': round(change_pct, 2) if change_pct is not None else None,
        }

        self._oi_cache[cache_key] = result
        self._oi_cache_time = datetime.now()

        change_str = f"{change_pct:+.2f}%" if change_pct is not None else "N/A"
        logger.info(f"Fetched OI for {symbol_upper}: {oi_value:.2f} BTC (${oi_usd/1e9:.2f}B, {change_str})")
        return result

    def fetch_open_interest_hist(self, symbol: str, period: str = '1h', limit: int = 500) -> List[Dict]:
        """Fetch historical OI time series."""
        symbol_upper = symbol.upper()
        binance_symbol = self.PERP_MAP.get(symbol_upper, f"{symbol_upper}USDT")

        resp = self._request(
            'GET', "https://fapi.binance.com/futures/data/openInterestHist",
            params={'symbol': binance_symbol, 'period': period, 'limit': limit},
            timeout=15, provider='binance_futures',
        )
        if resp is None or not resp.ok:
            return []

        try:
            data = resp.json()
        except Exception as e:
            logger.warning(f"OI hist parse failed for {symbol_upper}: {e}")
            return []

        if not data:
            return []

        result = []
        for item in data:
            ts = int(item.get('timestamp', 0)) // 1000
            oi = float(item.get('sumOpenInterest', 0))
            oi_usd = float(item.get('sumOpenInterestValue', 0))
            result.append({'time': ts, 'oi': oi, 'oi_usd': oi_usd})

        logger.info(f"Fetched {len(result)} OI hist points for {symbol_upper} ({period})")
        return result

    def fetch_global_long_short_ratio_hist(self, symbol: str, period: str = '1h',
                                            limit: int = 500) -> List[Dict]:
        """Fetch historical global long/short ACCOUNT ratio (fraction of
        accounts long vs short, not position-weighted).

        Used by the liquidation-heatmap model (carrac/liq_heatmap.py) to
        split newly-inferred OI-delta positions long/short at matching
        per-bucket granularity. This is the same Binance endpoint
        quant/collectors/binance_daily.py already uses for the daily
        quant.derivatives.long_short_ratio column, just parameterized at a
        finer `period` -- the stored daily average is too coarse to align
        with hourly OI buckets.
        """
        symbol_upper = symbol.upper()
        binance_symbol = self.PERP_MAP.get(symbol_upper, f"{symbol_upper}USDT")

        resp = self._request(
            'GET', "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={'symbol': binance_symbol, 'period': period, 'limit': limit},
            timeout=15, provider='binance_futures',
        )
        if resp is None or not resp.ok:
            return []

        try:
            data = resp.json()
        except Exception as e:
            logger.warning(f"Global L/S ratio hist parse failed for {symbol_upper}: {e}")
            return []

        if not data:
            return []

        result = []
        for item in data:
            ts = int(item.get('timestamp', 0)) // 1000
            long_account = float(item.get('longAccount', 0.5))
            result.append({
                'time': ts,
                'long_account': long_account,
                'long_short_ratio': float(item.get('longShortRatio', 1.0)),
            })

        logger.info(f"Fetched {len(result)} global L/S ratio hist points for {symbol_upper} ({period})")
        return result

    # ========================================
    # Futures: CVD (Cumulative Volume Delta)
    # ========================================

    def fetch_cvd_futures(self, symbol: str, period: str = '1h', limit: int = 500,
                          margin_type: str = 'usdt') -> List[Dict]:
        """Fetch futures taker buy/sell volume for CVD calculation."""
        symbol_upper = symbol.upper()
        binance_symbol = self.PERP_MAP.get(symbol_upper, f"{symbol_upper}USDT")

        if margin_type == 'coin':
            pair = symbol_upper + 'USD'
            url = "https://dapi.binance.com/futures/data/takerBuySellVol"
            params = {'pair': pair, 'contractType': 'PERPETUAL', 'period': period, 'limit': limit}
        else:
            # Use takerBuySellVol (base-volume amounts), not takerlongshortRatio (account ratios)
            url = "https://fapi.binance.com/futures/data/takerBuySellVol"
            params = {'symbol': binance_symbol, 'period': period, 'limit': limit}

        resp = self._request('GET', url, params=params, timeout=15,
                              provider='binance_futures')
        if resp is None or not resp.ok:
            return []

        try:
            data = resp.json()
        except Exception as e:
            label = 'COIN-M' if margin_type == 'coin' else 'USDT-M'
            logger.warning(f"CVD {label} parse failed for {symbol_upper}: {e}")
            return []

        if not data:
            return []

        result = []
        cumulative = 0.0
        for item in data:
            ts = int(item.get('timestamp', 0)) // 1000
            if margin_type == 'coin':
                buy_vol = float(item.get('takerBuyVol', 0))
                sell_vol = float(item.get('takerSellVol', 0))
            else:
                buy_vol = float(item.get('buyVol', 0))
                sell_vol = float(item.get('sellVol', 0))

            delta = buy_vol - sell_vol
            cumulative += delta
            result.append({'time': ts, 'cvd': round(cumulative, 4), 'delta': round(delta, 4)})

        label = 'COIN-M' if margin_type == 'coin' else 'USDT-M'
        logger.info(f"Fetched {len(result)} CVD {label} points for {symbol_upper} ({period})")
        return result

    # ========================================
    # Futures: Long/Short Ratio
    # ========================================

    def fetch_long_short_ratio(self, symbols: List[str] = None) -> Dict[str, Dict]:
        """Fetch top trader long/short ratio."""
        if (self._ls_cache_time and
                (datetime.now() - self._ls_cache_time).total_seconds() < self.LS_CACHE_TTL and
                self._ls_cache):
            return self._ls_cache

        if symbols is None:
            symbols = ['BTC', 'ETH', 'SOL']

        result = {}
        for symbol in symbols:
            binance_symbol = f"{symbol}USDT"
            resp = self._request(
                'GET', "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
                params={'symbol': binance_symbol, 'period': '1h', 'limit': 1},
                timeout=10, provider='binance_futures',
            )
            if resp is not None and resp.ok:
                try:
                    data = resp.json()
                    if data and len(data) > 0:
                        latest = data[0]
                        result[symbol] = {
                            'longShortRatio': float(latest.get('longShortRatio', 1.0)),
                            'longAccount': float(latest.get('longAccount', 0.5)),
                            'shortAccount': float(latest.get('shortAccount', 0.5)),
                            'timestamp': latest.get('timestamp', 0),
                        }
                except Exception as e:
                    logger.warning(f"L/S parse failed for {symbol}: {e}")
            time.sleep(0.1)

        self._ls_cache = result
        self._ls_cache_time = datetime.now()

        logger.info(f"Fetched L/S ratios for {len(result)} symbols from Binance Futures")
        return result

    # ========================================
    # Helpers
    # ========================================

    def _parse_klines(self, candles: list, symbol: str) -> pd.DataFrame:
        """Parse Binance klines format into DataFrame."""
        df = pd.DataFrame(candles, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_volume', 'trades', 'taker_buy_base',
            'taker_buy_quote', 'ignore'
        ])
        df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms')
        df.set_index('timestamp', inplace=True)

        cols = ['open', 'high', 'low', 'close', 'volume']
        if 'taker_buy_base' in df.columns:
            cols.append('taker_buy_base')
        df = df[cols].astype(float)

        df = df[df['close'] > 0]
        df = df[~df.index.duplicated(keep='last')].sort_index()

        logger.info(f"Downloaded {len(df)} rows for {symbol} from Binance")
        return df
