"""Shared constants used across consensio indicators and scorers."""
from __future__ import annotations

# =============================================================================
# Thresholds and Parameters
# =============================================================================

# RSI thresholds
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# ADX thresholds for trend strength
ADX_STRONG_TREND = 25
ADX_WEAK_TREND = 20

# Scoring thresholds
FRESH_FLIP_DAYS = 3  # Days since EMA flip to be considered "fresh"
