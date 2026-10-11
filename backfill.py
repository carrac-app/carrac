"""
Gradual History Backfiller — extends crypto OHLCV CSV caches backward in time.

Runs as a daemon thread inside the single-worker lock (same as cache_manager).
One fetch every BACKFILL_INTERVAL_SECONDS (default 300 = 5 min), cycling through
all (symbol, timeframe) pairs until every symbol reaches maximum available depth.

Progress persisted to data/backfill_progress.json so it survives container restarts.
"""

import json
import os
import threading
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from typing import Optional

from api_rate_limiter import (
    can_call, record_call,
)

logger = logging.getLogger('carrac.backfill')

# How long between individual fetch operations
BACKFILL_INTERVAL = int(os.environ.get('BACKFILL_INTERVAL_SECONDS', '300'))

# After completing all symbols, sleep this long before rechecking
RECHECK_INTERVAL = 7 * 24 * 3600  # 1 week

# Bar caps — must match the raised caps in database.py
DAILY_CAP = 5000
HOURLY_CAP = 25000

# Priority symbols (most-viewed, fetched first)
PRIORITY_SYMBOLS = ['BTC', 'ETH', 'SOL', 'XRP']


class HistoryBackfiller:
    """Background thread that gradually extends CSV history backward."""

    def __init__(self, db):
        """
        Args:
            db: OHLCVDatabase instance (provides cache I/O, symbol maps, API keys)
        """
        self.db = db
        self.data_dir = db.cache_dir
        self.progress_path = self.data_dir / 'backfill_progress.json'
        self.progress = self._load_progress()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Launch the backfill daemon thread."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name='HistoryBackfiller',
        )
        self._thread.start()
        logger.info("Backfill thread started (interval=%ds)", BACKFILL_INTERVAL)

    def stop(self):
        """Signal the thread to stop (for graceful shutdown)."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Progress persistence
    # ------------------------------------------------------------------

    def _load_progress(self) -> dict:
        try:
            if self.progress_path.exists():
                with open(self.progress_path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning("Failed to load backfill progress: %s", e)
        return {'daily': {}, 'hourly': {}}

    def _save_progress(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.progress['last_updated'] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            tmp = self.progress_path.with_suffix('.tmp')
            with open(tmp, 'w') as f:
                json.dump(self.progress, f, indent=2)
            tmp.replace(self.progress_path)
        except Exception as e:
            logger.warning("Failed to save backfill progress: %s", e)

    # ------------------------------------------------------------------
    # Work queue
    # ------------------------------------------------------------------

    def _get_crypto_symbols(self) -> list:
        """Return all crypto symbols sorted with priority symbols first."""
        all_syms = sorted(self.db.crypto_map.keys())
        # Exclude crypto metrics (BTC.D, TOTAL, etc.) — they have no Binance/CC history
        # Also exclude ratio symbols
        result = [s for s in all_syms if '.' not in s]

        # Move priority symbols to front
        ordered = []
        for sym in PRIORITY_SYMBOLS:
            if sym in result:
                ordered.append(sym)
                result.remove(sym)
        ordered.extend(result)
        return ordered

    def _is_binance_symbol(self, symbol: str) -> bool:
        return symbol.upper() not in self.db.BINANCE_UNAVAILABLE

    def _next_work_item(self) -> Optional[tuple]:
        """Find next (symbol, timeframe) that still needs backfill.

        Order: all daily first (more visual impact), then all hourly.
        Within each: priority symbols first, then alphabetical.
        """
        symbols = self._get_crypto_symbols()

        for tf in ('daily', '1h'):
            tf_key = tf if tf == 'daily' else 'hourly'
            for sym in symbols:
                entry = self.progress.get(tf_key, {}).get(sym, {})
                if entry.get('status') not in ('done', 'waiting'):
                    return (sym, tf, tf_key)

        return None  # All done

    # ------------------------------------------------------------------
    # Fetch logic
    # ------------------------------------------------------------------

    def _fetch_binance_backward(self, symbol: str, timeframe: str,
                                oldest_ts_ms: int) -> pd.DataFrame:
        """Fetch up to 1000 bars from Binance ending before oldest_ts_ms."""
        allowed, reason = can_call('binance')
        if not allowed:
            logger.debug("Backfill: Binance rate limit (%s), skipping", reason)
            return pd.DataFrame()

        binance_symbol = self.db.BINANCE_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        pair = f"{binance_symbol}USDT"

        interval = '1d' if timeframe == 'daily' else '1h'
        # endTime is exclusive in Binance API — use oldest_ts - 1ms
        end_time = oldest_ts_ms - 1

        url = "https://data-api.binance.vision/api/v3/klines"
        params = {
            'symbol': pair,
            'interval': interval,
            'endTime': end_time,
            'limit': 1000,
        }

        try:
            response = requests.get(url, params=params, timeout=30)
            record_call('binance')

            if response.status_code == 400:
                # Try USD pair
                params['symbol'] = f"{binance_symbol}USD"
                response = requests.get(url, params=params, timeout=30)
                record_call('binance')
                if response.status_code != 200:
                    return pd.DataFrame()

            response.raise_for_status()
            candles = response.json()

            if not candles:
                return pd.DataFrame()

            df = pd.DataFrame(candles, columns=[
                'open_time', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore',
            ])
            df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms')
            df.set_index('timestamp', inplace=True)

            cols = ['open', 'high', 'low', 'close', 'volume']
            if 'taker_buy_base' in df.columns:
                cols.append('taker_buy_base')
            df = df[cols].astype(float)

            df = df[df['close'] > 0]
            df = df[~df.index.duplicated(keep='last')]
            df = df.sort_index()
            return df

        except Exception as e:
            logger.warning("Backfill Binance error for %s/%s: %s", symbol, timeframe, e)
            return pd.DataFrame()


    def _backfill_one(self, symbol: str, timeframe: str, tf_key: str) -> bool:
        """Execute one backfill fetch for (symbol, timeframe).

        Returns True if marked done (no more data), False if more to fetch.
        """
        # Load existing CSV
        csv_tf = timeframe  # 'daily' or '1h'
        cached_df = self.db._load_cached_data(symbol, timeframe=csv_tf)

        if cached_df is None or cached_df.empty:
            # No CSV yet — skip, let normal delta flow create it first
            logger.debug("Backfill: no CSV for %s/%s, skipping (needs initial fetch)", symbol, csv_tf)
            self._update_progress(tf_key, symbol, 'waiting', None, 0)
            return True  # Mark done for now, will recheck later

        oldest_ts = cached_df.index[0]
        oldest_ts_ms = int(oldest_ts.timestamp() * 1000)
        bars_before = len(cached_df)

        # Choose source
        if self._is_binance_symbol(symbol):
            new_df = self._fetch_binance_backward(symbol, timeframe, oldest_ts_ms)
        else:
            # There is no permitted non-Binance deep-history source
            # (CryptoCompare is a barred vendor). Say so and stop at
            # the current depth rather than silently pretending completeness.
            logger.info("Backfill: no permitted deep-history source for %s/%s "
                        "(CryptoCompare removed) — keeping current "
                        "depth of %d bars", symbol, timeframe, bars_before)
            new_df = pd.DataFrame()

        if new_df.empty:
            # No more historical data available
            logger.info("Backfill DONE: %s/%s — %d bars total (oldest: %s)",
                        symbol, csv_tf, bars_before, oldest_ts.date())
            self._update_progress(tf_key, symbol, 'done', str(oldest_ts.date()), bars_before)
            return True

        # Filter: only keep bars strictly before our current oldest
        new_df = new_df[new_df.index < oldest_ts]

        if new_df.empty:
            # All returned data overlaps — we've reached the beginning
            logger.info("Backfill DONE (overlap): %s/%s — %d bars (oldest: %s)",
                        symbol, csv_tf, bars_before, oldest_ts.date())
            self._update_progress(tf_key, symbol, 'done', str(oldest_ts.date()), bars_before)
            return True

        # Prepend new data
        combined = pd.concat([new_df, cached_df])
        combined = combined[~combined.index.duplicated(keep='last')]
        combined = combined.sort_index()

        # Apply bar cap
        cap = DAILY_CAP if timeframe == 'daily' else HOURLY_CAP
        if len(combined) > cap:
            combined = combined.tail(cap)

        # Save
        self.db._save_to_cache(symbol, combined, timeframe=csv_tf)

        new_oldest = combined.index[0]
        added = len(combined) - bars_before
        logger.info("Backfill: %s/%s +%d bars (now %d, oldest: %s)",
                    symbol, csv_tf, added, len(combined), new_oldest.date())

        if added <= 0:
            # Hit the bar cap — no room for more history
            logger.info("Backfill DONE (cap reached): %s/%s — %d bars (oldest: %s)",
                        symbol, csv_tf, len(combined), new_oldest.date())
            self._update_progress(tf_key, symbol, 'done',
                                  str(new_oldest.date()), len(combined))
            return True

        self._update_progress(tf_key, symbol, 'pending',
                              str(new_oldest.date()), len(combined))
        return False

    def _update_progress(self, tf_key: str, symbol: str,
                         status: str, oldest_bar, total_bars: int):
        if tf_key not in self.progress:
            self.progress[tf_key] = {}
        self.progress[tf_key][symbol] = {
            'status': status,
            'oldest_bar': oldest_bar,
            'total_bars': total_bars,
        }
        self._save_progress()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run(self):
        """Main daemon loop — one fetch per interval."""
        # Initial delay to let startup warmup finish first
        logger.info("Backfill: waiting 60s for startup warmup...")
        if self._stop_event.wait(60):
            return

        while not self._stop_event.is_set():
            try:
                work = self._next_work_item()

                if work is None:
                    # All symbols done — sleep and recheck (new symbols may appear)
                    logger.info("Backfill: all symbols complete. Rechecking in 1 week.")
                    # Reset all 'waiting' entries so they get retried
                    for tf_key in ('daily', 'hourly'):
                        for sym, entry in self.progress.get(tf_key, {}).items():
                            if entry.get('status') == 'waiting':
                                entry['status'] = 'pending'
                    self._save_progress()

                    if self._stop_event.wait(RECHECK_INTERVAL):
                        return
                    # Reload progress in case it was edited externally
                    self.progress = self._load_progress()
                    continue

                symbol, timeframe, tf_key = work
                logger.debug("Backfill tick: %s/%s", symbol, timeframe)
                self._backfill_one(symbol, timeframe, tf_key)

            except Exception as e:
                logger.error("Backfill error: %s", e, exc_info=True)

            # Wait for next interval
            if self._stop_event.wait(BACKFILL_INTERVAL):
                return

        logger.info("Backfill thread stopped.")
