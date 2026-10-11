"""
Stooq data source for stocks, ETFs, indices, forex, commodities.

Primary source for non-crypto daily data with Yahoo Finance fallback.
Uses the shared BaseDataSource for retry/breaker/rate-limit plumbing.
"""

import pandas as pd
import logging
import threading
import time
from io import StringIO
from datetime import datetime, timedelta
from typing import Optional

from data_sources.base import BaseDataSource
from data_sources.circuit_breaker import CircuitBreaker

logger = logging.getLogger('carrac.data_sources.stooq')

# Symbols Stooq cannot actually serve.
#
# Stooq carries no spot commodity or FX series, so commodity_map / forex_map
# point these at ETF PROXIES on a completely different scale — OIL at USO,
# DXY at UUP (~$28 against a real DXY of ~104), EURUSD at FXE (~$100 against a
# rate of ~1.08). A successful Stooq fetch therefore returns the wrong
# INSTRUMENT, correctly parsed and silently cached under the spot symbol.
# Only GOLD and SILVER ever had a correction, via hardcoded multipliers that
# drift with ETF expense-ratio decay; the other fourteen had none at all.
#
# Note this fires on Stooq SUCCESS, not on the Yahoo fallback — the fallback is
# the safe path here, since YahooClient.SYMBOL_MAP has the real instrument for
# every symbol below (CL=F, DX-Y.NYB, EURUSD=X, ...). The maps themselves stay
# intact: database.py, carrac/search/routes.py and carrac/market/breadth.py all
# use them to classify a symbol as commodity/forex and to enumerate the
# universe, so removing entries would break search, breadth and the report
# blocks. Only the FETCH is rerouted.
#
# When Yahoo is also unavailable these symbols now return empty rather than
# wrong. That is deliberate: a gap is visible and recoverable, a wrong price
# published on the public brief is neither.
WRONG_SCALE_ON_STOOQ = frozenset({
    # Commodities — Stooq has the ETF, Yahoo has the future.
    'OIL', 'GOLD', 'SILVER', 'COPPER', 'NATGAS', 'PLATINUM',
    'CORN', 'WHEAT', 'SOYBEANS',
    # FX — Stooq has the currency ETF, Yahoo has the rate.
    'DXY', 'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCAD', 'USDCHF',
    # Already Yahoo-format in forex_map, so the Stooq request could only 404.
    'NZDUSD', 'EURHUF', 'USDHUF',
})

# Impact-level alert: BOTH stooq and Yahoo breakers open = TradFi data actually
# unavailable (the stooq breaker alone is notify=False — chronic, Yahoo-covered).
# Per-process cooldown; outages are rare enough that cross-process dedup is
# not worth the plumbing.
_IMPACT_NOTIFY_COOLDOWN_SECS: float = 6 * 3600.0
_impact_lock = threading.Lock()
_impact_state = {'last_notified_at': None}


def _notify_tradfi_outage(symbol: str) -> None:
    """ntfy push when stooq AND Yahoo are both down. Swallows all failures."""
    now = time.monotonic()
    with _impact_lock:
        last = _impact_state['last_notified_at']
        if last is not None and (now - last) < _IMPACT_NOTIFY_COOLDOWN_SECS:
            return
        _impact_state['last_notified_at'] = now
    try:
        from carrac.notifications.ntfy import send_ntfy
        send_ntfy(
            body=f"TradFi data unavailable: stooq AND Yahoo breakers both open (latest symbol: {symbol})",
            title="⚡ Carrac data source alert",
            priority="4",
            tags="rotating_light",
        )
    except Exception as e:
        logger.debug("TradFi outage notify failed (swallowed): %s", e)


class StooqClient(BaseDataSource):
    """Stooq data client with circuit breaker and Yahoo fallback."""

    NAME = 'stooq'
    # Fail fast: stooq is frequently TCP-unreachable from this host, so a 5s
    # connect timeout × 3 retries blocks workers for ~30s. 2s + a single attempt
    # + the hard-tripped breaker (see database.py) keeps the cost to ~2s once,
    # then stooq is skipped for 30 min and stock data falls back to Yahoo.
    DEFAULT_TIMEOUT = 2.0
    MAX_RETRIES = 1

    def __init__(self, breaker: CircuitBreaker, yahoo_client):
        # Preserve the externally-injected breaker (shared with callers that
        # may inspect it via stooq.breaker).
        super().__init__(breaker=breaker)
        self.yahoo = yahoo_client

        # Stock symbols (Stooq format)
        self.stock_map = {
            # Tech Giants (FAANG+)
            'AAPL': 'AAPL.US', 'GOOG': 'GOOG.US', 'AMZN': 'AMZN.US',
            'META': 'META.US', 'TSLA': 'TSLA.US', 'NVDA': 'NVDA.US', 'MSFT': 'MSFT.US',
            # Semiconductors
            'AMD': 'AMD.US', 'INTC': 'INTC.US', 'QCOM': 'QCOM.US',
            # Defense & Aerospace
            'BA': 'BA.US', 'LMT': 'LMT.US', 'RTX': 'RTX.US',
            'NOC': 'NOC.US', 'GD': 'GD.US', 'RHM': 'RHM.DE', 'HON': 'HON.US',
            # Energy
            'XOM': 'XOM.US', 'CVX': 'CVX.US',
            # Financials
            'JPM': 'JPM.US', 'GS': 'GS.US', 'V': 'V.US', 'MA': 'MA.US',
            # Healthcare
            'UNH': 'UNH.US', 'JNJ': 'JNJ.US',
            # Consumer
            'PG': 'PG.US', 'KO': 'KO.US', 'WMT': 'WMT.US',
            # Other
            'FMNB': 'FMNB.US', 'AM': 'AM.US',
            'BYDDY': '1211.HK', 'MSTR': 'MSTR.US',
            # Crypto-adjacent
            'COIN': 'COIN.US', 'RIOT': 'RIOT.US', 'MARA': 'MARA.US', 'HOOD': 'HOOD.US',
            # Fintech
            'SOFI': 'SOFI.US', 'SQ': 'SQ.US', 'PYPL': 'PYPL.US',
            # AI & Cloud
            'PLTR': 'PLTR.US', 'ARM': 'ARM.US', 'SMCI': 'SMCI.US',
            'CRWD': 'CRWD.US', 'NET': 'NET.US', 'SNOW': 'SNOW.US',
            # Media & Consumer
            'NFLX': 'NFLX.US', 'DIS': 'DIS.US', 'UBER': 'UBER.US',
            # Pharma
            'LLY': 'LLY.US', 'PFE': 'PFE.US',
            # Shipping / Energy
            'TNK': 'TNK.US',
        }

        # ETF symbols (Stooq format)
        self.etf_map = {
            # Broad Market
            'VTI': 'VTI.US', 'QQQ': 'QQQ.US', 'IWM': 'IWM.US',
            # Sector ETFs
            'XLE': 'XLE.US', 'XLF': 'XLF.US', 'XLK': 'XLK.US', 'XLV': 'XLV.US',
            # International
            'EEM': 'EEM.US', 'EWJ': 'EWJ.US', 'EWZ': 'EWZ.US',
            'FXI': 'FXI.US', 'KWEB': 'KWEB.US',
            # Commodities & Resources
            'GDX': 'GDX.US', 'URA': 'URA.US', 'MAGS': 'MAGS.US',
            # Bonds
            'TLT': 'TLT.US', 'HYG': 'HYG.US', 'LQD': 'LQD.US', 'SHY': 'SHY.US',
            # Innovation & Thematic
            'ARKK': 'ARKK.US', 'IBIT': 'IBIT.US', 'BITO': 'BITO.US',
            # Semiconductor
            'SOXX': 'SOXX.US', 'SMH': 'SMH.US', 'IGV': 'IGV.US',
            # Sector
            'XBI': 'XBI.US', 'VNQ': 'VNQ.US', 'XLP': 'XLP.US', 'XLI': 'XLI.US',
        }

        # Commodity ETF symbols (Stooq format)
        self.commodity_map = {
            'OIL': 'USO.US', 'GOLD': 'GLD.US', 'SILVER': 'SLV.US',
            'COPPER': 'CPER.US', 'NATGAS': 'UNG.US', 'PLATINUM': 'PPLT.US',
            'CORN': 'CORN.US', 'WHEAT': 'WEAT.US', 'SOYBEANS': 'SOYB.US',
        }

        # ETF → spot price multipliers (only for Stooq ETF fallback)
        #
        # These are now unreachable via fetch() — every symbol they
        # applied to is in WRONG_SCALE_ON_STOOQ and routes to Yahoo, which has
        # the real instrument. Kept only because scripts/backfill_daily_history.py
        # still reads them for its own separate Stooq parse path.
        self.etf_to_spot_multipliers = {
            'GOLD': 10.9,
            'SILVER': 1.85,
        }

        # Index symbols (Stooq format)
        self.index_map = {
            # US Indices
            'SPX': '^SPX', 'SPY': 'SPY.US', 'NDX': '^NDX',
            'DJI': '^DJI', 'RUT': '^RUT', 'VIX': '^VIX',
            # European
            'DAX': '^DAX', 'FTSE': '^FTSE', 'CAC': '^CAC', 'BUX': '^BUX',
            # Asian
            'NIKKEI': '^NKX', 'HSI': '^HSI', 'HSTECH': '3033.HK', 'KOSPI': '^KS11',
            # Bond Yields (via Yahoo)
            'US10Y': '^TNX', 'US30Y': '^TYX', 'US5Y': '^FVX',
        }

        # Forex/Currency ETFs (Stooq format)
        self.forex_map = {
            'DXY': 'UUP.US', 'EURUSD': 'FXE.US', 'GBPUSD': 'FXB.US',
            'USDJPY': 'FXY.US', 'AUDUSD': 'FXA.US', 'USDCAD': 'FXC.US',
            'USDCHF': 'FXF.US',
            'NZDUSD': 'NZDUSD=X',  # Yahoo direct
            'EURHUF': 'EURHUF=X',  # Yahoo direct
            'USDHUF': 'USDHUF=X',  # Yahoo direct
        }

    @property
    def breaker(self):
        # Backwards-compat alias (callers may still reference client.breaker).
        return self._breaker

    def _check_rate_limit(self, provider=None):
        try:
            from api_rate_limiter import can_call
            return can_call('stooq')
        except ImportError:
            return True, ''

    def _record_call(self, count: int = 1, provider=None):
        try:
            from api_rate_limiter import record_call
            record_call('stooq', count=count)
        except ImportError:
            pass

    def get_stooq_symbol(self, symbol: str) -> str:
        """Get Stooq symbol for asset."""
        symbol_upper = symbol.upper()
        for mapping in (self.stock_map, self.etf_map, self.commodity_map,
                        self.index_map, self.forex_map):
            if symbol_upper in mapping:
                return mapping[symbol_upper]
        return f"{symbol_upper}.US"

    def fetch(self, symbol: str, start_date: Optional[datetime] = None) -> pd.DataFrame:
        """Fetch OHLCV data from Stooq with Yahoo fallback and circuit breaker."""
        # Never ask Stooq for a symbol it can only answer with a
        # different instrument. Yahoo has the real one for all of these.
        if symbol.upper() in WRONG_SCALE_ON_STOOQ:
            logger.debug(
                f"{symbol}: Stooq has no spot series, going straight to Yahoo"
            )
            return self._yahoo_fallback(symbol, start_date)

        stooq_symbol = self.get_stooq_symbol(symbol)

        if start_date is None:
            start = datetime.now() - timedelta(days=730)
        else:
            start = start_date + timedelta(days=1)

        end = datetime.now()

        logger.info(f"Fetching {symbol} ({stooq_symbol}) from Stooq: {start.date()} to {end.date()}")

        url = (
            f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"
            f"&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}"
        )

        response = self._request('GET', url)
        if response is None:
            # Breaker open, rate limited, or terminal failure — fall through to Yahoo.
            return self._yahoo_fallback(symbol, start_date)

        if not response.ok:
            return self._yahoo_fallback(symbol, start_date)

        body = response.text
        if 'Exceeded' in body or 'limit' in body.lower():
            self._breaker.trip()
            logger.warning(f"Stooq rate limit exceeded for {symbol} - tripping circuit breaker")
            return self._yahoo_fallback(symbol, start_date)

        try:
            df = pd.read_csv(StringIO(body))
        except Exception as e:
            # A malformed CSV for one symbol (e.g. ^BUX returns extra columns)
            # is a per-symbol data quality issue, NOT an infra failure. Do NOT
            # call record_failure() here — that would trip the SHARED stooq
            # circuit breaker and cause ALL other symbols to fall back to Yahoo
            # for the breaker window. Log at warning and route to the fallback.
            logger.warning(f"Stooq parse error for {symbol}: {e}")
            return self._yahoo_fallback(symbol, start_date)

        if df.empty or 'Date' not in df.columns:
            return self._yahoo_fallback(symbol, start_date)

        df['Date'] = pd.to_datetime(df['Date'])
        df.set_index('Date', inplace=True)
        df = df.sort_index()
        df.columns = df.columns.str.lower()

        columns_to_keep = ['open', 'high', 'low', 'close', 'volume']
        df = df[[col for col in columns_to_keep if col in df.columns]]

        if 'volume' not in df.columns:
            df['volume'] = 0

        # Convert ETF prices to spot prices for commodities
        multiplier = self.etf_to_spot_multipliers.get(symbol.upper(), 1)
        if multiplier != 1:
            for col in ['open', 'high', 'low', 'close']:
                if col in df.columns:
                    df[col] = df[col] * multiplier
            logger.info(f"Applied ETF→spot multiplier {multiplier}x for {symbol}")

        logger.info(f"Downloaded {len(df)} new rows for {symbol} from Stooq")
        return df

    def _yahoo_fallback(self, symbol: str, start_date: Optional[datetime]) -> pd.DataFrame:
        if self.yahoo.daily_breaker.is_open():
            logger.warning("yfinance circuit OPEN - skipping fallback (using cache)")
            # is_tripped (read-only — no half-open probe release) so this check
            # can't perturb the stooq breaker's probe clock.
            if self._breaker.is_tripped:
                _notify_tradfi_outage(symbol)
            return pd.DataFrame()
        return self.yahoo.fetch_daily(symbol, start_date)
