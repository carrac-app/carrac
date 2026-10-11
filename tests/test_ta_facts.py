"""Tests for morning_engine/ta_facts.py — deterministic TA-fact checks.

All fixtures are hand-crafted from the spec semantics and expected values
are computed by hand before being written here.
"""
import math
import pytest
import pandas as pd

from morning_engine.ta_facts import (
    _tol,
    level_streaks,
    decisive_closes,
    candle_geometry,
    cluster_distances,
    ma_stack,
    price_percentile,
    funding_streak,
)


# ===========================================================================
# Helper: _tol
# ===========================================================================

class TestTol:
    def test_atr_dominates(self):
        # 0.3 * 100 = 30 > 0.0025 * 1000 = 2.5
        assert _tol(100.0, 1000.0) == pytest.approx(30.0)

    def test_price_floor_dominates(self):
        # 0.3 * 0 = 0 < 0.0025 * 40000 = 100
        assert _tol(0.0, 40000.0) == pytest.approx(100.0)

    def test_exactly_equal_uses_max(self):
        # 0.3 * 10 = 3 vs 0.0025 * 1200 = 3  → equal, either is fine
        assert _tol(10.0, 1200.0) == pytest.approx(3.0)


# ===========================================================================
# Helper builders
# ===========================================================================

def _df(highs, lows, closes):
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


# ===========================================================================
# Function 1 — level_streaks
# ===========================================================================

class TestLevelStreaks:
    """
    Setup:
      price = 100.0, atr14 = 10.0
      tol = max(0.3*10, 0.0025*100) = max(3, 0.25) = 3.0
      level = 90 (support, since 90 < 100)

    For "defending" on a support:
      bar must close > level + tol = 93  (cleanly above)
      bar must NOT: low > level + tol (no interaction) or close < level - tol = 87 (break)
      It must come near enough: either low < level + tol or close in [87, 93]
      Defending = "touched band but closed cleanly above"
        => low <= level + tol AND close > level + tol

    We build 5 consecutive "defending" bars then a "no-interaction" bar before them.
    Bars (oldest first):
      Bar 0 (oldest): low=94, close=95, high=96  → low(94) > level+tol(93)  → no interaction (STOP)
      Bar 1: low=91, close=95, high=96  → touches band (low=91 < 93), close=95 > 93 → defending
      Bar 2: low=92, close=96, high=97  → touches band (low=92 < 93), close=96 > 93 → defending
      Bar 3: low=90, close=94, high=97  → touches band (low=90 < 93), close=94 > 93 → defending
      Bar 4: low=91, close=95, high=96  → defending
      Bar 5 (newest): low=92, close=96, high=97  → defending

    Walk newest→oldest: bars 5,4,3,2,1 = 5 bars (bar 0 stops it).
    streak = 5, type = "defending" (bar 5's classification).
    """
    price = 100.0
    atr14 = 10.0
    level = 90.0

    def _base_df(self):
        highs  = [96, 96, 97, 97, 96, 97]
        lows   = [94, 91, 92, 90, 91, 92]
        closes = [95, 95, 96, 94, 95, 96]
        return _df(highs, lows, closes)

    def test_empty_df(self):
        assert level_streaks(pd.DataFrame(), {"S1": 90.0}, 10.0, 100.0) == []

    def test_far_from_level_no_streak(self):
        # All bars have low > level + tol (94 > 93) → no interaction at all
        df = _df([96]*5, [94]*5, [95]*5)
        result = level_streaks(df, {"S1": 90.0}, self.atr14, self.price)
        assert result == []

    def test_streak_less_than_2_omitted(self):
        # 1 defending bar then a break → streak=1 → omitted
        highs  = [96, 97]
        lows   = [94, 91]
        closes = [95, 95]
        df = _df(highs, lows, closes)
        # bar0: low=94 > 93 → no interaction; bar1: low=91 < 93, close=95 > 93 → defending
        # newest→oldest: bar1 (defending, streak=1), bar0 (no interact → stop) → streak=1 → omitted
        result = level_streaks(df, {"S1": 90.0}, self.atr14, self.price)
        assert result == []

    def test_support_defending_streak(self):
        df = self._base_df()
        result = level_streaks(df, {"S1": self.level}, self.atr14, self.price)
        assert len(result) == 1
        r = result[0]
        assert r["level"] == "S1"
        assert r["price"] == self.level
        assert r["bars"] == 5
        assert r["type"] == "defending"

    def test_support_testing_streak(self):
        """
        Testing: abs(close - level) <= tol  →  close in [87, 93]
        level=90, tol=3 → testing if close in [87,93]
        Bar structure (oldest first):
          Bar 0: low=94, close=95  → no interaction (low > 93)
          Bar 1: low=88, close=91  → low<87? no (88>87), close=91 in [87,93] → testing
          Bar 2: low=89, close=92  → low=89 < 93, close=92 in [87,93] → testing
          Bar 3 (newest): low=88, close=90  → close=90 in [87,93] → testing
        streak=3, type="testing"
        """
        highs  = [96, 95, 95, 95]
        lows   = [94, 88, 89, 88]
        closes = [95, 91, 92, 90]
        df = _df(highs, lows, closes)
        result = level_streaks(df, {"S1": 90.0}, self.atr14, self.price)
        assert len(result) == 1
        assert result[0]["bars"] == 3
        assert result[0]["type"] == "testing"

    def test_support_rejecting(self):
        """
        Rejecting: low < level - tol = 87 AND close > level + tol = 93
        Bar structure:
          Bar 0: low=94, close=95  → no interaction (low > 93)
          Bar 1: low=85, close=96  → low=85 < 87, close=96 > 93 → rejecting
          Bar 2 (newest): low=84, close=97  → rejecting
        streak=2, type="rejecting"
        """
        highs  = [96, 98, 99]
        lows   = [94, 85, 84]
        closes = [95, 96, 97]
        df = _df(highs, lows, closes)
        result = level_streaks(df, {"S1": 90.0}, self.atr14, self.price)
        assert len(result) == 1
        assert result[0]["bars"] == 2
        assert result[0]["type"] == "rejecting"

    def test_resistance_level(self):
        """
        Resistance: level = 110 (> price=100)
        tol = 3
        Capped: high <= level+tol BUT close < level-tol = 107
          → high touches band (high >= level-tol=107), close < 107
        Bar structure (oldest first):
          Bar 0: high=105, close=104  → high=105 < 107 (never came near) → no interaction
          Bar 1: high=108, close=106  → high=108 > 107, close=106 < 107 → capped
          Bar 2: high=109, close=106  → capped
          Bar 3 (newest): high=108, close=105  → capped
        streak=3, type="capped"
        """
        price_r = 100.0  # current price is still 100
        level_r = 110.0
        highs  = [105, 108, 109, 108]
        lows   = [100, 102, 102, 100]
        closes = [104, 106, 106, 105]
        df = _df(highs, lows, closes)
        result = level_streaks(df, {"R1": level_r}, self.atr14, price_r)
        assert len(result) == 1
        assert result[0]["bars"] == 3
        assert result[0]["type"] == "capped"

    def test_break_in_middle_stops_streak(self):
        """
        A decisive break-below (close < level - tol = 87) in the middle stops the streak.
        Bars (oldest first):
          Bar 0: low=94, close=95  → no interaction (no streak yet)
          Bar 1: low=91, close=95  → defending
          Bar 2: low=88, close=86  → close=86 < 87 → break (no interaction) → streak STOPS
          Bar 3: low=91, close=95  → defending
          Bar 4 (newest): low=92, close=96  → defending
        Walk newest→oldest: bar4 (defending,1), bar3 (defending,2), bar2 (break→STOP)
        streak=2, type="defending"
        """
        highs  = [96, 96, 92, 96, 97]
        lows   = [94, 91, 88, 91, 92]
        closes = [95, 95, 86, 95, 96]
        df = _df(highs, lows, closes)
        result = level_streaks(df, {"S1": 90.0}, self.atr14, self.price)
        assert len(result) == 1
        assert result[0]["bars"] == 2
        assert result[0]["type"] == "defending"

    def test_none_level_skipped(self):
        df = self._base_df()
        result = level_streaks(df, {"S1": None, "S2": 90.0}, self.atr14, self.price)
        # Only S2 should appear
        assert all(r["level"] != "S1" for r in result)

    def test_multiple_levels(self):
        """Two levels both with streaks → both returned."""
        # level=90 (support) and level=80 (support, also below price=100)
        # tol = 3
        # For level=90: band [87, 93]
        # For level=80: band [77, 83]
        # Bars (oldest→newest):
        #   Bar 0: low=80, close=83  → low=80 < 87, close=83 < 87 → break for 90; close=83 in band for 80 → testing
        #   Bar 1: low=75, close=85  → low=75 < 87, close=85 < 87 → break for 90; close=85 > 83 → no interaction for 80
        #   Bar 2: low=91, close=95  → low=91 < 93, close=95 > 93 → defending for 90
        #   Bar 3: low=90, close=94  → defending for 90
        #   Bar 4: low=91, close=95  → defending for 90
        #   Bar 5: low=92, close=96  → defending for 90
        # S1=90 streak: bars 5,4,3,2 (newest→oldest) = 4 bars (bar 1 breaks)
        # S2=80 streak: bar 0 testing, bar 1 no interaction → streak=1 < 2 → S2 not in results
        highs  = [84, 86, 97, 97, 96, 97]
        lows   = [80, 75, 91, 90, 91, 92]
        closes = [83, 85, 95, 94, 95, 96]
        df = _df(highs, lows, closes)
        result = level_streaks(df, {"S1": 90.0, "S2": 80.0}, self.atr14, self.price)
        names = {r["level"] for r in result}
        assert "S1" in names
        assert result[0]["bars"] == 4  # S1 has 4 defending bars
        assert "S2" not in names  # S2 has only 1 testing bar (< 2 threshold)


# ===========================================================================
# Function 2 — decisive_closes
# ===========================================================================

class TestDecisiveCloses:
    """
    Setup: price=100, atr14=10, tol=3
    Level = 90 (support)
    tol band: [87, 93]
    """
    price = 100.0
    atr14 = 10.0
    level = 90.0

    def test_empty_df(self):
        assert decisive_closes(pd.DataFrame(), {"S1": 90.0}, 10.0, 100.0) == []

    def test_close_within_tol_omitted(self):
        """Latest close at 91 (within [87,93]) → omitted."""
        df = _df([95]*3, [88]*3, [95, 92, 91])
        result = decisive_closes(df, {"S1": 90.0}, self.atr14, self.price)
        assert result == []

    def test_decisive_above_fresh_flip(self):
        """
        Bars: [close=85, close=86, close=97]
        Latest close=97 > level+tol=93 → direction "above"
        Walk newest→oldest: bar2(97>90→above), bar1(86<90→STOP)
        bars_ago = 1
        """
        df = _df([95]*3, [83]*3, [85, 86, 97])
        result = decisive_closes(df, {"S1": 90.0}, self.atr14, self.price)
        assert len(result) == 1
        r = result[0]
        assert r["direction"] == "above"
        assert r["bars_ago"] == 1
        assert r["confirmed"] is True

    def test_decisive_above_multi_bar(self):
        """
        Bars: [close=85, close=97, close=98, close=99]
        Latest=99 > 93 → above
        Walk: 99(above), 98(above), 97(above), 85(below→STOP) → bars_ago=3
        """
        df = _df([99]*4, [83]*4, [85, 97, 98, 99])
        result = decisive_closes(df, {"S1": 90.0}, self.atr14, self.price)
        assert len(result) == 1
        assert result[0]["bars_ago"] == 3

    def test_decisive_below(self):
        """
        Bars: [close=95, close=85, close=84]
        Latest=84 < level-tol=87 → direction "below"
        Walk: 84(below), 85(below), 95(above→STOP) → bars_ago=2
        """
        df = _df([97]*3, [82]*3, [95, 85, 84])
        result = decisive_closes(df, {"S1": 90.0}, self.atr14, self.price)
        assert len(result) == 1
        r = result[0]
        assert r["direction"] == "below"
        assert r["bars_ago"] == 2

    def test_none_level_skipped(self):
        df = _df([95]*3, [83]*3, [85, 86, 97])
        result = decisive_closes(df, {"S1": None}, self.atr14, self.price)
        assert result == []

    def test_multiple_levels(self):
        """
        Levels: S1=90 (close=97 → above), S2=80 (close=97 → above, bars_ago=3)
        Bars: [close=75, close=97, close=98, close=97]
        For S1=90 (tol=3, band=[87,93]): latest=97>93 → above
          Walk: 97,98,97 all >90, 75<90 → bars_ago=3
        For S2=80 (tol=3, band=[77,83]): latest=97>83 → above
          Walk: 97,98,97 all >80, 75<80 → bars_ago=3
        """
        df = _df([99]*4, [73]*4, [75, 97, 98, 97])
        result = decisive_closes(df, {"S1": 90.0, "S2": 80.0}, self.atr14, self.price)
        assert len(result) == 2
        by_level = {r["level"]: r for r in result}
        assert by_level["S1"]["direction"] == "above"
        assert by_level["S2"]["direction"] == "above"
        assert by_level["S1"]["bars_ago"] == 3
        assert by_level["S2"]["bars_ago"] == 3


# ===========================================================================
# Function 3 — candle_geometry
# ===========================================================================

class TestCandleGeometry:
    def test_zero_range_no_crash(self):
        r = candle_geometry(100, 100, 100, 100)
        assert r["pattern_label"] is None
        assert r["body"] == 0.0
        assert r["body_pct"] == 0.0

    def test_malformed_candle_high_below_low(self):
        """Candle with h < l (malformed) should not crash and return pattern_label == None."""
        r = candle_geometry(100, 90, 110, 105)  # h=90 < l=110
        assert r["pattern_label"] is None
        assert r["body"] == 0.0
        assert r["body_pct"] == 0.0

    def test_marubozu(self):
        """
        Full bull candle: o=100, h=110, l=100, c=110
        rng=10, body=10, body_pct=1.0 → marubozu
        upper_wick = 110 - max(100,110) = 0
        lower_wick = min(100,110) - 100 = 0
        close_in_range = (110-100)/10 = 1.0
        """
        r = candle_geometry(100, 110, 100, 110)
        assert r["pattern_label"] == "marubozu"
        assert r["body_pct"] == pytest.approx(1.0)
        assert r["close_in_range"] == pytest.approx(1.0)

    def test_marubozu_bear(self):
        """Bear marubozu: o=110, h=110, l=100, c=100 → body_pct=1.0"""
        r = candle_geometry(110, 110, 100, 100)
        assert r["pattern_label"] == "marubozu"

    def test_hammer(self):
        """
        Hammer: long lower wick, small body near top.
        o=108, h=109, l=100, c=109
        rng=9
        body = abs(109-108) = 1; body_pct = 1/9 ≈ 0.111 (not marubozu)
        upper_wick = 109 - max(108,109) = 0
        lower_wick = min(108,109) - 100 = 8
        denom = body = 1
        upper_wick_ratio = 0/1 = 0.0 (<= 0.5 ✓)
        lower_wick_ratio = 8/1 = 8.0 (>= 2 ✓)
        close_in_range = (109-100)/9 = 1.0 (>= 0.6 ✓)
        → hammer
        """
        r = candle_geometry(108, 109, 100, 109)
        assert r["pattern_label"] == "hammer"
        assert r["lower_wick_ratio"] >= 2.0
        assert r["upper_wick_ratio"] <= 0.5
        assert r["close_in_range"] >= 0.6

    def test_inverted_hammer(self):
        """
        Inverted hammer: long upper wick, small body near bottom.
        o=102, h=110, l=101, c=101
        rng=9
        body = abs(101-102) = 1; body_pct = 1/9 ≈ 0.111
        upper_wick = 110 - max(102,101) = 8
        lower_wick = min(102,101) - 101 = 0
        denom = 1
        upper_wick_ratio = 8 (>= 2 ✓)
        lower_wick_ratio = 0 (<= 0.5 ✓)
        close_in_range = (101-101)/9 = 0.0 (<= 0.4 ✓)
        → inverted_hammer
        """
        r = candle_geometry(102, 110, 101, 101)
        assert r["pattern_label"] == "inverted_hammer"
        assert r["upper_wick_ratio"] >= 2.0
        assert r["lower_wick_ratio"] <= 0.5
        assert r["close_in_range"] <= 0.4

    def test_doji(self):
        """
        Doji: very small body.
        o=100, h=105, l=95, c=100.4
        rng=10, body=0.4, body_pct=0.04 < 0.1
        No long dominant wick (wicks are ~5 each) → not hammer/inverted_hammer
        → doji
        """
        r = candle_geometry(100, 105, 95, 100.4)
        assert r["pattern_label"] == "doji"
        assert r["body_pct"] < 0.1

    def test_small_body_long_lower_wick_is_hammer_not_doji(self):
        """
        Even with body_pct < 0.1, if it has a long lower wick, it should be hammer.
        o=104, h=105, l=100, c=104.2
        rng=5, body=0.2, body_pct=0.04 < 0.1  (doji territory)
        upper_wick = 105 - 104.2 = 0.8
        lower_wick = 104 - 100 = 4
        denom = 0.2
        lower_wick_ratio = 4/0.2 = 20 (>= 2 ✓)
        upper_wick_ratio = 0.8/0.2 = 4.0 (> 0.5 ✗)
        → NOT hammer (upper_wick_ratio > 0.5)

        Try again with tighter upper wick:
        o=104.1, h=104.3, l=100, c=104.2
        rng=4.3, body=0.1, body_pct=0.1/4.3 ≈ 0.023 < 0.1
        upper_wick = 104.3 - 104.2 = 0.1
        lower_wick = 104.1 - 100 = 4.1
        denom = 0.1
        lower_wick_ratio = 4.1/0.1 = 41 (>= 2 ✓)
        upper_wick_ratio = 0.1/0.1 = 1.0 (> 0.5 ✗)
        Still fails. Let's use: o=104.05, h=104.1, l=100, c=104.09
        body=0.04; upper=0.01; lower=4.05; denom=0.04
        upper_wick_ratio=0.25 (<= 0.5 ✓)
        lower_wick_ratio=101.25 (>= 2 ✓)
        close_in_range=(104.09-100)/4.1 ≈ 0.998 >= 0.6 ✓
        body_pct=0.04/4.1 ≈ 0.0098 < 0.1 → but hammer checked FIRST → hammer
        """
        r = candle_geometry(104.05, 104.1, 100, 104.09)
        assert r["pattern_label"] == "hammer"  # NOT doji

    def test_no_pattern(self):
        """
        A plain candle with moderate body and moderate wicks → None.
        o=100, h=107, l=96, c=104
        rng=11, body=4, body_pct=4/11≈0.364 (not marubozu)
        upper_wick=107-104=3; lower_wick=100-96=4
        denom=4
        upper_wick_ratio=3/4=0.75 (not inverted_hammer: 0.75 < 2)
        lower_wick_ratio=4/4=1.0 (not hammer: < 2)
        body_pct=0.364 (not doji: >= 0.1)
        → None
        """
        r = candle_geometry(100, 107, 96, 104)
        assert r["pattern_label"] is None

    def test_gap_pct_computed(self):
        """gap_pct = (o - prev_close) / prev_close * 100"""
        r = candle_geometry(105, 110, 103, 108, prev_close=100)
        assert r["gap_pct"] == pytest.approx(5.0)

    def test_gap_pct_none_prev_close(self):
        r = candle_geometry(105, 110, 103, 108)
        assert r["gap_pct"] == pytest.approx(0.0)

    def test_all_fields_present(self):
        r = candle_geometry(100, 110, 90, 105)
        for field in ("body", "body_pct", "upper_wick", "lower_wick",
                      "upper_wick_ratio", "lower_wick_ratio", "close_in_range",
                      "gap_pct", "pattern_label"):
            assert field in r

    def test_numeric_values_correct(self):
        """
        o=100, h=110, l=90, c=105
        rng=20
        body=5, body_pct=5/20=0.25
        upper_wick=110-105=5; lower_wick=100-90=10
        denom=5
        upper_wick_ratio=5/5=1.0
        lower_wick_ratio=10/5=2.0
        close_in_range=(105-90)/20=0.75
        lower_wick_ratio=2.0 >= 2 ✓
        upper_wick_ratio=1.0 > 0.5 ✗ → not hammer
        body_pct=0.25 >= 0.1 → not doji
        pattern_label=None
        """
        r = candle_geometry(100, 110, 90, 105)
        assert r["body"] == pytest.approx(5.0)
        assert r["body_pct"] == pytest.approx(0.25)
        assert r["upper_wick"] == pytest.approx(5.0)
        assert r["lower_wick"] == pytest.approx(10.0)
        assert r["upper_wick_ratio"] == pytest.approx(1.0)
        assert r["lower_wick_ratio"] == pytest.approx(2.0)
        assert r["close_in_range"] == pytest.approx(0.75)
        assert r["pattern_label"] is None


# ===========================================================================
# Function 4 — cluster_distances
# ===========================================================================

class TestClusterDistances:
    def test_empty_input(self):
        r = cluster_distances({"supports": [], "resistances": []}, 100.0)
        assert r["nearest_support_dist_pct"] is None
        assert r["nearest_resistance_dist_pct"] is None
        assert r["clusters"] == []

    def test_no_supports(self):
        zones = {"supports": [], "resistances": [{"price": 110.0, "count": 1}]}
        r = cluster_distances(zones, 100.0)
        assert r["nearest_support_dist_pct"] is None
        assert r["nearest_resistance_dist_pct"] is not None

    def test_no_resistances(self):
        zones = {"supports": [{"price": 90.0, "count": 1}], "resistances": []}
        r = cluster_distances(zones, 100.0)
        assert r["nearest_resistance_dist_pct"] is None
        assert r["nearest_support_dist_pct"] is not None

    def test_nearest_support_is_largest_below(self):
        """
        Price=100, supports at 80 and 90 → nearest support = 90
        dist_pct = (90-100)/100*100 = -10.0
        """
        zones = {
            "supports": [{"price": 80.0}, {"price": 90.0}],
            "resistances": [],
        }
        r = cluster_distances(zones, 100.0)
        assert r["nearest_support_dist_pct"] == pytest.approx(-10.0)

    def test_nearest_resistance_is_smallest_above(self):
        """
        Price=100, resistances at 110 and 120 → nearest resistance = 110
        dist_pct = (110-100)/100*100 = +10.0
        """
        zones = {
            "supports": [],
            "resistances": [{"price": 120.0}, {"price": 110.0}],
        }
        r = cluster_distances(zones, 100.0)
        assert r["nearest_resistance_dist_pct"] == pytest.approx(10.0)

    def test_support_dist_is_negative(self):
        zones = {"supports": [{"price": 95.0}], "resistances": []}
        r = cluster_distances(zones, 100.0)
        assert r["nearest_support_dist_pct"] < 0

    def test_resistance_dist_is_positive(self):
        zones = {"supports": [], "resistances": [{"price": 105.0}]}
        r = cluster_distances(zones, 100.0)
        assert r["nearest_resistance_dist_pct"] > 0

    def test_clusters_sorted_by_abs_distance(self):
        """
        Price=100
        Support at 95  → dist = -5%
        Resistance at 103 → dist = +3%
        Sorted by abs: resistance(3%) first, then support(5%)
        """
        zones = {
            "supports": [{"price": 95.0, "count": 2}],
            "resistances": [{"price": 103.0, "count": 1}],
        }
        r = cluster_distances(zones, 100.0)
        assert len(r["clusters"]) == 2
        assert abs(r["clusters"][0]["dist_pct"]) <= abs(r["clusters"][1]["dist_pct"])
        assert r["clusters"][0]["side"] == "resistance"
        assert r["clusters"][1]["side"] == "support"

    def test_members_and_count_carried_through(self):
        """members and count are preserved in cluster entries."""
        zones = {
            "supports": [{"price": 90.0, "members": ["SMA200", "VWAP"], "count": 2}],
            "resistances": [{"price": 110.0, "members": ["CPR"], "count": 1}],
        }
        r = cluster_distances(zones, 100.0)
        sup_entry = next(c for c in r["clusters"] if c["side"] == "support")
        res_entry = next(c for c in r["clusters"] if c["side"] == "resistance")
        assert sup_entry["members"] == ["SMA200", "VWAP"]
        assert sup_entry["count"] == 2
        assert res_entry["members"] == ["CPR"]
        assert res_entry["count"] == 1

    def test_flat_list_input(self):
        """Bare list: items below price → support, above → resistance."""
        zones = [{"price": 90.0}, {"price": 110.0}]
        r = cluster_distances(zones, 100.0)
        assert r["nearest_support_dist_pct"] == pytest.approx(-10.0)
        assert r["nearest_resistance_dist_pct"] == pytest.approx(10.0)

    def test_all_key_input(self):
        """Dict with 'all' key."""
        zones = {"all": [{"price": 90.0}, {"price": 110.0}]}
        r = cluster_distances(zones, 100.0)
        assert r["nearest_support_dist_pct"] is not None
        assert r["nearest_resistance_dist_pct"] is not None

    def test_zone_at_price_excluded(self):
        """Zones exactly at price are neither support nor resistance."""
        zones = {
            "supports": [{"price": 100.0}],
            "resistances": [],
        }
        r = cluster_distances(zones, 100.0)
        assert r["nearest_support_dist_pct"] is None

    def test_dist_pct_values(self):
        """Verify dist_pct arithmetic precisely."""
        zones = {
            "supports": [{"price": 95.0}],
            "resistances": [{"price": 106.0}],
        }
        r = cluster_distances(zones, 100.0)
        assert r["nearest_support_dist_pct"] == pytest.approx(-5.0)
        assert r["nearest_resistance_dist_pct"] == pytest.approx(6.0)


# ===========================================================================
# Function 5 — ma_stack
# ===========================================================================

class TestMaStack:
    def _build_series(self, n, price_val, sma20_val, sma50_val, sma200_val):
        """Build constant series of length n."""
        return (
            [price_val] * n,
            [sma20_val] * n,
            [sma50_val] * n,
            [sma200_val] * n,
        )

    def test_empty_series(self):
        r = ma_stack([], [], [], [])
        assert r["bars_held"] == 0
        assert r["cross"] == "none"

    def test_bullish_state(self):
        """price > sma20 > sma50 > sma200 → bullish"""
        c, m20, m50, m200 = self._build_series(5, 200, 150, 100, 50)
        r = ma_stack(c, m20, m50, m200)
        assert r["state"] == "bullish"
        assert r["bars_held"] == 5
        assert r["cross"] == "none"

    def test_bearish_state(self):
        """price < sma20 < sma50 < sma200 → bearish"""
        c, m20, m50, m200 = self._build_series(3, 30, 60, 90, 120)
        r = ma_stack(c, m20, m50, m200)
        assert r["state"] == "bearish"
        assert r["bars_held"] == 3

    def test_bullish_partial_above_200(self):
        """price > sma200 but order not strict bullish → bullish_partial"""
        # price=200 > sma200=50 but sma20=100 < sma50=150 → not bullish
        c, m20, m50, m200 = self._build_series(4, 200, 100, 150, 50)
        r = ma_stack(c, m20, m50, m200)
        assert r["state"] == "bullish_partial"

    def test_bearish_partial_below_200(self):
        """price < sma200 but order not strict bearish → bearish_partial"""
        # price=30 < sma200=120 but sma20=90 > sma50=60 → not bearish
        c, m20, m50, m200 = self._build_series(2, 30, 90, 60, 120)
        r = ma_stack(c, m20, m50, m200)
        assert r["state"] == "bearish_partial"

    def test_bars_held_breaks_correctly(self):
        """
        5 bars of bullish then 1 bar of bearish_partial (oldest)
        Bars (oldest→newest): [mixed, bullish, bullish, bullish, bullish, bullish]
        bars_held = 5
        """
        # Last 5 bars: price=200>sma20=150>sma50=100>sma200=50 (bullish)
        # First bar: price=80 < sma200=50? No...let's use price=70, sma20=80, sma50=90, sma200=60
        # state = price<sma200? 70>60 → bullish_partial (breaks bullish streak)
        closes  = [70]  + [200] * 5
        sma20s  = [80]  + [150] * 5
        sma50s  = [90]  + [100] * 5
        sma200s = [60]  + [50]  * 5
        r = ma_stack(closes, sma20s, sma50s, sma200s)
        assert r["state"] == "bullish"
        assert r["bars_held"] == 5

    def test_golden_cross(self):
        """
        sma50 crosses from below sma200 to above sma200 on the last bar.
        Prev bar: sma50=95 <= sma200=100
        Last bar: sma50=105 > sma200=100
        """
        closes  = [200, 200]
        sma20s  = [150, 150]
        sma50s  = [95,  105]
        sma200s = [100, 100]
        r = ma_stack(closes, sma20s, sma50s, sma200s)
        assert r["cross"] == "golden_cross"

    def test_death_cross(self):
        """
        sma50 crosses from above sma200 to below sma200 on the last bar.
        Prev: sma50=105 >= sma200=100
        Last: sma50=95 < sma200=100
        """
        closes  = [30, 30]
        sma20s  = [60, 60]
        sma50s  = [105, 95]
        sma200s = [100, 100]
        r = ma_stack(closes, sma20s, sma50s, sma200s)
        assert r["cross"] == "death_cross"

    def test_no_cross(self):
        """sma50 stays above sma200 for both bars → no cross."""
        closes  = [200, 200]
        sma20s  = [150, 150]
        sma50s  = [110, 115]
        sma200s = [100, 100]
        r = ma_stack(closes, sma20s, sma50s, sma200s)
        assert r["cross"] == "none"

    def test_single_bar_no_cross(self):
        """Only 1 bar → no cross possible."""
        r = ma_stack([200], [150], [100], [50])
        assert r["cross"] == "none"

    def test_pandas_series_input(self):
        """Accepts pd.Series as well as lists."""
        closes  = pd.Series([200, 200, 200])
        sma20s  = pd.Series([150, 150, 150])
        sma50s  = pd.Series([100, 100, 100])
        sma200s = pd.Series([50,  50,  50])
        r = ma_stack(closes, sma20s, sma50s, sma200s)
        assert r["state"] == "bullish"
        assert r["bars_held"] == 3

    def test_none_ma_values_insufficient(self):
        """All MA values None → insufficient (no bearish/bullish mislabel)."""
        closes  = [100, 100]
        sma20s  = [None, None]
        sma50s  = [None, None]
        sma200s = [None, None]
        r = ma_stack(closes, sma20s, sma50s, sma200s)
        assert r["state"] == "insufficient"
        assert r["bars_held"] == 0
        assert r["cross"] == "none"

    def test_bullish_recent_but_sma200_none_is_insufficient(self):
        """Bullish-ordered recent bar but sma200 is None → insufficient, not bearish."""
        # 5 bars with clearly bullish price/sma20/sma50 ordering but sma200 all None
        # (fewer than 200 bars of history)
        closes  = [200, 200, 200, 200, 200]
        sma20s  = [150, 150, 150, 150, 150]
        sma50s  = [100, 100, 100, 100, 100]
        sma200s = [None, None, None, None, None]
        r = ma_stack(closes, sma20s, sma50s, sma200s)
        assert r["state"] == "insufficient"
        assert r["bars_held"] == 0

    def test_result_keys(self):
        r = ma_stack([200]*3, [150]*3, [100]*3, [50]*3)
        assert set(r.keys()) == {"state", "bars_held", "cross"}


# ===========================================================================
# Fix 1 — cluster_distances: members dict→string coercion
# ===========================================================================

class TestClusterDistancesMembersCoercion:
    """cluster_distances must coerce list-of-dicts members (real compute_confluences
    shape) to a plain list of code strings so "+".join(members) works in ta_cards."""

    def test_dict_members_coerced_to_code_strings(self):
        """Real compute_confluences shape: members are list-of-dicts with 'code' key."""
        zones = {
            "supports": [{
                "price": 99.8,
                "members": [
                    {"code": "POC", "name": "Point of Control",
                     "price": 99.8, "weight": 3.0, "family": "vp"}
                ],
                "count": 1,
            }],
            "resistances": [],
        }
        r = cluster_distances(zones, 100.0)
        assert len(r["clusters"]) == 1
        assert r["clusters"][0]["members"] == ["POC"]

    def test_string_members_pass_through_unchanged(self):
        """When members are already strings, they are returned as-is (back-compat)."""
        zones = {
            "supports": [{"price": 90.0, "members": ["SMA50", "PWH", "R1"], "count": 3}],
            "resistances": [],
        }
        r = cluster_distances(zones, 100.0)
        assert r["clusters"][0]["members"] == ["SMA50", "PWH", "R1"]

    def test_dict_without_code_key_is_dropped(self):
        """Dicts with no 'code' key produce None which is filtered out."""
        zones = {
            "supports": [{
                "price": 90.0,
                "members": [
                    {"name": "No Code Entry", "price": 90.0},  # no 'code'
                    {"code": "VWAP", "name": "VWAP", "price": 90.0},
                ],
            }],
            "resistances": [],
        }
        r = cluster_distances(zones, 100.0)
        assert r["clusters"][0]["members"] == ["VWAP"]

    def test_mixed_dict_and_string_members(self):
        """Mixed list (some dicts, some strings) is handled gracefully."""
        zones = {
            "supports": [{
                "price": 90.0,
                "members": [
                    {"code": "SMA200", "name": "SMA 200"},
                    "R1",   # already a plain string
                ],
            }],
            "resistances": [],
        }
        r = cluster_distances(zones, 100.0)
        assert r["clusters"][0]["members"] == ["SMA200", "R1"]

    def test_members_joinable_as_strings(self):
        """After coercion, '+'.join(members) must not raise TypeError."""
        zones = {
            "resistances": [{
                "price": 110.0,
                "members": [
                    {"code": "R1", "name": "R1 pivot", "weight": 2.0, "family": "pivot"},
                    {"code": "SMA200", "name": "SMA200", "weight": 3.0, "family": "ma"},
                ],
                "count": 2,
            }],
            "supports": [],
        }
        r = cluster_distances(zones, 100.0)
        members = r["clusters"][0]["members"]
        # This must not raise — it's exactly what ta_cards.py does
        result = "+".join(members)
        assert result == "R1+SMA200"


# ===========================================================================
# Function — price_percentile
# ===========================================================================

class TestPricePercentile:
    """Tests for price_percentile.

    Convention: the latest close is ranked against the PRECEDING `window` closes
    (i.e. the latest close itself is EXCLUDED from the comparison window).
    Percentile = (count of window values <= latest) / window * 100, rounded to int.
    """

    def test_latest_is_max_returns_100(self):
        # window=3, window_vals=[10,20,30], latest=40
        # count_le=3, pct=3/3*100=100
        closes = [10.0, 20.0, 30.0, 40.0]
        result = price_percentile(closes, window=3)
        assert result == 100

    def test_latest_is_min_returns_0(self):
        # window=3, window_vals=[10,20,30], latest=5
        # count_le=0, pct=0/3*100=0
        closes = [10.0, 20.0, 30.0, 5.0]
        result = price_percentile(closes, window=3)
        assert result == 0

    def test_latest_is_median_returns_50(self):
        # window=4, window_vals=[10,20,30,40], latest=20
        # count_le=2 (10 and 20 are <=20), pct=2/4*100=50
        closes = [10.0, 20.0, 30.0, 40.0, 20.0]
        result = price_percentile(closes, window=4)
        assert result == 50

    def test_ascending_series_latest_is_max(self):
        # window=5, closes=[1,2,3,4,5,6], window_vals=[1,2,3,4,5], latest=6
        # count_le=5, pct=5/5*100=100
        closes = [float(i) for i in range(1, 7)]
        result = price_percentile(closes, window=5)
        assert result == 100

    def test_insufficient_data_returns_none(self):
        # 50 closes, window=90 → need 91, have 50 → None
        closes = [float(i) for i in range(50)]
        result = price_percentile(closes, window=90)
        assert result is None

    def test_empty_list_returns_none(self):
        assert price_percentile([], window=90) is None

    def test_none_input_returns_none(self):
        # Non-numeric values get filtered; after filter not enough → None
        closes = ["a", "b", "c"] * 40  # 120 entries but all non-numeric
        assert price_percentile(closes, window=90) is None  # type: ignore[arg-type]

    def test_result_is_int(self):
        closes = [float(i) for i in range(100)]
        result = price_percentile(closes, window=90)
        assert result is not None
        assert isinstance(result, int)

    def test_result_clamped_0_to_100(self):
        closes = [float(i) for i in range(100)]
        result = price_percentile(closes, window=90)
        assert result is not None
        assert 0 <= result <= 100

    def test_default_window_is_90(self):
        # Exactly 91 closes (minimum for window=90) → should return a value
        closes = [float(i) for i in range(91)]
        result = price_percentile(closes)  # no window arg
        assert result is not None

    def test_exactly_window_plus_one_closes(self):
        # window=3, exactly 4 closes → should compute, not return None
        closes = [1.0, 2.0, 3.0, 4.0]
        result = price_percentile(closes, window=3)
        assert result is not None
        assert result == 100  # latest=4 > window=[1,2,3]


# ===========================================================================
# Function — funding_streak
# ===========================================================================

class TestFundingStreak:
    """Tests for funding_streak.

    spec:
    - Filter series to numeric values (drop None/NaN/str).
    - Return None if fewer than 2 usable values OR latest == 0.
    - sign = "positive" if latest > 0 else "negative"
    - streak_days = consecutive same-sign count from newest end (0 or opposite breaks)
    - percentile = rank of latest vs preceding window (capped to window_days, EXCLUDE latest)
      pct = count(v <= latest) / N * 100, rounded int, clamped 0..100
    - Returns {"streak_days", "sign", "latest", "percentile", "window_days": N}
    """

    def test_all_positive_same_value(self):
        # [0.01]*6 → sign positive, streak_days 6, percentile 100
        # preceding=[0.01]*5, count(v<=0.01)=5, pct=100
        result = funding_streak([0.01] * 6)
        assert result is not None
        assert result["sign"] == "positive"
        assert result["streak_days"] == 6
        assert result["percentile"] == 100
        assert result["window_days"] == 5  # actual N = 5 preceding values

    def test_mixed_ends_positive(self):
        # [-0.01,-0.01,0.01,0.02] → positive, streak_days 2, percentile 100
        # streak: 0.02(pos), 0.01(pos), -0.01(neg→stop) = 2
        # window=[-0.01,-0.01,0.01], count(v<=0.02)=3/3=100
        result = funding_streak([-0.01, -0.01, 0.01, 0.02])
        assert result is not None
        assert result["sign"] == "positive"
        assert result["streak_days"] == 2
        assert result["percentile"] == 100

    def test_negative_streak(self):
        # [0.01,-0.01,-0.02] → negative, streak_days 2, percentile 0
        # streak: -0.02(neg), -0.01(neg), 0.01(pos→stop) = 2
        # window=[0.01,-0.01], count(v<=-0.02)=0
        result = funding_streak([0.01, -0.01, -0.02])
        assert result is not None
        assert result["sign"] == "negative"
        assert result["streak_days"] == 2
        assert result["percentile"] == 0

    def test_single_value_returns_none(self):
        assert funding_streak([0.01]) is None

    def test_empty_returns_none(self):
        assert funding_streak([]) is None

    def test_latest_zero_returns_none(self):
        # Latest value == 0 → None (sign is undefined)
        assert funding_streak([0.01, 0.02, 0.0]) is None

    def test_non_numeric_tail_filtered(self):
        # [0.01, 0.02, "bad", None] → filter → [0.01, 0.02]
        # latest=0.02, sign="positive", streak=2, window=[0.01], pct=100
        result = funding_streak([0.01, 0.02, "bad", None])
        assert result is not None
        assert result["sign"] == "positive"
        assert result["streak_days"] == 2
        assert result["percentile"] == 100

    def test_window_days_is_actual_n_used(self):
        # 7 values: window_days param=90, but only 6 preceding → N=6
        result = funding_streak([0.01] * 7)
        assert result is not None
        assert result["window_days"] == 6

    def test_window_days_capped_at_param(self):
        # 100 positive values, window_days=5 → N=5 (capped)
        result = funding_streak([0.01] * 100, window_days=5)
        assert result is not None
        assert result["window_days"] == 5

    def test_return_keys(self):
        result = funding_streak([0.01, 0.02, 0.03])
        assert result is not None
        for key in ("streak_days", "sign", "latest", "percentile", "window_days"):
            assert key in result

    def test_streak_days_minimum_one(self):
        # single valid latest after filtering, but with a different-sign predecessor
        result = funding_streak([-0.01, 0.02])
        assert result is not None
        assert result["streak_days"] == 1  # only the latest is positive

    def test_non_fatal_on_bad_input(self):
        # Should not raise even with weird inputs
        result = funding_streak(None)  # type: ignore[arg-type]
        assert result is None

    def test_inf_values_filtered_out(self):
        """±inf values must be excluded from the usable filter.

        An inf `latest` would propagate into the bundle and break
        json.dumps(allow_nan=False) in store_asset_facts for that asset.
        After filtering [0.01, 0.02, inf, -inf, 0.03] → [0.01, 0.02, 0.03].
        """
        result = funding_streak([0.01, 0.02, float("inf"), float("-inf"), 0.03])
        assert result is not None
        assert math.isfinite(result["latest"])
        assert result["latest"] == pytest.approx(0.03)
        assert result["sign"] == "positive"

    def test_pos_inf_only_returns_none(self):
        """A series of only ±inf values → all filtered → fewer than 2 usable → None."""
        result = funding_streak([float("inf"), float("-inf")])
        assert result is None
