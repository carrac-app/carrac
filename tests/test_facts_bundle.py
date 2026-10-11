"""Tests for carrac.facts.bundle — build_asset_facts."""
from __future__ import annotations

import pandas as pd

from carrac.facts.bundle import build_asset_facts


# ---------------------------------------------------------------------------
# Helpers — stub ctx builder
# ---------------------------------------------------------------------------

def _make_ohlc(n: int = 10) -> pd.DataFrame:
    """Build a small OHLC DataFrame (oldest→newest)."""
    close = [100.0 + i for i in range(n)]
    return pd.DataFrame({
        "open":  [c - 0.5 for c in close],
        "high":  [c + 1.0 for c in close],
        "low":   [c - 1.0 for c in close],
        "close": close,
    })


def _make_full_ctx(daily_closes_len: int = 1500) -> dict:
    """Full, valid stub ctx."""
    ohlc = _make_ohlc(20)
    closes = ohlc["close"].tolist()
    # pad sma series to same length
    sma20  = [c - 1 for c in closes]
    sma50  = [c - 2 for c in closes]
    sma200 = [c - 3 for c in closes]

    # Synthetic daily_closes for mayer — ascending from 50 to cover 1400+
    daily_closes = [50.0 + i * 0.05 for i in range(daily_closes_len)]

    return {
        "price": 119.0,
        "as_of": "2026-06-26T00:00:00",
        "daily": {
            "ohlc_df": ohlc,
            "atr14": 2.0,
            "sma20_series": sma20,
            "sma50_series": sma50,
            "sma200_series": sma200,
            "rsi": {"value": 55.0, "zone": "neutral", "divergence": "none"},
            "volume": {"rvol": 1.2, "signal": "high", "reliable": True},
            "candle": {"o": 118.5, "h": 120.0, "l": 117.0, "c": 119.0, "prev_close": 117.5},
        },
        "levels": {
            "SMA200": 116.0,
            "PDH":    121.0,
        },
        "monthly_cpr": {"position": "above_bc"},
        "key_levels": {"pdh": 121.0, "pdl": 115.0},
        "zones": {
            "supports":    [{"price": 115.0, "members": ["s1"], "count": 2}],
            "resistances": [{"price": 122.0, "members": ["r1"], "count": 1}],
        },
        "daily_closes": daily_closes,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildAssetFacts:

    def test_all_top_level_keys_present(self):
        ctx = _make_full_ctx()
        bundle = build_asset_facts("BTCUSDT", ctx)
        expected = {
            "symbol", "as_of", "price",
            "stretch", "ma_stack", "candle",
            "volume", "rsi", "levels",
            "streaks", "decisive_close", "timeframes",
        }
        assert expected <= set(bundle.keys()), f"Missing keys: {expected - set(bundle.keys())}"

    def test_symbol_and_as_of_echo(self):
        ctx = _make_full_ctx()
        bundle = build_asset_facts("ETHUSDT", ctx)
        assert bundle["symbol"] == "ETHUSDT"
        assert bundle["as_of"] == ctx["as_of"]
        assert bundle["price"] == ctx["price"]

    def test_stretch_populated_with_1400_closes(self):
        ctx = _make_full_ctx(daily_closes_len=1500)
        bundle = build_asset_facts("BTCUSDT", ctx)
        stretch = bundle["stretch"]
        assert "mayer" in stretch
        assert "mayer_pct" in stretch
        assert isinstance(stretch["mayer"], float)
        assert isinstance(stretch["mayer_pct"], int)

    def test_stretch_empty_when_daily_closes_absent(self):
        ctx = _make_full_ctx()
        del ctx["daily_closes"]
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert bundle["stretch"] == {}

    def test_stretch_empty_when_fewer_than_1400_closes(self):
        ctx = _make_full_ctx(daily_closes_len=100)
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert bundle["stretch"] == {}

    def test_clusters_populated(self):
        ctx = _make_full_ctx()
        bundle = build_asset_facts("BTCUSDT", ctx)
        levels = bundle["levels"]
        assert "clusters" in levels
        assert len(levels["clusters"]) > 0

    def test_levels_subkeys_present(self):
        ctx = _make_full_ctx()
        bundle = build_asset_facts("BTCUSDT", ctx)
        lv = bundle["levels"]
        for key in ("clusters", "nearest_support_dist_pct", "nearest_resistance_dist_pct",
                    "monthly_cpr", "key_levels"):
            assert key in lv, f"levels missing key: {key}"

    def test_streaks_is_list(self):
        ctx = _make_full_ctx()
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert isinstance(bundle["streaks"], list)

    def test_decisive_close_is_list(self):
        ctx = _make_full_ctx()
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert isinstance(bundle["decisive_close"], list)

    def test_rsi_volume_echoed(self):
        ctx = _make_full_ctx()
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert bundle["rsi"] == ctx["daily"]["rsi"]
        assert bundle["volume"] == ctx["daily"]["volume"]

    def test_timeframes_no_weekly(self):
        ctx = _make_full_ctx()
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert bundle["timeframes"] == ["1D"]
        assert "weekly" not in bundle

    def test_timeframes_with_weekly_block(self):
        ctx = _make_full_ctx()
        # Add a minimal weekly block (same shape as daily)
        ohlc_w = _make_ohlc(10)
        closes_w = ohlc_w["close"].tolist()
        ctx["weekly"] = {
            "ohlc_df": ohlc_w,
            "atr14": 5.0,
            "sma20_series": [c - 1 for c in closes_w],
            "sma50_series": [c - 2 for c in closes_w],
            "sma200_series": [c - 3 for c in closes_w],
            "rsi": {"value": 60.0, "zone": "bullish", "divergence": "none"},
            "volume": {"rvol": 0.8, "signal": "low", "reliable": True},
            "candle": {"o": 118.0, "h": 121.0, "l": 116.0, "c": 119.0, "prev_close": 117.0},
        }
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert bundle["timeframes"] == ["1D", "1W"]
        assert "weekly" in bundle
        weekly = bundle["weekly"]
        for key in ("streaks", "decisive_close", "ma_stack", "candle"):
            assert key in weekly, f"weekly block missing key: {key}"

    def test_partial_ctx_missing_zones_no_crash(self):
        ctx = _make_full_ctx()
        del ctx["zones"]
        bundle = build_asset_facts("BTCUSDT", ctx)
        # clusters should be empty list, not crash
        assert bundle["levels"]["clusters"] == []
        assert bundle["levels"]["nearest_support_dist_pct"] is None
        assert bundle["levels"]["nearest_resistance_dist_pct"] is None

    def test_partial_ctx_missing_rsi_no_crash(self):
        ctx = _make_full_ctx()
        del ctx["daily"]["rsi"]
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert bundle["rsi"] == {}

    def test_partial_ctx_missing_candle_no_crash(self):
        ctx = _make_full_ctx()
        del ctx["daily"]["candle"]
        bundle = build_asset_facts("BTCUSDT", ctx)
        # candle should be {} or an empty result, not crash
        assert isinstance(bundle["candle"], dict)

    def test_monthly_cpr_and_key_levels_echoed(self):
        ctx = _make_full_ctx()
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert bundle["levels"]["monthly_cpr"] == ctx["monthly_cpr"]
        assert bundle["levels"]["key_levels"] == ctx["key_levels"]

    def test_missing_price_no_crash(self):
        """Price None → graceful degradation: empty clusters, empty streaks/decisive, None price."""
        ctx = _make_full_ctx()
        del ctx["price"]  # Simulate missing price
        bundle = build_asset_facts("BTCUSDT", ctx)

        # Price should be None
        assert bundle["price"] is None

        # Clusters and distance metrics should be empty/None
        assert bundle["levels"]["clusters"] == []
        assert bundle["levels"]["nearest_support_dist_pct"] is None
        assert bundle["levels"]["nearest_resistance_dist_pct"] is None

        # Streaks and decisive closes should be empty lists
        assert bundle["streaks"] == []
        assert bundle["decisive_close"] == []

    # -----------------------------------------------------------------------
    # range_pctile tests
    # -----------------------------------------------------------------------

    def test_range_pctile_present_with_sufficient_closes(self):
        """≥91 closes → range_pctile key present with pct in 0..100 and window_days=90."""
        ctx = _make_full_ctx()
        ctx["daily_closes"] = [float(i) for i in range(100)]  # 100 >= 91
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert "range_pctile" in bundle, "range_pctile key should be present"
        rp = bundle["range_pctile"]
        assert 0 <= rp["pct"] <= 100
        assert rp["window_days"] == 90

    def test_range_pctile_absent_with_few_closes(self):
        """<91 closes → range_pctile key must be absent (not None, not there at all)."""
        ctx = _make_full_ctx()
        ctx["daily_closes"] = [float(i) for i in range(50)]  # 50 < 91
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert "range_pctile" not in bundle

    def test_range_pctile_absent_when_daily_closes_missing(self):
        """Missing daily_closes key → range_pctile absent, no crash."""
        ctx = _make_full_ctx()
        del ctx["daily_closes"]
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert "range_pctile" not in bundle

    # -----------------------------------------------------------------------
    # funding tests
    # -----------------------------------------------------------------------

    def test_funding_present_with_positive_series(self):
        """ctx with a positive funding_series → bundle has funding key with correct sign."""
        ctx = _make_full_ctx()
        ctx["funding_series"] = [0.01] * 10
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert "funding" in bundle, "funding key should be present when series provided"
        f = bundle["funding"]
        assert f["sign"] == "positive"
        assert f["streak_days"] == 10
        assert isinstance(f["percentile"], int)
        assert 0 <= f["percentile"] <= 100

    def test_funding_absent_without_funding_series(self):
        """ctx without funding_series → no funding key (omit, never null)."""
        ctx = _make_full_ctx()
        # No funding_series key at all
        assert "funding_series" not in ctx
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert "funding" not in bundle

    def test_funding_absent_when_series_is_none(self):
        """ctx with funding_series=None → no funding key."""
        ctx = _make_full_ctx()
        ctx["funding_series"] = None
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert "funding" not in bundle

    def test_funding_absent_when_series_is_empty(self):
        """ctx with funding_series=[] → funding_streak returns None → no funding key."""
        ctx = _make_full_ctx()
        ctx["funding_series"] = []
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert "funding" not in bundle

    def test_funding_absent_when_latest_is_zero(self):
        """funding_streak returns None for latest==0 → key omitted."""
        ctx = _make_full_ctx()
        ctx["funding_series"] = [0.01, 0.02, 0.0]
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert "funding" not in bundle

    def test_funding_non_fatal_on_bad_series(self):
        """A series that causes funding_streak to return None → bundle still returns, no crash."""
        ctx = _make_full_ctx()
        ctx["funding_series"] = [0.01]  # only 1 value → None from funding_streak
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert "funding" not in bundle
        # bundle itself is still valid
        assert "symbol" in bundle
