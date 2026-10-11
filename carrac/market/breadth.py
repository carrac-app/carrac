"""
Market Breadth Engine
=====================
Computes daily breadth statistics across ALL tracked assets (~195-350 symbols):
  - RSI(14) daily oversold (<30) / overbought (>70)
  - Price above / below SMA(50)
  - New 20-day highs / lows (close >= 20D rolling high / <= 20D rolling low)
  - One-word label derived from the oversold/overbought balance

DATA SOURCE
-----------
Daily CSV caches on disk (csv_cache.CSVCache / data/*.csv).  These are written
by the precompute worker after every OHLCV refresh cycle, so they are always
fresh without triggering any live API calls.

This module NEVER reads live OHLCV or the Redis response_cache.  It only reads
already-persisted CSV files.  When a symbol has no CSV yet it is excluded from
the covered count (universe_size is the full list; covered is what has data).

CACHING
-------
The computed breadth dict is written to Redis at key ``mcd:breadth`` with a
1-hour TTL (breadth moves slowly intraday on daily indicators).  In-memory
dict serves as a fallback when Redis is unavailable.

LABEL THRESHOLDS
----------------
Label is derived from the *oversold percentage* of the covered universe,
adjusted for the overbought side:

  washed-out : oversold_pct  >= WASHED_OUT_THRESHOLD (35 %)
  weak       : oversold_pct  >= WEAK_THRESHOLD        (20 %)
  mixed      : default when neither extreme dominates
  strong     : overbought_pct >= STRONG_THRESHOLD     (25 %) and oversold_pct <  20 %
  frothy     : overbought_pct >= FROTHY_THRESHOLD     (40 %)

The frothy check runs before strong so an extreme OB reading wins.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger('carrac.market.breadth')

# ─── label thresholds ────────────────────────────────────────────────────────
WASHED_OUT_THRESHOLD = 35.0   # % oversold  → washed-out
WEAK_THRESHOLD = 20.0         # % oversold  → weak
FROTHY_THRESHOLD = 40.0       # % overbought → frothy
STRONG_THRESHOLD = 25.0       # % overbought (when <20% oversold) → strong

# Minimum bars needed before we trust RSI / SMA computations for a symbol.
_RSI_MIN_BARS = 28            # 14-period RSI needs 2× lookback to be stable
_SMA_MIN_BARS = 50
_SMA200_MIN_BARS = 200
_HIGH_LOW_MIN_BARS = 20

BREADTH_CACHE_TTL = 3600      # 1 hour — breadth moves slowly on daily indicators
_BREADTH_REDIS_KEY = 'mcd:breadth'

# ─── in-memory fallback ───────────────────────────────────────────────────────
_memory_cache: Optional[dict] = None
_memory_cache_ts: float = 0.0
_memory_lock = threading.Lock()


# ─── helpers ─────────────────────────────────────────────────────────────────

def _get_redis():
    try:
        from carrac.redis_cache import get_redis
        return get_redis()
    except Exception:
        return None


def _compute_rsi14(close: pd.Series) -> Optional[float]:
    """Wilder RSI(14) — returns the most-recent value or None on insufficient data."""
    if len(close) < _RSI_MIN_BARS:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    # Wilder smoothing: use ewm with alpha=1/14, adjust=False
    avg_gain = gain.ewm(alpha=1.0 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / 14, adjust=False).mean()
    last_loss = float(avg_loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = float(avg_gain.iloc[-1]) / last_loss
    return round(100 - (100 / (1 + rs)), 2)


def _compute_sma50(close: pd.Series) -> Optional[float]:
    if len(close) < _SMA_MIN_BARS:
        return None
    return float(close.rolling(50).mean().iloc[-1])


def _compute_sma200(close: pd.Series) -> Optional[float]:
    if len(close) < _SMA200_MIN_BARS:
        return None
    return float(close.rolling(200).mean().iloc[-1])


def _is_20d_high(close: pd.Series) -> Optional[bool]:
    """True if latest close >= 20-day rolling high close."""
    if len(close) < _HIGH_LOW_MIN_BARS:
        return None
    rolling_high = float(close.rolling(20).max().iloc[-1])
    return float(close.iloc[-1]) >= rolling_high


def _is_20d_low(close: pd.Series) -> Optional[bool]:
    """True if latest close <= 20-day rolling low close."""
    if len(close) < _HIGH_LOW_MIN_BARS:
        return None
    rolling_low = float(close.rolling(20).min().iloc[-1])
    return float(close.iloc[-1]) <= rolling_low


def _derive_label(oversold_pct: float, overbought_pct: float) -> str:
    """Map breadth percentages to a one-word label."""
    if oversold_pct >= WASHED_OUT_THRESHOLD:
        return 'washed-out'
    if overbought_pct >= FROTHY_THRESHOLD:
        return 'frothy'
    if oversold_pct >= WEAK_THRESHOLD:
        return 'weak'
    if overbought_pct >= STRONG_THRESHOLD and oversold_pct < WEAK_THRESHOLD:
        return 'strong'
    return 'mixed'


# ─── universe ────────────────────────────────────────────────────────────────

def _build_universe() -> list[str]:
    """Return the full tradeable symbol universe (crypto + TradFi, no metrics).

    Does NOT import services/database at module load time — the import happens
    lazily on the first request so Docker startup is unaffected.
    """
    try:
        from carrac.services import get_services
        svc = get_services()
        db = svc.db
        # Exclude crypto metric symbols (BTC.D, TOTAL, etc.) — no useful breadth signal
        symbols = sorted(set(db.crypto_map.keys()) |
                         set(db.stock_map.keys()) |
                         set(db.etf_map.keys()) |
                         set(db.commodity_map.keys()) |
                         set(db.index_map.keys()) |
                         set(db.forex_map.keys()))
        return symbols
    except Exception as e:
        logger.warning(f'breadth: could not build universe from services: {e}')
        return []


# ─── core computation ─────────────────────────────────────────────────────────

def compute_breadth() -> dict:
    """Scan all tracked assets and return breadth statistics.

    Reads from daily CSV caches only (no live API calls).
    May take a few seconds on first call; result is cached for TTL seconds.
    """
    from csv_cache import CSVCache
    data_dir = os.environ.get('DATA_DIR', 'data')
    csv = CSVCache(data_dir)

    universe = _build_universe()
    if not universe:
        return _empty_breadth()

    oversold: list[str] = []
    overbought: list[str] = []
    above_sma50: list[str] = []
    below_sma50: list[str] = []
    above_sma200: list[str] = []
    new_20d_highs: list[str] = []
    new_20d_lows: list[str] = []
    covered: list[str] = []
    advancers: int = 0
    decliners: int = 0

    t0 = time.time()

    for symbol in universe:
        try:
            df = csv.load(symbol, 'daily')
            if df is None or df.empty or 'close' not in df.columns:
                continue
            close = df['close'].dropna()
            if len(close) < _HIGH_LOW_MIN_BARS:  # need at least 20 bars for anything
                continue
            covered.append(symbol)

            # RSI(14)
            rsi = _compute_rsi14(close)
            if rsi is not None:
                if rsi < 30:
                    oversold.append(symbol)
                elif rsi > 70:
                    overbought.append(symbol)

            # SMA(50)
            sma50 = _compute_sma50(close)
            if sma50 is not None:
                price = float(close.iloc[-1])
                if price >= sma50:
                    above_sma50.append(symbol)
                else:
                    below_sma50.append(symbol)

            # SMA(200)
            sma200 = _compute_sma200(close)
            if sma200 is not None:
                price = float(close.iloc[-1])
                if price >= sma200:
                    above_sma200.append(symbol)

            # Advancers / decliners (covered symbols always have ≥ 20 bars)
            last_close = float(close.iloc[-1])
            prev_close = float(close.iloc[-2])
            if last_close > prev_close:
                advancers += 1
            elif last_close < prev_close:
                decliners += 1

            # 20D highs / lows
            is_high = _is_20d_high(close)
            if is_high is True:
                new_20d_highs.append(symbol)
            is_low = _is_20d_low(close)
            if is_low is True:
                new_20d_lows.append(symbol)

        except Exception as e:
            logger.debug(f'breadth: skipping {symbol}: {e}')
            continue

    elapsed = round(time.time() - t0, 2)
    n = len(covered)

    def _pct(count: int) -> float:
        return round(count / n * 100, 1) if n else 0.0

    oversold_pct = _pct(len(oversold))
    overbought_pct = _pct(len(overbought))

    result = {
        'as_of': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'universe_size': len(universe),
        'covered': n,
        'oversold': {
            'count': len(oversold),
            'pct': oversold_pct,
            'symbols_sample': oversold[:5],
        },
        'overbought': {
            'count': len(overbought),
            'pct': overbought_pct,
            'symbols_sample': overbought[:5],
        },
        'above_sma50': {
            'count': len(above_sma50),
            'pct': _pct(len(above_sma50)),
        },
        'below_sma50': {
            'count': len(below_sma50),
            'pct': _pct(len(below_sma50)),
        },
        'above_sma200': {
            'count': len(above_sma200),
            'pct': _pct(len(above_sma200)),
        },
        'advancers': advancers,
        'decliners': decliners,
        'new_20d_highs': {
            'count': len(new_20d_highs),
            'pct': _pct(len(new_20d_highs)),
            'symbols_sample': new_20d_highs[:5],
        },
        'new_20d_lows': {
            'count': len(new_20d_lows),
            'pct': _pct(len(new_20d_lows)),
            'symbols_sample': new_20d_lows[:5],
        },
        'label': _derive_label(oversold_pct, overbought_pct),
        '_computed_in_s': elapsed,
    }
    logger.info(
        f'breadth: covered {n}/{len(universe)} symbols in {elapsed}s — '
        f'OS={len(oversold)} OB={len(overbought)} '
        f'above50={len(above_sma50)} above200={len(above_sma200)} '
        f'adv={advancers} dec={decliners} '
        f'highs={len(new_20d_highs)} label={result["label"]}'
    )
    return result


def _empty_breadth() -> dict:
    return {
        'as_of': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'universe_size': 0,
        'covered': 0,
        'oversold': {'count': 0, 'pct': 0.0, 'symbols_sample': []},
        'overbought': {'count': 0, 'pct': 0.0, 'symbols_sample': []},
        'above_sma50': {'count': 0, 'pct': 0.0},
        'below_sma50': {'count': 0, 'pct': 0.0},
        'above_sma200': {'count': 0, 'pct': 0.0},
        'advancers': 0,
        'decliners': 0,
        'new_20d_highs': {'count': 0, 'pct': 0.0, 'symbols_sample': []},
        'new_20d_lows': {'count': 0, 'pct': 0.0, 'symbols_sample': []},
        'label': 'mixed',
        '_computed_in_s': 0.0,
    }


# ─── cache layer ─────────────────────────────────────────────────────────────

def get_breadth(force_refresh: bool = False) -> dict:
    """Return cached breadth or compute fresh.

    Cache hierarchy:
      1. Redis (cross-worker, 1h TTL)
      2. In-memory dict (same-process fallback)
      3. Fresh compute (first call or post-TTL)
    """
    global _memory_cache, _memory_cache_ts

    if not force_refresh:
        # Try Redis
        r = _get_redis()
        if r:
            try:
                raw = r.get(_BREADTH_REDIS_KEY)
                if raw:
                    return json.loads(raw)
            except Exception as e:
                logger.debug(f'breadth: Redis read error: {e}')

        # Try in-memory fallback
        with _memory_lock:
            if _memory_cache and (time.time() - _memory_cache_ts) < BREADTH_CACHE_TTL:
                return _memory_cache

    # Compute fresh
    result = compute_breadth()
    _write_cache(result)
    return result


def _write_cache(result: dict) -> None:
    global _memory_cache, _memory_cache_ts

    r = _get_redis()
    if r:
        try:
            r.set(_BREADTH_REDIS_KEY, json.dumps(result), ex=BREADTH_CACHE_TTL)
        except Exception as e:
            logger.debug(f'breadth: Redis write error: {e}')

    with _memory_lock:
        _memory_cache = result
        _memory_cache_ts = time.time()
