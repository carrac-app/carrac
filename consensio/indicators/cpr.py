"""CPRCalculator indicator(s).

Extracted from the monolithic consensio.py into the consensio package.
Behaviour is byte-identical to the monolithic implementation —
pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd


class CPRCalculator:
    """
    Calculate Central Pivot Range (CPR) for support/resistance levels.

    Monthly CPR uses previous month's data to calculate current month's levels:
    - Pivot = (High + Low + Close) / 3
    - BC (Bottom Central) = (High + Low) / 2
    - TC (Top Central) = (Pivot - BC) + Pivot = 2*Pivot - BC

    Extended Pivot Levels:
    - R1, R2, R3: Resistance levels
    - S1, S2, S3: Support levels

    V-CPR Detection:
    - When price hasn't touched middle CPR lines (Pivot/TC/BC) = vacuum pull

    Price position relative to CPR:
    - Above TC: Bullish bias
    - Below BC: Bearish bias
    - Inside CPR: Neutral/consolidation
    """

    def _calculate_cpr_levels(self, prev_high: float, prev_low: float, prev_close: float,
                               current_price: float, curr_period_bars: pd.DataFrame,
                               timeframe: str = 'monthly') -> Optional[Dict]:
        """Shared CPR calculation from previous period's H/L/C.

        Args:
            prev_high/prev_low/prev_close: Previous period's OHLC values
            current_price: Latest close price
            curr_period_bars: DataFrame of bars in the current period (for V-CPR detection)
            timeframe: 'monthly', 'weekly', or 'daily'
        """
        pivot = (prev_high + prev_low + prev_close) / 3
        bc = (prev_high + prev_low) / 2
        tc = (2 * pivot) - bc

        range_val = prev_high - prev_low
        r1 = 2 * pivot - prev_low
        r2 = pivot + range_val
        r3 = r2 + range_val
        s1 = 2 * pivot - prev_high
        s2 = pivot - range_val
        s3 = s2 - range_val

        cpr_width_pct = abs(tc - bc) / current_price * 100 if current_price else 0

        if current_price > tc:
            price_position = 'above'
        elif current_price < bc:
            price_position = 'below'
        else:
            price_position = 'inside'

        # V-CPR detection
        is_v_cpr = True
        middle_lines = [pivot, tc, bc]
        if curr_period_bars is not None and len(curr_period_bars) > 0:
            lows = curr_period_bars['low']
            highs = curr_period_bars['high']
            for line in middle_lines:
                if ((lows <= line) & (highs >= line)).any():
                    is_v_cpr = False
                    break
        if is_v_cpr:
            for line in middle_lines:
                if line > 0 and abs(current_price - line) / line * 100 <= 1.0:
                    is_v_cpr = False
                    break

        # S3/R3 extreme detection
        near_s3 = current_price <= s3 * 1.02 if s3 > 0 else current_price <= s3 * 0.98
        near_r3 = current_price >= r3 * 0.98 if r3 > 0 else current_price >= r3 * 1.02
        is_extreme = near_s3 or near_r3
        extreme_level = 's3' if near_s3 else ('r3' if near_r3 else None)

        # Price level zone
        if current_price <= s3:
            price_level = 'Below S3'
        elif current_price <= s2:
            price_level = 'S2-S3'
        elif current_price <= s1:
            price_level = 'S1-S2'
        elif current_price <= bc:
            price_level = 'BC-S1'
        elif current_price <= pivot:
            price_level = 'BC-CP'
        elif current_price <= tc:
            price_level = 'CP-TC'
        elif current_price <= r1:
            price_level = 'TC-R1'
        elif current_price <= r2:
            price_level = 'R1-R2'
        elif current_price <= r3:
            price_level = 'R2-R3'
        else:
            price_level = 'Above R3'

        return {
            'pivot': round(pivot, 4), 'tc': round(tc, 4), 'bc': round(bc, 4),
            's1': round(s1, 4), 's2': round(s2, 4), 's3': round(s3, 4),
            'r1': round(r1, 4), 'r2': round(r2, 4), 'r3': round(r3, 4),
            'cpr_width_pct': round(cpr_width_pct, 2),
            'price_position': price_position, 'price_level': price_level,
            'current_price': round(current_price, 4),
            'is_v_cpr': is_v_cpr, 'is_extreme': is_extreme,
            'extreme_level': extreme_level, 'timeframe': timeframe,
        }

    def calculate_monthly_cpr(self, df: pd.DataFrame) -> Optional[Dict]:
        """Calculate monthly CPR from daily OHLCV data."""
        if len(df) < 30:
            return None
        try:
            if not isinstance(df.index, pd.DatetimeIndex):
                return None
            df_copy = df.copy()
            # Normalise to UTC before period bucketing, then drop
            # the timezone so .to_period('M') doesn't warn about dropping it.
            # Without UTC normalisation a daily bar dated "31 March 23:00
            # UTC" rendered in a local timezone rolls into April on the
            # server, putting it in the wrong month vs the client
            # (`getUTCMonth()`).
            if df_copy.index.tz is None:
                idx_utc = df_copy.index
            else:
                idx_utc = df_copy.index.tz_convert('UTC').tz_localize(None)
            df_copy['year_month'] = idx_utc.to_period('M')
            months = df_copy['year_month'].unique()
            if len(months) < 2:
                return None
            prev_data = df_copy[df_copy['year_month'] == months[-2]]
            curr_data = df_copy[df_copy['year_month'] == months[-1]]
            if len(prev_data) == 0:
                return None
            return self._calculate_cpr_levels(
                float(prev_data['high'].max()), float(prev_data['low'].min()),
                float(prev_data['close'].iloc[-1]), float(df['close'].iloc[-1]),
                curr_data, 'monthly')
        except Exception:
            return None

    def calculate_weekly_cpr(self, df: pd.DataFrame) -> Optional[Dict]:
        """Calculate weekly CPR from daily OHLCV data (previous week's H/L/C)."""
        if len(df) < 10:
            return None
        try:
            if not isinstance(df.index, pd.DatetimeIndex):
                return None
            df_copy = df.copy()
            # Use ISO-CALENDAR year, not calendar year. Early-January
            # dates frequently belong to ISO week 52/53 of the PREVIOUS
            # calendar year (e.g. 2022-01-01 is ISO week 52 of 2021). Pairing
            # calendar `.year` with ISO `.week` produced wrong bucket IDs
            # around New Year — weekly pivots were mis-attributed for 1-2
            # weeks every January.
            iso = df_copy.index.isocalendar()
            df_copy['iso_week'] = iso.week.astype(int) + iso.year.astype(int) * 100
            weeks = sorted(df_copy['iso_week'].unique())
            if len(weeks) < 2:
                return None
            prev_data = df_copy[df_copy['iso_week'] == weeks[-2]]
            curr_data = df_copy[df_copy['iso_week'] == weeks[-1]]
            if len(prev_data) == 0:
                return None
            return self._calculate_cpr_levels(
                float(prev_data['high'].max()), float(prev_data['low'].min()),
                float(prev_data['close'].iloc[-1]), float(df['close'].iloc[-1]),
                curr_data, 'weekly')
        except Exception:
            return None

    def calculate_daily_cpr(self, df: pd.DataFrame) -> Optional[Dict]:
        """Calculate daily CPR from daily OHLCV data (previous day's H/L/C)."""
        if len(df) < 2:
            return None
        try:
            if not isinstance(df.index, pd.DatetimeIndex):
                return None
            prev_bar = df.iloc[-2]
            return self._calculate_cpr_levels(
                float(prev_bar['high']), float(prev_bar['low']),
                float(prev_bar['close']), float(df['close'].iloc[-1]),
                df.iloc[-1:], 'daily')
        except Exception:
            return None
