"""
Global Cache Manager for Multi-User API Rate Limit Protection

This module provides server-wide caching that ensures:
1. All users share the same cached data (no redundant API calls)
2. Background refresh keeps data fresh without user-triggered fetches
3. Graceful fallback when APIs are unavailable
4. Thread-safe access for concurrent requests

Architecture:
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│  User A     │────▶│                  │     │             │
│  User B     │────▶│  Global Cache    │────▶│  APIs       │
│  User C     │────▶│  (This Module)   │     │             │
└─────────────┘     └──────────────────┘     └─────────────┘
                           │
                    Background refresh every 30-60s
"""

import threading
import time
import logging
import requests
from datetime import datetime
from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger('carrac.cache_manager')


@dataclass
class CacheEntry:
    """Single cache entry with metadata."""
    data: Any
    timestamp: datetime
    ttl_seconds: int
    source: str
    error_count: int = 0
    last_error: Optional[str] = None

    def is_expired(self) -> bool:
        return (datetime.now() - self.timestamp).total_seconds() > self.ttl_seconds

    def is_stale(self, stale_factor: float = 2.0) -> bool:
        """Check if data is stale (past TTL but within grace period)."""
        age = (datetime.now() - self.timestamp).total_seconds()
        return age > self.ttl_seconds and age < (self.ttl_seconds * stale_factor)


class GlobalCacheManager:
    """
    Thread-safe global cache manager for API responses.

    Features:
    - Server-wide singleton pattern
    - Background refresh thread
    - Circuit breaker for failing APIs
    - Stale-while-revalidate pattern
    - Multi-source fallback
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """Singleton pattern - one cache for entire server."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._cache: Dict[str, CacheEntry] = {}
        self._cache_lock = threading.RLock()
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop_refresh = threading.Event()
        self._refresh_callbacks: Dict[str, Callable] = {}
        self._refreshing: set = set()  # Keys currently being refreshed (dedup guard)

        # Circuit breaker state
        self._circuit_breaker: Dict[str, int] = {}
        self.CIRCUIT_BREAKER_THRESHOLD = 3
        self.CIRCUIT_BREAKER_RESET = 300  # seconds
        self._circuit_breaker_reset_time: Dict[str, datetime] = {}

        # Default TTLs (in seconds)
        # Client has Binance WS for live crypto prices — server caches are fallbacks
        self.DEFAULT_TTLS = {
            # Crypto prices (client WS is real-time, server is fallback)
            'realtime_prices': 120,     # Crypto prices - 2 min
            'binance_tickers': 120,     # Binance 24hr tickers - 2 min
            'combined_crypto_prices': 300,  # Combined (Binance+CoinPaprika) - 5 min
            'coinpaprika_tickers': 300,  # CoinPaprika prices - 5 min
            'coinpaprika_top_coins': 3600,  # CoinPaprika top coins - 1 hour
            # Market data
            'fear_greed': 300,          # Fear & Greed - refresh every 5min
            'funding_rates': 300,       # Funding rates - refresh every 5min
            'long_short_ratio': 300,    # L/S ratio - refresh every 5min
            'alt_season_index': 3600,   # Alt Season Index - refresh hourly
            'mvrv_zscore': 3600,        # MVRV Z-Score - refresh hourly
            'market_sentiment': 300,    # Aggregated sentiment - refresh every 5min
            'top_coins': 3600,          # Top coins list - refresh hourly
            'market_caps': 3600,        # Market cap data - refresh hourly
            # OHLCV data
            'ltf_data': 900,            # LTF (hourly) data - refresh every 15min
            'stooq_ohlcv': 3600,        # Stooq OHLCV - refresh hourly (daily data)
            'yahoo_ohlcv': 3600,        # Yahoo OHLCV - refresh hourly (daily data)
            'onchain_data': 1800,       # On-chain data - refresh every 30min
            'btc_mempool': 900,         # BTC mempool - refresh every 15min
        }

        self._initialized = True
        logger.info("Global Cache Manager initialized")

    def get(self, key: str, fetch_func: Optional[Callable] = None,
            ttl: Optional[int] = None, allow_stale: bool = True) -> Optional[Any]:
        """
        Get cached data, optionally refreshing if expired.

        Args:
            key: Cache key
            fetch_func: Function to call if cache miss/expired
            ttl: TTL in seconds (uses default if not specified)
            allow_stale: Return stale data while refreshing in background

        Returns:
            Cached data or None if unavailable
        """
        with self._cache_lock:
            entry = self._cache.get(key)

            # Cache hit and not expired
            if entry and not entry.is_expired():
                return entry.data

            # Cache hit but expired - return stale if allowed
            if entry and allow_stale and entry.is_stale():
                # Trigger background refresh
                if fetch_func:
                    self._schedule_refresh(key, fetch_func, ttl)
                return entry.data

            # Cache miss or fully expired - fetch now
            if fetch_func and not self._is_circuit_open(key):
                try:
                    data = fetch_func()
                    if data is not None:
                        self.set(key, data, ttl or self.DEFAULT_TTLS.get(key, 60),
                                source='direct_fetch')
                        self._reset_circuit_breaker(key)
                        return data
                except Exception as e:
                    logger.warning(f"Cache fetch failed for {key}: {e}")
                    self._record_failure(key, str(e))
                    # Return stale data if available
                    if entry:
                        return entry.data

            # Return whatever we have (could be old data or None)
            return entry.data if entry else None

    def set(self, key: str, data: Any, ttl: Optional[int] = None,
            source: str = 'manual') -> None:
        """Set cache entry."""
        with self._cache_lock:
            self._cache[key] = CacheEntry(
                data=data,
                timestamp=datetime.now(),
                ttl_seconds=ttl or self.DEFAULT_TTLS.get(key, 60),
                source=source
            )

    def get_if_fresh(self, key: str, max_age: int) -> Optional[Any]:
        """Return the entry only if younger than ``max_age`` seconds, else None.

        ``get()`` is deliberately stale-tolerant: every one of its branches
        ends at ``return entry.data if entry else None``, so a caller that
        passes no ``fetch_func`` gets its entry back forever, at any age. That
        is the right behaviour for a serving path with an upstream that may be
        down — it is the wrong behaviour for a memo that sits in front of a
        shared, TTL'd on-disk tier, because the lower tier can then never be
        re-read and a data fix cannot propagate without recreating the process.

        Use this when a miss is cheap and correctness beats availability.
        """
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            age = (datetime.now() - entry.timestamp).total_seconds()
            return entry.data if age <= max_age else None

    def invalidate(self, key: str) -> None:
        """Invalidate a specific cache key."""
        with self._cache_lock:
            if key in self._cache:
                del self._cache[key]

    def clear_all(self) -> None:
        """Clear entire cache."""
        with self._cache_lock:
            self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._cache_lock:
            stats = {
                'total_entries': len(self._cache),
                'entries': {}
            }
            for key, entry in self._cache.items():
                age = (datetime.now() - entry.timestamp).total_seconds()
                stats['entries'][key] = {
                    'age_seconds': round(age, 1),
                    'ttl_seconds': entry.ttl_seconds,
                    'is_expired': entry.is_expired(),
                    'source': entry.source,
                    'error_count': entry.error_count
                }
            return stats

    # ========================================
    # Background Refresh System
    # ========================================

    def register_refresh_callback(self, key: str, callback: Callable,
                                   ttl: Optional[int] = None) -> None:
        """Register a callback for background refresh."""
        self._refresh_callbacks[key] = {
            'callback': callback,
            'ttl': ttl or self.DEFAULT_TTLS.get(key, 60)
        }

    def start_background_refresh(self, interval: int = 30) -> None:
        """Start background refresh thread."""
        if self._refresh_thread and self._refresh_thread.is_alive():
            logger.warning("Background refresh already running")
            return

        self._stop_refresh.clear()
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            args=(interval,),
            daemon=True,
            name="CacheRefreshThread"
        )
        self._refresh_thread.start()
        logger.info(f"Background cache refresh started (interval: {interval}s)")

    def stop_background_refresh(self) -> None:
        """Stop background refresh thread."""
        self._stop_refresh.set()
        if self._refresh_thread:
            self._refresh_thread.join(timeout=5)
        logger.info("Background cache refresh stopped")

    def _refresh_loop(self, interval: int) -> None:
        """Background refresh loop."""
        while not self._stop_refresh.is_set():
            try:
                self._do_refresh_cycle()
            except Exception as e:
                logger.error(f"Error in refresh cycle: {e}")

            # Wait for next cycle
            self._stop_refresh.wait(timeout=interval)

    def _do_refresh_cycle(self) -> None:
        """Execute one refresh cycle for all registered callbacks."""
        for key, config in self._refresh_callbacks.items():
            if self._is_circuit_open(key):
                continue

            try:
                # Check if refresh needed
                with self._cache_lock:
                    entry = self._cache.get(key)
                    if entry and not entry.is_expired():
                        continue

                # Execute refresh
                data = config['callback']()
                if data is not None:
                    self.set(key, data, config['ttl'], source='background_refresh')
                    self._reset_circuit_breaker(key)
                    logger.debug(f"Background refreshed: {key}")

            except Exception as e:
                logger.warning(f"Background refresh failed for {key}: {e}")
                self._record_failure(key, str(e))

    def _schedule_refresh(self, key: str, fetch_func: Callable,
                          ttl: Optional[int]) -> None:
        """Schedule an async refresh (fire and forget, deduplicated)."""
        with self._cache_lock:
            if key in self._refreshing:
                return  # Already refreshing this key
            self._refreshing.add(key)

        def do_refresh():
            try:
                data = fetch_func()
                if data is not None:
                    self.set(key, data, ttl, source='async_refresh')
            except Exception:
                # ERROR (not WARNING) so Sentry captures this as an
                # event rather than silently dropping it into a breadcrumb.
                logger.exception(f"Async refresh failed for {key}")
            finally:
                with self._cache_lock:
                    self._refreshing.discard(key)

        thread = threading.Thread(target=do_refresh, daemon=True)
        thread.start()

    # ========================================
    # Circuit Breaker
    # ========================================

    def _is_circuit_open(self, key: str) -> bool:
        """Check if circuit breaker is open (too many failures)."""
        if key not in self._circuit_breaker:
            return False

        if self._circuit_breaker[key] >= self.CIRCUIT_BREAKER_THRESHOLD:
            # Check if reset time has passed
            reset_time = self._circuit_breaker_reset_time.get(key)
            if reset_time and (datetime.now() - reset_time).total_seconds() > self.CIRCUIT_BREAKER_RESET:
                self._reset_circuit_breaker(key)
                return False
            return True
        return False

    def _record_failure(self, key: str, error: str) -> None:
        """Record a failure for circuit breaker."""
        self._circuit_breaker[key] = self._circuit_breaker.get(key, 0) + 1
        if self._circuit_breaker[key] >= self.CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_breaker_reset_time[key] = datetime.now()
            logger.warning(f"Circuit breaker opened for {key}")

        # Update cache entry error info
        with self._cache_lock:
            if key in self._cache:
                self._cache[key].error_count += 1
                self._cache[key].last_error = error

    def _reset_circuit_breaker(self, key: str) -> None:
        """Reset circuit breaker for a key."""
        self._circuit_breaker[key] = 0
        self._circuit_breaker_reset_time.pop(key, None)


# ========================================
# API Fetch Functions
# ========================================

class CryptoAPIFetcher:
    """
    Centralized crypto API fetcher with multi-source support.

    Priority order (fetch_combined_prices): Binance first (permitted, primary)
    > CoinPaprika (coverage gap-fill + enrichment only).

    ⚠ LICENSING: CoinPaprika's free tier
    bars ALL commercial use (ToS §3.6) and requires "Powered by Coinpaprika"
    attribution (§3.9) that Carrac does not display. Carrac is a commercial
    SaaS, so every live CoinPaprika call below is a known compliance gap.
    They remain wired ONLY because no permitted source (Binance, Hyperliquid,
    mempool.space) covers market-cap-denominated data (aggregate market cap,
    dominance, non-Binance-listed altcoin prices) — see the residual-decision
    writeup for the operator (paid CoinPaprika plan / alternate commercial-safe
    source / drop the dependent feature). Do not add new CoinPaprika call
    sites without that decision being made first.
    """

    # CoinPaprika ID mapping (symbol -> coinpaprika_id)
    COINPAPRIKA_MAP = {
        # Major coins
        'BTC': 'btc-bitcoin', 'ETH': 'eth-ethereum', 'SOL': 'sol-solana',
        'BNB': 'bnb-binance-coin', 'XRP': 'xrp-xrp', 'ADA': 'ada-cardano',
        'DOGE': 'doge-dogecoin', 'AVAX': 'avax-avalanche', 'DOT': 'dot-polkadot',
        # Stablecoins (needed for USDT.D calculation)
        'USDT': 'usdt-tether', 'USDC': 'usdc-usd-coin',
        'LINK': 'link-chainlink', 'TRX': 'trx-tron', 'POL': 'pol-polygon-ecosystem-token',
        'SHIB': 'shib-shiba-inu', 'LTC': 'ltc-litecoin', 'ATOM': 'atom-cosmos',
        'UNI': 'uni-uniswap', 'XLM': 'xlm-stellar', 'NEAR': 'near-near-protocol',
        'SUI': 'sui-sui', 'PEPE': 'pepe-pepe', 'APT': 'apt-aptos',

        # Layer 2s
        'ARB': 'arb-arbitrum', 'OP': 'op-optimism', 'IMX': 'imx-immutable-x',
        'STRK': 'strk-starknet', 'MNT': 'mnt-mantle', 'MANTA': 'manta-manta-network',

        # AI & Computing
        # RENDER is the live Binance ticker (post-rename); RNDR was the
        # pre-rename ticker and must NOT be re-added here — it collides
        # with RENDER in the reverse-map inversion below and shadows it.
        'FET': 'fet-fetch-ai', 'RENDER': 'rndr-render-token', 'TAO': 'tao-bittensor',
        'OCEAN': 'ocean-ocean-protocol',
        'AKT': 'akt-akash-network', 'AR': 'ar-arweave',

        # DeFi
        'AAVE': 'aave-aave', 'MKR': 'mkr-maker', 'CRV': 'crv-curve-dao-token',
        'SNX': 'snx-synthetix', 'COMP': 'comp-compound', 'LDO': 'ldo-lido-dao',
        'RUNE': 'rune-thorchain', '1INCH': '1inch-1inch', 'GMX': 'gmx-gmx',
        'PENDLE': 'pendle-pendle', 'JUP': 'jup-jupiter', 'ENA': 'ena-ethena',

        # Gaming & Metaverse
        'GALA': 'gala-gala', 'SAND': 'sand-the-sandbox', 'MANA': 'mana-decentraland',
        'AXS': 'axs-axie-infinity', 'ENJ': 'enj-enjin-coin', 'IMX': 'imx-immutable-x',
        'PRIME': 'prime-echelon-prime', 'BEAM': 'beam-beam',

        # Infrastructure
        'FIL': 'fil-filecoin', 'ICP': 'icp-internet-computer', 'INJ': 'inj-injective',
        'ALGO': 'algo-algorand', 'VET': 'vet-vechain', 'HBAR': 'hbar-hedera',
        'XTZ': 'xtz-tezos', 'EOS': 'eos-eos', 'FLOW': 'flow-flow',
        'EGLD': 'egld-elrond-erd', 'XMR': 'xmr-monero',
        'SEI': 'sei-sei', 'TIA': 'tia-celestia', 'STX': 'stx-stacks',

        # Memecoins
        'BONK': 'bonk-bonk', 'WIF': 'wif-dogwifhat', 'FLOKI': 'floki-floki',
        'TURBO': 'turbo-turbo', 'BRETT': 'brett-brett', 'FARTCOIN': 'fartcoin-fartcoin',

        # Other notable
        'TON': 'ton-toncoin', 'THETA': 'theta-theta-token',
        'QNT': 'qnt-quant', 'ONDO': 'ondo-ondo-finance', 'PYTH': 'pyth-pyth-network',
        'JTO': 'jto-jito', 'W': 'w-wormhole',

        # ===== NEW SYMBOLS =====
        # Major additions
        'BCH': 'bch-bitcoin-cash', 'ETC': 'etc-ethereum-classic', 'NEO': 'neo-neo',
        'ZEC': 'zec-zcash',
        # DeFi (extended)
        'DYDX': 'dydx-dydx-chain', 'ENS': 'ens-ethereum-name-service',
        'GRT': 'grt-the-graph', 'SUSHI': 'sushi-sushiswap',
        'CAKE': 'cake-pancakeswap', 'LQTY': 'lqty-liquity',
        'RSR': 'rsr-reserve-rights', 'RDNT': 'rdnt-radiant-capital',
        'LISTA': 'lista-lista-dao',
        # Gaming (extended)
        'APE': 'ape-apecoin', 'PIXEL': 'pixel-pixels',
        'PORTAL': 'portal-portal', 'BIGTIME': 'bigtime-big-time',
        'MAGIC': 'magic-magic', 'ILV': 'ilv-illuvium',
        # AI (extended)
        'AIXBT': 'aixbt-aixbt', 'WLD': 'wld-worldcoin',
        'ARKM': 'arkm-arkham',
        # Infrastructure (extended)
        'JASMY': 'jasmy-jasmycoin', 'CHZ': 'chz-chiliz', 'ROSE': 'rose-oasis-network',
        'CFX': 'cfx-conflux-network', 'KAVA': 'kava-kava', 'OSMO': 'osmo-osmosis',
        'LUNA': 'luna2-terra-v2', 'ZRO': 'zro-layerzero',
        'SAGA': 'saga-saga', 'DYM': 'dym-dymension',
        'NTRN': 'ntrn-neutron', 'RAY': 'ray-raydium',
        'ACH': 'ach-alchemy-pay', 'BLUR': 'blur-blur',
        'SSV': 'ssv-ssv-network', 'ETHFI': 'ethfi-etherfi',
        'MOVE': 'move-movement',
        # New L1/L2
        'BERA': 'bera-berachain', 'KAITO': 'kaito-kaito',
        # Memecoins (extended)
        'ORDI': 'ordi-ordi', 'NOT': 'not-notcoin', 'DOGS': 'dogs-dogs',
        'NEIRO': 'neiro-first-neiro-on-ethereum', 'PNUT': 'pnut-peanut-the-squirrel',
        'TRUMP': 'trump-official-trump', 'VIRTUAL': 'virtual-virtual-protocol',
        'ANIME': 'anime-animecoin',
        # Narrative
        'EIGEN': 'eigen-eigenlayer',
    }

    # No CoinCap (its keyless v2 API is retired; REST is dead and WS requires
    # an API key) and no CoinGecko (non-commercial on the free tier).
    # Binance > CoinPaprika carry combined
    # prices; XMR/AKT/DEEP live ticks moved to Kraken + Gate.io browser WS
    # (static/js/table-tickers-ws.js).

    @staticmethod
    def fetch_coinpaprika_tickers() -> Dict[str, Dict[str, Any]]:
        """
        Fetch all tickers from CoinPaprika.
        Returns dict mapping our symbol to price data.

        ⚠ LICENSING GATE: CoinPaprika's free tier bars commercial
        use — see the class docstring. This call is NOT safely removable:
        fetch_combined_prices() uses it for (a) price/volume for the ~14
        COINPAPRIKA_MAP symbols Binance does not list (no permitted
        alternative), and (b) ath_distance_pct/change_pct_7d/change_pct_30d
        enrichment for EVERY symbol, which Binance's ticker payload never
        provides. Removing this call silently drops both. Residual — an open
        decision (paid CoinPaprika plan / commercial-safe replacement
        / drop the affected fields).
        """
        try:
            # Check rate limit before calling
            try:
                from api_rate_limiter import can_call, record_call
                allowed, reason = can_call('coinpaprika')
                if not allowed:
                    logger.warning(f"CoinPaprika rate limit: {reason}")
                    return {}
            except ImportError:
                pass

            url = "https://api.coinpaprika.com/v1/tickers"
            response = requests.get(url, timeout=15)

            # Record the API call
            try:
                from api_rate_limiter import record_call
                record_call('coinpaprika')
            except ImportError:
                pass

            response.raise_for_status()
            all_tickers = response.json()

            # Build reverse map for quick lookup
            reverse_map = {v: k for k, v in CryptoAPIFetcher.COINPAPRIKA_MAP.items()}

            result = {}
            for ticker in all_tickers:
                cp_id = ticker.get('id', '')
                if cp_id in reverse_map:
                    symbol = reverse_map[cp_id]
                    quotes = ticker.get('quotes', {}).get('USD', {})
                    result[symbol] = {
                        'price': quotes.get('price'),
                        'change_pct': quotes.get('percent_change_24h', 0),
                        'volume_24h': quotes.get('volume_24h', 0),
                        'market_cap': quotes.get('market_cap', 0),
                        # Already-paid-for enrichment fields.
                        # No default — missing upstream field must
                        # surface as None, not a fabricated 0.
                        'ath_distance_pct': quotes.get('percent_from_price_ath'),
                        'change_pct_7d': quotes.get('percent_change_7d'),
                        'change_pct_30d': quotes.get('percent_change_30d'),
                        'source': 'coinpaprika'
                    }

            logger.info(f"CoinPaprika: fetched {len(result)} tickers")
            return result

        except Exception as e:
            logger.warning(f"CoinPaprika fetch failed: {e}")
            return {}

    # There is deliberately no fetch for CoinPaprika's /v1/global endpoint.
    # TOTAL/BTC.D are computed from fetch_coinpaprika_top_coins() below via
    # database.py's _get_combined_market_caps(), not from that endpoint. Do
    # not add it without a real caller and a licensing check (see the class
    # docstring).

    @staticmethod
    def fetch_coinpaprika_top_coins(limit: int = 100) -> List[Dict[str, Any]]:
        """
        Fetch top coins by market cap from CoinPaprika.

        Returns list of coins with symbol, name, market_cap, price.

        No API key required, ~2500 calls/day limit.

        ⚠ LICENSING GATE: see the class docstring — CoinPaprika's
        free tier bars commercial use. This is the PRIMARY source for
        aggregate market-cap data (TOTAL/TOTAL2/TOTAL3/BTC.D/USDT.D/
        OTHERS.D via database.py's _get_combined_market_caps, plus the Alt
        Season Index). Binance has no market-cap field at all, so there is
        no permitted replacement in the codebase today. data_sources/
        coinpaprika.py serves stale cache on failure, which is
        an uptime measure, not a licensing fix. Residual — an open decision.
        """
        try:
            from api_rate_limiter import can_call, record_call
            allowed, reason = can_call('coinpaprika')
            if not allowed:
                logger.warning(f"CoinPaprika top coins rate limit: {reason}")
                return []
        except ImportError:
            pass

        try:
            # Tickers endpoint returns coins sorted by market cap (with quotes parameter)
            url = "https://api.coinpaprika.com/v1/tickers?quotes=USD"
            response = requests.get(url, timeout=20)

            try:
                from api_rate_limiter import record_call
                record_call('coinpaprika')
            except ImportError:
                pass

            response.raise_for_status()
            all_tickers = response.json()

            # Sort by market cap (should already be sorted, but ensure)
            all_tickers.sort(
                key=lambda x: x.get('quotes', {}).get('USD', {}).get('market_cap', 0),
                reverse=True
            )

            coins = []
            for ticker in all_tickers[:limit]:
                quotes = ticker.get('quotes', {}).get('USD', {})
                market_cap = quotes.get('market_cap', 0)

                # Skip coins without market cap
                if market_cap <= 0:
                    continue

                coins.append({
                    'symbol': ticker.get('symbol', ''),
                    'name': ticker.get('name', ''),
                    'market_cap': market_cap,
                    'price': quotes.get('price', 0),
                    'volume_24h': quotes.get('volume_24h', 0),
                    'change_24h': quotes.get('percent_change_24h', 0),
                    'source': 'coinpaprika'
                })

            logger.info(f"CoinPaprika: fetched top {len(coins)} coins by market cap")
            return coins

        except Exception as e:
            logger.warning(f"CoinPaprika top coins fetch failed: {e}")
            return []

    @staticmethod
    def fetch_binance_tickers() -> Dict[str, Dict[str, Any]]:
        """Fetch 24hr tickers from Binance."""
        try:
            # Check rate limit before calling
            try:
                from api_rate_limiter import can_call, record_call
                allowed, reason = can_call('binance')
                if not allowed:
                    logger.warning(f"Binance rate limit: {reason}")
                    return {}
            except ImportError:
                pass

            url = "https://data-api.binance.vision/api/v3/ticker/24hr"
            response = requests.get(url, timeout=10)

            # Record the API call
            try:
                from api_rate_limiter import record_call
                record_call('binance')
            except ImportError:
                pass

            response.raise_for_status()
            all_tickers = response.json()

            result = {}
            zombie_count = 0
            for ticker in all_tickers:
                symbol = ticker.get('symbol', '')
                # Only USDT pairs
                if symbol.endswith('USDT'):
                    our_symbol = symbol[:-4]  # Remove USDT suffix
                    if our_symbol in CryptoAPIFetcher.COINPAPRIKA_MAP:
                        # Delisted/frozen pairs still show up in the raw dump
                        # with a stale lastPrice but a dead order book
                        # (bidPrice=askPrice=0) — skip, don't import as live.
                        try:
                            bid_price = float(ticker.get('bidPrice', 1))
                            ask_price = float(ticker.get('askPrice', 1))
                        except (TypeError, ValueError):
                            bid_price = ask_price = 1.0
                        if bid_price == 0 and ask_price == 0:
                            zombie_count += 1
                            continue

                        result[our_symbol] = {
                            'price': float(ticker.get('lastPrice', 0)),
                            'open': float(ticker.get('openPrice', 0)),
                            'change_pct': float(ticker.get('priceChangePercent', 0)),
                            'high_24h': float(ticker.get('highPrice', 0)),
                            'low_24h': float(ticker.get('lowPrice', 0)),
                            'volume_24h': float(ticker.get('volume', 0)),
                            'source': 'binance'
                        }

            if zombie_count > 0:
                logger.info(f"Binance: skipped {zombie_count} delisted/frozen tickers (dead order book)")
            logger.info(f"Binance: fetched {len(result)} tickers")
            return result

        except Exception as e:
            logger.warning(f"Binance fetch failed: {e}")
            return {}

    @staticmethod
    def fetch_combined_prices() -> Dict[str, Dict[str, Any]]:
        """
        Fetch prices from multiple sources and merge.
        OPTIMIZED: Parallel API calls with proper thread safety.

        Priority: Binance (for % change + as primary) > CoinPaprika (coverage)
        """
        result = {}
        result_lock = threading.Lock()
        paprika_data: Dict[str, Dict[str, Any]] = {}

        def fetch_binance():
            return ('binance', CryptoAPIFetcher.fetch_binance_tickers())

        def fetch_paprika():
            return ('paprika', CryptoAPIFetcher.fetch_coinpaprika_tickers())

        # Parallel fetch from Binance and CoinPaprika (primary sources)
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix='CryptoFetch') as executor:
            futures = [
                executor.submit(fetch_binance),
                executor.submit(fetch_paprika)
            ]

            # Process results as they complete
            try:
                for future in as_completed(futures, timeout=20):
                    try:
                        source_name, data = future.result()
                        with result_lock:
                            if source_name == 'binance':
                                # Binance is primary - add all
                                for symbol, price_data in data.items():
                                    result[symbol] = price_data
                            else:
                                # Secondary sources - only add if not already present
                                paprika_data = data
                                for symbol, price_data in data.items():
                                    if symbol not in result:
                                        result[symbol] = price_data
                    except Exception as e:
                        logger.warning(f"API fetch error: {e}")
            except TimeoutError:
                logger.warning("fetch_combined_prices: startup price fetch timed out after 20s — proceeding with partial results")
                for f in futures:
                    f.cancel()

        # CoinPaprika-only enrichment fields (ATH distance,
        # 7d/30d change) don't exist in Binance's ticker payload, so the
        # "Binance owns this symbol -> Paprika dict discarded" merge above
        # would otherwise silently drop them for every Binance-covered
        # symbol (the common case). Splice them onto the winning entry
        # instead of discarding — pure enrichment, doesn't touch price/
        # change_pct/volume/market_cap/source which stay Binance-sourced.
        _CP_ENRICH_KEYS = ('ath_distance_pct', 'change_pct_7d', 'change_pct_30d')
        for symbol, cp_data in paprika_data.items():
            entry = result.get(symbol)
            if entry is not None and entry.get('source') != 'coinpaprika':
                for key in _CP_ENRICH_KEYS:
                    if key in cp_data:
                        entry[key] = cp_data[key]

        logger.info(f"Combined prices: {len(result)} total symbols")
        return result


# Global singleton instance
cache_manager = GlobalCacheManager()
crypto_fetcher = CryptoAPIFetcher()


def initialize_cache(start_refresh: bool = False):
    """Initialize cache with all data sources.

    Args:
        start_refresh: If True, start the background refresh thread.
            Only ONE gunicorn worker should set this to True (use file lock).
    """
    # Register refresh callbacks
    # NOTE: Client has Binance WebSocket for live crypto prices.
    # These server-side caches are only for initial page load + non-WS displays.
    # Keep TTLs high to stay within API rate limits (esp. CoinPaprika 2000/day).
    cache_manager.register_refresh_callback(
        'combined_crypto_prices',
        CryptoAPIFetcher.fetch_combined_prices,
        ttl=300  # 5 min (client WS has real-time prices, this is a fallback)
    )
    cache_manager.register_refresh_callback(
        'binance_tickers',
        CryptoAPIFetcher.fetch_binance_tickers,
        ttl=120  # 2 min (Binance has generous limits)
    )
    # NOTE: coinpaprika_tickers not registered separately — already called
    # via combined_crypto_prices. Avoids double CoinPaprika calls.

    # Do initial fetch
    logger.info("Performing initial cache population...")
    cache_manager.set('combined_crypto_prices',
                      CryptoAPIFetcher.fetch_combined_prices(),
                      ttl=300, source='initial')

    # Only start background refresh in the designated worker
    if start_refresh:
        cache_manager.start_background_refresh(interval=30)
        logger.info("Cache initialization complete (with background refresh)")
    else:
        logger.info("Cache initialization complete (no background refresh — another worker handles it)")


def get_cached_crypto_prices() -> Dict[str, Dict[str, Any]]:
    """Get cached crypto prices (primary interface for the web app and worker)."""
    return cache_manager.get(
        'combined_crypto_prices',
        fetch_func=CryptoAPIFetcher.fetch_combined_prices,
        ttl=300,
        allow_stale=True
    ) or {}


# ========================================
# Unified Cache Interface for database.py
# ========================================

_ltf_cache = {}
_ltf_cache_lock = threading.Lock()
_LTF_CACHE_TTL = 2400  # 40 min — must survive across precompute cycles (30 min interval + 18 min compute)
# Callers may pass a longer per-symbol TTL (see set_cached_ltf_data) for slow-moving
# fallback sources, to limit upstream re-fetches.

_ltf_log = logging.getLogger('carrac.ltf_cache')


def _get_ltf_redis():
    """Get Redis client for LTF L2 cache (reuses the same singleton as caches.py)."""
    try:
        from carrac.redis_cache import get_redis
        return get_redis()
    except Exception:
        return None


def _ltf_redis_key(symbol_upper: str) -> str:
    return f"mcd:ltf:df:{symbol_upper}"


def get_cached_ltf_data(symbol: str) -> Optional[Dict[str, Any]]:
    """Get cached LTF (hourly) DataFrame dict for a symbol.

    Two-level cache:
      L1 — in-process dict (fast, per-worker)
      L2 — Redis pickle blob (cross-worker, same TTL)

    Flow:
      L1 hit  → return immediately (no Redis call)
      L2 hit  → deserialize, populate L1, return
      Both miss → return None (caller fetches/computes + calls set_cached_ltf_data)

    Redis is a pure accelerator — if it is down or returns a corrupt blob the
    function degrades to L1-only.

    Uses a dedicated TTL cache instead of cache_manager to ensure data
    actually expires.  cache_manager.get() without a fetch_func returns
    stale data indefinitely, which caused the recurring BTC 1H gap bug.

    Per-entry TTL: entries written via set_cached_ltf_data(ttl=...) carry their
    own TTL. L1 reads
    use the stored TTL; L2 expiry is set to the same value at write time.
    """
    import pickle as _pickle

    key = symbol.upper()

    # ── L1 check ──────────────────────────────────────────────────────────
    with _ltf_cache_lock:
        entry = _ltf_cache.get(key)
        if entry:
            # entry is (timestamp, data) or (timestamp, data, ttl)
            ts, data = entry[0], entry[1]
            ttl = entry[2] if len(entry) > 2 else _LTF_CACHE_TTL
            if (time.time() - ts) < ttl:
                return data

    # ── L2 check (Redis) ──────────────────────────────────────────────────
    r = _get_ltf_redis()
    if r:
        try:
            raw = r.get(_ltf_redis_key(key))
            if raw:
                # pickle is safe here only because the Redis is internal: it runs
                # on the private compose network with no published port, requires
                # --requirepass, and this app is the only writer of this key.
                # Anyone who could write this key already holds the Redis password.
                data = _pickle.loads(raw)  # nosec B301  # nosemgrep
                # Populate L1 with default TTL (Redis still has it → data is fresh)
                with _ltf_cache_lock:
                    _ltf_cache[key] = (time.time(), data, _LTF_CACHE_TTL)
                _ltf_log.debug("LTF L2 cache hit for %s", key)
                return data
        except Exception as exc:
            # Corrupt blob, version mismatch, connection error — fall through to recompute
            _ltf_log.debug("LTF L2 cache miss/error for %s: %s", key, exc)

    return None


def set_cached_ltf_data(symbol: str, data: Dict[str, Any],
                        ttl: Optional[int] = None) -> None:
    """Cache LTF DataFrame dict for a symbol.

    Writes to both L1 (in-process) and L2 (Redis, if available).
    Redis failure is silently swallowed — L1 always gets the data.

    Args:
        symbol: Upper-cased ticker symbol.
        data:   {1h, 2h, 4h} DataFrame dict.
        ttl:    Override cache TTL in seconds.  Pass a per-source TTL
                for fallback-sourced data to reduce hourly re-fetches
                from ~36/day to ~6/day per symbol.  Defaults to _LTF_CACHE_TTL (40 min).
    """
    import pickle as _pickle

    key = symbol.upper()
    effective_ttl = ttl if ttl is not None else _LTF_CACHE_TTL

    # ── L1 write (always) ─────────────────────────────────────────────────
    with _ltf_cache_lock:
        _ltf_cache[key] = (time.time(), data, effective_ttl)

    # ── L2 write (Redis, best-effort) ────────────────────────────────────
    r = _get_ltf_redis()
    if r:
        try:
            blob = _pickle.dumps(data, protocol=4)  # paired with the loads above: internal, password-protected Redis
            r.set(_ltf_redis_key(key), blob, ex=effective_ttl)
        except Exception as exc:
            _ltf_log.debug("LTF L2 set error for %s: %s", key, exc)


def get_cached_top_coins() -> Optional[List[Dict[str, Any]]]:
    """Get cached top coins list."""
    return cache_manager.get('top_coins', ttl=3600, allow_stale=True)


def set_cached_top_coins(coins: List[Dict[str, Any]], source: str = 'coinpaprika') -> None:
    """Cache top coins list."""
    cache_manager.set('top_coins', coins, ttl=3600, source=source)


def get_coinpaprika_top_coins(limit: int = 100) -> Optional[List[Dict[str, Any]]]:
    """Get top coins by market cap from CoinPaprika (cached)."""
    return cache_manager.get(
        'coinpaprika_top_coins',
        fetch_func=lambda: CryptoAPIFetcher.fetch_coinpaprika_top_coins(limit),
        ttl=3600,
        allow_stale=True
    )


MARKET_CAP_MEMO_TTL = 3600


def get_cached_market_caps() -> Optional[Any]:
    """Get the in-process market-cap memo, or None once it is past its TTL.

    Must NOT be stale-tolerant. This memo fronts a 6h on-disk tier that is
    shared by every process, and _get_combined_market_caps returns on the
    first non-None tier. A stale-tolerant get() would let a worker that had
    computed a frame once serve it until the process is recycled — the file
    tier would never be re-read, so a corrected market-cap dataset could not
    reach a running worker, and a stale memo could silently overwrite a
    repaired BTC.D with its corrupt shape. Falling
    through to the file tier costs a JSON restore, not a rebuild.
    """
    return cache_manager.get_if_fresh('market_caps', MARKET_CAP_MEMO_TTL)


def set_cached_market_caps(data: Any) -> None:
    """Cache market cap DataFrame."""
    cache_manager.set('market_caps', data, ttl=3600, source='calculated')

