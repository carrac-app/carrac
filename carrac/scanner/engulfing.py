"""
Stream signal detectors — lightweight pattern/state-change detection on OHLCV.

Each detector checks the last 2 bars for a state transition and returns
a list of signal dicts (empty if nothing fired).

Signals: engulfing candles, MACD zero cross, EMA Ribbon trend flip, Alligator alignment.
"""

import pandas as pd
import numpy as np

from carrac.charts.indicators import _ema_pine_compat


# ============================================================
# Engulfing Candle
# ============================================================

def detect_engulfing(df, vol_period=20):
    """Detect engulfing candle patterns on the last 2 bars.

    Requires: volume > 20-SMA average.

    Returns:
        list of {type, direction, candle_pos, detail}
    """
    if df is None or len(df) < vol_period + 2:
        return []

    required = {'open', 'close', 'volume'}
    if not required.issubset(df.columns):
        return []

    signals = []

    for offset, label in [(-1, 'current'), (-2, 'previous')]:
        try:
            candle = df.iloc[offset]
            prev = df.iloc[offset - 1]
        except IndexError:
            continue

        c_open = float(candle['open'])
        c_close = float(candle['close'])
        c_vol = float(candle['volume'])
        p_open = float(prev['open'])
        p_close = float(prev['close'])

        if not c_open or not c_close or not p_open or not p_close:
            continue
        if c_vol <= 0:
            continue

        end = len(df) + offset
        start = max(0, end - vol_period)
        avg_vol = float(df['volume'].iloc[start:end].mean())
        if not avg_vol or avg_vol <= 0:
            continue

        if c_vol <= avg_vol:
            continue

        c_body = abs(c_close - c_open)
        p_body = abs(p_close - p_open)

        if p_body == 0 or c_body <= p_body:
            continue

        is_bullish = (
            p_close < p_open and c_close > c_open and
            c_open <= p_close and c_close >= p_open
        )
        is_bearish = (
            p_close > p_open and c_close < c_open and
            c_open >= p_close and c_close <= p_open
        )

        if is_bullish or is_bearish:
            vol_ratio = round(c_vol / avg_vol, 2)
            signals.append({
                'type': 'engulfing',
                'direction': 'bullish' if is_bullish else 'bearish',
                'candle_pos': label,
                'volume_ratio': vol_ratio,
                'detail': f'{vol_ratio}x vol',
            })

    return signals


# ============================================================
# MACD Zero Cross
# ============================================================

def detect_macd_cross(df):
    """Detect MACD line crossing zero on the last 2 bars.

    MACD(12,26,9). Signals when the MACD line crosses above or below zero.

    Returns:
        list of {type, direction, candle_pos, detail}
    """
    if df is None or 'close' not in df.columns or len(df) < 28:
        return []

    close = df['close']
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26

    signals = []

    for offset, label in [(-1, 'current'), (-2, 'previous')]:
        try:
            curr = float(macd_line.iloc[offset])
            prev = float(macd_line.iloc[offset - 1])
        except (IndexError, ValueError):
            continue

        if prev <= 0 < curr:
            signals.append({
                'type': 'macd_cross',
                'direction': 'bullish',
                'candle_pos': label,
                'detail': f'MACD {curr:.2f}',
            })
        elif prev >= 0 > curr:
            signals.append({
                'type': 'macd_cross',
                'direction': 'bearish',
                'candle_pos': label,
                'detail': f'MACD {curr:.2f}',
            })

    return signals


# ============================================================
# EMA Ribbon Trend Flip
# ============================================================

def _ema_ribbon_trend_series(df, fast=30, slow=60, atr_period=60, atr_mult=0.30):
    """Compute EMA Ribbon trend for each bar. Returns Series of 'bull'/'bear'/'neutral'."""
    close = df['close']
    # SMA-seeded EMA (Pine-compatible).
    ema_fast = _ema_pine_compat(close, fast)
    ema_slow = _ema_pine_compat(close, slow)

    high, low = df['high'], df['low']
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(window=atr_period).mean()
    margin = atr_mult * atr

    ema_diff = ema_fast - ema_slow
    trends = pd.Series('neutral', index=df.index)
    trends[ema_diff > margin] = 'bull'
    trends[ema_diff < -margin] = 'bear'
    return trends


def detect_ema_ribbon_flip(df):
    """Detect EMA Ribbon trend flip on the last 2 bars.

    EMA(30) vs EMA(60) with ATR(60)*0.30 margin.
    Signals when trend changes to 'bull' or 'bear' from a different state.

    Returns:
        list of {type, direction, candle_pos, detail}
    """
    min_len = 60 + 10 + 2
    if df is None or len(df) < min_len:
        return []
    if not {'close', 'high', 'low'}.issubset(df.columns):
        return []

    trends = _ema_ribbon_trend_series(df)
    signals = []

    for offset, label in [(-1, 'current'), (-2, 'previous')]:
        try:
            curr = trends.iloc[offset]
            prev = trends.iloc[offset - 1]
        except IndexError:
            continue

        if curr == prev:
            continue

        if curr == 'bull':
            signals.append({
                'type': 'ema_ribbon_flip',
                'direction': 'bullish',
                'candle_pos': label,
                'detail': f'from {prev}',
            })
        elif curr == 'bear':
            signals.append({
                'type': 'ema_ribbon_flip',
                'direction': 'bearish',
                'candle_pos': label,
                'detail': f'from {prev}',
            })

    return signals


# ============================================================
# Williams Alligator Alignment
# ============================================================

def _smma(series, period):
    """Smoothed Moving Average (Wilder's smoothing)."""
    result = np.empty(len(series))
    result[:] = np.nan
    if len(series) < period:
        return result
    # Seed with SMA
    result[period - 1] = series.iloc[:period].mean()
    alpha = 1.0 / period
    for i in range(period, len(series)):
        result[i] = result[i - 1] * (1 - alpha) + series.iloc[i] * alpha
    return result


def detect_alligator_alignment(df):
    """Detect Williams Alligator fully turning bullish or bearish on the last 2 bars.

    Jaw: SMMA(13) of HL/2, shifted 8 bars forward.
    Teeth: SMMA(8) of HL/2, shifted 5 bars forward.
    Lips: SMMA(5) of HL/2, shifted 3 bars forward.

    Bullish: lips > teeth > jaw.
    Bearish: lips < teeth < jaw.

    Signals when alignment just formed (wasn't aligned on the prior bar).

    Returns:
        list of {type, direction, candle_pos, detail}
    """
    if df is None or len(df) < 25:
        return []
    if not {'high', 'low'}.issubset(df.columns):
        return []

    hl2 = (df['high'] + df['low']) / 2

    jaw_raw = _smma(hl2, 13)
    teeth_raw = _smma(hl2, 8)
    lips_raw = _smma(hl2, 5)

    n = len(df)

    def _shifted(raw, shift, idx):
        """Get shifted SMMA value at bar idx (shift = forward displacement)."""
        src = idx - shift
        if 0 <= src < n and not np.isnan(raw[src]):
            return raw[src]
        return None

    def _alignment(idx):
        """Return 'bullish', 'bearish', or None for bar idx."""
        j = _shifted(jaw_raw, 8, idx)
        t = _shifted(teeth_raw, 5, idx)
        li = _shifted(lips_raw, 3, idx)
        if j is None or t is None or li is None:
            return None
        if li > t > j:
            return 'bullish'
        if li < t < j:
            return 'bearish'
        return None

    signals = []

    for offset, label in [(-1, 'current'), (-2, 'previous')]:
        idx = n + offset
        prev_idx = idx - 1
        if prev_idx < 0:
            continue

        curr_align = _alignment(idx)
        prev_align = _alignment(prev_idx)

        if curr_align and curr_align != prev_align:
            signals.append({
                'type': 'alligator',
                'direction': curr_align,
                'candle_pos': label,
                'detail': 'fully aligned',
            })

    return signals
