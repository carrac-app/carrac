"""ResearchScorer indicator(s).

Split out of the monolithic consensio.py. Behaviour must stay byte-identical
to the pre-split implementation — pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from consensio.scoring.base import BaseScorer, ScorerConfig


class ResearchScorer(BaseScorer):
    """
    Research-based Early Trend Detection Scorer.

    Based on quantitative research for early trend detection:
    - ADX sweet spot 20-30 (trend forming, not mature)
    - Trend age penalty (>45 days = old, don't chase)
    - RSI divergence confirmation
    - Price position relative to EMAs
    - Momentum indicators

    Focus: Technical indicator-driven early detection
    """

    config = ScorerConfig()  # defaults: ema_weight=2, 55/35, trade/watch/ignore

    def calculate_score(self,
                       consensio_signals: Dict,
                       ema_ribbon: Dict,
                       rsi_data: Dict,
                       price_data: pd.DataFrame,
                       volume_metrics: Dict,
                       adx_data: Optional[Dict]) -> Dict:
        """
        Calculate research-based early entry score.

        Returns:
            Dictionary with score, direction, classification, components, patterns
        """
        components = {}
        patterns = []

        # Determine direction
        direction = self._determine_direction(consensio_signals, ema_ribbon)

        # ========================================
        # 1. ADX Sweet Spot (25 pts max)
        # ========================================
        adx_score = 0
        adx = adx_data.get('adx', 25) if adx_data else 25

        if 20 <= adx <= 30:
            adx_score = 25  # Sweet spot for early entries
            patterns.append('adx_sweet_spot')
        elif 15 <= adx < 20:
            adx_score = 15  # Trend starting to form
            patterns.append('adx_forming')
        elif 30 < adx <= 40:
            adx_score = 10  # Still okay but getting mature
            patterns.append('adx_moderate')
        # ADX > 40 or < 15 = no points

        if adx_score > 0:
            components['adx_position'] = adx_score

        # ========================================
        # 2. Trend Age / Freshness (25 pts max)
        # ========================================
        freshness = self._calculate_freshness(ema_ribbon.get('last_change') if ema_ribbon else None)
        components['freshness'] = freshness['score']
        patterns.append(freshness['label'])

        # ========================================
        # 3. RSI Position (15 pts max)
        # ========================================
        rsi_score = 0
        rsi_value = rsi_data.get('value') if rsi_data else None

        if rsi_value is not None:
            if direction == 'bullish':
                if rsi_value < 30:
                    rsi_score = 15  # Oversold - strong reversal potential
                    patterns.append('rsi_oversold')
                elif rsi_value < 40:
                    rsi_score = 10
                    patterns.append('rsi_near_oversold')
                elif 40 <= rsi_value <= 60:
                    rsi_score = 5  # Neutral zone
            elif direction == 'bearish':
                if rsi_value > 70:
                    rsi_score = 15  # Overbought - strong reversal potential
                    patterns.append('rsi_overbought')
                elif rsi_value > 60:
                    rsi_score = 10
                    patterns.append('rsi_near_overbought')
                elif 40 <= rsi_value <= 60:
                    rsi_score = 5

        if rsi_score > 0:
            components['rsi_position'] = rsi_score

        # ========================================
        # 4. RSI Divergence (15 pts max)
        # ========================================
        div_score = 0
        divergences = rsi_data.get('divergences', {}) if rsi_data else {}
        daily_div = divergences.get('daily', {}) or {}
        weekly_div = divergences.get('weekly', {}) or {}

        if direction == 'bullish':
            if daily_div.get('bullish_divergence') or weekly_div.get('bullish_divergence'):
                div_score = 15
                patterns.append('rsi_bull_div')
        elif direction == 'bearish':
            if daily_div.get('bearish_divergence') or weekly_div.get('bearish_divergence'):
                div_score = 15
                patterns.append('rsi_bear_div')

        if div_score > 0:
            components['rsi_divergence'] = div_score

        # ========================================
        # 5. Volume Confirmation (10 pts max)
        # ========================================
        volume_score = 0
        if volume_metrics and volume_metrics.get('is_above_average'):
            volume_score = 10
            patterns.append('volume_above_avg')

        if volume_score > 0:
            components['volume'] = volume_score

        # ========================================
        # 6. Price vs EMA Position (10 pts max)
        # ========================================
        ema_score = 0
        if len(price_data) >= 20:
            close = price_data['close']
            current_price = close.iloc[-1]
            ema_20 = close.ewm(span=20, adjust=False).mean().iloc[-1]

            if direction == 'bullish' and current_price > ema_20:
                ema_score = 10
                patterns.append('price_above_ema20')
            elif direction == 'bearish' and current_price < ema_20:
                ema_score = 10
                patterns.append('price_below_ema20')

        if ema_score > 0:
            components['ema_position'] = ema_score

        # ========================================
        # Calculate Total Score
        # ========================================
        score = sum(components.values())

        # ADX modifiers
        if adx < 15:
            score = int(score * 0.5)  # Ranging market penalty
            patterns.append('ranging_penalty')
        elif adx > 45:
            score = int(score * 0.6)  # Mature trend penalty
            patterns.append('mature_trend_penalty')

        score = int(min(score, 100))
        components = {k: int(v) for k, v in components.items()}

        return {
            'score': score,
            'direction': direction,
            'classification': self._classify(score),
            'components': components,
            'patterns': patterns,
            'regime': adx_data.get('regime', 'unknown') if adx_data else 'unknown'
        }
