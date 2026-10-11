"""Consensio package — indicators, analysis, and scoring.

All names formerly exported from consensio.py are re-exported here so existing
callers (`from consensio import EmaRibbonCalculator`) continue to work
unchanged. The split is behaviour-preserving; pinned by
tests/test_consensio_golden.py.
"""
from __future__ import annotations

from consensio.constants import (
    ADX_STRONG_TREND,
    ADX_WEAK_TREND,
    FRESH_FLIP_DAYS,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
)
from consensio.indicators.ema_ribbon import EmaRibbonCalculator
from consensio.indicators.rsi import RSICalculator, RSIDivergenceDetector
from consensio.indicators.market_structure import MarketStructureAnalyzer
from consensio.indicators.adx import ADXCalculator
from consensio.indicators.bollinger import BollingerBandCalculator
from consensio.indicators.cpr import CPRCalculator
from consensio.indicators.consensio_core import ConsensioCalculator
from consensio.analysis.trend import TrendAnalyzer
from consensio.scoring.opportunity import OpportunityScorer
from consensio.scoring.research import ResearchScorer
from consensio.scoring.user import UserScorer
from consensio.scoring.ema_focused import EMAFocusedScorer
from consensio.scoring.balanced import BalancedScorer

__all__ = [
    # Constants
    'ADX_STRONG_TREND', 'ADX_WEAK_TREND', 'FRESH_FLIP_DAYS',
    'RSI_OVERBOUGHT', 'RSI_OVERSOLD',
    # Indicators
    'EmaRibbonCalculator', 'RSICalculator', 'RSIDivergenceDetector',
    'MarketStructureAnalyzer', 'ADXCalculator', 'BollingerBandCalculator',
    'CPRCalculator', 'ConsensioCalculator',
    # Analysis
    'TrendAnalyzer',
    # Scoring
    'OpportunityScorer', 'ResearchScorer', 'UserScorer',
    'EMAFocusedScorer', 'BalancedScorer',
]
