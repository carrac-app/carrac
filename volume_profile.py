"""
Volume Profile computation module.

Computes volume distribution across price levels from lower-timeframe kline data.
Returns bins with buy/sell split, POC, VAH, VAL for chart overlay rendering.
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger('carrac.volume_profile')

# Chart TF → source interval for granular VP data
TF_SOURCE_MAP = {
    '1M': '1d',
    '1W': '4h',
    '3D': '1h',
    '1D': '15m',
    '4h': '5m',
    '1h': '1m',
}


def compute_volume_profile(df, num_bins=70):
    """
    Compute volume profile from kline DataFrame.

    Args:
        df: DataFrame with columns [open, high, low, close, volume, taker_buy_base]
        num_bins: Number of price bins (default 200)

    Returns:
        dict with bins, poc, vah, val, price_low, price_high
        or None if insufficient data.
    """
    if df is None or df.empty or len(df) < 2:
        return None

    price_low = float(df['low'].min())
    price_high = float(df['high'].max())

    if price_high <= price_low or price_high == 0:
        return None

    bin_size = (price_high - price_low) / num_bins

    # Initialize bins
    total_vol = [0.0] * num_bins
    buy_vol = [0.0] * num_bins
    sell_vol = [0.0] * num_bins

    has_taker = 'taker_buy_base' in df.columns

    # Distribute each candle's volume across its H-L range
    for _, row in df.iterrows():
        h = float(row['high'])
        l = float(row['low'])
        v = float(row['volume'])
        if v <= 0 or h <= l:
            continue

        # Buy/sell split
        if has_taker:
            bv = float(row['taker_buy_base'])
            sv = v - bv
        else:
            # Fallback: close > open = more buy
            bv = v * 0.5
            sv = v * 0.5

        # Find bin range for this candle
        lo_bin = max(0, int((l - price_low) / bin_size))
        hi_bin = min(num_bins - 1, int((h - price_low) / bin_size))
        span = hi_bin - lo_bin + 1

        if span <= 0:
            continue

        # Weight volume toward the OHLC midpoint (open+close)/2 with
        # a linear triangular falloff toward the high and low extremes.  Uniform
        # distribution over-allocates to the tails on volatile bars and drifts
        # the POC by up to 1 bin from the true activity centre.
        #
        # Method: triangular kernel centred on mid = (open+close)/2.
        #   - Compute a non-negative weight for each bin proportional to how
        #     close the bin centre is to `mid` relative to the half-range.
        #   - Bins exactly at `mid` get weight 1; bins at H/L extremes get
        #     weight approaching 0.  All weights are normalised to sum to 1.
        o = float(row['open'])
        mid = (o + h + l + float(row['close'])) / 4.0  # OHLC midpoint
        half_range = (h - l) / 2.0 if h > l else 1.0

        weights = []
        for b in range(lo_bin, hi_bin + 1):
            bin_centre = price_low + (b + 0.5) * bin_size
            distance = abs(bin_centre - mid)
            # Linear falloff: weight = max(0, 1 - distance/half_range)
            w = max(0.0, 1.0 - distance / half_range)
            weights.append(w)

        weight_sum = sum(weights)
        if weight_sum <= 0:
            # All-zero weights (degenerate bar) — fall back to uniform
            weight_sum = span
            weights = [1.0] * span

        for idx, b in enumerate(range(lo_bin, hi_bin + 1)):
            frac = weights[idx] / weight_sum
            total_vol[b] += v * frac
            buy_vol[b] += bv * frac
            sell_vol[b] += sv * frac

    # Find POC (bin with highest volume)
    max_vol = max(total_vol)
    if max_vol <= 0:
        return None

    poc_idx = total_vol.index(max_vol)
    poc_price = price_low + (poc_idx + 0.5) * bin_size

    # VAH/VAL: CME algorithm — expand from POC until 70% captured
    total_all = sum(total_vol)
    target = total_all * 0.70
    accumulated = total_vol[poc_idx]
    lo_idx = poc_idx
    hi_idx = poc_idx

    while accumulated < target and (lo_idx > 0 or hi_idx < num_bins - 1):
        # Check volume one step above vs one step below
        up_vol = total_vol[hi_idx + 1] if hi_idx < num_bins - 1 else 0
        dn_vol = total_vol[lo_idx - 1] if lo_idx > 0 else 0

        if up_vol >= dn_vol and hi_idx < num_bins - 1:
            hi_idx += 1
            accumulated += total_vol[hi_idx]
        elif lo_idx > 0:
            lo_idx -= 1
            accumulated += total_vol[lo_idx]
        else:
            hi_idx += 1
            accumulated += total_vol[hi_idx]

    vah = price_low + (hi_idx + 1) * bin_size
    val = price_low + lo_idx * bin_size

    # Build output bins (only non-zero for compactness)
    bins = []
    for i in range(num_bins):
        if total_vol[i] > 0:
            bins.append({
                'price': round(price_low + (i + 0.5) * bin_size, 8),
                'total': round(total_vol[i], 4),
                'buy': round(buy_vol[i], 4),
                'sell': round(sell_vol[i], 4),
            })

    return {
        'bins': bins,
        'poc': round(poc_price, 8),
        'vah': round(vah, 8),
        'val': round(val, 8),
        'max_vol': round(max_vol, 4),
        'price_low': round(price_low, 8),
        'price_high': round(price_high, 8),
        'bin_size': round(bin_size, 8),
    }


def get_source_interval(chart_tf):
    """Get the lower-TF source interval for a given chart timeframe."""
    return TF_SOURCE_MAP.get(chart_tf)


def compute_naked_pocs(df_15m, lookback_days=90, num_bins=50):
    """
    Find "naked" (virgin) POCs: completed-session points-of-control that
    later price action has never revisited.

    Args:
        df_15m: DataFrame of 15m OHLCV(+taker) bars, indexed by a UTC
            DatetimeIndex, covering the lookback window plus at least one
            bar after the last completed session (so touches can be
            checked). Columns as expected by ``compute_volume_profile``.
        lookback_days: how many of the most recent COMPLETED UTC days
            (i.e. excluding the day of the last bar, which is treated as
            the current/partial day) to compute sessions for.
        num_bins: bin count passed to ``compute_volume_profile`` for each
            day's session profile.

    Returns:
        List of dicts, sorted newest-first:
            {date: 'YYYY-MM-DD', poc: float, naked: bool,
             touch_ts: int (ms) | None, age_days: int}
        Days whose session profile can't be computed (too few/degenerate
        bars) are skipped. Returns [] if there isn't at least one
        completed day plus a later bar to scan for touches.
    """
    if df_15m is None or df_15m.empty:
        return []

    if not isinstance(df_15m.index, pd.DatetimeIndex):
        return []

    df = df_15m.sort_index()

    day_keys = df.index.normalize()
    unique_days = day_keys.unique().sort_values()
    if len(unique_days) < 2:
        # Need at least one completed day plus a later (even partial) bar
        # to check for a touch.
        return []

    current_day = unique_days[-1]  # excluded — the partial/current day
    completed_days = unique_days[:-1]
    if lookback_days and lookback_days > 0:
        completed_days = completed_days[-lookback_days:]

    lows = df['low'].to_numpy()
    highs = df['high'].to_numpy()
    day_arr = day_keys.to_numpy()
    # ms epoch, robust to pandas' underlying datetime64 resolution (ns/us).
    ts_ms = df.index.astype('datetime64[ms]').astype('int64').to_numpy()
    current_day_np = current_day.to_numpy()

    levels = []
    for day in completed_days:
        day_np = np.datetime64(day)
        mask_day = day_arr == day_np
        day_df = df.loc[mask_day]
        if day_df.empty:
            continue

        result = compute_volume_profile(day_df, num_bins=num_bins)
        if result is None:
            continue
        poc = result['poc']

        # Scan bars strictly after this day's session for the first touch
        # of the POC. Vectorized per-level (at most ~lookback_days levels)
        # rather than a per-bar Python loop over the whole frame.
        after_mask = day_arr > day_np
        naked = True
        touch_ts = None
        age_days = int((current_day_np - day_np) / np.timedelta64(1, 'D'))

        if after_mask.any():
            touched = after_mask & (lows <= poc) & (highs >= poc)
            if touched.any():
                touch_idx = int(np.argmax(touched))  # first True
                naked = False
                touch_ts = int(ts_ms[touch_idx])
                touch_day_np = day_arr[touch_idx]
                age_days = int((touch_day_np - day_np) / np.timedelta64(1, 'D'))

        levels.append({
            'date': pd.Timestamp(day).strftime('%Y-%m-%d'),
            'poc': poc,
            'naked': naked,
            'touch_ts': touch_ts,
            'age_days': age_days,
        })

    levels.sort(key=lambda lv: lv['date'], reverse=True)
    return levels
