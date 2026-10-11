"""
Shared indicator computation for chart routes.

Deduplicates the EMA Ribbon, Weekly MAs, Bull Band, SMA lines, RSI, and MACD
calculations that were copy-pasted between /api/chart/ and /api/chart-internal/.
"""

import logging

import numpy as np
import pandas as pd

from consensio.indicators.ema_ribbon import _ema_pine_compat  # noqa: F401 — re-exported for callers

logger = logging.getLogger('carrac.charts.indicators')


def compute_sma_lines(df, periods):
    """Simple-moving-average overlay lines for a set of periods.

    Single source of truth for the "SMA 20/200" chart overlay, shared by
    ``compute_chart_indicators`` (sma20/50/200, via /api/chart-internal) and
    the public ``/api/chart/<symbol>`` route (sma20/200 only), so the two
    cannot diverge. Returns a dict keyed ``'sma<period>'``
    -> list of ``{'time': int, 'value': float}`` points (NaN-dropped bars,
    values rounded to 2dp), matching the historical payload shape exactly.
    """
    close = df['close']
    sma_lines = {}
    for period in periods:
        name = f'sma{period}'
        sma_lines[name] = []
        series = close.rolling(window=period).mean()
        s_notna = series.dropna()
        if len(s_notna) > 0:
            s_ts = s_notna.index.astype('datetime64[s]').astype(int).tolist()
            s_vals = s_notna.round(2).astype(float).tolist()
            sma_lines[name] = [
                {'time': s_ts[i], 'value': s_vals[i]}
                for i in range(len(s_ts))
            ]
    return sma_lines


def compute_macd_series(df, symbol=None):
    """MACD(12, 26, 9) line/signal/histogram arrays for a chart payload.

    Single source of truth for the chart MACD series, shared by
    ``compute_chart_indicators`` (which powers /api/chart-internal via
    build_chart_payload) and the public /api/chart/<symbol> route's
    optional ``?macd=1`` overlay.

    Returns a dict ``{'line': [...], 'signal': [...], 'histogram': [...]}``
    of ``{'time': int, 'value': float}`` points, or an empty dict when the
    series can't be computed (fewer than 35 bars, or on error).
    """
    if len(df) < 35:
        return {}
    try:
        close_m = df['close']
        ema12 = _ema_pine_compat(close_m, 12)
        ema26 = _ema_pine_compat(close_m, 26)
        macd_line = ema12 - ema26
        signal_line = _ema_pine_compat(macd_line, 9)
        histogram = macd_line - signal_line
        macd_arr, signal_arr, hist_arr = [], [], []
        for idx_m, row_idx_m in enumerate(df.index):
            ts_m = int(row_idx_m.timestamp()) if hasattr(row_idx_m, 'timestamp') else int(pd.Timestamp(row_idx_m).timestamp())
            mv = macd_line.iloc[idx_m]
            sv = signal_line.iloc[idx_m]
            hv = histogram.iloc[idx_m]
            if pd.notna(mv):
                macd_arr.append({'time': ts_m, 'value': round(float(mv), 2)})
            if pd.notna(sv):
                signal_arr.append({'time': ts_m, 'value': round(float(sv), 2)})
            if pd.notna(hv):
                hist_arr.append({'time': ts_m, 'value': round(float(hv), 2)})
        return {'line': macd_arr, 'signal': signal_arr, 'histogram': hist_arr}
    except Exception as e:
        logger.warning(f"MACD calculation error for {symbol}: {e}")
        return {}


def compute_chart_indicators(df, tf_data, symbol, rsi_calc=None):
    """
    Compute EMA Ribbon, Weekly MAs, Bull Band, SMA lines, RSI, MACD for chart response.

    Args:
        df: The OHLCV DataFrame for the current timeframe (already tail'd).
        tf_data: Multi-timeframe data dict (must contain '1W' for weekly MAs).
                 Can be None if not available.
        symbol: Symbol string (for logging).
        rsi_calc: RSICalculator instance (optional, for RSI series).

    Returns:
        dict with keys:
            'indicators' — dict of indicator overlay data (ema_ribbon, weeklyMAs, etc.)
            'rsi' — list of RSI data points [{time, value}, ...]
            'macd' — dict with line/signal/histogram arrays (or empty dict)
    """
    indicators = {}
    rsi_data = []
    macd_data = {}

    ts_list = df.index.astype('datetime64[s]').astype(int).tolist()

    # 1. EMA RIBBON (30/60 EMA with fill color based on trend)
    if len(df) >= 60:
        close = df['close']
        # SMA-seeded EMA (Pine-compatible) — see _ema_pine_compat docstring.
        ema_fast = _ema_pine_compat(close, 30)
        ema_slow = _ema_pine_compat(close, 60)

        # ATR margin for trend determination
        h_s, l_s = df['high'], df['low']
        tr = pd.concat([
            h_s - l_s,
            abs(h_s - close.shift(1)),
            abs(l_s - close.shift(1))
        ], axis=1).max(axis=1)
        atr = tr.rolling(window=60).mean()
        margin = 0.30 * atr

        ema_diff = ema_fast - ema_slow
        mask_valid = ema_fast.notna() & ema_slow.notna()
        trend_arr = np.where(
            ema_diff > margin.fillna(0), 'bull',
            np.where(ema_diff < -margin.fillna(0), 'bear', 'neutral')
        )
        color_map = {'bull': '#7cb518', 'bear': '#fb6107', 'neutral': '#f3de2c'}

        fast_vals = ema_fast.astype(float).tolist()
        slow_vals = ema_slow.astype(float).tolist()
        valid_mask = mask_valid.tolist()

        ema_ribbon_fast = []
        ema_ribbon_slow = []
        ema_ribbon_fill = []
        for i in range(len(ts_list)):
            if valid_mask[i]:
                c = color_map[trend_arr[i]]
                ema_ribbon_fast.append({'time': ts_list[i], 'value': fast_vals[i], 'color': c})
                ema_ribbon_slow.append({'time': ts_list[i], 'value': slow_vals[i], 'color': c})
                ema_ribbon_fill.append({'time': ts_list[i], 'trend': trend_arr[i]})

        indicators['ema_ribbon'] = {
            'fast': ema_ribbon_fast,
            'slow': ema_ribbon_slow,
            'fill': ema_ribbon_fill
        }

    # 2. WEEKLY MAs (50/100/200 WMA)
    if tf_data and '1W' in tf_data and not tf_data['1W'].empty:
        df_weekly = tf_data['1W']
        if len(df_weekly) >= 200:
            weekly_close = df_weekly['close']
            wma_50 = weekly_close.rolling(window=50).mean()
            wma_100 = weekly_close.rolling(window=100).mean()
            wma_200 = weekly_close.rolling(window=200).mean()

            weekly_mas = {'wma50': [], 'wma100': [], 'wma200': []}
            for name, series in [('wma50', wma_50), ('wma100', wma_100), ('wma200', wma_200)]:
                s_notna = series.dropna()
                if len(s_notna) > 0:
                    s_ts = s_notna.index.astype('datetime64[s]').astype(int).tolist()
                    s_vals = s_notna.astype(float).tolist()
                    weekly_mas[name] = [
                        {'time': s_ts[i], 'value': s_vals[i]}
                        for i in range(len(s_ts))
                    ]
            indicators['weeklyMAs'] = weekly_mas

        # Bull Market Support Band (21W EMA + 20W SMA)
        if len(df_weekly) >= 21:
            weekly_close_b = df_weekly['close']
            bmsb_ema21 = weekly_close_b.ewm(span=21).mean()
            bmsb_sma20 = weekly_close_b.rolling(window=20).mean()
            bull_band = {'ema21w': [], 'sma20w': []}
            for name_b, series_b in [('ema21w', bmsb_ema21), ('sma20w', bmsb_sma20)]:
                s_notna_b = series_b.dropna()
                if len(s_notna_b) > 0:
                    s_ts_b = s_notna_b.index.astype('datetime64[s]').astype(int).tolist()
                    s_vals_b = s_notna_b.astype(float).tolist()
                    bull_band[name_b] = [
                        {'time': s_ts_b[i], 'value': s_vals_b[i]}
                        for i in range(len(s_ts_b))
                    ]
            indicators['bullMarketBand'] = bull_band

    # 3. SMA 20/200 lines (chart overlay)
    if len(df) >= 220:
        try:
            indicators['smaLines'] = compute_sma_lines(df, [20, 50, 200])
        except Exception as e:
            logger.warning(f"SMA line error for {symbol}: {e}")

    # 4. RSI(14) series + RSI-based MA(14)
    if len(df) >= 15 and rsi_calc is not None:
        try:
            rsi_series = rsi_calc.calculate_series(df)
            if rsi_series is not None and len(rsi_series) > 0:
                rsi_ma = rsi_series.rolling(window=14, min_periods=14).mean()
                rsi_notna = rsi_series.dropna()
                if len(rsi_notna) > 0:
                    rsi_ts = rsi_notna.index.astype('datetime64[s]').astype(int).tolist()
                    rsi_vals = rsi_notna.round(2).astype(float).tolist()
                    rsi_ma_aligned = rsi_ma.reindex(rsi_notna.index)
                    rsi_data = []
                    for i in range(len(rsi_ts)):
                        pt = {'time': rsi_ts[i], 'value': rsi_vals[i]}
                        ma_v = rsi_ma_aligned.iloc[i]
                        if pd.notna(ma_v):
                            pt['ma'] = round(float(ma_v), 2)
                        rsi_data.append(pt)
        except Exception as e:
            logger.warning(f"RSI calculation error for {symbol}: {e}")

    # 5. MACD(12, 26, 9) — shared single-source-of-truth helper.
    macd_data = compute_macd_series(df, symbol=symbol)

    return {
        'indicators': indicators,
        'rsi': rsi_data,
        'macd': macd_data,
    }
