"""
Modeled liquidation-level heatmap.

Flagship visual of Coinglass/Hyblock/Coinalyze, built from data we already
collect ourselves (Binance Futures OI, price candles, long/short account
ratio, and our own forced-liquidation WS feed) -- NEVER from CoinGlass
(dormant/commercial-only integration).

METHODOLOGY
===========

Inputs (per symbol, per compute cycle):
  - Open-interest history at `DEFAULT_OI_PERIOD` granularity, fetched live
    from Binance's own `openInterestHist` endpoint (keeps ~30 days -- no
    need to persist our own OI history for this).
  - Price candles at the same period (Binance klines).
  - Global long/short ACCOUNT ratio history at the same period (Binance
    `globalLongShortAccountRatio` -- the same family of data
    quant/collectors/binance_daily.py already collects daily; here we fetch
    it fresh at matching per-bucket granularity instead of reading the
    daily-only quant.derivatives column, which would be too coarse to align
    with hourly OI buckets). Falls back to a stated 50/50 split when this
    call fails or returns nothing for a bucket.

Position inference: for every step where OI increases, the delta is treated
as newly-opened notional at that step's candle close, split long/short by
the long-account fraction (or 50/50 -- see above).

Leverage-tier distribution: each side's new notional is spread across
leverage tiers {10, 25, 50, 100} with weights {0.4, 0.3, 0.2, 0.1} (default). Modeled liquidation price uses a FLAT maintenance-margin rate
(DEFAULT_MMR = 0.5%) -- a stated simplification; real Binance MMR schedules
are tiered by notional bracket and vary per symbol, but a single flat rate
keeps the model tractable and matches the intended spec exactly:
    long_liq  = entry * (1 - 1/L + mmr)
    short_liq = entry * (1 + 1/L - mmr)

Bucketing: DEFAULT_N_BUCKETS buckets spanning +/-DEFAULT_BAND_PCT around the
symbol's current spot price. A leverage tier's modeled liq price that falls
outside today's band is silently dropped (a position opened days ago at a
very different spot no longer maps onto a *current* liquidation-risk band;
the decay/clear-on-traverse steps below prune stale contributions anyway).

Decay (OI falls): applied PRO-RATA across every bucket, both sides, by
`|delta_oi| / oi_prev` -- a stated simplification. Binance does not expose
which side is closing on a net OI decrease (both longs and shorts could be
closing simultaneously), so rather than guess we shrink the whole existing
intensity state proportionally. The alternative (oldest-first / FIFO) would
require tracking per-bucket insertion order for no real gain in accuracy
given the same missing information.

Clear-on-traverse: for every step, any bucket whose price range overlaps
that step's candle [low, high] is zeroed on BOTH sides -- price has
literally traversed that level, so any liquidation cluster resting there has
already triggered by this point in history.

Normalization: both sides are normalized 0..1 against a SHARED max (not
normalized independently per side) so relative long-vs-short intensity
comparison survives normalization -- an independent per-side max would make
a quiet side look just as "hot" as a loud one.

Paint threshold (Coinglass-style): only buckets whose
combined (long+short) intensity clears the HEATMAP_PAINT_QUANTILE quantile
of that cycle's own nonzero combined values are persisted/painted. This is
a v1 config constant, not a UI slider.

Time axis: the heatmap is a TIME x PRICE grid, not
just a latest snapshot -- every compute cycle's thresholded bands are
persisted as one row in quant.liq_heatmap_snapshots, capped retention. The
API serves the series for a requested window.

Universe: HEATMAP_SYMBOLS reuses the existing "top-10 by futures volume"
quant target universe (quant.collectors.binance_daily.ASSETS -- the same
list data_sources/binance_liq_ws.py's TARGET_ASSETS mirrors) rather than
deriving a new "top OI" ranking, so the model, the daily collector, and the
live liq-WS persistence filter never drift apart.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from quant.collectors.binance_daily import ASSETS as HEATMAP_SYMBOLS

logger = logging.getLogger('carrac.liq_heatmap')

# ============================================================
# Config (module-level constants -- no UI slider for v1)
# ============================================================

DEFAULT_LEVERAGE_WEIGHTS: Dict[int, float] = {10: 0.4, 25: 0.3, 50: 0.2, 100: 0.1}
DEFAULT_MMR = 0.005                # flat maintenance-margin rate (stated simplification)
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_BAND_PCT = 0.25            # +/-25% band around spot
DEFAULT_N_BUCKETS = 150
DEFAULT_OI_PERIOD = '1h'           # Binance openInterestHist period backing one bucket-step

# Coinglass-style "don't paint weak levels" cutoff -- quantile of nonzero
# combined (long+short) intensity within one cycle.
HEATMAP_PAINT_QUANTILE = 0.85

SNAPSHOT_RETENTION_DAYS = DEFAULT_LOOKBACK_DAYS   # quant.liq_heatmap_snapshots ring buffer
LIQ_HEATMAP_INTERVAL_SECONDS = 900                # worker recompute cadence (15 min)

REDIS_LATEST_KEY_FMT = 'mcd:liq_heatmap:latest:{symbol}'
REDIS_LATEST_TTL = 1800  # 30 min > compute cadence, self-heals if the worker stalls


# ============================================================
# Pure model math -- unit-tested with synthetic fixtures, no I/O
# ============================================================

def build_price_buckets(spot_price: float, band_pct: float = DEFAULT_BAND_PCT,
                         n_buckets: int = DEFAULT_N_BUCKETS) -> List[Tuple[float, float]]:
    """Return `n_buckets` evenly-spaced (price_low, price_high) tuples
    spanning spot*(1-band_pct) .. spot*(1+band_pct), ascending."""
    if not spot_price or spot_price <= 0 or n_buckets <= 0:
        return []
    low = spot_price * (1 - band_pct)
    high = spot_price * (1 + band_pct)
    step = (high - low) / n_buckets
    if step <= 0:
        return []
    return [(low + i * step, low + (i + 1) * step) for i in range(n_buckets)]


def bucket_index_for_price(buckets: Sequence[Tuple[float, float]], price: Optional[float]) -> Optional[int]:
    """O(1) lookup assuming uniform bucket width (as built by
    build_price_buckets). Returns None when price is outside the band."""
    if not buckets or price is None:
        return None
    low = buckets[0][0]
    high = buckets[-1][1]
    if price < low or price >= high:
        return None
    step = buckets[0][1] - buckets[0][0]
    if step <= 0:
        return None
    idx = int((price - low) / step)
    return max(0, min(len(buckets) - 1, idx))


def leverage_liq_price(entry_price: float, side: str, leverage: float,
                        mmr: float = DEFAULT_MMR) -> float:
    """Modeled liquidation price for a position opened at entry_price.

    long:  entry * (1 - 1/L + mmr)
    short: entry * (1 + 1/L - mmr)
    """
    if side == 'long':
        return entry_price * (1 - 1.0 / leverage + mmr)
    if side == 'short':
        return entry_price * (1 + 1.0 / leverage - mmr)
    raise ValueError(f"side must be 'long' or 'short', got {side!r}")


def infer_position_notional(oi_delta_usd: Optional[float],
                             long_fraction: float = 0.5) -> Tuple[float, float]:
    """Split a positive OI-delta into (long_notional, short_notional).

    `long_fraction` is the fraction of new positions inferred long -- pass
    the Binance global long-account fraction when available, else the
    50/50 fallback (default). OI decreases (<=0) are NOT new positions --
    they're handled by apply_prorata_decay -- so this returns (0, 0).
    """
    if oi_delta_usd is None or oi_delta_usd <= 0:
        return 0.0, 0.0
    long_fraction = max(0.0, min(1.0, long_fraction))
    return oi_delta_usd * long_fraction, oi_delta_usd * (1 - long_fraction)


def distribute_position_to_tiers(notional: float, entry_price: float, side: str,
                                  buckets: Sequence[Tuple[float, float]],
                                  weights: Optional[Dict[int, float]] = None,
                                  mmr: float = DEFAULT_MMR) -> Dict[int, float]:
    """Spread one side's new position notional across leverage tiers,
    returning {bucket_index: added_intensity}. A tier whose modeled liq
    price falls outside the current band is silently dropped (see module
    docstring)."""
    if weights is None:
        weights = DEFAULT_LEVERAGE_WEIGHTS
    out: Dict[int, float] = {}
    if notional <= 0 or not entry_price or entry_price <= 0 or not buckets:
        return out
    for leverage, weight in weights.items():
        portion = notional * weight
        if portion <= 0:
            continue
        liq_price = leverage_liq_price(entry_price, side, leverage, mmr)
        idx = bucket_index_for_price(buckets, liq_price)
        if idx is None:
            continue
        out[idx] = out.get(idx, 0.0) + portion
    return out


def apply_prorata_decay(intensity: List[float], decay_fraction: float) -> List[float]:
    """Shrink every bucket's intensity by the same fraction, in place
    (pro-rata decay on an OI decrease -- see module docstring)."""
    decay_fraction = max(0.0, min(1.0, decay_fraction))
    if decay_fraction <= 0:
        return intensity
    keep = 1.0 - decay_fraction
    for i in range(len(intensity)):
        intensity[i] *= keep
    return intensity


def clear_traversed_buckets(long_intensity: List[float], short_intensity: List[float],
                             buckets: Sequence[Tuple[float, float]],
                             low: Optional[float], high: Optional[float]) -> None:
    """Zero out BOTH sides' intensity for every bucket whose price range
    overlaps [low, high] -- a candle's traversal of that level. In place."""
    if low is None or high is None or low > high:
        return
    for i, (blo, bhi) in enumerate(buckets):
        if bhi < low or blo > high:
            continue
        long_intensity[i] = 0.0
        short_intensity[i] = 0.0


def normalize_intensity(long_intensity: Sequence[float],
                         short_intensity: Sequence[float]) -> Tuple[List[float], List[float]]:
    """Normalize both arrays 0..1 against a SHARED max (see module
    docstring for why not per-side). All-zero input passes through
    unchanged (avoids a divide-by-zero, and there is nothing to normalize)."""
    peak = 0.0
    if long_intensity:
        peak = max(peak, max(long_intensity))
    if short_intensity:
        peak = max(peak, max(short_intensity))
    if peak <= 0:
        return list(long_intensity), list(short_intensity)
    return (
        [v / peak for v in long_intensity],
        [v / peak for v in short_intensity],
    )


def compute_liq_heatmap_from_series(
    oi_series: Sequence[Optional[float]],
    price_series: Sequence[Optional[dict]],
    spot_price: Optional[float] = None,
    long_fraction_series: Optional[Sequence[Optional[float]]] = None,
    band_pct: float = DEFAULT_BAND_PCT,
    n_buckets: int = DEFAULT_N_BUCKETS,
    leverage_weights: Optional[Dict[int, float]] = None,
    mmr: float = DEFAULT_MMR,
) -> Tuple[List[Tuple[float, float]], List[float], List[float]]:
    """Pure orchestration -- build the full (dense) bucket grid for one
    compute cycle from time-aligned OI + price (+ optional long-fraction)
    series, all index-aligned (index i = the same point in time across all
    three inputs; a step's price entry may be None if a matching candle
    was unavailable -- gracefully skipped, see fetch_model_inputs).

    Returns (buckets, long_intensity, short_intensity); buckets is the
    dense (price_low, price_high) list from build_price_buckets(), the two
    intensity lists are same-length and normalized 0..1.
    """
    n = len(oi_series)
    n = min(n, len(price_series))
    if long_fraction_series is not None:
        n = min(n, len(long_fraction_series))
    if n < 2:
        return [], [], []

    ref_price = spot_price
    if not ref_price:
        for candle in reversed(price_series[:n]):
            if isinstance(candle, dict) and candle.get('close'):
                ref_price = candle['close']
                break
    if not ref_price:
        return [], [], []

    buckets = build_price_buckets(ref_price, band_pct, n_buckets)
    if not buckets:
        return [], [], []

    long_intensity = [0.0] * len(buckets)
    short_intensity = [0.0] * len(buckets)

    for i in range(1, n):
        oi_prev = oi_series[i - 1]
        oi_now = oi_series[i]
        delta = (oi_now - oi_prev) if (oi_prev is not None and oi_now is not None) else None

        # 1. Decay first -- an OI decrease at step i reflects closes that
        #    happened between step i-1 and i.
        if delta is not None and delta < 0 and oi_prev and oi_prev > 0:
            decay_fraction = min(1.0, abs(delta) / oi_prev)
            apply_prorata_decay(long_intensity, decay_fraction)
            apply_prorata_decay(short_intensity, decay_fraction)

        # 2. Clear-on-traverse -- this step's candle sweeping through a bucket
        #    triggers any resting liquidation levels there.
        candle = price_series[i]
        low = candle.get('low') if isinstance(candle, dict) else None
        high = candle.get('high') if isinstance(candle, dict) else None
        if low is not None and high is not None:
            clear_traversed_buckets(long_intensity, short_intensity, buckets, low, high)

        # 3. New positions -- an OI increase opens fresh positions near this
        #    step's close.
        if delta is not None and delta > 0:
            close = candle.get('close') if isinstance(candle, dict) else None
            if close and close > 0:
                long_frac = 0.5
                if long_fraction_series is not None and long_fraction_series[i] is not None:
                    long_frac = long_fraction_series[i]
                long_notional, short_notional = infer_position_notional(delta, long_frac)
                for idx, add in distribute_position_to_tiers(
                        long_notional, close, 'long', buckets, leverage_weights, mmr).items():
                    long_intensity[idx] += add
                for idx, add in distribute_position_to_tiers(
                        short_notional, close, 'short', buckets, leverage_weights, mmr).items():
                    short_intensity[idx] += add

    long_intensity, short_intensity = normalize_intensity(long_intensity, short_intensity)
    return buckets, long_intensity, short_intensity


def bands_from_grid(buckets: Sequence[Tuple[float, float]],
                     long_intensity: Sequence[float],
                     short_intensity: Sequence[float]) -> List[dict]:
    """Dense grid -> the output shape: array of
    {price_low, price_high, intensity_long, intensity_short}."""
    return [
        {
            'price_low': round(lo, 8),
            'price_high': round(hi, 8),
            'intensity_long': round(long_intensity[i], 6),
            'intensity_short': round(short_intensity[i], 6),
        }
        for i, (lo, hi) in enumerate(buckets)
    ]


def filter_bands_by_threshold(bands: Sequence[dict],
                               quantile: float = HEATMAP_PAINT_QUANTILE) -> List[dict]:
    """Sparsify a dense bands array for storage/paint: keep only buckets
    whose combined (long+short) intensity clears the `quantile` quantile of
    the cycle's own nonzero combined values (Coinglass-style
    'don't paint weak levels'; a config constant, not a UI slider). A cycle with fewer than 2 nonzero buckets returns whatever
    nonzero buckets exist unfiltered (nothing meaningful to threshold
    against)."""
    combined = [b['intensity_long'] + b['intensity_short'] for b in bands]
    nonzero = sorted(v for v in combined if v > 0)
    if len(nonzero) < 2:
        return [b for b, c in zip(bands, combined) if c > 0]
    idx = min(len(nonzero) - 1, int(quantile * len(nonzero)))
    cutoff = nonzero[idx]
    return [b for b, c in zip(bands, combined) if c >= cutoff]


# ============================================================
# I/O: fetch model inputs from Binance
# ============================================================

def _get_binance_client():
    from data_sources.binance import BinanceClient
    return BinanceClient()


def fetch_model_inputs(symbol: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                        period: str = DEFAULT_OI_PERIOD, client=None):
    """Fetch time-aligned OI history + price candles + long/short-ratio
    history for one symbol, all at `period` granularity, directly from
    Binance. Unlike the liquidation feed, Binance's own openInterestHist /
    globalLongShortAccountRatio endpoints already keep enough history
    (up to 30 days) that nothing needs persisting on our side for this.

    Returns (oi_series, price_series, long_fraction_series, spot_price) --
    all index-aligned to the OI history's own timestamps. `limit` assumes
    period='1h' (lookback_days * 24, capped at Binance's 500-point max);
    a different period still works, just covers a different span.
    """
    client = client or _get_binance_client()
    limit = min(500, max(2, lookback_days * 24))

    oi_points = client.fetch_open_interest_hist(symbol, period=period, limit=limit)
    if not oi_points:
        return [], [], [], None

    oi_series = [p.get('oi_usd') for p in oi_points]
    oi_ts = [p.get('time') for p in oi_points]

    candle_lookup: Dict[int, dict] = {}
    try:
        candles_df = client.fetch_klines(symbol, interval=period, bars=len(oi_points))
        if candles_df is not None and not candles_df.empty:
            for ts_index, row in candles_df.iterrows():
                epoch = int(ts_index.timestamp())
                candle_lookup[epoch] = {
                    'low': float(row['low']), 'high': float(row['high']), 'close': float(row['close']),
                }
    except Exception as e:
        logger.warning(f"liq_heatmap: kline fetch failed for {symbol}: {e}")

    ls_lookup: Dict[int, float] = {}
    try:
        ls_points = client.fetch_global_long_short_ratio_hist(symbol, period=period, limit=limit)
        ls_lookup = {p['time']: p.get('long_account') for p in ls_points}
    except Exception as e:
        logger.debug(f"liq_heatmap: L/S ratio hist unavailable for {symbol}: {e}")

    price_series = [candle_lookup.get(ts) for ts in oi_ts]
    long_fraction_series = [ls_lookup.get(ts) for ts in oi_ts]

    spot_price = None
    for ts in reversed(oi_ts):
        if ts in candle_lookup:
            spot_price = candle_lookup[ts]['close']
            break
    if spot_price is None and candle_lookup:
        spot_price = max(candle_lookup.items(), key=lambda kv: kv[0])[1]['close']

    return oi_series, price_series, long_fraction_series, spot_price


def compute_symbol_heatmap(symbol: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                            period: str = DEFAULT_OI_PERIOD, band_pct: float = DEFAULT_BAND_PCT,
                            n_buckets: int = DEFAULT_N_BUCKETS,
                            leverage_weights: Optional[Dict[int, float]] = None,
                            mmr: float = DEFAULT_MMR, client=None):
    """Fetch inputs + run the pure model for one symbol.

    Returns (spot_price, dense_bands). (None, []) if inputs were
    unavailable (e.g. Binance rate-limited or symbol temporarily missing)."""
    oi_series, price_series, long_fraction_series, spot_price = fetch_model_inputs(
        symbol, lookback_days=lookback_days, period=period, client=client)
    if not oi_series or spot_price is None:
        return None, []
    buckets, long_i, short_i = compute_liq_heatmap_from_series(
        oi_series, price_series, spot_price=spot_price,
        long_fraction_series=long_fraction_series, band_pct=band_pct,
        n_buckets=n_buckets, leverage_weights=leverage_weights, mmr=mmr)
    if not buckets:
        return spot_price, []
    return spot_price, bands_from_grid(buckets, long_i, short_i)


# ============================================================
# Persistence (quant.liq_heatmap_snapshots + Redis latest-cache)
# ============================================================

def persist_heatmap_snapshot(symbol: str, spot_price: Optional[float], dense_bands: List[dict],
                              cycle_ts: Optional[datetime] = None,
                              quantile: float = HEATMAP_PAINT_QUANTILE,
                              band_pct: float = DEFAULT_BAND_PCT) -> int:
    """Threshold-filter the dense grid to a sparse 'paint list' and upsert
    one snapshot row into quant.liq_heatmap_snapshots. Also refreshes the
    'latest' Redis cache. Returns the number of sparse bands persisted."""
    import json as _json
    from carrac.db_pool import get_quant_conn as get_conn

    cycle_ts = cycle_ts or datetime.now(timezone.utc)
    sparse = filter_bands_by_threshold(dense_bands, quantile) if dense_bands else []
    band_low = spot_price * (1 - band_pct) if spot_price else None
    band_high = spot_price * (1 + band_pct) if spot_price else None

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO quant.liq_heatmap_snapshots (symbol, cycle_ts, price, band_low, band_high, bands)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, cycle_ts) DO UPDATE SET
                price = EXCLUDED.price, band_low = EXCLUDED.band_low,
                band_high = EXCLUDED.band_high, bands = EXCLUDED.bands
        """, (symbol, cycle_ts, spot_price, band_low, band_high, _json.dumps(sparse)))
        cur.execute(
            "DELETE FROM quant.liq_heatmap_snapshots WHERE symbol = %s AND cycle_ts < NOW() - (%s || ' days')::interval",
            (symbol, SNAPSHOT_RETENTION_DAYS),
        )

    _write_latest_redis_cache(symbol, cycle_ts, spot_price, band_low, band_high, sparse)
    return len(sparse)


def _write_latest_redis_cache(symbol, cycle_ts, spot_price, band_low, band_high, sparse):
    try:
        from carrac.redis_cache import get_redis
        r = get_redis()
        if not r:
            return
        import json as _json
        payload = {
            'symbol': symbol, 'ts': cycle_ts.isoformat(), 'price': spot_price,
            'band_low': band_low, 'band_high': band_high, 'bands': sparse,
        }
        r.set(REDIS_LATEST_KEY_FMT.format(symbol=symbol), _json.dumps(payload), ex=REDIS_LATEST_TTL)
    except Exception as e:
        logger.debug(f"liq_heatmap: redis latest-cache write failed for {symbol}: {e}")


def get_heatmap_series(symbol: str, window_hours: Optional[int] = None):
    """Read path for GET /api/liq-heatmap.

    Returns (status, payload):
      'not_covered' -- symbol outside HEATMAP_SYMBOLS      (payload None)
      'cold'        -- covered, but no cycles persisted yet (payload None)
      'ok'          -- payload populated

    payload (status 'ok'):
        {symbol, band_pct, n_buckets, paint_quantile,
         cycles: [{ts, price, band_low, band_high, bands: [...]}]}
        cycles ascending by ts, restricted to the requested window
        (default DEFAULT_LOOKBACK_DAYS*24 hours).
    """
    symbol = (symbol or '').upper().strip()
    if symbol not in HEATMAP_SYMBOLS:
        return 'not_covered', None

    from carrac.db_pool import get_quant_conn as get_conn

    hours = window_hours or (DEFAULT_LOOKBACK_DAYS * 24)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT cycle_ts, price, band_low, band_high, bands
            FROM quant.liq_heatmap_snapshots
            WHERE symbol = %s AND cycle_ts >= NOW() - (%s || ' hours')::interval
            ORDER BY cycle_ts ASC
        """, (symbol, hours))
        rows = cur.fetchall()

    if not rows:
        return 'cold', None

    cycles = [
        {
            'ts': row['cycle_ts'].isoformat(),
            'price': float(row['price']) if row['price'] is not None else None,
            'band_low': float(row['band_low']) if row['band_low'] is not None else None,
            'band_high': float(row['band_high']) if row['band_high'] is not None else None,
            'bands': row['bands'],
        }
        for row in rows
    ]
    return 'ok', {
        'symbol': symbol,
        'band_pct': DEFAULT_BAND_PCT,
        'n_buckets': DEFAULT_N_BUCKETS,
        'paint_quantile': HEATMAP_PAINT_QUANTILE,
        'cycles': cycles,
    }


# ============================================================
# Worker scheduler -- mirrors carrac/precompute.py's
# _start_sentiment_scheduler (the smallest existing periodic-job pattern):
# a jittered-start daemon thread, plain sleep loop, no crontab entry.
# ============================================================

_stop_liq_heatmap = threading.Event()


def _compute_all_symbols():
    client = _get_binance_client()
    covered = []
    for symbol in HEATMAP_SYMBOLS:
        try:
            spot_price, bands = compute_symbol_heatmap(symbol, client=client)
            if spot_price is None:
                logger.info(f"liq_heatmap: {symbol} skipped (no OI/price data)")
                continue
            n_painted = persist_heatmap_snapshot(symbol, spot_price, bands)
            covered.append(symbol)
            logger.debug(f"liq_heatmap: {symbol} cycle stored, {n_painted} bands painted")
        except Exception as e:
            logger.warning(f"liq_heatmap: {symbol} compute failed: {e}")
        time.sleep(0.3)  # be nice to Binance's rate limits, mirrors binance_daily.py
    logger.info(
        f"liq_heatmap: cycle complete, covered {len(covered)}/{len(HEATMAP_SYMBOLS)}: {covered}"
    )
    return covered


def _start_liq_heatmap_scheduler():
    """Background thread recomputing the liquidation heatmap for
    HEATMAP_SYMBOLS every LIQ_HEATMAP_INTERVAL_SECONDS."""
    def loop():
        _stop_liq_heatmap.wait(random.randint(0, 60))
        while not _stop_liq_heatmap.is_set():
            try:
                _compute_all_symbols()
            except Exception as e:
                logger.error(f"liq_heatmap: cycle failed: {e}")
            _stop_liq_heatmap.wait(LIQ_HEATMAP_INTERVAL_SECONDS)

    t = threading.Thread(target=loop, daemon=True, name="LiqHeatmapScheduler")
    t.start()
    logger.info(f"Liquidation heatmap scheduler started (every {LIQ_HEATMAP_INTERVAL_SECONDS}s)")
    return t
