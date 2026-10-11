"""
CCXT WebSocket worker — streams 1h candles for auto-discovered coins.

Uses ccxt.pro (async) to subscribe to OHLCV streams on MEXC, Gate.io,
KuCoin, and Bybit. Writes candle updates to csv_cache so the scanner,
chart endpoints, and morning report see near-real-time data.

Lifecycle:
  - Started from create_app() (and worker.py) during init, gated by CCXT_WS_ENABLED env var
  - File-locked so only one Flask worker (the lock holder) spawns this
  - Runs asyncio event loop in a daemon thread
  - Per-exchange reconnect with exponential backoff

Feature flags:
  - CCXT_WS_ENABLED=true    — start the WS worker (default: off)
  - CCXT_DISABLED=1          — kills everything (REST + WS)
"""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional, Dict

import pandas as pd

logger = logging.getLogger('carrac.data_sources.ccxt_ws')


class CcxtWSWorker:
    """WebSocket worker that streams candles for CCXT-routed coins."""

    SUB_INTERVAL = "1h"
    MAX_BACKOFF_SECONDS = 30
    HEALTHCHECK_STALE_THRESHOLD = 300  # 5 min
    MAX_CACHE_ROWS = 25000
    WRITE_INTERVAL = 10  # seconds per coin (throttle CSV writes)
    HEALTH_FILE = Path('/tmp/.ccxt_ws_health.json')

    def __init__(self, csv_cache, ccxt_client):
        """
        Args:
            csv_cache: CSVCache instance.
            ccxt_client: CcxtClient instance (for routing table access).
        """
        self._cache = csv_cache
        self._ccxt_client = ccxt_client
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_message_ts: Dict[str, float] = {}  # coin → time.time()
        self._last_write_ts: Dict[str, float] = {}
        self._candle_buffer: Dict[str, list] = {}  # coin → [ts, o, h, l, c, v]
        self._reconnect_count = 0
        self._connected_exchanges: set = set()
        self._subscribed_count = 0

    # ========================================
    # Lifecycle
    # ========================================

    def start(self):
        """Spawn daemon thread. Idempotent."""
        if self._thread and self._thread.is_alive():
            logger.warning("CCXT WS worker already running")
            return

        if os.environ.get('CCXT_DISABLED', '').lower() in ('1', 'true', 'yes'):
            logger.info("CCXT WS worker skipped — CCXT_DISABLED is set")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_in_thread,
            daemon=True,
            name="CcxtWSWorker",
        )
        self._thread.start()
        logger.info("CCXT WS worker started")

    def stop(self):
        """Signal the worker to stop."""
        self._stop_event.set()
        self._flush_all_buffers()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("CCXT WS worker stopped")

    def _run_in_thread(self):
        """Entry point for the daemon thread — runs the asyncio event loop."""
        try:
            asyncio.run(self._run_forever())
        except Exception as e:
            logger.error(f"CCXT WS worker crashed: {e}")
        finally:
            self._connected_exchanges.clear()
            self._write_health_state()

    # ========================================
    # Async main loop
    # ========================================

    async def _run_forever(self):
        """Main async loop: connect to all exchanges and stream candles."""
        while not self._stop_event.is_set():
            try:
                await self._connect_and_stream()
            except Exception as e:
                self._connected_exchanges.clear()
                self._flush_all_buffers()
                self._write_health_state()
                if self._stop_event.is_set():
                    break
                backoff = min(2 ** self._reconnect_count, self.MAX_BACKOFF_SECONDS)
                logger.warning(f"CCXT WS disconnected: {e}. Reconnecting in {backoff}s "
                               f"(attempt #{self._reconnect_count + 1})")
                self._reconnect_count += 1
                await asyncio.sleep(backoff)

    async def _connect_and_stream(self):
        """Connect to exchanges and stream candles."""
        import ccxt.pro as ccxtpro

        routing = self._ccxt_client.get_routing_table()
        if not routing:
            logger.info("CCXT WS: no routing table yet, waiting 60s...")
            await asyncio.sleep(60)
            return

        # Group symbols by exchange
        exchange_symbols: Dict[str, list] = {}
        for symbol, exchange_id in routing.items():
            env_key = f'CCXT_{exchange_id.upper()}_DISABLED'
            if os.environ.get(env_key, '').lower() in ('1', 'true', 'yes'):
                continue
            exchange_symbols.setdefault(exchange_id, []).append(symbol)

        if not exchange_symbols:
            logger.info("CCXT WS: all exchanges disabled, sleeping 60s...")
            await asyncio.sleep(60)
            return

        # Limit total subscriptions to avoid overwhelming
        MAX_SYMBOLS_PER_EXCHANGE = 50
        for ex_id in exchange_symbols:
            exchange_symbols[ex_id] = exchange_symbols[ex_id][:MAX_SYMBOLS_PER_EXCHANGE]

        total = sum(len(s) for s in exchange_symbols.values())
        logger.info(f"CCXT WS: subscribing to {total} symbols across "
                    f"{len(exchange_symbols)} exchanges")

        # Create async exchange instances
        exchange_classes = {
            'mexc': ccxtpro.mexc,
            'gateio': ccxtpro.gateio,
            'kucoin': ccxtpro.kucoin,
            'bybit': ccxtpro.bybit,
        }

        exchanges = {}
        for ex_id in exchange_symbols:
            cls = exchange_classes.get(ex_id)
            if cls:
                exchanges[ex_id] = cls({'enableRateLimit': True, 'timeout': 30000})

        self._subscribed_count = total
        self._reconnect_count = 0
        self._write_health_state()

        try:
            # Run all exchange watchers concurrently
            tasks = []
            for ex_id, symbols in exchange_symbols.items():
                if ex_id in exchanges:
                    tasks.append(self._watch_exchange(exchanges[ex_id], ex_id, symbols))

            await asyncio.gather(*tasks)
        finally:
            # Close all exchange connections
            for ex in exchanges.values():
                try:
                    await ex.close()
                except Exception:
                    pass

    async def _watch_exchange(self, exchange, exchange_id: str, symbols: list):
        """Watch OHLCV for all symbols on one exchange."""
        pairs_and_tfs = [[f'{s}/USDT', self.SUB_INTERVAL] for s in symbols]
        self._connected_exchanges.add(exchange_id)
        self._write_health_state()
        logger.info(f"CCXT WS/{exchange_id}: watching {len(symbols)} symbols")

        while not self._stop_event.is_set():
            try:
                ohlcvs = await exchange.watch_ohlcv_for_symbols(pairs_and_tfs)
                # ohlcvs is a dict: {'BTC/USDT': {'1h': [[ts, o, h, l, c, v], ...]}}
                for pair, timeframes in ohlcvs.items():
                    base = pair.split('/')[0].upper()
                    for tf, candles in timeframes.items():
                        if candles:
                            latest = candles[-1]  # Most recent candle
                            self._on_candle(base, latest, exchange_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"CCXT WS/{exchange_id} error: {e}")
                self._connected_exchanges.discard(exchange_id)
                self._write_health_state()
                raise  # Let _run_forever handle reconnect

    # ========================================
    # Candle processing (same pattern as HyperliquidWSWorker)
    # ========================================

    def _on_candle(self, coin_upper: str, candle: list, exchange_id: str):
        """Process a candle update. Throttled writes to CSV."""
        try:
            # candle = [timestamp_ms, open, high, low, close, volume]
            if len(candle) < 6 or float(candle[4]) <= 0:
                return

            self._last_message_ts[coin_upper] = time.time()
            self._candle_buffer[coin_upper] = candle

            now = time.time()
            last_write = self._last_write_ts.get(coin_upper, 0)
            if now - last_write < self.WRITE_INTERVAL:
                return

            self._flush_candle(coin_upper)
        except (ValueError, TypeError, IndexError) as e:
            logger.debug(f"CCXT WS: bad candle for {coin_upper}: {e}")

    def _flush_candle(self, coin_upper: str):
        """Write the buffered candle to csv_cache."""
        candle = self._candle_buffer.pop(coin_upper, None)
        if not candle:
            return

        try:
            ts = pd.Timestamp(int(candle[0]), unit='ms')
            new_row = pd.DataFrame({
                'open': [float(candle[1])],
                'high': [float(candle[2])],
                'low': [float(candle[3])],
                'close': [float(candle[4])],
                'volume': [float(candle[5])],
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
        except Exception as e:
            logger.debug(f"CCXT WS: flush failed for {coin_upper}: {e}")

    def _flush_all_buffers(self):
        """Flush all buffered candles before disconnect/shutdown."""
        for coin in list(self._candle_buffer.keys()):
            self._flush_candle(coin)

    # ========================================
    # Healthcheck
    # ========================================

    def healthcheck(self) -> dict:
        """Return health status for the /api/health/ccxt-ws endpoint."""
        now = time.time()
        stale = []
        oldest_age = 0

        for coin, last in self._last_message_ts.items():
            age = now - last
            if age > self.HEALTHCHECK_STALE_THRESHOLD:
                stale.append(coin)
            oldest_age = max(oldest_age, age)

        return {
            'enabled': True,
            'connected_exchanges': sorted(self._connected_exchanges),
            'reconnect_count': self._reconnect_count,
            'subscribed_symbols': self._subscribed_count,
            'active_symbols': len(self._last_message_ts),
            'stale_symbols': stale[:20],  # Cap list size
            'oldest_bar_age_seconds': round(oldest_age, 1),
            'thread_alive': self._thread.is_alive() if self._thread else False,
        }

    def _write_health_state(self):
        """Write health state to shared file (atomic write)."""
        try:
            state = self.healthcheck()
            tmp = self.HEALTH_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(state))
            tmp.rename(self.HEALTH_FILE)
        except Exception:
            pass

    @staticmethod
    def read_health_state() -> dict:
        """Read health state from shared file (any gunicorn worker)."""
        try:
            path = CcxtWSWorker.HEALTH_FILE
            if path.exists():
                data = json.loads(path.read_text())
                mtime = path.stat().st_mtime
                if time.time() - mtime > 120:
                    data['warning'] = 'health file stale (>120s old)'
                return data
        except Exception:
            pass
        return {'enabled': False, 'reason': 'no health state file found'}
