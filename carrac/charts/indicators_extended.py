"""
Extended indicator snapshot computation for morning engine consumption.

Computes ~24 indicators and returns a snapshot dict (latest values + state
classifications) rather than full time-series. Designed for the /api/chart-internal/
endpoint to supply the morning report with comprehensive technical analysis data.

All formulas ported from static/js/carrac-indicator-pack.js.
"""

import logging
import math

import numpy as np
import pandas as pd

from consensio.indicators.consensio_core import ConsensioCalculator

logger = logging.getLogger('carrac.charts.indicators_extended')

# Timeframes where fast oscillators are skipped (too noisy)
SKIP_FAST_OSCILLATORS_TFS = {'3D', '1W', '1M'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _atr(df, period=14):
    """Wilder's ATR. Returns Series."""
    if len(df) < period:
        return pd.Series(np.nan, index=df.index)
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)
    # Wilder's smoothing
    atr = tr.copy()
    atr.iloc[:period] = np.nan
    atr.iloc[period - 1] = tr.iloc[:period].mean()
    for i in range(period, len(tr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period
    return atr


def _true_range(df):
    """True Range series."""
    h, l, c = df['high'], df['low'], df['close']
    return pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs()
    ], axis=1).max(axis=1)


def _wma(series, period):
    """Weighted Moving Average."""
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def _smma(series, period):
    """Smoothed Moving Average (Wilder's)."""
    result = series.copy() * np.nan
    result.iloc[period - 1] = series.iloc[:period].mean()
    for i in range(period, len(series)):
        result.iloc[i] = (result.iloc[i - 1] * (period - 1) + series.iloc[i]) / period
    return result


def _donchian_mid(df, period):
    """Donchian channel midpoint (highest high + lowest low) / 2."""
    hh = df['high'].rolling(period).max()
    ll = df['low'].rolling(period).min()
    return (hh + ll) / 2


def _clean_nan(obj):
    """Recursively replace NaN/Inf floats with None so the result is JSON-safe.

    Flask's jsonify uses Python's default json encoder which emits the literal
    `NaN` / `Infinity` tokens — those break browser `JSON.parse()`. A handful of
    indicator helpers (e.g. PSAR, SuperTrend) call `round(float(x), N)` without
    checking for NaN, so any NaN that slips through gets sanitized here.
    """
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_nan(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    # numpy scalar floats slip past isinstance(float) on some platforms
    if isinstance(obj, np.floating):
        f = float(obj)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    return obj


# ---------------------------------------------------------------------------
# Individual indicator computations — each returns a snapshot dict
# ---------------------------------------------------------------------------

def _bollinger_bands(df, period=20, mult=2.0):
    """Bollinger Bands snapshot."""
    if len(df) < period:
        return None
    close = df['close']
    basis = close.rolling(period).mean()
    # Population std (ddof=0) to match TradingView/Pine
    # Script stdev() and consensio/indicators/bollinger.py, not pandas' sample
    # std default (ddof=1).
    std = close.rolling(period).std(ddof=0)
    upper = basis + std * mult
    lower = basis - std * mult
    last_basis = basis.iloc[-1]
    last_upper = upper.iloc[-1]
    last_lower = lower.iloc[-1]
    price = close.iloc[-1]
    if pd.isna(last_basis):
        return None
    bw = (last_upper - last_lower) / last_basis * 100 if last_basis else 0
    pct_b = (price - last_lower) / (last_upper - last_lower) * 100 if last_upper != last_lower else 50
    if price > last_upper:
        position = "above_upper"
    elif price < last_lower:
        position = "below_lower"
    else:
        position = "inside"
    return {
        "basis": round(last_basis, 2),
        "upper": round(last_upper, 2),
        "lower": round(last_lower, 2),
        "bandwidth_pct": round(bw, 2),
        "pct_b": round(pct_b, 1),
        "position": position,
    }


def _bars_since_last_qualifying_squeeze(squeeze_bars, min_duration=3):
    """Bars since the most recent *qualifying* squeeze run ended.

    `squeeze_bars` is a plain oldest-first sequence of bool (True = BB inside
    KC that bar — same semantics/order as `_ttm_squeeze`'s `squeeze` Series).
    Scans backward from the last element. A "qualifying" run is one that
    reached >= min_duration consecutive True bars, mirroring the `active`
    duration gate elsewhere in this function — a shorter run is just a blip,
    not a real squeeze, so it doesn't count as something to have "released"
    from.

    Returns:
        None — the latest bar is itself inside squeeze condition (nothing
               has released yet — still forming/inside one), or no
               qualifying run exists anywhere in the sequence (never
               squeezed for real in this window).
        int  — bars between the end of the most recent qualifying run and
               the latest bar (0 == the bar immediately after it ended).
    """
    n = len(squeeze_bars)
    if n == 0 or squeeze_bars[n - 1]:
        return None
    j = n - 2  # last element already known False; look further back
    while j >= 0 and not squeeze_bars[j]:
        j -= 1
    if j < 0:
        return None
    run_end = j  # last bar of the most recent True run
    run_len = 0
    while j >= 0 and squeeze_bars[j]:
        run_len += 1
        j -= 1
    if run_len < min_duration:
        return None
    # The bar right after run_end (run_end + 1) is "0 bars since release"
    # (just released, this very bar) — so subtract one more than the raw
    # index distance to the latest bar (n - 1).
    return (n - 1) - run_end - 1


def _ttm_squeeze(df, bb_len=30, bb_mult=2.0, kc_len=30, kc_mult=2.5, min_duration=3):
    """TTM Squeeze: BB inside KC detection.

    Also reports `bars_since_release` — how many bars ago the
    most recent *qualifying* squeeze (a run of >= min_duration consecutive
    squeeze bars, mirroring the `active` duration gate) ended, found by
    scanning the squeeze boolean series backward from the latest bar to the
    end of that run. This lets ttm_squeeze_state() (indicator_eval.py)
    distinguish a genuine "just released" moment from momentum that simply
    happens to point a direction while no squeeze has fired recently.
    None means: currently still in/entering a squeeze (nothing to measure
    release-recency from yet), or no run in this window ever met the
    min_duration threshold (never squeezed for real, or only ever saw
    sub-threshold blips) — i.e. no meaningful "release" exists to report.
    """
    if len(df) < max(bb_len, kc_len) + 10:
        return None
    close = df['close']
    # Bollinger
    bb_sma = close.rolling(bb_len).mean()
    # Population std (ddof=0), matching bollinger.py.
    bb_std = close.rolling(bb_len).std(ddof=0)
    bb_upper = bb_sma + bb_std * bb_mult
    bb_lower = bb_sma - bb_std * bb_mult
    # Keltner (SMA-based TR, matching JS impl)
    tr = _true_range(df)
    kc_basis = close.rolling(kc_len).mean()
    kc_tr_sma = tr.rolling(kc_len).mean()
    kc_upper = kc_basis + kc_tr_sma * kc_mult
    kc_lower = kc_basis - kc_tr_sma * kc_mult
    # Squeeze: BB inside KC
    squeeze = (bb_lower > kc_lower) & (bb_upper < kc_upper)
    n = len(squeeze)
    # Count consecutive bars in squeeze at the end
    bars_in_squeeze = 0
    for i in range(n - 1, -1, -1):
        if squeeze.iloc[i]:
            bars_in_squeeze += 1
        else:
            break
    active = bars_in_squeeze >= min_duration
    bars_since_release = _bars_since_last_qualifying_squeeze(squeeze.tolist(), min_duration)

    # Momentum direction from MACD histogram
    mom_dir = "neutral"
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    hist = ema12 - ema26
    if len(hist) >= 2:
        h_now = hist.iloc[-1]
        h_prev = hist.iloc[-2]
        if h_now > 0:
            mom_dir = "bullish" if h_now > h_prev else "bullish_fading"
        else:
            mom_dir = "bearish" if h_now < h_prev else "bearish_fading"
    return {
        "active": active,
        "bars": bars_in_squeeze,
        "momentum": mom_dir,
        "bars_since_release": bars_since_release,
    }


def _ichimoku(df, tenkan_len=9, kijun_len=26, senkou_b_len=52, displacement=26):
    """Ichimoku Cloud snapshot."""
    if len(df) < senkou_b_len + displacement:
        return None
    tenkan = _donchian_mid(df, tenkan_len)
    kijun = _donchian_mid(df, kijun_len)
    senkou_a = ((tenkan + kijun) / 2).shift(displacement)
    senkou_b = _donchian_mid(df, senkou_b_len).shift(displacement)
    price = df['close'].iloc[-1]
    t_val = tenkan.iloc[-1]
    k_val = kijun.iloc[-1]
    sa_val = senkou_a.iloc[-1]
    sb_val = senkou_b.iloc[-1]
    if any(pd.isna(x) for x in [t_val, k_val, sa_val, sb_val]):
        return None
    cloud_top = max(sa_val, sb_val)
    cloud_bottom = min(sa_val, sb_val)
    cloud_color = "bullish" if sa_val >= sb_val else "bearish"
    if price > cloud_top:
        cloud_position = f"above_{cloud_color}"
    elif price < cloud_bottom:
        cloud_position = f"below_{cloud_color}"
    else:
        cloud_position = "inside"
    # TK cross
    if len(tenkan) >= 2 and len(kijun) >= 2:
        t_prev, k_prev = tenkan.iloc[-2], kijun.iloc[-2]
        if t_prev <= k_prev and t_val > k_val:
            tk_cross = "bullish"
        elif t_prev >= k_prev and t_val < k_val:
            tk_cross = "bearish"
        else:
            tk_cross = "none"
    else:
        tk_cross = "none"
    return {
        "cloud_position": cloud_position,
        "cloud_color": cloud_color,
        "tk_cross": tk_cross,
        "tenkan": round(t_val, 2),
        "kijun": round(k_val, 2),
        "cloud_top": round(cloud_top, 2),
        "cloud_bottom": round(cloud_bottom, 2),
    }


def _psar(df, af_start=0.02, af_step=0.02, af_max=0.20):
    """Parabolic SAR snapshot."""
    if len(df) < 3:
        return None
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    n = len(df)
    sar = np.full(n, np.nan)
    direction = np.zeros(n, dtype=int)  # 1=long, -1=short
    # Initialize
    is_long = closes[1] >= closes[0]
    af = af_start
    ep = highs[0] if is_long else lows[0]
    sar[0] = lows[0] if is_long else highs[0]
    direction[0] = 1 if is_long else -1
    for i in range(1, n):
        new_sar = sar[i - 1] + af * (ep - sar[i - 1])
        if is_long:
            new_sar = min(new_sar, lows[i - 1])
            if i >= 2:
                new_sar = min(new_sar, lows[i - 2])
            if new_sar > lows[i]:
                is_long = False
                new_sar = ep
                af = af_start
                ep = lows[i]
            else:
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + af_step, af_max)
        else:
            new_sar = max(new_sar, highs[i - 1])
            if i >= 2:
                new_sar = max(new_sar, highs[i - 2])
            if new_sar < highs[i]:
                is_long = True
                new_sar = ep
                af = af_start
                ep = highs[i]
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + af_step, af_max)
        sar[i] = new_sar
        direction[i] = 1 if is_long else -1
    last_dir = direction[-1]
    flip = direction[-1] != direction[-2] if n >= 2 else False
    return {
        "direction": "long" if last_dir == 1 else "short",
        "value": round(float(sar[-1]), 2),
        "flip": bool(flip),
    }


def _supertrend(df, period=10, mult=3.0):
    """SuperTrend snapshot."""
    atr = _atr(df, period)
    if atr.isna().all():
        return None
    hl2 = (df['high'] + df['low']) / 2
    upper_band = hl2 + mult * atr
    lower_band = hl2 - mult * atr
    close = df['close']
    n = len(df)
    st = np.full(n, np.nan)
    dirs = np.zeros(n, dtype=int)
    ub = upper_band.values.copy()
    lb = lower_band.values.copy()
    # Find first valid index
    start = period
    if start >= n:
        return None
    dirs[start] = 1
    st[start] = lb[start]
    for i in range(start + 1, n):
        # Adjust bands
        if lb[i] > lb[i - 1] and close.iloc[i - 1] > lb[i - 1]:
            pass  # keep
        elif close.iloc[i - 1] <= lb[i - 1]:
            pass  # keep
        else:
            lb[i] = lb[i - 1]
        if ub[i] < ub[i - 1] and close.iloc[i - 1] < ub[i - 1]:
            pass  # keep
        elif close.iloc[i - 1] >= ub[i - 1]:
            pass  # keep
        else:
            ub[i] = ub[i - 1]
        if dirs[i - 1] == 1:
            if close.iloc[i] < lb[i]:
                dirs[i] = -1
                st[i] = ub[i]
            else:
                dirs[i] = 1
                st[i] = lb[i]
        else:
            if close.iloc[i] > ub[i]:
                dirs[i] = 1
                st[i] = lb[i]
            else:
                dirs[i] = -1
                st[i] = ub[i]
    last_dir = dirs[-1]
    flip = dirs[-1] != dirs[-2] if n >= start + 2 else False
    return {
        "direction": "long" if last_dir == 1 else "short",
        "value": round(float(st[-1]), 2),
        "flip": bool(flip),
    }


def _stochastic(df, k_period=14, k_smooth=3, d_smooth=3):
    """Stochastic Oscillator snapshot."""
    if len(df) < k_period + k_smooth + d_smooth:
        return None
    hh = df['high'].rolling(k_period).max()
    ll = df['low'].rolling(k_period).min()
    raw_k = (df['close'] - ll) / (hh - ll) * 100
    raw_k = raw_k.replace([np.inf, -np.inf], np.nan)
    pct_k = raw_k.rolling(k_smooth).mean()
    pct_d = pct_k.rolling(d_smooth).mean()
    k_val = pct_k.iloc[-1]
    d_val = pct_d.iloc[-1]
    if pd.isna(k_val):
        return None
    if k_val < 20:
        zone = "oversold"
    elif k_val > 80:
        zone = "overbought"
    else:
        zone = "neutral"
    return {
        "k": round(k_val, 1),
        "d": round(d_val, 1) if pd.notna(d_val) else None,
        "zone": zone,
    }


def _stoch_rsi(df, rsi_period=14, stoch_period=14, k_smooth=3, d_smooth=3):
    """Stochastic RSI snapshot."""
    if len(df) < rsi_period + stoch_period + k_smooth + d_smooth:
        return None
    close = df['close']
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi_hh = rsi.rolling(stoch_period).max()
    rsi_ll = rsi.rolling(stoch_period).min()
    stoch_rsi_raw = (rsi - rsi_ll) / (rsi_hh - rsi_ll) * 100
    stoch_rsi_raw = stoch_rsi_raw.replace([np.inf, -np.inf], np.nan)
    k = stoch_rsi_raw.rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()
    k_val = k.iloc[-1]
    d_val = d.iloc[-1]
    if pd.isna(k_val):
        return None
    if k_val < 20:
        zone = "oversold"
    elif k_val > 80:
        zone = "overbought"
    else:
        zone = "neutral"
    return {
        "k": round(k_val, 1),
        "d": round(d_val, 1) if pd.notna(d_val) else None,
        "zone": zone,
    }


def _obv(df):
    """On-Balance Volume snapshot with divergence detection."""
    if len(df) < 20:
        return None
    close = df['close']
    volume = df['volume'] if 'volume' in df.columns else None
    if volume is None or volume.sum() == 0:
        return None
    sign = np.sign(close.diff())
    sign.iloc[0] = 0
    obv = (sign * volume).cumsum()
    # Trend: compare OBV 20-bar SMA direction vs price direction
    obv_sma = obv.rolling(20).mean()
    price_chg = close.iloc[-1] - close.iloc[-20] if len(close) >= 20 else 0
    obv_chg = obv_sma.iloc[-1] - obv_sma.iloc[-20] if len(obv_sma) >= 20 and pd.notna(obv_sma.iloc[-20]) else 0
    if price_chg > 0 and obv_chg < 0:
        divergence = "bearish"
    elif price_chg < 0 and obv_chg > 0:
        divergence = "bullish"
    else:
        divergence = "none"
    trend = "rising" if obv_chg > 0 else ("falling" if obv_chg < 0 else "flat")
    return {
        "trend": trend,
        "divergence": divergence,
    }


def _auto_fib(df, lookback=265):
    """Auto Fibonacci levels from swing high/low."""
    if len(df) < 20:
        return None
    window = df.tail(min(lookback, len(df)))
    highest = window['high'].max()
    lowest = window['low'].min()
    highest_idx = window['high'].idxmax()
    lowest_idx = window['low'].idxmin()
    if highest == lowest:
        return None
    # Determine trend direction based on which extreme came first
    is_uptrend = window.index.get_loc(lowest_idx) < window.index.get_loc(highest_idx)
    span = highest - lowest
    ratios = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    if is_uptrend:
        levels = {r: round(lowest + span * r, 2) for r in ratios}
    else:
        levels = {r: round(highest - span * r, 2) for r in ratios}
    price = df['close'].iloc[-1]
    # Find position in fib structure
    sorted_lvls = sorted(levels.values())
    position = None
    for i in range(len(sorted_lvls) - 1):
        if sorted_lvls[i] <= price <= sorted_lvls[i + 1]:
            position = f"between_{sorted_lvls[i]:,.0f}_and_{sorted_lvls[i + 1]:,.0f}"
            break
    if position is None:
        if price > max(sorted_lvls):
            position = "above_all"
        else:
            position = "below_all"
    return {
        "swing_high": round(highest, 2),
        "swing_low": round(lowest, 2),
        "trend": "uptrend" if is_uptrend else "downtrend",
        "levels": levels,
        "price_position": position,
    }


# --- Additional indicators (used when user selects them) ---

def _keltner(df, ema_len=20, atr_len=14, mult=1.5):
    """Keltner Channel snapshot."""
    if len(df) < max(ema_len, atr_len) + 5:
        return None
    basis = df['close'].ewm(span=ema_len, adjust=False).mean()
    atr = _atr(df, atr_len)
    upper = basis + atr * mult
    lower = basis - atr * mult
    last_u = upper.iloc[-1]
    last_l = lower.iloc[-1]
    last_b = basis.iloc[-1]
    if pd.isna(last_b):
        return None
    price = df['close'].iloc[-1]
    if price > last_u:
        position = "above_upper"
    elif price < last_l:
        position = "below_lower"
    else:
        position = "inside"
    return {
        "basis": round(last_b, 2),
        "upper": round(last_u, 2),
        "lower": round(last_l, 2),
        "position": position,
    }


def _bbw_percentile(df, bb_len=20, lookback=120, threshold=20):
    """Bollinger Band Width percentile."""
    if len(df) < bb_len + 10:
        return None
    close = df['close']
    sma = close.rolling(bb_len).mean()
    # Population std (ddof=0) for consistency with the
    # other BB calculations in this file (does not change the percentile
    # ranking itself — a constant-window rolling std under a fixed ddof is a
    # uniform scalar multiple of the ddof=0 series, which cancels out in the
    # < comparison — but keeps the raw bbw value itself correct/comparable).
    std = close.rolling(bb_len).std(ddof=0)
    bbw = ((sma + std * 2) - (sma - std * 2)) / sma
    bbw = bbw.dropna()
    if len(bbw) < 10:
        return None
    current = bbw.iloc[-1]
    window = bbw.tail(min(lookback, len(bbw)))
    pctl = (window < current).sum() / len(window) * 100
    return {
        "percentile": round(float(pctl), 1),
        "compression": bool(pctl < threshold),
    }


def _atr_percentile(df, atr_len=14, lookback=100, threshold=20):
    """ATR percentile — low values indicate compression."""
    atr = _atr(df, atr_len)
    atr = atr.dropna()
    if len(atr) < 10:
        return None
    current = atr.iloc[-1]
    window = atr.tail(min(lookback, len(atr)))
    pctl = (window < current).sum() / len(window) * 100
    return {
        "percentile": round(float(pctl), 1),
        "low_volatility": bool(pctl < threshold),
    }


def _williams_r(df, period=14):
    """%R oscillator."""
    if len(df) < period:
        return None
    hh = df['high'].rolling(period).max()
    ll = df['low'].rolling(period).min()
    r = (hh - df['close']) / (hh - ll) * -100
    val = r.iloc[-1]
    if pd.isna(val):
        return None
    if val < -80:
        zone = "oversold"
    elif val > -20:
        zone = "overbought"
    else:
        zone = "neutral"
    return {"value": round(val, 1), "zone": zone}


def _cci(df, period=20):
    """Commodity Channel Index."""
    if len(df) < period:
        return None
    tp = (df['high'] + df['low'] + df['close']) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    cci = (tp - sma) / (0.015 * mad)
    val = cci.iloc[-1]
    if pd.isna(val):
        return None
    if val > 100:
        zone = "overbought"
    elif val < -100:
        zone = "oversold"
    else:
        zone = "neutral"
    return {"value": round(val, 1), "zone": zone}


def _donchian(df, period=20):
    """Donchian Channel snapshot."""
    if len(df) < period:
        return None
    upper = df['high'].rolling(period).max().iloc[-1]
    lower = df['low'].rolling(period).min().iloc[-1]
    if pd.isna(upper):
        return None
    mid = (upper + lower) / 2
    price = df['close'].iloc[-1]
    # Breakout detection
    prev_upper = df['high'].rolling(period).max().iloc[-2] if len(df) > period else upper
    prev_lower = df['low'].rolling(period).min().iloc[-2] if len(df) > period else lower
    breakout = None
    if price >= upper and df['close'].iloc[-2] < prev_upper:
        breakout = "bullish"
    elif price <= lower and df['close'].iloc[-2] > prev_lower:
        breakout = "bearish"
    return {
        "upper": round(upper, 2),
        "lower": round(lower, 2),
        "mid": round(mid, 2),
        "breakout": breakout,
    }


def _hull_ma(df, period=20):
    """Hull Moving Average snapshot."""
    sqrt_p = round(math.sqrt(period))
    half_p = period // 2
    needed = period + sqrt_p
    if len(df) < needed:
        return None
    close = df['close']
    wma_half = _wma(close, half_p)
    wma_full = _wma(close, period)
    intermediate = 2 * wma_half - wma_full
    hma = _wma(intermediate, sqrt_p)
    val = hma.iloc[-1]
    prev = hma.iloc[-2] if len(hma) >= 2 else val
    if pd.isna(val):
        return None
    return {
        "value": round(val, 2),
        "direction": "rising" if val > prev else "falling",
    }


def _alma(df, period=20, offset=0.85, sigma=6):
    """Arnaud Legoux Moving Average snapshot."""
    if len(df) < period:
        return None
    close = df['close'].values
    m = math.floor(offset * (period - 1))
    s = period / sigma
    weights = np.array([math.exp(-((i - m) ** 2) / (2 * s * s)) for i in range(period)])
    weights /= weights.sum()
    if len(close) < period:
        return None
    val = np.dot(close[-period:], weights)
    prev_val = np.dot(close[-period - 1:-1], weights) if len(close) > period else val
    return {
        "value": round(float(val), 2),
        "direction": "rising" if val > prev_val else "falling",
    }


def _kama(df, period=10, fast_period=2, slow_period=30):
    """Kaufman Adaptive Moving Average snapshot."""
    if len(df) < period + 5:
        return None
    close = df['close'].values
    fast_sc = 2.0 / (fast_period + 1)
    slow_sc = 2.0 / (slow_period + 1)
    n = len(close)
    kama_vals = np.full(n, np.nan)
    kama_vals[period] = close[period]
    for i in range(period + 1, n):
        direction = abs(close[i] - close[i - period])
        volatility = sum(abs(close[i - j] - close[i - j - 1]) for j in range(period))
        if volatility == 0:
            er = 0
        else:
            er = direction / volatility
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        kama_vals[i] = kama_vals[i - 1] + sc * (close[i] - kama_vals[i - 1])
    val = kama_vals[-1]
    prev = kama_vals[-2] if n > period + 1 else val
    if np.isnan(val):
        return None
    return {
        "value": round(float(val), 2),
        "direction": "rising" if val > prev else "falling",
    }


def _ema_cross(df, fast=9, slow=21):
    """EMA Cross snapshot with crossover detection."""
    if len(df) < slow + 2:
        return None
    close = df['close']
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    f_now, s_now = ema_fast.iloc[-1], ema_slow.iloc[-1]
    f_prev, s_prev = ema_fast.iloc[-2], ema_slow.iloc[-2]
    state = "bullish" if f_now > s_now else "bearish"
    cross = None
    if f_prev <= s_prev and f_now > s_now:
        cross = "bullish"
    elif f_prev >= s_prev and f_now < s_now:
        cross = "bearish"
    # Count bars since last cross
    bars_since = 0
    for i in range(len(ema_fast) - 2, 0, -1):
        above_now = ema_fast.iloc[i] > ema_slow.iloc[i]
        above_prev = ema_fast.iloc[i - 1] > ema_slow.iloc[i - 1]
        if above_now != above_prev:
            break
        bars_since += 1
    return {
        "state": state,
        "cross": cross,
        "bars_since_cross": bars_since,
        "fast": round(f_now, 2),
        "slow": round(s_now, 2),
    }


def _alligator(df, jaw_len=13, jaw_off=8, teeth_len=8, teeth_off=5, lips_len=5, lips_off=3):
    """Bill Williams Alligator snapshot."""
    needed = jaw_len + jaw_off + 5
    if len(df) < needed:
        return None
    hl2 = (df['high'] + df['low']) / 2
    jaw_raw = _smma(hl2, jaw_len)
    teeth_raw = _smma(hl2, teeth_len)
    lips_raw = _smma(hl2, lips_len)
    # Apply offset (backward displacement — shift values forward in time)
    jaw = jaw_raw.shift(jaw_off)
    teeth = teeth_raw.shift(teeth_off)
    lips = lips_raw.shift(lips_off)
    j_val = jaw.iloc[-1]
    t_val = teeth.iloc[-1]
    l_val = lips.iloc[-1]
    if any(pd.isna(x) for x in [j_val, t_val, l_val]):
        return None
    price = df['close'].iloc[-1]
    # State detection
    if l_val > t_val > j_val and price > l_val:
        state = "eating_bullish"
    elif l_val < t_val < j_val and price < l_val:
        state = "eating_bearish"
    elif abs(l_val - t_val) / t_val < 0.005 and abs(t_val - j_val) / j_val < 0.005:
        state = "sleeping"
    else:
        state = "mixed"
    return {
        "state": state,
        "jaw": round(j_val, 2),
        "teeth": round(t_val, 2),
        "lips": round(l_val, 2),
    }


def _consensio(df, period_s=3, period_m=7, period_l=30):
    """Consensio — multi-MA alignment signal.

    The `signal` field is delegated to the canonical
    ConsensioCalculator.calculate_signal() (consensio/indicators/consensio_core.py)
    — the single source of truth also used by the scanner/precompute scoring
    path and consensio_core's own golden tests — via a thin adapter (it takes
    the OHLCV DataFrame + the same period_short/medium/long this function
    already uses, so no shape mismatch). A separate local truth table (e.g. one
    with a price-vs-MA comparison, which the canonical calculator does NOT do)
    could disagree with the canonical signal for the same MA ordering (e.g.
    M>S>L giving -1 vs +2 canonically); this snapshot feeds
    compute_indicator_snapshot() -> morning-report / personal-indicator
    snapshot, so such a divergence would reach user-facing output. The
    canonical truth table is pure MA-ordering
    (no price interaction, no magnitude weighting):
        S>M>L -> +3   M>S>L -> +2   S>L>M -> +1
        M>L>S -> -1   L>S>M -> -2   L>M>S -> -3   (any tie -> 0)
    """
    if len(df) < period_l + 5:
        return None
    close = df['close']
    sma_s = close.rolling(period_s).mean().iloc[-1]
    sma_m = close.rolling(period_m).mean().iloc[-1]
    sma_l = close.rolling(period_l).mean().iloc[-1]
    if any(pd.isna(x) for x in [sma_s, sma_m, sma_l]):
        return None
    signal = ConsensioCalculator(
        period_short=period_s, period_medium=period_m, period_long=period_l,
    ).calculate_signal(df)
    return {
        "signal": signal,
        "sma_short": round(sma_s, 2),
        "sma_mid": round(sma_m, 2),
        "sma_long": round(sma_l, 2),
    }


def _sr_channels(df, pivot_period=10, channel_width_pct=5, min_strength=1, max_channels=6, lookback=290):
    """Support/Resistance channel detection."""
    window = df.tail(min(lookback, len(df)))
    if len(window) < pivot_period * 2 + 1:
        return None
    highs = window['high'].values
    lows = window['low'].values
    close_last = df['close'].iloc[-1]
    pivots = []
    for i in range(pivot_period, len(window) - pivot_period):
        is_pivot_high = all(highs[i] > highs[i - j] and highs[i] > highs[i + j] for j in range(1, pivot_period + 1))
        is_pivot_low = all(lows[i] < lows[i - j] and lows[i] < lows[i + j] for j in range(1, pivot_period + 1))
        if is_pivot_high:
            pivots.append(("R", highs[i]))
        if is_pivot_low:
            pivots.append(("S", lows[i]))
    if not pivots:
        return None
    # Cluster pivots
    price_range = highs.max() - lows.min()
    if price_range == 0:
        return None
    channel_w = price_range * channel_width_pct / 100
    channels = []
    used = [False] * len(pivots)
    for i, (ptype, plevel) in enumerate(pivots):
        if used[i]:
            continue
        cluster = [plevel]
        used[i] = True
        for j in range(i + 1, len(pivots)):
            if not used[j] and abs(pivots[j][1] - plevel) <= channel_w:
                cluster.append(pivots[j][1])
                used[j] = True
        if len(cluster) >= min_strength:
            avg_level = sum(cluster) / len(cluster)
            sr_type = "R" if avg_level > close_last else "S"
            channels.append({
                "type": sr_type,
                "level": round(avg_level, 2),
                "strength": len(cluster),
            })
    channels.sort(key=lambda x: x["strength"], reverse=True)
    return channels[:max_channels] if channels else None


def _fvg(df, atr_threshold_pct=0.25, atr_len=14, max_gaps=10):
    """Fair Value Gap detection — unfilled gaps only."""
    if len(df) < atr_len + 3:
        return None
    atr = _atr(df, atr_len)
    highs = df['high'].values
    lows = df['low'].values
    close_last = df['close'].iloc[-1]
    bullish_gaps = []
    bearish_gaps = []
    for i in range(2, len(df)):
        threshold = atr.iloc[i] * atr_threshold_pct if pd.notna(atr.iloc[i]) else 0
        # Bullish FVG: current low > 2-bars-ago high
        if lows[i] > highs[i - 2] and (lows[i] - highs[i - 2]) >= threshold:
            gap = {"top": round(lows[i], 2), "bottom": round(highs[i - 2], 2)}
            # Check if filled by subsequent price action
            filled = any(lows[j] <= gap["bottom"] for j in range(i + 1, len(df)))
            if not filled:
                bullish_gaps.append(gap)
        # Bearish FVG: current high < 2-bars-ago low
        if highs[i] < lows[i - 2] and (lows[i - 2] - highs[i]) >= threshold:
            gap = {"top": round(lows[i - 2], 2), "bottom": round(highs[i], 2)}
            filled = any(highs[j] >= gap["top"] for j in range(i + 1, len(df)))
            if not filled:
                bearish_gaps.append(gap)
    # Keep only recent unfilled gaps
    bullish_gaps = bullish_gaps[-max_gaps:]
    bearish_gaps = bearish_gaps[-max_gaps:]
    if not bullish_gaps and not bearish_gaps:
        return None
    # Find nearest gap to current price
    all_gaps = [(g, "bullish") for g in bullish_gaps] + [(g, "bearish") for g in bearish_gaps]
    nearest = min(all_gaps, key=lambda x: min(abs(close_last - x[0]["top"]), abs(close_last - x[0]["bottom"])))
    return {
        "bullish_count": len(bullish_gaps),
        "bearish_count": len(bearish_gaps),
        "nearest_type": nearest[1],
        "nearest_top": nearest[0]["top"],
        "nearest_bottom": nearest[0]["bottom"],
    }


def _nr7(df, lookback=7):
    """NR7 — Narrowest Range of 7 bars."""
    if len(df) < lookback:
        return None
    ranges = df['high'] - df['low']
    current_range = ranges.iloc[-1]
    window = ranges.iloc[-lookback:]
    is_nr7 = current_range == window.min() and current_range < window.iloc[:-1].min()
    return {"active": bool(is_nr7)}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_indicator_snapshot(df, tf='1D'):
    """
    Compute extended indicator snapshots (latest values + state only).

    Args:
        df: OHLCV DataFrame (already tail'd, index = datetime).
        tf: Timeframe string (e.g. '4H', '1D', '3D', '1W').

    Returns:
        dict of indicator_key -> snapshot dict. Only includes indicators
        that computed successfully.
    """
    result = {}
    skip_fast = tf in SKIP_FAST_OSCILLATORS_TFS

    # --- High-value subset (always computed, appears in shared report) ---
    try:
        snap = _bollinger_bands(df)
        if snap:
            result['boll'] = snap
    except Exception as e:
        logger.debug("boll failed: %s", e)

    try:
        snap = _ttm_squeeze(df)
        if snap:
            result['ttm_squeeze'] = snap
    except Exception as e:
        logger.debug("ttm_squeeze failed: %s", e)

    try:
        snap = _ichimoku(df)
        if snap:
            result['ichimoku'] = snap
    except Exception as e:
        logger.debug("ichimoku failed: %s", e)

    try:
        snap = _psar(df)
        if snap:
            result['psar'] = snap
    except Exception as e:
        logger.debug("psar failed: %s", e)

    try:
        snap = _supertrend(df)
        if snap:
            result['supertrend'] = snap
    except Exception as e:
        logger.debug("supertrend failed: %s", e)

    if not skip_fast:
        try:
            snap = _stochastic(df)
            if snap:
                result['stoch'] = snap
        except Exception as e:
            logger.debug("stoch failed: %s", e)

        try:
            snap = _stoch_rsi(df)
            if snap:
                result['stoch_rsi'] = snap
        except Exception as e:
            logger.debug("stoch_rsi failed: %s", e)

    try:
        snap = _obv(df)
        if snap:
            result['obv'] = snap
    except Exception as e:
        logger.debug("obv failed: %s", e)

    try:
        snap = _auto_fib(df)
        if snap:
            result['auto_fib'] = snap
    except Exception as e:
        logger.debug("auto_fib failed: %s", e)

    # --- Additional indicators (for personal section when user selects) ---
    try:
        snap = _keltner(df)
        if snap:
            result['keltner'] = snap
    except Exception as e:
        logger.debug("keltner failed: %s", e)

    try:
        snap = _bbw_percentile(df)
        if snap:
            result['bbw_pct'] = snap
    except Exception as e:
        logger.debug("bbw_pct failed: %s", e)

    try:
        snap = _atr_percentile(df)
        if snap:
            result['atr_pct'] = snap
    except Exception as e:
        logger.debug("atr_pct failed: %s", e)

    if not skip_fast:
        try:
            snap = _williams_r(df)
            if snap:
                result['williams_r'] = snap
        except Exception as e:
            logger.debug("williams_r failed: %s", e)

        try:
            snap = _cci(df)
            if snap:
                result['cci'] = snap
        except Exception as e:
            logger.debug("cci failed: %s", e)

    try:
        snap = _donchian(df)
        if snap:
            result['donchian'] = snap
    except Exception as e:
        logger.debug("donchian failed: %s", e)

    try:
        snap = _hull_ma(df)
        if snap:
            result['hull_ma'] = snap
    except Exception as e:
        logger.debug("hull_ma failed: %s", e)

    try:
        snap = _alma(df)
        if snap:
            result['alma'] = snap
    except Exception as e:
        logger.debug("alma failed: %s", e)

    try:
        snap = _kama(df)
        if snap:
            result['kama'] = snap
    except Exception as e:
        logger.debug("kama failed: %s", e)

    try:
        snap = _ema_cross(df)
        if snap:
            result['ema_cross'] = snap
    except Exception as e:
        logger.debug("ema_cross failed: %s", e)

    try:
        snap = _alligator(df)
        if snap:
            result['alligator'] = snap
    except Exception as e:
        logger.debug("alligator failed: %s", e)

    try:
        snap = _consensio(df)
        if snap:
            result['consensio'] = snap
    except Exception as e:
        logger.debug("consensio failed: %s", e)

    try:
        snap = _sr_channels(df)
        if snap:
            result['sr_channels'] = snap
    except Exception as e:
        logger.debug("sr_channels failed: %s", e)

    try:
        snap = _fvg(df)
        if snap:
            result['fvg'] = snap
    except Exception as e:
        logger.debug("fvg failed: %s", e)

    try:
        snap = _nr7(df)
        if snap:
            result['nr7'] = snap
    except Exception as e:
        logger.debug("nr7 failed: %s", e)

    return _clean_nan(result)


# ---------------------------------------------------------------------------
# Cockpit-specific subset (lightweight, for precompute loop)
# ---------------------------------------------------------------------------

COCKPIT_EXTENDED_KEYS = [
    'ichimoku', 'alligator', 'stoch_rsi', 'ttm_squeeze',
    'supertrend', 'psar', 'donchian', 'nr7',
    'bbw_pct', 'atr_pct', 'ema_cross', 'fvg', 'obv',
]


def compute_cockpit_snapshot(df, tf='1D'):
    """
    Compute cockpit-specific extended indicator snapshots.

    Lighter subset of compute_indicator_snapshot() — only indicators
    shown in the asset cockpit panel. Called once per asset in the
    precompute loop.

    Args:
        df: OHLCV DataFrame (already tail'd, index = datetime).
        tf: Timeframe string (e.g. '1D', '3D', '1W').

    Returns:
        dict of indicator_key -> snapshot dict.
    """
    if df is None or df.empty or len(df) < 10:
        return {}

    result = {}
    skip_fast = tf in SKIP_FAST_OSCILLATORS_TFS

    for key, fn in [
        ('ichimoku', _ichimoku),
        ('alligator', _alligator),
        ('ttm_squeeze', _ttm_squeeze),
        ('supertrend', _supertrend),
        ('psar', _psar),
        ('donchian', _donchian),
        ('nr7', _nr7),
        ('bbw_pct', _bbw_percentile),
        ('atr_pct', _atr_percentile),
        ('ema_cross', _ema_cross),
        ('fvg', _fvg),
        ('obv', _obv),
    ]:
        try:
            snap = fn(df)
            if snap:
                result[key] = snap
        except Exception as e:
            logger.debug("cockpit %s failed: %s", key, e)

    # Stoch RSI — skip on higher timeframes
    if not skip_fast:
        try:
            snap = _stoch_rsi(df)
            if snap:
                result['stoch_rsi'] = snap
        except Exception as e:
            logger.debug("cockpit stoch_rsi failed: %s", e)

    return _clean_nan(result)
