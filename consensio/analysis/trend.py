"""TrendAnalyzer indicator(s).

Behaviour is pinned by tests/test_consensio_golden.py.
"""
from __future__ import annotations

from typing import Dict, Optional



class TrendAnalyzer:
    """
    Analyze emerging trends and calculate opportunity scores.

    Detects patterns:
    - Timeframe divergence (1D leading 3D/1W)
    - Fresh EMA Ribbon flips (within 3 days)
    - RSI extremes (oversold <30, overbought >70)
    - RSI divergences (regular - reversal signals)
    - RSI hidden divergences (continuation signals)
    - Consensio direction changes

    Scoring weights:
    - Divergence: 30 pts
    - Fresh Flip: 25 pts
    - RSI Extreme: 20 pts
    - RSI Divergence: 25 pts
    - RSI Hidden Divergence: 20 pts (continuation)
    - Direction Change: 25 pts
    - Momentum Bonus: 10 pts
    """

    def __init__(self):
        # Scoring weights
        self.WEIGHT_DIVERGENCE = 30
        self.WEIGHT_FRESH_FLIP = 25
        self.WEIGHT_RSI = 20
        self.WEIGHT_RSI_DIVERGENCE = 25
        self.WEIGHT_RSI_HIDDEN_DIVERGENCE = 20  # Trend continuation signal
        self.WEIGHT_DIRECTION_CHANGE = 25
        self.WEIGHT_MOMENTUM = 10

        # Thresholds
        self.FRESH_DAYS_THRESHOLD = 3
        self.RSI_OVERSOLD = 30
        self.RSI_OVERBOUGHT = 70
        self.RSI_NEAR_OVERSOLD = 40
        self.RSI_NEAR_OVERBOUGHT = 60

    def _parse_days_since_change(self, last_change: Optional[str]) -> int:
        """
        Parse days since last EMA Ribbon change from date string.

        Args:
            last_change: Date string in 'YY-MM-DD' format

        Returns:
            Days since change, or 999 if unknown
        """
        if not last_change:
            return 999

        try:
            from datetime import datetime
            # Parse YY-MM-DD format
            change_date = datetime.strptime(last_change, '%y-%m-%d')
            today = datetime.now()
            delta = (today - change_date).days
            return max(0, delta)
        except Exception:
            return 999

    def analyze(self, asset: Dict, previous: Optional[Dict] = None) -> Dict:
        """
        Analyze an asset for emerging trend patterns.

        Args:
            asset: Current asset data from API
            previous: Previous day's snapshot data for this asset

        Returns:
            Dictionary with score, patterns, direction, momentum, and changes
        """
        if previous is None:
            previous = {}

        bullish_score = 0
        bearish_score = 0
        bullish_patterns = []
        bearish_patterns = []

        # Extract current values
        consensio = asset.get('consensio', {})
        signal_1d = consensio.get('1D', {}).get('signal', 0)
        signal_1w = consensio.get('1W', {}).get('signal', 0)
        current_positions = asset.get('positions', 0)

        ema_ribbon = asset.get('ema_ribbon', {}) or {}
        ema_ribbon_trend = ema_ribbon.get('trend', 'neutral')
        last_change = ema_ribbon.get('last_change')

        rsi = asset.get('weekly_rsi')

        # Previous values
        prev_positions = previous.get('positions', current_positions)
        prev_streak = previous.get('streak', {'direction': 'flat', 'count': 0})

        # ========================================
        # Pattern Detection: Bullish
        # ========================================

        # Parse days since EMA Ribbon flip (used for fresh flip detection)
        days_since = self._parse_days_since_change(last_change)

        # 1. Timeframe Divergence (1D bullish, 1W bearish)
        # IMPROVED: Only flag meaningful divergences, not normal pullbacks
        # Conditions: (A) Significant gap OR (B) Fresh flip + RSI extreme
        has_bullish_tf_div = signal_1d > 0 and signal_1w < 0
        if has_bullish_tf_div:
            # A: Significant magnitude gap (daily strong positive, weekly strong negative)
            significant_gap = signal_1d >= 2 and signal_1w <= -2
            # B: Fresh divergence (EMA Ribbon just flipped) + weekly RSI oversold
            is_fresh = days_since <= self.FRESH_DAYS_THRESHOLD
            rsi_oversold = rsi is not None and rsi < self.RSI_OVERSOLD

            if significant_gap or (is_fresh and rsi_oversold):
                bullish_score += self.WEIGHT_DIVERGENCE
                bullish_patterns.append('divergence')

        # 2. Fresh EMA Ribbon Flip to Bull
        if ema_ribbon_trend == 'bull' and days_since <= self.FRESH_DAYS_THRESHOLD:
            bullish_score += self.WEIGHT_FRESH_FLIP
            bullish_patterns.append('fresh_flip')

        # 3. RSI Oversold
        if rsi is not None:
            if rsi < self.RSI_OVERSOLD:
                bullish_score += self.WEIGHT_RSI
                bullish_patterns.append('rsi_oversold')
            elif rsi < self.RSI_NEAR_OVERSOLD:
                bullish_score += self.WEIGHT_RSI // 2

        # 4. Consensio Direction Change (0 -> positive)
        if current_positions > 0 and prev_positions == 0:
            bullish_score += self.WEIGHT_DIRECTION_CHANGE
            bullish_patterns.append('direction_change')

        # 5. Momentum Bonus (improving streak)
        if prev_streak.get('direction') == 'up' and prev_streak.get('count', 0) >= 2:
            bullish_score += self.WEIGHT_MOMENTUM
            bullish_patterns.append('momentum')

        # 6. RSI Bullish Divergence (price lower low, RSI higher low - reversal)
        rsi_divergence = asset.get('rsi_divergence', {})
        daily_div = rsi_divergence.get('daily', {}) or {}
        weekly_div = rsi_divergence.get('weekly', {}) or {}

        if daily_div.get('bullish_divergence') or weekly_div.get('bullish_divergence'):
            bullish_score += self.WEIGHT_RSI_DIVERGENCE
            bullish_patterns.append('rsi_bull_div')

        # 7. RSI Hidden Bullish Divergence (price higher low, RSI lower low - continuation)
        if daily_div.get('hidden_bullish_divergence') or weekly_div.get('hidden_bullish_divergence'):
            bullish_score += self.WEIGHT_RSI_HIDDEN_DIVERGENCE
            bullish_patterns.append('rsi_hidden_bull_div')

        # ========================================
        # Pattern Detection: Bearish
        # ========================================

        # 1. Timeframe Divergence (1D bearish, 1W bullish)
        # IMPROVED: Only flag meaningful divergences, not normal pullbacks
        # Conditions: (A) Significant gap OR (B) Fresh flip + RSI extreme
        has_bearish_tf_div = signal_1d < 0 and signal_1w > 0
        if has_bearish_tf_div:
            # A: Significant magnitude gap (daily strong negative, weekly strong positive)
            significant_gap = signal_1d <= -2 and signal_1w >= 2
            # B: Fresh divergence (EMA Ribbon just flipped) + weekly RSI overbought
            is_fresh = days_since <= self.FRESH_DAYS_THRESHOLD
            rsi_overbought = rsi is not None and rsi > self.RSI_OVERBOUGHT

            if significant_gap or (is_fresh and rsi_overbought):
                bearish_score += self.WEIGHT_DIVERGENCE
                bearish_patterns.append('divergence')

        # 2. Fresh EMA Ribbon Flip to Bear
        if ema_ribbon_trend == 'bear' and days_since <= self.FRESH_DAYS_THRESHOLD:
            bearish_score += self.WEIGHT_FRESH_FLIP
            bearish_patterns.append('fresh_flip')

        # 3. RSI Overbought
        if rsi is not None:
            if rsi > self.RSI_OVERBOUGHT:
                bearish_score += self.WEIGHT_RSI
                bearish_patterns.append('rsi_overbought')
            elif rsi > self.RSI_NEAR_OVERBOUGHT:
                bearish_score += self.WEIGHT_RSI // 2

        # 4. Consensio Direction Change (9 -> less)
        if current_positions < 9 and prev_positions == 9:
            bearish_score += self.WEIGHT_DIRECTION_CHANGE
            bearish_patterns.append('direction_change')

        # 5. Momentum Bonus (declining streak)
        if prev_streak.get('direction') == 'down' and prev_streak.get('count', 0) >= 2:
            bearish_score += self.WEIGHT_MOMENTUM
            bearish_patterns.append('momentum')

        # 6. RSI Bearish Divergence (price higher high, RSI lower high - reversal)
        if daily_div.get('bearish_divergence') or weekly_div.get('bearish_divergence'):
            bearish_score += self.WEIGHT_RSI_DIVERGENCE
            bearish_patterns.append('rsi_bear_div')

        # 7. RSI Hidden Bearish Divergence (price lower high, RSI higher high - continuation)
        if daily_div.get('hidden_bearish_divergence') or weekly_div.get('hidden_bearish_divergence'):
            bearish_score += self.WEIGHT_RSI_HIDDEN_DIVERGENCE
            bearish_patterns.append('rsi_hidden_bear_div')

        # ========================================
        # Determine Direction and Final Score
        # ========================================

        if bullish_score > bearish_score:
            direction = 'bullish'
            score = min(bullish_score, 100)
            patterns = bullish_patterns
        elif bearish_score > bullish_score:
            direction = 'bearish'
            score = min(bearish_score, 100)
            patterns = bearish_patterns
        else:
            direction = 'neutral'
            score = 0
            patterns = []

        # Calculate position changes
        positions_delta = current_positions - prev_positions

        return {
            'score': score,
            'patterns': patterns,
            'direction': direction,
            'momentum': prev_streak,
            'changes': {
                'positions_delta': positions_delta,
                'improved': positions_delta > 0
            }
        }
