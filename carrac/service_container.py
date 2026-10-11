"""
ServiceContainer — typed holder for shared application services.

Built once at process startup via ``ServiceContainer.build()``. The resulting
instance is installed in two places:

    1. ``app.extensions['carrac_services']`` — set by ``create_app()``.
       Preferred access path from Flask request handlers.
    2. ``carrac.services._fallback_container`` — module global used by
       non-Flask contexts (worker.py, quant collectors, morning engine,
       CLI scripts, tests running outside an app context).

Both pointers reference the same instance, so mutation in one is visible
in the other. ``get_services()`` in ``carrac.services`` abstracts the
resolution order.
"""

from dataclasses import dataclass, fields
import logging
from typing import Any

logger = logging.getLogger('carrac.service_container')


@dataclass
class ServiceContainer:
    """Typed container for shared application services."""

    db: Any = None
    user_db: Any = None
    consensio: Any = None
    rsi_calc: Any = None
    ema_ribbon_calc: Any = None
    trend_analyzer: Any = None
    divergence_detector_daily: Any = None
    divergence_detector_weekly: Any = None
    adx_calc: Any = None
    weekly_signal_engine: Any = None
    cpr_calc: Any = None
    opportunity_scorer: Any = None
    research_scorer: Any = None
    user_scorer: Any = None
    ema_focused_scorer: Any = None
    balanced_scorer: Any = None
    structure_analyzer: Any = None
    bb_calc: Any = None

    @classmethod
    def build(cls) -> "ServiceContainer":
        """Instantiate all service singletons in dependency order.

        Construction order mirrors the historical behaviour of the
        legacy ``init_services()``. ``WeeklySignalEngine`` depends on
        ``db`` + ``consensio`` and must be constructed last.
        """
        from database import OHLCVDatabase
        from consensio.indicators.adx import ADXCalculator
        from consensio.indicators.bollinger import BollingerBandCalculator
        from consensio.indicators.consensio_core import ConsensioCalculator
        from consensio.indicators.cpr import CPRCalculator
        from consensio.indicators.ema_ribbon import EmaRibbonCalculator
        from consensio.indicators.market_structure import MarketStructureAnalyzer
        from consensio.indicators.rsi import RSICalculator, RSIDivergenceDetector
        from consensio.analysis.trend import TrendAnalyzer
        from consensio.scoring.balanced import BalancedScorer
        from consensio.scoring.ema_focused import EMAFocusedScorer
        from consensio.scoring.opportunity import OpportunityScorer
        from consensio.scoring.research import ResearchScorer
        from consensio.scoring.user import UserScorer
        from weekly_signal_engine import WeeklySignalEngine
        from user_db import user_db as _user_db_singleton

        db = OHLCVDatabase()
        consensio = ConsensioCalculator()

        container = cls(
            db=db,
            user_db=_user_db_singleton,
            consensio=consensio,
            rsi_calc=RSICalculator(period=14),
            ema_ribbon_calc=EmaRibbonCalculator(),
            trend_analyzer=TrendAnalyzer(),
            divergence_detector_daily=RSIDivergenceDetector(rsi_period=14, lookback=20, swing_strength=3),
            divergence_detector_weekly=RSIDivergenceDetector(rsi_period=14, lookback=12, swing_strength=2),
            adx_calc=ADXCalculator(period=14),
            cpr_calc=CPRCalculator(),
            opportunity_scorer=OpportunityScorer(),
            research_scorer=ResearchScorer(),
            user_scorer=UserScorer(),
            ema_focused_scorer=EMAFocusedScorer(),
            balanced_scorer=BalancedScorer(),
            structure_analyzer=MarketStructureAnalyzer(left_bars=5, right_bars=5),
            bb_calc=BollingerBandCalculator(period=20, std_dev=2.0, lookback=60),
            weekly_signal_engine=WeeklySignalEngine(db, consensio),
        )
        logger.info("ServiceContainer built (%d services)", len(fields(cls)))
        return container
