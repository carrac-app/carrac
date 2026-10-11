"""FastAPI chart front door: concurrency, limiter exemption,
session-transplant unit coverage, SWR spawn survival, kill switch.

See tests/parity/test_chart_public_parity.py for Flask/FastAPI response
parity coverage (headers, body, auth, tier gating, param passthrough).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import httpx
import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport

SCREENSHOT_KEY = "test-api-key-for-testing"  # forced in tests/conftest.py
API_KEY_HEADERS = {"X-Internal-Key": SCREENSHOT_KEY}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Mock helpers (mirrors tests/parity/test_chart_public_parity.py)
# ---------------------------------------------------------------------------

def _make_mock_df(n=120):
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


def _make_mock_db(mock_df, fake_tf_data, *, slow_multi_tf=False, sleep_s=0.5):
    db = MagicMock()
    db.is_curated.return_value = True
    db.validate_symbol.return_value = None
    db._is_crypto.return_value = True
    if slow_multi_tf:
        def _slow_get_multi_timeframe_data(*_a, **_kw):
            time.sleep(sleep_s)
            return fake_tf_data
        db.get_multi_timeframe_data.side_effect = _slow_get_multi_timeframe_data
    else:
        db.get_multi_timeframe_data.return_value = fake_tf_data
    db.get_ltf_multi_timeframe_data.return_value = {
        "4h": mock_df, "1h": mock_df, "2h": mock_df,
    }
    db.get_dynamic_ohlcv.return_value = mock_df
    db.fetch_klines_for_chart.return_value = mock_df
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


def _make_cached_payload(age_s=5, symbol="BTC"):
    return {
        "symbol": symbol, "timeframe": "1D", "ohlcv": [], "volume": [], "rsi": [],
        "markers": [], "priceLines": [], "indicators": {}, "cpr": None,
        "weekly_cpr": None, "daily_cpr": None, "tier": "curated",
        "live_ws": False, "ws_venue": None,
        "_cached_at": int(time.time()) - age_s,
    }


@pytest.fixture(autouse=True)
def _reset_refresh_state(monkeypatch):
    monkeypatch.setattr("carrac.charts.routes._chart_refresh_last", {})
    monkeypatch.setattr("carrac.redis_cache.get_redis", lambda: None)


# ---------------------------------------------------------------------------
# Router import smoke test
# ---------------------------------------------------------------------------


def test_chart_public_router_imports_cleanly():
    from carrac.fastapi_app.routers import chart_public  # noqa: F401
    assert any(r.path == "/api/chart/{symbol}" for r in chart_public.router.routes)


# ---------------------------------------------------------------------------
# Concurrency proof — N=4 simultaneous requests complete in ~0.5-0.7s, not ~2s
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_four_concurrent_requests_overlap(self, asgi_app, monkeypatch):
        """A 0.5s-per-call chart build must NOT serialise 4 concurrent callers
        to ~2s — asyncio.to_thread runs them on independent OS threads, off
        the asgiref single-thread executor that would otherwise serialise
        every Flask request in the worker."""
        mock_df = _make_mock_df()
        fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
        mock_db = _make_mock_db(mock_df, fake_tf_data, slow_multi_tf=True, sleep_s=0.5)

        # Count how many chart builds are in flight at once. Overlap is what
        # the test is about; wall-clock is not (it inflates under CPU load).
        import threading
        lock = threading.Lock()
        state = {"active": 0, "peak": 0}
        slow_build = mock_db.get_multi_timeframe_data.side_effect

        def _tracked(*a, **kw):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                return slow_build(*a, **kw)
            finally:
                with lock:
                    state["active"] -= 1

        mock_db.get_multi_timeframe_data.side_effect = _tracked
        mock_consensio = _make_mock_consensio()
        mock_rsi = MagicMock()
        mock_rsi.calculate_series.return_value = None
        mock_cpr = MagicMock()
        mock_cpr.calculate_monthly_cpr.return_value = None

        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: None)
        monkeypatch.setattr("carrac.charts.routes._set_cached_chart", lambda *a, **kw: None)
        monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
        monkeypatch.setattr("carrac.charts.routes._get_consensio", lambda: mock_consensio)
        monkeypatch.setattr("carrac.charts.routes._get_rsi_calc", lambda: mock_rsi)
        monkeypatch.setattr("carrac.charts.routes._get_cpr_calc", lambda: mock_cpr)
        monkeypatch.setattr(
            "carrac.charts.routes.compute_session_data", lambda *a, **kw: None, raising=False
        )
        monkeypatch.setattr("carrac.charts.routes._get_cached_subhourly", lambda *a, **kw: None)
        monkeypatch.setattr("carrac.charts.routes._set_cached_subhourly", lambda *a, **kw: None)

        async def _go():
            transport = ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                t0 = time.monotonic()
                results = await asyncio.gather(*[
                    client.get("/api/chart/BTC?timeframe=1D", headers=API_KEY_HEADERS)
                    for _ in range(4)
                ])
                elapsed = time.monotonic() - t0
                return results, elapsed

        results, elapsed = _run(_go())

        for r in results:
            assert r.status_code == 200, r.text[:300]
        # Serialised (asgiref single-thread executor): never more than one
        # build in flight. Concurrent (asyncio.to_thread): several overlap.
        assert state["peak"] >= 2, (
            f"chart builds never overlapped (peak in flight: {state['peak']}) "
            f"— the 4 requests were serialised"
        )


# ---------------------------------------------------------------------------
# Inner Flask limiter exemption. Uses an ISOLATED Flask app + a fresh
# flask-limiter Limiter (same isolation principle tests/fastapi/
# test_rate_limit.py already uses for the FastAPI side: "avoid bleeding
# limits across the production module-level singleton") wired with the SAME
# exempt_when=_is_asgi_replay_active callback carrac/charts/routes.py's real
# /api/chart/<symbol> route uses — so this exercises the actual production
# callback, just without depending on the shared carrac.asgi.flask_app
# limiter's cross-test global storage state (which multiple create_app()
# calls across the session rebind).
# ---------------------------------------------------------------------------


class TestInnerLimiterExemption:
    @staticmethod
    def _make_isolated_app():
        from flask import Flask, jsonify
        from flask_limiter import Limiter
        from flask_limiter.util import get_remote_address

        from carrac.charts.routes import _is_asgi_replay_active

        probe_app = Flask(__name__)
        probe_limiter = Limiter(app=probe_app, key_func=get_remote_address, storage_uri="memory://")

        @probe_app.route("/probe")
        @probe_limiter.limit("5 per minute", exempt_when=_is_asgi_replay_active)
        def probe():
            return jsonify({"ok": True})

        return probe_app

    def test_control_429_without_replay_flag(self):
        """Control: the REAL _is_asgi_replay_active callback, wired the same
        way as the production route, DOES let flask-limiter 429 a caller
        once the flag is not set — proves the isolated harness enforces a
        real limit (not a no-op), so the treatment test below is meaningful."""
        client = self._make_isolated_app().test_client()
        statuses = [client.get("/probe").status_code for _ in range(8)]
        assert 429 in statuses, f"expected a 429 among 8 hits of a 5/minute limit, got {statuses}"

    def test_exempt_when_replay_flag_active(self):
        """Treatment: with _chart_via_asgi_replay.active set (exactly as the
        FastAPI front door sets it around its replayed call), the SAME
        limiter/route never 429s even far past the nominal limit."""
        from carrac.charts.routes import _chart_via_asgi_replay

        client = self._make_isolated_app().test_client()
        _chart_via_asgi_replay.active = True
        try:
            statuses = [client.get("/probe").status_code for _ in range(8)]
        finally:
            _chart_via_asgi_replay.active = False

        assert 429 not in statuses, statuses
        assert all(s == 200 for s in statuses), statuses


# ---------------------------------------------------------------------------
# SWR background spawn still fires from the migrated path
# ---------------------------------------------------------------------------


class TestSWRSpawnSurvives:
    def test_stale_hit_via_front_door_spawns_refresh(self, asgi_client, monkeypatch):
        mock_df = _make_mock_df()
        fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
        mock_db = _make_mock_db(mock_df, fake_tf_data)
        monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)

        payload = _make_cached_payload(age_s=500)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: (payload, False))
        mock_pool = MagicMock()
        monkeypatch.setattr("carrac.charts.routes._CHART_REFRESH_POOL", mock_pool)

        resp = asgi_client.get("/api/chart/BTC?timeframe=1D", headers=API_KEY_HEADERS)

        assert resp.status_code == 200, resp.text[:300]
        assert resp.headers.get("X-Carrac-Stale") == "1"
        assert mock_pool.submit.call_count == 1


# ---------------------------------------------------------------------------
# Session-identity transplant — unit test (no Redis needed: calls the
# router's own replay helper directly rather than going through the FastAPI
# HTTP layer, which can't simulate a real session cookie in this test env —
# see tests/parity/test_chart_public_parity.py's module docstring).
# ---------------------------------------------------------------------------


class TestSessionIdentityTransplant:
    def test_session_identity_sets_flask_session_and_tier_gates_normally(self, monkeypatch):
        """identity_source='session' must land user_id + tier in the
        REPLAYED Flask session so get_request_tier()/get_tier_limits() see
        the real caller — verified here by forcing a blocked TF and
        confirming the rewrite fires (tier gating applies NORMALLY, not
        bypassed like the SWR replay)."""
        import carrac.auth.decorators as auth_decorators
        from carrac.fastapi_app.routers.chart_public import _replay_flask_chart

        mock_df = _make_mock_df()
        fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
        mock_db = _make_mock_db(mock_df, fake_tf_data)
        monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
        mock_get_cached = MagicMock(return_value=None)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", mock_get_cached)

        # Real per-user tier limits code path (not the SWR bypass): a 'free'
        # tier with '5m' blocked must see the TF rewritten to '4h'.
        real_get_tier_limits = auth_decorators.get_tier_limits

        def _blocked_5m(tier="free"):
            limits = dict(real_get_tier_limits(tier))
            limits["blocked_tfs"] = frozenset({"5m"})
            return limits

        monkeypatch.setattr(auth_decorators, "get_tier_limits", _blocked_5m)

        status_code, body, headers = _replay_flask_chart(
            symbol="BTC",
            query_string="timeframe=5m",
            forwarded_headers={},
            identity_source="session",
            user_id=4242,
            tier="free",
        )

        assert status_code == 200, body[:300]
        used_tf = mock_get_cached.call_args[0][1]
        assert used_tf.startswith("4h"), (
            f"expected the transplanted session (user_id=4242, tier=free) to "
            f"trigger the REAL blocked-TF rewrite, got key tf={used_tf}"
        )

    def test_api_key_identity_sets_g_rate_tier_not_session(self, monkeypatch):
        """identity_source='api_key' must NOT write user_id into the replayed
        Flask session (production API-key traffic never has one either —
        writing it would erroneously apply per-user tier overrides real
        API-key callers never get). Instead it sets flask.g.rate_tier, which
        get_request_tier() reads via its second branch
        (session.get('user_id') is falsy -> hasattr(g, 'rate_tier') -> True).
        Verified by capturing get_request_tier()'s REAL return value for a
        non-default tier ('member') — that value can only have come from
        g.rate_tier, since the session is empty and the header-based legacy
        fallback always returns the hard-coded string 'admin'."""
        import carrac.auth.decorators as auth_decorators
        from carrac.fastapi_app.routers.chart_public import _replay_flask_chart

        mock_df = _make_mock_df()
        fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
        mock_db = _make_mock_db(mock_df, fake_tf_data)
        monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
        payload = _make_cached_payload(age_s=5)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: (payload, True))
        monkeypatch.setattr("carrac.formatters.maybe_compact", lambda p, _fn: p, raising=False)

        real_get_request_tier = auth_decorators.get_request_tier
        captured: dict = {}

        def _spy_get_request_tier():
            result = real_get_request_tier()
            captured["tier"] = result
            return result

        monkeypatch.setattr(auth_decorators, "get_request_tier", _spy_get_request_tier)

        # timeframe= must be present so get_chart_data actually calls
        # get_request_tier() (it's only consulted for TF/derivatives gating).
        # forwarded_headers carries the API key — this is what lets the
        # REPLAYED Flask view's own auth_or_key_required pass for the
        # api_key identity source (unlike the session source, which needs
        # no header at all since the session itself carries the identity).
        status_code, body, _headers = _replay_flask_chart(
            symbol="BTC",
            query_string="timeframe=1D",
            forwarded_headers=dict(API_KEY_HEADERS),
            identity_source="api_key",
            user_id=None,
            tier="member",
        )

        assert status_code == 200, body[:300]
        assert captured.get("tier") == "member", (
            "expected get_request_tier() to resolve 'member' via the "
            "transplanted flask.g.rate_tier — got "
            f"{captured.get('tier')!r} (session or legacy-'admin' fallback?)"
        )


# ---------------------------------------------------------------------------
# Kill switch — CHART_ASGI_ENABLED gates MIGRATED_READ_PREFIXES at import
# ---------------------------------------------------------------------------


class TestKillSwitch:
    def test_enabled_by_default(self):
        from carrac.asgi import CHART_ASGI_ENABLED, MIGRATED_READ_PREFIXES

        assert CHART_ASGI_ENABLED is True
        assert MIGRATED_READ_PREFIXES == (("/api/chart/", frozenset({"GET"})),)

    def test_disabled_env_yields_empty_prefixes_and_falls_through_to_flask(self, monkeypatch):
        """Reload carrac.asgi with CHART_ASGI_ENABLED=0 — MIGRATED_READ_PREFIXES
        must be empty and GET /api/chart/<symbol> must be served by the Flask
        Mount catch-all (no FastAPI route in the dispatch list matches it),
        exactly reverting to the original behaviour. Reloads back to the
        default afterwards so later tests in this session see the normal
        (enabled) module state regardless of pass/fail."""
        import importlib
        import carrac.asgi as asgi_mod

        monkeypatch.setenv("CHART_ASGI_ENABLED", "0")
        try:
            importlib.reload(asgi_mod)
            assert asgi_mod.CHART_ASGI_ENABLED is False
            assert asgi_mod.MIGRATED_READ_PREFIXES == ()

            # No route in the strangler dispatch list should now claim
            # GET /api/chart/<symbol> for the FastAPI sub-app.
            for route in asgi_mod.asgi_app.routes:
                prefix = getattr(route, "prefix", None)
                if prefix == "/api/chart/":
                    methods = getattr(route, "methods", None)
                    assert methods is None or "GET" not in methods, (
                        "a GET-matching /api/chart/ PrefixPath survived the "
                        "kill switch being off"
                    )
        finally:
            monkeypatch.delenv("CHART_ASGI_ENABLED", raising=False)
            importlib.reload(asgi_mod)
            assert asgi_mod.CHART_ASGI_ENABLED is True
