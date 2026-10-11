"""Finnhub trade-stream worker.

Opens a single WebSocket connection to wss://ws.finnhub.io using the
server-side FINNHUB_API_KEY, subscribes to a curated list of US stocks +
ETFs, and writes each symbol's latest tick to Redis so browsers can poll
/api/finnhub/quotes without ever seeing the API key.

Runs inside the compute worker process (see worker.py). Handles
reconnects with exponential backoff.

Free tier is rate-limited to 5 concurrent handshakes/minute. A
naive reconnect loop burns through that budget in seconds and hot-loops
on 429s. We detect `Handshake status 429` specifically, apply a longer
back-off (60s → 120s → 300s → 900s with jitter), and open a circuit
breaker (15 min sleep) after 5 consecutive 429s.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import Iterable, Optional

import websocket

logger = logging.getLogger('carrac.data_sources.finnhub_ws')

# Same curated list the browser subscribes to — keep in sync with
# static/js/finnhub-ws.js FINNHUB_SYMBOLS.
FINNHUB_SYMBOLS: tuple[str, ...] = (
    # Stocks
    'AAPL', 'GOOG', 'AMZN', 'META', 'TSLA', 'NVDA', 'MSFT',
    'AMD', 'INTC', 'QCOM',
    'BA', 'LMT', 'RTX', 'NOC', 'GD', 'HON',
    'XOM', 'CVX',
    'JPM', 'GS', 'V', 'MA',
    'UNH', 'JNJ',
    'PG', 'KO', 'WMT',
    'MSTR',
    'COIN', 'RIOT', 'MARA', 'HOOD',
    'SOFI', 'SQ', 'PYPL',
    'PLTR', 'ARM', 'SMCI', 'CRWD', 'NET', 'SNOW',
    'NFLX', 'DIS', 'UBER',
    'LLY', 'PFE',
    'TNK',
    # ETFs
    'VTI', 'QQQ', 'IWM', 'SPY',
    'XLE', 'XLF', 'XLK', 'XLV',
    'GDX', 'URA', 'MAGS',
    'TLT', 'HYG', 'LQD', 'SHY',
    'ARKK', 'IBIT', 'BITO',
    'SOXX', 'SMH', 'IGV',
    'XBI', 'VNQ', 'XLP', 'XLI',
)

REDIS_KEY_PREFIX = 'mcd:finnhub:tick:'
REDIS_TICK_TTL = 120  # 2 minutes — outlasts a quick client poll gap


class FinnhubWSWorker:
    """Long-lived Finnhub WebSocket worker.

    Runs in its own daemon thread. Pushes ticks into Redis keyed by symbol;
    the web layer reads them synchronously via /api/finnhub/quotes.
    """

    def __init__(self, api_key: str, symbols: Iterable[str] = FINNHUB_SYMBOLS,
                 redis_client=None):
        self.api_key = api_key
        self.symbols = tuple(symbols)
        self._redis = redis_client
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60
        # 429 tracking state — separate from the generic reconnect
        # backoff because rate-limit windows are much longer than transient
        # socket drops.
        self._last_error_was_429 = False
        self._consecutive_429s = 0
        self._rate_limit_backoff_seq = (60, 120, 300, 600, 900)
        self._circuit_breaker_threshold = 5
        self._circuit_breaker_sleep = 900  # 15 min
        # Half-open handling. After the 900s circuit-breaker sleep,
        # resetting _consecutive_429s = 0 unconditionally is wrong: if
        # Finnhub's rate window has NOT cleared, we hit a fresh ladder
        # of 5×429s from zero — a reconnect storm (~53/h).
        #
        # So the streak only resets when we successfully connect AND
        # stay connected for at least _stable_open_seconds. We track this
        # via a timestamp set in _on_open and cleared on every error.
        self._stable_open_seconds = 30
        self._last_open_ts: Optional[float] = None
        # Cap the number of consecutive circuit-breaker openings so a
        # truly-banned IP backs off into hour-long sleeps instead of
        # cycling at 15 min forever.
        self._cb_openings = 0
        self._cb_max_sleep = 7200  # 2 hours upper cap

    def _get_redis(self):
        if self._redis is not None:
            return self._redis
        try:
            import redis
            url = os.environ.get('REDIS_URL', '')
            if not url:
                return None
            self._redis = redis.from_url(url, socket_connect_timeout=3, decode_responses=True)
            return self._redis
        except Exception as exc:
            logger.warning("Finnhub: redis unavailable: %s", exc)
            return None

    def start(self):
        if not self.api_key:
            logger.info("Finnhub WS not started: FINNHUB_API_KEY unset")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='finnhub-ws', daemon=True)
        self._thread.start()
        logger.info("Finnhub WS worker started (%d symbols)", len(self.symbols))

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # --- internals ---

    def _on_open(self, ws):
        logger.info("Finnhub WS connected (%d symbols)", len(self.symbols))
        self._reconnect_delay = 5
        # Do NOT reset the 429 streak here. Finnhub frequently
        # accepts the handshake then immediately closes the socket — we
        # have to wait until _stable_open_seconds before declaring victory.
        # The reset now happens in the post-disconnect branch of _run().
        self._last_open_ts = time.time()
        self._last_error_was_429 = False
        for sym in self.symbols:
            try:
                ws.send(json.dumps({'type': 'subscribe', 'symbol': sym}))
            except Exception as exc:
                logger.warning("Finnhub subscribe failed %s: %s", sym, exc)

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get('type') != 'trade':
            return
        data = msg.get('data') or []
        if not data:
            return
        r = self._get_redis()
        if r is None:
            return
        # Keep only the latest tick per symbol in the batch
        latest: dict[str, dict] = {}
        for t in data:
            s = t.get('s')
            if not s:
                continue
            latest[s] = t
        try:
            pipe = r.pipeline()
            for sym, t in latest.items():
                payload = json.dumps({
                    'symbol': sym,
                    'price': t.get('p'),
                    'volume': t.get('v'),
                    'ts': t.get('t'),
                })
                pipe.set(f'{REDIS_KEY_PREFIX}{sym}', payload, ex=REDIS_TICK_TTL)
            pipe.execute()
        except Exception as exc:
            logger.debug("Finnhub redis write failed: %s", exc)

    def _on_error(self, ws, err):
        # Detect 429 by string match. websocket-client stringifies the
        # WebSocketBadStatusException with the handshake status line.
        err_text = str(err)
        self._last_error_was_429 = 'Handshake status 429' in err_text
        logger.warning("Finnhub WS error: %s", err_text)

    def _on_close(self, ws, code, reason):
        logger.info("Finnhub WS closed (code=%s reason=%s)", code, reason)

    def _rate_limit_sleep(self) -> float:
        """Pick the next 429-specific sleep with ±20% jitter."""
        idx = min(self._consecutive_429s - 1, len(self._rate_limit_backoff_seq) - 1)
        base = self._rate_limit_backoff_seq[max(idx, 0)]
        jitter = base * 0.2 * (random.random() * 2 - 1)
        return max(30.0, base + jitter)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    f'wss://ws.finnhub.io?token={self.api_key}',
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                logger.warning("Finnhub WS crashed: %s", exc)
            if self._stop.is_set():
                break

            # Only declare the 429 streak ended if we held the
            # socket open for _stable_open_seconds AFTER the handshake.
            # Finnhub will sometimes 200 the handshake then immediately
            # close — that does NOT prove the IP/account is healthy.
            stable_session = (
                self._last_open_ts is not None
                and (time.time() - self._last_open_ts) >= self._stable_open_seconds
            )
            if stable_session and self._consecutive_429s > 0:
                logger.info(
                    "Finnhub: 429 streak ended after %d attempts (stable session %ds) — counters reset",
                    self._consecutive_429s, int(time.time() - self._last_open_ts),
                )
                self._consecutive_429s = 0
                self._cb_openings = 0
            self._last_open_ts = None

            # Branch on 429 vs ordinary drop.
            if self._last_error_was_429:
                self._consecutive_429s += 1
                if self._consecutive_429s >= self._circuit_breaker_threshold:
                    # Extend circuit-breaker sleep on repeat
                    # openings so a truly-banned IP backs off into
                    # progressively longer sleeps (15m → 30m → 1h → 2h cap)
                    # instead of cycling at 15min forever.
                    self._cb_openings += 1
                    cb_sleep = min(
                        self._circuit_breaker_sleep * self._cb_openings,
                        self._cb_max_sleep,
                    )
                    logger.warning(
                        "Finnhub: circuit breaker #%d opened after %d consecutive 429s — sleeping %ds",
                        self._cb_openings, self._consecutive_429s, cb_sleep,
                    )
                    # Sleep in 5s slices so stop() stays responsive.
                    for _ in range(cb_sleep // 5):
                        if self._stop.is_set():
                            break
                        time.sleep(5)
                    # Half-open: only drop the streak by one so the next
                    # failure re-opens immediately, instead of resetting
                    # to zero and granting another 4 free attempts.
                    self._consecutive_429s = max(self._consecutive_429s - 1, self._circuit_breaker_threshold - 1)
                else:
                    delay = self._rate_limit_sleep()
                    logger.warning(
                        "Finnhub: 429 #%d — backing off %.0fs",
                        self._consecutive_429s, delay,
                    )
                    time.sleep(delay)
                self._last_error_was_429 = False
                # Don't let the transient-drop ladder bleed into the next attempt.
                self._reconnect_delay = 5
            else:
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
