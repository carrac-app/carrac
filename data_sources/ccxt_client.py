"""
CCXT multi-exchange client for coins not available on Binance or Hyperliquid.

Wraps MEXC, Gate.io, KuCoin, and Bybit via the CCXT unified API.
Auto-discovery via load_markets() + curated seed/override list.

# Fallback chain position: Binance -> Hyperliquid -> CCXT (last venue)

# WARNING: Volume data from CCXT exchanges is single-venue volume, NOT
#          comparable to Binance aggregate spot volume. Volume-based indicators
#          (CVD, MFI, OBV) work correctly per-coin (self-relative ratios),
#          but absolute cross-venue volume comparisons are meaningless.
#          Use the `_data_venue` attribute on returned DataFrames for
#          venue-aware display and sorting.

# Kill switches:
#   CCXT_DISABLED=1            — disable all CCXT exchanges
#   CCXT_MEXC_DISABLED=1       — per-exchange kill switches
#   CCXT_GATEIO_DISABLED=1
#   CCXT_KUCOIN_DISABLED=1
#   CCXT_BYBIT_DISABLED=1
"""

import json
import logging
import os
import re
import time
import threading
from datetime import datetime, timedelta
from typing import Optional, Tuple, Set, Dict

import pandas as pd

from data_sources.circuit_breaker import CircuitBreaker

logger = logging.getLogger('carrac.data_sources.ccxt_client')


class CcxtClient:
    """CCXT-based multi-exchange client for coins not on Binance or Hyperliquid.

    CCXT has its own retry + timeout handling, so we don't wrap it in
    BaseDataSource._request(). Instead we maintain per-exchange circuit
    breakers that skip a venue for HALF_OPEN_AFTER seconds after repeated
    failures — matches the resilience story of the other clients.
    """

    # gateio first: MEXC blocks its public JSON kline WS
    # channel ("Blocked!" on subscribe), so mexc-routed coins get REST candles
    # but NO browser live tick — prefer a venue whose WS works for new coins.
    EXCHANGE_PRIORITY = ['gateio', 'mexc', 'kucoin', 'bybit']
    MARKET_CACHE_TTL = 86400  # 24h — refresh routing table daily

    # Redis persistence: the routing table is expensive to rebuild (~14s of
    # load_markets() calls across 4 exchanges), so we persist the RESULT in
    # Redis. It survives container restarts (no rebuild on cold start) and
    # de-dups the build across gunicorn/uvicorn workers.
    ROUTING_REDIS_KEY = 'mcd:ccxt:routing'        # payload: {table, built_at, logic_version}

    # Bump when the ROUTING LOGIC changes (what gets excluded, which venues,
    # how a base is accepted) — not for ordinary listing churn.
    #
    # Without this, a routing change stays dormant for up to
    # MARKET_CACHE_TTL after deploy, because a table built by the previous
    # logic is still inside its TTL and is loaded verbatim. The change would
    # look like it did nothing for a day.
    #   1 — original routing logic
    #   2 — exclusion subtracts BinanceClient.UNAVAILABLE
    ROUTING_LOGIC_VERSION = 2
    ROUTING_LOCK_KEY = 'mcd:ccxt:routing:lock'    # NX build lock (cross-worker)
    ROUTING_LOCK_TTL = 120                        # s — generous vs ~14s build

    # Curated seed/override: symbol -> preferred exchange ID.
    # These take priority over auto-discovery. Use for coins where
    # we've verified the best venue by volume.
    # NOTE: seed changes only take effect after the Redis-persisted routing
    # table (ROUTING_REDIS_KEY) is rebuilt — delete the key on deploy or wait
    # out MARKET_CACHE_TTL.
    # The five ex-mexc coins are routed to gateio (verified tradable)
    # because MEXC blocks its public JSON kline WS channel — gateio gives
    # them working browser live ticks.
    CCXT_COINS_SEED = {
        'XMR': 'kucoin',       # Monero, ~$55M daily vol on KuCoin (table tick via Kraken WS)
        'CC': 'bybit',         # Cloudcoin, ~$2.2M daily vol on Bybit
        'AKT': 'gateio',       # Akash Network
        'ARC': 'gateio',       # AI Rig Complex (was mexc)
        'BUTTHOLE': 'mexc',    # Memecoin — not on gateio; REST candles only, no browser WS
        'CHEX': 'gateio',      # CHEX Token (was mexc)
        'DEEP': 'bybit',       # DeepBook
        'DSYNC': 'gateio',     # DSYNC (was mexc)
        'KIP': 'gateio',       # KIP Protocol
        'MLC': 'gateio',       # MLC (was mexc)
        'PIPPIN': 'gateio',    # Pippin (was mexc)
        'PRIME': 'gateio',     # Echelon Prime
    }

    # Valid symbol pattern: 2-10 alphanumeric chars (filters garbage)
    _SYMBOL_PATTERN = re.compile(r'^[A-Z0-9]{2,10}$')

    # Maps Carrac TF strings to CCXT interval strings (they match).
    SUPPORTED_INTERVALS = {
        '1m', '3m', '5m', '15m', '30m',
        '1h', '2h', '4h', '8h', '12h', '1d',
    }

    def __init__(self):
        self._exchanges: Dict[str, object] = {}  # exchange_id -> ccxt.Exchange (lazy)
        self._init_lock = threading.Lock()
        self._disabled = os.environ.get('CCXT_DISABLED', '').lower() in ('1', 'true', 'yes')

        # Auto-discovery routing table (built by _ensure_markets)
        self._routing_table: Dict[str, str] = {}  # SYMBOL -> exchange_id
        self._markets_loaded_at: Optional[datetime] = None
        self._load_lock = threading.Lock()          # guards the cold (empty) build
        self._refresh_lock = threading.Lock()       # dedups the background refresh
        self._last_refresh_sched = 0.0              # monotonic ts of last bg schedule

        # Exclusion sets (populated lazily)
        self._excluded_symbols: Optional[Set[str]] = None

        # Per-exchange circuit breakers (skip a venue after repeated failures).
        # MEXC opens chronically — it blocks this host and now only
        # serves one REST-only memecoin (BUTTHOLE), so its breaker ntfy is pure
        # noise (same reasoning as stooq's notify=False). Other venues still alert.
        self._breakers: Dict[str, CircuitBreaker] = {
            ex_id: CircuitBreaker(
                f'ccxt_{ex_id}', threshold=3, half_open_after=120.0,
                notify=(ex_id != 'mexc'),
            )
            for ex_id in self.EXCHANGE_PRIORITY
        }

    def _get_breaker(self, exchange_id: str) -> CircuitBreaker:
        if exchange_id not in self._breakers:
            self._breakers[exchange_id] = CircuitBreaker(
                f'ccxt_{exchange_id}', threshold=3, half_open_after=120.0,
                notify=(exchange_id != 'mexc'),
            )
        return self._breakers[exchange_id]

    # ========================================
    # Exchange management (lazy init)
    # ========================================

    def _get_exchange(self, exchange_id: str):
        """Get or create a CCXT exchange instance. Thread-safe lazy init."""
        if exchange_id in self._exchanges:
            return self._exchanges[exchange_id]

        with self._init_lock:
            if exchange_id in self._exchanges:
                return self._exchanges[exchange_id]

            import ccxt
            # ccxt 4.5+ renamed the Gate.io class `gateio` -> `gate`. Resolve via
            # getattr so we work on both old and new ccxt (and degrade to None
            # rather than raising AttributeError when the dict literal is built —
            # a direct `ccxt.gateio` reference would throw for EVERY exchange lookup).
            exchange_classes = {
                'mexc': ccxt.mexc,
                'gateio': getattr(ccxt, 'gate', None) or getattr(ccxt, 'gateio', None),
                'kucoin': ccxt.kucoin,
                'bybit': ccxt.bybit,
            }
            cls = exchange_classes.get(exchange_id)
            if not cls:
                return None

            ex = cls({'enableRateLimit': True, 'timeout': 30000})
            self._exchanges[exchange_id] = ex
            logger.debug(f"CCXT: initialized {exchange_id}")
            return ex

    def _get_excluded_symbols(self) -> Set[str]:
        """Symbols another source ALREADY SERVES, which CCXT must not duplicate.

        "Listed on Binance" and "served by Binance" are not the
        same set, and treating them as one orphaned eight symbols.

        ``spot_map`` is the raw pair list; ``UNAVAILABLE`` is the set Binance's
        own ``fetch_ohlcv`` refuses (delisted
        tickers whose order book is dead, ``bidPrice == askPrice == 0``,
        while the pair still appears in the exchange dump). A symbol in both
        was excluded from CCXT for being Binance's, then rejected by Binance,
        so nothing fetched it: BEAM, EOS, FTM, MKR, NTRN, OCEAN, RDNT, TON all
        fell through the chain with no venue to serve them, leaving
        the chart frozen with nothing to say so.

        Subtracting UNAVAILABLE lets auto-discovery pick these up wherever they
        genuinely trade. RDNT and BEAM come back (gate,
        plus mexc/bybit for BEAM). The rest are not on any configured venue and
        are a retirement question, not a routing one.
        """
        if self._excluded_symbols is not None:
            return self._excluded_symbols

        excluded = set()
        try:
            from data_sources.binance import BinanceClient
            excluded.update(BinanceClient().spot_map.keys())
            # Only what Binance will actually SERVE stays excluded.
            excluded.difference_update(BinanceClient.UNAVAILABLE)
        except Exception:
            pass
        try:
            from data_sources.hyperliquid import HyperliquidClient
            excluded.update(HyperliquidClient.HL_COINS)
        except Exception:
            pass

        self._excluded_symbols = excluded
        return excluded

    # ========================================
    # Auto-discovery
    # ========================================

    def _is_routing_fresh(self) -> bool:
        """True if the in-memory routing table exists and is within TTL."""
        return bool(
            self._routing_table and self._markets_loaded_at and
            (datetime.now() - self._markets_loaded_at).total_seconds() < self.MARKET_CACHE_TTL
        )

    def _ensure_markets(self):
        """Ensure the routing table is populated and reasonably fresh.

        Resolution order, cheapest first:
          1. In-memory table still fresh           → return (O(1)).
          2. In-memory table present but stale      → serve it, refresh in a
             background thread (non-blocking).
          3. No table yet (cold start / new worker) → populate synchronously:
             load from Redis if a previous build is cached there, otherwise
             rebuild from the exchanges. The first request blocks once.

        The actual Redis-load-or-rebuild lives in _populate_routing_table().
        Thread-safe.
        """
        if self._disabled:
            return

        if self._is_routing_fresh():
            return

        # Stale-but-present: keep serving the stale table, refresh out of band so
        # no user request eats the ~14s rebuild.
        if self._routing_table:
            self._schedule_routing_refresh()
            return

        # Cold: must populate before we can answer. Single builder per process.
        with self._load_lock:
            if self._routing_table:  # filled while we waited for the lock
                return
            self._populate_routing_table()

    def _populate_routing_table(self):
        """Adopt the routing table from Redis if fresh, else rebuild + persist.

        Called either under _load_lock (cold start) or from the background
        refresh thread (under _refresh_lock). Never run concurrently with
        itself in-process. Fail-safe: any Redis error degrades to a local
        rebuild.
        """
        # 1. A previous build cached in Redis survives restarts and is shared
        #    across workers — by far the common case after a deploy.
        table, built_at = self._load_routing_from_redis()
        if table and built_at and (datetime.now() - built_at).total_seconds() < self.MARKET_CACHE_TTL:
            self._routing_table = table
            self._markets_loaded_at = built_at
            age = int((datetime.now() - built_at).total_seconds())
            logger.info(f"CCXT routing table loaded from Redis: {len(table)} symbols "
                        f"(age {age}s) — skipped rebuild")
            return

        # 2. Redis miss/stale → rebuild. Gate the build with a cross-worker NX
        #    lock so a fleet restart doesn't have every worker hammering the
        #    exchanges at once. A worker that already has a (stale) table can
        #    keep serving it and let the lock-holder do the work.
        has_table = bool(self._routing_table)
        got_lock = self._acquire_build_lock()
        if not got_lock and has_table:
            logger.debug("CCXT: another worker is rebuilding routing table; serving stale")
            return

        try:
            new_table = self._build_routing_table()
            if new_table:
                self._routing_table = new_table
                self._markets_loaded_at = datetime.now()
                logger.info(f"CCXT routing table built: {len(new_table)} symbols "
                            f"across {len(set(new_table.values()))} exchanges")
                self._save_routing_to_redis(new_table, self._markets_loaded_at)
            elif not self._routing_table:
                # All exchanges failed and we have nothing — seed list fallback.
                excluded = self._get_excluded_symbols()
                for sym, ex_id in self.CCXT_COINS_SEED.items():
                    if sym not in excluded:
                        self._routing_table[sym] = ex_id
                logger.warning("CCXT: all exchanges failed, using seed list as fallback")
        finally:
            if got_lock:
                self._release_build_lock()

    def _build_routing_table(self) -> Dict[str, str]:
        """Build a fresh routing table by loading markets from each exchange.

        Pure(ish): returns the table, does not mutate self. Returns {} if every
        exchange failed.
        """
        excluded = self._get_excluded_symbols()
        new_table: Dict[str, str] = {}

        # Seed with curated overrides first (highest priority)
        for sym, ex_id in self.CCXT_COINS_SEED.items():
            if sym not in excluded:
                env_key = f'CCXT_{ex_id.upper()}_DISABLED'
                if os.environ.get(env_key, '').lower() not in ('1', 'true', 'yes'):
                    new_table[sym] = ex_id

        # Auto-discover from exchanges in priority order
        for exchange_id in self.EXCHANGE_PRIORITY:
            env_key = f'CCXT_{exchange_id.upper()}_DISABLED'
            if os.environ.get(env_key, '').lower() in ('1', 'true', 'yes'):
                continue

            try:
                ex = self._get_exchange(exchange_id)
                if not ex:
                    continue
                ex.load_markets()
                count = 0
                for pair, market in ex.markets.items():
                    # Only spot USDT pairs
                    if not market.get('spot', False):
                        continue
                    quote = market.get('quote', '')
                    if quote != 'USDT':
                        continue
                    if not market.get('active', True):
                        continue

                    base = market.get('base', '').upper()
                    if not base or not self._SYMBOL_PATTERN.match(base):
                        continue
                    if base in excluded:
                        continue
                    # First exchange wins (priority order); seed overrides already in table
                    if base not in new_table:
                        new_table[base] = exchange_id
                        count += 1

                logger.info(f"CCXT: {exchange_id} loaded, {count} new symbols added")
            except Exception as e:
                logger.warning(f"CCXT: failed to load markets for {exchange_id}: {e}")

        return new_table

    def _schedule_routing_refresh(self):
        """Kick off a single background routing-table refresh (non-blocking).

        Deduplicated by _refresh_lock (only one refresh in flight) and a short
        time guard so a burst of requests against a stale table doesn't churn
        threads while the refresh is settling.
        """
        now = time.monotonic()
        if now - self._last_refresh_sched < 10:
            return
        if not self._refresh_lock.acquire(blocking=False):
            return  # a refresh is already running
        self._last_refresh_sched = now

        def _run():
            try:
                self._populate_routing_table()
            except Exception:
                logger.exception("CCXT: background routing refresh failed")
            finally:
                self._refresh_lock.release()

        threading.Thread(target=_run, daemon=True, name='CcxtRoutingRefresh').start()

    # ========================================
    # Redis persistence (best-effort, fail-safe)
    # ========================================

    def _redis(self):
        """Shared Redis client (db 0 = cache), or None if unavailable."""
        try:
            from carrac.redis_cache import get_redis
            return get_redis()
        except Exception:
            return None

    def _load_routing_from_redis(self) -> Tuple[Optional[Dict[str, str]], Optional[datetime]]:
        """Return (table, built_at) from Redis, or (None, None) on miss/error."""
        r = self._redis()
        if not r:
            return None, None
        try:
            raw = r.get(self.ROUTING_REDIS_KEY)
            if not raw:
                return None, None
            payload = json.loads(raw)
            table = payload.get('table')
            built_at_raw = payload.get('built_at')
            if not table or not built_at_raw:
                return None, None
            # A table built by older routing logic is not merely old, it is
            # WRONG — discard it rather than serve it out for the rest of its
            # TTL. Absent key == version 1, the pre-stamp format.
            cached_version = payload.get('logic_version', 1)
            if cached_version != self.ROUTING_LOGIC_VERSION:
                logger.info(
                    "CCXT: discarding cached routing table built by logic "
                    "v%s (current v%s) — rebuilding",
                    cached_version, self.ROUTING_LOGIC_VERSION,
                )
                return None, None
            return table, datetime.fromisoformat(built_at_raw)
        except Exception as e:
            logger.debug(f"CCXT: routing-table Redis read failed: {e}")
            return None, None

    def _save_routing_to_redis(self, table: Dict[str, str], built_at: datetime):
        """Persist the routing table to Redis with a TTL just past MARKET_CACHE_TTL."""
        r = self._redis()
        if not r:
            return
        try:
            payload = json.dumps({
                'table': table,
                'built_at': built_at.isoformat(),
                'logic_version': self.ROUTING_LOGIC_VERSION,
            })
            r.set(self.ROUTING_REDIS_KEY, payload, ex=self.MARKET_CACHE_TTL + 300)
            logger.info(f"CCXT: routing table cached to Redis ({len(table)} symbols)")
        except Exception as e:
            logger.debug(f"CCXT: routing-table Redis write failed: {e}")

    def _acquire_build_lock(self) -> bool:
        """Try to claim the cross-worker build lock. True if we may build.

        No Redis → always True (no coordination possible, just build locally).
        """
        r = self._redis()
        if not r:
            return True
        try:
            return bool(r.set(self.ROUTING_LOCK_KEY, '1', nx=True, ex=self.ROUTING_LOCK_TTL))
        except Exception:
            return True

    def _release_build_lock(self):
        r = self._redis()
        if not r:
            return
        try:
            r.delete(self.ROUTING_LOCK_KEY)
        except Exception:
            pass

    def _resolve_symbol(self, symbol: str) -> Optional[Tuple]:
        """Resolve a Carrac symbol to (exchange, pair_string, exchange_id)."""
        self._ensure_markets()
        upper = symbol.upper()
        exchange_id = self._routing_table.get(upper)
        if not exchange_id:
            return None

        # Per-exchange kill switch
        env_key = f'CCXT_{exchange_id.upper()}_DISABLED'
        if os.environ.get(env_key, '').lower() in ('1', 'true', 'yes'):
            return None

        exchange = self._get_exchange(exchange_id)
        if not exchange:
            return None

        pair = f'{upper}/USDT'
        return exchange, pair, exchange_id

    # ========================================
    # Public interface (matches BinanceClient/HyperliquidClient)
    # ========================================

    def is_supported(self, symbol: str) -> bool:
        """Check if this symbol is served by CCXT.

        Triggers auto-discovery on first call (lazy). Subsequent calls
        use the cached routing table (O(1) dict lookup).
        """
        if self._disabled:
            return False
        self._ensure_markets()
        upper = symbol.upper()
        exchange_id = self._routing_table.get(upper)
        if not exchange_id:
            return False
        env_key = f'CCXT_{exchange_id.upper()}_DISABLED'
        if os.environ.get(env_key, '').lower() in ('1', 'true', 'yes'):
            return False
        return True

    def get_available_symbols(self) -> Set[str]:
        """Return all symbols in the routing table. Used by validate_symbol()."""
        self._ensure_markets()
        return set(self._routing_table.keys())

    def get_venue_for_symbol(self, symbol: str) -> Optional[str]:
        """Return the exchange ID serving this symbol, or None."""
        self._ensure_markets()
        return self._routing_table.get(symbol.upper())

    def get_routing_table(self) -> Dict[str, str]:
        """Return copy of routing table. Used by WS worker for subscriptions."""
        self._ensure_markets()
        return dict(self._routing_table)

    def _tag_venue(self, df: pd.DataFrame, exchange_id: str) -> pd.DataFrame:
        """Tag a DataFrame with its data venue for downstream volume awareness."""
        if not df.empty:
            df.attrs['_data_venue'] = f'ccxt_{exchange_id}'
        return df

    def _call_venue(self, symbol: str, fetch_fn) -> pd.DataFrame:
        """Shared wrapper: rate-limit → breaker → fetch → record.

        `fetch_fn(exchange, pair, exchange_id)` runs the actual ccxt call and
        returns a list of OHLCV rows (or raises).
        """
        if not self.is_supported(symbol):
            return pd.DataFrame()

        from api_rate_limiter import can_call, record_call

        resolved = self._resolve_symbol(symbol)
        if not resolved:
            return pd.DataFrame()

        exchange, pair, exchange_id = resolved
        provider = f'ccxt_{exchange_id}'
        breaker = self._get_breaker(exchange_id)

        if breaker.is_open():
            logger.warning(f"CCXT/{exchange_id} circuit OPEN, skipping {symbol}")
            return pd.DataFrame()

        allowed, reason = can_call(provider)
        if not allowed:
            logger.warning(f"CCXT rate limit ({exchange_id}): {reason}")
            return pd.DataFrame()

        try:
            ohlcv = fetch_fn(exchange, pair, exchange_id)
            record_call(provider)
            breaker.reset()
            df = self._ccxt_to_df(ohlcv)
            return self._tag_venue(df, exchange_id)
        except Exception as e:
            breaker.record_failure()
            logger.warning(f"CCXT/{exchange_id} fetch failed for {symbol}: {e}")
            return pd.DataFrame()

    def fetch_ohlcv(self, symbol: str, days: int = 365) -> pd.DataFrame:
        """Fetch daily OHLCV. Returns empty DataFrame on failure."""
        since = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

        def _fetch(exchange, pair, exchange_id):
            ohlcv = self._fetch_paginated(exchange, pair, '1d', since)
            if ohlcv:
                logger.info(f"CCXT/{exchange_id}: fetched {len(ohlcv)} daily rows for {symbol}")
            return ohlcv

        return self._call_venue(symbol, _fetch)

    def fetch_klines(self, symbol: str, interval: str = '15m', bars: int = 2500) -> pd.DataFrame:
        """Fetch klines at any supported interval."""
        if interval not in self.SUPPORTED_INTERVALS:
            logger.debug(f"CCXT: unsupported interval {interval}")
            return pd.DataFrame()

        interval_ms = self._interval_to_ms(interval)
        since = int(datetime.now().timestamp() * 1000) - (interval_ms * bars)

        def _fetch(exchange, pair, exchange_id):
            ohlcv = self._fetch_paginated(exchange, pair, interval, since)
            if ohlcv:
                logger.info(f"CCXT/{exchange_id}: fetched {len(ohlcv)} {interval} rows for {symbol}")
            return ohlcv

        return self._call_venue(symbol, _fetch)

    def fetch_hourly_ohlcv(self, symbol: str, hours: int = 500) -> pd.DataFrame:
        """Fetch hourly OHLCV."""
        since = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)

        def _fetch(exchange, pair, exchange_id):
            ohlcv = self._fetch_paginated(exchange, pair, '1h', since)
            if ohlcv:
                logger.info(f"CCXT/{exchange_id}: fetched {len(ohlcv)} hourly rows for {symbol}")
            return ohlcv

        return self._call_venue(symbol, _fetch)

    def fetch_klines_for_range(self, symbol: str, interval: str,
                                start_ms: int, end_ms: int) -> pd.DataFrame:
        """Fetch klines for a specific time range (used by Volume Profile)."""
        if interval not in self.SUPPORTED_INTERVALS:
            return pd.DataFrame()

        def _fetch(exchange, pair, exchange_id):
            ohlcv = self._fetch_paginated(exchange, pair, interval, start_ms, end_ms)
            if ohlcv:
                logger.info(
                    f"CCXT/{exchange_id}: VP fetched {len(ohlcv)} {interval} klines for {symbol}"
                )
            return ohlcv

        return self._call_venue(symbol, _fetch)

    # ========================================
    # Internal helpers
    # ========================================

    def _fetch_paginated(self, exchange, pair: str, interval: str,
                          since: int, until: Optional[int] = None) -> list:
        """Fetch OHLCV with pagination. CCXT returns max ~1000 bars per call."""
        all_ohlcv = []
        limit = 1000
        current = since

        while True:
            data = exchange.fetch_ohlcv(pair, interval, since=current, limit=limit)
            if not data:
                break

            all_ohlcv.extend(data)
            last_ts = data[-1][0]
            interval_ms = self._interval_to_ms(interval)
            next_start = last_ts + interval_ms
            if next_start <= current:
                break
            current = next_start

            if until and current >= until:
                break
            if len(data) < limit:
                # A short page only means "reached the present"
                # when the last candle actually is the present. gate.io hands
                # back 999 rows for GT/USDT from 1500d back, ending long before
                # the present, and stopping there would freeze GT/LEO/PI/BGB at
                # that cutoff on every rebuild. Keep walking until the venue returns nothing
                # or the last bar is within one interval of now.
                now_ms = int(time.time() * 1000)
                if last_ts >= now_ms - 2 * interval_ms:
                    break

            time.sleep(0.05)

        if until:
            all_ohlcv = [c for c in all_ohlcv if c[0] <= until]

        return all_ohlcv

    @staticmethod
    def _ccxt_to_df(ohlcv: list) -> pd.DataFrame:
        """Convert CCXT OHLCV list to Carrac-shaped DataFrame.

        CCXT format: [[timestamp_ms, open, high, low, close, volume], ...]
        Carrac expects: DatetimeIndex + columns [open, high, low, close, volume].
        """
        if not ohlcv:
            return pd.DataFrame()

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df = df.astype(float)
        df = df[df['close'] > 0]
        df = df[~df.index.duplicated(keep='last')].sort_index()
        return df

    @staticmethod
    def _interval_to_ms(interval: str) -> int:
        """Convert interval string to milliseconds."""
        mapping = {
            '1m': 60_000, '3m': 180_000, '5m': 300_000,
            '15m': 900_000, '30m': 1_800_000,
            '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000,
            '8h': 28_800_000, '12h': 43_200_000, '1d': 86_400_000,
        }
        return mapping.get(interval, 86_400_000)
