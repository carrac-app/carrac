"""TDD tests for carrac.facts.translate — statistics → intuition layer."""
from carrac.facts.translate import rarity_label, interpret


# ---------------------------------------------------------------------------
# rarity_label — 9-tier ladder
# ---------------------------------------------------------------------------

class TestRarityLabel:
    # None guard
    def test_none_returns_empty_string(self):
        assert rarity_label(None) == ""

    # Record low  (0–1)
    def test_0_is_record_low(self):
        assert rarity_label(0) == "Record low"

    def test_1_is_record_low(self):
        assert rarity_label(1) == "Record low"

    # Extremely low  (2–5)
    def test_2_is_extremely_low(self):
        assert rarity_label(2) == "Extremely low"

    def test_5_is_extremely_low(self):
        assert rarity_label(5) == "Extremely low"

    # Bottom decile  (6–10)
    def test_6_is_bottom_decile(self):
        assert rarity_label(6) == "Bottom decile"

    def test_10_is_bottom_decile(self):
        assert rarity_label(10) == "Bottom decile"

    # Below normal  (11–24)
    def test_11_is_below_normal(self):
        assert rarity_label(11) == "Below normal"

    def test_24_is_below_normal(self):
        assert rarity_label(24) == "Below normal"

    # Typical  (25–75)
    def test_25_is_typical(self):
        assert rarity_label(25) == "Typical"

    def test_50_is_typical(self):
        assert rarity_label(50) == "Typical"

    def test_75_is_typical(self):
        assert rarity_label(75) == "Typical"

    # Above normal  (76–89)
    def test_76_is_above_normal(self):
        assert rarity_label(76) == "Above normal"

    def test_89_is_above_normal(self):
        assert rarity_label(89) == "Above normal"

    # Top decile  (90–94)
    def test_90_is_top_decile(self):
        assert rarity_label(90) == "Top decile"

    def test_94_is_top_decile(self):
        assert rarity_label(94) == "Top decile"

    # Extremely high  (95–98)
    def test_95_is_extremely_high(self):
        assert rarity_label(95) == "Extremely high"

    def test_98_is_extremely_high(self):
        assert rarity_label(98) == "Extremely high"

    # Record high  (99–100)
    def test_99_is_record_high(self):
        assert rarity_label(99) == "Record high"

    def test_100_is_record_high(self):
        assert rarity_label(100) == "Record high"

    # Clamp out-of-range
    def test_negative_clamps_to_record_low(self):
        assert rarity_label(-5) == "Record low"

    def test_above_100_clamps_to_record_high(self):
        assert rarity_label(105) == "Record high"

    # Float inputs accepted
    def test_float_boundary(self):
        assert rarity_label(6.0) == "Bottom decile"

    def test_float_mid(self):
        assert rarity_label(50.5) == "Typical"


# ---------------------------------------------------------------------------
# interpret — metric-specific plain-English hints
# ---------------------------------------------------------------------------

class TestInterpretRsi:
    def test_rsi_deeply_oversold(self):
        assert interpret("rsi", value=20) == "deeply oversold"

    def test_rsi_deeply_oversold_below_20(self):
        assert interpret("rsi", value=10) == "deeply oversold"

    def test_rsi_near_oversold(self):
        assert interpret("rsi", value=35) == "near oversold"

    def test_rsi_near_oversold_mid(self):
        assert interpret("rsi", value=28) == "near oversold"

    def test_rsi_deeply_overbought(self):
        assert interpret("rsi", value=80) == "deeply overbought"

    def test_rsi_deeply_overbought_above_80(self):
        assert interpret("rsi", value=90) == "deeply overbought"

    def test_rsi_near_overbought(self):
        assert interpret("rsi", value=65) == "near overbought"

    def test_rsi_near_overbought_mid(self):
        assert interpret("rsi", value=72) == "near overbought"

    def test_rsi_neutral_returns_none(self):
        assert interpret("rsi", value=50) is None

    def test_rsi_exactly_36_is_none(self):
        # 36 is above 35, below 65 — no extreme
        assert interpret("rsi", value=36) is None

    def test_rsi_exactly_64_is_none(self):
        assert interpret("rsi", value=64) is None


class TestInterpretVolume:
    def test_volume_rvol_half(self):
        assert interpret("volume", value=0.5) == "less than half its normal activity"

    def test_volume_rvol_very_low(self):
        assert interpret("volume", value=0.3) == "less than half its normal activity"

    def test_volume_rvol_below_avg(self):
        assert interpret("volume", value=0.8) == "below-average activity"

    def test_volume_rvol_below_avg_mid(self):
        assert interpret("volume", value=0.7) == "below-average activity"

    def test_volume_rvol_sharp_surge(self):
        assert interpret("volume", value=2.5) == "a sharp surge in activity"

    def test_volume_rvol_above_surge(self):
        assert interpret("volume", value=4.0) == "a sharp surge in activity"

    def test_volume_rvol_elevated(self):
        assert interpret("volume", value=1.5) == "elevated activity"

    def test_volume_rvol_elevated_mid(self):
        assert interpret("volume", value=2.0) == "elevated activity"

    def test_volume_rvol_normal_returns_none(self):
        assert interpret("volume", value=1.0) is None

    def test_volume_rvol_just_above_threshold(self):
        # 0.81 is above 0.8, below 1.5 — normal territory
        assert interpret("volume", value=0.81) is None


class TestInterpretMayer:
    def test_mayer_pct_very_low(self):
        assert interpret("mayer", pct=5) == "further below its 200-week average than almost ever"

    def test_mayer_pct_below_5(self):
        assert interpret("mayer", pct=2) == "further below its 200-week average than almost ever"

    def test_mayer_pct_below_usual_stretch(self):
        assert interpret("mayer", pct=20) == "below its usual stretch"

    def test_mayer_pct_below_usual_mid(self):
        assert interpret("mayer", pct=12) == "below its usual stretch"

    def test_mayer_pct_very_high(self):
        assert interpret("mayer", pct=95) == "more stretched than almost ever"

    def test_mayer_pct_above_95(self):
        assert interpret("mayer", pct=99) == "more stretched than almost ever"

    def test_mayer_pct_above_usual_stretch(self):
        assert interpret("mayer", pct=80) == "above its usual stretch"

    def test_mayer_pct_above_usual_mid(self):
        assert interpret("mayer", pct=88) == "above its usual stretch"

    def test_mayer_pct_middle_returns_none(self):
        assert interpret("mayer", pct=50) is None

    def test_mayer_pct_just_above_20_returns_none(self):
        assert interpret("mayer", pct=21) is None

    def test_mayer_pct_just_below_80_returns_none(self):
        assert interpret("mayer", pct=79) is None


class TestInterpretFunding:
    def test_funding_pct_low(self):
        assert interpret("funding", pct=10) == "lower than usual"

    def test_funding_pct_very_low(self):
        assert interpret("funding", pct=3) == "lower than usual"

    def test_funding_pct_high(self):
        assert interpret("funding", pct=90) == "higher than usual"

    def test_funding_pct_very_high(self):
        assert interpret("funding", pct=97) == "higher than usual"

    def test_funding_pct_middle_returns_none(self):
        assert interpret("funding", pct=50) is None

    def test_funding_pct_just_above_10_returns_none(self):
        assert interpret("funding", pct=11) is None

    def test_funding_pct_just_below_90_returns_none(self):
        assert interpret("funding", pct=89) is None


class TestInterpretRange:
    def test_range_always_none(self):
        assert interpret("range") is None

    def test_range_with_value_none(self):
        assert interpret("range", value=42) is None

    def test_range_with_pct_none(self):
        assert interpret("range", pct=99) is None


class TestInterpretUnknownMetric:
    def test_unknown_metric_returns_none(self):
        assert interpret("unknown_metric", value=99, pct=99) is None

    def test_empty_string_metric_returns_none(self):
        assert interpret("", value=50) is None

    def test_btc_dominance_returns_none(self):
        assert interpret("btc_dominance", pct=80) is None
