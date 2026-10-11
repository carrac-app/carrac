"""carrac.facts.translate — translate raw statistics into trader-readable intuition.

Pure functions; no I/O, no side effects.
Every function is descriptive only — no advice, no prediction.
"""
from __future__ import annotations

from typing import Optional, Union


# ---------------------------------------------------------------------------
# rarity_label
# ---------------------------------------------------------------------------

_RARITY_BANDS = [
    (0,   1,   "Record low"),
    (2,   5,   "Extremely low"),
    (6,   10,  "Bottom decile"),
    (11,  24,  "Below normal"),
    (25,  75,  "Typical"),
    (76,  89,  "Above normal"),
    (90,  94,  "Top decile"),
    (95,  98,  "Extremely high"),
    (99,  100, "Record high"),
]


def rarity_label(pct: Union[int, float, None]) -> str:
    """Return a human-readable rarity label for a percentile.

    Returns "" for None. Out-of-range values are clamped to the nearest band.
    """
    if pct is None:
        return ""

    # Clamp
    pct = max(0, min(100, pct))

    for lo, hi, label in _RARITY_BANDS:
        if lo <= pct <= hi:
            return label

    # Unreachable given the bands cover 0–100 exhaustively,
    # but satisfy the type-checker.
    return "Typical"  # pragma: no cover


# ---------------------------------------------------------------------------
# interpret
# ---------------------------------------------------------------------------

def interpret(
    metric: str,
    value: Optional[float] = None,
    pct: Optional[float] = None,
) -> Optional[str]:
    """Return a short (≤8-word) plain-English meaning for cryptic metrics.

    Returns None when the number/label already speaks for itself, or when
    the metric is not in the handled set.  Never advice or prediction.
    """
    if metric == "rsi":
        if value is None:
            return None
        if value <= 20:
            return "deeply oversold"
        if value <= 35:
            return "near oversold"
        if value >= 80:
            return "deeply overbought"
        if value >= 65:
            return "near overbought"
        return None

    if metric == "volume":
        # value = rvol (relative volume ratio)
        if value is None:
            return None
        if value <= 0.5:
            return "less than half its normal activity"
        if value <= 0.8:
            return "below-average activity"
        if value >= 2.5:
            return "a sharp surge in activity"
        if value >= 1.5:
            return "elevated activity"
        return None

    if metric == "mayer":
        if pct is None:
            return None
        if pct <= 5:
            return "further below its 200-week average than almost ever"
        if pct <= 20:
            return "below its usual stretch"
        if pct >= 95:
            return "more stretched than almost ever"
        if pct >= 80:
            return "above its usual stretch"
        return None

    if metric == "funding":
        if pct is None:
            return None
        if pct <= 10:
            return "lower than usual"
        if pct >= 90:
            return "higher than usual"
        return None

    if metric == "range":
        # rarity_label already carries the meaning; avoid redundancy
        return None

    return None


# ---------------------------------------------------------------------------
# display_hint — suppress interpret() when a notable band chip is already shown
# ---------------------------------------------------------------------------

NOTABLE_BANDS: frozenset = frozenset({
    "Record low",
    "Extremely low",
    "Bottom decile",
    "Top decile",
    "Extremely high",
    "Record high",
})




# ---------------------------------------------------------------------------
# band_meaning — convenience combiner
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# meaning — plain-English reading gloss (a separate channel from interpret)
# ---------------------------------------------------------------------------

def meaning(metric: str, bundle: dict) -> Optional[str]:
    """Plain-English gloss of what a reading MEANS, in human terms.

    Distinct from ``interpret`` (which is percentile/rarity-driven): ``meaning``
    reads the actual value off the bundle. The rarity band chip carries the
    rarity; this carries the meaning, so the two don't double up. Pure; no I/O.
    Returns None when the source reading is absent.
    """
    if metric == "trend":
        ms = bundle.get("ma_stack") or {}
        st = ms.get("state")
        bars = ms.get("bars_held") or 0
        d = {
            "bullish": "above all its moving averages",
            "bearish": "below all its moving averages",
            "bullish_partial": "above most of its moving averages",
            "bearish_partial": "below most of its moving averages",
        }.get(st)
        return f"price has held {d} for {bars} days" if d else None

    if metric == "rsi":
        rv = (bundle.get("rsi") or {}).get("value")
        if rv is None:
            return None
        if rv >= 70:
            return "overbought — momentum stretched to the upside"
        if rv >= 55:
            return "firm upward momentum"
        if rv <= 30:
            return "oversold — momentum stretched to the downside"
        if rv <= 45:
            return "soft momentum, leaning oversold"
        return "balanced momentum"

    if metric == "mayer":
        m = (bundle.get("stretch") or {}).get("mayer")
        if m is None:
            return None
        gap = abs(1 - m) * 100
        side = "below" if m < 1 else "above"
        return f"trading about {gap:.0f}% {side} its 200-week average price"

    if metric == "volume":
        rv = (bundle.get("volume") or {}).get("rvol")
        if rv is None:
            return None
        if rv >= 1.5:
            return "trading on heavy volume vs its 20-day norm"
        if rv <= 0.6:
            return "trading on light volume vs its 20-day norm"
        return "ordinary volume vs its 20-day norm"

    if metric == "funding":
        sign = (bundle.get("funding") or {}).get("sign")
        if sign is None:
            return None
        return ("shorts are paying longs to hold the trade" if sign == "negative"
                else "longs are paying shorts to hold the trade")

    if metric == "range":
        rp = (bundle.get("range_pctile") or {}).get("pct")
        if rp is None:
            return None
        if rp <= 10:
            return "sitting near the bottom of its 90-day range"
        if rp >= 90:
            return "pressing the top of its 90-day range"
        if rp <= 35:
            return "in the lower part of its 90-day range"
        if rp >= 65:
            return "in the upper part of its 90-day range"
        return "mid 90-day range"

    return None
