"""
FRED (Federal Reserve Economic Data) API client.

Provides macro indicators: M2 money supply, Fed Funds rate,
10Y-2Y yield spread, 10Y yield, DXY broad index.
All data cached with configurable TTL to minimize API calls.
"""

import logging
import os
import time
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Iterable

from data_sources.base import BaseDataSource

# FRED's free tier rate-limits concurrent requests aggressively (~120/min but
# bursty concurrency causes 429s during precompute when ~6 calls fire at once).
# A semaphore of 2 serializes most of the burst while still allowing two
# in-flight requests (e.g. the sentiment ThreadPoolExecutor + a regime call).
# The existing per-series in-memory cache means real calls are rare (TTL 1-4h),
# so this semaphore does not add meaningful latency in steady state.
_FRED_REQUEST_SEMAPHORE = threading.Semaphore(2)

logger = logging.getLogger('carrac.data_sources.fred')

# FRED series IDs
SERIES = {
    'M2': 'WM2NS',           # M2 money supply (weekly, billions)
    'FED_FUNDS': 'DFF',       # Federal Funds effective rate (daily, %)
    'YIELD_10Y2Y': 'T10Y2Y',  # 10-Year minus 2-Year spread (daily, %)
    'YIELD_10Y': 'DGS10',     # 10-Year Treasury yield (daily, %)
    'DXY': 'DTWEXBGS',        # Trade-weighted USD broad index (daily)
}


class FredClient(BaseDataSource):
    """FRED API client with in-memory caching."""

    NAME = 'fred'
    BASE_URL = 'https://api.stlouisfed.org/fred/series/observations'
    DEFAULT_TIMEOUT = 15.0

    def __init__(self, api_key: Optional[str] = None):
        super().__init__()
        self.api_key = api_key or os.environ.get('FRED_API_KEY', '')
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

        # Cache TTLs (seconds)
        self.TTL_DAILY = 3600      # 1 hour for daily series
        self.TTL_WEEKLY = 14400    # 4 hours for weekly series (M2)

    def _fetch_series(self, series_id: str, limit: int = 30,
                      sort_order: str = 'desc') -> list:
        """Fetch observations from FRED API."""
        if not self.api_key:
            logger.warning("FRED API key not configured")
            return []

        with _FRED_REQUEST_SEMAPHORE:
            return self._fetch_series_http(series_id, limit=limit, sort_order=sort_order)

    def _fetch_series_http(self, series_id: str, limit: int = 30,
                           sort_order: str = 'desc') -> list:
        """Perform the actual HTTP fetch (called under _FRED_REQUEST_SEMAPHORE)."""
        resp = self._request('GET', self.BASE_URL, params={
            'series_id': series_id,
            'api_key': self.api_key,
            'file_type': 'json',
            'limit': limit,
            'sort_order': sort_order,
        })

        if resp is None or not resp.ok:
            if resp is not None:
                logger.warning(f"FRED API error for {series_id}: {resp.status_code}")
            return []

        try:
            data = resp.json()
            observations = data.get('observations', [])

            # Filter out missing values (FRED uses "." for missing)
            return [
                {'date': obs['date'], 'value': float(obs['value'])}
                for obs in observations
                if obs.get('value') not in (None, '.', '')
            ]
        except Exception as e:
            logger.warning(f"FRED parse failed for {series_id}: {e}")
            return []

    def _get_cached(self, key: str, ttl: int) -> Optional[Dict]:
        """Get cached data if still fresh."""
        with self._lock:
            cached = self._cache.get(key)
            if cached and (time.time() - cached['fetched_at']) < ttl:
                return cached['data']
        return None

    def _set_cached(self, key: str, data: Dict):
        """Store data in cache."""
        with self._lock:
            self._cache[key] = {'data': data, 'fetched_at': time.time()}

    def _get_series_summary(self, series_key: str, limit: int = 30) -> Optional[Dict]:
        """Get latest value + trend for a FRED series."""
        series_id = SERIES.get(series_key)
        if not series_id:
            return None

        ttl = self.TTL_WEEKLY if series_key == 'M2' else self.TTL_DAILY
        cached = self._get_cached(series_key, ttl)
        if cached is not None:
            return cached

        observations = self._fetch_series(series_id, limit=limit)
        if not observations:
            return None

        latest = observations[0]
        result = {
            'series': series_key,
            'series_id': series_id,
            'value': latest['value'],
            'date': latest['date'],
            'fetched_at': datetime.now().isoformat(),
        }

        # Calculate changes if we have history
        if len(observations) >= 2:
            prev = observations[1]
            result['prev_value'] = prev['value']
            result['change'] = round(latest['value'] - prev['value'], 4)

        # For M2: calculate YoY growth rate (need ~52 weekly observations)
        if series_key == 'M2' and len(observations) >= 2:
            # Find value from ~1 year ago (closest to 52 weeks back)
            year_ago = None
            target_date = datetime.strptime(latest['date'], '%Y-%m-%d') - timedelta(days=365)
            for obs in observations:
                obs_date = datetime.strptime(obs['date'], '%Y-%m-%d')
                if obs_date <= target_date:
                    year_ago = obs
                    break

            if year_ago and year_ago['value'] > 0:
                yoy = ((latest['value'] - year_ago['value']) / year_ago['value']) * 100
                result['yoy_pct'] = round(yoy, 2)

            # 3-month growth (13 weeks)
            three_mo_ago = None
            target_3m = datetime.strptime(latest['date'], '%Y-%m-%d') - timedelta(days=90)
            for obs in observations:
                obs_date = datetime.strptime(obs['date'], '%Y-%m-%d')
                if obs_date <= target_3m:
                    three_mo_ago = obs
                    break

            if three_mo_ago and three_mo_ago['value'] > 0:
                mom3 = ((latest['value'] - three_mo_ago['value']) / three_mo_ago['value']) * 100
                result['growth_3m_pct'] = round(mom3, 2)

        # For yields/rates: determine trend from last 5 observations
        if len(observations) >= 5 and series_key != 'M2':
            recent_values = [obs['value'] for obs in observations[:5]]
            avg_recent = sum(recent_values[:3]) / 3
            avg_older = sum(recent_values[2:]) / 3
            if avg_recent > avg_older + 0.02:
                result['trend'] = 'rising'
            elif avg_recent < avg_older - 0.02:
                result['trend'] = 'falling'
            else:
                result['trend'] = 'flat'

        self._set_cached(series_key, result)
        return result

    def get_m2(self) -> Optional[Dict]:
        """Get M2 money supply with YoY growth rate."""
        return self._get_series_summary('M2', limit=60)

    def get_fed_funds(self) -> Optional[Dict]:
        """Get Federal Funds effective rate."""
        return self._get_series_summary('FED_FUNDS', limit=30)

    def get_yield_spread(self) -> Optional[Dict]:
        """Get 10Y-2Y Treasury yield spread."""
        return self._get_series_summary('YIELD_10Y2Y', limit=30)

    def get_yield_10y(self) -> Optional[Dict]:
        """Get 10-Year Treasury yield."""
        return self._get_series_summary('YIELD_10Y', limit=30)

    def get_dxy(self) -> Optional[Dict]:
        """Get trade-weighted USD index (broad)."""
        return self._get_series_summary('DXY', limit=30)

    # Default wall-clock budget for the WHOLE get_all() fan-out,
    # not per-request-caller-configurable in production (only tests override
    # it). Chosen well under `_SENTIMENT_FRED_SUBBUDGET_FRACTION *
    # SENTIMENT_COMPUTE_BUDGET_S` in carrac/sentiment/routes.py (default
    # 0.5 * 12 = 6s) so get_all() gives up on its own before the caller's
    # sub-budget does, instead of the caller's timeout being the only thing
    # that ever fires.
    GET_ALL_TIMEOUT_S = 5.0

    def get_all(self, timeout_s: Optional[float] = None,
                keys: Optional[Iterable[str]] = None) -> Dict[str, Optional[Dict]]:
        """Get all FRED indicators at once. Used by sentiment endpoint.

        ``keys`` narrows the fan-out to a subset of the five
        series (regime needs only yield_spread/m2/fed_funds) so a second
        caller can share this deadline instead of making its own untimed
        sequential calls through ``_FRED_REQUEST_SEMAPHORE``.

        Fetches all five series concurrently.  The shared ``_FRED_REQUEST_SEMAPHORE``
        (capacity 2) already limits parallel HTTP requests to FRED, so at most two
        uncached series hit the wire at the same time — the same serialisation as the
        old sequential code but with overlapping I/O wait for the others.

        Bounded to ``timeout_s`` (default ``GET_ALL_TIMEOUT_S``) of
        total wall clock, mirroring
        ``carrac/sentiment/routes.py::_compute_sentiment``. Without a timeout
        (e.g. a `with ThreadPoolExecutor(...)` block iterating
        `as_completed(futures)`) one series retrying through a FRED outage
        would block `get_all()` (and hold a `_FRED_REQUEST_SEMAPHORE` slot) for
        the full per-series retry duration, starving the other series and
        producing "Sentiment compute budget exhausted ... 'fred'" events.
        `executor.shutdown(wait=False)` is deliberate for the same reason as
        routes.py: `with`'s `__exit__` calls `shutdown(wait=True)`, which would
        re-join the very task `future.result(timeout=...)` just gave up on,
        making the timeout above decorative rather than a real bound.
        """
        from concurrent.futures import ThreadPoolExecutor

        if timeout_s is None:
            timeout_s = self.GET_ALL_TIMEOUT_S
        deadline = time.monotonic() + timeout_s

        tasks = {
            'm2': self.get_m2,
            'fed_funds': self.get_fed_funds,
            'yield_spread': self.get_yield_spread,
            'yield_10y': self.get_yield_10y,
            'dxy': self.get_dxy,
        }
        if keys is not None:
            tasks = {key: tasks[key] for key in keys}
        results: Dict[str, Optional[Dict]] = {}
        executor = ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix='fred_all')
        try:
            futures = {key: executor.submit(fn) for key, fn in tasks.items()}
            for key, future in futures.items():
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    results[key] = future.result(timeout=remaining)
                except Exception as exc:
                    # Same discriminator as routes.py: `future.done()` tells a
                    # real failure apart from a budget timeout. Since 3.10
                    # `socket.timeout` IS `TimeoutError`, testing the exception
                    # type here would misreport genuine failures as timeouts.
                    if not future.done():
                        logger.warning(
                            f"FRED get_all: {key} still running past the "
                            f"{timeout_s}s budget — serving None for it"
                        )
                    else:
                        logger.warning(f"FRED get_all: {key} failed: {exc}")
                    results[key] = None
        finally:
            executor.shutdown(wait=False)
        return results


# ---------------------------------------------------------------- shared client

# FredClient was constructed per call at every production call site,
# so BOTH the circuit breaker in BaseDataSource.__init__ and the per-series cache
# above were born empty on every request. `breaker.is_open()` was therefore always
# False on entry, every request paid the full retry budget from scratch, and the
# TTL_DAILY / TTL_WEEKLY cache never once produced a cross-request hit — which is
# how one GET /api/market-sentiment came to block for 79.30s during a FRED
# outage. A per-instance breaker also defeated NOTIFY_COOLDOWN_SECS, since the
# instance holding the cooldown clock died with the request.
#
# Every production caller must go through get_fred_client(). Constructing
# FredClient directly is reserved for tests and for one-off scripts that
# deliberately want their own breaker (see scripts/backfill_daily_history.py).
_shared_client: Optional['FredClient'] = None
_shared_client_lock = threading.Lock()


def get_fred_client() -> 'FredClient':
    """Return the process-wide FredClient (breaker + series cache persist)."""
    global _shared_client
    if _shared_client is None:
        with _shared_client_lock:
            if _shared_client is None:
                _shared_client = FredClient()
    return _shared_client
