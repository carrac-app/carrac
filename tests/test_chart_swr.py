"""Serve-stale-while-revalidate (SWR) for the chart response cache.

Coverage
--------
* Route-level: a stale cache hit (``_get_cached_chart`` returns
  ``(payload, False)``) is served immediately with the SW-mirrored stale
  headers, ``Cache-Control: no-store``, and spawns exactly one background
  refresh. A fresh hit (``(payload, True)``) is unchanged — no stale headers,
  no refresh.
* ``?fresh=1`` bypasses the cache lookup entirely (existing behaviour;
  pinned here since it is also the SWR bypass).
* The tier-gate bypass flag (``_is_tier_bypass_active`` /
  ``_chart_refresh_bypass_tier``) that a background refresh replay sets so it
  isn't re-poisoned by tier gating (no session -> 'free' -> TF rewrite).

Mirrors the Flask-chart mocking pattern from test_chart_timing_log.py /
test_chart_macd_param.py (test env has empty symbol maps, so db + consensio +
indicator calcs are monkeypatched to deterministic stubs). Self-contained.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Mock helpers (copied from test_chart_timing_log.py's Flask chart mocking)
# ---------------------------------------------------------------------------

def _make_mock_df(n=50):
    dates = pd.date_range(end="2025-01-01", periods=n, freq="D")
    rng = np.random.default_rng(0)
    close = 30000 + np.cumsum(rng.standard_normal(n) * 100)
    return pd.DataFrame(
        {
            "open": close - 10,
            "high": close + 20,
            "low": close - 20,
            "close": close,
            "volume": rng.uniform(100, 1000, n),
        },
        index=dates,
    )


def _make_mock_db(mock_df, fake_tf_data):
    db = MagicMock()
    db.is_curated.return_value = True
    db.validate_symbol.return_value = None
    db._is_crypto.return_value = True
    db.get_multi_timeframe_data.return_value = fake_tf_data
    db.get_ltf_multi_timeframe_data.return_value = {
        "4h": mock_df, "1h": mock_df, "2h": mock_df,
    }
    # Sub-hourly path (1m/3m/5m/15m/30m) — needed by the tier-bypass test,
    # which requests '5m' specifically to prove it survives un-rewritten.
    db.fetch_klines_for_chart.return_value = mock_df
    db.get_dynamic_ohlcv.return_value = mock_df
    db.binance_map = {"BTC": True}
    db.stock_map = {}
    db.etf_map = {}
    db.hyperliquid = MagicMock()
    db.hyperliquid.is_supported.return_value = False
    db.ccxt = MagicMock()
    db.ccxt.is_supported.return_value = False
    return db


def _make_mock_consensio():
    consensio = MagicMock()
    consensio.calculate_multi_timeframe.return_value = {
        "1D": {"position": 1, "signal": 1},
        "3D": {"position": 0, "signal": 0},
        "1W": {"position": -1, "signal": -1},
        "total": {"positions": 0},
    }
    return consensio


def _make_cached_payload(age_s=500):
    """A minimal cached-chart dict, stamped as if written `age_s` ago."""
    return {
        "symbol": "BTC",
        "timeframe": "1D",
        "ohlcv": [],
        "volume": [],
        "rsi": [],
        "markers": [],
        "priceLines": [],
        "indicators": {},
        "cpr": None,
        "weekly_cpr": None,
        "daily_cpr": None,
        "tier": "curated",
        "live_ws": False,
        "ws_venue": None,
        "_cached_at": int(time.time()) - age_s,
    }


@pytest.fixture
def _db_env(monkeypatch):
    """Stub just _get_db — the handler calls db.is_curated() before the cache
    lookup (validate_symbol moved BELOW it, so a warm cache no
    longer needs validation), and a hit/stale-serve test still needs the
    curated check stubbed."""
    mock_df = _make_mock_df()
    fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
    mock_db = _make_mock_db(mock_df, fake_tf_data)
    monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
    return mock_db


@pytest.fixture
def _chart_env(monkeypatch):
    """Patch the chart route's collaborators with deterministic stubs
    (miss/rebuild path — used by the tier-bypass and fresh=1 tests, which
    need the request to fall all the way through to a full compute)."""
    mock_df = _make_mock_df()
    fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
    mock_db = _make_mock_db(mock_df, fake_tf_data)
    mock_consensio = _make_mock_consensio()
    mock_rsi = MagicMock()
    mock_rsi.calculate_series.return_value = None
    mock_cpr = MagicMock()
    mock_cpr.calculate_monthly_cpr.return_value = None

    monkeypatch.setattr("carrac.charts.routes._set_cached_chart", lambda *a, **kw: None)
    monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
    monkeypatch.setattr("carrac.charts.routes._get_consensio", lambda: mock_consensio)
    monkeypatch.setattr("carrac.charts.routes._get_rsi_calc", lambda: mock_rsi)
    monkeypatch.setattr("carrac.charts.routes._get_cpr_calc", lambda: mock_cpr)
    monkeypatch.setattr(
        "carrac.charts.routes.compute_session_data", lambda *a, **kw: None, raising=False
    )
    # Force a miss on the REAL (process-wide, not per-test) sub-hourly cache —
    # another test file (e.g. test_caches.py's TestSubhourlyCache) may have
    # already populated 'BTC:5m' there with an incompatible DataFrame, which
    # would short-circuit db.fetch_klines_for_chart and break the '5m' test.
    monkeypatch.setattr("carrac.charts.routes._get_cached_subhourly", lambda *a, **kw: None)
    monkeypatch.setattr("carrac.charts.routes._set_cached_subhourly", lambda *a, **kw: None)
    return mock_df


@pytest.fixture(autouse=True)
def _reset_refresh_state(monkeypatch):
    """Isolate the module-level SWR refresh bookkeeping between tests.

    Without this, the in-process single-flight fallback (`_chart_refresh_last`)
    would remember a key from an earlier test and block a same-key refresh
    spawn for _CHART_REFRESH_GUARD_S (30s) — flaky under a fast test run.
    Also forces Redis out of the single-flight lock path so the fallback
    (deterministic, no live Redis needed) is what's under test.
    """
    monkeypatch.setattr("carrac.charts.routes._chart_refresh_last", {})
    monkeypatch.setattr("carrac.redis_cache.get_redis", lambda: None)


# ---------------------------------------------------------------------------
# Stale vs fresh hit — headers, Cache-Control, refresh spawn
# ---------------------------------------------------------------------------


class TestStaleServe:
    def test_stale_hit_serves_with_headers_and_spawns_refresh_once(self, auth_client, monkeypatch, _db_env):
        payload = _make_cached_payload(age_s=500)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: (payload, False))
        mock_pool = MagicMock()
        monkeypatch.setattr("carrac.charts.routes._CHART_REFRESH_POOL", mock_pool)

        resp = auth_client.get("/api/chart/BTC?timeframe=1D")

        assert resp.status_code == 200, resp.data[:300]
        assert resp.headers.get("X-Carrac-Stale") == "1"
        # Same truncating-baseline boundary as the parity test: `_cached_at` is
        # `int(time.time()) - age_s` fixed at build time, and the route recomputes
        # the age at request time, so a whole-second crossing yields 501. Latent
        # here (one request, not two) but the identical defect.
        assert int(resp.headers["X-Carrac-Age"]) in (500, 501)
        assert resp.headers.get("Cache-Control") == "no-store"
        assert mock_pool.submit.call_count == 1

    def test_fresh_hit_has_no_stale_headers_and_no_refresh(self, auth_client, monkeypatch, _db_env):
        payload = _make_cached_payload(age_s=5)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: (payload, True))
        mock_pool = MagicMock()
        monkeypatch.setattr("carrac.charts.routes._CHART_REFRESH_POOL", mock_pool)

        resp = auth_client.get("/api/chart/BTC?timeframe=1D")

        assert resp.status_code == 200, resp.data[:300]
        assert "X-Carrac-Stale" not in resp.headers
        assert "X-Carrac-Age" not in resp.headers
        assert resp.headers.get("Cache-Control", "").startswith("private")
        assert mock_pool.submit.call_count == 0

    def test_stale_hit_refresh_spawned_exactly_once_for_repeat_requests(self, auth_client, monkeypatch, _db_env):
        """Two stale hits for the SAME key in quick succession must spawn at
        most one refresh — the single-flight guard (in-process fallback here,
        since Redis is patched out) de-dupes the second."""
        payload = _make_cached_payload(age_s=500)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: (payload, False))
        mock_pool = MagicMock()
        monkeypatch.setattr("carrac.charts.routes._CHART_REFRESH_POOL", mock_pool)

        auth_client.get("/api/chart/BTC?timeframe=1D")
        auth_client.get("/api/chart/BTC?timeframe=1D")

        assert mock_pool.submit.call_count == 1


class TestFreshParamBypassesStaleServing:
    def test_fresh_1_bypasses_cache_lookup_entirely(self, auth_client, monkeypatch, _chart_env):
        mock_get_cached = MagicMock(return_value=(_make_cached_payload(), False))
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", mock_get_cached)
        mock_pool = MagicMock()
        monkeypatch.setattr("carrac.charts.routes._CHART_REFRESH_POOL", mock_pool)

        resp = auth_client.get("/api/chart/BTC?timeframe=1D&fresh=1")

        assert resp.status_code == 200, resp.data[:300]
        mock_get_cached.assert_not_called()
        assert "X-Carrac-Stale" not in resp.headers
        assert mock_pool.submit.call_count == 0


# ---------------------------------------------------------------------------
# Tier-gate bypass flag (background refresh replay must not be re-gated)
# ---------------------------------------------------------------------------


class TestBackgroundRefreshEndToEnd:
    def test_run_chart_refresh_rebuilds_and_releases_lock(self, app, monkeypatch, _chart_env):
        """Drive _run_chart_refresh synchronously (bypassing the pool) end to
        end: it must replay into get_chart_data under the tier bypass, land a
        fresh _set_cached_chart write, and leave the bypass flag cleared."""
        from carrac.charts.routes import (
            _run_chart_refresh, _is_tier_bypass_active, _build_chart_refresh_query,
        )

        mock_set_cached = MagicMock()
        monkeypatch.setattr("carrac.charts.routes._set_cached_chart", mock_set_cached)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: None)

        query = _build_chart_refresh_query("1D", "htf", False, False, "", None)
        _run_chart_refresh(app, "BTC", query, "BTC:1D:0")

        assert mock_set_cached.call_count == 1
        call_symbol, call_tf, call_lite, _payload = mock_set_cached.call_args[0]
        assert call_symbol == "BTC"
        assert call_tf.startswith("1D")
        assert call_lite is False
        assert _is_tier_bypass_active() is False  # cleared in the `finally`


class TestTierBypassFlag:
    def test_flag_defaults_false_and_is_settable(self):
        from carrac.charts.routes import _is_tier_bypass_active, _chart_refresh_bypass_tier

        assert _is_tier_bypass_active() is False
        _chart_refresh_bypass_tier.active = True
        try:
            assert _is_tier_bypass_active() is True
        finally:
            _chart_refresh_bypass_tier.active = False
        assert _is_tier_bypass_active() is False

    def test_bypass_flag_skips_blocked_tf_rewrite(self, auth_client, monkeypatch, _chart_env):
        """Without the bypass, a tier that blocks '5m' gets rewritten to
        '4h' (existing gating behaviour). With the bypass active (as a
        background refresh replay sets it), the same blocked TF must survive
        unrewritten — otherwise the replay would rebuild under the WRONG
        cache key and never satisfy the original stale-serving request."""
        import carrac.auth.decorators as auth_decorators
        from carrac.charts.routes import _chart_refresh_bypass_tier

        monkeypatch.setattr(auth_decorators, "get_request_tier", lambda: "free")
        monkeypatch.setattr(
            auth_decorators, "get_tier_limits",
            lambda tier="free": {"blocked_tfs": frozenset({"5m"}), "derivatives": False},
        )
        mock_get_cached = MagicMock(return_value=None)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", mock_get_cached)

        # 1) No bypass -> rewritten to 4h.
        resp = auth_client.get("/api/chart/BTC?timeframe=5m")
        assert resp.status_code == 200, resp.data[:300]
        used_tf = mock_get_cached.call_args[0][1]
        assert used_tf.startswith("4h"), f"expected blocked TF rewritten to 4h, got key tf={used_tf}"

        # 2) Bypass active (as the background refresh runner sets it) -> NOT rewritten.
        mock_get_cached.reset_mock()
        _chart_refresh_bypass_tier.active = True
        try:
            resp = auth_client.get("/api/chart/BTC?timeframe=5m")
        finally:
            _chart_refresh_bypass_tier.active = False
        assert resp.status_code == 200, resp.data[:300]
        used_tf = mock_get_cached.call_args[0][1]
        assert used_tf.startswith("5m"), f"expected bypass to preserve '5m', got key tf={used_tf}"


class TestTimeframeCaseCanonicalisation:
    """'1H' (keyboard shortcuts) and '1h' (TF lists) both occur in
    real traffic; unnormalised they fragment the cache per spelling, break the
    per-TF TTL lookup, and make the warm job (lowercase keys)
    invisible to uppercase readers. '1m' (minute) vs '1M' (month) differ only
    by case and must never be rewritten."""

    def _capture_lookup_tf(self, auth_client, monkeypatch, tf_param):
        seen = {}

        def mock_get_cached(symbol, tf, lite):
            seen["tf"] = tf
            return ({"candles": [], "_cached_at": int(time.time())}, True)

        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", mock_get_cached)
        resp = auth_client.get(f"/api/chart/BTC?timeframe={tf_param}")
        assert resp.status_code == 200, resp.data[:300]
        return seen["tf"]

    def test_uppercase_1H_lands_on_lowercase_cache_key(self, auth_client, monkeypatch, _db_env):
        tf = self._capture_lookup_tf(auth_client, monkeypatch, "1H")
        assert tf.split(":")[0] == "1h"

    def test_uppercase_4H_lands_on_lowercase_cache_key(self, auth_client, monkeypatch, _db_env):
        tf = self._capture_lookup_tf(auth_client, monkeypatch, "4H")
        assert tf.split(":")[0] == "4h"

    def test_lowercase_1d_lands_on_canonical_1D(self, auth_client, monkeypatch, _db_env):
        tf = self._capture_lookup_tf(auth_client, monkeypatch, "1d")
        assert tf.split(":")[0] == "1D"

    def test_minute_and_month_pass_through_untouched(self, auth_client, monkeypatch, _db_env):
        assert self._capture_lookup_tf(auth_client, monkeypatch, "1m").split(":")[0] == "1m"
        assert self._capture_lookup_tf(auth_client, monkeypatch, "1M").split(":")[0] == "1M"


# ---------------------------------------------------------------------------
# Cold-build verdict — miss vs preload vs refresh
# ---------------------------------------------------------------------------


class TestColdBuildVerdict:
    """A cold build only counts as a blocking ``miss`` when someone waited on
    it. The frontend's background timeframe preload sends
    ``X-Carrac-Preload: 1`` and must log as ``preload`` and stay OUT of the
    miss ledger — untagged it was 37% of the ledger the warm tier is sized
    from. The worker/SWR replay (tier-bypass flag) stays ``refresh``."""

    def _force_miss(self, monkeypatch):
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: None)
        ledger = MagicMock()
        monkeypatch.setattr("carrac.charts.routes._record_chart_miss", ledger)
        return ledger

    def test_untagged_cold_build_is_a_miss_and_hits_the_ledger(self, auth_client, monkeypatch, _chart_env, caplog):
        ledger = self._force_miss(monkeypatch)
        with caplog.at_level("INFO", logger="carrac.charts"):
            resp = auth_client.get("/api/chart/BTC?timeframe=1D&fmt=col")
        assert resp.status_code == 200, resp.data[:300]
        assert any(" miss " in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records]
        ledger.assert_called_once()
        assert ledger.call_args[0][0] == "BTC"

    def test_preload_header_logs_preload_and_skips_the_ledger(self, auth_client, monkeypatch, _chart_env, caplog):
        ledger = self._force_miss(monkeypatch)
        with caplog.at_level("INFO", logger="carrac.charts"):
            resp = auth_client.get(
                "/api/chart/BTC?timeframe=1D&fmt=col", headers={"X-Carrac-Preload": "1"},
            )
        assert resp.status_code == 200, resp.data[:300]
        msgs = [r.getMessage() for r in caplog.records]
        assert any(" preload " in m for m in msgs), msgs
        assert not any(" miss " in m for m in msgs), msgs
        ledger.assert_not_called()

    def test_preload_header_must_be_exactly_1(self, auth_client, monkeypatch, _chart_env, caplog):
        ledger = self._force_miss(monkeypatch)
        with caplog.at_level("INFO", logger="carrac.charts"):
            auth_client.get("/api/chart/BTC?timeframe=1D&fmt=col", headers={"X-Carrac-Preload": "yes"})
        assert any(" miss " in r.getMessage() for r in caplog.records)
        ledger.assert_called_once()

    def test_front_door_forwards_the_preload_hint(self):
        # The FastAPI front door replays into Flask with an allow-list of
        # headers; the hint has to be on it or every preload counts as a miss.
        from carrac.fastapi_app.routers import chart_public
        assert "X-Carrac-Preload" in chart_public._FORWARD_HINT_HEADERS
        assert "X-Carrac-Preload" not in chart_public._FORWARD_AUTH_HEADERS
