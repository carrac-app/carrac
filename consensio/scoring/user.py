"""UserScorer indicator(s).

Behaviour is pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from consensio.scoring.base import BaseScorer, ScorerConfig


class UserScorer(BaseScorer):
    """
    User Criteria-based Early Trend Detection Scorer.

    Based on user's trading style criteria:
    - Consensio Divergence: Emerging patterns like 2/9 or +3 -1 -3
    - Volume Confirmation: Above 20-day MA
    - EMA Ribbon Freshness: Line TURNING (from red to yellow/green)
    - CPR Position: Price above/below monthly middle CPR

    Focus: Timeframe divergence + pivot levels for early entry
    """

    config = ScorerConfig(ema_weight=3)  # EMA ribbon weighted 3x; thresholds 55/35 default

    def calculate_score(self,
                       consensio_signals: Dict,
                       ema_ribbon: Dict,
                       rsi_data: Dict,
                       price_data: pd.DataFrame,
                       volume_metrics: Dict,
                       adx_data: Optional[Dict],
                       cpr_data: Optional[Dict]) -> Dict:
        """
        Calculate user criteria-based early entry score.

        Returns:
            Dictionary with score, direction, classification, components, patterns
        """
        components = {}
        patterns = []

        # Determine direction from EMA Ribbon (primary indicator)
        direction = self._determine_direction(consensio_signals, ema_ribbon)

        # ========================================
        # 1. Consensio Divergence (30 pts max)
        # ========================================
        divergence = self._detect_consensio_divergence(consensio_signals, direction)
        components['divergence'] = divergence['score']
        if divergence['pattern']:
            patterns.append(divergence['pattern'])

        # ========================================
        # 2. EMA Ribbon Freshness (25 pts max)
        # ========================================
        freshness = self._calculate_freshness(ema_ribbon.get('last_change') if ema_ribbon else None)
        components['freshness'] = freshness['score']
        patterns.append(freshness['label'])

        # ========================================
        # 3. Volume Confirmation (20 pts max)
        # ========================================
        if volume_metrics and volume_metrics.get('is_above_average'):
            components['volume'] = 20
            patterns.append('volume_confirmed')
        else:
            components['volume'] = 0

        # ========================================
        # 4. CPR Position (15 pts max)
        # ========================================
        cpr_score = self._score_cpr_position(cpr_data, direction)
        components['cpr'] = cpr_score
        if cpr_score > 0:
            patterns.append('cpr_aligned')

        # ========================================
        # 5. RSI Divergence Bonus (10 pts max)
        # ========================================
        div_score = 0
        divergences = rsi_data.get('divergences', {}) if rsi_data else {}
        daily_div = divergences.get('daily', {}) or {}
        weekly_div = divergences.get('weekly', {}) or {}

        if direction == 'bullish':
            if daily_div.get('bullish_divergence') or weekly_div.get('bullish_divergence'):
                div_score = 10
                patterns.append('rsi_bull_div')
        elif direction == 'bearish':
            if daily_div.get('bearish_divergence') or weekly_div.get('bearish_divergence'):
                div_score = 10
                patterns.append('rsi_bear_div')

        if div_score > 0:
            components['rsi_divergence'] = div_score

        # ========================================
        # Calculate Total Score
        # ========================================
        score = sum(components.values())

        # ADX modifiers (same as research scorer)
        adx = adx_data.get('adx', 25) if adx_data else 25
        if 20 <= adx <= 30:
            score += 10  # Sweet spot bonus
            patterns.append('adx_sweet_spot')
        elif adx < 20:
            score = int(score * 0.5)  # Ranging penalty
            patterns.append('ranging_market')
        elif adx > 40:
            score = int(score * 0.7)  # Mature trend penalty
            patterns.append('mature_trend')

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

    def _detect_consensio_divergence(self, signals: Dict, direction: str) -> Dict:
        """
        Detect emerging trend patterns from timeframe divergence.

        KEY INSIGHT: Early trend changes show DIVERGENCE between timeframes.
        - Maximum divergence: daily opposite to weekly (+3 with -3) = 30 pts
        - 2TF aligned, 1 diverging = 25 pts
        - Fully aligned (9/9 or 0/9) = 0 pts (mature trend)

        Args:
            signals: {'1d': int(-3 to +3), '3d': int, '1w': int}
            direction: 'bullish' or 'bearish'

        Returns:
            {'pattern': str, 'score': int, 'description': str}
        """
        signal_1d = signals.get('1d', 0)
        signal_3d = signals.get('3d', 0)
        signal_1w = signals.get('1w', 0)

        # Calculate total positions for aligned check
        total_positions = max(0, signal_1d) + max(0, signal_3d) + max(0, signal_1w)

        # Check for fully aligned (mature trend) - NO SIGNAL
        if total_positions == 9:  # All bullish +3+3+3
            return {'pattern': 'fully_aligned_bull', 'score': 0, 'description': '9/9 mature bullish'}
        if total_positions == 0 and signal_1d <= 0 and signal_3d <= 0 and signal_1w <= 0:
            # All bearish or neutral
            if signal_1d == -3 and signal_3d == -3 and signal_1w == -3:
                return {'pattern': 'fully_aligned_bear', 'score': 0, 'description': '0/9 mature bearish'}

        # Check for emerging reversal patterns
        if direction == 'bullish':
            # Look for daily bullish while weekly still bearish
            if signal_1d > 0 and signal_1w < 0:
                if signal_1d >= 2:  # Strong daily bullish
                    return {'pattern': 'emerging_bull', 'score': 30, 'description': f'+{signal_1d} 1D with {signal_1w} 1W'}
                else:
                    return {'pattern': 'early_bull', 'score': 25, 'description': f'+{signal_1d} 1D with {signal_1w} 1W'}
            # Daily and 3D bullish, weekly still bearish
            if signal_1d > 0 and signal_3d > 0 and signal_1w < 0:
                return {'pattern': 'developing_bull', 'score': 25, 'description': f'2TF bull, 1W {signal_1w}'}
            # Low position count but positive (2/9, 3/9)
            if 2 <= total_positions <= 4:
                return {'pattern': 'low_position_bull', 'score': 20, 'description': f'{total_positions}/9 positions'}

        elif direction == 'bearish':
            # Look for daily bearish while weekly still bullish
            if signal_1d < 0 and signal_1w > 0:
                if signal_1d <= -2:  # Strong daily bearish
                    return {'pattern': 'emerging_bear', 'score': 30, 'description': f'{signal_1d} 1D with +{signal_1w} 1W'}
                else:
                    return {'pattern': 'early_bear', 'score': 25, 'description': f'{signal_1d} 1D with +{signal_1w} 1W'}
            # Daily and 3D bearish, weekly still bullish
            if signal_1d < 0 and signal_3d < 0 and signal_1w > 0:
                return {'pattern': 'developing_bear', 'score': 25, 'description': f'2TF bear, 1W +{signal_1w}'}
            # High position count but declining (6/9, 7/9)
            if 5 <= total_positions <= 7:
                return {'pattern': 'declining_bull', 'score': 20, 'description': f'{total_positions}/9 declining'}

        # Nearly aligned (late entry) - low score
        if total_positions >= 7:
            return {'pattern': 'nearly_aligned', 'score': 5, 'description': f'{total_positions}/9 late entry'}

        # Mixed signals with no clear pattern
        return {'pattern': 'mixed', 'score': 10, 'description': 'mixed signals'}

    def _score_cpr_position(self, cpr_data: Optional[Dict], direction: str) -> int:
        """
        Score based on price position relative to monthly CPR.

        - Price above TC with bullish direction: +15 pts
        - Price below BC with bearish direction: +15 pts
        - Price inside CPR: 0 pts (consolidation)
        """
        if not cpr_data:
            return 0

        price_position = cpr_data.get('price_position', 'inside')

        if direction == 'bullish' and price_position == 'above':
            return 15
        elif direction == 'bearish' and price_position == 'below':
            return 15

        return 0
