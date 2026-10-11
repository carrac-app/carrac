"""
BaseDataSource — shared retry / circuit-breaker / timeout / observability.

Each venue client (binance, fred, yahoo, ...) subclasses BaseDataSource and
inherits a single `_request()` helper that:

  - Honours the client's circuit breaker (skips when open, allows one probe
    per half_open_after window).
  - Calls the `_check_rate_limit()` hook (default: always allowed); clients
    override to integrate `api_rate_limiter`.
  - Retries on 5xx / 429 with jittered exponential backoff (max MAX_RETRIES).
  - Does NOT retry on 4xx (caller opts in via retry_4xx=True).
  - Emits a Sentry breadcrumb per attempt.
  - Records breaker failure on terminal failure; resets on success.
  - Returns the `requests.Response` object (or None on failure) — shape parsing
    stays in the caller so CSV / XML / JSON clients are all first-class.

Not in scope: response parsing, caching, rate-limiter accounting details.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import requests

from data_sources.circuit_breaker import CircuitBreaker

logger = logging.getLogger('carrac.data_sources.base')

try:  # pragma: no cover — Sentry is optional in tests
    import sentry_sdk
    _HAS_SENTRY = True
except ImportError:  # pragma: no cover
    sentry_sdk = None
    _HAS_SENTRY = False


# Status codes that warrant a retry (transient).
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class BaseDataSource:
    """Mixin providing shared HTTP plumbing for data-source clients.

    Subclasses should set NAME (used for breaker + logging) and may override
    DEFAULT_TIMEOUT / MAX_RETRIES / BACKOFF_BASE / BREAKER_THRESHOLD /
    HALF_OPEN_AFTER as class attributes, and `_check_rate_limit()` /
    `_record_call()` as hooks.
    """

    NAME: str = 'base'
    DEFAULT_TIMEOUT: float = 15.0
    MAX_RETRIES: int = 3
    BACKOFF_BASE: float = 0.5           # seconds; jittered exponential
    BREAKER_THRESHOLD: int = 3
    HALF_OPEN_AFTER: float = 60.0       # seconds before a probe is allowed

    def __init__(self, *, session: Optional[requests.Session] = None,
                 breaker: Optional[CircuitBreaker] = None):
        self._session = session or requests.Session()
        self._breaker = breaker or CircuitBreaker(
            self.NAME,
            threshold=self.BREAKER_THRESHOLD,
            half_open_after=self.HALF_OPEN_AFTER,
        )

    # ------------------------------------------------------------------ hooks

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def _check_rate_limit(self, provider: Optional[str] = None) -> tuple[bool, str]:
        """Override to integrate `api_rate_limiter`. Default: always allowed.

        `provider` lets a client route to one of several rate-limit pools
        (e.g. Binance spot vs futures).
        """
        return True, ""

    def _record_call(self, count: int = 1, provider: Optional[str] = None) -> None:
        """Override to update `api_rate_limiter` post-success."""
        pass

    # ------------------------------------------------------------------ core

    def _request(self, method: str, url: str, *,
                 params: Any = None,
                 data: Any = None,
                 json: Any = None,
                 headers: Optional[dict] = None,
                 timeout: Optional[float] = None,
                 retry_4xx: bool = False,
                 max_retries: Optional[int] = None,
                 breaker: Optional[CircuitBreaker] = None,
                 provider: Optional[str] = None,
                 record_count: int = 1) -> Optional[requests.Response]:
        """Make an HTTP request with retry, breaker, rate-limit, and observability.

        Returns the `requests.Response` on success or None if skipped / failed.
        The caller parses the body (text / json / csv / xml).

        The optional `breaker` argument overrides self._breaker for this call
        (used by clients that maintain per-endpoint breakers).
        """
        active_breaker = breaker or self._breaker
        if active_breaker.is_open():
            logger.warning(f"{self.NAME}: circuit OPEN, skipping {method} {url}")
            return None

        allowed, reason = self._check_rate_limit(provider=provider)
        if not allowed:
            logger.warning(f"{self.NAME}: rate limit — {reason}")
            return None

        attempts = max_retries if max_retries is not None else self.MAX_RETRIES
        resolved_timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT

        last_error: Exception | None = None
        last_response: requests.Response | None = None

        for attempt in range(attempts):
            try:
                resp = self._session.request(
                    method, url,
                    params=params, data=data, json=json,
                    headers=headers, timeout=resolved_timeout,
                )
                _breadcrumb(self.NAME, method, url, attempt, status=resp.status_code)

                # Retryable transient (5xx / 408 / 425 / 429)?
                if resp.status_code in RETRYABLE_STATUS:
                    if attempt < attempts - 1:
                        delay = self._backoff_delay(attempt)
                        logger.info(
                            f"{self.NAME}: {method} {url} → {resp.status_code}, "
                            f"retry {attempt + 1}/{attempts - 1} in {delay:.2f}s"
                        )
                        time.sleep(delay)
                        last_response = resp
                        continue
                    # Exhausted retries on a retryable status — terminal failure.
                    last_response = resp
                    break

                # Non-retryable 4xx — return to caller (server is healthy).
                if 400 <= resp.status_code < 500 and not retry_4xx:
                    self._record_call(count=record_count, provider=provider)
                    return resp

                # Retryable 4xx (opt-in, rare)
                if retry_4xx and resp.status_code >= 400 and attempt < attempts - 1:
                    delay = self._backoff_delay(attempt)
                    time.sleep(delay)
                    last_response = resp
                    continue

                # 2xx → success
                if resp.ok:
                    active_breaker.reset()
                    self._record_call(count=record_count, provider=provider)
                    return resp

                # Fell through (e.g. 3xx not handled) — treat as terminal.
                last_response = resp
                break

            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                _breadcrumb(self.NAME, method, url, attempt, error=str(exc))
                if attempt < attempts - 1:
                    delay = self._backoff_delay(attempt)
                    logger.info(
                        f"{self.NAME}: {method} {url} → {type(exc).__name__}, "
                        f"retry {attempt + 1}/{attempts - 1} in {delay:.2f}s"
                    )
                    time.sleep(delay)
                    continue
                break

            except requests.RequestException as exc:
                # Non-transient requests error (InvalidURL, SSL, etc.)
                last_error = exc
                _breadcrumb(self.NAME, method, url, attempt, error=str(exc))
                break

        # Terminal failure: bump breaker, log, return None.
        active_breaker.record_failure()
        if last_response is not None:
            logger.warning(
                f"{self.NAME}: {method} {url} → {last_response.status_code} "
                f"after {attempts} attempts"
            )
        elif last_error is not None:
            logger.warning(
                f"{self.NAME}: {method} {url} failed after {attempts} attempts: {last_error}"
            )
        return None

    # ------------------------------------------------------------- internal

    def _backoff_delay(self, attempt: int) -> float:
        """Jittered exponential backoff: base * 2^attempt * (0.5..1.5)."""
        exp = self.BACKOFF_BASE * (2 ** attempt)
        jitter = random.uniform(0.5, 1.5)
        return exp * jitter


def _breadcrumb(name: str, method: str, url: str, attempt: int,
                status: Optional[int] = None, error: Optional[str] = None) -> None:
    """Emit a Sentry breadcrumb for this attempt (no-op if Sentry absent)."""
    if not _HAS_SENTRY:
        return
    data = {'attempt': attempt, 'url': url, 'method': method}
    if status is not None:
        data['status'] = status
    if error is not None:
        data['error'] = error
    try:
        sentry_sdk.add_breadcrumb(
            category='http.data_source',
            message=f'{name} {method} {url}',
            data=data,
            level='info' if status and status < 400 else 'warning',
        )
    except Exception:  # pragma: no cover — never let breadcrumbs break the call
        pass
