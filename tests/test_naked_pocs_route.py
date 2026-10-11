"""compute_naked_pocs() unit tests + GET /api/naked-pocs/<symbol> route tests
.

Mirrors the hermetic mocking pattern from tests/test_volume_profile_route.py:
monkeypatched ``_get_db``, synthetic 15m OHLCV(+taker_buy_base) DataFrames,
a dict-backed fake cache standing in for the real SharedTTLCache singleton
(no Redis, no network, no real DB) — plus the ``auth_client`` fixture for
session auth.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from volume_profile import compute_volume_profile, compute_naked_pocs


# ---------------------------------------------------------------------------
# Shared synthetic-data helpers
# ---------------------------------------------------------------------------

def _make_day_df(date_str, n_bars, base_price, rng):
    """One UTC day's worth of 15m OHLCV(+taker_buy_base) bars, tight around
    ``base_price`` so distinct days' POCs don't accidentally overlap."""
    dates = pd.date_range(start=f"{date_str} 00:00", periods=n_bars, freq="15min")
    close = base_price + np.cumsum(rng.standard_normal(n_bars) * 2)
    volume = rng.uniform(100, 1000, n_bars)
    return pd.DataFrame(
        {
            "open": close - 1,
            "high": close + 3,
            "low": close - 3,
            "close": close,
            "volume": volume,
            "taker_buy_base": volume * 0.5,
        },
        index=dates,
    )


def _engineer_touch(df, day_df, touch_i, poc, day_offset_bars):
    """Mutate the bar at ``day_offset_bars + touch_i`` (row position in the
    concatenated ``df``) so its [low, high] range brackets ``poc`` — i.e.
    engineer a guaranteed touch of that POC at that bar."""
    pos = day_offset_bars + touch_i
    df.iloc[pos, df.columns.get_loc("low")] = poc - 1
    df.iloc[pos, df.columns.get_loc("high")] = poc + 1
    return int(df.index[pos].value // 10**6)  # ms epoch of the touch bar


# ---------------------------------------------------------------------------
# Unit tests: compute_naked_pocs() directly (no Flask, no route)
# ---------------------------------------------------------------------------

class TestComputeNakedPocsUnit:
    """3-day synthetic frame: day 1's POC gets touched on day 3, day 2's POC
    stays naked, day 3 is the current partial day (excluded as a session but
    still scanned for touches)."""

    @pytest.fixture
    def three_day_frame(self):
        rng = np.random.default_rng(7)
        day1 = _make_day_df("2026-07-10", 96, 30000, rng)
        day2 = _make_day_df("2026-07-11", 96, 31000, rng)
        day3 = _make_day_df("2026-07-12", 40, 32000, rng)  # partial current day

        day1_poc = compute_volume_profile(day1, num_bins=50)["poc"]
        day2_poc = compute_volume_profile(day2, num_bins=50)["poc"]

        df = pd.concat([day1, day2, day3])
        touch_i = 15
        expected_touch_ts = _engineer_touch(
            df, day3, touch_i, day1_poc, len(day1) + len(day2)
        )

        return {
            "df": df,
            "day1_poc": day1_poc,
            "day2_poc": day2_poc,
            "expected_touch_ts": expected_touch_ts,
        }

    def test_day1_poc_touched_on_day3(self, three_day_frame):
        levels = compute_naked_pocs(three_day_frame["df"], lookback_days=90, num_bins=50)
        by_date = {lv["date"]: lv for lv in levels}

        day1 = by_date["2026-07-10"]
        assert day1["naked"] is False
        assert day1["poc"] == pytest.approx(three_day_frame["day1_poc"])
        assert day1["touch_ts"] == three_day_frame["expected_touch_ts"]
        # Touched 2 days after the session (fixture dates).
        assert day1["age_days"] == 2

    def test_day2_poc_stays_naked(self, three_day_frame):
        levels = compute_naked_pocs(three_day_frame["df"], lookback_days=90, num_bins=50)
        by_date = {lv["date"]: lv for lv in levels}

        day2 = by_date["2026-07-11"]
        assert day2["naked"] is True
        assert day2["poc"] == pytest.approx(three_day_frame["day2_poc"])
        assert day2["touch_ts"] is None
        # Naked as of the last (partial, current) day (fixture dates).
        assert day2["age_days"] == 1

    def test_current_partial_day_excluded_as_session(self, three_day_frame):
        levels = compute_naked_pocs(three_day_frame["df"], lookback_days=90, num_bins=50)
        dates = {lv["date"] for lv in levels}

        assert "2026-07-12" not in dates
        assert dates == {"2026-07-10", "2026-07-11"}

    def test_sorted_newest_first(self, three_day_frame):
        levels = compute_naked_pocs(three_day_frame["df"], lookback_days=90, num_bins=50)
        assert [lv["date"] for lv in levels] == ["2026-07-11", "2026-07-10"]

    def test_single_day_returns_empty(self):
        rng = np.random.default_rng(1)
        only_day = _make_day_df("2026-07-10", 96, 30000, rng)
        assert compute_naked_pocs(only_day, lookback_days=90, num_bins=50) == []

    def test_empty_df_returns_empty(self):
        assert compute_naked_pocs(pd.DataFrame(), lookback_days=90, num_bins=50) == []
        assert compute_naked_pocs(None, lookback_days=90, num_bins=50) == []

    def test_non_datetime_index_returns_empty(self):
        df = pd.DataFrame({"open": [1, 2], "high": [2, 3], "low": [0, 1], "close": [1.5, 2.5],
                            "volume": [10, 10]})
        assert compute_naked_pocs(df, lookback_days=90, num_bins=50) == []


# ---------------------------------------------------------------------------
# Route tests: GET /api/naked-pocs/<symbol>
# ---------------------------------------------------------------------------

def _make_multi_day_frame():
    """4 completed UTC days + 1 partial current day. Day 1's POC is
    engineered to be touched inside day 3; days 2-4 are left naked (their
    price bands are far enough apart that nothing else touches them)."""
    rng = np.random.default_rng(11)
    d1 = _make_day_df("2026-06-01", 96, 30000, rng)
    d2 = _make_day_df("2026-06-02", 96, 31000, rng)
    d3 = _make_day_df("2026-06-03", 96, 32000, rng)
    d4 = _make_day_df("2026-06-04", 96, 33000, rng)
    d5 = _make_day_df("2026-06-05", 30, 34000, rng)  # partial current day

    d1_poc = compute_volume_profile(d1, num_bins=50)["poc"]

    df = pd.concat([d1, d2, d3, d4, d5])
    _engineer_touch(df, d3, 5, d1_poc, len(d1) + len(d2))
    return df


def _make_mock_db(df):
    db = MagicMock()
    db.crypto_map = {"BTC": True}
    db.BINANCE_SYMBOL_MAP = {}
    db.fetch_klines_for_range = MagicMock(return_value=df)
    return db


class _FakeNpocCache:
    """Dict-backed stand-in for the route's SharedTTLCache-backed npoc
    cache — deterministic, hermetic, and inspectable (no Redis / no shared
    in-memory singleton leaking state across tests)."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, data):
        self.store[key] = data


@pytest.fixture
def npoc_env(monkeypatch):
    df = _make_multi_day_frame()
    mock_db = _make_mock_db(df)
    fake_cache = _FakeNpocCache()

    monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
    monkeypatch.setattr("carrac.charts.routes._get_cached_npoc", fake_cache.get)
    monkeypatch.setattr("carrac.charts.routes._set_cached_npoc", fake_cache.set)

    return {"df": df, "db": mock_db, "cache": fake_cache}


def _get_json(resp):
    return json.loads(resp.data)


def _npoc_url(symbol="BTC", lookback=None, include_touched=None):
    url = f"/api/naked-pocs/{symbol}"
    params = []
    if lookback is not None:
        params.append(f"lookback={lookback}")
    if include_touched is not None:
        params.append(f"include_touched={include_touched}")
    if params:
        url += "?" + "&".join(params)
    return url


class TestNakedOnlyVsIncludeTouched:
    def test_naked_only_by_default(self, auth_client, npoc_env):
        resp = auth_client.get(_npoc_url())
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)

        assert all(lv["naked"] for lv in body["levels"])
        # 4 sessions total (days 1-4), 1 touched (day 1) -> 3 naked.
        assert len(body["levels"]) == 3

    def test_include_touched_returns_all(self, auth_client, npoc_env):
        resp = auth_client.get(_npoc_url(include_touched=1))
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)

        assert len(body["levels"]) == 4
        assert any(not lv["naked"] for lv in body["levels"])

    def test_naked_only_response_smaller_than_include_touched(self, auth_client, npoc_env):
        resp_naked = auth_client.get(_npoc_url(lookback=90))
        resp_all = auth_client.get(_npoc_url(lookback=91, include_touched=1))

        naked_levels = _get_json(resp_naked)["levels"]
        all_levels = _get_json(resp_all)["levels"]
        assert len(naked_levels) < len(all_levels)


class TestLookbackClamping:
    def test_lookback_below_min_clamped_to_10(self, auth_client, npoc_env):
        resp = auth_client.get(_npoc_url(lookback=5))
        assert resp.status_code == 200, resp.data[:300]
        assert _get_json(resp)["lookback_days"] == 10

    def test_lookback_above_max_clamped_to_365(self, auth_client, npoc_env):
        resp = auth_client.get(_npoc_url(lookback=9999))
        assert resp.status_code == 200, resp.data[:300]
        assert _get_json(resp)["lookback_days"] == 365

    def test_lookback_in_range_passed_through(self, auth_client, npoc_env):
        resp = auth_client.get(_npoc_url(lookback=45))
        assert resp.status_code == 200, resp.data[:300]
        assert _get_json(resp)["lookback_days"] == 45

    def test_lookback_omitted_defaults_to_90(self, auth_client, npoc_env):
        resp = auth_client.get(_npoc_url())
        assert resp.status_code == 200, resp.data[:300]
        assert _get_json(resp)["lookback_days"] == 90

    def test_response_never_400s_on_bad_lookback(self, auth_client, npoc_env):
        # House style: clamp, never reject with 400.
        resp = auth_client.get(_npoc_url(lookback=-50))
        assert resp.status_code == 200
        assert _get_json(resp)["lookback_days"] == 10


class TestNonCryptoSymbol:
    def test_non_crypto_symbol_returns_unavailable(self, auth_client, npoc_env):
        resp = auth_client.get(_npoc_url(symbol="AAPL"))
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)

        assert body == {"levels": [], "unavailable": True}
        # Never even reached the DB fetch for an unsupported asset class.
        assert npoc_env["db"].fetch_klines_for_range.call_count == 0


class TestCacheIsolation:
    def test_different_lookbacks_each_hit_db(self, auth_client, npoc_env):
        db = npoc_env["db"]

        resp_a = auth_client.get(_npoc_url(lookback=30))
        resp_b = auth_client.get(_npoc_url(lookback=60))

        assert resp_a.status_code == 200, resp_a.data[:300]
        assert resp_b.status_code == 200, resp_b.data[:300]
        assert _get_json(resp_a)["lookback_days"] == 30
        assert _get_json(resp_b)["lookback_days"] == 60

        assert db.fetch_klines_for_range.call_count == 2
        assert len(npoc_env["cache"].store) == 2
        keys = list(npoc_env["cache"].store.keys())
        assert keys[0] != keys[1]
        assert keys[0].split(":")[2] == "30"
        assert keys[1].split(":")[2] == "60"

    def test_same_lookback_is_served_from_cache(self, auth_client, npoc_env):
        db = npoc_env["db"]

        resp_1 = auth_client.get(_npoc_url(lookback=42))
        resp_2 = auth_client.get(_npoc_url(lookback=42))

        assert resp_1.status_code == 200
        assert resp_2.status_code == 200
        assert db.fetch_klines_for_range.call_count == 1
        assert _get_json(resp_1) == _get_json(resp_2)

    def test_include_touched_has_its_own_cache_key(self, auth_client, npoc_env):
        """naked-only and include_touched=1 must not cross-serve each
        other's (differently-sized) payload."""
        db = npoc_env["db"]

        resp_naked = auth_client.get(_npoc_url(lookback=30))
        resp_touched = auth_client.get(_npoc_url(lookback=30, include_touched=1))

        assert db.fetch_klines_for_range.call_count == 2
        assert len(_get_json(resp_naked)["levels"]) != len(_get_json(resp_touched)["levels"])
