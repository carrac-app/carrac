"""Shared direction/classification logic for all five scorers.

Before the shared base class, every scorer (Opportunity, Research, User, EMAFocused,
Balanced) carried its own copy of ``_determine_direction`` and ``_classify``.
The bodies were near-identical — only two things varied:

* how heavily the EMA ribbon vote was weighted (``ema_weight`` below),
* where the score thresholds and their labels sat (``trade_threshold``,
  ``watch_threshold``, label triplet).

Those are lifted into ``ScorerConfig``. Behaviour is byte-identical to the
pre-split implementation — pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ScorerConfig:
    """Per-scorer configuration for direction weighting and classification."""

    ema_weight: int = 2
    trade_threshold: int = 55
    watch_threshold: int = 35
    trade_label: str = 'trade'
    watch_label: str = 'watch'
    ignore_label: str = 'ignore'


class BaseScorer:
    """Common direction + classification helpers shared by all scorers.

    Subclasses override ``config`` at class level; everything else (the
    scorer-specific score-composition in ``calculate_score``) stays in the
    subclass.
    """

    config: ScorerConfig = ScorerConfig()

    def _determine_direction(self, consensio_signals: Dict, ema_ribbon: Dict) -> str:
        """Aggregate Consensio signals + EMA ribbon trend into bullish/bearish/neutral."""
        bullish_signals = 0
        bearish_signals = 0

        for tf in ['1d', '3d', '1w']:
            sig = consensio_signals.get(tf, 0)
            if sig > 0:
                bullish_signals += 1
            elif sig < 0:
                bearish_signals += 1

        ema_ribbon_trend = ema_ribbon.get('trend', 'neutral') if ema_ribbon else 'neutral'
        if ema_ribbon_trend == 'bull':
            bullish_signals += self.config.ema_weight
        elif ema_ribbon_trend == 'bear':
            bearish_signals += self.config.ema_weight

        if bullish_signals > bearish_signals:
            return 'bullish'
        elif bearish_signals > bullish_signals:
            return 'bearish'
        return 'neutral'

    def _classify(self, score: int) -> str:
        """Bucket the integer score into the scorer's trade/watch/ignore labels."""
        if score >= self.config.trade_threshold:
            return self.config.trade_label
        elif score >= self.config.watch_threshold:
            return self.config.watch_label
        return self.config.ignore_label

    def _calculate_freshness(self, last_change: Optional[str]) -> Dict:
        """
        Calculate days since EMA Ribbon flip and score.

        Freshness scoring (25 pts max):
        - 0-3 days: 25 pts (Fresh flip)
        - 4-7 days: 20 pts (Recent)
        - 8-14 days: 15 pts (Developing)
        - 15-30 days: 10 pts (Established)
        - 31-45 days: 5 pts (Mature)
        - >45 days: 0 pts (Old)
        """
        if not last_change:
            return {'days': 999, 'score': 0, 'label': 'unknown'}

        try:
            from datetime import datetime
            change_date = datetime.strptime(last_change, '%y-%m-%d')
            today = datetime.now()
            days = max(0, (today - change_date).days)

            if days <= 3:
                return {'days': days, 'score': 25, 'label': 'fresh_flip'}
            elif days <= 7:
                return {'days': days, 'score': 20, 'label': 'recent'}
            elif days <= 14:
                return {'days': days, 'score': 15, 'label': 'developing'}
            elif days <= 30:
                return {'days': days, 'score': 10, 'label': 'established'}
            elif days <= 45:
                return {'days': days, 'score': 5, 'label': 'mature'}
            else:
                return {'days': days, 'score': 0, 'label': 'old'}

        except Exception:
            return {'days': 999, 'score': 0, 'label': 'unknown'}

    def _apply_adx_modifier(self, score: int, adx_data: Optional[Dict], patterns: list) -> int:
        """Apply ADX-based modifier to score."""
        if not adx_data:
            return score

        adx = adx_data.get('adx', 25)

        # ADX 20-30 "Sweet Spot" = +10 bonus
        if 20 <= adx <= 30:
            patterns.append('adx_sweet_spot')
            return score + 10

        # ADX < 20 "Ranging" = 50% penalty
        if adx < 20:
            patterns.append('ranging_penalty')
            return int(score * 0.5)

        # ADX > 40 "Mature" = 30% penalty
        if adx > 40:
            patterns.append('mature_penalty')
            return int(score * 0.7)

        return score
