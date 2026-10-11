"""
Reusable circuit breaker for data source clients.

Tracks consecutive failures per source and skips calls when threshold is exceeded.
After `half_open_after` seconds open, allows one probe through — if it succeeds,
the breaker resets; if it fails, the probe clock restarts.

Thread-safe for use in multi-threaded Flask server.

Operator notifications: on the CLOSED→OPEN transition (record_failure reaching
threshold, or trip()), a single ntfy notification is fired with a per-source
cooldown of NOTIFY_COOLDOWN_SECS (default 3600s) so a flapping source can't spam.
Any failure in the notify call is swallowed — the breaker must never break because
notification failed.
"""

import threading
import time
import logging

logger = logging.getLogger('carrac.circuit_breaker')

# Minimum seconds between successive OPEN notifications for the same source.
NOTIFY_COOLDOWN_SECS: float = 3600.0


def _notify_open(name: str) -> None:
    """Fire an ntfy notification that a circuit breaker has opened.

    Failures are caught and logged at debug so the breaker is never disrupted.
    """
    try:
        from carrac.notifications.ntfy import send_ntfy
        send_ntfy(
            body=f"Circuit breaker OPEN: {name}",
            title="⚡ Carrac data source alert",
            priority="3",
            tags="warning",
        )
    except Exception as e:
        logger.debug("Circuit breaker notify failed (swallowed): %s", e)


class CircuitBreaker:
    """Thread-safe circuit breaker with half-open probe.

    Usage:
        breaker = CircuitBreaker('stooq', threshold=3, half_open_after=60)
        if breaker.is_open():
            return fallback_result
        try:
            result = make_api_call()
            breaker.reset()
            return result
        except Exception:
            breaker.record_failure()
            raise
    """

    def __init__(self, name: str, threshold: int = 3, half_open_after: float = 60.0,
                 notify: bool = True):
        self.name = name
        self.threshold = threshold
        self.half_open_after = half_open_after
        # notify=False → breaker still opens/probes/logs, but never pushes ntfy.
        # Use for sources whose failure is chronic AND fallback-covered (stooq:
        # intermittent IP block of this host, Yahoo carries every code path —
        # the mechanism alert was pure noise; the real signal is BOTH sources
        # down, alerted at the fallback site instead).
        self.notify = notify
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        # Monotonic timestamp of the last OPEN notification sent (None = never).
        self._last_notified_at: float | None = None

    def is_open(self) -> bool:
        """Check whether calls should be skipped.

        Returns False once per `half_open_after` seconds to let a probe through.
        """
        with self._lock:
            if self._failures < self.threshold:
                return False
            # In the open state: allow a probe after half_open_after seconds.
            now = time.monotonic()
            if self._opened_at is not None and (now - self._opened_at) >= self.half_open_after:
                # Bump the clock so only one probe is allowed per window.
                self._opened_at = now
                logger.info(f"Circuit breaker HALF-OPEN probe for {self.name}")
                return False
            return True

    def record_failure(self) -> int:
        """Increment failure counter. Returns new count."""
        should_notify = False
        with self._lock:
            self._failures += 1
            count = self._failures
            if count == self.threshold:
                now = time.monotonic()
                self._opened_at = now
                logger.warning(f"Circuit breaker OPEN for {self.name} ({count} failures)")
                if self.notify and (self._last_notified_at is None
                        or (now - self._last_notified_at) >= NOTIFY_COOLDOWN_SECS):
                    self._last_notified_at = now
                    should_notify = True
        if should_notify:
            try:
                _notify_open(self.name)
            except Exception as e:
                logger.debug("Circuit breaker open-notify raised (swallowed): %s", e)
        return count

    def reset(self):
        """Reset failure counter after a successful call."""
        with self._lock:
            if self._failures > 0:
                logger.info(f"Circuit breaker RESET for {self.name}")
            self._failures = 0
            self._opened_at = None

    def trip(self):
        """Trip circuit breaker immediately (e.g., on rate limit response)."""
        should_notify = False
        with self._lock:
            self._failures = self.threshold
            now = time.monotonic()
            self._opened_at = now
            if self.notify and (self._last_notified_at is None
                    or (now - self._last_notified_at) >= NOTIFY_COOLDOWN_SECS):
                self._last_notified_at = now
                should_notify = True
        logger.warning(f"Circuit breaker TRIPPED for {self.name}")
        if should_notify:
            try:
                _notify_open(self.name)
            except Exception as e:
                logger.debug("Circuit breaker trip-notify raised (swallowed): %s", e)

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    @property
    def is_tripped(self) -> bool:
        """Read-only open check — unlike is_open(), never releases a half-open
        probe (no side effects). For status/alerting reads."""
        with self._lock:
            return self._failures >= self.threshold
