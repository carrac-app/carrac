"""GET /api/volume-profile/<symbol> — num_bins param, cache-key isolation,
and response metadata (source_interval / bar_count).

Mirrors the Flask-chart mocking pattern from test_chart_macd_param.py /
test_chart_timing_log.py (test env has empty symbol maps, so the db is
monkeypatched to a deterministic stub — no network, no real DB, no real
Redis: the volume-profile cache getters/setters are monkeypatched to a
plain dict so tests are fully hermetic).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


def _make_mock_df(n=50, base_price=30000):
    """Deterministic OHLCV DataFrame with the columns compute_volume_profile
    expects (incl. taker_buy_base)."""
    dates = pd.date_range(end="2025-01-01", periods=n, freq="15min")
    rng = np.random.default_rng(0)
    close = base_price + np.cumsum(rng.standard_normal(n) * 10)
    volume = rng.uniform(100, 1000, n)
    return pd.DataFrame(
        {
            "open": close - 5,
            "high": close + 10,
            "low": close - 10,
            "close": close,
            "volume": volume,
            "taker_buy_base": volume * 0.5,
        },
        index=dates,
    )


def _make_mock_db(df):
    db = MagicMock()
    db.crypto_map = {"BTC": True}
    db.BINANCE_SYMBOL_MAP = {}
    db.fetch_klines_for_range = MagicMock(return_value=df)
    return db


class _FakeVpCache:
    """Dict-backed stand-in for carrac.caches' Redis-or-local vp cache.

    Lets tests both control cache behavior deterministically and inspect
    which keys were written, without touching Redis or the real in-memory
    SharedTTLCache singleton (which would leak state across tests).
    """

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, data):
        self.store[key] = data


@pytest.fixture
def vp_env(monkeypatch):
    """Patch the volume-profile route's collaborators with deterministic
    stubs: db (crypto path, synthetic df) + a hermetic fake cache."""
    mock_df = _make_mock_df()
    mock_db = _make_mock_db(mock_df)
    fake_cache = _FakeVpCache()

    monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
    monkeypatch.setattr("carrac.charts.routes._get_cached_vp", fake_cache.get)
    monkeypatch.setattr("carrac.charts.routes._set_cached_vp", fake_cache.set)

    return {"df": mock_df, "db": mock_db, "cache": fake_cache}


def _get_json(resp):
    return json.loads(resp.data)


def _vp_url(symbol="BTC", tf="1D", start=1_700_000_000_000, end=1_700_100_000_000, num_bins=None):
    url = f"/api/volume-profile/{symbol}?tf={tf}&start={start}&end={end}"
    if num_bins is not None:
        url += f"&num_bins={num_bins}"
    return url


class TestNumBinsClamping:
    """num_bins is clamped into [20, 500] rather than rejected with 400 —
    matches the tier-gating style of this route (degrade gracefully)."""

    def test_num_bins_above_max_clamped_to_500(self, auth_client, vp_env, monkeypatch):
        captured = {}

        def _fake_compute(df, num_bins=200):
            captured["num_bins"] = num_bins
            return {"bins": [], "poc": 0, "vah": 0, "val": 0}

        monkeypatch.setattr("volume_profile.compute_volume_profile", _fake_compute)

        resp = auth_client.get(_vp_url(num_bins=9999))
        assert resp.status_code == 200, resp.data[:300]
        assert captured["num_bins"] == 500
        assert _get_json(resp)["num_bins"] == 500

    def test_num_bins_below_min_clamped_to_20(self, auth_client, vp_env, monkeypatch):
        captured = {}

        def _fake_compute(df, num_bins=200):
            captured["num_bins"] = num_bins
            return {"bins": [], "poc": 0, "vah": 0, "val": 0}

        monkeypatch.setattr("volume_profile.compute_volume_profile", _fake_compute)

        resp = auth_client.get(_vp_url(num_bins=5))
        assert resp.status_code == 200, resp.data[:300]
        assert captured["num_bins"] == 20
        assert _get_json(resp)["num_bins"] == 20

    def test_num_bins_in_range_passed_through(self, auth_client, vp_env, monkeypatch):
        captured = {}

        def _fake_compute(df, num_bins=200):
            captured["num_bins"] = num_bins
            return {"bins": [], "poc": 0, "vah": 0, "val": 0}

        monkeypatch.setattr("volume_profile.compute_volume_profile", _fake_compute)

        resp = auth_client.get(_vp_url(num_bins=70))
        assert resp.status_code == 200, resp.data[:300]
        assert captured["num_bins"] == 70
        assert _get_json(resp)["num_bins"] == 70

    def test_num_bins_omitted_defaults_to_200(self, auth_client, vp_env, monkeypatch):
        captured = {}

        def _fake_compute(df, num_bins=200):
            captured["num_bins"] = num_bins
            return {"bins": [], "poc": 0, "vah": 0, "val": 0}

        monkeypatch.setattr("volume_profile.compute_volume_profile", _fake_compute)

        resp = auth_client.get(_vp_url())
        assert resp.status_code == 200, resp.data[:300]
        assert captured["num_bins"] == 200
        assert _get_json(resp)["num_bins"] == 200


class TestCacheKeyIsolation:
    """Different num_bins must not cross-serve cached payloads."""

    def test_different_num_bins_each_hit_db_and_dont_cross_serve(self, auth_client, vp_env):
        db = vp_env["db"]

        resp_a = auth_client.get(_vp_url(num_bins=20))
        resp_b = auth_client.get(_vp_url(num_bins=500))

        assert resp_a.status_code == 200, resp_a.data[:300]
        assert resp_b.status_code == 200, resp_b.data[:300]

        body_a = _get_json(resp_a)
        body_b = _get_json(resp_b)
        assert body_a["num_bins"] == 20
        assert body_b["num_bins"] == 500

        # Both requests hit the (mocked) DB fresh — the second num_bins
        # never found a cache entry seeded by the first.
        assert db.fetch_klines_for_range.call_count == 2

        # Two distinct cache entries were written (key includes num_bins).
        assert len(vp_env["cache"].store) == 2
        keys = list(vp_env["cache"].store.keys())
        assert keys[0] != keys[1]
        assert keys[0].endswith(":20")
        assert keys[1].endswith(":500")

    def test_same_num_bins_is_served_from_cache(self, auth_client, vp_env):
        """Sanity check the fake cache actually caches (repeat request, same
        num_bins → second call is a cache hit, no second DB fetch)."""
        db = vp_env["db"]

        resp_1 = auth_client.get(_vp_url(num_bins=100))
        resp_2 = auth_client.get(_vp_url(num_bins=100))

        assert resp_1.status_code == 200
        assert resp_2.status_code == 200
        assert db.fetch_klines_for_range.call_count == 1
        assert _get_json(resp_1) == _get_json(resp_2)


class TestResponseMetadata:
    """source_interval and bar_count must be present (and cached) in the
    response, alongside the num_bins echo."""

    def test_source_interval_and_bar_count_present(self, auth_client, vp_env):
        resp = auth_client.get(_vp_url(tf="1D", num_bins=70))
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)

        assert body["source_interval"] == "15m"  # TF_SOURCE_MAP['1D']
        assert body["bar_count"] == len(vp_env["df"])
        assert body["num_bins"] == 70

    def test_metadata_survives_cache_hit(self, auth_client, vp_env):
        """The metadata is stored IN the cached payload, so a cache-hit
        response still carries source_interval/bar_count."""
        resp_1 = auth_client.get(_vp_url(tf="1D", num_bins=70))
        assert resp_1.status_code == 200

        # Second identical request is served from the fake cache (no second
        # DB fetch) but must carry the same metadata.
        resp_2 = auth_client.get(_vp_url(tf="1D", num_bins=70))
        assert resp_2.status_code == 200
        assert vp_env["db"].fetch_klines_for_range.call_count == 1

        body_2 = _get_json(resp_2)
        assert body_2["source_interval"] == "15m"
        assert body_2["bar_count"] == len(vp_env["df"])
        assert body_2["num_bins"] == 70
