"""BollingerBandCalculator indicator(s).

Behaviour is pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd


class BollingerBandCalculator:
    """
    Calculate Bollinger Band width and squeeze detection.

    Measures current BB width as percentage of 60-day average width.
    Low % = squeeze (bands tight), High % = expansion (bands wide).
    """

    def __init__(self, period: int = 20, std_dev: float = 2.0, lookback: int = 60, offset: int = 0):
        """
        Initialize Bollinger Band calculator.

        Args:
            period: Period for BB calculation (default: 20)
            std_dev: Standard deviation multiplier (default: 2.0)
            lookback: Days to calculate average width (default: 60)
            offset: Shift bands ±N bars on the time axis (default: 0).
                    Positive = lag (shift right), negative = lead (shift left).
        """
        self.period = period
        self.std_dev = std_dev
        self.lookback = lookback
        self.offset = offset

    def calculate(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        Calculate Bollinger Band width and squeeze percentage.

        Returns last-bar scalar values only. For full-series access (e.g. to
        scan historical bars for a breakout), use :meth:`calculate_series`.

        Args:
            df: DataFrame with OHLCV data (needs 'close' column)

        Returns:
            Dictionary with:
            - upper: float (upper band price)
            - middle: float (middle band / SMA)
            - lower: float (lower band price)
            - width: float (current width in price)
            - width_pct: float (width as % of price)
            - avg_width_pct: float (60-day average width %)
            - squeeze_pct: float (current width as % of average - key metric)
            - is_squeeze: bool (squeeze_pct < 50 = significant squeeze)
        """
        # Old guard (period + lookback + 5 = 85 for defaults) was far
        # too aggressive — TradingView shows BB from bar `period` onwards and
        # uses all available bars for the squeeze avg.  We now only require at
        # least `period` bars to produce valid bands.  The lookback average for
        # squeeze_pct gracefully falls back to whatever data is available.
        if len(df) < self.period:
            return None

        try:
            close = df['close']

            # Calculate Bollinger Bands
            # Use population std (ddof=0) to match TradingView/Pine
            # Script stdev(), which is population variance, not sample variance.
            sma = close.rolling(window=self.period).mean()
            std = close.rolling(window=self.period).std(ddof=0)

            upper = sma + (self.std_dev * std)
            lower = sma - (self.std_dev * std)

            # Apply time-axis offset (TradingView-style).
            # shift(+N) lags the series (right shift, positive offset = lag).
            # shift(-N) leads the series (left shift, negative offset = lead).
            if self.offset != 0:
                upper = upper.shift(self.offset)
                sma = sma.shift(self.offset)
                lower = lower.shift(self.offset)

            # Current values
            current_upper = float(upper.iloc[-1])
            current_middle = float(sma.iloc[-1])
            current_lower = float(lower.iloc[-1])

            # Current width
            current_width = current_upper - current_lower
            current_width_pct = (current_width / current_middle * 100) if current_middle > 0 else 0

            # Calculate width series for lookback period
            width_series = upper - lower
            width_pct_series = (width_series / sma * 100).dropna()

            # Get last N periods for average (use lookback parameter)
            if len(width_pct_series) >= self.lookback:
                avg_width_pct = float(width_pct_series.iloc[-self.lookback:].mean())
            else:
                avg_width_pct = float(width_pct_series.mean())

            # Squeeze percentage: current width as % of average
            # 50% means current width is half of normal
            # 200% means current width is double normal
            squeeze_pct = (current_width_pct / avg_width_pct * 100) if avg_width_pct > 0 else 100

            # Handle NaN/Inf values that break JSON serialization
            import math
            if math.isnan(squeeze_pct) or math.isinf(squeeze_pct):
                squeeze_pct = 100.0
            if math.isnan(avg_width_pct) or math.isinf(avg_width_pct):
                avg_width_pct = 0.0
            if math.isnan(current_width_pct) or math.isinf(current_width_pct):
                current_width_pct = 0.0

            # Squeeze threshold: < 50% = significant squeeze
            is_squeeze = squeeze_pct < 50

            # Final NaN check for all numeric values
            def safe_float(val, default=0.0):
                if math.isnan(val) or math.isinf(val):
                    return default
                return val

            return {
                'upper': round(safe_float(current_upper), 4),
                'middle': round(safe_float(current_middle), 4),
                'lower': round(safe_float(current_lower), 4),
                'width': round(safe_float(current_width), 4),
                'width_pct': round(safe_float(current_width_pct), 2),
                'avg_width_pct': round(safe_float(avg_width_pct), 2),
                'squeeze_pct': round(safe_float(squeeze_pct, 100.0), 1),
                'is_squeeze': is_squeeze
            }

        except Exception:
            return None

    def calculate_series(self, df: pd.DataFrame):
        """
        Compute full upper/middle/lower Bollinger Band Series for all bars in *df*.

        Unlike :meth:`calculate`, which returns last-bar scalar values, this
        method returns three pandas Series aligned to *df*'s index so callers
        can inspect any historical bar (e.g. to detect breakouts within a
        lookback window).

        Args:
            df: DataFrame with OHLCV data (needs 'close' column)

        Returns:
            An object with attributes:
            - upper: pd.Series (upper band for each bar)
            - middle: pd.Series (SMA for each bar)
            - lower: pd.Series (lower band for each bar)
            Returns None if insufficient data (< period bars).
        """
        if df is None or len(df) < self.period:
            return None

        try:
            close = df['close']

            sma = close.rolling(window=self.period).mean()
            std = close.rolling(window=self.period).std(ddof=0)

            upper = sma + (self.std_dev * std)
            lower = sma - (self.std_dev * std)

            if self.offset != 0:
                upper = upper.shift(self.offset)
                sma = sma.shift(self.offset)
                lower = lower.shift(self.offset)

            class _BBSeries:
                pass

            result = _BBSeries()
            result.upper = upper
            result.middle = sma
            result.lower = lower
            return result

        except Exception:
            return None

    def calculate_squeeze_series(self, df: pd.DataFrame):
        """
        Compute full BB series plus a per-bar squeeze boolean Series.

        Like :meth:`calculate_series` but also returns ``is_squeeze`` — a
        boolean Series aligned to *df*'s index that is True whenever
        ``squeeze_pct < 50`` (current width is less than half the lookback
        average width).  This is the same threshold used by
        :meth:`calculate` for the scalar ``is_squeeze`` field.

        Args:
            df: DataFrame with OHLCV data (needs 'close' column)

        Returns:
            An object with attributes:
            - upper: pd.Series (upper band for each bar)
            - middle: pd.Series (SMA for each bar)
            - lower: pd.Series (lower band for each bar)
            - is_squeeze: pd.Series[bool] (True when squeeze_pct < 50)
            Returns None if insufficient data (< period bars).
        """
        if df is None or len(df) < self.period:
            return None

        try:
            close = df['close']

            sma = close.rolling(window=self.period).mean()
            std = close.rolling(window=self.period).std(ddof=0)

            upper = sma + (self.std_dev * std)
            lower = sma - (self.std_dev * std)

            if self.offset != 0:
                upper = upper.shift(self.offset)
                sma = sma.shift(self.offset)
                lower = lower.shift(self.offset)

            # Width series (same formula as calculate())
            width_series = upper - lower
            # width_pct: width as % of middle band price
            width_pct_series = width_series / sma * 100

            # Rolling lookback average — mirrors the scalar path in calculate()
            avg_width_pct_series = width_pct_series.rolling(
                window=self.lookback, min_periods=self.period
            ).mean()

            # squeeze_pct = current_width_pct / avg_width_pct * 100 (< 50 = squeeze)
            squeeze_pct_series = width_pct_series / avg_width_pct_series * 100

            # is_squeeze: True when squeeze_pct < 50 (same threshold as calculate())
            is_squeeze_series = squeeze_pct_series < 50

            class _BBSqueezeSeries:
                pass

            result = _BBSqueezeSeries()
            result.upper = upper
            result.middle = sma
            result.lower = lower
            result.is_squeeze = is_squeeze_series
            return result

        except Exception:
            return None
