"""
Crypto market metrics: top coins, MVRV, Mayer, Puell, alt season.

Top coins use CoinPaprika only. There is deliberately no CryptoCompare
fallback: CryptoCompare is barred on every commercial tier.
On-chain metrics use CoinMetrics public CSV data (GitHub raw).
"""

import logging
import time
from datetime import datetime
from typing import Optional, Dict

from cache_manager import (
    get_cached_top_coins, set_cached_top_coins,
    get_coinpaprika_top_coins
)
from data_sources.base import BaseDataSource

logger = logging.getLogger('carrac.data_sources.coinpaprika')


# Crypto provenance registration, in ONE place.
#
# ``get_top_coins`` writes discovered symbols into ``crypto_map``. Registering
# only on the cache-MISS branch is a bug: the top-coins list has a 1h
# Redis TTL, so on any ordinary call the function returns at the cache-hit
# early return and the registration line is never reached, e.g.
#
#     cache miss -> crypto_map has 'M'/'PI'/'SKY'/'LEO'
#     cache hit  -> crypto_map lacks all four
#
# An absent symbol is not crypto to ``database._is_crypto``, so
# ``get_ohlcv_data`` sends it down the EQUITY path and resolves a crypto ticker
# against a US stock universe (M becomes Macy's, PI becomes Impinj).
#
# Every return path therefore goes through here, so a cache hit registers exactly
# what a cache miss registers.
def _register_crypto_provenance(coins: list, crypto_map: Optional[dict]) -> None:
    """Record that these symbols came from a crypto market-cap list."""
    if crypto_map is None or not coins:
        return
    for coin in coins:
        symbol = (coin or {}).get('symbol')
        if symbol:
            crypto_map[symbol] = symbol



class CryptoMetricsClient(BaseDataSource):
    """Crypto market metrics: top coins, on-chain indicators, alt season.

    Subclasses BaseDataSource with NAME='coinmetrics' for the MVRV/Mayer/Puell
    GitHub CSV pulls. There is no CryptoCompare fallback for top coins any
    more — it was removed (barred vendor, and it never worked).
    """

    NAME = 'coinmetrics'
    DEFAULT_TIMEOUT = 30.0

    # Exclude stablecoins and wrapped tokens from alt season
    ALT_SEASON_EXCLUDED = {
        'USDT', 'USDC', 'DAI', 'BUSD', 'TUSD', 'USDP', 'USDD', 'GUSD', 'FRAX',
        'WBTC', 'WETH', 'STETH', 'CBETH', 'RETH', 'WSTETH',
        'PAXG', 'XAUT'
    }

    # Fewest coins we will publish an Alt Season reading from. The Blockchain
    # Center methodology counts 50; below this a "% outperforming BTC" is a
    # coin-flip dressed up as a market read, so we report unavailable instead.
    # A healthy run counts the full 50.
    ALT_SEASON_MIN_SAMPLE = 20
    # wall-clock bound for the per-symbol loop below. Every
    # daily crypto cache goes stale at UTC midnight at once, so the first
    # compute after 00:00 would do ~51 live delta downloads on the request
    # thread and blow the 12 s sentiment budget. Kept under that budget
    # the same way FredClient.GET_ALL_TIMEOUT_S is.
    ALT_SEASON_COMPUTE_BUDGET_S = 8.0

    # Crypto blacklist - exclude from top coins API
    CRYPTO_BLACKLIST = {
        'DAI', 'JITOSOL', 'RLUSD', 'USD1', 'USDG', 'USDS', 'WBETH', 'WEETH',
        'AETHWETH', 'PYUSD', 'STETH', 'USDC', 'USDE', 'USDT', 'XAUT',
        'BTCB', 'CBBTC', 'WBNB', 'SUSDE', 'RSETH', 'USD0', 'USDTB', 'USDF', 'SUSDS',
    }

    def __init__(self):
        super().__init__()

        # Alt season cache
        self._alt_season_cache: Optional[Dict] = None
        self._alt_season_cache_time: Optional[datetime] = None
        self.ALT_SEASON_CACHE_TTL = 3600

    # ========================================
    # Top Coins
    # ========================================

    def get_top_coins(self, limit: int = 70, crypto_map: dict = None) -> list:
        """Get top coins by market cap.

        Source: CoinPaprika only. There is no CryptoCompare fallback —
        CryptoCompare is barred on every commercial tier. On a
        CoinPaprika failure this degrades explicitly: serve the last
        cached read if one exists, otherwise log loudly and return empty —
        never a silent [].
        Updates crypto_map with discovered coins.
        """
        # Check centralized cache (1 hour TTL)
        cached = get_cached_top_coins()
        if cached is not None:
            logger.debug(f"Top coins cache hit ({len(cached)} coins)")
            # Register on the HIT path too. This is the branch the
            # function actually takes almost every time (1h TTL), and it was
            # the one that skipped registration.
            _register_crypto_provenance(cached, crypto_map)
            return cached[:limit]

        # ========== TRY COINPAPRIKA FIRST ==========
        try:
            coins = get_coinpaprika_top_coins(limit=limit + 10)
            if coins and len(coins) >= limit // 2:
                filtered = [c for c in coins if c['symbol'] not in self.CRYPTO_BLACKLIST]

                # Ensure USDT is included
                usdt_symbols = [c['symbol'] for c in filtered]
                if 'USDT' not in usdt_symbols:
                    usdt_coin = next((c for c in coins if c['symbol'] == 'USDT'), None)
                    if usdt_coin:
                        filtered.insert(3, usdt_coin)
                        logger.info(f"Added USDT back (market cap: ${usdt_coin['market_cap']:,.0f})")

                set_cached_top_coins(filtered, source='coinpaprika')
                _register_crypto_provenance(filtered, crypto_map)

                logger.info(f"CoinPaprika: got {len(filtered)} top coins")
                return filtered[:limit]
        except Exception as e:
            logger.warning(f"CoinPaprika top coins failed: {e}")

        # No CryptoCompare fallback any more — it never
        # worked (401 on every call) and CryptoCompare is barred on every
        # commercial tier regardless. Degrade explicitly: serve stale cache
        # if there is any, otherwise say so loudly and return empty.
        stale = get_cached_top_coins()
        if stale:
            logger.warning(
                "CoinPaprika top coins failed and the CryptoCompare fallback "
                "was removed (barred vendor) — serving %d stale "
                "cached coins", len(stale)
            )
            _register_crypto_provenance(stale, crypto_map)
            return stale[:limit]

        logger.error(
            "Top coins unavailable: CoinPaprika failed and no cached data "
            "exists (CryptoCompare fallback removed) — "
            "returning empty rather than a barred/broken vendor"
        )
        return []

    # ========================================
    # Alt Season Index
    # ========================================

    @staticmethod
    def _alt_season_unavailable(reason: str) -> Dict:
        """The one shape every Alt Season failure returns.

        A failure must not return a fabricated reading such as ``{'index': 50,
        'season': 'neutral', ...}``, which would render identically to a
        measured one in the sentiment bar, ticker bar, widget, MCP tool and
        morning report. Omit, never invent (``carrac/facts/bands.py`` precedent).
        Consumers must treat ``index is None`` as "we do not know".
        """
        logger.warning("Alt Season Index unavailable: %s", reason)
        return {'index': None, 'season': None, 'error': reason}

    def calculate_alt_season_index(self, get_90d_performance_fn) -> Dict:
        """Calculate Alt Season Index using Blockchain Center methodology.

        Args:
            get_90d_performance_fn: Callable(symbol) -> Optional[float]
                Returns 90-day price performance % for a symbol.

        Returns:
            On success: index (0-100), season, btc_performance, outperformers,
            total_counted, timestamp.
            On any failure: ``index=None, season=None`` plus ``error``. Never a
            substituted number — see :meth:`_alt_season_unavailable`.
        """
        if (self._alt_season_cache_time and
                (datetime.now() - self._alt_season_cache_time).total_seconds() < self.ALT_SEASON_CACHE_TTL and
                self._alt_season_cache):
            return self._alt_season_cache

        try:
            top_coins = get_cached_top_coins()
            if not top_coins:
                top_coins = self.get_top_coins(70)

            if not top_coins:
                return self._alt_season_unavailable('no top-coin universe available')

            btc_perf = get_90d_performance_fn('BTC')
            if btc_perf is None:
                return self._alt_season_unavailable('no BTC 90d performance')

            outperformers = 0
            total_counted = 0
            deadline = time.monotonic() + self.ALT_SEASON_COMPUTE_BUDGET_S
            budget_hit = False

            for coin in top_coins[:70]:
                symbol = coin['symbol']
                if symbol == 'BTC' or symbol in self.ALT_SEASON_EXCLUDED:
                    continue
                if total_counted >= 50:
                    break
                if time.monotonic() > deadline:
                    budget_hit = True
                    logger.warning(
                        "Alt Season Index: %.1fs compute budget hit after %d coins "
                        "counted — serving a partial sample",
                        self.ALT_SEASON_COMPUTE_BUDGET_S, total_counted,
                    )
                    break

                coin_perf = get_90d_performance_fn(symbol)
                if coin_perf is not None:
                    total_counted += 1
                    if coin_perf > btc_perf:
                        outperformers += 1

            if total_counted < self.ALT_SEASON_MIN_SAMPLE:
                reason = (f'only {total_counted} of {self.ALT_SEASON_MIN_SAMPLE} required '
                          f'coins had 90d performance')
                if budget_hit:
                    reason += (f' before the {self.ALT_SEASON_COMPUTE_BUDGET_S:.0f}s '
                               f'compute budget ran out')
                return self._alt_season_unavailable(reason)

            index = (outperformers / total_counted) * 100

            if index >= 75:
                season = 'altcoin'
            elif index <= 25:
                season = 'bitcoin'
            else:
                season = 'neutral'

            result = {
                'index': round(index, 1),
                'season': season,
                'btc_performance': round(float(btc_perf), 2),
                'outperformers': outperformers,
                'total_counted': total_counted,
                # Consumers historically read `total_analyzed` and got 0 for
                # every reading — the sample size that makes
                # the number auditable was never actually exposed.
                'total_analyzed': total_counted,
                'timestamp': datetime.now().isoformat()
            }

            self._alt_season_cache = result
            self._alt_season_cache_time = datetime.now()

            logger.info(f"Alt Season Index: {index:.1f} ({season}) - {outperformers}/{total_counted} outperforming BTC")
            return result

        except Exception as e:
            return self._alt_season_unavailable(str(e))
