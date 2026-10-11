"""Lint: scanner scorer + trade-plan output must contain no prescriptive/advice language.

Targets the free-text narrative field `setup_strategy` inside trade_plan dicts,
which is the only place plans.py emits prose. Structural field KEYS (entry, sl, tp,
entry_type, sl_name, sl_pct, rr1, rr2, price_level) and verdict enum values
(STRONG/GOOD/NEUTRAL/WEAK/AVOID) are intentionally excluded from the check.

Banned phrases (case-insensitive):
  scale in, trail stop, enter on, you should, i recommend, i'd, " buy ", " sell "

Note: " buy " and " sell " are space-padded to avoid false positives on structural
label strings like "ATR_1.5x" or "BB_M" that merely contain word fragments.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from carrac.scanner.scorer.plans import _compute_trade_plan

# ---------------------------------------------------------------------------
# Banned-phrase list — update here if new advice words are found in other files
# ---------------------------------------------------------------------------

BANNED_PHRASES = [
    "scale in",
    "trail stop",
    "enter on",
    "you should",
    "i recommend",
    "i'd",
    " buy ",
    " sell ",
]

# ---------------------------------------------------------------------------
# Realistic asset fixture (mirrors test_scanner_engine.py / scorer criteria)
# ---------------------------------------------------------------------------

def _make_asset(price=50_000.0, direction="long"):
    """Return a minimal but realistic asset dict that exercises all plan branches."""
    # Structure: support well below, resistance well above
    if direction == "long":
        sw_l = price * 0.95   # support ~5% below
        sw_h = price * 1.08   # resistance ~8% above
    else:
        sw_h = price * 1.05   # resistance ~5% above
        sw_l = price * 0.92   # support ~8% below

    return {
        "symbol": "BTCUSDT",
        "latest_price": price,
        "atr_pct": 2.5,
        "bb": {
            "upper": price * 1.04,
            "middle": price * 1.00,
            "lower": price * 0.96,
        },
        "cpr": {
            "pivot": price * 0.99,
            "tc": price * 1.01,
            "bc": price * 0.98,
            "s1": price * 0.97,
            "s2": price * 0.94,
            "s3": price * 0.91,
            "r1": price * 1.02,
            "r2": price * 1.05,
            "r3": price * 1.08,
            "price_level": "Below Pivot",
        },
        "structure": {
            "last_high_price": sw_h,
            "prev_high_price": sw_h * 1.02,
            "last_low_price": sw_l,
            "prev_low_price": sw_l * 0.98,
        },
        "ema_ribbon": {
            "fast_ema": price * 0.985,
            "slow_ema": price * 0.970,
        },
        "ma_levels": {
            "ema21": price * 0.980,
            "ema50": price * 0.965,
            "ema200": price * 0.900,
            "sma200": price * 0.895,
        },
    }


def _make_confluences(price, direction="long"):
    """Build a minimal confluences dict (same shape as compute_confluences output)."""
    if direction == "long":
        supports = [
            {"price": price * 0.97, "label": "PIVOT+BC", "strength": 2.5, "tier": "medium"},
            {"price": price * 0.94, "label": "S2+EMA50", "strength": 2.0, "tier": "medium"},
            {"price": price * 0.90, "label": "EMA200+SMA200", "strength": 3.0, "tier": "strong"},
        ]
        resistances = [
            {"price": price * 1.02, "label": "R1+BB_U", "strength": 2.5, "tier": "medium"},
            {"price": price * 1.05, "label": "R2", "strength": 1.0, "tier": "faint"},
            {"price": price * 1.08, "label": "R3+BB_U", "strength": 2.0, "tier": "medium"},
        ]
    else:
        supports = [
            {"price": price * 0.95, "label": "PIVOT+EMA21", "strength": 2.5, "tier": "medium"},
            {"price": price * 0.91, "label": "S3+BB_L", "strength": 2.0, "tier": "medium"},
        ]
        resistances = [
            {"price": price * 1.03, "label": "R1+BB_M", "strength": 2.5, "tier": "medium"},
            {"price": price * 1.07, "label": "R2+EMA50", "strength": 2.0, "tier": "medium"},
            {"price": price * 1.10, "label": "R3+SMA200", "strength": 3.0, "tier": "strong"},
        ]
    return {"supports": supports, "resistances": resistances}


def _extract_narrative_text(plan):
    """Return only the free-text narrative fields from a trade_plan dict.

    Deliberately excludes structural field KEYS and their numeric/enum values so
    the lint does not fire on legitimate label strings (e.g. 'ATR_1.5x').
    """
    if not plan:
        return ""
    # Only the prose field(s)
    return plan.get("setup_strategy", "")


def _assert_no_banned_phrases(text, context=""):
    """Assert that text contains none of BANNED_PHRASES (case-insensitive)."""
    lower = text.lower()
    hits = [p for p in BANNED_PHRASES if p in lower]
    assert not hits, (
        f"Banned advice phrase(s) {hits!r} found in narrative text{' (' + context + ')' if context else ''}.\n"
        f"Full text: {text!r}"
    )


# ---------------------------------------------------------------------------
# Parameterised test: all setup types × both directions
# ---------------------------------------------------------------------------

SETUP_TYPES = [
    "trend_continuation",
    "pullback",
    "breakout",
    "reversal",
    "mean_reversion",
    "range",
    None,  # triggers mechanical fallback
]

DIRECTIONS = ["long", "short"]


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("setup_type", SETUP_TYPES)
def test_no_advice_in_trade_plan_narrative(setup_type, direction):
    """setup_strategy field must contain no banned advice phrases."""
    price = 50_000.0
    asset = _make_asset(price, direction)
    confluences = _make_confluences(price, direction)

    plan = _compute_trade_plan(
        asset, direction,
        setup_type=setup_type,
        confluences=confluences,
    )

    narrative = _extract_narrative_text(plan)
    ctx = f"setup_type={setup_type!r}, direction={direction!r}"
    _assert_no_banned_phrases(narrative, ctx)


# ---------------------------------------------------------------------------
# Explicit spot-check for the two known offenders
# ---------------------------------------------------------------------------

def test_trend_continuation_no_trail_no_scale():
    """Pins that _plan_trend does not emit 'trail stop' or 'scale in'."""
    price = 50_000.0
    asset = _make_asset(price, "long")
    confluences = _make_confluences(price, "long")
    plan = _compute_trade_plan(asset, "long", setup_type="trend_continuation", confluences=confluences)
    narrative = _extract_narrative_text(plan)
    assert "trail stop" not in narrative.lower(), f"'trail stop' still present: {narrative!r}"
    assert "scale in" not in narrative.lower(), f"'scale in' still present: {narrative!r}"


def test_breakout_no_enter_on():
    """Pins that _plan_breakout does not emit 'enter on'."""
    price = 50_000.0
    asset = _make_asset(price, "long")
    confluences = _make_confluences(price, "long")
    plan = _compute_trade_plan(asset, "long", setup_type="breakout", confluences=confluences)
    narrative = _extract_narrative_text(plan)
    assert "enter on" not in narrative.lower(), f"'enter on' still present: {narrative!r}"
