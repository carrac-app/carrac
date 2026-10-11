"""OpportunityScorer indicator(s).

Behaviour is pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from consensio.scoring.base import BaseScorer, ScorerConfig


class OpportunityScorer(BaseScorer):
    """
    Calculate opportunity score using ChatGPT's formula (0-100).

    Pre-filter: ADX regime detection
    - ADX < 20: Score = 0 (ranging market)
    - ADX > 30 + countertrend signal: Disable countertrend

    Components (max 100 pts):
    - Trend Alignment: 40 pts (Consensio 2+ TFs bullish: +20, EMA Ribbon bullish: +10, Fresh flip: +10)
    - Momentum Strength: 20 pts (EMA slope > 0: +10, ADX rising: +10)
    - Price Position: 10 pts (Price > all EMAs: +10)
    - Volume Confirmation: 20 pts (Volume > 20d avg: +10, Contrarian funding: +10)
    - Signal Quality: 10 pts (RSI divergence: +10, Hidden div: +5)

    Post-filter: No volume confirmation caps score at 50

    Classification:
    - Score >= 60: "trade_candidate"
    - Score 40-59: "watchlist"
    - Score < 40: "ignore"
    """

    config = ScorerConfig(
        ema_weight=2,
        trade_threshold=60,
        watch_threshold=40,
        trade_label='trade_candidate',
        watch_label='watchlist',
    )

    def __init__(self):
        self.FRESH_FLIP_DAYS = 3  # Days to consider an EMA Ribbon flip "fresh"

    def calculate_score(self,
                       consensio_signals: Dict,      # {'1d': int, '3d': int, '1w': int}
                       ema_ribbon: Dict,             # {'trend': str, 'last_change': str}
                       rsi_data: Dict,               # {'value': float, 'divergences': dict}
                       price_data: pd.DataFrame,     # OHLCV with close prices
                       volume_metrics: Dict,         # from calculate_volume_metrics
                       adx_data: Optional[Dict],     # from ADXCalculator
                       funding_rate: Optional[float] = None) -> Dict:
        """
        Calculate comprehensive opportunity score.

        Returns:
            Dictionary with:
            - score: int (0-100)
            - direction: 'bullish' or 'bearish'
            - classification: 'trade_candidate', 'watchlist', or 'ignore'
            - components: breakdown of each scoring component
            - regime: ADX regime info
            - reason: if score is 0, explains why
        """
        components = {}
        patterns = []

        # Determine overall direction first (for funding rate logic)
        direction = self._determine_direction(consensio_signals, ema_ribbon)

        # ========================================
        # Pre-filter: ADX Regime Check
        # ========================================
        regime = adx_data.get('regime', 'unknown') if adx_data else 'unknown'
        is_ranging = regime == 'ranging'

        # Note: We'll calculate the full score but apply a penalty for ranging markets later

        # ========================================
        # 1. Trend Alignment (40 pts max)
        # ========================================
        trend_alignment = 0

        # Count bullish timeframes (signal > 0)
        bullish_tfs = sum(1 for tf in ['1d', '3d', '1w']
                         if consensio_signals.get(tf, 0) > 0)
        bearish_tfs = sum(1 for tf in ['1d', '3d', '1w']
                         if consensio_signals.get(tf, 0) < 0)

        if direction == 'bullish' and bullish_tfs >= 2:
            trend_alignment += 20
            patterns.append('consensio_aligned')
        elif direction == 'bearish' and bearish_tfs >= 2:
            trend_alignment += 20
            patterns.append('consensio_aligned')

        # EMA Ribbon trend alignment
        ema_ribbon_trend = ema_ribbon.get('trend', 'neutral') if ema_ribbon else 'neutral'
        if (direction == 'bullish' and ema_ribbon_trend == 'bull') or \
           (direction == 'bearish' and ema_ribbon_trend == 'bear'):
            trend_alignment += 10
            patterns.append('ema_ribbon_aligned')

        # Fresh EMA Ribbon flip
        if self._is_recent_flip(ema_ribbon.get('last_change') if ema_ribbon else None):
            trend_alignment += 10
            patterns.append('fresh_flip')

        if trend_alignment > 0:
            components['trend_alignment'] = trend_alignment

        # ========================================
        # 2. Momentum Strength (20 pts max)
        # ========================================
        momentum = 0

        # EMA slope positive (use 10-period EMA slope)
        if len(price_data) >= 12:
            close = price_data['close']
            ema_10 = close.ewm(span=10, adjust=False).mean()
            if len(ema_10) >= 3:
                ema_slope = ema_10.iloc[-1] - ema_10.iloc[-3]
                if (direction == 'bullish' and ema_slope > 0) or \
                   (direction == 'bearish' and ema_slope < 0):
                    momentum += 10
                    patterns.append('ema_momentum')

        # ADX rising
        if adx_data and adx_data.get('is_rising'):
            momentum += 10
            patterns.append('adx_rising')

        if momentum > 0:
            components['momentum'] = momentum

        # ========================================
        # 3. Price Position vs EMAs (10 pts max)
        # ========================================
        price_position = 0

        if len(price_data) >= 30:
            close = price_data['close']
            current_price = close.iloc[-1]

            # Calculate EMAs
            ema_10 = close.ewm(span=10, adjust=False).mean().iloc[-1]
            ema_20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
            ema_50 = close.ewm(span=50, adjust=False).mean().iloc[-1] if len(close) >= 50 else ema_20

            if direction == 'bullish':
                if current_price > ema_10 and current_price > ema_20 and current_price > ema_50:
                    price_position = 10
                    patterns.append('price_above_emas')
            elif direction == 'bearish':
                if current_price < ema_10 and current_price < ema_20 and current_price < ema_50:
                    price_position = 10
                    patterns.append('price_below_emas')

        if price_position > 0:
            components['price_position'] = price_position

        # ========================================
        # 4. Volume Confirmation (20 pts max)
        # ========================================
        volume_score = 0

        # Volume above 20-day average
        if volume_metrics and volume_metrics.get('is_above_average'):
            volume_score += 10
            patterns.append('volume_confirmed')

        # Contrarian funding rate (crypto only)
        if funding_rate is not None:
            # Positive funding = bullish crowd, Negative = bearish crowd
            # Contrarian: crowd is wrong -> +10 pts
            if (direction == 'bullish' and funding_rate > 0.0001) or \
               (direction == 'bearish' and funding_rate < -0.0001):
                # Crowd agrees with our direction -> no bonus (they might be right)
                pass
            elif (direction == 'bullish' and funding_rate < -0.0001) or \
                 (direction == 'bearish' and funding_rate > 0.0001):
                # Crowd is against our direction -> contrarian bonus
                volume_score += 10
                patterns.append('contrarian_funding')

        if volume_score > 0:
            components['volume'] = volume_score

        # ========================================
        # 5. Signal Quality (10 pts max)
        # ========================================
        signal_quality = 0

        divergences = rsi_data.get('divergences', {}) if rsi_data else {}
        daily_div = divergences.get('daily', {}) or {}
        weekly_div = divergences.get('weekly', {}) or {}

        # Regular divergence (reversal signal)
        if direction == 'bullish':
            if daily_div.get('bullish_divergence') or weekly_div.get('bullish_divergence'):
                signal_quality = 10
                patterns.append('rsi_bull_div')
            elif daily_div.get('hidden_bullish_divergence') or weekly_div.get('hidden_bullish_divergence'):
                signal_quality = max(signal_quality, 5)
                patterns.append('rsi_hidden_bull_div')
        elif direction == 'bearish':
            if daily_div.get('bearish_divergence') or weekly_div.get('bearish_divergence'):
                signal_quality = 10
                patterns.append('rsi_bear_div')
            elif daily_div.get('hidden_bearish_divergence') or weekly_div.get('hidden_bearish_divergence'):
                signal_quality = max(signal_quality, 5)
                patterns.append('rsi_hidden_bear_div')

        if signal_quality > 0:
            components['signal_quality'] = signal_quality

        # ========================================
        # Calculate Total Score
        # ========================================
        score = int(sum(components.values()))

        # Post-filter: Volume cap
        if not volume_metrics or not volume_metrics.get('is_above_average'):
            score = min(score, 50)
            if score == 50:
                patterns.append('volume_cap_applied')

        # Ranging market penalty: reduce score by 50% (trends matter less in ranges)
        if is_ranging:
            score = int(score * 0.5)
            patterns.append('ranging_penalty')

        # Ensure max 100 and convert to Python int
        score = int(min(score, 100))

        # Convert components to native Python types
        components = {k: int(v) for k, v in components.items()}

        return {
            'score': score,
            'direction': direction,
            'classification': self._classify(score),
            'components': components,
            'patterns': patterns,
            'regime': regime
        }

    def _is_recent_flip(self, last_change: Optional[str], days: int = None) -> bool:
        """Check if EMA Ribbon flip is within threshold days."""
        if not last_change:
            return False

        if days is None:
            days = self.FRESH_FLIP_DAYS

        try:
            from datetime import datetime
            change_date = datetime.strptime(last_change, '%y-%m-%d')
            today = datetime.now()
            delta = (today - change_date).days
            return 0 <= delta <= days
        except Exception:
            return False

