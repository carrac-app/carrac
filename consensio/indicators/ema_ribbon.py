"""EmaRibbonCalculator indicator(s).

Extracted from the monolithic consensio.py into the consensio package.
Behaviour is byte-identical to the monolithic implementation —
pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd


def _ema_pine_compat(series, span):
    """EMA seeded from SMA(span) of the first 'span' VALID bars — matches TradingView ta.ema().

    pandas ewm(adjust=False) seeds from series[0], causing 5-15% divergence
    in the first ~80 bars and a permanent small offset vs TradingView/Pine Script.
    Pine seeds from SMA(N) of the first N values, which this replicates.

    Like Pine, leading NaNs are skipped: the seed window starts at the first
    valid value. This matters when the input is itself a warmed-up series —
    e.g. the MACD signal line is an EMA-9 of the MACD line, whose first 25
    values are NaN (EMA-26 warmup). Seeding from arr[:span] there yields a NaN
    seed that poisons the whole output.

    Single definition: ``carrac.charts.indicators`` re-exports it (consensio
    must not depend on the carrac app layer).
    """
    arr = np.asarray(series, dtype=float)
    n = len(arr)
    out = np.full(n, np.nan)
    valid = np.flatnonzero(~np.isnan(arr))
    if valid.size == 0 or n - valid[0] < span:
        return pd.Series(out, index=series.index if hasattr(series, 'index') else None)
    start = int(valid[0])
    alpha = 2.0 / (span + 1)
    out[start + span - 1] = float(np.mean(arr[start:start + span]))
    for i in range(start + span, n):
        if np.isnan(arr[i]) or np.isnan(out[i - 1]):
            continue
        out[i] = arr[i] * alpha + out[i - 1] * (1 - alpha)
    if hasattr(series, 'index'):
        return pd.Series(out, index=series.index)
    return pd.Series(out)


class EmaRibbonCalculator:
    """
    Calculate EMA Ribbon indicator.

    Ported from Pine Script:
    - Fast EMA(30), Slow EMA(60)
    - Trend determined by: emaDiff vs 0.30 * ATR(60)
    - Bull (green): emaDiff > margin
    - Bear (orange): emaDiff < -margin
    - Neutral (yellow): in between
    """

    def __init__(self, fast_period: int = 30, slow_period: int = 60,
                 atr_period: int = 60, atr_mult: float = 0.30):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.atr_mult = atr_mult

    def _calculate_atr(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Calculate Average True Range using Wilder's RMA.

        Was SMA (`tr.rolling(period).mean()`). Pine Script's `ta.atr`
        and the client-side `calculateATR` in `carrac-indicator-pack.js` both
        use Wilder's Recursive Moving Average:

            rma[period-1] = SMA of first `period` TRs
            rma[i]        = (rma[i-1] * (period-1) + tr[i]) / period

        Using SMA made the server's bull/bear/neutral flag disagree with the
        chart on the same bar because the `margin = atr_mult * ATR` threshold
        sits ~0.3 ATR around zero — right where SMA-vs-Wilder's divergence
        lives.
        """
        high = df['high']
        low = df['low']
        close = df['close']

        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        rma = pd.Series(index=tr.index, dtype=float)
        n = len(tr)
        if n < period:
            return rma  # all NaN
        rma.iloc[period - 1] = tr.iloc[:period].mean()
        for i in range(period, n):
            rma.iloc[i] = (rma.iloc[i - 1] * (period - 1) + tr.iloc[i]) / period
        return rma

    def calculate(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        Calculate EMA Ribbon status with EMA convergence detection.

        Returns:
            Dictionary with:
            - trend: 'bull', 'bear', or 'neutral'
            - position: 'above', 'below', or 'inside' (relative to the channel)
            - price_position: 'above_both', 'below_both', or 'sandwiched' (for scoring)
            - last_change: date string (YY-MM-DD) of last trend change
            - fast_ema: current fast EMA value
            - slow_ema: current slow EMA value
            - ema_gap_pct: EMA gap as percentage of price
            - is_converging: True if EMAs are narrowing
            - convergence_days: consecutive days of narrowing gap
            - is_squeeze: True if gap < 0.5% (imminent flip)
            - days_since_flip: days since last trend change
        """
        min_periods = max(self.slow_period, self.atr_period) + 10
        if len(df) < min_periods:
            return None

        close = df['close']

        # Calculate EMAs
        # SMA-seeded EMA (Pine-compatible), not pandas'
        # ewm(adjust=False) which seeds from close[0] and diverges from
        # TradingView for ~80 bars plus a permanent small offset thereafter.
        ema_fast = _ema_pine_compat(close, self.fast_period)
        ema_slow = _ema_pine_compat(close, self.slow_period)

        # Calculate ATR margin
        atr = self._calculate_atr(df, self.atr_period)
        margin = self.atr_mult * atr

        # Calculate trend for each bar
        ema_diff = ema_fast - ema_slow

        # Determine trend status for each bar
        trends = pd.Series(index=df.index, dtype='object')
        trends[ema_diff > margin] = 'bull'
        trends[ema_diff < -margin] = 'bear'
        trends[(ema_diff >= -margin) & (ema_diff <= margin)] = 'neutral'

        # Get current values
        current_trend = trends.iloc[-1]
        current_close = close.iloc[-1]
        current_fast = ema_fast.iloc[-1]
        current_slow = ema_slow.iloc[-1]

        # ========================================
        # NEW: EMA Convergence Detection
        # ========================================
        # Calculate EMA gap as percentage of price
        ema_gap = abs(current_fast - current_slow)
        ema_gap_pct = (ema_gap / current_close) * 100 if current_close > 0 else 0

        # Track gap over last 3 days to detect convergence
        is_converging = False
        convergence_days = 0
        if len(df) >= 4:
            gap_history = []
            for i in range(-3, 0):
                fast_val = ema_fast.iloc[i]
                slow_val = ema_slow.iloc[i]
                price_val = close.iloc[i]
                if price_val > 0:
                    gap_pct = abs(fast_val - slow_val) / price_val * 100
                    gap_history.append(gap_pct)

            # Add current gap
            gap_history.append(ema_gap_pct)

            # Check if gap is narrowing (converging)
            if len(gap_history) >= 2:
                is_converging = gap_history[-1] < gap_history[0]  # Current gap smaller than 3 days ago

                # Count consecutive days of narrowing
                for i in range(len(gap_history) - 1, 0, -1):
                    if gap_history[i] < gap_history[i - 1]:
                        convergence_days += 1
                    else:
                        break

        # EMA squeeze detection (imminent flip)
        is_squeeze = ema_gap_pct < 0.5

        # ========================================
        # Price Position (for scoring)
        # ========================================
        # Determine price position relative to channel (legacy)
        upper_line = max(current_fast, current_slow)
        lower_line = min(current_fast, current_slow)

        if current_close > upper_line:
            position = 'above'
        elif current_close < lower_line:
            position = 'below'
        else:
            position = 'inside'

        # NEW: Price position relative to both EMAs (for v2 scoring)
        if current_close > current_fast and current_close > current_slow:
            price_position = 'above_both'
        elif current_close < current_fast and current_close < current_slow:
            price_position = 'below_both'
        else:
            price_position = 'sandwiched'

        # Find last trend change date and calculate days since flip
        last_change_date = None
        days_since_flip = 999
        if len(trends.dropna()) > 1:
            # Find where trend changed
            trend_shifted = trends.shift(1)
            changes = trends != trend_shifted
            change_dates = df.index[changes & trends.notna() & trend_shifted.notna()]

            if len(change_dates) > 0:
                last_change_date = change_dates[-1].strftime('%y-%m-%d')
                # Calculate days since last flip
                from datetime import datetime
                try:
                    change_dt = datetime.strptime(last_change_date, '%y-%m-%d')
                    today = datetime.now()
                    days_since_flip = max(0, (today - change_dt).days)
                except Exception:
                    days_since_flip = 999

        return {
            'trend': current_trend if pd.notna(current_trend) else 'neutral',
            'position': position,
            'price_position': price_position,
            'last_change': last_change_date,
            'fast_ema': round(current_fast, 2),
            'slow_ema': round(current_slow, 2),
            'ema_gap_pct': round(ema_gap_pct, 3),
            'is_converging': is_converging,
            'convergence_days': convergence_days,
            'is_squeeze': is_squeeze,
            'days_since_flip': days_since_flip
        }
