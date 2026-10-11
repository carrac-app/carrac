"""BalancedScorer indicator(s).

Extracted from the monolithic consensio.py when it became a package.
Behaviour is byte-identical to the monolithic implementation — pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from consensio.scoring.base import BaseScorer, ScorerConfig


class BalancedScorer(BaseScorer):
    """
    Score B: Balanced Early Trend Detection (equal weights).

    Same components as EMAFocusedScorer but with equal weighting:
    - EMA Convergence + Fresh Flip: 25 pts max
    - Price Position: 25 pts max
    - Timeframe Divergence: 25 pts max
    - Volume: 15 pts max
    - ADX modifier: +10 bonus / penalties
    """

    config = ScorerConfig()  # defaults: ema_weight=2, 55/35, trade/watch/ignore

    def calculate_score(self,
                       consensio_signals: Dict,
                       ema_ribbon: Dict,
                       rsi_data: Dict,
                       price_data: pd.DataFrame,
                       volume_metrics: Dict,
                       adx_data: Optional[Dict],
                       cpr_data: Optional[Dict],
                       structure_data: Optional[Dict] = None) -> Dict:
        """
        Calculate balanced early entry score.

        Returns:
            Dictionary with score, direction, classification, components, patterns, alerts
        """
        components = {}
        patterns = []
        alerts = []

        # Determine direction
        direction = self._determine_direction(consensio_signals, ema_ribbon)

        # ========================================
        # 1. EMA Convergence + Fresh Flip (25 pts max)
        # ========================================
        ema_score = 0
        is_converging = ema_ribbon.get('is_converging', False) if ema_ribbon else False
        convergence_days = ema_ribbon.get('convergence_days', 0) if ema_ribbon else 0
        days_since_flip = ema_ribbon.get('days_since_flip', 999) if ema_ribbon else 999
        is_squeeze = ema_ribbon.get('is_squeeze', False) if ema_ribbon else False

        # Compound signal: Converging + Fresh flip (1-2 days) = BEST
        if is_converging and days_since_flip <= 2:
            ema_score = 25
            patterns.append('convergence_plus_flip')
        # Fresh flip only (1-2 days)
        elif days_since_flip <= 2:
            ema_score = 20
            patterns.append('fresh_flip')
        # Converging only (yellow phase)
        elif is_converging and convergence_days >= 1:
            ema_score = 15 if convergence_days >= 2 else 10
            patterns.append('converging')
        else:
            ema_score = 0

        components['ema_convergence'] = ema_score

        # EMA squeeze alert
        if is_squeeze:
            alerts.append('ema_squeeze')

        # ========================================
        # 2. Price Position (25 pts max)
        # ========================================
        price_pos_score = 0
        price_position = ema_ribbon.get('price_position', 'above_both') if ema_ribbon else 'above_both'
        is_reversal = self._is_reversal_setup(consensio_signals)

        if price_position == 'sandwiched':
            price_pos_score = 25 if is_reversal else 10
            patterns.append('sandwiched')
        elif price_position in ['above_both', 'below_both']:
            price_pos_score = 10 if is_reversal else 25
            patterns.append('above_emas' if price_position == 'above_both' else 'below_emas')

        components['price_position'] = price_pos_score

        # ========================================
        # 3. Timeframe Divergence (25 pts max)
        # ========================================
        div_score = self._score_divergence(consensio_signals, direction)
        components['divergence'] = div_score

        # ========================================
        # 4. Volume (15 pts max)
        # ========================================
        volume_score = 15 if (volume_metrics and volume_metrics.get('is_above_average')) else 0
        components['volume'] = volume_score

        # ========================================
        # Calculate Total + ADX Modifier
        # ========================================
        base_score = sum(components.values())
        score = self._apply_adx_modifier(base_score, adx_data, patterns)

        # Add ADX modifier to components for tooltip display
        adx_modifier = score - base_score
        if adx_modifier != 0:
            components['adx_modifier'] = adx_modifier

        # ========================================
        # Alert Detection
        # ========================================
        if cpr_data:
            if cpr_data.get('is_v_cpr'):
                alerts.append('v_cpr')
            if cpr_data.get('is_extreme'):
                extreme = cpr_data.get('extreme_level')
                alerts.append(f'near_{extreme}')

        # Structure Divergence: EMA Ribbon trend vs Market Structure trend
        # Only alert when they DISAGREE (potential warning/reversal)
        if structure_data and ema_ribbon:
            ema_ribbon_trend = ema_ribbon.get('trend', 'neutral')
            structure_trend = structure_data.get('structure_trend', 'neutral')

            # STR down: EMA Ribbon bullish but structure bearish (LH+LL)
            if ema_ribbon_trend == 'bull' and structure_trend == 'bearish':
                alerts.append('structure_bearish')

            # STR up: EMA Ribbon bearish but structure bullish (HH+HL)
            if ema_ribbon_trend == 'bear' and structure_trend == 'bullish':
                alerts.append('structure_bullish')

        score = int(min(score, 100))
        components = {k: int(v) for k, v in components.items()}

        return {
            'score': score,
            'direction': direction,
            'classification': self._classify(score),
            'components': components,
            'patterns': patterns,
            'has_alert': len(alerts) > 0,
            'alerts': alerts,
            'regime': adx_data.get('regime', 'unknown') if adx_data else 'unknown'
        }

    def _is_reversal_setup(self, signals: Dict) -> bool:
        """Check if this looks like a reversal setup."""
        signal_1d = signals.get('1d', 0)
        signal_1w = signals.get('1w', 0)
        return (signal_1d > 0 and signal_1w < 0) or (signal_1d < 0 and signal_1w > 0)

    def _score_divergence(self, signals: Dict, direction: str) -> int:
        """Score based on timeframe divergence (25 pts max)."""
        signal_1d = signals.get('1d', 0)
        signal_3d = signals.get('3d', 0)
        signal_1w = signals.get('1w', 0)

        # Daily opposite to Weekly = max score
        if (signal_1d > 0 and signal_1w < 0) or (signal_1d < 0 and signal_1w > 0):
            return 25

        # 2TF aligned, 1 diverging
        aligned_count = sum([
            1 if signal_1d > 0 else 0,
            1 if signal_3d > 0 else 0,
            1 if signal_1w > 0 else 0
        ])
        if aligned_count == 2:
            return 20

        # Mixed signals
        if 1 <= aligned_count <= 2:
            return 10

        # All aligned = mature trend
        return 0
