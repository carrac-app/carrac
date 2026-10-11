"""
Market-data orchestrator (despite the file name, not a database layer).

Routes each symbol to the right source and merges the results into OHLCV
frames, with delta downloads (only data newer than the last stored bar):
- Stocks, ETFs, Indices, Forex: Stooq, falling back to Yahoo Finance
- Crypto: CoinPaprika for market cap data
- Crypto Metrics: Calculated from CoinPaprika global data (dominance, total market cap)
- OHLCV: Binance (primary) -> Hyperliquid -> CCXT
  (no CryptoCompare fallback at the end of this chain: it is barred on every
  commercial tier)

Delegates to focused data source clients in data_sources/ package.
"""

import pandas as pd
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import os
import json
import time
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

# Data source clients
from data_sources.circuit_breaker import CircuitBreaker
from data_sources.binance import BinanceClient
from data_sources.hyperliquid import HyperliquidClient
from data_sources.ccxt_client import CcxtClient
from data_sources.yahoo import YahooClient
from data_sources.stooq import StooqClient
from data_sources.coinpaprika import CryptoMetricsClient

# CSV caching
from csv_cache import CSVCache, MarketCapCache

# Import global cache manager for server-wide API caching
from cache_manager import (
    cache_manager, get_cached_crypto_prices, CryptoAPIFetcher,
    get_cached_ltf_data, set_cached_ltf_data,
    get_cached_top_coins, set_cached_top_coins,
    get_cached_market_caps, set_cached_market_caps,
)

# Import rate limiter with multi-tier helpers for safe API calls

# Configure logging
logger = logging.getLogger('carrac.database')


# Largest staleness a crypto cache may be topped up with a delta fetch. Beyond
# this the delta window can no longer reach back to the cached tail, so the
# series is rebuilt in full instead (see _get_crypto_ohlcv).
_MAX_CRYPTO_DELTA_DAYS = 30

# ── Retired symbols ───────────────────────────────────────────────────────────
# Declared by the maintainers: these are not instruments Carrac tracks, and every fetch for
# them is waste. Checked at the single funnel (get_ohlcv_data) rather than at a
# caller, because the caller is the part we could not find.
#
# GRAM — retired deliberately. It is NOT the crypto ticker
# of the same name: it resolves to GRAM.US / "Gold Flora Corporation", a defunct
# OTC penny stock with regularMarketPrice 0.0, pinned at $0.00001 with zero
# volume. It is absent from _tracked_symbols.json (352),
# db.get_all_symbols(), crypto_map, every user watchlist (43 symbols), and every
# .py/.js/.json in the repo — yet something requested it roughly every 16
# minutes. The cost was not only noise: each fetch spent a Stooq call (the log
# reads "Approaching limit (36/50)" then "Rate limit reached (51/50)" on the
# next cycle), so a stock quoted at zero was consuming budget the real
# Stooq-routed equities need. It also produced ~23 synthetic-bar guard refusals a
# day — 360 of the app container's 366 ERROR lines over one week.
#
# This set is deliberately small and explicit. The general mechanism is
# the instrument master's lifecycle status (a delisted symbol stops being
# collected); this is a stopgap for symbols the maintainers have
# named, not a substitute for either.
RETIRED_SYMBOLS = frozenset({'GRAM'})


# ── Equity-path eligibility ──────────────────────────────────────
# `<TICKER>.US` is the DEFAULT for anything get_ohlcv_data does not
# recognise (data_sources/stooq.py::get_stooq_symbol falls through to it). So a
# symbol whose provenance is CoinPaprika's top-coins list — a crypto market-cap
# ranking — would get resolved against a US EQUITY universe, and the result is real,
# current, well-formed data for a completely different instrument. Observed
# by byte-exact match against the Yahoo quote:
#
#     M    22.82   Macy's, Inc.              (not MemeCore)
#     SKY  90.25   Champion Homes, Inc.      (not Sky/Maker)
#     PI  165.54   Impinj, Inc.              (not Pi Network)
#     LEO   6.17   BNY Mellon Strategic Muni (not UNUS SED LEO)
#     BGB  11.27   Blackstone Strategic Cr.  (not Bitget Token)
#     RAIN  0.846  (not Rain)
#
# Every cache guard built so far asks "is this data healthy?" — none of them
# asks "is this the instrument it claims to be?", and healthy-but-wrong passes
# all of them. So the default itself is the bug: an unrecognised symbol must
# mean "I do not know this symbol", not "assume it is a US stock".
#
# The equity path is therefore allowed only for symbols we can name a reason
# for. Two reasons qualify:
#
#   1. CURATED — the symbol is in one of the five Stooq maps (stock, ETF,
#      commodity, index, forex). That is the equity universe Carrac tracks.
#   2. PROVIDER-FORMAT — the symbol is already written in a data-provider's
#      own notation and so cannot be a bare guess: `^GSPC` (index),
#      `EURUSD=X` / `GC=F` (Yahoo FX/futures), `005930.KS` / `RHM.DE`
#      (exchange-suffixed). These reach us from the symbol search, which
#      returns Yahoo tickers verbatim; `005930.KS` can be in a live
#      watchlist, so refusing it would break existing users.
#
# Anything else returns an EMPTY frame, which is an already-supported outcome
# (get_multi_timeframe_data checks df_base.empty). A gap is visible and
# recoverable; a chart of the wrong company is neither — the same trade
# Made for the ETF-scale prices.
#
# CONSEQUENCE, stated rather than discovered later: a bare US ticker the user
# added from search but that is NOT in stock_map/etf_map (say `NKE`) is now
# refused instead of silently resolved. That is the intended meaning of the
# rule, and _beacon_equity_path_refusal below is how we hear about it — the
# refusal is loud, per symbol, in the error tracker.
_PROVIDER_FORMAT_TICKER = re.compile(r'^\^|[.=]')

_EQUITY_REFUSAL_PREFIX = 'mcd:equityrefused:'
_EQUITY_REFUSAL_TTL = 12 * 60 * 60   # one beacon per symbol per 12h


def _is_provider_format_ticker(symbol: str) -> bool:
    """True for symbols already written in a provider's own notation."""
    return bool(_PROVIDER_FORMAT_TICKER.search(symbol or ''))


def _has_crypto_provenance(symbol: str) -> bool:
    """True if this symbol came from a crypto market-cap list.

    Read from the cached top-coins list rather than ``crypto_map``, because
    the whole point of asking is that ``crypto_map`` may not have been
    populated in THIS process yet — see the registration bug this guards
    against. The cache is cross-process (Redis), so it answers even when the
    in-process map is still the static base.
    """
    try:
        return any((c or {}).get('symbol', '').upper() == symbol.upper()
                   for c in (get_cached_top_coins() or []))
    except Exception:
        return False


def _beacon_equity_path_refusal(symbol: str, reason: str) -> None:
    """Record that a symbol was refused the equity path — LOUD only when the
    refusal is something a human should act on.

    Two very different things reach here, and beaconing both at ERROR would
    bury the one that matters:

    * A symbol with CRYPTO PROVENANCE that this process has not registered
      yet. Every fresh process is in that state until its first
      ``_get_top_coins`` call, so the gate correctly catches a handful of
      tickers on every start. That is defence-in-depth working, not a defect,
      and it recurs forever — measured on real data immediately after the fix
      shipped: 20 symbols, all crypto, every one expected. At ERROR that is
      20 error-tracker issues every 12 hours for a non-event, i.e. exactly the
      Pattern of a guard that trains you to ignore guards.

    * A symbol we genuinely do not recognise from anywhere. THAT is worth a
      human: either it is a real equity missing from stock_map/etf_map (and
      someone's chart is now empty), or something is asking for a ticker
      nobody can account for.

    Deduplicated in Redis either way, so a symbol requested every precompute
    cycle beacons twice a day rather than 140 times.
    """
    key = f'{_EQUITY_REFUSAL_PREFIX}{symbol.upper()}'
    r = _get_cc_redis()
    if r is not None:
        try:
            if not r.set(key, '1', ex=_EQUITY_REFUSAL_TTL, nx=True):
                return
        except Exception:
            pass
    if _has_crypto_provenance(symbol):
        logger.info(
            "equity-path refused: %s (%s) — has crypto provenance, "
            "so this is the gate catching a symbol before its crypto "
            "registration landed in this process. Expected; not an alarm.",
            symbol.upper(), reason,
        )
        return
    logger.error(
        "equity-path refused: %s (%s), and it is NOT in the crypto "
        "top-coins list either — Carrac cannot account for this symbol at all. "
        "Returning empty rather than charting <TICKER>.US, which would be an "
        "unrelated instrument. If %s IS a real equity, add it to "
        "StooqClient.stock_map/etf_map; if it is crypto, it needs a venue route.",
        symbol.upper(), reason, symbol.upper(),
    )


# ── CryptoCompare removal ─────────────────────────────────────────────────────────
# There is no CryptoCompare client or consumption-control machinery
# (fallback-symbol tracking, empty-result backoff, 4h CSV-freshness window):
# CryptoCompare is barred on every commercial tier. `_get_cc_redis` — despite
# the name — is a general-purpose Redis-singleton accessor reused by the
# gap-repair, rebuild-backoff and fetch-slot helpers further down this file.


def _get_cc_redis():
    """Redis singleton shared by this module's Redis-backed helpers (best-effort).

    Named for its original CryptoCompare-only caller (removed);
    kept as-is because gap-repair/rebuild-backoff/fetch-slot helpers below
    all depend on it now.
    """
    try:
        from carrac.redis_cache import get_redis
        return get_redis()
    except Exception:
        return None


# Full-history rebuild backoff. A symbol whose tail is permanently stale
# (delisted/rebranded — KAS, CHILLGUY, ...) fails its rebuild every time, and
# without this every single request would re-attempt a 1500-day upstream fetch.
# Positive symbol validations. 24h — symbol validity changes on
# listing/delisting timescales, and a stale positive only means one avoidable
# data fetch that then 404s; a cached NEGATIVE would be a real bug (see
# validate_symbol), so only positives ever land under this prefix.
_SYMVAL_CACHE_PREFIX = 'mcd:symval:'
_SYMVAL_CACHE_TTL = 24 * 60 * 60

_REBUILD_BACKOFF_PREFIX = 'mcd:rebuild:failed:'
_REBUILD_BACKOFF_TTL = 6 * 60 * 60   # 6h — dead pairs do not come back quickly


def _rebuild_backoff_key(symbol: str) -> str:
    return f"{_REBUILD_BACKOFF_PREFIX}{symbol.upper()}"


# Single-flight for expensive upstream fetches. Web (4 gunicorn workers),
# carrac-worker and cron `docker exec` jobs all share ./data, so the same cold
# or stale symbol can be fetched by several processes at once. Without it
# the same 1500-day Binance request can run several times in a couple of
# hours. The lock is cross-process (Redis) and FAIL-OPEN — if Redis is
# unavailable we fetch rather than serve nothing.
_FETCH_SLOT_PREFIX = 'mcd:inflight:'
_FETCH_SLOT_TTL = 300          # generous: a 1500-day paginated fetch is slow
_FETCH_SLOT_WAIT = 20          # how long a loser waits for the winner
_FETCH_SLOT_POLL = 0.5


def _fetch_slot_key(symbol: str, scope: str) -> str:
    return f"{_FETCH_SLOT_PREFIX}{scope}:{symbol.upper()}"


def _try_acquire_fetch_slot(symbol: str, scope: str = 'rebuild') -> bool:
    """True if we may perform the fetch. True (fail-open) when Redis is down."""
    r = _get_cc_redis()
    if not r:
        return True
    try:
        return bool(r.set(_fetch_slot_key(symbol, scope), b'1',
                          ex=_FETCH_SLOT_TTL, nx=True))
    except Exception:
        return True


def _release_fetch_slot(symbol: str, scope: str = 'rebuild') -> None:
    r = _get_cc_redis()
    if not r:
        return
    try:
        r.delete(_fetch_slot_key(symbol, scope))
    except Exception:
        pass


def _await_fetch_slot(symbol: str, scope: str = 'rebuild',
                      max_wait: Optional[int] = None) -> bool:
    """Block until the in-flight fetch clears. True if it cleared in time.

    Losers do not queue up behind the winner to run the same request; they
    wait, then re-read the cache the winner just wrote.

    ``max_wait`` is resolved at call time, not bound as a default, so the
    module constant stays overridable.
    """
    if max_wait is None:
        max_wait = _FETCH_SLOT_WAIT
    r = _get_cc_redis()
    if not r:
        return False
    key = _fetch_slot_key(symbol, scope)
    waited = 0.0
    while waited < max_wait:
        try:
            if not r.get(key):
                return True
        except Exception:
            return False
        time.sleep(_FETCH_SLOT_POLL)
        waited += _FETCH_SLOT_POLL
    return False


# Interior-gap repair. The non-crypto freshness check below reads
# ONLY cached_df.index[-1], so a fresh tail hides a hole anywhere behind it —
# forever, because the check never looks further back. Commodity/FX caches
# missing one interior bar made the free public brief publish "Gold surged
# +5.6% today" off a bar pair spanning two days, when gold had moved +0.72%. csv_cache.trades_weekends and scripts/cache_integrity_check.py
# both describe this exact failure mode; neither changed the check.
#
# One repair attempt per symbol per ~day. Stooq is capped near 50 requests/day
# (observed at 44/50 mid-afternoon), so an unbounded "hole -> refetch" would
# exhaust the budget the first time a market holiday made every symbol look
# gappy at once, and then no symbol could refresh at all.
_GAP_REPAIR_PREFIX = 'mcd:gap:tried:'
_GAP_REPAIR_TTL = 20 * 60 * 60   # ~20h — at most one repair per symbol per day
_GAP_REPAIR_RETRY_TTL = 30 * 60  # 30m — the fetch never reached upstream; retry sooner
_GAP_LOOKBACK = 8                # recent bars inspected; older holes are history


def _gap_repair_key(symbol: str) -> str:
    return f"{_GAP_REPAIR_PREFIX}{symbol.upper()}"


def _is_gap_repair_backed_off(symbol: str) -> bool:
    """True when a gap repair for *symbol* was already attempted recently."""
    r = _get_cc_redis()
    if r:
        try:
            return bool(r.get(_gap_repair_key(symbol)))
        except Exception:
            pass
    return False


def _set_gap_repair_attempted(symbol: str) -> None:
    """Mark a repair attempt; expires after _GAP_REPAIR_TTL.

    Set BEFORE the fetch, not after: a holiday looks identical to a real hole
    without a trading calendar, so an attempt that legitimately fails to fill
    the hole must still count. Otherwise every holiday would refetch every
    affected symbol on every request until the next session.
    """
    r = _get_cc_redis()
    if r:
        try:
            r.set(_gap_repair_key(symbol), b'1', ex=_GAP_REPAIR_TTL)
        except Exception:
            pass


def _record_gap_repair_outcome(symbol: str, outcome: str) -> None:
    """Record what a repair attempt actually achieved, and re-time the backoff.

    Two failure modes this must handle: FX pairs staying gapped for a full
    day while the repair ran even though upstream had the missing bar, and
    nothing saying so.

    1. A repair whose fetch came back EMPTY achieved nothing, yet burned the
       whole 20h budget. Empty is not "upstream has no such bar" — it is the
       `_yahoo_fallback` breaker-open / terminal-failure path in
       data_sources/stooq.py, i.e. we never got an answer at all. Those retry
       on a short TTL instead. `unfilled` keeps the full TTL: upstream WAS
       reached and genuinely does not have the bar (a public holiday, most
       often), and hammering it changes nothing.

       The attempt is still marked BEFORE the fetch, so a crash mid-fetch
       still counts and the Stooq budget stays bounded. This only ever
       SHORTENS the wait after a failure we can identify.

    2. The only signal was a stdout warning, and `make promote` recreates the
       container, so earlier repairs become unreadable within hours.
       Outcomes go to Redis with a 7-day TTL so they outlive a deploy.
    """
    r = _get_cc_redis()
    if not r:
        return
    try:
        r.setex(f'mcd:gap:outcome:{symbol.upper()}', 7 * 24 * 3600, outcome)
        r.hincrby('mcd:gap:outcomes', outcome, 1)
        if outcome == 'nofetch':
            # Never reached upstream — do not spend the day on it.
            r.set(_gap_repair_key(symbol), b'1', ex=_GAP_REPAIR_RETRY_TTL)
    except Exception:
        pass


def _missing_business_days(df, lookback: int = _GAP_LOOKBACK) -> int:
    """Business days absent between consecutive bars in the recent window.

    Weekends are free: ``pd.bdate_range`` skips them, so Fri -> Mon scores 0.
    Public holidays DO score, because recognising them needs an exchange
    calendar this codebase does not have — deliberately accepted, since the
    cost of a false positive is one extra fetch per symbol per day (bounded by
    _GAP_REPAIR_TTL) while the cost of a false negative is a fabricated daily
    move on the public page.

    Returns 0 on any malformed input — this must never break a price lookup.
    """
    try:
        if df is None or len(df) < 2:
            return 0
        idx = df.index[-(lookback + 1):]
        missing = 0
        for a, b in zip(idx, idx[1:]):
            span = len(pd.bdate_range(a, b))
            if span > 2:              # both endpoints plus something in between
                missing += span - 2
        return missing
    except Exception:
        return 0


def _is_rebuild_backed_off(symbol: str) -> bool:
    """True when a recent full rebuild for *symbol* came back empty."""
    r = _get_cc_redis()
    if r:
        try:
            return bool(r.get(_rebuild_backoff_key(symbol)))
        except Exception:
            pass
    return False


def _set_rebuild_backoff(symbol: str) -> None:
    """Mark a failed rebuild; expires after _REBUILD_BACKOFF_TTL."""
    r = _get_cc_redis()
    if r:
        try:
            r.set(_rebuild_backoff_key(symbol), b'1', ex=_REBUILD_BACKOFF_TTL)
        except Exception:
            pass


try:
    import yfinance as yf  # noqa: F401 — availability probe only
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False


class OHLCVDatabase:
    """Database connector for crypto, stocks, ETFs, forex, indices OHLCV data.

    Orchestrates data source clients, handles caching and fallback logic.
    """

    def __init__(self, **kwargs):
        """Initialize data connector with all source clients."""

        # ========================================
        # CSV Cache
        # ========================================
        cache_dir = os.environ.get('DATA_DIR', 'data')
        self.cache = CSVCache(cache_dir)
        self.cache_dir = self.cache.cache_dir  # backward compat
        self.market_cap_cache = MarketCapCache(cache_dir)

        # ========================================
        # Circuit Breakers
        # ========================================
        # Daily bars try stooq first and fall back to Yahoo. stooq is often
        # unreachable (blocked, firewalled, or timing out on connect), so the
        # breaker trips after a SINGLE failure and stays open for 30 min:
        # stock fetches then skip stooq and go straight to Yahoo instead of
        # each stale-cache refresh blocking a worker on connect-timeouts.
        # notify=False: the stooq block is chronic + Yahoo-covered;
        # per-process hourly ntfy pushes would be pure noise. The real alert — stooq AND Yahoo both down — fires from
        # StooqClient._yahoo_fallback instead.
        self._stooq_breaker = CircuitBreaker('stooq', threshold=1, half_open_after=1800,
                                             notify=False)
        self._yahoo_breaker = CircuitBreaker('yfinance')
        self._yahoo_hourly_breaker = CircuitBreaker('yfinance_hourly')

        # ========================================
        # Data Source Clients
        # ========================================
        self.binance = BinanceClient()
        self.hyperliquid = HyperliquidClient()
        self.ccxt = CcxtClient()
        self.yahoo = YahooClient(self._yahoo_breaker, self._yahoo_hourly_breaker)
        self.stooq = StooqClient(self._stooq_breaker, self.yahoo)
        self.crypto_metrics = CryptoMetricsClient()

        # Symbols where the last realtime-price synthetic-candle injection
        # was rejected as implausible (>50% off recent history — a zombie/
        # delisted ticker). Cleared automatically once a sane price returns.
        self.price_injection_rejects: set = set()

        # ========================================
        # Symbol Maps (for routing)
        # ========================================

        # Backward-compat references to client-owned maps
        self.stock_map = self.stooq.stock_map
        self.etf_map = self.stooq.etf_map
        self.commodity_map = self.stooq.commodity_map
        self.index_map = self.stooq.index_map
        self.forex_map = self.stooq.forex_map
        self.YAHOO_SYMBOL_MAP = self.yahoo.SYMBOL_MAP
        self.binance_map = self.binance.spot_map
        self.binance_reverse_map = self.binance.reverse_map
        self.BINANCE_SYMBOL_MAP = self.binance.SYMBOL_MAP
        self.BINANCE_UNAVAILABLE = self.binance.UNAVAILABLE
        self.BINANCE_PERP_MAP = self.binance.PERP_MAP

        # Crypto symbols - Expanded list
        self.crypto_map = {
            # Major coins
            'BTC': 'BTC', 'ETH': 'ETH', 'SOL': 'SOL', 'ADA': 'ADA',
            'XRP': 'XRP', 'LTC': 'LTC', 'DOGE': 'DOGE', 'DOT': 'DOT',
            'AVAX': 'AVAX', 'LINK': 'LINK', 'POL': 'POL', 'UNI': 'UNI',
            'ATOM': 'ATOM', 'XLM': 'XLM', 'ALGO': 'ALGO', 'VET': 'VET',
            'FIL': 'FIL', 'AAVE': 'AAVE', 'EOS': 'EOS', 'XTZ': 'XTZ',
            'BNB': 'BNB', 'XMR': 'XMR', 'TRX': 'TRX', 'SHIB': 'SHIB',
            'NEAR': 'NEAR', 'SUI': 'SUI', 'APT': 'APT',
            # Layer 2s
            'ARB': 'ARB', 'OP': 'OP', 'IMX': 'IMX', 'STRK': 'STRK',
            'MANTA': 'MANTA', 'ZK': 'ZK', 'METIS': 'METIS', 'LRC': 'LRC',
            'SKL': 'SKL',
            # AI & Computing
            'FET': 'FET', 'RENDER': 'RENDER', 'TAO': 'TAO',
            'OCEAN': 'OCEAN', 'AKT': 'AKT', 'AR': 'AR',
            'IO': 'IO', 'AI': 'AI', 'CGPT': 'CGPT',
            # DeFi
            'CRV': 'CRV', 'SNX': 'SNX', 'COMP': 'COMP', 'LDO': 'LDO',
            'MKR': 'MKR', 'RUNE': 'RUNE', '1INCH': '1INCH', 'GMX': 'GMX',
            'PENDLE': 'PENDLE', 'JUP': 'JUP', 'ENA': 'ENA',
            'RPL': 'RPL', 'COW': 'COW', 'MORPHO': 'MORPHO',
            'DODO': 'DODO', 'USUAL': 'USUAL', 'FRAX': 'FRAX',
            # Gaming & Metaverse
            'GALA': 'GALA', 'SAND': 'SAND', 'MANA': 'MANA', 'AXS': 'AXS',
            'ENJ': 'ENJ', 'PRIME': 'PRIME', 'BEAM': 'BEAM',
            'YGG': 'YGG', 'SUPER': 'SUPER', 'GMT': 'GMT', 'ALICE': 'ALICE',
            'RONIN': 'RONIN',
            # Infrastructure
            'ICP': 'ICP', 'INJ': 'INJ', 'HBAR': 'HBAR', 'FLOW': 'FLOW',
            'EGLD': 'EGLD', 'SEI': 'SEI', 'TIA': 'TIA',
            'STX': 'STX', 'THETA': 'THETA', 'QNT': 'QNT',
            'IOTA': 'IOTA', 'KSM': 'KSM', 'ZIL': 'ZIL', 'MINA': 'MINA',
            'CKB': 'CKB', 'ONE': 'ONE', 'CELO': 'CELO', 'ZEN': 'ZEN',
            'ANKR': 'ANKR', 'ICX': 'ICX', 'ASTR': 'ASTR', 'AXL': 'AXL',
            'SC': 'SC', 'STORJ': 'STORJ', 'GLM': 'GLM', 'FLUX': 'FLUX',
            'RVN': 'RVN',
            # Memecoins
            'PEPE': 'PEPE', 'BONK': 'BONK', 'WIF': 'WIF', 'FLOKI': 'FLOKI',
            'TURBO': 'TURBO', 'BRETT': 'BRETT',
            'BOME': 'BOME', 'PEOPLE': 'PEOPLE', 'HMSTR': 'HMSTR',
            'LUNC': 'LUNC',
            # Other notable
            'TON': 'TON', 'ONDO': 'ONDO', 'PYTH': 'PYTH', 'JTO': 'JTO',
            'W': 'W', 'RNDR': 'RNDR',
            'BAND': 'BAND', 'NMR': 'NMR', 'BAT': 'BAT', 'DASH': 'DASH',
            'DCR': 'DCR', 'PAXG': 'PAXG', 'NEXO': 'NEXO', 'FTT': 'FTT',
            # Solana ecosystem
            'ORCA': 'ORCA', 'KMNO': 'KMNO', 'TNSR': 'TNSR', 'FIDA': 'FIDA',
            # Major additions
            'BCH': 'BCH', 'ETC': 'ETC', 'NEO': 'NEO', 'ZEC': 'ZEC',
            # DeFi (extended)
            'DYDX': 'DYDX', 'ENS': 'ENS', 'GRT': 'GRT', 'SUSHI': 'SUSHI',
            'CAKE': 'CAKE', 'LQTY': 'LQTY', 'LISTA': 'LISTA', 'RDNT': 'RDNT',
            'RSR': 'RSR',
            # Gaming & Metaverse (extended)
            'APE': 'APE', 'PIXEL': 'PIXEL', 'PORTAL': 'PORTAL',
            'BIGTIME': 'BIGTIME', 'MAGIC': 'MAGIC', 'ILV': 'ILV',
            # AI (extended)
            'AIXBT': 'AIXBT', 'WLD': 'WLD', 'ARKM': 'ARKM',
            # Infrastructure (extended)
            'JASMY': 'JASMY', 'CHZ': 'CHZ', 'ROSE': 'ROSE', 'CFX': 'CFX',
            'KAVA': 'KAVA', 'OSMO': 'OSMO', 'LUNA': 'LUNA',
            'ZRO': 'ZRO', 'SAGA': 'SAGA', 'DYM': 'DYM', 'NTRN': 'NTRN',
            'RAY': 'RAY', 'ACH': 'ACH', 'BLUR': 'BLUR',
            'SSV': 'SSV', 'ETHFI': 'ETHFI', 'MOVE': 'MOVE',
            # New L1/L2
            'BERA': 'BERA', 'KAITO': 'KAITO',
            'LAYER': 'LAYER', 'HYPER': 'HYPER', 'INIT': 'INIT',
            'PLUME': 'PLUME', 'FORM': 'FORM',
            # Memecoins (extended)
            'ORDI': 'ORDI', 'NOT': 'NOT', 'DOGS': 'DOGS', 'NEIRO': 'NEIRO',
            'PNUT': 'PNUT', 'TRUMP': 'TRUMP', 'VIRTUAL': 'VIRTUAL',
            'ANIME': 'ANIME',
            # Narrative plays
            'EIGEN': 'EIGEN',
            'SHELL': 'SHELL', 'KERNEL': 'KERNEL', 'SIGN': 'SIGN',
            'HUMA': 'HUMA', 'WAL': 'WAL',
            # Your custom additions
            'GRASS': 'GRASS', 'COTI': 'COTI', 'BIO': 'BIO',
            'PENGU': 'PENGU', 'DEEP': 'DEEP', 'CETUS': 'CETUS', 'ARC': 'ARC',
            'PIPPIN': 'PIPPIN', 'FARTCOIN': 'FARTCOIN', 'CHEX': 'CHEX',
            'GRIFFAIN': 'GRIFFAIN', 'COLLAT': 'COLLAT', 'KIP': 'KIP',
            'MLC': 'MLC', 'DSYNC': 'DSYNC', 'BUTTHOLE': 'BUTTHOLE', 'S': 'S',
            'CC': 'CC',
            # Top-100 tokens that fall through to Yahoo without this
            'HYPE': 'HYPE', 'WBT': 'WBT',
            # Hyperliquid-only / HL-first listings (served via HyperliquidClient fallback).
            # These return empty from BinanceClient and route to HL via _fetch_crypto_data.
            # Keep this list in sync with HL_COINS in data_sources/hyperliquid.py.
            # KPEPE/KBONK stored uppercase here; HyperliquidClient translates to
            # the case-sensitive 'kPEPE'/'kBONK' at the API boundary.
            'MON': 'MON', 'LIT': 'LIT', 'XPL': 'XPL', 'PUMP': 'PUMP',
            'KPEPE': 'KPEPE', 'KBONK': 'KBONK',
            'WLFI': 'WLFI', 'VVV': 'VVV', 'ASTER': 'ASTER',
            'CHILLGUY': 'CHILLGUY', 'AERO': 'AERO',
            'PURR': 'PURR',
        }

        # Crypto blacklist
        self.crypto_blacklist = self.crypto_metrics.CRYPTO_BLACKLIST

        # Special crypto metrics (derived / calculated)
        self.crypto_metric_map = {
            'BTC.D': 'btc_dominance',
            'USDT.D': 'usdt_dominance',
            'TOTAL': 'total_market_cap',
            'TOTAL2': 'total2_market_cap',
            'TOTAL3': 'total3_market_cap',
            'OTHERS.D': 'others_dominance',
            'ETHBTC': 'ethbtc_ratio',
            'XRPBTC': 'xrpbtc_ratio',
            'AVAXBTC': 'avaxbtc_ratio',
            'TOTAL3BTC': 'total3btc_ratio',
            'ALTBTC': 'altbtc_ratio',
        }

        # Realtime price cache (60 second TTL)
        self._realtime_cache: Dict[str, Any] = {}
        self._realtime_cache_time: Optional[datetime] = None
        self.REALTIME_CACHE_TTL = 60

        # Binance 24hr ticker cache (30 second TTL)
        self._binance_cache: Dict[str, Any] = {}
        self._binance_cache_time: Optional[datetime] = None
        self.BINANCE_CACHE_TTL = 30

    # ========================================
    # Asset Type Routing
    # ========================================

    def _get_asset_type(self, symbol: str) -> str:
        """Get the type of asset."""
        symbol_upper = symbol.upper()
        if symbol_upper in self.crypto_map:
            return 'crypto'
        if symbol_upper in self.crypto_metric_map:
            return 'crypto_metric'
        if symbol_upper in self.stock_map:
            return 'stock'
        if symbol_upper in self.etf_map:
            return 'etf'
        if symbol_upper in self.commodity_map:
            return 'commodity'
        if symbol_upper in self.index_map:
            return 'index'
        if symbol_upper in self.forex_map:
            return 'forex'
        return 'stock'

    def _is_crypto(self, symbol: str) -> bool:
        return symbol.upper() in self.crypto_map

    def _is_crypto_metric(self, symbol: str) -> bool:
        return symbol.upper() in self.crypto_metric_map

    def _equity_path_eligible(self, symbol: str) -> tuple:
        """May *symbol* be resolved against the equity universe? (ok, reason)

        Lives beside the other routing predicates because it is
        one: it answers "is this an equity?" the same way _is_crypto answers
        "is this a crypto?" — by explicit membership, never by fall-through.
        """
        symbol_upper = symbol.upper()
        for name, mapping in (('stock_map', self.stock_map),
                              ('etf_map', self.etf_map),
                              ('commodity_map', self.commodity_map),
                              ('index_map', self.index_map),
                              ('forex_map', self.forex_map)):
            if symbol_upper in mapping:
                return True, name
        if _is_provider_format_ticker(symbol):
            return True, 'provider-format'
        return False, 'unknown-symbol'

    def _get_all_symbols_set(self) -> set:
        return (set(self.crypto_map.keys()) | set(self.crypto_metric_map.keys()) |
                set(self.stock_map.keys()) | set(self.etf_map.keys()) |
                set(self.commodity_map.keys()) | set(self.index_map.keys()) |
                set(self.forex_map.keys()))

    def is_curated(self, symbol: str) -> bool:
        return symbol.upper() in self._get_all_symbols_set()

    # ========================================
    # Dynamic Symbol Resolution (Tier 2)
    # ========================================

    def validate_symbol(self, symbol: str) -> Optional[dict]:
        """Validate a symbol against curated list, Binance exchangeInfo, or Yahoo Finance.

        Positive results are cached in Redis for 24h. Every branch
        below the curated check can involve a live upstream call, and the Yahoo
        one shares its rate budget with the post-restart warmup — for ~140s
        after a deploy that budget sits exhausted and an uncached validation
        404s a perfectly valid symbol. A None is deliberately NEVER cached:
        a rate-limited denial is indistinguishable from an unknown symbol, and
        replaying it would 404 a valid symbol for the whole TTL.
        """
        symbol_upper = symbol.upper()

        # Check curated first (Tier 1) — in-process, needs no cache
        if self.is_curated(symbol_upper):
            info = self.get_asset_info(symbol_upper)
            return {'symbol': symbol_upper, 'name': info.get('name', symbol_upper),
                    'type': self._get_asset_type(symbol_upper), 'source': 'curated',
                    'tier': 'curated'}

        r = _get_cc_redis()
        cache_key = f'{_SYMVAL_CACHE_PREFIX}{symbol_upper}'
        if r is not None:
            try:
                cached = r.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception:
                pass

        result = None

        # Try Binance exchangeInfo (dynamic crypto)
        exchange_info = self.binance.get_exchange_info()
        if symbol_upper in exchange_info:
            result = {'symbol': symbol_upper, 'name': symbol_upper,
                      'type': 'crypto', 'source': 'binance', 'tier': 'dynamic'}

        # Try CCXT exchanges (MEXC, Gate.io, KuCoin, Bybit — auto-discovered)
        if result is None and self.ccxt.is_supported(symbol_upper):
            venue = self.ccxt.get_venue_for_symbol(symbol_upper)
            result = {'symbol': symbol_upper, 'name': symbol_upper,
                      'type': 'crypto', 'source': f'ccxt_{venue}' if venue else 'ccxt',
                      'tier': 'dynamic'}

        # Try Yahoo Finance validation
        if result is None:
            result = self.yahoo.validate_symbol(symbol_upper)

        if result is not None and r is not None:
            try:
                r.setex(cache_key, _SYMVAL_CACHE_TTL, json.dumps(result))
            except Exception:
                pass

        return result

    def get_dynamic_ohlcv(self, symbol: str, days: int = 730, timeframe: str = 'daily') -> pd.DataFrame:
        """Fetch OHLCV for a dynamic (non-curated) symbol with 5-minute cache."""
        symbol_upper = symbol.upper()

        # Check 5-minute cache
        cached = self.cache.load_dynamic(symbol_upper, timeframe)
        if cached is not None:
            return cached

        # Determine source and fetch
        exchange_info = self.binance.get_exchange_info()
        df = pd.DataFrame()

        if symbol_upper in exchange_info:
            df = self.binance.fetch_ohlcv(symbol_upper, days=days)
        elif self.ccxt.is_supported(symbol_upper):
            df = self.ccxt.fetch_ohlcv(symbol_upper, days=days)
        else:
            # Try Yahoo Finance
            yahoo_sym = self.yahoo.resolve_symbol(symbol_upper)
            try:
                import requests
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
                params = {'interval': '1d', 'range': '2y'}
                response = requests.get(url, params=params, headers=headers, timeout=15)
                if response.ok:
                    data = response.json()
                    result = data.get('chart', {}).get('result', [])
                    if result:
                        timestamps = result[0].get('timestamp', [])
                        indicators = result[0].get('indicators', {}).get('quote', [{}])[0]
                        if timestamps and indicators:
                            df = pd.DataFrame({
                                'open': indicators.get('open', []),
                                'high': indicators.get('high', []),
                                'low': indicators.get('low', []),
                                'close': indicators.get('close', []),
                                'volume': indicators.get('volume', []),
                            }, index=pd.to_datetime(timestamps, unit='s'))
                            df.index.name = 'timestamp'
                            df = df.dropna(subset=['close'])
                            df = df[df['close'] > 0]
                            df = df[~df.index.duplicated(keep='last')].sort_index()
            except Exception as e:
                logger.warning(f"Yahoo fetch failed for dynamic {symbol_upper}: {e}")

        # Cache result
        if not df.empty:
            self.cache.save_dynamic(symbol_upper, df, timeframe)

        return df

    # ========================================
    # CSV Cache (delegated)
    # ========================================

    def _load_cached_data(self, symbol, timeframe='daily'):
        return self.cache.load(symbol, timeframe)

    def _save_to_cache(self, symbol, df, timeframe='daily'):
        # Sole caller is backfill.py, which is crypto-only (see its module
        # docstring: "extends crypto OHLCV CSV caches backward in time").
        # Weekend bars are legitimate there, so exempt from the
        # weekend-bar breadcrumb the same way the main crypto paths are.
        self.cache.save(symbol, df, timeframe, is_crypto=True)

    # ========================================
    # Top Coins (delegated)
    # ========================================

    def _get_top_coins(self, limit=70):
        return self.crypto_metrics.get_top_coins(limit, crypto_map=self.crypto_map)

    # ========================================
    # Crypto OHLCV (Hyperliquid-owned coins: Hyperliquid; others: Binance -> CCXT)
    # ========================================

    def fetch_binance_klines_for_range(self, symbol, interval, start_ms, end_ms):
        return self.binance.fetch_klines_for_range(symbol, interval, start_ms, end_ms)

    def fetch_klines_for_range(self, symbol, interval, start_ms, end_ms):
        """Venue-aware kline range fetcher: Hyperliquid for its own coins, else Binance → CCXT."""
        if self.hyperliquid.is_supported(symbol):
            return self.hyperliquid.fetch_klines_for_range(symbol, interval, start_ms, end_ms)
        df = self.binance.fetch_klines_for_range(symbol, interval, start_ms, end_ms)
        if not df.empty:
            return df
        if self.ccxt.is_supported(symbol):
            return self.ccxt.fetch_klines_for_range(symbol, interval, start_ms, end_ms)
        return df

    # ========================================
    # Hyperliquid (delegated)
    # ========================================

    def fetch_klines_for_chart(self, symbol, interval='15m', bars=2500):
        """Venue-aware kline fetcher: Hyperliquid for its own coins, else Binance → CCXT.

        Used by chart endpoints so venue-specific coins render correctly
        without callers needing to know which venue serves which symbol.
        """
        if self.hyperliquid.is_supported(symbol):
            return self.hyperliquid.fetch_klines(symbol, interval, bars)
        df = self.binance.fetch_klines(symbol, interval, bars)
        if not df.empty:
            return df
        if self.ccxt.is_supported(symbol):
            return self.ccxt.fetch_klines(symbol, interval, bars)
        return df  # empty

    def _fetch_crypto_data(self, symbol: str, days: int = 365) -> pd.DataFrame:
        """Fetch crypto OHLCV: Binance → Hyperliquid → CCXT.

        Binance is preferred when available (deeper history, aggregate CEX volume).
        Hyperliquid handles HIP-3 listings + HL-exclusive coins (FARTCOIN, HYPE, etc.),
        and those coins skip Binance entirely: a few of those tickers exist on
        Binance as a different, delisted asset (LIT), whose stale series must
        never stand in for the Hyperliquid coin.
        CCXT handles MEXC/Gate.io/KuCoin/Bybit coins (XMR, AKT, DEEP, etc.).

        There is no CryptoCompare last-resort fallback after CCXT (barred on
        every commercial tier). A symbol none of Binance/HL/CCXT serves
        degrades explicitly (loud log, empty DataFrame) instead of reaching for
        a barred vendor. Symbols such as AKT, ARC, BEAM, BUTTHOLE, CC, CHEX,
        DEEP, DSYNC, KIP, MLC, PIPPIN, PRIME, RDNT, XMR have a live CCXT route
        and fall through cleanly. COLLAT does not — COLLATUSDT is delisted and
        has no source anywhere; it needs a curated delist decision, not a
        fetch fix.
        """
        if self.hyperliquid.is_supported(symbol):
            df = self.hyperliquid.fetch_ohlcv(symbol, days)
            if not df.empty:
                return df
        else:
            df = self.binance.fetch_ohlcv(symbol, days)
            if not df.empty:
                return df
        if self.ccxt.is_supported(symbol):
            df = self.ccxt.fetch_ohlcv(symbol, days)
            if not df.empty:
                return df
        logger.warning(
            f"No daily OHLCV source for {symbol}: Binance/Hyperliquid/CCXT "
            f"all miss — returning empty"
        )
        return pd.DataFrame()

    def _fetch_hourly_data(self, symbol: str, hours: int = 500) -> pd.DataFrame:
        """Fetch hourly OHLCV: Binance → Hyperliquid → CCXT.

        Same chain and same empty-result behaviour as _fetch_crypto_data.
        """
        if self.hyperliquid.is_supported(symbol):
            df = self.hyperliquid.fetch_hourly_ohlcv(symbol, hours)
            if not df.empty:
                return df
        else:
            df = self.binance.fetch_hourly_ohlcv(symbol, hours)
            if not df.empty:
                return df
        if self.ccxt.is_supported(symbol):
            df = self.ccxt.fetch_hourly_ohlcv(symbol, hours)
            if not df.empty:
                return df
        logger.warning(
            f"No hourly OHLCV source for {symbol}: Binance/Hyperliquid/CCXT "
            f"all miss — returning empty"
        )
        return pd.DataFrame()

    # ========================================
    # Market Cap Data
    # ========================================

    # Cache TTL constants
    LTF_CACHE_TTL = 2400  # 40 min — must survive across precompute cycles
    MARKET_CAP_CACHE_TTL_HOURS = 6
    MARKET_CAP_CACHE_FILE = 'market_caps_cache.json'

    def _restore_market_cap_df_from_cache(self, data):
        df = MarketCapCache.restore_dataframe(data)
        top_coins = data.get('top_coins', [])
        if top_coins:
            set_cached_top_coins(top_coins)
        return df

    def _apply_constituent_df(self, market_caps: dict, symbol: str, coin: dict,
                               df: pd.DataFrame) -> None:
        """Fold one constituent's OHLCV into the running market_caps dict (in-place)."""
        if df is not None and not df.empty:
            # Normalise onto a midnight daily grid so a
            # constituent whose cache carries intraday / timezone-stamped
            # timestamps aligns onto the shared dates instead of scattering
            # phantom union rows (where the deep constituents are absent and
            # the summed series collapses toward ~0).
            if len(df.index):
                df = df.copy()
                df.index = pd.DatetimeIndex(df.index).normalize()
                df = df[~df.index.duplicated(keep='last')]
            current_mcap = coin['market_cap']
            current_price = coin['price']
            if current_price > 0:
                supply = current_mcap / current_price
                market_caps[symbol] = df['close'] * supply
            logger.debug(f"  {symbol}: OK ({len(df)} days)")
        else:
            logger.debug(f"  {symbol}: no data")

    # Coverage-gate tunables.
    #   RATIO: a constituent must hold at least this fraction of the days the
    #          best-covered constituent has within the recent window.
    #   END_TOLERANCE: its latest observation must reach within this many days
    #          of ref_end (catches deep-history-but-delisted "zombie" pairs).
    #   VALUE_SANITY_MAX_RATIO: a constituent's largest recent scaled cap may
    #          not exceed this multiple of its live snapshot market cap
    #          (catches redenominated/rebranded pairs whose historical closes
    #          are on a different scale — SKY/PI/RAIN class). Very generous so
    #          only physically-impossible-as-a-price-move spikes are dropped.
    COVERAGE_WINDOW_MIN_RATIO = 0.95
    COVERAGE_END_TOLERANCE_DAYS = 2
    VALUE_SANITY_MAX_RATIO = 100.0

    @staticmethod
    def _constituent_window(df: pd.DataFrame, days: Optional[int]) -> pd.DataFrame:
        """Last ``days`` CALENDAR DAYS of a constituent, not its last N rows.

        ``df.tail(days)`` counts rows. A constituent with an interior hole has
        fewer rows than days, so its tail reaches back *past* the hole — and
        every date it carries is unioned into the summed market-cap frame, so
        the derived indices (TOTAL, TOTAL2, TOTAL3, BTC.D, USDT.D, OTHERS.D)
        inherit a gap they never had themselves.

        Example: constituents with 532-day holes had their ``.tail(365)`` start
        in the far past, and several together put a 270-day orphan block into
        all six dominance series — a 702-day gap in every one of them, from a
        single upstream defect.

        A constituent with nothing inside the window returns empty and is
        dropped by the caller rather than contributing stale rows. This is
        complementary to _gate_constituent_coverage, which polices reliability
        *within* the recent window; this bounds the window itself.
        """
        if df is None or df.empty:
            return df
        if not days:
            return df
        cutoff = pd.Timestamp(datetime.now().date()) - pd.Timedelta(days=days)
        return df[df.index >= cutoff]

    def _gate_constituent_coverage(self, market_caps: Dict[str, pd.Series],
                                    snapshot_caps: Optional[Dict[str, float]] = None) -> pd.DataFrame:
        """Assemble the combined market-cap frame, dropping
        constituents whose recent-window data is unreliable before summing.

        TOTAL's OHLC is a sum of cache-first constituent closes
        whose coverage/freshness/scale varies per coin. Folding an unreliable
        constituent in with sound ones distorts the recent window of the
        summed series — the window CPR reads prevHigh/prevLow/prevClose from.
        Three real-world shapes corrupt it, all gated here (verified against
        live TOTAL data):

          * **Holey coverage** — a constituent present on only some recent
            days (e.g. M/GT/LEO held 67–90% of the June/July window). Its
            scaled cap blinks in and out, swinging the sum (observed
            1.9T↔6.3T) and blowing up the pivot range.
          * **End-truncated "zombie"** — deep start-history but coverage that
            stops days/weeks ago (delisted pair, dead history source). It
            passes any start-depth test, then its absence from recent days
            punches a step-dip into the summed series.
          * **Redenomination / bad-scale** — a dense, fresh constituent whose
            historical closes are on a different scale than its live price
            (SKY post-Maker rebrand spiked to a ~1.95T scaled cap, ~1400× its
            real ~1.5B snapshot). It passes coverage yet still swings the sum.

        Coverage: a constituent must hold at least ``COVERAGE_WINDOW_MIN_RATIO``
        of the best-covered constituent's day count over [previous full month
        .. ref_end] AND its latest observation must reach within
        ``COVERAGE_END_TOLERANCE_DAYS`` of ref_end. The density threshold is
        *relative* to the best constituent (not an absolute calendar count) so
        a uniform refresh lag / short fixture / cold universe — where every
        constituent is equally (in)complete — drops nobody; the coverage gate
        only engages when complete and incomplete coverage are actually mixed.

        Value sanity (only when ``snapshot_caps`` is supplied): a constituent
        whose largest recent scaled cap exceeds ``VALUE_SANITY_MAX_RATIO`` ×
        its live snapshot cap is dropped unconditionally — such a spike cannot
        be a real price move and always signals corrupt/rescaled history.

        The cache-first perf win is untouched: this gates what gets summed, it
        does not add fetches.
        """
        if not market_caps:
            return pd.DataFrame()

        combined = pd.DataFrame(market_caps)
        combined = combined.dropna(how='all')
        if combined.empty:
            return combined

        idx = pd.DatetimeIndex(combined.index).normalize()
        ref_end = idx.max()
        cur_month_start = ref_end.replace(day=1)
        # Start of the previous full calendar month.
        window_start = (cur_month_start - pd.Timedelta(days=1)).replace(day=1)

        in_window = (idx >= window_start) & (idx <= ref_end)
        if not in_window.any():
            return combined

        # Per-constituent recent-window presence + last observation.
        counts: Dict[str, int] = {}
        last_obs: Dict[str, Optional[pd.Timestamp]] = {}
        for col in combined.columns:
            present = in_window & combined[col].notna().to_numpy()
            counts[col] = int(present.sum())
            seen = idx[present]
            last_obs[col] = seen.max() if len(seen) else None

        best = max(counts.values()) if counts else 0
        if best == 0:
            return combined

        min_days = self.COVERAGE_WINDOW_MIN_RATIO * best
        end_floor = ref_end - pd.Timedelta(days=self.COVERAGE_END_TOLERANCE_DAYS)
        snapshot_caps = snapshot_caps or {}

        keep, cov_skip, val_skip = [], [], []
        for col in combined.columns:
            dense = counts[col] >= min_days
            fresh = last_obs[col] is not None and last_obs[col] >= end_floor
            if not (dense and fresh):
                cov_skip.append(col)
                continue
            # Value sanity: largest recent scaled cap vs live snapshot cap.
            snap = snapshot_caps.get(col)
            if snap and snap > 0:
                window_max = combined[col][in_window].max()
                if pd.notna(window_max) and window_max > self.VALUE_SANITY_MAX_RATIO * snap:
                    val_skip.append(col)
                    continue
            keep.append(col)

        # The relative-to-best density means a uniformly (in)complete universe
        # (short fixture, cold start, whole-source lag) leaves cov_skip empty —
        # the best+freshest constituent always clears coverage — so no special
        # "engage only when mixed" branch is needed. Drop the unreliable
        # constituents only while at least one sound constituent remains, so a
        # pathological all-bad universe is never emptied.
        skip = cov_skip + val_skip
        if skip and keep:
            def _reason(c):
                if c in val_skip:
                    snap = snapshot_caps.get(c) or 0
                    wm = combined[c][in_window].max()
                    r = (wm / snap) if snap else float('inf')
                    return f"{c}(cap x{r:.0f} snapshot)"
                stale = not (last_obs[c] is not None and last_obs[c] >= end_floor)
                return f"{c}({counts[c]}/{best}{',stale' if stale else ''})"

            logger.warning(
                "Market-cap coverage gate: skipping %d unreliable "
                "constituent(s) over %s..%s (coverage: >=%.0f%% of %d window "
                "days + last obs within %dd; value: cap <= %.0fx snapshot): %s",
                len(skip), window_start.date(), ref_end.date(),
                self.COVERAGE_WINDOW_MIN_RATIO * 100, best,
                self.COVERAGE_END_TOLERANCE_DAYS, self.VALUE_SANITY_MAX_RATIO,
                ", ".join(_reason(c) for c in sorted(skip)),
            )
            combined = combined[keep].dropna(how='all')

        return combined

    def _get_combined_market_caps(self, days: int = 365, num_coins: int = 70) -> pd.DataFrame:
        """Get combined market cap data for top coins.

        Constituent prices are cache-first. Most of the top
        ``num_coins`` overlap with the ~350-symbol curated universe the
        background precompute pass keeps warm in the per-symbol CSV cache
        (``self.cache`` — same store ``_get_crypto_ohlcv`` reads), so this is
        normally a batch of disk reads with zero network calls. Live-fetching
        every symbol from Binance sequentially with a 0.1s pause between each
        would, on a cold combined-cap cache (this method's own two tiers
        below), mean ~70 sequential live HTTP calls in the critical path of a
        single chart request (75-80s for TOTAL3/USDT.D, whose own metric cache
        isn't part of the warm set).
        Only symbols with NO cache entry at all (never precomputed — genuine
        long-tail coins) fall through to a live fetch, and those are bounded
        to a small thread pool since they're independent, irreducibly
        network-bound calls.
        """
        # Tier 1: in-memory cache
        cached = get_cached_market_caps()
        if cached is not None:
            logger.debug("Using centralized market cap cache")
            return cached

        # Tier 2: persistent file cache
        cached_data = self.market_cap_cache.load()
        if cached_data is not None:
            logger.debug("Restoring from persistent cache...")
            combined = self._restore_market_cap_df_from_cache(cached_data)
            set_cached_market_caps(combined)
            logger.debug(f"Restored {len(combined)} days of market cap data from persistent cache")
            return combined

        # Tier 3: build from constituent prices — cache-first
        logger.info(f"Building market cap data for top {num_coins} coins...")

        top_coins = self._get_top_coins(num_coins)
        if not top_coins:
            logger.warning("Failed to get top coins list")
            return pd.DataFrame()

        market_caps: Dict[str, pd.Series] = {}
        cold_coins = []

        for coin in top_coins:
            symbol = coin['symbol']

            if symbol in ('USDT', 'USDC'):
                current_mcap = coin['market_cap']
                current_price = coin.get('price', 1.0) or 1.0
                supply = current_mcap / current_price if current_price > 0 else current_mcap
                dates = pd.date_range(end=datetime.now().date(), periods=days, freq='D')
                stablecoin_df = pd.DataFrame({
                    'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 0
                }, index=dates)
                market_caps[symbol] = stablecoin_df['close'] * supply
                logger.info(f"  {symbol}: Stablecoin (supply: {supply:,.0f})")
                continue

            cached_df = self.cache.load(symbol)
            windowed = self._constituent_window(cached_df, days)
            if windowed is not None and not windowed.empty:
                self._apply_constituent_df(market_caps, symbol, coin, windowed)
            elif cached_df is not None and not cached_df.empty:
                # Cache exists but holds nothing inside the window (stale or
                # holed). Refetching would not help — skip rather than let its
                # old rows extend the derived indices backwards.
                logger.debug(
                    f"  {symbol}: no data within {days}d window, excluded "
                    f"from market-cap sum"
                )
            else:
                cold_coins.append(coin)

        if cold_coins:
            logger.info(
                f"Live-fetching {len(cold_coins)}/{len(top_coins)} uncached "
                f"market-cap constituent(s) (bounded concurrency)..."
            )
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix='mcap-fetch') as pool:
                future_to_coin = {
                    pool.submit(self._fetch_crypto_data, coin['symbol'], days): coin
                    for coin in cold_coins
                }
                for future in as_completed(future_to_coin):
                    coin = future_to_coin[future]
                    try:
                        df = future.result()
                    except Exception as e:
                        logger.warning(f"  {coin['symbol']}: live fetch failed: {e}")
                        df = pd.DataFrame()
                    self._apply_constituent_df(market_caps, coin['symbol'], coin, df)

        if not market_caps:
            return pd.DataFrame()

        # Gate constituent reliability (coverage + value sanity)
        # before summing so an under-covered or rescaled constituent can't
        # distort the recent window CPR reads from. Snapshot caps are the live
        # market-cap ground truth used for the value-sanity check.
        snapshot_caps = {
            c['symbol']: c.get('market_cap')
            for c in top_coins if c.get('symbol')
        }
        combined = self._gate_constituent_coverage(market_caps, snapshot_caps)
        if combined.empty:
            return combined

        set_cached_market_caps(combined)
        self.market_cap_cache.save(combined, top_coins)

        logger.debug(f"Cached {len(combined)} days of market cap data for {len(market_caps)} coins")
        return combined

    # ========================================
    # Crypto Metric Data (dominance, ratios)
    # ========================================

    def _fetch_crypto_metric_data(self, symbol: str, days: int = 365) -> pd.DataFrame:
        """Fetch crypto metric data by calculating from historical market caps."""
        try:
            symbol_upper = symbol.upper()
            metric_type = self.crypto_metric_map.get(symbol_upper)
            if not metric_type:
                logger.warning(f"Unknown crypto metric: {symbol}")
                return pd.DataFrame()

            # Ratio calculations
            ratio_pairs = {
                'ETHBTC': ('ETH', 'BTC'),
                'XRPBTC': ('XRP', 'BTC'),
                'AVAXBTC': ('AVAX', 'BTC'),
            }
            if symbol_upper in ratio_pairs:
                alt, base = ratio_pairs[symbol_upper]

                # These are real Binance spot markets. Prefer the native pair —
                # it carries true OHLC and volume. Dividing close by close only
                # ever yields open==high==low==close, which the chart draws as a
                # flat horizontal line instead of a candle.
                native_df = self.binance.fetch_pair_ohlcv(symbol_upper, days)
                if not native_df.empty:
                    logger.debug(
                        f"{symbol_upper}: {len(native_df)} native Binance bars, "
                        f"latest close={native_df['close'].iloc[-1]:.8f}"
                    )
                    return native_df

                logger.warning(
                    f"No native Binance market for {symbol_upper}, "
                    f"falling back to derived {alt}/{base} close ratio (flat OHLC)"
                )
                alt_df = self._fetch_crypto_data(alt, days)
                base_df = self._fetch_crypto_data(base, days)
                if alt_df.empty or base_df.empty:
                    return pd.DataFrame()
                common_idx = alt_df.index.intersection(base_df.index)
                ratio = alt_df.loc[common_idx, 'close'] / base_df.loc[common_idx, 'close']
                df = pd.DataFrame({
                    'open': ratio, 'high': ratio, 'low': ratio, 'close': ratio, 'volume': 0
                }, index=common_idx)
                logger.debug(f"Calculated {symbol_upper}: {len(df)} data points, latest={ratio.iloc[-1]:.8f}")
                return df

            # TOTAL3/BTC ratio
            if symbol_upper == 'TOTAL3BTC':
                logger.debug("Calculating TOTAL3/BTC ratio...")
                total3_df = self._fetch_crypto_metric_data('TOTAL3', days)
                btc_df = self._fetch_crypto_data('BTC', days)
                if total3_df.empty or btc_df.empty:
                    return pd.DataFrame()
                common_idx = total3_df.index.intersection(btc_df.index)
                ratio = total3_df.loc[common_idx, 'close'] / btc_df.loc[common_idx, 'close']
                df = pd.DataFrame({
                    'open': ratio, 'high': ratio, 'low': ratio, 'close': ratio, 'volume': 0
                }, index=common_idx)
                logger.debug(f"Calculated TOTAL3BTC: {len(df)} data points, latest={ratio.iloc[-1]:.8f}")
                return df

            # ALTBTC ratio
            if symbol_upper == 'ALTBTC':
                logger.debug("Calculating ALTBTC ratio (TOTAL3 - stablecoins) / BTC...")
                combined = self._get_combined_market_caps(days, num_coins=70)
                if combined.empty:
                    return pd.DataFrame()
                btc_df = self._fetch_crypto_data('BTC', days)
                if btc_df.empty:
                    return pd.DataFrame()

                total_cap = combined.sum(axis=1)
                btc_cap = combined.get('BTC', pd.Series(0, index=combined.index))
                eth_cap = combined.get('ETH', pd.Series(0, index=combined.index))
                usdc_cap = combined.get('USDC', pd.Series(0, index=combined.index))
                usdt_cap = combined.get('USDT', pd.Series(0, index=combined.index))

                alt_cap = (total_cap - btc_cap - eth_cap - usdc_cap - usdt_cap) / 1e12
                common_idx = alt_cap.index.intersection(btc_df.index)
                ratio = alt_cap.loc[common_idx] / btc_df.loc[common_idx, 'close']

                df = pd.DataFrame({
                    'open': ratio, 'high': ratio, 'low': ratio, 'close': ratio, 'volume': 0
                }, index=common_idx)
                logger.debug(f"Calculated ALTBTC: {len(df)} data points, latest={ratio.iloc[-1]:.8f}")
                return df

            # Dominance / total cap metrics
            logger.debug(f"Calculating {symbol} from market cap data...")
            combined = self._get_combined_market_caps(days, num_coins=70)
            if combined.empty:
                logger.debug(f"No market cap data available for {symbol}")
                return pd.DataFrame()

            total_cap = combined.sum(axis=1)
            btc_cap = combined.get('BTC', pd.Series(0, index=combined.index))
            eth_cap = combined.get('ETH', pd.Series(0, index=combined.index))
            usdt_cap = combined.get('USDT', pd.Series(0, index=combined.index))

            top10_symbols = ['BTC', 'ETH', 'USDT', 'BNB', 'SOL', 'XRP', 'USDC', 'ADA', 'DOGE', 'TRX']
            top10_cap = combined[[s for s in top10_symbols if s in combined.columns]].sum(axis=1)

            safe_total = total_cap.replace(0, float('nan'))

            if metric_type == 'btc_dominance':
                values = (btc_cap / safe_total * 100).fillna(0)
            elif metric_type == 'usdt_dominance':
                values = (usdt_cap / safe_total * 100).fillna(0)
            elif metric_type == 'total_market_cap':
                values = total_cap / 1e12
            elif metric_type == 'total2_market_cap':
                values = (total_cap - btc_cap) / 1e12
            elif metric_type == 'total3_market_cap':
                values = (total_cap - btc_cap - eth_cap) / 1e12
            elif metric_type == 'others_dominance':
                others_cap = total_cap - top10_cap
                values = (others_cap / safe_total * 100).fillna(0)
            else:
                values = pd.Series(0, index=combined.index)

            # Create proper OHLCV with realistic high/low variation
            opens = values.shift(1).fillna(values.iloc[0])
            rolling_std = values.rolling(window=5, min_periods=1).std().fillna(values * 0.02)
            max_oc = pd.concat([opens, values], axis=1).max(axis=1)
            min_oc = pd.concat([opens, values], axis=1).min(axis=1)
            highs = max_oc + rolling_std * 0.5
            lows = (min_oc - rolling_std * 0.5).clip(lower=0)

            df = pd.DataFrame({
                'open': opens, 'high': highs, 'low': lows, 'close': values, 'volume': 0
            }, index=values.index)

            logger.debug(f"Calculated {symbol}: {len(df)} data points, latest={values.iloc[-1]:.2f}")
            return df

        except Exception as e:
            logger.warning(f"Error fetching crypto metric {symbol}: {e}")
            return pd.DataFrame()

    # ========================================
    # Main OHLCV Entry Points
    # ========================================

    def get_ohlcv_data(self, symbol: str = 'BTC', start_date: Optional[datetime] = None,
                       end_date: Optional[datetime] = None, limit: Optional[int] = None) -> pd.DataFrame:
        """Fetch OHLCV data with delta download support."""
        symbol_upper = symbol.upper()

        # Retired: refuse before any network call. Returning an empty frame is
        # an already-supported path — get_multi_timeframe_data checks
        # df_base.empty and returns empty timeframes.
        if symbol_upper in RETIRED_SYMBOLS:
            logger.debug("Skipping retired symbol %s (see RETIRED_SYMBOLS)", symbol_upper)
            return pd.DataFrame()

        if self._is_crypto_metric(symbol_upper):
            return self._get_crypto_metric_ohlcv(symbol_upper, limit)
        if self._is_crypto(symbol_upper):
            return self._get_crypto_ohlcv(symbol_upper, limit)

        # The equity path needs a REASON, not a default. See the
        # _PROVIDER_FORMAT_TICKER block above for why <TICKER>.US as a
        # fall-through is the defect rather than a convenience.
        eligible, reason = self._equity_path_eligible(symbol_upper)
        if not eligible:
            _beacon_equity_path_refusal(symbol_upper, reason)
            return pd.DataFrame()

        # Non-crypto assets (stocks, ETFs, indices, forex, commodities).
        # Use self.stooq.fetch() which has correct symbol mappings for indices
        # (e.g. BUX→^BUX, HSTECH→3033.HK) and foreign stocks (e.g. RHM→RHM.DE).
        # StooqClient falls back to Yahoo automatically when Stooq is unavailable,
        # so all existing Yahoo-only symbols (US stocks, ETFs, forex) still work.
        # Do not call self.yahoo.fetch_daily() directly: it fails delta-refresh
        # for any symbol not in YahooClient.SYMBOL_MAP (BUX, RHM, etc.) — they
        # map to themselves on Yahoo and return 404, leaving the CSV cache
        # permanently stale.
        cached_df = self.cache.load(symbol)

        if cached_df is not None and not cached_df.empty:
            last_cached_date = cached_df.index[-1]
            days_old = (datetime.now() - last_cached_date).days

            # A fresh tail is not the same as a complete series.
            # Check the recent window for holes before taking the shortcut,
            # otherwise a missing weekday is welded in permanently.
            gap_repair = False
            if days_old < 2:
                missing = _missing_business_days(cached_df)
                if missing and not _is_gap_repair_backed_off(symbol):
                    _set_gap_repair_attempted(symbol)
                    gap_repair = True
                    logger.warning(
                        f"Interior gap in {symbol} cache: {missing} business day(s) "
                        f"missing within the last {_GAP_LOOKBACK} bars — refetching "
                        f"to repair (tail {last_cached_date.date()} looked fresh)"
                    )
                else:
                    logger.debug(f"Cache for {symbol} is up to date (last: {last_cached_date.date()})")
                    return cached_df.tail(limit) if limit else cached_df

            # A repair has to reach BEHIND the hole; the normal delta only needs
            # to reach the tail — but "reach the tail" means the tail's own
            # bar must come back in new_df, not just bars after it.
            #
            # Tail-freeze: stooq.fetch(start_date=X) queries from
            # X+1 forward (data_sources/stooq.py::fetch — deliberately exclusive,
            # so a normal delta never re-asks for what it already has). Passing
            # `last_cached_date` as-is therefore asks upstream for everything
            # AFTER the tail, so the tail's own row — even if it was written
            # intraday and is still wrong — can never come back in a response
            # and never gets corrected, no matter how many deltas run.
            # Example: GOLD's stored close was 4320.70 vs upstream's settled
            # 4242.00 (+1.86%), while every row behind the tail matched
            # upstream exactly.
            #
            # Going back one extra calendar day cancels the source's own +1,
            # so the effective query start lands ON the tail regardless of the
            # offset (and regardless of a weekend sitting behind the tail —
            # the extra day is inside the request WINDOW, not an exact date
            # match). This does not add fetches: it only changes what a fetch
            # that was already going to happen asks for. The merge below keeps
            # `keep='last'` (new row wins over cached for the same date), so
            # once the tail comes back it replaces the stale value.
            fetch_from = last_cached_date - timedelta(days=1)
            if gap_repair and len(cached_df) > _GAP_LOOKBACK:
                fetch_from = cached_df.index[-(_GAP_LOOKBACK + 1)]
            else:
                logger.info(f"Fetching delta for {symbol} (cache is {days_old} days old)")
            new_df = self.stooq.fetch(symbol, start_date=fetch_from)

            if not new_df.empty:
                new_df.index = new_df.index.normalize()
                cached_df.index = cached_df.index.normalize()
                combined_df = pd.concat([cached_df, new_df])
                combined_df = combined_df[~combined_df.index.duplicated(keep='last')].sort_index()
                self.cache.save(symbol, combined_df)
                if gap_repair:
                    # Did the repair actually close the hole? "Fetched some
                    # rows" is not the same as "the missing session arrived",
                    # and only the second one is a fix.
                    still_missing = _missing_business_days(combined_df)
                    _record_gap_repair_outcome(
                        symbol, 'unfilled' if still_missing else 'filled')
                    if still_missing:
                        logger.warning(
                            f"Gap repair for {symbol} fetched {len(new_df)} rows but "
                            f"{still_missing} business day(s) are still missing — "
                            f"upstream does not have them (holiday, or genuinely absent)"
                        )
                return combined_df.tail(limit) if limit else combined_df
            else:
                if gap_repair:
                    # Empty is NOT "upstream has no such bar" — stooq.fetch
                    # returns an empty frame from _yahoo_fallback when the
                    # yfinance breaker is open or both sources fail, i.e. we
                    # never got an answer. Burning the full 20h on that leaves
                    # pairs gapped for a day while the bars sit upstream.
                    _record_gap_repair_outcome(symbol, 'nofetch')
                    logger.warning(
                        f"Gap repair for {symbol} could not reach upstream (empty "
                        f"response); retrying in {_GAP_REPAIR_RETRY_TTL // 60}m "
                        f"rather than holding the hole for {_GAP_REPAIR_TTL // 3600}h"
                    )
                return cached_df.tail(limit) if limit else cached_df
        else:
            logger.debug(f"No cache for {symbol}, fetching full history")
            df = self.stooq.fetch(symbol)
            if not df.empty:
                self.cache.save(symbol, df)
            return df.tail(limit) if limit else df

    def _get_crypto_ohlcv(self, symbol: str, limit: Optional[int] = None) -> pd.DataFrame:
        """Get crypto OHLCV data with delta downloads."""
        cached_df = self.cache.load(symbol)

        if cached_df is not None and not cached_df.empty:
            last_cached_date = cached_df.index[-1]
            days_old = (datetime.now() - last_cached_date).days

            if days_old < 1:
                logger.debug(f"Cache for {symbol} is up to date (last: {last_cached_date.date()})")
                return cached_df.tail(limit) if limit else cached_df

            # A delta window that does not reach back to the cached tail would
            # concatenate two disjoint blocks and save the result — welding a
            # permanent hole into the cache, because the freshness check above
            # only looks at the LAST row and so never revisits the interior.
            # That is how a daily CSV can end up as an old block of rows plus
            # a short recent block with nothing in between. Rebuild instead.
            if days_old + 2 > _MAX_CRYPTO_DELTA_DAYS:
                # Delisted/rebranded pairs are permanently stale: the rebuild
                # fails every time and the tail never advances, so without a
                # backoff every request re-attempts a 1500-day upstream fetch.
                if _is_rebuild_backed_off(symbol):
                    logger.debug(
                        f"Rebuild for {symbol} backed off (recent attempt came "
                        f"back empty); serving stale cache"
                    )
                    return cached_df.tail(limit) if limit else cached_df

                # Single-flight: a full rebuild is the most expensive call we
                # make (1500 paginated days). If another process is already
                # doing it, wait for it and re-read what it wrote rather than
                # running the same request again.
                if not _try_acquire_fetch_slot(symbol, 'rebuild'):
                    logger.info(
                        f"Rebuild for {symbol} already in flight elsewhere; "
                        f"waiting instead of duplicating the fetch"
                    )
                    if _await_fetch_slot(symbol, 'rebuild'):
                        fresh = self.cache.load(symbol)
                        if fresh is not None and not fresh.empty and len(fresh) > len(cached_df):
                            logger.info(f"Picked up {symbol} rebuild from the winner")
                            return fresh.tail(limit) if limit else fresh
                    return cached_df.tail(limit) if limit else cached_df

                try:
                    logger.warning(
                        f"Cache for {symbol} is {days_old}d stale — beyond the "
                        f"{_MAX_CRYPTO_DELTA_DAYS}d delta window; rebuilding full "
                        f"history rather than welding a gap into the cache"
                    )
                    rebuilt = self._fetch_crypto_data(symbol, days=max(days_old + 30, 1500))
                    if not rebuilt.empty:
                        merged = pd.concat([cached_df, rebuilt])
                        merged = merged[~merged.index.duplicated(keep='last')].sort_index()
                        if len(merged) > 5000:
                            merged = merged.tail(5000)
                        self.cache.save(symbol, merged, is_crypto=True)
                        return merged.tail(limit) if limit else merged
                    _set_rebuild_backoff(symbol)
                    logger.warning(
                        f"Full rebuild failed for {symbol} (likely delisted); "
                        f"backing off {_REBUILD_BACKOFF_TTL // 3600}h, using stale cache"
                    )
                    return cached_df.tail(limit) if limit else cached_df
                finally:
                    _release_fetch_slot(symbol, 'rebuild')

            days_to_fetch = days_old + 2
            logger.info(f"Delta update for {symbol} (fetching {days_to_fetch} days)")
            new_df = self._fetch_crypto_data(symbol, days=days_to_fetch)

            if not new_df.empty:
                combined_df = pd.concat([cached_df, new_df])
                combined_df = combined_df[~combined_df.index.duplicated(keep='last')].sort_index()
                if len(combined_df) > 5000:
                    combined_df = combined_df.tail(5000)
                self.cache.save(symbol, combined_df, is_crypto=True)
                return combined_df.tail(limit) if limit else combined_df
            else:
                logger.warning(f"Delta fetch failed for {symbol}, using stale cache")
                return cached_df.tail(limit) if limit else cached_df
        else:
            # Cold cache — same 1500-day cost as a rebuild, same coalescing.
            # A loser here has nothing stale to fall back on, so it re-reads
            # the cache the winner wrote; only if that is still empty does it
            # fetch itself (fail-open beats serving nothing).
            if not _try_acquire_fetch_slot(symbol, 'cold'):
                logger.info(f"Cold fetch for {symbol} already in flight; waiting")
                if _await_fetch_slot(symbol, 'cold'):
                    fresh = self.cache.load(symbol)
                    if fresh is not None and not fresh.empty:
                        return fresh.tail(limit) if limit else fresh
            try:
                logger.info(f"No cache for {symbol}, fetching full history...")
                df = self._fetch_crypto_data(symbol, days=1500)
            finally:
                _release_fetch_slot(symbol, 'cold')
            if not df.empty:
                self.cache.save(symbol, df, is_crypto=True)
            return df.tail(limit) if limit else df

    def _get_crypto_metric_ohlcv(self, symbol: str, limit: Optional[int] = None) -> pd.DataFrame:
        """Get crypto metric OHLCV data with caching."""
        cached_df = self.cache.load(symbol)

        if cached_df is not None and not cached_df.empty:
            last_cached_date = cached_df.index[-1]
            hours_old = (datetime.now() - last_cached_date).total_seconds() / 3600
            if hours_old < 4:
                logger.debug(f"Cache for {symbol} is up to date (last: {last_cached_date})")
                return cached_df.tail(limit) if limit else cached_df

        logger.info(f"Fetching crypto metric data for {symbol}...")
        df = self._fetch_crypto_metric_data(symbol, days=365)

        if not df.empty:
            # crypto metric: fixed 365d window replaces wholesale by design
            self.cache.save(symbol, df, allow_shrink=True, is_crypto=True)
            return df.tail(limit) if limit else df

        if cached_df is not None and not cached_df.empty:
            logger.warning(f"API refresh failed for {symbol}, using stale cache")
            return cached_df.tail(limit) if limit else cached_df

        return df

    # ========================================
    # Multi-Timeframe Data
    # ========================================

    def resample_to_timeframe(self, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """Resample OHLCV data to different timeframe."""
        # Bars are labelled by their OPEN, like Binance / TradingView and every
        # intraday TF. pandas' defaults label a week by its Sunday END ('W')
        # and a month by its last day ('ME'), which stamps the in-progress bar
        # with a FUTURE date — the frontend live-tick code then skips it and
        # writes live prices into the previous bar
        # (tests/test_resample_bar_labels.py).
        timeframe_map = {
            '1D': '1D', '3D': '3D', '1W': 'W-MON', '1M': 'MS',
            '1h': '1h', '2h': '2h', '4h': '4h'
        }
        offset = timeframe_map.get(timeframe, timeframe)
        agg_dict = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
        if 'taker_buy_base' in df.columns:
            agg_dict['taker_buy_base'] = 'sum'
        kwargs = {'closed': 'left', 'label': 'left'} if offset == 'W-MON' else {}
        resampled = df.resample(offset, **kwargs).agg(agg_dict)
        return resampled.dropna(subset=['open', 'high', 'low', 'close'])

    def get_multi_timeframe_data(self, symbol: str = 'BTC', lookback_days: int = 365) -> dict:
        """Get OHLCV data for multiple timeframes with live candle injection."""
        df_base = self.get_ohlcv_data(symbol)

        if df_base.empty:
            return {'1D': pd.DataFrame(), '3D': pd.DataFrame(), '1W': pd.DataFrame(), '1M': pd.DataFrame()}

        symbol_upper = symbol.upper()
        if not self._is_crypto_metric(symbol_upper):
            df_base = self._inject_live_candle(df_base, symbol_upper)

        return {
            '1D': df_base.copy(),
            '3D': self.resample_to_timeframe(df_base, '3D'),
            '1W': self.resample_to_timeframe(df_base, '1W'),
            '1M': self.resample_to_timeframe(df_base, '1M')
        }

    def _inject_live_candle(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Replace today's daily candle with a live one built from hourly data."""
        try:
            df_1h = self.cache.load(symbol, timeframe='1h')
            if df_1h is None or df_1h.empty:
                return df

            today = pd.Timestamp(datetime.now().date())
            today_hours = df_1h[df_1h.index.normalize() == today]
            if today_hours.empty:
                return df

            live_candle = pd.DataFrame({
                'open': [float(today_hours.iloc[0]['open'])],
                'high': [float(today_hours['high'].max())],
                'low': [float(today_hours['low'].min())],
                'close': [float(today_hours.iloc[-1]['close'])],
                'volume': [float(today_hours['volume'].sum())]
            }, index=[today])

            df_updated = df[df.index < today]
            return pd.concat([df_updated, live_candle]).sort_index()

        except Exception as e:
            logger.debug(f"Live candle injection failed for {symbol}: {e}")
            return df

    def _get_yahoo_ltf_data(self, symbol_upper: str) -> dict:
        """Get LTF data for non-crypto assets via Yahoo Finance hourly."""
        empty = {'1h': pd.DataFrame(), '2h': pd.DataFrame(), '4h': pd.DataFrame()}

        cached = get_cached_ltf_data(symbol_upper)
        if cached is not None:
            logger.debug(f"Yahoo LTF memory cache hit for {symbol_upper}")
            return cached

        df_1h_cached = self.cache.load(symbol_upper, timeframe='1h')

        if df_1h_cached is not None and len(df_1h_cached) > 0:
            last_ts = df_1h_cached.index[-1]
            if hasattr(last_ts, 'to_pydatetime'):
                last_ts = last_ts.to_pydatetime()
            elif isinstance(last_ts, str):
                last_ts = pd.Timestamp(last_ts).to_pydatetime()
            hours_since = max(1, int((datetime.now() - last_ts).total_seconds() / 3600) + 2)

            if hours_since <= 1:
                df_1h = df_1h_cached
                logger.debug(f"Yahoo LTF CSV cache fresh for {symbol_upper}")
            else:
                logger.info(f"Yahoo LTF delta update for {symbol_upper}: {hours_since} hours")
                df_new = self.yahoo.fetch_hourly(symbol_upper, hours=hours_since)
                if not df_new.empty:
                    df_1h = pd.concat([df_1h_cached, df_new])
                    df_1h = df_1h[~df_1h.index.duplicated(keep='last')]
                    df_1h.sort_index(inplace=True)
                    if len(df_1h) > 25000:
                        df_1h = df_1h.tail(25000)
                else:
                    df_1h = df_1h_cached
        else:
            logger.info(f"Yahoo LTF initial fetch for {symbol_upper}: 10000 hours")
            df_1h = self.yahoo.fetch_hourly(symbol_upper, hours=10000)

        if df_1h.empty:
            return empty

        self.cache.save(symbol_upper, df_1h, timeframe='1h')

        df_2h = self.resample_to_timeframe(df_1h, '2h')
        df_4h = self.resample_to_timeframe(df_1h, '4h')

        result = {'1h': df_1h, '2h': df_2h, '4h': df_4h}
        set_cached_ltf_data(symbol_upper, result)
        return result

    def get_ltf_multi_timeframe_data(self, symbol: str) -> dict:
        """Get OHLCV data for LTF timeframes (1h/2h/4h)."""
        symbol_upper = symbol.upper()

        if self._is_crypto_metric(symbol_upper):
            daily_data = self.get_multi_timeframe_data(symbol_upper)
            return {'1h': daily_data['1D'], '2h': daily_data['3D'], '4h': daily_data['1W']}

        if not self._is_crypto(symbol_upper):
            return self._get_yahoo_ltf_data(symbol_upper)

        cached = get_cached_ltf_data(symbol_upper)
        if cached is not None:
            logger.debug(f"LTF memory cache hit for {symbol_upper}")
            return cached

        df_1h_cached = self.cache.load(symbol_upper, timeframe='1h')

        # Every crypto symbol uses the standard 1h CSV-freshness window.
        csv_fresh_hours = 1.0

        if df_1h_cached is not None and len(df_1h_cached) > 0:
            last_ts = df_1h_cached.index[-1]
            if hasattr(last_ts, 'to_pydatetime'):
                last_ts = last_ts.to_pydatetime()
            elif isinstance(last_ts, str):
                last_ts = pd.Timestamp(last_ts).to_pydatetime()
            hours_since_raw = (datetime.now() - last_ts).total_seconds() / 3600

            if hours_since_raw < csv_fresh_hours:
                df_1h = df_1h_cached
                logger.debug(f"LTF CSV cache fresh for {symbol_upper} ({hours_since_raw:.1f}h old, threshold={csv_fresh_hours}h), no fetch needed")
            else:
                hours_since = int(hours_since_raw) + 2  # +2 overlap for dedup
                logger.info(f"LTF delta update for {symbol_upper}: fetching last {hours_since} hours")
                df_new = self._fetch_hourly_data(symbol_upper, hours=hours_since)
                if not df_new.empty:
                    df_1h = pd.concat([df_1h_cached, df_new])
                    df_1h = df_1h[~df_1h.index.duplicated(keep='last')]
                    df_1h.sort_index(inplace=True)
                    if len(df_1h) > 25000:
                        df_1h = df_1h.tail(25000)
                else:
                    df_1h = df_1h_cached
        else:
            logger.info(f"LTF initial fetch for {symbol_upper}: 10000 hours")
            df_1h = self._fetch_hourly_data(symbol_upper, hours=10000)

        if df_1h.empty:
            return {'1h': pd.DataFrame(), '2h': pd.DataFrame(), '4h': pd.DataFrame()}

        self.cache.save(symbol_upper, df_1h, timeframe='1h')

        df_2h = self.resample_to_timeframe(df_1h, '2h')
        df_4h = self.resample_to_timeframe(df_1h, '4h')

        result = {'1h': df_1h, '2h': df_2h, '4h': df_4h}
        # Every crypto symbol uses the standard in-memory TTL.
        set_cached_ltf_data(symbol_upper, result, ttl=None)
        return result

    # ========================================
    # Asset Info & Symbol Listing
    # ========================================

    def get_all_symbols(self) -> list:
        """Get list of all available symbols."""
        all_symbols = []
        all_symbols.extend(self.crypto_map.keys())
        all_symbols.extend(self.crypto_metric_map.keys())
        all_symbols.extend(self.stock_map.keys())
        all_symbols.extend(self.etf_map.keys())
        all_symbols.extend(self.commodity_map.keys())
        all_symbols.extend(self.index_map.keys())
        all_symbols.extend(self.forex_map.keys())
        return sorted(all_symbols)

    def get_asset_info(self, symbol: str) -> dict:
        """Get asset type and display information."""
        symbol_upper = symbol.upper()

        if symbol_upper in self.crypto_map:
            name = symbol_upper
            top_coins = get_cached_top_coins()
            if top_coins:
                for coin in top_coins:
                    if coin['symbol'] == symbol_upper:
                        name = coin['name']
                        break
            fallback_names = {
                'BTC': 'Bitcoin', 'ETH': 'Ethereum', 'SOL': 'Solana',
                'ADA': 'Cardano', 'XRP': 'XRP', 'LTC': 'Litecoin',
                'DOGE': 'Dogecoin', 'DOT': 'Polkadot', 'AVAX': 'Avalanche',
                'LINK': 'Chainlink', 'POL': 'Polygon', 'UNI': 'Uniswap',
                'ATOM': 'Cosmos', 'XLM': 'Stellar', 'ALGO': 'Algorand',
                'VET': 'VeChain', 'FIL': 'Filecoin', 'AAVE': 'Aave',
                'EOS': 'EOS', 'XTZ': 'Tezos', 'BNB': 'BNB', 'TRX': 'TRON',
                'USDT': 'Tether', 'USDC': 'USD Coin',
                'BCH': 'Bitcoin Cash', 'ETC': 'Ethereum Classic', 'NEO': 'NEO', 'ZEC': 'Zcash',
                'DYDX': 'dYdX', 'ENS': 'ENS', 'GRT': 'The Graph',
                'SUSHI': 'SushiSwap', 'CAKE': 'PancakeSwap', 'LQTY': 'Liquity',
                'LISTA': 'Lista DAO', 'RDNT': 'Radiant Capital', 'RSR': 'Reserve Rights',
                'APE': 'ApeCoin', 'PIXEL': 'Pixels', 'PORTAL': 'Portal',
                'BIGTIME': 'Big Time', 'MAGIC': 'MAGIC', 'ILV': 'Illuvium',
                'AIXBT': 'aixbt', 'WLD': 'Worldcoin', 'ARKM': 'Arkham',
                'JASMY': 'JasmyCoin', 'CHZ': 'Chiliz', 'ROSE': 'Oasis',
                'CFX': 'Conflux', 'KAVA': 'Kava', 'OSMO': 'Osmosis',
                'LUNA': 'Terra', 'ZRO': 'LayerZero', 'SAGA': 'Saga',
                'DYM': 'Dymension', 'NTRN': 'Neutron', 'RAY': 'Raydium',
                'ACH': 'Alchemy Pay', 'BLUR': 'Blur', 'SSV': 'SSV Network',
                'ETHFI': 'ether.fi', 'MOVE': 'Movement',
                'BERA': 'Berachain', 'KAITO': 'Kaito',
                'ORDI': 'ORDI', 'NOT': 'Notcoin', 'DOGS': 'DOGS',
                'NEIRO': 'Neiro', 'PNUT': 'Peanut', 'TRUMP': 'TRUMP',
                'VIRTUAL': 'Virtuals Protocol', 'ANIME': 'Animecoin',
                'EIGEN': 'EigenLayer',
            }
            if name == symbol_upper:
                name = fallback_names.get(symbol_upper, symbol_upper)
            return {'symbol': symbol_upper, 'name': name, 'type': 'crypto', 'category': 'Cryptocurrency'}

        if symbol_upper in self.crypto_metric_map:
            names = {
                'BTC.D': 'BTC Dominance', 'USDT.D': 'USDT Dominance',
                'TOTAL': 'Total Crypto MC', 'TOTAL2': 'Total2 (ex-BTC)',
                'TOTAL3': 'Total3 (ex-BTC,ETH)', 'OTHERS.D': 'Others Dominance',
                'ETHBTC': 'ETH/BTC Ratio', 'XRPBTC': 'XRP/BTC Ratio',
                'AVAXBTC': 'AVAX/BTC Ratio', 'TOTAL3BTC': 'TOTAL3/BTC Ratio',
                'ALTBTC': 'ALT/BTC (ex-stables)'
            }
            return {'symbol': symbol_upper, 'name': names.get(symbol_upper, symbol_upper), 'type': 'crypto_metric', 'category': 'Crypto Metric'}

        if symbol_upper in self.commodity_map:
            names = {
                'OIL': 'Crude Oil', 'GOLD': 'Gold', 'SILVER': 'Silver',
                'COPPER': 'Copper', 'NATGAS': 'Natural Gas', 'PLATINUM': 'Platinum',
                'CORN': 'Corn', 'WHEAT': 'Wheat', 'SOYBEANS': 'Soybeans'
            }
            return {'symbol': symbol_upper, 'name': names.get(symbol_upper, symbol_upper), 'type': 'commodity', 'category': 'Commodity'}

        if symbol_upper in self.etf_map:
            names = {
                'VTI': 'Total Stock Mkt', 'QQQ': 'NASDAQ 100', 'IWM': 'Russell 2000',
                'XLE': 'Energy Sector', 'XLF': 'Financials', 'XLK': 'Technology', 'XLV': 'Healthcare',
                'EEM': 'Emerging Mkts', 'EWJ': 'Japan', 'EWZ': 'Brazil',
                'FXI': 'China Large Cap', 'KWEB': 'China Internet',
                'GDX': 'Gold Miners', 'URA': 'Uranium', 'MAGS': 'Magnificent 7',
                'TLT': '20Y Treasury', 'HYG': 'High Yield Corp',
                'LQD': 'IG Corporate', 'SHY': '1-3Y Treasury',
                'ARKK': 'ARK Innovation', 'IBIT': 'iShares Bitcoin', 'BITO': 'Bitcoin Strategy',
                'SOXX': 'Semiconductor', 'SMH': 'VanEck Semis', 'IGV': 'Software',
                'XBI': 'Biotech', 'VNQ': 'Real Estate', 'XLP': 'Consumer Staples', 'XLI': 'Industrials'
            }
            return {'symbol': symbol_upper, 'name': names.get(symbol_upper, symbol_upper), 'type': 'etf', 'category': 'ETF'}

        if symbol_upper in self.index_map:
            names = {
                'SPX': 'S&P 500', 'SPY': 'S&P 500 ETF', 'NDX': 'NASDAQ 100',
                'DJI': 'Dow Jones', 'RUT': 'Russell 2000', 'VIX': 'Volatility Index',
                'US5Y': '5Y Treasury Yield', 'US10Y': '10Y Treasury Yield', 'US30Y': '30Y Treasury Yield',
                'DAX': 'German DAX', 'FTSE': 'UK FTSE 100', 'CAC': 'France CAC 40',
                'BUX': 'Budapest BUX', 'NIKKEI': 'Nikkei 225',
                'HSI': 'Hang Seng', 'HSTECH': 'Hang Seng Tech', 'KOSPI': 'Korea KOSPI'
            }
            return {'symbol': symbol_upper, 'name': names.get(symbol_upper, symbol_upper), 'type': 'index', 'category': 'Index'}

        if symbol_upper in self.forex_map:
            names = {
                'DXY': 'US Dollar Index', 'EURUSD': 'EUR/USD', 'GBPUSD': 'GBP/USD',
                'USDJPY': 'USD/JPY', 'AUDUSD': 'AUD/USD', 'USDCAD': 'USD/CAD',
                'USDCHF': 'USD/CHF', 'NZDUSD': 'NZD/USD',
                'EURHUF': 'EUR/HUF', 'USDHUF': 'USD/HUF'
            }
            return {'symbol': symbol_upper, 'name': names.get(symbol_upper, symbol_upper), 'type': 'forex', 'category': 'Forex'}

        # Stocks (default)
        names = {
            'AAPL': 'Apple', 'GOOG': 'Alphabet', 'AMZN': 'Amazon',
            'META': 'Meta', 'TSLA': 'Tesla', 'NVDA': 'Nvidia', 'MSFT': 'Microsoft',
            'AMD': 'AMD', 'INTC': 'Intel', 'QCOM': 'Qualcomm',
            'BA': 'Boeing', 'LMT': 'Lockheed Martin', 'RTX': 'RTX Corp',
            'NOC': 'Northrop Grumman', 'GD': 'General Dynamics',
            'RHM': 'Rheinmetall', 'HON': 'Honeywell',
            'XOM': 'Exxon Mobil', 'CVX': 'Chevron',
            'JPM': 'JPMorgan Chase', 'GS': 'Goldman Sachs', 'V': 'Visa', 'MA': 'Mastercard',
            'UNH': 'UnitedHealth', 'JNJ': 'Johnson & Johnson',
            'PG': 'Procter & Gamble', 'KO': 'Coca-Cola', 'WMT': 'Walmart',
            'FMNB': 'Farmers Natl Banc', 'AM': 'Antero Midstream',
            'BYDDY': 'BYD Company', 'MSTR': 'MicroStrategy',
            'COIN': 'Coinbase', 'RIOT': 'Riot Platforms', 'MARA': 'Marathon Digital', 'HOOD': 'Robinhood',
            'SOFI': 'SoFi', 'SQ': 'Block', 'PYPL': 'PayPal',
            'PLTR': 'Palantir', 'ARM': 'Arm Holdings', 'SMCI': 'Super Micro',
            'CRWD': 'CrowdStrike', 'NET': 'Cloudflare', 'SNOW': 'Snowflake',
            'NFLX': 'Netflix', 'DIS': 'Disney', 'UBER': 'Uber',
            'LLY': 'Eli Lilly', 'PFE': 'Pfizer', 'TNK': 'Teekay Tankers'
        }
        return {'symbol': symbol_upper, 'name': names.get(symbol_upper, symbol_upper), 'type': 'stock', 'category': 'Stock'}

    # ========================================
    # Real-Time Price Integration
    # ========================================

    def fetch_realtime_crypto_prices(self) -> Dict[str, Dict[str, Any]]:
        """Fetch current prices using global cache (multi-source API)."""
        cached_prices = get_cached_crypto_prices()

        if cached_prices:
            logger.debug(f"Using globally cached prices ({len(cached_prices)} symbols)")
            result = {}
            for symbol, data in cached_prices.items():
                result[symbol] = {
                    'price': data.get('price'),
                    'change_24h': data.get('change_pct', 0),
                    'updated_at': datetime.now().isoformat(),
                    'source': data.get('source', 'global_cache'),
                    # Pass through CoinPaprika enrichment
                    # fields (may be absent for non-crypto/Binance-only rows).
                    'ath_distance_pct': data.get('ath_distance_pct'),
                    'change_pct_7d': data.get('change_pct_7d'),
                    'change_pct_30d': data.get('change_pct_30d'),
                }
            return result

        # No CoinGecko realtime fallback (non-commercial on the
        # free tier). On a cold price cache there is no third-party fallback —
        # Binance + CoinPaprika repopulate the global cache within one refresh
        # cycle, and get_combined_crypto_prices() still merges live Binance tickers.
        logger.warning("Global price cache empty; no realtime fallback (CoinGecko removed)")
        return {}

    def get_realtime_price(self, symbol: str) -> Optional[float]:
        prices = self.fetch_realtime_crypto_prices()
        return prices.get(symbol.upper(), {}).get('price')

    def fetch_binance_24hr_tickers(self) -> Dict[str, Dict[str, Any]]:
        """Fetch 24hr ticker data using global cache."""
        cached_binance = cache_manager.get(
            'binance_tickers',
            fetch_func=CryptoAPIFetcher.fetch_binance_tickers,
            ttl=30, allow_stale=True
        )
        if cached_binance:
            logger.debug(f"Using globally cached Binance tickers ({len(cached_binance)} symbols)")
            return cached_binance

        logger.warning("Global Binance cache unavailable, falling back to direct fetch")
        return self.binance.fetch_24hr_tickers()

    def get_combined_crypto_prices(self) -> Dict[str, Dict[str, Any]]:
        """Get crypto prices using global cache (multi-source)."""
        cached_combined = get_cached_crypto_prices()
        if cached_combined:
            logger.debug(f"Using globally cached combined prices ({len(cached_combined)} symbols)")
            return cached_combined

        logger.warning("Global combined cache unavailable, falling back to separate fetches")
        realtime_prices = self.fetch_realtime_crypto_prices()
        binance_prices = self.fetch_binance_24hr_tickers()

        result = {}
        for symbol, rt_data in realtime_prices.items():
            result[symbol] = rt_data.copy()
            if symbol in binance_prices:
                result[symbol]['change_pct'] = binance_prices[symbol].get('change_pct', 0)
                result[symbol]['open'] = binance_prices[symbol].get('open')
            else:
                result[symbol]['change_pct'] = rt_data.get('change_24h', 0)
        return result

    def inject_synthetic_candle(self, df: pd.DataFrame, realtime_price: float) -> pd.DataFrame:
        """Inject a synthetic candle with realtime price into historical data."""
        if df.empty or realtime_price is None:
            return df
        today = pd.Timestamp(datetime.now().date())
        synthetic_candle = pd.DataFrame({
            'open': [realtime_price], 'high': [realtime_price],
            'low': [realtime_price], 'close': [realtime_price], 'volume': [0.0]
        }, index=[today])
        df_filtered = df[df.index < today]
        return pd.concat([df_filtered, synthetic_candle]).sort_index()

    def get_multi_timeframe_data_with_realtime(self, symbol: str, realtime_price: Optional[float] = None) -> dict:
        """Get OHLCV for multiple timeframes with optional realtime price injection."""
        df_base = self.get_ohlcv_data(symbol)
        if df_base.empty:
            return {'1D': pd.DataFrame(), '3D': pd.DataFrame(), '1W': pd.DataFrame(), '1M': pd.DataFrame()}
        if realtime_price is not None:
            # A delisted/frozen Binance ticker can report a stale lastPrice
            # wildly detached from stored history (e.g. RNDR frozen at 7.03
            # while history sits ~1.68). Splicing that onto the daily series
            # corrupts CPR/RSI/BB downstream — gate on plausibility first.
            # The plausibility band above only asks "is this price
            # near the recent ones" — it says nothing about whether that recent
            # history is still ALIVE. A collector can stop and leave a symbol
            # frozen at a flat 1499.0; a live price of 1305.92 is 0.87x the
            # median, so it would pass cleanly and get spliced onto an
            # 8-week-old flatline. Downstream that produces a fully-formed technical read
            # on the public API and the MCP server: eight decisive_close entries
            # all "confirmed", key levels pdh/pdl/mh/ml identical at 1499.0
            # because that is the only value in the history, and RSI 3.7.
            # Confidently wrong, with nothing in the payload saying otherwise —
            # the worst possible shape for the channel we advertise.
            #
            # Reuses precompute's _is_history_stale rather than re-deriving the
            # rule: it already looks at the last bar strictly BEFORE today, so a
            # synthetic candle injected earlier cannot mask the staleness it
            # is being asked to detect.
            from carrac.precompute import _is_history_stale

            recent_median = df_base['close'].tail(5).median()
            if _is_history_stale(df_base, symbol):
                logger.warning(
                    f"{symbol}: rejecting synthetic candle — stored history is stale, "
                    f"so a live price would fabricate a bar against dead data"
                )
                self.price_injection_rejects.add(symbol)
            elif recent_median and recent_median > 0 and (
                realtime_price > recent_median * 1.5 or realtime_price < recent_median * 0.5
            ):
                logger.warning(
                    f"{symbol}: rejecting synthetic candle — realtime price {realtime_price} "
                    f"deviates >50% from recent median {recent_median}"
                )
                self.price_injection_rejects.add(symbol)
            else:
                df_base = self.inject_synthetic_candle(df_base, realtime_price)
                self.price_injection_rejects.discard(symbol)
        return {
            '1D': df_base.copy(),
            '3D': self.resample_to_timeframe(df_base, '3D'),
            '1W': self.resample_to_timeframe(df_base, '1W'),
            '1M': self.resample_to_timeframe(df_base, '1M')
        }

    # ========================================
    # Snapshot Storage for Trend Analysis
    # ========================================

    def _get_snapshot_path(self):
        history_dir = self.cache_dir / 'history'
        history_dir.mkdir(parents=True, exist_ok=True)
        return history_dir / 'snapshots.json'

    def load_previous_snapshot(self) -> Dict[str, Any]:
        snapshot_path = self._get_snapshot_path()
        if not snapshot_path.exists():
            logger.debug("No previous snapshot found")
            return {}
        try:
            with open(snapshot_path, 'r') as f:
                data = json.load(f)
            snapshot_date = data.get('_meta', {}).get('date', 'unknown')
            asset_count = len([k for k in data.keys() if not k.startswith('_')])
            logger.debug(f"Loaded previous snapshot from {snapshot_date} ({asset_count} assets)")
            return data
        except Exception as e:
            logger.warning(f"Error loading snapshot: {e}")
            return {}

    def save_daily_snapshot(self, assets_data: list) -> bool:
        snapshot_path = self._get_snapshot_path()
        today = datetime.now().strftime('%Y-%m-%d')

        existing = {}
        if snapshot_path.exists():
            try:
                with open(snapshot_path, 'r') as f:
                    existing = json.load(f)
            except Exception:
                pass

        if existing.get('_meta', {}).get('date', '') == today:
            logger.debug(f"Snapshot already saved for {today}")
            return False

        snapshot = {
            '_meta': {'date': today, 'saved_at': datetime.now().isoformat(), 'asset_count': len(assets_data)}
        }

        for asset in assets_data:
            symbol = asset.get('symbol', '')
            if not symbol:
                continue
            consensio = asset.get('consensio', {})
            ema_ribbon = asset.get('ema_ribbon', {})
            current_positions = asset.get('positions', 0)
            previous_data = existing.get(symbol, {})
            previous_positions = previous_data.get('positions', current_positions)
            streak = self._calculate_streak(
                current_positions, previous_positions,
                previous_data.get('streak', {'direction': 'flat', 'count': 0})
            )
            snapshot[symbol] = {
                'date': today, 'positions': current_positions,
                'signal_1d': consensio.get('1D', {}).get('signal', 0),
                'signal_3d': consensio.get('3D', {}).get('signal', 0),
                'signal_1w': consensio.get('1W', {}).get('signal', 0),
                'ema_ribbon_trend': ema_ribbon.get('trend', 'neutral') if ema_ribbon else 'neutral',
                'rsi': asset.get('weekly_rsi'),
                'streak': streak,
                'price': asset.get('latest_price'),
            }

        try:
            with open(snapshot_path, 'w') as f:
                json.dump(snapshot, f, indent=2)

            logger.debug(f"Saved snapshot for {today} ({len(assets_data)} assets)")
            return True
        except Exception as e:
            logger.error("Daily snapshot for %s not saved: %s", today, e)
            return False

    def _calculate_streak(self, current_positions, previous_positions, previous_streak):
        prev_direction = previous_streak.get('direction', 'flat')
        prev_count = previous_streak.get('count', 0)

        if current_positions > previous_positions:
            today_direction = 'up'
        elif current_positions < previous_positions:
            today_direction = 'down'
        else:
            today_direction = 'flat'

        if today_direction == prev_direction and today_direction != 'flat':
            new_count = prev_count + 1
        elif today_direction != 'flat':
            new_count = 1
        else:
            return previous_streak

        return {'direction': today_direction, 'count': new_count}

    def should_save_snapshot(self) -> bool:
        snapshot_path = self._get_snapshot_path()
        if not snapshot_path.exists():
            return True
        try:
            with open(snapshot_path, 'r') as f:
                data = json.load(f)
            return data.get('_meta', {}).get('date', '') != datetime.now().strftime('%Y-%m-%d')
        except Exception:
            return True

    # ========================================
    # Volume Metrics
    # ========================================

    def calculate_volume_metrics(self, df: pd.DataFrame, period: int = 20) -> Dict[str, Any]:
        if 'volume' not in df.columns or len(df) < period:
            return {'volume_ma_20': None, 'current_volume': None, 'ratio': None, 'is_above_average': False}

        volume = df['volume']
        volume_ma = volume.rolling(period).mean().iloc[-1]
        current_volume = volume.iloc[-1]

        if pd.isna(volume_ma) or volume_ma == 0:
            return {
                'volume_ma_20': None,
                'current_volume': current_volume if not pd.isna(current_volume) else None,
                'ratio': None, 'is_above_average': False
            }

        ratio = current_volume / volume_ma if volume_ma > 0 else 0
        return {
            'volume_ma_20': float(round(volume_ma, 2)),
            'current_volume': float(round(current_volume, 2)) if not pd.isna(current_volume) else None,
            'ratio': float(round(ratio, 2)),
            'is_above_average': bool(ratio > 1.0)
        }

    # ========================================
    # Delegated Futures / Metrics Methods
    # ========================================

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        if not self._is_crypto(symbol.upper()):
            return None
        return self.binance.fetch_funding_rates().get(symbol.upper())

    def get_perp_basis(self, symbol: str) -> Optional[float]:
        """Perp basis (markPrice vs indexPrice, %).

        Same guard pattern as get_funding_rate: non-crypto -> None, and the
        underlying fetch itself returns None (not 0.0) for symbols with no
        computable basis (missing/zero indexPrice).
        """
        if not self._is_crypto(symbol.upper()):
            return None
        return self.binance.fetch_perp_basis().get(symbol.upper())

    def fetch_open_interest(self, symbol: str = 'BTC'):
        if not self._is_crypto(symbol.upper()):
            return {'value': None, 'value_usd': None, 'change_pct': None}
        return self.binance.fetch_open_interest(symbol)

    def fetch_open_interest_hist(self, symbol, period='1h', limit=500):
        if not self._is_crypto(symbol.upper()):
            return []
        return self.binance.fetch_open_interest_hist(symbol, period, limit)

    def fetch_cvd_futures(self, symbol, period='1h', limit=500, margin_type='usdt'):
        if not self._is_crypto(symbol.upper()):
            return []
        return self.binance.fetch_cvd_futures(symbol, period, limit, margin_type)

    def fetch_long_short_ratio(self, symbols=None):
        return self.binance.fetch_long_short_ratio(symbols)

    def fetch_mvrv_zscore(self):
        # MVRV z-score dropped — its only source (CoinMetrics public CSV) is
        # non-commercial (CC BY-NC). Returns the same None-shape consumers already
        # handle. Reintroduce via a paid on-chain provider (tracked: ONCHAIN-1).
        return {'zscore': None, 'zone': 'unknown'}

    def fetch_mayer_multiple(self):
        # Mayer Multiple (price / 200-week MA) computed from our OWN BTC daily
        # closes via the pure mayer_card() helper — repointed off the CoinMetrics
        # CSV (non-commercial license). BTC only; same dict shape as before.
        from carrac.facts.mayer import mayer_card
        try:
            df = self.get_ohlcv_data('BTC')
            if df is None or df.empty or 'close' not in df:
                return {'mayer_multiple': None, 'zone': 'unknown', 'error': 'No BTC OHLCV'}
            return mayer_card(df['close'])
        except Exception as e:
            logger.warning(f"Mayer multiple (local 200WMA) failed: {e}")
            return {'mayer_multiple': None, 'zone': 'unknown', 'error': str(e)}

    def fetch_puell_multiple(self):
        # Puell Multiple dropped — its only source (CoinMetrics public CSV) is
        # non-commercial (CC BY-NC). Returns the same None-shape consumers already
        # handle. Reintroduce via a paid on-chain provider (tracked: ONCHAIN-1).
        return {'puell_multiple': None, 'zone': 'unknown'}

    def calculate_alt_season_index(self):
        return self.crypto_metrics.calculate_alt_season_index(self._get_90d_performance)

    # A 90-day return does not need today's bar. The daily cache's
    # own freshness test (`days_old < 1` in _get_crypto_ohlcv) flips every
    # crypto symbol to stale at UTC midnight simultaneously, which made the
    # Alt Season loop the first thing to refetch ~51 of them on the request
    # thread. One extra day of tolerance here means that loop reads what is on
    # disk; the chart path and the worker's asset loop keep the disk current.
    ALT_SEASON_STALE_OK_DAYS = 1

    def _get_90d_performance(self, symbol: str) -> Optional[float]:
        try:
            df = self.cache.load(symbol)
            if (df is None or df.empty
                    or (datetime.now() - df.index[-1]).days > self.ALT_SEASON_STALE_OK_DAYS):
                df = self.get_multi_timeframe_data(symbol).get('1D')
            if df is None or df.empty or len(df) < 30:
                return None
            old_price = df['close'].iloc[-90] if len(df) >= 90 else df['close'].iloc[0]
            current_price = df['close'].iloc[-1]
            return ((current_price - old_price) / old_price) * 100 if old_price > 0 else None
        except Exception as e:
            logger.debug(f"Error getting 90d performance for {symbol}: {e}")
            return None
