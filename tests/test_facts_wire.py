"""Unit tests for carrac/facts/wire.py — no precompute or network dependency."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from carrac.facts.wire import build_facts_ctx, store_asset_facts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_daily_df(n: int = 20, seed: int = 42) -> pd.DataFrame:
    """Synthetic daily OHLCV DataFrame with n rows."""
    rng = np.random.default_rng(seed)
    closes = 100.0 + np.cumsum(rng.standard_normal(n))
    return pd.DataFrame(
        {
            "open": closes * 0.99,
            "high": closes * 1.02,
            "low": closes * 0.98,
            "close": closes,
            "volume": rng.uniform(1000, 5000, n),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="D"),
    )


class _FakeRedis:
    """Redis stub that records set() calls."""

    def __init__(self):
        self.calls: list[dict] = []

    def set(self, key, value, ex=None):
        self.calls.append({"key": key, "value": value, "ex": ex})


class _BrokenRedis:
    """Redis stub whose set() always raises."""

    def set(self, key, value, ex=None):
        raise RuntimeError("Redis unavailable")


# ---------------------------------------------------------------------------
# build_facts_ctx — structure tests
# ---------------------------------------------------------------------------

class TestBuildFactsCtx:
    def test_top_level_keys_present(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        for key in ("price", "as_of", "daily", "levels", "zones",
                    "monthly_cpr", "key_levels", "daily_closes"):
            assert key in ctx, f"Missing top-level key: {key}"

    def test_daily_block_keys_present(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        daily = ctx["daily"]
        for key in ("ohlc_df", "atr14", "sma20_series", "sma50_series",
                    "sma200_series", "candle", "volume", "rsi"):
            assert key in daily, f"Missing daily key: {key}"

    # --- ATR14 ---

    def test_atr14_positive_with_enough_bars(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        assert ctx["daily"]["atr14"] > 0

    def test_atr14_zero_with_too_few_bars(self):
        df = _make_daily_df(10)
        ctx = build_facts_ctx("BTC", df, latest_price=50.0)
        assert ctx["daily"]["atr14"] == 0.0

    def test_atr14_zero_at_exactly_14_bars(self):
        df = _make_daily_df(14)
        ctx = build_facts_ctx("BTC", df, latest_price=50.0)
        assert ctx["daily"]["atr14"] == 0.0

    def test_atr14_nonzero_at_15_bars(self):
        df = _make_daily_df(15)
        ctx = build_facts_ctx("BTC", df, latest_price=50.0)
        assert ctx["daily"]["atr14"] > 0

    # --- SMA series ---

    def test_sma_series_length_matches_df(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        daily = ctx["daily"]
        assert len(daily["sma20_series"]) == 20
        assert len(daily["sma50_series"]) == 20
        assert len(daily["sma200_series"]) == 20

    def test_sma20_last_value_not_none_with_20_bars(self):
        df = _make_daily_df(25)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        assert ctx["daily"]["sma20_series"][-1] is not None

    def test_sma200_all_none_with_fewer_than_200_bars(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        assert all(v is None for v in ctx["daily"]["sma200_series"])

    def test_sma_head_values_none_before_period_filled(self):
        df = _make_daily_df(25)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        # First 19 values for SMA20 should be None (not enough history)
        assert ctx["daily"]["sma20_series"][0] is None

    # --- Candle ---

    def test_candle_present_and_keys(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        candle = ctx["daily"]["candle"]
        assert candle is not None
        for key in ("o", "h", "l", "c", "prev_close"):
            assert key in candle, f"Missing candle key: {key}"

    def test_candle_close_matches_last_row(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        assert ctx["daily"]["candle"]["c"] == pytest.approx(float(df["close"].iloc[-1]))

    def test_candle_prev_close_matches_second_last_row(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        assert ctx["daily"]["candle"]["prev_close"] == pytest.approx(
            float(df["close"].iloc[-2])
        )

    def test_candle_none_on_empty_df(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        ctx = build_facts_ctx("BTC", df, latest_price=0.0)
        assert ctx["daily"]["candle"] is None

    # --- Levels merge ---

    def test_levels_merged_from_key_and_ma(self):
        df = _make_daily_df(20)
        key_levels = {"pdh": 110.0, "pdl": 95.0}
        ma_levels = {"ema21": 102.0, "sma200": 99.5}
        ctx = build_facts_ctx("BTC", df, 105.0,
                               key_levels=key_levels, ma_levels=ma_levels)
        assert ctx["levels"]["pdh"] == 110.0
        assert ctx["levels"]["ema21"] == 102.0
        assert ctx["levels"]["sma200"] == 99.5

    def test_levels_none_values_skipped(self):
        df = _make_daily_df(20)
        key_levels = {"pdh": 110.0}
        ma_levels = {"sma200": None, "ema21": 102.0}
        ctx = build_facts_ctx("BTC", df, 105.0,
                               key_levels=key_levels, ma_levels=ma_levels)
        assert "sma200" not in ctx["levels"]
        assert ctx["levels"]["ema21"] == 102.0

    def test_levels_empty_when_none_passed(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, 105.0)
        assert ctx["levels"] == {}

    # --- daily_closes ---

    def test_daily_closes_derived_from_df(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, 105.0)
        assert len(ctx["daily_closes"]) == 20
        assert ctx["daily_closes"][-1] == pytest.approx(float(df["close"].iloc[-1]))

    def test_daily_closes_passed_in_takes_precedence(self):
        df = _make_daily_df(20)
        closes = [1.0, 2.0, 3.0]
        ctx = build_facts_ctx("BTC", df, 105.0, daily_closes=closes)
        assert ctx["daily_closes"] == closes

    # --- pass-through fields ---

    def test_as_of_echoed(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, 105.0, as_of="2024-06-01T00:00:00Z")
        assert ctx["as_of"] == "2024-06-01T00:00:00Z"

    def test_price_set(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, latest_price=42.5)
        assert ctx["price"] == 42.5

    def test_rsi_passed_through(self):
        df = _make_daily_df(20)
        rsi = {"value": 55.0, "zone": "neutral"}
        ctx = build_facts_ctx("BTC", df, 105.0, rsi=rsi)
        assert ctx["daily"]["rsi"] == rsi

    def test_volume_passed_through(self):
        df = _make_daily_df(20)
        vol = {"rvol": 1.2, "signal": "high"}
        ctx = build_facts_ctx("BTC", df, 105.0, volume=vol)
        assert ctx["daily"]["volume"] == vol

    def test_zones_passed_through(self):
        df = _make_daily_df(20)
        zones = {"supports": [90.0], "resistances": [115.0]}
        ctx = build_facts_ctx("BTC", df, 105.0, zones=zones)
        assert ctx["zones"] == zones

    def test_zones_none_by_default(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, 105.0)
        assert ctx["zones"] is None

    def test_monthly_cpr_passed_through(self):
        df = _make_daily_df(20)
        cpr = {"position": "above"}
        ctx = build_facts_ctx("BTC", df, 105.0, monthly_cpr=cpr)
        assert ctx["monthly_cpr"] == cpr

    def test_ohlc_df_in_daily_block(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, 105.0)
        assert ctx["daily"]["ohlc_df"] is not None
        assert len(ctx["daily"]["ohlc_df"]) == 20

    def test_rsi_defaults_to_empty_dict(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, 105.0)
        assert ctx["daily"]["rsi"] == {}

    def test_volume_defaults_to_empty_dict(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, 105.0)
        assert ctx["daily"]["volume"] == {}

    def test_monthly_cpr_defaults_to_empty_dict(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, 105.0)
        assert ctx["monthly_cpr"] == {}

    def test_key_levels_defaults_to_empty_dict(self):
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, 105.0)
        assert ctx["key_levels"] == {}


# ---------------------------------------------------------------------------
# store_asset_facts — Redis integration + error handling
# ---------------------------------------------------------------------------

class TestStoreAssetFacts:
    def _ctx(self, n: int = 20) -> dict:
        df = _make_daily_df(n)
        return build_facts_ctx(
            "BTCUSDT", df, latest_price=105.0,
            as_of="2024-06-01T00:00:00Z",
            key_levels={"pdh": 110.0, "pdl": 95.0},
        )

    def test_returns_bundle_dict(self):
        ctx = self._ctx()
        result = store_asset_facts("BTCUSDT", ctx, r=_FakeRedis())
        assert isinstance(result, dict)

    def test_bundle_top_level_keys_present(self):
        ctx = self._ctx()
        result = store_asset_facts("BTCUSDT", ctx, r=_FakeRedis())
        for key in ("symbol", "as_of", "price", "streaks", "decisive_close",
                    "ma_stack", "candle", "levels", "timeframes"):
            assert key in result, f"Missing bundle key: {key}"

    def test_bundle_symbol_matches(self):
        ctx = self._ctx()
        result = store_asset_facts("BTCUSDT", ctx, r=_FakeRedis())
        assert result["symbol"] == "BTCUSDT"

    def test_redis_set_called_once(self):
        ctx = self._ctx()
        r = _FakeRedis()
        store_asset_facts("BTCUSDT", ctx, r=r)
        assert len(r.calls) == 1

    def test_redis_key_is_facts_bundle_symbol(self):
        ctx = self._ctx()
        r = _FakeRedis()
        store_asset_facts("BTCUSDT", ctx, r=r)
        assert r.calls[0]["key"] == "facts:bundle:BTCUSDT"

    def test_redis_ex_is_3600(self):
        ctx = self._ctx()
        r = _FakeRedis()
        store_asset_facts("BTCUSDT", ctx, r=r)
        assert r.calls[0]["ex"] == 3600

    def test_redis_value_is_json_string(self):
        ctx = self._ctx()
        r = _FakeRedis()
        store_asset_facts("BTCUSDT", ctx, r=r)
        raw = r.calls[0]["value"]
        parsed = json.loads(raw)
        assert parsed["symbol"] == "BTCUSDT"

    def test_redis_json_roundtrips_price(self):
        ctx = self._ctx()
        r = _FakeRedis()
        result = store_asset_facts("BTCUSDT", ctx, r=r)
        parsed = json.loads(r.calls[0]["value"])
        assert parsed["price"] == result["price"]

    def test_returns_none_on_broken_redis(self):
        ctx = self._ctx()
        result = store_asset_facts("BTCUSDT", ctx, r=_BrokenRedis())
        assert result is None

    def test_no_exception_on_broken_redis(self):
        ctx = self._ctx()
        # Must not raise even when Redis fails
        store_asset_facts("BTCUSDT", ctx, r=_BrokenRedis())

    def test_works_without_r_arg_when_redis_unavailable(self, monkeypatch):
        """No r= passed → falls back to get_redis() returning None: no crash."""
        monkeypatch.setattr("carrac.redis_cache.get_redis", lambda: None)
        ctx = self._ctx()
        result = store_asset_facts("BTCUSDT", ctx)
        # With Redis None, bundle is still built and returned
        assert result is not None
        assert result["symbol"] == "BTCUSDT"

    def test_symbol_key_variation(self):
        """Key uses the exact symbol passed in."""
        ctx = self._ctx()
        r = _FakeRedis()
        store_asset_facts("SOLUSDT", ctx, r=r)
        assert r.calls[0]["key"] == "facts:bundle:SOLUSDT"

    def test_nan_price_returns_none_not_malformed_json(self):
        """A NaN price raises in json.dumps(allow_nan=False) → caught → None returned."""
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTCUSDT", df, latest_price=float("nan"))
        r = _FakeRedis()
        result = store_asset_facts("BTCUSDT", ctx, r=r)
        # Must return None (fail-safe) and must NOT have written a malformed set
        assert result is None
        # Redis should not have received a successful set call
        assert len(r.calls) == 0


# ---------------------------------------------------------------------------
# E4 — weekly block in build_facts_ctx
# ---------------------------------------------------------------------------

class TestBuildFactsCtxWeekly:
    def test_no_weekly_key_without_weekly_df(self):
        """Back-compat: omitting weekly_df must not add ctx['weekly']."""
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0)
        assert "weekly" not in ctx

    def test_weekly_key_present_with_weekly_df(self):
        df = _make_daily_df(20)
        wdf = _make_daily_df(15, seed=99)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0, weekly_df=wdf)
        assert "weekly" in ctx

    def test_weekly_block_has_ohlc_df(self):
        df = _make_daily_df(20)
        wdf = _make_daily_df(15, seed=99)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0, weekly_df=wdf)
        weekly = ctx["weekly"]
        assert weekly["ohlc_df"] is not None
        assert len(weekly["ohlc_df"]) == 15

    def test_weekly_block_has_numeric_atr14(self):
        df = _make_daily_df(20)
        wdf = _make_daily_df(20, seed=99)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0, weekly_df=wdf)
        assert isinstance(ctx["weekly"]["atr14"], float)
        assert ctx["weekly"]["atr14"] > 0

    def test_weekly_block_has_sma_series(self):
        df = _make_daily_df(20)
        wdf = _make_daily_df(25, seed=99)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0, weekly_df=wdf)
        weekly = ctx["weekly"]
        assert len(weekly["sma20_series"]) == 25
        assert len(weekly["sma50_series"]) == 25
        assert len(weekly["sma200_series"]) == 25

    def test_weekly_block_has_candle(self):
        df = _make_daily_df(20)
        wdf = _make_daily_df(15, seed=99)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0, weekly_df=wdf)
        candle = ctx["weekly"]["candle"]
        assert candle is not None
        for key in ("o", "h", "l", "c", "prev_close"):
            assert key in candle

    def test_weekly_rsi_passed_through(self):
        df = _make_daily_df(20)
        wdf = _make_daily_df(15, seed=99)
        weekly_rsi = {"value": 62.0, "zone": "neutral", "divergence": None}
        ctx = build_facts_ctx("BTC", df, latest_price=105.0,
                               weekly_df=wdf, weekly_rsi=weekly_rsi)
        assert ctx["weekly"]["rsi"] == weekly_rsi

    def test_weekly_rsi_defaults_to_empty_dict(self):
        df = _make_daily_df(20)
        wdf = _make_daily_df(15, seed=99)
        ctx = build_facts_ctx("BTC", df, latest_price=105.0, weekly_df=wdf)
        assert ctx["weekly"]["rsi"] == {}

    def test_no_weekly_on_empty_df(self):
        """An empty DataFrame weekly_df must not add ctx['weekly']."""
        df = _make_daily_df(20)
        wdf = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        ctx = build_facts_ctx("BTC", df, latest_price=105.0, weekly_df=wdf)
        assert "weekly" not in ctx

    def test_daily_block_unchanged_with_weekly_df(self):
        """Adding weekly_df must not alter the daily block."""
        df = _make_daily_df(20)
        wdf = _make_daily_df(15, seed=99)
        ctx_no_w = build_facts_ctx("BTC", df, latest_price=105.0)
        ctx_w = build_facts_ctx("BTC", df, latest_price=105.0, weekly_df=wdf)
        # daily atr14 must be the same
        assert ctx_no_w["daily"]["atr14"] == ctx_w["daily"]["atr14"]
        assert ctx_no_w["daily"]["sma20_series"] == ctx_w["daily"]["sma20_series"]


# ---------------------------------------------------------------------------
# E4 — end-to-end: build_asset_facts with weekly block
# ---------------------------------------------------------------------------

class TestBuildAssetFactsWeekly:
    def test_timeframes_include_weekly_when_weekly_df_given(self):
        from carrac.facts.bundle import build_asset_facts
        df = _make_daily_df(20)
        wdf = _make_daily_df(20, seed=77)
        ctx = build_facts_ctx("BTCUSDT", df, latest_price=105.0,
                               weekly_df=wdf,
                               as_of="2024-06-01T00:00:00Z")
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert bundle["timeframes"] == ["1D", "1W"]

    def test_weekly_key_in_bundle_when_weekly_df_given(self):
        from carrac.facts.bundle import build_asset_facts
        df = _make_daily_df(20)
        wdf = _make_daily_df(20, seed=77)
        ctx = build_facts_ctx("BTCUSDT", df, latest_price=105.0,
                               weekly_df=wdf,
                               as_of="2024-06-01T00:00:00Z")
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert "weekly" in bundle

    def test_timeframes_daily_only_without_weekly_df(self):
        from carrac.facts.bundle import build_asset_facts
        df = _make_daily_df(20)
        ctx = build_facts_ctx("BTCUSDT", df, latest_price=105.0,
                               as_of="2024-06-01T00:00:00Z")
        bundle = build_asset_facts("BTCUSDT", ctx)
        assert bundle["timeframes"] == ["1D"]
        assert "weekly" not in bundle


# ---------------------------------------------------------------------------
# Fix 2 — closed-candle volume helper: volume_ratio_closed + reliable guard
# ---------------------------------------------------------------------------

class TestClosedCandleVolumeHelper:
    """Tests for morning_engine.candle_utils.volume_ratio_closed as used in
    the fixed precompute facts-bundle path (E5)."""

    _DAY = 86400
    _DAY_MS = 86400 * 1000

    def _make_series(self, values, last_open_ms=None, step_ms=None):
        """Build a list-of-dicts series with time+value keys."""
        step_ms = step_ms or self._DAY_MS
        last_open_ms = last_open_ms or (1_000 * self._DAY_MS)
        n = len(values)
        start = last_open_ms - (n - 1) * step_ms
        return [{"time": start + i * step_ms, "value": float(v)}
                for i, v in enumerate(values)]

    def test_clean_rising_volume_is_reliable(self):
        """Clean daily volume series → reliable=True + sensible rvol."""
        from morning_engine.candle_utils import volume_ratio_closed
        # 20 prior bars at ~1000, last closed bar at 1500 (1.5× avg)
        prior_vols = [1000.0] * 20
        last_closed_vol = 1500.0
        values = prior_vols + [last_closed_vol]
        last_open_ms = 1_000 * self._DAY_MS
        # now_ms = well past the last candle's close time
        now_ms = last_open_ms + 3 * self._DAY_MS
        series = self._make_series(values, last_open_ms=last_open_ms)
        result = volume_ratio_closed(series, self._DAY, now_ms, period=20)
        assert result["reliable"] is True
        assert result["ratio"] is not None
        # median of 20 × 1000 = 1000; ratio = 1500/1000 = 1.5
        assert abs(result["ratio"] - 1.5) < 0.01

    def test_junk_series_mostly_zero_is_unreliable(self):
        """Mostly-zero volume (Yahoo forward-fill artefact) → reliable=False, ratio=None."""
        from morning_engine.candle_utils import volume_ratio_closed
        # 18 near-zero bars, 2 real bars, last closed bar also real
        # 18/20 near-zero > 25% threshold → unreliable
        prior_vols = [0.0] * 18 + [1000.0, 1000.0]
        last_closed_vol = 1000.0
        values = prior_vols + [last_closed_vol]
        last_open_ms = 1_000 * self._DAY_MS
        now_ms = last_open_ms + 3 * self._DAY_MS
        series = self._make_series(values, last_open_ms=last_open_ms)
        result = volume_ratio_closed(series, self._DAY, now_ms, period=20)
        assert result["reliable"] is False

    def test_in_progress_bar_excluded_via_closed_candle_check(self):
        """In-progress (today's) bar is excluded; last CLOSED bar is used."""
        from morning_engine.candle_utils import volume_ratio_closed
        # 20 prior bars at 100, then today's in-progress candle at 5
        prior_vols = [100.0] * 20
        in_progress_vol = 5.0   # would give ~0.05× if used — should be excluded
        values = prior_vols + [in_progress_vol]
        today_open_ms = 1_000 * self._DAY_MS
        now_ms = today_open_ms + 3_600_000  # only 1h into today
        series = self._make_series(values, last_open_ms=today_open_ms)
        result = volume_ratio_closed(series, self._DAY, now_ms, period=20)
        # Should use the 20th bar (index -2), not the in-progress bar
        assert result["candle_complete"] is False
        assert result["used_index"] == len(series) - 2
        # ratio is based on the prior bar (100) vs prior-20 median (100) = 1.0
        assert result["ratio"] is not None
        assert abs(result["ratio"] - 1.0) < 0.01
