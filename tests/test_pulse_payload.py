"""
Unit tests for carrac/reports/pulse.py pure helpers.

Only the pure functions (nearest_cpr_level, trend_label) are tested here
because they have no external dependencies.  _build_board, _build_weather,
_build_calendar all require live caches / DB and are covered by integration runs.
"""

# ---------------------------------------------------------------------------
# Import only the pure helpers — avoids pulling in Flask / app imports.
# ---------------------------------------------------------------------------


def _import_pure_helpers():
    """Import pulse module helpers without triggering Flask app init.

    pulse.py's top-level imports are all deferred inside functions
    (standard pattern in this codebase), so a plain import works fine
    at module level.
    """
    import carrac.reports.pulse as p
    return p.nearest_cpr_level, p.trend_label


nearest_cpr_level, trend_label = _import_pure_helpers()


# ---------------------------------------------------------------------------
# trend_label
# ---------------------------------------------------------------------------

class TestTrendLabel:
    def test_strong_up(self):
        assert trend_label(3) == "strong up"

    def test_strong_down(self):
        assert trend_label(-3) == "strong down"

    def test_flat(self):
        assert trend_label(0) == "flat"

    def test_mild_up(self):
        assert trend_label(1) == "mild up"

    def test_mild_down(self):
        assert trend_label(-1) == "mild down"

    def test_up(self):
        assert trend_label(2) == "up"

    def test_down(self):
        assert trend_label(-2) == "down"

    def test_none_is_flat(self):
        assert trend_label(None) == "flat"

    def test_out_of_range_defaults_flat(self):
        assert trend_label(99) == "flat"

    def test_string_int(self):
        # Consensio signals sometimes arrive as floats from JSON
        assert trend_label(3.0) == "strong up"


# ---------------------------------------------------------------------------
# nearest_cpr_level
# ---------------------------------------------------------------------------

class TestNearestCprLevel:
    """Tests for nearest_cpr_level(price, cpr)."""

    BASE_CPR = {"pivot": 100, "r1": 110, "s1": 90, "r2": 120, "s2": 80}

    def test_picks_closest_level(self):
        # price=103: dist to pivot=3, dist to r1=7 → pivot wins
        out = nearest_cpr_level(103, self.BASE_CPR)
        assert out["level_name"] == "pivot"

    def test_pivot_is_support_when_below_price(self):
        out = nearest_cpr_level(103, self.BASE_CPR)
        assert out["side"] == "support"

    def test_distance_pct_calculation(self):
        out = nearest_cpr_level(103, self.BASE_CPR)
        # distance_pct = (103 - 100) / 100 * 100 = 3.0
        assert round(out["distance_pct"], 1) == 3.0

    def test_resistance_side_when_above_price(self):
        # price=95 → closest is s1(90, dist 5) or pivot(100, dist 5) — tie goes to first found
        # but r1=110 (dist 15), s1=90 (dist 5), pivot=100 (dist 5), r2=120 (dist 25), s2=80 (dist 15)
        # pivot and s1 tie at distance 5; min() returns first found in list order (pivot first in _CPR_KEYS)
        out = nearest_cpr_level(95, self.BASE_CPR)
        # pivot(100) is above price(95) → resistance
        # s1(90) is below price(95) → support
        # Closest is either pivot or s1 (both dist=5); pivot comes first in _CPR_KEYS so wins
        assert out["level_name"] in ("pivot", "s1")
        # Verify side is consistent
        if out["level_name"] == "pivot":
            assert out["side"] == "resistance"
        else:
            assert out["side"] == "support"

    def test_returns_none_on_no_price(self):
        assert nearest_cpr_level(None, self.BASE_CPR) is None

    def test_returns_none_on_no_cpr(self):
        assert nearest_cpr_level(100, {}) is None

    def test_returns_none_on_none_cpr(self):
        assert nearest_cpr_level(100, None) is None

    def test_exact_price_at_level_is_support(self):
        # price exactly on pivot → side = support (lvl <= price)
        out = nearest_cpr_level(100, {"pivot": 100, "r1": 110})
        assert out["level_name"] == "pivot"
        assert out["side"] == "support"
        assert out["distance_pct"] == 0.0

    def test_all_cpr_keys_considered(self):
        cpr = {
            "pivot": 100, "tc": 102, "bc": 98,
            "s1": 90, "s2": 80, "s3": 70,
            "r1": 110, "r2": 120, "r3": 130,
        }
        # price=103: tc=102 (dist 1) is the closest
        out = nearest_cpr_level(103, cpr)
        assert out["level_name"] == "tc"
        assert out["side"] == "support"

    def test_ignores_none_values_in_cpr(self):
        # cpr with some None/missing values should still work
        cpr = {"pivot": None, "r1": 110, "s1": 90}
        out = nearest_cpr_level(100, cpr)
        assert out is not None
        assert out["level_name"] in ("r1", "s1")
