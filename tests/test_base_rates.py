"""Tests for carrac/facts/base_rates.py — Option A, the
per-asset "History says" base-rate line on spotlight cards.

All tests are deterministic and mock the DB cursor / Redis client — no real
DB or Redis is touched. Synthetic data only, same idiom as
tests/test_facts_event_study.py (this module's sibling).
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from carrac.facts.base_rates import (
    _compute_history_says,
    _condition_label,
    _quartile,
    _range_pctile_series,
    _rsi_series,
    get_history_says,
    history_says,
)
from morning_engine.ta_facts import price_percentile


# ──────────────────────────────────────────────────────────────────────────
# Fake Redis stub (same shape as tests/test_facts_cost.py's FakeRedis)
# ──────────────────────────────────────────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self._store: dict[str, str] = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, ex=None):
        self._store[key] = value


# ──────────────────────────────────────────────────────────────────────────
# _rsi_series — PURE
# ──────────────────────────────────────────────────────────────────────────

class TestRsiSeries:

    def test_too_short_returns_all_none(self):
        closes = [100.0 + i for i in range(10)]  # < period + 1 = 15
        assert _rsi_series(closes) == [None] * 10

    def test_strictly_increasing_series_first_value_is_100(self):
        """No losses at all in the seed window -> RSI = 100 (Wilder's
        avg_loss == 0 special case)."""
        closes = [float(i) for i in range(1, 20)]  # 19 values, period 14
        series = _rsi_series(closes)
        assert series[:14] == [None] * 14
        assert series[14] == 100.0

    def test_strictly_decreasing_series_first_value_is_0(self):
        closes = [float(20 - i) for i in range(19)]  # strictly decreasing
        series = _rsi_series(closes)
        assert series[14] == 0.0

    def test_length_matches_input(self):
        closes = [100.0 + (i % 5) for i in range(40)]
        assert len(_rsi_series(closes)) == 40


# ──────────────────────────────────────────────────────────────────────────
# _range_pctile_series — PURE, parity with morning_engine.ta_facts.price_percentile
# ──────────────────────────────────────────────────────────────────────────

class TestRangePctileSeries:

    def test_too_short_returns_all_none(self):
        closes = [1.0, 2.0, 3.0]
        assert _range_pctile_series(closes, window=90) == [None] * 3

    def test_small_window_hand_computed(self):
        # window=3: at i=3, window_vals=[10,20,30] vs latest=25 -> 2 of 3 <= 25 -> 66.67 -> 67
        closes = [10.0, 20.0, 30.0, 25.0]
        result = _range_pctile_series(closes, window=3)
        assert result == [None, None, None, 67]

    def test_latest_excluded_from_its_own_window(self):
        # window=2: at i=2, window_vals = closes[0:2] = [5, 5]; latest=5 -> both <= 5 -> 100
        closes = [5.0, 5.0, 5.0]
        result = _range_pctile_series(closes, window=2)
        assert result == [None, None, 100]

    def test_parity_with_price_percentile_for_latest_value(self):
        """The series' last value must equal calling price_percentile() once
        on the full list — same rule, computed a different way."""
        closes = [100.0 + (i * 0.37 % 13) for i in range(150)]
        series = _range_pctile_series(closes, window=90)
        assert series[-1] == price_percentile(closes, window=90)

    def test_parity_across_multiple_prefixes(self):
        closes = [50.0 + ((i * 7) % 23) for i in range(200)]
        series = _range_pctile_series(closes, window=90)
        for i in (90, 91, 120, 150, 199):
            assert series[i] == price_percentile(closes[: i + 1], window=90)

    def test_clamped_0_to_100(self):
        closes = [float(i) for i in range(95)]  # strictly increasing
        result = _range_pctile_series(closes, window=90)
        # Strictly increasing -> the latest value is the highest ever seen
        # relative to its trailing window -> top percentile (100) throughout.
        assert result[90] == 100
        assert all(v is None or 0 <= v <= 100 for v in result)


# ──────────────────────────────────────────────────────────────────────────
# _quartile — PURE
# ──────────────────────────────────────────────────────────────────────────

class TestQuartile:

    @pytest.mark.parametrize("pct,expected", [
        (0, 1), (24, 1),
        (25, 2), (49, 2),
        (50, 3), (74, 3),
        (75, 4), (100, 4),
    ])
    def test_boundaries(self, pct, expected):
        assert _quartile(pct) == expected

    def test_none_passes_through(self):
        assert _quartile(None) is None


# ──────────────────────────────────────────────────────────────────────────
# _condition_label — PURE
# ──────────────────────────────────────────────────────────────────────────

class TestConditionLabel:

    def test_both_defined_combines_with_plus(self):
        assert _condition_label("oversold", 1) == "RSI oversold + bottom-quartile range"

    def test_quartile_only(self):
        assert _condition_label(None, 4) == "top-quartile range"

    def test_zone_only(self):
        assert _condition_label("overbought", None) == "RSI overbought"

    def test_neither_defined_returns_none(self):
        assert _condition_label(None, None) is None

    def test_neutral_zone_treated_as_undefined(self):
        # _compute_history_says pre-filters 'neutral' to None before calling
        # this, but the label function itself is defensive too.
        assert _condition_label("neutral", 2) == "lower-mid quartile range"

    @pytest.mark.parametrize("q,label", [
        (1, "bottom-quartile range"),
        (2, "lower-mid quartile range"),
        (3, "upper-mid quartile range"),
        (4, "top-quartile range"),
    ])
    def test_all_quartile_labels(self, q, label):
        assert _condition_label(None, q) == label


# ──────────────────────────────────────────────────────────────────────────
# _compute_history_says — PURE orchestration (matching/gating/labeling),
# tested with _rsi_series/_range_pctile_series patched to isolate this
# logic from the sub-computations (which have their own tests above).
# ──────────────────────────────────────────────────────────────────────────

class TestComputeHistorySays:

    def test_too_few_closes_returns_none_without_computing_series(self):
        closes = [100.0] * 5  # < horizon_days(7) + 1
        with patch("carrac.facts.base_rates._rsi_series") as mock_rsi:
            result = _compute_history_says(closes, min_n=1, horizon_days=7)
        assert result is None
        mock_rsi.assert_not_called()

    def test_neither_condition_defined_today_returns_none(self):
        closes = [100.0 + i for i in range(40)]
        with patch("carrac.facts.base_rates._rsi_series", return_value=[50.0] * 40), \
             patch("carrac.facts.base_rates._range_pctile_series", return_value=[None] * 40):
            result = _compute_history_says(closes, min_n=1, horizon_days=7)
        assert result is None

    # NOTE: _range_pctile_series returns RAW PERCENTILES (0..100) — the SUT
    # buckets them into quartiles itself via the real (unpatched) _quartile().
    # These tests mock _range_pctile_series but let _quartile() run for
    # real, so the mocked values below are percentiles, not bucket numbers:
    # 10 -> bottom quartile(1), 30 -> quartile 2, 60 -> quartile 3,
    # 90 -> top quartile(4).

    def test_quartile_only_matches_and_computes_median(self):
        n = 40
        closes = [100.0] * n
        # Clean, non-overlapping match/forward pairs (gap > horizon_days=7).
        for i in (0, 10, 20):
            closes[i] = 100.0
            closes[i + 7] = 110.0  # +10% move
        rsi = [50.0] * n  # all neutral -> today_zone is None
        pct = [60.0] * n  # quartile 3 by default (non-matching)
        for i in (0, 10, 20, n - 1):  # today is index n-1
            pct[i] = 10.0  # bottom quartile
        with patch("carrac.facts.base_rates._rsi_series", return_value=rsi), \
             patch("carrac.facts.base_rates._range_pctile_series", return_value=pct):
            result = _compute_history_says(closes, min_n=3, horizon_days=7)
        assert result == {
            "n": 3, "horizon_days": 7, "median_move_pct": 10.0,
            "condition_label": "bottom-quartile range",
        }

    def test_rsi_only_matches_when_quartile_not_computable(self):
        n = 40
        closes = [100.0] * n
        for i in (0, 10, 20):
            closes[i] = 100.0
            closes[i + 7] = 90.0  # -10% move
        rsi = [50.0] * n
        for i in (0, 10, 20, n - 1):
            rsi[i] = 20.0  # oversold (<30)
        pct = [None] * n  # simulate insufficient depth for percentile
        with patch("carrac.facts.base_rates._rsi_series", return_value=rsi), \
             patch("carrac.facts.base_rates._range_pctile_series", return_value=pct):
            result = _compute_history_says(closes, min_n=3, horizon_days=7)
        assert result == {
            "n": 3, "horizon_days": 7, "median_move_pct": -10.0,
            "condition_label": "RSI oversold",
        }

    def test_combined_condition_requires_both_and_signs_negative_median(self):
        n = 50
        closes = [100.0] * n
        moves = {0: 110.0, 10: 110.0, 20: 80.0, 30: 80.0}   # +10,+10,-20,-20
        for i, fwd in moves.items():
            closes[i] = 100.0
            closes[i + 7] = fwd
        rsi = [50.0] * n     # default: neutral (won't match zone)
        pct = [30.0] * n     # default: quartile 2 (non-matching)
        for i in list(moves.keys()) + [n - 1]:
            rsi[i] = 80.0     # overbought
            pct[i] = 90.0     # top quartile
        # A day that matches ONLY quartile (not rsi) must NOT count.
        rsi[35], pct[35] = 50.0, 90.0
        closes[35], closes[42] = 100.0, 999.0  # would wildly skew median if wrongly counted
        with patch("carrac.facts.base_rates._rsi_series", return_value=rsi), \
             patch("carrac.facts.base_rates._range_pctile_series", return_value=pct):
            result = _compute_history_says(closes, min_n=4, horizon_days=7)
        assert result["n"] == 4
        assert result["median_move_pct"] == -5.0  # median of [-20,-20,10,10]
        assert result["condition_label"] == "RSI overbought + top-quartile range"

    def test_below_min_n_gate_returns_none(self):
        n = 40
        closes = [100.0] * n
        closes[0], closes[7] = 100.0, 110.0
        rsi = [50.0] * n
        pct = [30.0] * n
        pct[0] = 10.0
        pct[n - 1] = 10.0
        with patch("carrac.facts.base_rates._rsi_series", return_value=rsi), \
             patch("carrac.facts.base_rates._range_pctile_series", return_value=pct):
            result = _compute_history_says(closes, min_n=25, horizon_days=7)
        assert result is None

    def test_last_horizon_days_excluded_from_matches(self):
        """A day within the last horizon_days that would otherwise match
        must not count toward n (no forward window exists for it yet)."""
        n = 40
        closes = [100.0] * n
        for i in (0, 10, 20):
            closes[i], closes[i + 7] = 100.0, 110.0
        rsi = [50.0] * n
        pct = [30.0] * n
        for i in (0, 10, 20, n - 1):
            pct[i] = 10.0
        # n - 3 is within the last 7 days (horizon_days=7) and "matches" the
        # condition, but has no closes[i+7] within range -> must be excluded.
        pct[n - 3] = 10.0
        with patch("carrac.facts.base_rates._rsi_series", return_value=rsi), \
             patch("carrac.facts.base_rates._range_pctile_series", return_value=pct):
            result = _compute_history_says(closes, min_n=3, horizon_days=7)
        assert result["n"] == 3  # not 4 — the near-end day never counted

    def test_median_move_pct_is_signed_not_absolute(self):
        n = 40
        closes = [100.0] * n
        for i in (0, 10, 20):
            closes[i], closes[i + 7] = 100.0, 95.0  # -5% each
        rsi = [50.0] * n
        pct = [30.0] * n
        for i in (0, 10, 20, n - 1):
            pct[i] = 10.0
        with patch("carrac.facts.base_rates._rsi_series", return_value=rsi), \
             patch("carrac.facts.base_rates._range_pctile_series", return_value=pct):
            result = _compute_history_says(closes, min_n=3, horizon_days=7)
        assert result["median_move_pct"] == -5.0


# ──────────────────────────────────────────────────────────────────────────
# history_says — DB reader wrapper
# ──────────────────────────────────────────────────────────────────────────

class TestHistorySays:

    def _fake_conn(self, price_rows):
        cursor = MagicMock()
        cursor.fetchall.return_value = price_rows
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    def test_returns_none_for_unrecognized_symbol(self):
        conn = MagicMock()
        result = history_says("NOT_A_REAL_ASSET", conn=conn)
        assert result is None
        conn.cursor.assert_not_called()

    def test_price_query_uses_target_assets_mapping(self):
        """GOLD resolves to (asset='GOLD_F', source='yahoo') via
        quant.engine.config.TARGET_ASSETS — never guessed."""
        conn = self._fake_conn([])
        history_says("GOLD", conn=conn)
        params = conn.cursor.return_value.execute.call_args[0][1]
        assert params[0] == "GOLD_F"
        assert params[1] == "yahoo"

    def test_uses_today_override_in_query(self):
        conn = self._fake_conn([])
        today = date(2026, 7, 2)
        history_says("SPX", conn=conn, today=today)
        params = conn.cursor.return_value.execute.call_args[0][1]
        assert params[2] == today

    def test_delegates_to_pure_compute_with_fetched_closes(self):
        rows = [{"close": 100.0 + i} for i in range(30)]
        conn = self._fake_conn(rows)
        expected = {"n": 30, "horizon_days": 7, "median_move_pct": 1.0, "condition_label": "x"}
        with patch("carrac.facts.base_rates._compute_history_says", return_value=expected) as mock_compute:
            result = history_says("BTC", conn=conn)
        assert result == expected
        called_closes = mock_compute.call_args[0][0]
        assert called_closes == [100.0 + i for i in range(30)]

    def test_non_fatal_on_db_error_with_injected_conn(self):
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB offline")
        assert history_says("BTC", conn=conn) is None

    def test_non_fatal_when_reader_raises(self):
        """conn=None path: get_quant_conn() itself raising is caught (non-fatal)."""
        with patch("carrac.facts.base_rates.get_quant_conn", side_effect=Exception("quant DB down")):
            assert history_says("BTC") is None

    def test_non_fatal_when_compute_raises(self):
        conn = self._fake_conn([{"close": 1.0}])
        with patch("carrac.facts.base_rates._compute_history_says", side_effect=RuntimeError("boom")):
            assert history_says("BTC", conn=conn) is None


# ──────────────────────────────────────────────────────────────────────────
# get_history_says — Redis-cached wrapper
# ──────────────────────────────────────────────────────────────────────────

class TestGetHistorySays:

    def test_cache_hit_returns_cached_without_recompute(self):
        fake = FakeRedis()
        today = date(2026, 7, 2)
        cached = {"n": 40, "horizon_days": 7, "median_move_pct": 1.5, "condition_label": "x"}
        fake.set(f"histsays:BTC:{today.isoformat()}", json.dumps(cached))
        with patch("carrac.facts.base_rates.history_says") as mock_hs:
            result = get_history_says("BTC", today=today, r=fake)
        assert result == cached
        mock_hs.assert_not_called()

    def test_cache_miss_computes_and_stores(self):
        fake = FakeRedis()
        today = date(2026, 7, 2)
        computed = {"n": 30, "horizon_days": 7, "median_move_pct": -2.0, "condition_label": "y"}
        with patch("carrac.facts.base_rates.history_says", return_value=computed) as mock_hs:
            result = get_history_says("ETH", today=today, r=fake)
        assert result == computed
        mock_hs.assert_called_once()
        stored = fake.get(f"histsays:ETH:{today.isoformat()}")
        assert json.loads(stored) == computed

    def test_none_result_is_cached_and_not_recomputed(self):
        fake = FakeRedis()
        today = date(2026, 7, 2)
        with patch("carrac.facts.base_rates.history_says", return_value=None):
            result = get_history_says("SPX", today=today, r=fake)
        assert result is None
        stored = fake.get(f"histsays:SPX:{today.isoformat()}")
        assert json.loads(stored) is None

        with patch("carrac.facts.base_rates.history_says") as mock_hs2:
            result2 = get_history_says("SPX", today=today, r=fake)
        assert result2 is None
        mock_hs2.assert_not_called()

    def test_no_redis_client_falls_through_to_live_compute(self, monkeypatch):
        monkeypatch.setattr("carrac.redis_cache.get_redis", lambda: None)
        with patch("carrac.facts.base_rates.history_says", return_value={"n": 1}) as mock_hs:
            result = get_history_says("BTC")
        assert result == {"n": 1}
        mock_hs.assert_called_once()

    def test_redis_get_error_falls_through_to_compute(self):
        class ErrorGetRedis:
            def get(self, key):
                raise ConnectionError("down")

            def set(self, key, value, ex=None):
                pass

        with patch("carrac.facts.base_rates.history_says", return_value={"n": 2}):
            result = get_history_says("BTC", r=ErrorGetRedis())
        assert result == {"n": 2}

    def test_redis_set_error_is_swallowed(self):
        class ErrorSetRedis:
            def get(self, key):
                return None

            def set(self, key, value, ex=None):
                raise ConnectionError("down")

        with patch("carrac.facts.base_rates.history_says", return_value={"n": 3}):
            result = get_history_says("BTC", r=ErrorSetRedis())
        assert result == {"n": 3}

    def test_cache_key_is_per_symbol_and_date(self):
        fake = FakeRedis()
        d1, d2 = date(2026, 7, 1), date(2026, 7, 2)
        with patch("carrac.facts.base_rates.history_says", return_value={"n": 1}) as mock_hs:
            get_history_says("BTC", today=d1, r=fake)
            get_history_says("BTC", today=d2, r=fake)
        assert mock_hs.call_count == 2  # different dates -> both computed
        assert set(fake._store.keys()) == {"histsays:BTC:2026-07-01", "histsays:BTC:2026-07-02"}
