"""RSICalculator, RSIDivergenceDetector indicator(s).

Extracted from the monolithic consensio.py during the package split.
Behaviour is byte-identical to the pre-split implementation —
pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd


class RSICalculator:
    """Calculate RSI (Relative Strength Index) indicator."""

    def __init__(self, period: int = 14):
        """
        Initialize RSI calculator.

        Args:
            period: RSI period (default: 14)
        """
        self.period = period

    def _wilder_rsi(self, close: pd.Series) -> np.ndarray:
        """Wilder's RSI: SMA seed for first period, then recursive smoothing.
        Matches TradingView's RSI calculation exactly."""
        n = len(close)
        delta = close.values
        gains = np.zeros(n)
        losses = np.zeros(n)
        for i in range(1, n):
            ch = delta[i] - delta[i - 1]
            if ch > 0:
                gains[i] = ch
            else:
                losses[i] = -ch

        rsi_out = np.full(n, np.nan)
        if n < self.period + 1:
            return rsi_out

        # SMA seed over first 'period' changes (indices 1..period)
        avg_g = gains[1:self.period + 1].mean()
        avg_l = losses[1:self.period + 1].mean()

        if avg_l == 0:
            rsi_out[self.period] = 100.0
        else:
            rsi_out[self.period] = 100.0 - (100.0 / (1.0 + avg_g / avg_l))

        # Wilder's recursive smoothing
        p = self.period
        for i in range(self.period + 1, n):
            avg_g = (avg_g * (p - 1) + gains[i]) / p
            avg_l = (avg_l * (p - 1) + losses[i]) / p
            if avg_l == 0:
                rsi_out[i] = 100.0
            else:
                rsi_out[i] = 100.0 - (100.0 / (1.0 + avg_g / avg_l))

        return rsi_out

    def calculate(self, df: pd.DataFrame) -> Optional[float]:
        """
        Calculate RSI for the given OHLCV data.

        Args:
            df: DataFrame with OHLCV data

        Returns:
            RSI value (0-100) or None if insufficient data
        """
        if len(df) < self.period + 1:
            return None

        rsi_arr = self._wilder_rsi(df['close'])
        latest = rsi_arr[-1]
        if np.isnan(latest):
            return None
        return round(float(latest), 1)

    def calculate_series(self, df: pd.DataFrame) -> pd.Series:
        """
        Calculate full RSI series for divergence detection.

        Args:
            df: DataFrame with OHLCV data

        Returns:
            RSI series
        """
        if len(df) < self.period + 1:
            return pd.Series(dtype=float)

        rsi_arr = self._wilder_rsi(df['close'])
        return pd.Series(rsi_arr, index=df.index)

class RSIDivergenceDetector:
    """
    Detect RSI divergences (price vs RSI highs/lows).

    Regular Divergences (reversal signals):
    - Bullish Divergence: Price makes lower low, RSI makes higher low
    - Bearish Divergence: Price makes higher high, RSI makes lower high

    Hidden Divergences (trend continuation signals):
    - Hidden Bullish Divergence: Price makes higher low, RSI makes lower low
    - Hidden Bearish Divergence: Price makes lower high, RSI makes higher high
    """

    def __init__(self, rsi_period: int = 14, lookback: int = 20, swing_strength: int = 5):
        """
        Initialize RSI Divergence Detector.

        Args:
            rsi_period: Period for RSI calculation
            lookback: Number of bars to look back for swing points
            swing_strength: Bars on each side to confirm swing (higher = fewer, stronger swings)
        """
        self.rsi_calc = RSICalculator(period=rsi_period)
        self.lookback = lookback
        self.swing_strength = swing_strength

    def _find_swing_lows(self, series: pd.Series, strength: int) -> list:
        """Find swing low points in a series."""
        swings = []
        # Need at least 2*strength+1 data points for valid swing detection
        if len(series) < 2 * strength + 1:
            return swings
        for i in range(strength, len(series) - strength):
            is_low = True
            for j in range(1, strength + 1):
                if series.iloc[i] >= series.iloc[i - j] or series.iloc[i] >= series.iloc[i + j]:
                    is_low = False
                    break
            if is_low:
                swings.append((i, series.iloc[i]))
        return swings

    def _find_swing_highs(self, series: pd.Series, strength: int) -> list:
        """Find swing high points in a series."""
        swings = []
        # Need at least 2*strength+1 data points for valid swing detection
        if len(series) < 2 * strength + 1:
            return swings
        for i in range(strength, len(series) - strength):
            is_high = True
            for j in range(1, strength + 1):
                if series.iloc[i] <= series.iloc[i - j] or series.iloc[i] <= series.iloc[i + j]:
                    is_high = False
                    break
            if is_high:
                swings.append((i, series.iloc[i]))
        return swings

    def detect(self, df: pd.DataFrame) -> Dict:
        """
        Detect RSI divergences in the given data.

        Args:
            df: DataFrame with OHLCV data

        Returns:
            Dictionary with:
            - bullish_divergence: bool (regular - reversal signal)
            - bearish_divergence: bool (regular - reversal signal)
            - hidden_bullish_divergence: bool (trend continuation)
            - hidden_bearish_divergence: bool (trend continuation)
            - divergence_type: 'bullish', 'bearish', 'hidden_bullish', 'hidden_bearish', or None
        """
        result = {
            'bullish_divergence': False,
            'bearish_divergence': False,
            'hidden_bullish_divergence': False,
            'hidden_bearish_divergence': False,
            'divergence_type': None
        }

        if len(df) < self.lookback + self.rsi_calc.period:
            return result

        # Get recent data for analysis
        recent_df = df.tail(self.lookback + self.rsi_calc.period)

        # Calculate RSI series
        rsi = self.rsi_calc.calculate_series(recent_df)
        if rsi.empty:
            return result

        # Get close prices aligned with RSI
        close = recent_df['close']

        # Take only the lookback period for swing detection
        close_recent = close.tail(self.lookback)
        rsi_recent = rsi.tail(self.lookback)

        if len(close_recent) < self.lookback or len(rsi_recent) < self.lookback:
            return result

        # Find swing points
        price_lows = self._find_swing_lows(close_recent.reset_index(drop=True), self.swing_strength)
        price_highs = self._find_swing_highs(close_recent.reset_index(drop=True), self.swing_strength)
        rsi_lows = self._find_swing_lows(rsi_recent.reset_index(drop=True), self.swing_strength)
        rsi_highs = self._find_swing_highs(rsi_recent.reset_index(drop=True), self.swing_strength)

        # Check for bullish divergence (lower price lows, higher RSI lows)
        if len(price_lows) >= 2 and len(rsi_lows) >= 2:
            # Get last two swing lows
            prev_price_low = price_lows[-2][1]
            curr_price_low = price_lows[-1][1]
            prev_rsi_low = rsi_lows[-2][1]
            curr_rsi_low = rsi_lows[-1][1]

            # Bullish: price lower low, RSI higher low
            if curr_price_low < prev_price_low and curr_rsi_low > prev_rsi_low:
                result['bullish_divergence'] = True
                result['divergence_type'] = 'bullish'

        # Check for bearish divergence (higher price highs, lower RSI highs)
        if len(price_highs) >= 2 and len(rsi_highs) >= 2:
            # Get last two swing highs
            prev_price_high = price_highs[-2][1]
            curr_price_high = price_highs[-1][1]
            prev_rsi_high = rsi_highs[-2][1]
            curr_rsi_high = rsi_highs[-1][1]

            # Bearish: price higher high, RSI lower high
            if curr_price_high > prev_price_high and curr_rsi_high < prev_rsi_high:
                result['bearish_divergence'] = True
                # If both divergences found, bearish takes precedence (more recent signal)
                result['divergence_type'] = 'bearish'

        # Check for hidden bullish divergence (higher price lows, lower RSI lows - trend continuation)
        if len(price_lows) >= 2 and len(rsi_lows) >= 2:
            prev_price_low = price_lows[-2][1]
            curr_price_low = price_lows[-1][1]
            prev_rsi_low = rsi_lows[-2][1]
            curr_rsi_low = rsi_lows[-1][1]

            # Hidden Bullish: price higher low, RSI lower low (continuation in uptrend)
            if curr_price_low > prev_price_low and curr_rsi_low < prev_rsi_low:
                result['hidden_bullish_divergence'] = True
                if result['divergence_type'] is None:
                    result['divergence_type'] = 'hidden_bullish'

        # Check for hidden bearish divergence (lower price highs, higher RSI highs - trend continuation)
        if len(price_highs) >= 2 and len(rsi_highs) >= 2:
            prev_price_high = price_highs[-2][1]
            curr_price_high = price_highs[-1][1]
            prev_rsi_high = rsi_highs[-2][1]
            curr_rsi_high = rsi_highs[-1][1]

            # Hidden Bearish: price lower high, RSI higher high (continuation in downtrend)
            if curr_price_high < prev_price_high and curr_rsi_high > prev_rsi_high:
                result['hidden_bearish_divergence'] = True
                if result['divergence_type'] is None:
                    result['divergence_type'] = 'hidden_bearish'

        return result
