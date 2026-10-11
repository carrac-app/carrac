"""MarketStructureAnalyzer indicator(s).

Extracted from the monolithic consensio.py. Behaviour is byte-identical to
the pre-split implementation —
pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

import math
from typing import Dict, Optional

import pandas as pd


def _finite6(x):
    """Round to 6 decimals, or None when the value is NaN/Inf/unparseable.

    A single NaN price here poisons the whole /api/assets JSON payload
    (bare NaN is invalid JSON and browsers refuse to parse the response).
    """
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, 6) if math.isfinite(v) else None


class MarketStructureAnalyzer:
    """
    Detect market structure patterns: HH (Higher High), HL (Higher Low),
    LH (Lower High), LL (Lower Low).

    Based on pivot point analysis - compares each pivot with previous
    same-type pivot to classify the structure.

    Structure Trend:
    - Bullish: Making HH and HL (higher highs, higher lows)
    - Bearish: Making LH and LL (lower highs, lower lows)
    """

    def __init__(self, left_bars: int = 5, right_bars: int = 5):
        """
        Initialize Market Structure Analyzer.

        Args:
            left_bars: Bars to the left for pivot confirmation
            right_bars: Bars to the right for pivot confirmation
        """
        self.left_bars = left_bars
        self.right_bars = right_bars

    def _find_pivot_highs(self, highs: pd.Series) -> list:
        """Find pivot highs (local maxima)."""
        pivots = []
        for i in range(self.left_bars, len(highs) - self.right_bars):
            is_pivot = True
            current = highs.iloc[i]

            # Check left side
            for j in range(1, self.left_bars + 1):
                if highs.iloc[i - j] >= current:
                    is_pivot = False
                    break

            # Check right side
            if is_pivot:
                for j in range(1, self.right_bars + 1):
                    if highs.iloc[i + j] >= current:
                        is_pivot = False
                        break

            if is_pivot:
                pivots.append((i, current))

        return pivots

    def _find_pivot_lows(self, lows: pd.Series) -> list:
        """Find pivot lows (local minima)."""
        pivots = []
        for i in range(self.left_bars, len(lows) - self.right_bars):
            is_pivot = True
            current = lows.iloc[i]

            # Check left side
            for j in range(1, self.left_bars + 1):
                if lows.iloc[i - j] <= current:
                    is_pivot = False
                    break

            # Check right side
            if is_pivot:
                for j in range(1, self.right_bars + 1):
                    if lows.iloc[i + j] <= current:
                        is_pivot = False
                        break

            if is_pivot:
                pivots.append((i, current))

        return pivots

    def analyze(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        Analyze market structure from OHLCV data.

        Args:
            df: DataFrame with OHLCV data

        Returns:
            Dictionary with:
            - structure_trend: 'bullish', 'bearish', or 'neutral'
            - last_pivot_type: 'HH', 'HL', 'LH', 'LL', or None
            - last_high_type: 'HH' or 'LH'
            - last_low_type: 'HL' or 'LL'
            - pivot_highs: list of (index, value) tuples
            - pivot_lows: list of (index, value) tuples
            - bars_since_pivot: bars since last pivot
        """
        min_bars = (self.left_bars + self.right_bars) * 3 + 10
        if len(df) < min_bars:
            return None

        try:
            highs = df['high']
            lows = df['low']

            # Find all pivot points
            pivot_highs = self._find_pivot_highs(highs)
            pivot_lows = self._find_pivot_lows(lows)

            if len(pivot_highs) < 2 or len(pivot_lows) < 2:
                return None

            # Classify the last two highs
            last_high = pivot_highs[-1][1]
            prev_high = pivot_highs[-2][1]
            last_high_type = 'HH' if last_high > prev_high else 'LH'

            # Classify the last two lows
            last_low = pivot_lows[-1][1]
            prev_low = pivot_lows[-2][1]
            last_low_type = 'HL' if last_low > prev_low else 'LL'

            # Determine which pivot is more recent
            last_high_idx = pivot_highs[-1][0]
            last_low_idx = pivot_lows[-1][0]

            if last_high_idx > last_low_idx:
                last_pivot_type = last_high_type
                bars_since_pivot = len(df) - 1 - last_high_idx
            else:
                last_pivot_type = last_low_type
                bars_since_pivot = len(df) - 1 - last_low_idx

            # Determine overall structure trend
            # Bullish: HH + HL (both making higher)
            # Bearish: LH + LL (both making lower)
            # Neutral: Mixed signals
            if last_high_type == 'HH' and last_low_type == 'HL':
                structure_trend = 'bullish'
            elif last_high_type == 'LH' and last_low_type == 'LL':
                structure_trend = 'bearish'
            else:
                structure_trend = 'neutral'

            return {
                'structure_trend': structure_trend,
                'last_pivot_type': last_pivot_type,
                'last_high_type': last_high_type,
                'last_low_type': last_low_type,
                'bars_since_pivot': bars_since_pivot,
                'pivot_count': len(pivot_highs) + len(pivot_lows),
                'last_high_price': _finite6(last_high),
                'prev_high_price': _finite6(prev_high),
                'last_low_price': _finite6(last_low),
                'prev_low_price': _finite6(prev_low),
            }

        except Exception:
            return None
