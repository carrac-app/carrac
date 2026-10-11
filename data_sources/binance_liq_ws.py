"""
Binance USDT-futures forced-liquidation WebSocket consumer.

Streams the public, unauthenticated `!forceOrder@arr` combined stream —
every forced liquidation order across all USDT-M futures symbols — and:

  1. Keeps a 60-minute, memory-bounded rolling window of liquidation
     notional per symbol, and writes a summary to Redis (`mcd:liq:summary`)
     every ~10s (heartbeat even when the market is quiet) for the Radar
     surface to read.
  2. Accumulates same-UTC-day totals per symbol and upserts them into
     `quant.derivatives.liquidations_long` / `liquidations_short` every
     ~10 minutes, mirroring the partial-column upsert pattern used by
     `quant/collectors/binance_daily.py`.

Side semantics (Binance forceOrder payload, field `S`):
  - S == "SELL" -> the liquidation order SOLD the position -> a LONG was
    force-closed -> counts toward the LONG liquidation bucket.
  - S == "BUY"  -> the liquidation order BOUGHT to close -> a SHORT was
    force-closed -> counts toward the SHORT liquidation bucket.
(This is the standard "the forced order is opposite the liquidated side"
convention used by every Binance liquidation dashboard.)

Lifecycle:
  - Started from worker.py as a daemon thread, gated by LIQ_WS_ENABLED
    (default ON — set to '0'/'false'/'no' to disable).
  - Reconnects with capped exponential backoff (1s -> 30s) on any drop.
  - A second daemon thread (`_summary_loop`) periodically writes the Redis
    summary and flushes the daily quant.derivatives upsert — decoupled from
    message arrival so the heartbeat still fires when the stream is quiet.
  - Never raises out of start()/stop() — a total failure here must not take
    down the compute worker.

No new dependency: reuses `websocket-client` (already in requirements.txt,
same library data_sources/hyperliquid_ws.py and data_sources/finnhub_ws.py
use) and the existing Redis singleton (carrac.redis_cache.get_redis).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger('carrac.data_sources.binance_liq_ws')


class BinanceLiqWSWorker:
    """WebSocket worker that streams Binance forced liquidations into Redis + quant.derivatives."""

    # Routed /market endpoint: liquidation streams are /market-class; legacy
    # unrouted /ws/ connections complete the handshake but push NO data for
    # them, which looks like silent zero-event operation.
    URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"

    MAX_BACKOFF_SECONDS = 30

    # In-memory rolling window kept per symbol.
    WINDOW_SECONDS = 3600      # 60 min — everything older is pruned
    M15_SECONDS = 900          # 15 min sub-window reported alongside h1
    MAX_EVENTS_PER_SYMBOL = 5000  # hard cap regardless of time window (memory bound)

    SUMMARY_INTERVAL = 10          # seconds between mcd:liq:summary writes
    DAILY_UPSERT_INTERVAL = 600    # seconds between quant.derivatives upserts (~10 min)
    LOG_INTERVAL = 300              # seconds between rate-limited INFO stat lines

    # per-event ring-buffer persistence (quant.liquidation_events).
    # Individual events were never durable anywhere before this — the daily
    # totals above are an aggregate, and Binance has no historical REST
    # endpoint for past liquidation orders — so the liq-heatmap validation
    # harness (scripts/validate_liq_heatmap.py) needs this table to replay
    # against. Flushed in batches (not per-event) to bound DB write volume
    # during a liquidation cascade.
    EVENTS_FLUSH_INTERVAL = 30      # seconds between quant.liquidation_events flushes
    EVENTS_RETENTION_DAYS = 14      # pruned alongside the daily upsert cycle
    MAX_PENDING_EVENTS = 20000      # hard cap on the in-memory flush buffer

    REDIS_KEY = 'mcd:liq:summary'
    REDIS_TTL = 180  # > 3x SUMMARY_INTERVAL heartbeat; self-heals if the worker dies

    # Assets the daily quant.derivatives upsert persists for. Must match the
    # ASSETS convention in quant/collectors/binance_daily.py &
    # quant/collectors/ccxt_funding.py (top-10-by-futures-volume quant target
    # universe) — the Redis summary above is NOT filtered this way and
    # covers every symbol resolvable via the Binance perp -> dash mapping.
    TARGET_ASSETS = frozenset({
        'BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'DOGE', 'ADA', 'AVAX', 'LINK', 'DOT',
    })

    def __init__(self, symbol_map: Optional[Dict[str, str]] = None):
        """
        Args:
            symbol_map: Binance perp symbol (e.g. 'BTCUSDT') -> Carrac dash
                symbol (e.g. 'BTC'). Defaults to the reverse of
                data_sources.binance.BinanceClient.PERP_MAP. Injectable for
                tests.
        """
        if symbol_map is None:
            from data_sources.binance import BinanceClient
            symbol_map = {v: k for k, v in BinanceClient.PERP_MAP.items()}
        self._symbol_map = symbol_map

        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._summary_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # symbol -> deque of (event_ts_seconds, 'long'|'short', notional_usd)
        self._events: Dict[str, Deque[Tuple[float, str, float]]] = {}
        # (utc_date_str, symbol) -> {'liquidations_long': float, 'liquidations_short': float}
        self._daily_totals: Dict[Tuple[str, str], Dict[str, float]] = {}
        self._last_message_ts: Dict[str, float] = {}
        # Pending rows for the next quant.liquidation_events batch flush:
        # (ts_datetime_utc, asset, side, price, qty, notional_usd)
        self._pending_events: List[Tuple] = []

        self._reconnect_count = 0
        self._connected = False

        self._event_count_since_log = 0
        self._last_log_ts = time.time()

    # ========================================
    # Lifecycle
    # ========================================

    def start(self):
        """Spawn the recv + summary daemon threads. Idempotent."""
        if self._thread and self._thread.is_alive():
            logger.warning("Binance Liq WS worker already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_forever, daemon=True, name="BinanceLiqWSWorker",
        )
        self._thread.start()

        self._summary_thread = threading.Thread(
            target=self._summary_loop, daemon=True, name="BinanceLiqWSSummary",
        )
        self._summary_thread.start()

        logger.info("Binance Liq WS worker started")

    def stop(self):
        """Signal both threads to stop, flush final state, and join."""
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        if self._summary_thread:
            self._summary_thread.join(timeout=5)
        try:
            self._upsert_daily_totals()
        except Exception as e:
            logger.warning(f"Binance Liq WS: final daily upsert failed: {e}")
        try:
            self._flush_events_to_db()
        except Exception as e:
            logger.warning(f"Binance Liq WS: final events flush failed: {e}")
        self._connected = False
        logger.info("Binance Liq WS worker stopped")

    # ========================================
    # Reconnect loop (recv thread)
    # ========================================

    def _backoff_seconds(self) -> float:
        """Capped exponential backoff based on the current reconnect count."""
        return min(2 ** self._reconnect_count, self.MAX_BACKOFF_SECONDS)

    def _run_forever(self):
        """Reconnect loop with exponential backoff. Never raises."""
        while not self._stop_event.is_set():
            try:
                self._connect_and_run()
            except Exception as e:
                self._connected = False
                if self._stop_event.is_set():
                    break
                backoff = self._backoff_seconds()
                logger.warning(
                    f"Binance Liq WS disconnected: {e}. Reconnecting in {backoff}s "
                    f"(attempt #{self._reconnect_count + 1})"
                )
                self._reconnect_count += 1
                self._stop_event.wait(timeout=backoff)

        self._connected = False
        logger.info("Binance Liq WS loop exited")

    def _connect_and_run(self):
        """Connect to the raw combined stream and recv in a blocking loop.

        No subscribe message is needed — `!forceOrder@arr` is a raw stream
        URL that starts pushing forceOrder events immediately on connect.
        """
        # Lazy import — mirrors data_sources/hyperliquid_ws.py so a Phase-1
        # image missing websocket-client degrades gracefully instead of
        # crashing the whole worker process.
        try:
            import websocket
        except ImportError:
            logger.error(
                "Binance Liq WS: websocket-client not installed. "
                "Add 'websocket-client>=1.7.0' to requirements.txt and rebuild."
            )
            self._stop_event.set()
            return

        logger.info(f"Binance Liq WS connecting to {self.URL}...")
        self._ws = websocket.create_connection(
            self.URL, timeout=30, enable_multithread=True,
        )
        self._connected = True
        self._reconnect_count = 0
        logger.info("Binance Liq WS connected")

        while not self._stop_event.is_set():
            try:
                raw = self._ws.recv()
                if raw:
                    self._handle_message(raw)
            except websocket.WebSocketTimeoutException:
                continue  # recv timeout is normal, just loop
            except (websocket.WebSocketConnectionClosedException, ConnectionError):
                raise  # reconnect

    # ========================================
    # Message handling
    # ========================================

    def _handle_message(self, raw: str):
        """Parse one forceOrder message and fold it into in-memory state.

        Malformed / incomplete / unmapped-symbol messages are dropped
        silently (debug-logged) — never raises.
        """
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.debug(f"Binance Liq WS: unparseable message (len={len(raw) if raw else 0})")
            return

        if not isinstance(msg, dict):
            return
        o = msg.get('o')
        if not isinstance(o, dict):
            return

        try:
            binance_symbol = o['s']
            side = o['S']  # 'SELL' or 'BUY'
            qty = float(o['q'])
            avg_price = float(o['ap'])
            trade_time_ms = int(o['T'])
        except (KeyError, TypeError, ValueError):
            logger.debug("Binance Liq WS: malformed order payload, skipped")
            return

        symbol = self._symbol_map.get(binance_symbol)
        if not symbol:
            return  # unmapped symbol -- drop per contract

        if side == 'SELL':
            bucket = 'long'   # SELL forced order == a LONG position was liquidated
        elif side == 'BUY':
            bucket = 'short'  # BUY forced order == a SHORT position was liquidated
        else:
            logger.debug(f"Binance Liq WS: unknown side {side!r}, skipped")
            return

        notional = qty * avg_price
        if notional <= 0:
            return

        ts = trade_time_ms / 1000.0  # ms -> s

        dq = self._events.get(symbol)
        if dq is None:
            dq = deque(maxlen=self.MAX_EVENTS_PER_SYMBOL)
            self._events[symbol] = dq
        dq.append((ts, bucket, notional))
        self._prune(symbol, time.time())

        self._accumulate_daily(symbol, bucket, notional, ts)
        self._buffer_event(symbol, bucket, avg_price, qty, notional, ts)

        self._last_message_ts[symbol] = time.time()
        self._event_count_since_log += 1

    def _prune(self, symbol: str, now: float):
        """Drop events older than WINDOW_SECONDS from the front of the deque."""
        dq = self._events.get(symbol)
        if not dq:
            return
        cutoff = now - self.WINDOW_SECONDS
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    # ========================================
    # Daily quant.derivatives persistence
    # ========================================

    def _accumulate_daily(self, symbol: str, bucket: str, notional: float, ts: float):
        """Add this event's notional to the running same-UTC-day total.

        Only tracked for TARGET_ASSETS (the quant target universe) — the
        Redis summary is not filtered this way, only this DB persistence.
        """
        if symbol not in self.TARGET_ASSETS:
            return
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
        key = (date_str, symbol)
        entry = self._daily_totals.setdefault(
            key, {'liquidations_long': 0.0, 'liquidations_short': 0.0},
        )
        entry[f'liquidations_{bucket}'] += notional

    def _upsert_daily_totals(self):
        """Idempotent upsert of accumulated daily totals into quant.derivatives.

        Mirrors BinanceDailyCollector._upsert_derivatives in
        quant/collectors/binance_daily.py: partial-column upsert via
        ON CONFLICT (ts, asset) DO UPDATE, keyed by (date, asset) so
        re-running with a larger running total is always safe.
        """
        if not self._daily_totals:
            return

        from carrac.db_pool import get_quant_conn as get_conn

        with get_conn() as conn:
            cur = conn.cursor()
            for (date_str, symbol), totals in list(self._daily_totals.items()):
                cur.execute("""
                    INSERT INTO quant.derivatives (ts, asset, liquidations_long, liquidations_short)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (ts, asset) DO UPDATE SET
                        liquidations_long = EXCLUDED.liquidations_long,
                        liquidations_short = EXCLUDED.liquidations_short
                """, (date_str, symbol, totals['liquidations_long'], totals['liquidations_short']))

        # Bound memory: keep only today + yesterday (covers the UTC-midnight
        # rollover window) — older dates have already been durably upserted.
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
        keep = {today, yesterday}
        for key in list(self._daily_totals.keys()):
            if key[0] not in keep:
                del self._daily_totals[key]

    # ========================================
    # Per-event ring buffer (quant.liquidation_events)
    # ========================================

    def _buffer_event(self, symbol: str, bucket: str, price: float, qty: float,
                       notional: float, ts: float):
        """Queue one event for the next quant.liquidation_events batch flush.

        Only buffered for TARGET_ASSETS — the same universe the liquidation
        heatmap model (carrac/liq_heatmap.py) covers; there is no point
        persisting history for a symbol the heatmap will never build.
        """
        if symbol not in self.TARGET_ASSETS:
            return
        if len(self._pending_events) >= self.MAX_PENDING_EVENTS:
            return  # buffer full — a flush is overdue; drop rather than grow unbounded
        self._pending_events.append((
            datetime.fromtimestamp(ts, tz=timezone.utc), symbol, bucket, price, qty, notional,
        ))

    def _flush_events_to_db(self):
        """Batch-insert every pending event into quant.liquidation_events.

        Batched (not per-event) so a liquidation cascade doesn't turn into a
        DB write storm. Never raises — a failed flush just leaves the
        buffer for the next attempt (bounded by MAX_PENDING_EVENTS above).
        """
        if not self._pending_events:
            return
        from carrac.db_pool import get_quant_conn as get_conn

        batch = self._pending_events
        self._pending_events = []
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.executemany("""
                    INSERT INTO quant.liquidation_events (ts, asset, side, price, qty, notional_usd)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, batch)
        except Exception as e:
            logger.warning(f"Binance Liq WS: events flush failed ({len(batch)} rows): {e}")
            # Re-queue on failure (bounded) rather than silently losing the batch.
            self._pending_events = (batch + self._pending_events)[:self.MAX_PENDING_EVENTS]

    def _prune_old_events(self):
        """Delete rows older than EVENTS_RETENTION_DAYS — the actual 'ring
        buffer' cap (application-enforced, not a DB constraint). Cheap:
        indexed on (asset, ts)."""
        from carrac.db_pool import get_quant_conn as get_conn

        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM quant.liquidation_events WHERE ts < NOW() - (%s || ' days')::interval",
                    (self.EVENTS_RETENTION_DAYS,),
                )
        except Exception as e:
            logger.warning(f"Binance Liq WS: events prune failed: {e}")

    # ========================================
    # Redis summary (mcd:liq:summary)
    # ========================================

    def _compute_summary(self, now: float) -> dict:
        """Build the mcd:liq:summary payload from current in-memory windows.

        A symbol drops out of `symbols` once all of its tracked events age
        out past WINDOW_SECONDS — `updated_at` keeps ticking regardless of
        whether the market is quiet (the required heartbeat).
        """
        symbols_out = {}
        for symbol in list(self._events.keys()):
            self._prune(symbol, now)
            dq = self._events.get(symbol)
            if not dq:
                continue

            m15_long = m15_short = h1_long = h1_short = 0.0
            last_ts = 0.0
            for ts, bucket, notional in dq:
                last_ts = max(last_ts, ts)
                age = now - ts
                if age > self.WINDOW_SECONDS:
                    continue  # shouldn't happen post-prune, defensive only
                if bucket == 'long':
                    h1_long += notional
                else:
                    h1_short += notional
                if age <= self.M15_SECONDS:
                    if bucket == 'long':
                        m15_long += notional
                    else:
                        m15_short += notional

            symbols_out[symbol] = {
                'm15_long_usd': m15_long,
                'm15_short_usd': m15_short,
                'h1_long_usd': h1_long,
                'h1_short_usd': h1_short,
                'last_ts': int(last_ts),
            }

        return {'updated_at': int(now), 'symbols': symbols_out}

    def _write_summary(self, now: float):
        """Write the current summary to Redis. No-op if Redis is unavailable."""
        summary = self._compute_summary(now)
        from carrac.redis_cache import get_redis
        r = get_redis()
        if not r:
            return
        try:
            r.set(self.REDIS_KEY, json.dumps(summary), ex=self.REDIS_TTL)
        except Exception as e:
            logger.debug(f"Binance Liq WS: redis write failed: {e}")

    # ========================================
    # Periodic loop (summary + daily upsert + stats log)
    # ========================================

    def _maybe_log_stats(self, now: float):
        """Rate-limited (~5 min) INFO line with event counts."""
        if now - self._last_log_ts < self.LOG_INTERVAL:
            return
        logger.info(
            f"Binance Liq WS: {self._event_count_since_log} events in last "
            f"{int(self.LOG_INTERVAL // 60)}min, {len(self._events)} symbols tracked, "
            f"reconnects={self._reconnect_count}"
        )
        self._event_count_since_log = 0
        self._last_log_ts = now

    def _summary_loop(self):
        """Periodic writer thread: Redis summary every SUMMARY_INTERVAL,
        events flush every EVENTS_FLUSH_INTERVAL, quant.derivatives upsert
        (+ events prune) every DAILY_UPSERT_INTERVAL. Decoupled from the
        recv thread so both the heartbeat and the DB flush keep firing
        even if the stream connection is down."""
        last_daily_upsert = 0.0
        last_events_flush = 0.0
        while not self._stop_event.is_set():
            now = time.time()
            try:
                self._write_summary(now)
            except Exception as e:
                logger.debug(f"Binance Liq WS: summary loop write failed: {e}")

            if now - last_events_flush >= self.EVENTS_FLUSH_INTERVAL:
                try:
                    self._flush_events_to_db()
                except Exception as e:
                    logger.warning(f"Binance Liq WS: events flush failed: {e}")
                last_events_flush = now

            if now - last_daily_upsert >= self.DAILY_UPSERT_INTERVAL:
                try:
                    self._upsert_daily_totals()
                except Exception as e:
                    logger.warning(f"Binance Liq WS: daily upsert failed: {e}")
                try:
                    self._prune_old_events()
                except Exception as e:
                    logger.warning(f"Binance Liq WS: events prune failed: {e}")
                last_daily_upsert = now

            self._maybe_log_stats(now)
            self._stop_event.wait(timeout=self.SUMMARY_INTERVAL)

        logger.info("Binance Liq WS summary loop exited")

    # ========================================
    # Healthcheck (lightweight, in-process only)
    # ========================================

    def healthcheck(self) -> dict:
        """Return a small health snapshot — no shared file, just in-process state."""
        return {
            'enabled': True,
            'connected': self._connected,
            'reconnect_count': self._reconnect_count,
            'active_symbols': len(self._events),
            'pending_events_buffered': len(self._pending_events),
            'thread_alive': self._thread.is_alive() if self._thread else False,
            'summary_thread_alive': self._summary_thread.is_alive() if self._summary_thread else False,
        }
