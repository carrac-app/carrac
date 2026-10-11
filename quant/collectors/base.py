"""
Base collector class for Quant DB data collection.

All collectors inherit from BaseCollector, which provides:
- Structured logging to quant.collection_log
- Idempotent upserts (INSERT ON CONFLICT)
- Rate limit awareness
- Dry-run mode
- Backfill support with --backfill and --backfill-start flags
"""

import argparse
import json
import logging
import os
import random
import re
import signal
import threading
import time as _time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from carrac.db_pool import get_quant_conn as get_conn

try:
    from psycopg2 import Error as _DBError
except ImportError:
    _DBError = Exception  # type: ignore[misc,assignment]

logger = logging.getLogger('quant.collectors')


class _CollectorBreaker:
    """Lightweight circuit breaker for collectors (thread-safe)."""

    def __init__(self, name: str, threshold: int = 3):
        self.name = name
        self.threshold = threshold
        self._lock = threading.Lock()
        self._failures = 0

    def is_open(self) -> bool:
        with self._lock:
            return self._failures >= self.threshold

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                logger.warning(f"Circuit breaker OPEN for {self.name} ({self._failures} failures)")

    def reset(self):
        with self._lock:
            self._failures = 0


class BaseCollector:
    """Base class for all Quant DB collectors."""

    name: str = 'base'  # Override in subclass
    expected_items: List[str] = []  # Override in subclass for completeness tracking
    expected_items_key: str = 'asset'  # Row key to track ('asset' or 'metric')

    # Exceptions that should NOT be retried (non-transient)
    _NO_RETRY = (KeyboardInterrupt, SystemExit, ValueError, TypeError)

    # Circuit breakers: collector_name → (CircuitBreaker, last_failure_time)
    _breakers: dict = {}
    _BREAKER_COOLDOWN = 86400  # 24h auto-reset

    @classmethod
    def _get_breaker(cls, name: str):
        """Get or create a circuit breaker for a collector, with 24h auto-reset."""
        if name not in cls._breakers:
            cls._breakers[name] = (_CollectorBreaker(name, threshold=3), 0.0)
        breaker, last_fail = cls._breakers[name]
        # Auto-reset after 24h cooldown
        if breaker.is_open() and last_fail and (_time.time() - last_fail > cls._BREAKER_COOLDOWN):
            logger.info(f"[{name}] Circuit breaker auto-reset after 24h cooldown")
            breaker.reset()
            cls._breakers[name] = (breaker, 0.0)
        return breaker

    def __init__(self, dry_run: bool = False, backfill: bool = False,
                 backfill_start: Optional[str] = None,
                 max_retries: int = 3):
        self.dry_run = dry_run
        self.backfill = backfill
        self.backfill_start = backfill_start  # YYYY-MM-DD
        self.max_retries = max_retries
        self._rows_upserted = 0
        self._log_id = None
        self._collected_items: set = set()

    def collect(self):
        """Override in subclass. Fetch and store data."""
        raise NotImplementedError

    def run(self):
        """Execute the collector with health tracking and retry."""
        # Circuit breaker check — skip if too many consecutive failures
        breaker = self._get_breaker(self.name)
        if breaker and breaker.is_open():
            logger.warning(f"[{self.name}] Circuit breaker OPEN — skipping collection")
            self._log_start()
            self._log_finish('skipped', 'Circuit breaker open (3+ consecutive failures)')
            return

        self._log_start()

        # Install signal handlers for graceful shutdown
        prev_term = signal.signal(signal.SIGTERM, self._handle_signal)
        prev_int = signal.signal(signal.SIGINT, self._handle_signal)

        try:
            for attempt in range(self.max_retries + 1):
                try:
                    if attempt > 0:
                        self._rows_upserted = 0  # Reset for idempotent re-run
                        self._collected_items.clear()
                        logger.info(f"[{self.name}] Retry {attempt}/{self.max_retries}")

                    self.collect()
                    self._log_finish('success')
                    logger.info(f"[{self.name}] Done -- {self._rows_upserted} rows upserted")
                    if breaker:
                        breaker.reset()
                    return  # Success

                except self._NO_RETRY as e:
                    self._log_finish('error', self._sanitize_error(str(e)))
                    logger.error("[%s] Non-retryable: %s", self.name,
                                 self._sanitize_exc(e))
                    self._send_failure_alert(str(e))
                    if breaker:
                        breaker.record_failure()
                        self._breakers[self.name] = (breaker, _time.time())
                    raise

                except Exception as e:
                    if attempt < self.max_retries:
                        sleep_secs = self._retry_sleep(
                            attempt, self._extract_retry_after(e))
                        logger.warning(
                            "[%s] Attempt %d failed: %s -- retrying in %.1fs",
                            self.name, attempt + 1,
                            self._sanitize_error(str(e)), sleep_secs,
                        )
                        _time.sleep(sleep_secs)
                    else:
                        self._log_finish('error', self._sanitize_error(str(e)))
                        logger.error(
                            "[%s] Failed after %d attempts: %s",
                            self.name, self.max_retries + 1,
                            self._sanitize_exc(e),
                        )
                        self._send_failure_alert(str(e))
                        if breaker:
                            breaker.record_failure()
                            self._breakers[self.name] = (breaker, _time.time())
                        raise
        finally:
            signal.signal(signal.SIGTERM, prev_term)
            signal.signal(signal.SIGINT, prev_int)

    def _handle_signal(self, signum, frame):
        """Handle SIGTERM/SIGINT: log interruption and exit cleanly."""
        sig_name = signal.Signals(signum).name
        logger.warning(f"[{self.name}] Received {sig_name}, shutting down")
        self._log_finish('interrupted', f'Received {sig_name}')
        raise SystemExit(128 + signum)

    # ── Failure alerting ───────────────────────────────────────────

    def _send_failure_alert(self, error_msg: str):
        """Send immediate ntfy notification on collector failure.

        Best-effort: never raises. Rate-limited to 1 alert per collector
        per hour via collection_log check.
        """
        if self.dry_run:
            return

        ntfy_topic = os.environ.get('NTFY_TOPIC', '')
        if not ntfy_topic:
            return

        try:
            if self._recent_error_exists():
                logger.info(f"[{self.name}] Skipping ntfy alert (rate limited)")
                return
        except Exception:
            pass  # If rate-limit check fails, send anyway

        from carrac.notifications import send_ntfy
        sanitized = self._sanitize_error(error_msg)[:200]
        body = (
            f"Collector: {self.name}\n"
            f"Error: {sanitized}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        send_ntfy(
            body,
            title=f'QE Collector FAILED: {self.name}',
            topic=ntfy_topic,
            tags='rotating_light',
            priority='4',
        )

    def _recent_error_exists(self) -> bool:
        """Check if this collector logged another error in the last hour."""
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM quant.collection_log
                    WHERE collector = %s
                      AND status = 'error'
                      AND started_at > NOW() - INTERVAL '1 hour'
                      AND id != %s
                """, (self.name, self._log_id or 0))
                row = cur.fetchone()
                return (row['cnt'] or 0) > 0
        except Exception:
            return False

    # ── Retry helpers ──────────────────────────────────────────────

    @staticmethod
    def _retry_sleep(attempt: int, retry_after: Optional[float] = None) -> float:
        """Compute sleep duration with exponential backoff + jitter.

        Returns seconds to sleep. Respects Retry-After if provided.
        """
        if retry_after is not None and retry_after > 0:
            return min(retry_after, 120)  # Cap at 2 minutes
        return (2 ** (attempt + 1)) + random.uniform(0, 1)

    @staticmethod
    def _extract_retry_after(exc: Exception) -> Optional[float]:
        """Extract Retry-After header value from HTTP 429 responses."""
        try:
            import requests
            if isinstance(exc, requests.exceptions.HTTPError):
                resp = exc.response
                if resp is not None and resp.status_code == 429:
                    ra = resp.headers.get('Retry-After', '')
                    if ra:
                        return float(ra)
        except (ValueError, AttributeError):
            pass
        return None

    def _request(self, url: str, *, params=None, headers=None,
                 timeout: int = 15, max_429_retries: int = 3, **kwargs):
        """HTTP GET with automatic 429 backoff. Drop-in for requests.get().

        Collectors can opt-in by calling self._request() instead of
        requests.get() for transparent rate limit handling.
        """
        import requests
        for attempt in range(max_429_retries + 1):
            resp = requests.get(url, params=params, headers=headers,
                                timeout=timeout, **kwargs)
            if resp.status_code != 429:
                return resp
            ra = resp.headers.get('Retry-After', '')
            try:
                retry_after = float(ra) if ra else None
            except ValueError:
                retry_after = None
            sleep_secs = self._retry_sleep(attempt, retry_after)
            logger.warning(
                f"[{self.name}] 429 on {url} — sleeping {sleep_secs:.1f}s"
            )
            _time.sleep(sleep_secs)
        resp.raise_for_status()  # Raise last 429 as HTTPError
        return resp  # Unreachable but keeps type checkers happy

    def upsert_rows(self, table: str, rows: List[Dict[str, Any]],
                    conflict_cols: List[str],
                    update_cols: Optional[List[str]] = None) -> int:
        """Idempotent upsert: INSERT ... ON CONFLICT DO UPDATE.

        Args:
            table: Full table name (e.g., 'quant.macro')
            rows: List of dicts, each representing a row
            conflict_cols: Columns forming the unique constraint
            update_cols: Columns to update on conflict (default: all non-conflict)

        Returns:
            Number of rows affected.
        """
        if not rows:
            return 0

        if self.dry_run:
            logger.info(f"[{self.name}] DRY RUN: would upsert {len(rows)} rows into {table}")
            return len(rows)

        all_cols = list(rows[0].keys())
        if update_cols is None:
            update_cols = [c for c in all_cols if c not in conflict_cols]

        col_names = ', '.join(all_cols)
        conflict = ', '.join(conflict_cols)

        if update_cols:
            updates = ', '.join(f'{c} = EXCLUDED.{c}' for c in update_cols)
            sql = f"""
                INSERT INTO {table} ({col_names})
                VALUES %s
                ON CONFLICT ({conflict}) DO UPDATE SET {updates}
            """
        else:
            sql = f"""
                INSERT INTO {table} ({col_names})
                VALUES %s
                ON CONFLICT ({conflict}) DO NOTHING
            """

        from psycopg2.extras import execute_values

        tuple_rows = [tuple(row[c] for c in all_cols) for row in rows]

        # Track collected items for completeness checks
        if self.expected_items:
            for row in rows:
                item = row.get(self.expected_items_key, '')
                if item:
                    self._collected_items.add(item)

        with get_conn() as conn:
            cur = conn.cursor()
            execute_values(cur, sql, tuple_rows, page_size=1000)
            count = len(tuple_rows)
            self._rows_upserted += count

        return count

    def _log_start(self):
        """Record collection start in health log."""
        if self.dry_run:
            return
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO quant.collection_log (collector, status)
                    VALUES (%s, 'running')
                    RETURNING id
                """, (self.name,))
                row = cur.fetchone()
                self._log_id = row['id'] if row else None
        except (_DBError, OSError) as e:
            logger.warning(f"Failed to log collection start: {e}")

    def _log_finish(self, status: str, error_msg: str = None):
        """Record collection result in health log."""
        if self.dry_run or not self._log_id:
            return
        completeness = self._track_completeness() if status == 'success' else None
        metadata_json = json.dumps(completeness) if completeness else None
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE quant.collection_log
                    SET finished_at = NOW(), status = %s,
                        rows_upserted = %s, error_msg = %s,
                        metadata = %s::jsonb
                    WHERE id = %s
                """, (status, self._rows_upserted, error_msg,
                      metadata_json, self._log_id))
        except (_DBError, OSError) as e:
            logger.warning(f"Failed to log collection finish: {e}")

    def _track_completeness(self) -> Optional[Dict[str, Any]]:
        """Compare expected vs actual items collected."""
        if not self.expected_items or self.dry_run:
            return None
        expected_set = set(self.expected_items)
        actual_set = self._collected_items & expected_set
        missing = sorted(expected_set - self._collected_items)
        return {
            'expected': len(expected_set),
            'actual': len(actual_set),
            'missing': missing,
        }

    @staticmethod
    def _sanitize_exc(exc: BaseException) -> str:
        """Formatted traceback with secrets removed — safe to log.

        `logger.error(..., exc_info=True)` renders the exception's own str()
        as the traceback's last line, and `requests` puts the full request URL
        there. For a collector whose credential travels as a query parameter
        that publishes the key into the logfile on every failure (seen with the
        Finnhub economic-calendar token, in plaintext, once a day).

        So the traceback is formatted, sanitized, and logged as text rather
        than handed to exc_info. Same frames, no key.
        """
        import traceback
        return BaseCollector._sanitize_error(
            ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    @staticmethod
    def _sanitize_error(msg: str) -> str:
        """Remove secrets from an error message.

        Applied to DB storage, ntfy alerts AND log lines. The log lines were
        the gap — this existed and was tested, and the three logger calls in
        the retry loop passed the raw exception straight past it.
        """
        if not msg:
            return msg
        for name, value in os.environ.items():
            if not value or len(value) < 8:
                continue
            if any(s in name.upper() for s in ('KEY', 'SECRET', 'TOKEN', 'PASSWORD', 'DSN')):
                msg = msg.replace(value, '***')
        msg = re.sub(
            r'([?&])(api_key|key|token|secret|password)=[^&\s]+',
            r'\1\2=***', msg, flags=re.IGNORECASE,
        )
        if len(msg) > 500:
            msg = msg[:500] + '...[truncated]'
        return msg

    @staticmethod
    def parse_args(description: str = ''):
        """Common CLI argument parser for all collectors."""
        parser = argparse.ArgumentParser(description=description)
        parser.add_argument('--dry-run', action='store_true',
                            help='Log actions without writing to DB')
        parser.add_argument('--backfill', action='store_true',
                            help='Backfill historical data')
        parser.add_argument('--backfill-start', type=str, default=None,
                            help='Backfill start date (YYYY-MM-DD)')
        parser.add_argument('--max-retries', type=int, default=3,
                            help='Max retry attempts on transient failure (default: 3)')
        return parser.parse_args()
