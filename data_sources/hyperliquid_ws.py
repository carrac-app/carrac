"""
Hyperliquid WebSocket worker — keeps HL_COINS hourly cache fresh.

Subscribes to 1h candle streams for
every coin in HL_COINS and writes deltas into csv_cache so that:
  - _inject_live_candle (database.py) picks up fresh 1h bars
  - The scanner sees real-time prices for HL coins
  - The morning report uses up-to-date HL data

Lifecycle:
  - Started from create_app() (and worker.py) during init, gated by HYPERLIQUID_WS_ENABLED env var
  - File-locked so only one Flask worker (the lock holder) spawns this
  - Runs in a daemon thread — dies when the process dies
  - Reconnect with exponential backoff (1s → 30s)

Feature flags:
  - HYPERLIQUID_WS_ENABLED=true  — start the WS worker (default: off)
  - HYPERLIQUID_DISABLED=1       — kill switch; if set, WS worker exits immediately
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger('carrac.data_sources.hyperliquid_ws')


class HyperliquidWSWorker:
    """WebSocket worker that streams HL 1h candles into csv_cache."""

    URL = "wss://api.hyperliquid.xyz/ws"
    SUB_INTERVAL = "1h"
    MAX_BACKOFF_SECONDS = 30
    # WARNING: If no message arrives for any coin in this many seconds,
    # healthcheck flags it as stale. The error tracker or monitoring should alert.
    HEALTHCHECK_STALE_THRESHOLD = 300  # 5 min
    # Trim 1h cache to this many rows to prevent unbounded growth
    MAX_CACHE_ROWS = 25000
    # Throttle CSV writes — chart gets live prices from client-side WS,
    # so server-side writes only need to keep the scanner/precompute (60s) fresh.
    # This reduces disk I/O from ~12 writes/sec to ~1-2 writes/min.
    WRITE_INTERVAL = 10  # seconds per coin
    # Shared health state file — readable by any gunicorn worker's healthcheck route
    HEALTH_FILE = Path('/tmp/.hl_ws_health.json')

    def __init__(self, csv_cache, hl_coins: set, k_prefix_translation: dict):
        """
        Args:
            csv_cache: CSVCache instance (from csv_cache.py).
            hl_coins: Set of uppercase coin symbols from HyperliquidClient.HL_COINS.
            k_prefix_translation: Mapping from uppercase to HL API names (e.g. KPEPE → kPEPE).
        """
        self._cache = csv_cache
        self._coins = hl_coins
        self._k_translate = k_prefix_translation
        # Reverse: kPEPE → KPEPE for incoming messages
        self._k_reverse = {v: k for k, v in k_prefix_translation.items()}

        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_message_ts: dict = {}  # coin (uppercase) → time.time()
        self._last_write_ts: dict = {}   # coin → time.time() of last CSV write
        self._candle_buffer: dict = {}   # coin → latest candle dict (for throttled writes)
        self._reconnect_count = 0
        self._connected = False

    # ========================================
    # Lifecycle
    # ========================================

    def start(self):
        """Spawn daemon thread. Idempotent — does nothing if already running."""
        if self._thread and self._thread.is_alive():
            logger.warning("HL WS worker already running")
            return

        # Kill switch check
        if os.environ.get('HYPERLIQUID_DISABLED', '').lower() in ('1', 'true', 'yes'):
            logger.info("HL WS worker skipped — HYPERLIQUID_DISABLED is set")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_forever,
            daemon=True,
            name="HyperliquidWSWorker",
        )
        self._thread.start()
        logger.info(f"HL WS worker started ({len(self._coins)} coins, interval={self.SUB_INTERVAL})")

    def stop(self):
        """Signal the worker to stop and close the WS connection."""
        self._stop_event.set()
        self._flush_all_buffers()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("HL WS worker stopped")

    # ========================================
    # Main loop
    # ========================================

    def _flush_all_buffers(self):
        """Flush any buffered candles to cache (e.g. before disconnect/shutdown)."""
        for coin in list(self._candle_buffer.keys()):
            self._flush_candle(coin)

    def _run_forever(self):
        """Reconnect loop with exponential backoff."""
        while not self._stop_event.is_set():
            try:
                self._connect_and_run()
            except Exception as e:
                self._connected = False
                self._flush_all_buffers()
                self._write_health_state()
                if self._stop_event.is_set():
                    break
                backoff = min(2 ** self._reconnect_count, self.MAX_BACKOFF_SECONDS)
                logger.warning(f"HL WS disconnected: {e}. Reconnecting in {backoff}s "
                               f"(attempt #{self._reconnect_count + 1})")
                self._reconnect_count += 1
                self._stop_event.wait(timeout=backoff)

        self._connected = False
        logger.info("HL WS worker loop exited")

    def _connect_and_run(self):
        """Connect, subscribe, and recv in a blocking loop."""
        # Lazy import — websocket-client may not be installed (older images)
        try:
            import websocket
        except ImportError:
            logger.error("HL WS worker: websocket-client not installed. "
                         "Add 'websocket-client>=1.7.0' to requirements.txt and rebuild.")
            self._stop_event.set()
            return

        logger.info(f"HL WS connecting to {self.URL}...")
        self._ws = websocket.create_connection(
            self.URL,
            timeout=30,
            enable_multithread=True,
        )
        self._connected = True
        logger.info("HL WS connected")

        # Subscribe to candle streams for all coins
        for coin_upper in sorted(self._coins):
            api_name = self._k_translate.get(coin_upper, coin_upper)
            sub = {
                "method": "subscribe",
                "subscription": {
                    "type": "candle",
                    "coin": api_name,
                    "interval": self.SUB_INTERVAL,
                },
            }
            self._ws.send(json.dumps(sub))
            logger.debug(f"HL WS subscribed: {api_name} {self.SUB_INTERVAL}")

        # Reset backoff on successful connect + subscribe
        self._reconnect_count = 0
        logger.info(f"HL WS subscribed to {len(self._coins)} coins")
        self._write_health_state()

        # Blocking recv loop
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
        """Parse a WS message and route to _on_candle if it's a candle update."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(f"HL WS: unparseable message (len={len(raw)})")
            return

        channel = msg.get('channel')
        if channel != 'candle':
            return  # subscription ack, heartbeat, etc. — ignore

        data = msg.get('data')
        if not data:
            return

        # HL sends candle data as a dict (single candle), not a list
        candles = [data] if isinstance(data, dict) else data
        for candle in candles:
            coin_api = candle.get('s', '')
            # Translate k-prefix back to uppercase storage form
            coin_upper = self._k_reverse.get(coin_api, coin_api.upper())

            if coin_upper not in self._coins:
                continue  # stray message for a coin we don't track

            self._on_candle(coin_upper, candle)

    def _on_candle(self, coin_upper: str, candle: dict):
        """Merge a single candle into the 1h csv_cache for this coin.

        Throttled: buffers candles in memory and writes to CSV at most once
        per WRITE_INTERVAL seconds per coin. This keeps disk I/O manageable
        now that the browser handles live chart ticking via client-side WS.
        """
        try:
            if float(candle['c']) <= 0:
                return  # bad data — skip

            self._last_message_ts[coin_upper] = time.time()
            self._candle_buffer[coin_upper] = candle

            # Throttle: only flush to CSV every WRITE_INTERVAL seconds
            now = time.time()
            last_write = self._last_write_ts.get(coin_upper, 0)
            if now - last_write < self.WRITE_INTERVAL:
                return  # buffered, will flush later

            self._flush_candle(coin_upper)

        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"HL WS: bad candle for {coin_upper}: {e}")

    def _flush_candle(self, coin_upper: str):
        """Write the buffered candle to csv_cache."""
        candle = self._candle_buffer.pop(coin_upper, None)
        if not candle:
            return

        try:
            ts = pd.Timestamp(int(candle['t']), unit='ms')
            new_row = pd.DataFrame({
                'open': [float(candle['o'])],
                'high': [float(candle['h'])],
                'low': [float(candle['l'])],
                'close': [float(candle['c'])],
                'volume': [float(candle['v'])],
            }, index=pd.DatetimeIndex([ts]))

            existing = self._cache.load(coin_upper, '1h')
            if existing is not None and not existing.empty:
                df = pd.concat([existing, new_row])
                df = df[~df.index.duplicated(keep='last')].sort_index()
                if len(df) > self.MAX_CACHE_ROWS:
                    df = df.tail(self.MAX_CACHE_ROWS)
            else:
                df = new_row

            self._cache.save(coin_upper, df, '1h')
            self._last_write_ts[coin_upper] = time.time()
            self._write_health_state()

        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"HL WS: flush failed for {coin_upper}: {e}")

    # ========================================
    # Healthcheck
    # ========================================

    def healthcheck(self) -> dict:
        """Return health status for the /api/health/hyperliquid-ws endpoint."""
        now = time.time()
        stale = []
        oldest_age = 0

        for coin in sorted(self._coins):
            last = self._last_message_ts.get(coin)
            if last is None:
                stale.append(coin)
                continue
            age = now - last
            if age > self.HEALTHCHECK_STALE_THRESHOLD:
                stale.append(coin)
            oldest_age = max(oldest_age, age)

        return {
            'enabled': True,
            'connected': self._connected,
            'reconnect_count': self._reconnect_count,
            'subscribed_coins': len(self._coins),
            'stale_coins': stale,
            'oldest_bar_age_seconds': round(oldest_age, 1),
            'thread_alive': self._thread.is_alive() if self._thread else False,
        }

    def _write_health_state(self):
        """Write health state to a shared file so any gunicorn worker can read it.

        Called after each candle update and at lifecycle transitions (connect/disconnect).
        Non-lock-holder workers read this via read_health_state() for the healthcheck route.
        Uses atomic write-to-temp-then-rename to avoid partial reads.
        """
        try:
            state = self.healthcheck()
            tmp = self.HEALTH_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(state))
            tmp.rename(self.HEALTH_FILE)
        except Exception:
            pass  # non-critical — stale health state is better than crashing

    @staticmethod
    def read_health_state() -> dict:
        """Read the shared health state file written by the lock-holder's WS worker.

        Called by the healthcheck route in any gunicorn worker — doesn't require
        access to the worker object itself.
        """
        try:
            path = HyperliquidWSWorker.HEALTH_FILE
            if path.exists():
                data = json.loads(path.read_text())
                # Add staleness check — if file is >60s old, the worker may have died
                mtime = path.stat().st_mtime
                if time.time() - mtime > 120:
                    data['warning'] = 'health file stale (>120s old)'
                return data
        except Exception:
            pass
        return {'enabled': False, 'reason': 'no health state file found'}
