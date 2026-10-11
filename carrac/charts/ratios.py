"""
Ratio chart helpers — compute OHLCV ratios (e.g. BTC/SPX) and ratio consensio.

Ratio and relative-strength chart helpers.
"""

import logging

import pandas as pd
from flask import jsonify

from carrac.services import get_services

logger = logging.getLogger('carrac.charts.ratios')


def _get_db():
    return get_services().db


def _get_consensio():
    return get_services().consensio


def _handle_ratio_chart(num_sym: str, den_sym: str, timeframe: str, days: int, include_macd: bool = False):
    """Calculate ratio OHLCV (numerator / denominator) and return chart JSON.

    Always builds the ratio from aligned 1D data, then resamples to the target
    timeframe.  This avoids 3D resample-anchor misalignment between assets.

    When ``include_macd`` is True a top-level ``macd`` dict (line/signal/
    histogram) is appended — same shape /api/chart-internal ships for ratios.
    Default False keeps the response byte-identical for existing consumers.
    """
    tf_map = {'1D': '1D', '3D': '3D', '1W': '1W', '1M': '1M'}
    tf = tf_map.get(timeframe.upper(), '1D')

    ratio_df = _build_ratio_df(num_sym, den_sym, tf)
    if ratio_df.empty:
        return jsonify({'error': f'No {tf} data for {num_sym}/{den_sym}'}), 404

    ratio_df = ratio_df.tail(days)

    ohlcv = []
    for idx in ratio_df.index:
        row = ratio_df.loc[idx]
        ts = int(pd.Timestamp(idx).timestamp()) if not hasattr(idx, 'timestamp') else int(idx.timestamp())
        ohlcv.append({
            'time': ts,
            'open': float(row['open']),
            'high': float(row['high']),
            'low': float(row['low']),
            'close': float(row['close']),
        })

    payload = {
        'symbol': f'{num_sym}_{den_sym}',
        'timeframe': tf,
        'ohlcv': ohlcv,
        'volume': [],
        'indicators': {}
    }
    if include_macd:
        from carrac.charts.indicators import compute_macd_series
        payload['macd'] = compute_macd_series(ratio_df, symbol=f'{num_sym}_{den_sym}')
    return jsonify(payload)


def _build_ratio_df(num_sym: str, den_sym: str, tf: str) -> pd.DataFrame:
    """Build a ratio OHLCV DataFrame for a single timeframe.

    Always computes from aligned 1D data first, then resamples to the target
    timeframe.  This avoids the 3D resample-anchor misalignment bug where
    independently resampled assets get different bin start dates → zero overlap.
    """
    db = _get_db()
    num_data = db.get_multi_timeframe_data(num_sym)
    den_data = db.get_multi_timeframe_data(den_sym)
    if not num_data or not den_data:
        return pd.DataFrame()
    # Always start from 1D — the only timeframe guaranteed to align
    num_df = num_data.get('1D', pd.DataFrame())
    den_df = den_data.get('1D', pd.DataFrame())
    if num_df.empty or den_df.empty:
        return pd.DataFrame()
    common = num_df.index.intersection(den_df.index)
    if len(common) == 0:
        return pd.DataFrame()
    n = num_df.loc[common]
    d = den_df.loc[common]
    # Avoid division by zero
    mask = (d['close'] != 0) & (d['open'] != 0) & (d['high'] != 0) & (d['low'] != 0)
    n = n[mask]
    d = d[mask]
    if n.empty:
        return pd.DataFrame()
    ratio = pd.DataFrame({
        'open': n['open'].values / d['open'].values,
        'high': n['high'].values / d['low'].values,
        'low': n['low'].values / d['high'].values,
        'close': n['close'].values / d['close'].values,
        'volume': 0.0,  # placeholder — resample_to_timeframe requires it
    }, index=n.index)
    # Resample from the unified 1D ratio to the target timeframe
    if tf != '1D':
        ratio = db.resample_to_timeframe(ratio, tf)
    return ratio


def _compute_ratio_consensio(symbol: str) -> dict | None:
    """Compute consensio for a ratio symbol like BTC_SPX, with prev-day change."""
    parts = symbol.split('_')
    if len(parts) != 2:
        return None
    num_sym, den_sym = parts
    consensio = _get_consensio()
    try:
        df_1d = _build_ratio_df(num_sym, den_sym, '1D')
        df_3d = _build_ratio_df(num_sym, den_sym, '3D')
        df_1w = _build_ratio_df(num_sym, den_sym, '1W')
        if df_1d.empty:
            return None
        result = consensio.calculate_multi_timeframe(df_1d, df_3d, df_1w)
        entry = {
            '1D': result['1D']['signal'],
            '3D': result['3D']['signal'],
            '1W': result['1W']['signal'],
            'total': result['total']['positions'],
        }
        # Compute prev-day signals using data without last row
        prev_1d = df_1d.iloc[:-1] if len(df_1d) > 1 else df_1d
        prev_3d = df_3d.iloc[:-1] if len(df_3d) > 1 else df_3d
        prev_1w = df_1w.iloc[:-1] if len(df_1w) > 1 else df_1w
        prev_result = consensio.calculate_multi_timeframe(prev_1d, prev_3d, prev_1w)
        entry['prev_1D'] = prev_result['1D']['signal']
        entry['prev_3D'] = prev_result['3D']['signal']
        entry['prev_1W'] = prev_result['1W']['signal']
        entry['prev_total'] = prev_result['total']['positions']
        return entry
    except Exception:
        return None
