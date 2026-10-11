"""ConsensioCalculator indicator(s).

Extracted from the monolithic consensio.py when it became a package.
Behaviour is byte-identical to the pre-split implementation —
pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


class ConsensioCalculator:
    """Calculate Consensio indicator values from OHLCV data."""

    def __init__(self, period_short: int = 3, period_medium: int = 7, period_long: int = 30, ma_type: str = "SMA"):
        """
        Initialize Consensio calculator.

        Args:
            period_short: Period for short MA (default: 3)
            period_medium: Period for medium MA (default: 7)
            period_long: Period for long MA (default: 30)
            ma_type: Type of moving average (SMA, EMA, WMA)
        """
        self.period_short = period_short
        self.period_medium = period_medium
        self.period_long = period_long
        self.ma_type = ma_type

    def _calculate_ma(self, series: pd.Series, period: int) -> pd.Series:
        """Calculate moving average based on type."""
        if self.ma_type == "EMA":
            return series.ewm(span=period, adjust=False).mean()
        elif self.ma_type == "WMA":
            weights = np.arange(1, period + 1)
            return series.rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)
        else:  # Default SMA
            return series.rolling(window=period).mean()

    def calculate_signal(self, df: pd.DataFrame) -> int:
        """
        Calculate Consensio signal for a single timeframe.

        Args:
            df: DataFrame with OHLCV data (needs at least period_long rows)

        Returns:
            Signal value (-3 to +3)
        """
        if len(df) < self.period_long:
            return 0

        close = df["close"]

        # Calculate the three MAs
        ma_short = self._calculate_ma(close, self.period_short)
        ma_medium = self._calculate_ma(close, self.period_medium)
        ma_long = self._calculate_ma(close, self.period_long)

        # Get latest values
        s = ma_short.iloc[-1]
        m = ma_medium.iloc[-1]
        l = ma_long.iloc[-1]

        # Determine signal based on MA ordering
        # Pine Script logic:
        # S > M > L  ->  +3
        # M > S > L  ->  +2
        # S > L > M  ->  +1
        # M > L > S  ->  -1
        # L > S > M  ->  -2
        # L > M > S  ->  -3

        if s > m and m > l:
            return 3
        elif m > s and s > l:
            return 2
        elif s > l and l > m:
            return 1
        elif m > l and l > s:
            return -1
        elif l > s and s > m:
            return -2
        elif l > m and m > s:
            return -3
        else:
            return 0

    def signal_to_positions(self, signal: int) -> int:
        """
        Convert Consensio signal to number of positions.
        Only positive signals count as positions.

        Args:
            signal: Consensio signal value (-3 to +3)

        Returns:
            Number of positions (0-3)
        """
        return max(0, signal)

    def calculate_multi_timeframe(self,
                                   df_1d: pd.DataFrame,
                                   df_3d: pd.DataFrame,
                                   df_1w: pd.DataFrame) -> Dict[str, Dict]:
        """
        Calculate Consensio across multiple timeframes.

        Args:
            df_1d: 1-day OHLCV data
            df_3d: 3-day OHLCV data
            df_1w: 1-week OHLCV data

        Returns:
            Dictionary with signals and positions for each timeframe
        """
        # Calculate signals for each timeframe
        signal_1d = self.calculate_signal(df_1d) if len(df_1d) > 0 else 0
        signal_3d = self.calculate_signal(df_3d) if len(df_3d) > 0 else 0
        signal_1w = self.calculate_signal(df_1w) if len(df_1w) > 0 else 0

        # Convert to positions (only positive signals count)
        pos_1d = self.signal_to_positions(signal_1d)
        pos_3d = self.signal_to_positions(signal_3d)
        pos_1w = self.signal_to_positions(signal_1w)

        total_positions = pos_1d + pos_3d + pos_1w

        return {
            "1D": {
                "signal": signal_1d,
                "positions": pos_1d,
                "max_positions": 3
            },
            "3D": {
                "signal": signal_3d,
                "positions": pos_3d,
                "max_positions": 3
            },
            "1W": {
                "signal": signal_1w,
                "positions": pos_1w,
                "max_positions": 3
            },
            "total": {
                "positions": total_positions,
                "max_positions": 9
            }
        }
