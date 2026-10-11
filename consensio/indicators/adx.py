"""ADXCalculator indicator(s).

Split out of the monolithic consensio.py. Behaviour must stay byte-identical
to the pre-split implementation — pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd


class ADXCalculator:
    """
    Calculate ADX (Average Directional Index) for trend strength measurement.

    ADX Regimes:
    - ADX < 20: Ranging market (weak/no trend)
    - ADX 20-30: Trend forming
    - ADX > 30: Strong trend

    Also tracks ADX direction (rising = strengthening trend)
    """

    def __init__(self, period: int = 14):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        Calculate ADX and directional indicators.

        Args:
            df: DataFrame with OHLCV data (needs high, low, close columns)

        Returns:
            Dictionary with:
            - adx: Current ADX value (0-100)
            - plus_di: +DI value
            - minus_di: -DI value
            - regime: 'ranging', 'trend_forming', or 'strong_trend'
            - is_rising: bool (ADX trending up over 3 periods)
            - trend_direction: 'bullish' (+DI > -DI), 'bearish', or 'neutral'
        """
        min_periods = self.period * 2 + 5  # Need enough data for smoothing
        if len(df) < min_periods:
            return None

        high = df['high']
        low = df['low']
        close = df['close']

        # Calculate True Range
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Calculate Directional Movement
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low

        # +DM: Up move > down move AND up move > 0
        plus_dm = pd.Series(0.0, index=df.index)
        plus_mask = (up_move > down_move) & (up_move > 0)
        plus_dm.loc[plus_mask] = up_move.loc[plus_mask]

        # -DM: Down move > up move AND down move > 0
        minus_dm = pd.Series(0.0, index=df.index)
        minus_mask = (down_move > up_move) & (down_move > 0)
        minus_dm.loc[minus_mask] = down_move.loc[minus_mask]

        # Wilder's RMA: seed[period-1] = SMA of first `period` values, then
        # rma[i] = (rma[i-1] * (period-1) + x[i]) / period.
        # Was ewm(alpha=1/period, adjust=False) which seeds from the
        # first observation rather than from the SMA, diverging from Pine Script.
        def _wilder_rma(series: pd.Series, period: int) -> pd.Series:
            """Wilder's RMA seeded with SMA of first `period` *valid* values."""
            rma = pd.Series(index=series.index, dtype=float)
            valid_idx = series.first_valid_index()
            if valid_idx is None:
                return rma
            iloc_start = series.index.get_loc(valid_idx)
            n = len(series)
            if n - iloc_start < period:
                return rma  # not enough valid data
            seed_pos = iloc_start + period - 1
            rma.iloc[seed_pos] = series.iloc[iloc_start:iloc_start + period].mean()
            for i in range(seed_pos + 1, n):
                rma.iloc[i] = (rma.iloc[i - 1] * (period - 1) + series.iloc[i]) / period
            return rma

        smoothed_tr = _wilder_rma(tr, self.period)
        smoothed_plus_dm = _wilder_rma(plus_dm, self.period)
        smoothed_minus_dm = _wilder_rma(minus_dm, self.period)

        # Calculate +DI and -DI
        plus_di = 100 * smoothed_plus_dm / smoothed_tr.replace(0, float('nan'))
        minus_di = 100 * smoothed_minus_dm / smoothed_tr.replace(0, float('nan'))

        # Calculate DX
        di_sum = plus_di + minus_di
        di_diff = abs(plus_di - minus_di)
        dx = 100 * di_diff / di_sum.replace(0, float('nan'))

        # Calculate ADX (second Wilder's RMA on DX)
        adx = _wilder_rma(dx, self.period)

        # Get current values
        current_adx = adx.iloc[-1]
        current_plus_di = plus_di.iloc[-1]
        current_minus_di = minus_di.iloc[-1]

        if pd.isna(current_adx):
            return None

        # Determine regime
        if current_adx < 20:
            regime = 'ranging'
        elif current_adx < 30:
            regime = 'trend_forming'
        else:
            regime = 'strong_trend'

        # Check if ADX is rising (3-period slope)
        is_rising = False
        if len(adx) >= 4:
            recent_adx = adx.iloc[-4:-1].dropna()
            if len(recent_adx) >= 3:
                is_rising = bool(adx.iloc[-1] > adx.iloc[-4])

        # Determine trend direction from DI comparison
        if current_plus_di > current_minus_di + 5:  # +5 buffer for noise
            trend_direction = 'bullish'
        elif current_minus_di > current_plus_di + 5:
            trend_direction = 'bearish'
        else:
            trend_direction = 'neutral'

        # ATR = smoothed TR (already computed for DI calculation)
        current_atr = float(smoothed_tr.iloc[-1]) if not pd.isna(smoothed_tr.iloc[-1]) else None
        current_close = float(close.iloc[-1])
        atr_pct = round((current_atr / current_close) * 100, 2) if current_atr and current_close else None

        return {
            'adx': float(round(current_adx, 1)),
            'plus_di': float(round(current_plus_di, 1)) if not pd.isna(current_plus_di) else None,
            'minus_di': float(round(current_minus_di, 1)) if not pd.isna(current_minus_di) else None,
            'regime': regime,
            'is_rising': is_rising,
            'trend_direction': trend_direction,
            'atr': current_atr,
            'atr_pct': atr_pct,
        }
