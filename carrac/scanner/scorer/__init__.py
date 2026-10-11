"""carrac.scanner.scorer — split package.

All previous top-level names are re-exported so every caller
``from carrac.scanner.scorer import score_asset`` keeps working.
"""
from __future__ import annotations

from carrac.scanner.scorer.criteria import (
    _verdict,
    _score_trend,
    _score_momentum,
    _score_rsi,
    _score_structure,
    _score_level,
    _score_macro,
)
from carrac.scanner.scorer.plans import _compute_trade_plan
from carrac.scanner.scorer.setups import classify_setup
from carrac.scanner.scorer.engine import (
    _build_macro_data,
    get_quick_score,
    score_all_assets,
    score_asset,
)

__all__ = [
    'score_asset',
    'score_all_assets',
    'get_quick_score',
    '_build_macro_data',
    'classify_setup',
    '_compute_trade_plan',
    '_verdict',
    '_score_trend',
    '_score_momentum',
    '_score_rsi',
    '_score_structure',
    '_score_level',
    '_score_macro',
]
