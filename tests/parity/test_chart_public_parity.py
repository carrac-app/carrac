"""Flask/FastAPI parity for GET /api/chart/<symbol>.

The strangler-parity gotcha: a test that only ever drives the Flask test
client can go green on a route that was silently mis-migrated (the FastAPI
front door in carrac/fastapi_app/routers/chart_public.py never gets
exercised at all). Every test class below drives BOTH the Flask test client
(``auth_client``) and the FastAPI-routed strangler ASGI app (``asgi_client``,
same as production traffic hits) for the SAME mocked app state, and asserts
the two responses agree.

Session-cookie auth is NOT exercisable end-to-end here: the FastAPI session
middleware (``CarracSessionMiddleware``) is Redis-backed and the test
environment runs without Redis (matches every other FastAPI test file — see
``tests/fastapi/conftest.py``'s ``member_key`` fixture, which uses an API key
for exactly this reason). All ``asgi_client`` calls below therefore
authenticate via the shared API key (``SCREENSHOT_API_KEY``, forced to
``test-api-key-for-testing`` in ``tests/conftest.py``) — this exercises the
router's ``identity_source == "api_key"`` branch (``flask.g.rate_tier``
transplant). The ``identity_source == "session"`` branch (session transplant)
is unit-tested directly against ``_replay_flask_chart`` in
``tests/fastapi/test_chart_public.py`` since it needs no Redis at all.

Neither tier differs from the default (non-monkeypatched) admin/member tier
limits for chart TF/derivatives gating (both inherit the same ``_PAID_LIMITS``
base — ``blocked_tfs=frozenset()``, ``derivatives=True``), so using the
shared key on the FastAPI side does not skew the body-equality comparisons
below relative to the Flask side's session-authenticated (member-tier)
``auth_client``.

This file lives under tests/parity/, not tests/fastapi/, so
tests/fastapi/conftest.py's fixtures aren't in scope — the ``asgi_app``/
``asgi_client`` fixtures are duplicated here, self-contained, mirroring the
pattern tests/test_chart_timing_log.py already established for the same
reason.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Iterator
from unittest.mock import MagicMock

import httpx
import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport

SCREENSHOT_KEY = "test-api-key-for-testing"  # forced in tests/conftest.py
API_KEY_HEADERS = {"X-Internal-Key": SCREENSHOT_KEY}


# ---------------------------------------------------------------------------
# asgi_client fixture — self-contained (see module docstring).
# ---------------------------------------------------------------------------

@pytest.fixture
def asgi_app(app):  # noqa: ARG001 — depends on Flask app fixture for env init
    """Return the strangler ASGI app composed of FastAPI + Flask."""
    from carrac.asgi import asgi_app as _app
    return _app


@pytest.fixture
def asgi_client(asgi_app) -> Iterator[httpx.Client]:
    """Sync wrapper around httpx.AsyncClient driving the strangler ASGI app."""
    transport = ASGITransport(app=asgi_app)
    async_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")

    class _SyncWrapper:
        def __init__(self, ac: httpx.AsyncClient) -> None:
            self._ac = ac
            self._loop = asyncio.new_event_loop()

        def __getattr__(self, name: str):
            method = getattr(self._ac, name)

            def call(*args, **kwargs):
                return self._loop.run_until_complete(method(*args, **kwargs))

            return call

        def close(self) -> None:
            self._loop.run_until_complete(self._ac.aclose())
            self._loop.close()

    wrapper = _SyncWrapper(async_client)
    yield wrapper
    wrapper.close()


# ---------------------------------------------------------------------------
# Mock helpers — mirrors tests/test_chart_swr.py / test_chart_timing_log.py /
# test_chart_macd_param.py exactly, so the miss-path shape is proven-good.
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


def _make_mock_db(mock_df, fake_tf_data, curated=True):
    db = MagicMock()
    db.is_curated.return_value = curated
    db.validate_symbol.return_value = None
    db._is_crypto.return_value = True
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
        "symbol": symbol,
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
    """Minimal db stub — the handler calls db.is_curated()/validate_symbol()
    BEFORE the cache lookup, so even a pure hit/stale-serve test needs it."""
    mock_df = _make_mock_df()
    fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
    mock_db = _make_mock_db(mock_df, fake_tf_data)
    monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
    return mock_db


@pytest.fixture
def _chart_env(monkeypatch):
    """Full miss/rebuild path stubs (mirrors test_chart_swr.py's _chart_env)."""
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
    monkeypatch.setattr("carrac.charts.routes._get_cached_subhourly", lambda *a, **kw: None)
    monkeypatch.setattr("carrac.charts.routes._set_cached_subhourly", lambda *a, **kw: None)
    return mock_db


@pytest.fixture(autouse=True)
def _reset_refresh_state(monkeypatch):
    """Isolate SWR refresh bookkeeping between tests (mirrors test_chart_swr.py) —
    also keeps a stale-serve test's background-refresh spawn from touching a
    real thread pool / Redis."""
    monkeypatch.setattr("carrac.charts.routes._chart_refresh_last", {})
    monkeypatch.setattr("carrac.redis_cache.get_redis", lambda: None)


def _strip_volatile(payload: dict) -> dict:
    """Drop fields that legitimately differ between two separate calls (the
    server clock ticks between the Flask call and the FastAPI-replay call)."""
    payload = dict(payload)
    payload.pop("_server_ts", None)
    payload.pop("_cached_at", None)
    return payload


def _flask_json(resp):
    return json.loads(resp.data)


# ---------------------------------------------------------------------------
# 1. Fresh cache hit
# ---------------------------------------------------------------------------

class TestFreshCacheHitParity:
    def test_headers_and_body_match(self, auth_client, asgi_client, monkeypatch, _db_env):
        payload = _make_cached_payload(age_s=5)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: (payload, True))
        monkeypatch.setattr("carrac.formatters.maybe_compact", lambda p, _fn: p, raising=False)

        flask_resp = auth_client.get("/api/chart/BTC?timeframe=1D")
        fastapi_resp = asgi_client.get("/api/chart/BTC?timeframe=1D", headers=API_KEY_HEADERS)

        assert flask_resp.status_code == 200, flask_resp.data[:300]
        assert fastapi_resp.status_code == 200, fastapi_resp.text[:300]
        assert flask_resp.headers.get("Cache-Control") == fastapi_resp.headers.get("Cache-Control")
        assert flask_resp.headers.get("Cache-Control", "").startswith("private")
        assert "X-Carrac-Stale" not in flask_resp.headers
        assert "X-Carrac-Stale" not in fastapi_resp.headers
        assert _strip_volatile(_flask_json(flask_resp)) == _strip_volatile(fastapi_resp.json())


# ---------------------------------------------------------------------------
# 2. Stale entry
# ---------------------------------------------------------------------------

class TestStaleEntryParity:
    def test_stale_headers_match(self, auth_client, asgi_client, monkeypatch, _db_env):
        payload = _make_cached_payload(age_s=500)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: (payload, False))
        mock_pool = MagicMock()
        monkeypatch.setattr("carrac.charts.routes._CHART_REFRESH_POOL", mock_pool)

        flask_resp = auth_client.get("/api/chart/BTC?timeframe=1D")
        fastapi_resp = asgi_client.get("/api/chart/BTC?timeframe=1D", headers=API_KEY_HEADERS)

        assert flask_resp.status_code == 200, flask_resp.data[:300]
        assert fastapi_resp.status_code == 200, fastapi_resp.text[:300]
        assert flask_resp.headers.get("X-Carrac-Stale") == fastapi_resp.headers.get("X-Carrac-Stale") == "1"

        # The age is recomputed per request against a TRUNCATING baseline
        # (`_cached_at = int(time.time()) - age_s`, set once when the payload was
        # built). These are two sequential requests, so if the wall clock crosses
        # a whole-second boundary between them one legitimately reports 501 while
        # the other reports 500 — which makes the test flaky under
        # parallel runs (`-n 4`) while it passes serially.
        #
        # The property under test is PARITY between the two stacks plus a correct
        # age, not a specific integer. Assert both, tolerating the one-second
        # boundary and nothing wider — a real drift of 2s or a wrong baseline
        # still fails.
        flask_age = int(flask_resp.headers["X-Carrac-Age"])
        fastapi_age = int(fastapi_resp.headers["X-Carrac-Age"])
        assert abs(flask_age - fastapi_age) <= 1, (
            f"stacks disagree on age beyond a second boundary: "
            f"flask={flask_age} fastapi={fastapi_age}"
        )
        assert {flask_age, fastapi_age} <= {500, 501}, (
            f"expected ~500s, got flask={flask_age} fastapi={fastapi_age}"
        )
        assert flask_resp.headers.get("Cache-Control") == fastapi_resp.headers.get("Cache-Control") == "no-store"


# ---------------------------------------------------------------------------
# 3. Cache miss (heavy build mocked, like test_chart_swr.py)
# ---------------------------------------------------------------------------

class TestCacheMissParity:
    def test_miss_body_matches(self, auth_client, asgi_client, monkeypatch, _chart_env):
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: None)

        flask_resp = auth_client.get("/api/chart/BTC?timeframe=1D")
        fastapi_resp = asgi_client.get("/api/chart/BTC?timeframe=1D", headers=API_KEY_HEADERS)

        assert flask_resp.status_code == 200, flask_resp.data[:300]
        assert fastapi_resp.status_code == 200, fastapi_resp.text[:300]
        assert _strip_volatile(_flask_json(flask_resp)) == _strip_volatile(fastapi_resp.json())
        assert flask_resp.headers.get("Cache-Control", "").startswith("private")
        assert fastapi_resp.headers.get("Cache-Control", "").startswith("private")


# ---------------------------------------------------------------------------
# 4. 404 unknown symbol
# ---------------------------------------------------------------------------

class TestUnknownSymbolParity:
    def test_404_matches(self, auth_client, asgi_client, monkeypatch, _chart_env):
        # _chart_env's db defaults to curated=True; force the dynamic-symbol
        # lookup path and make it fail (validate_symbol -> None -> 404).
        mock_df = _make_mock_df()
        fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
        mock_db = _make_mock_db(mock_df, fake_tf_data, curated=False)
        mock_db.validate_symbol.return_value = None
        monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: None)

        flask_resp = auth_client.get("/api/chart/UNKNOWNXYZ?timeframe=1D")
        fastapi_resp = asgi_client.get("/api/chart/UNKNOWNXYZ?timeframe=1D", headers=API_KEY_HEADERS)

        assert flask_resp.status_code == 404, flask_resp.data[:300]
        assert fastapi_resp.status_code == 404, fastapi_resp.text[:300]
        assert _flask_json(flask_resp) == fastapi_resp.json()


# ---------------------------------------------------------------------------
# 5. Unauthenticated browser request
# ---------------------------------------------------------------------------

class TestUnauthenticatedParity:
    def test_no_session_no_key_json_client_401(self, client, asgi_client):
        """A JSON-ish caller (Accept: application/json) with no session/key —
        both sides return the SAME 401 JSON shape."""
        flask_resp = client.get(
            "/api/chart/BTC?timeframe=1D", headers={"Accept": "application/json"}
        )
        fastapi_resp = asgi_client.get(
            "/api/chart/BTC?timeframe=1D", headers={"Accept": "application/json"}
        )
        assert flask_resp.status_code == 401
        assert fastapi_resp.status_code == 401
        assert _flask_json(flask_resp) == {"error": "Authentication required"}
        assert fastapi_resp.json() == {"error": "Authentication required"}

    def test_no_session_no_key_browser_ua_diverges_by_design(self, client, asgi_client):
        """KNOWN, PRE-EXISTING deviation (not introduced by the FastAPI front door): Flask's
        auth_or_key_required decides 401-vs-redirect purely from the Accept
        header (`request.accept_mimetypes.best == 'application/json'`), so a
        plain browser Accept header (no 'application/json' anywhere) redirects
        to /login. FastAPI's shared `auth_or_key` dependency
        (carrac/fastapi_app/deps/auth.py::_is_json_client) ALSO short-circuits
        to True whenever `request.url.path.startswith('/api/')` — which is
        every path this endpoint ever serves — so it always returns 401 JSON
        instead of redirecting for this route. This is inherited, unmodified
        behaviour from the dependency already shared by every other migrated
        authed GET route (scanner, composite, asset, batch, ...)
        reuses that dependency verbatim per its own design brief rather than
        forking it. Documented here, not fixed — see the router's docstring
        for the full reasoning.
        """
        browser_headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        flask_resp = client.get("/api/chart/BTC?timeframe=1D", headers=browser_headers)
        fastapi_resp = asgi_client.get("/api/chart/BTC?timeframe=1D", headers=browser_headers)

        assert flask_resp.status_code == 302
        assert flask_resp.headers.get("Location", "").endswith("/login")
        assert fastapi_resp.status_code == 401
        assert fastapi_resp.json() == {"error": "Authentication required"}


# ---------------------------------------------------------------------------
# 6. Bad API key
# ---------------------------------------------------------------------------

class TestBadApiKeyParity:
    def test_bad_key_401_json_matches(self, client, asgi_client):
        bad_headers = {"X-API-Key": "definitely-not-a-real-key", "Accept": "application/json"}
        flask_resp = client.get("/api/chart/BTC?timeframe=1D", headers=bad_headers)
        fastapi_resp = asgi_client.get("/api/chart/BTC?timeframe=1D", headers=bad_headers)

        assert flask_resp.status_code == 401, flask_resp.data[:300]
        assert fastapi_resp.status_code == 401, fastapi_resp.text[:300]
        assert _flask_json(flask_resp) == {"error": "Authentication required"}
        assert fastapi_resp.json() == {"error": "Authentication required"}

    def test_url_param_key_rejected_400_matches(self, client, asgi_client):
        """?key= is explicitly rejected on both sides (deprecated surface)."""
        flask_resp = client.get("/api/chart/BTC?timeframe=1D&key=whatever")
        fastapi_resp = asgi_client.get("/api/chart/BTC?timeframe=1D&key=whatever")

        assert flask_resp.status_code == 400, flask_resp.data[:300]
        assert fastapi_resp.status_code == 400, fastapi_resp.text[:300]


# ---------------------------------------------------------------------------
# 7. Tier gating — free user requesting a blocked intraday TF -> 4h rewrite
# ---------------------------------------------------------------------------

class TestTierGatingParity:
    def test_blocked_tf_rewritten_to_4h_both_paths(self, auth_client, asgi_client, monkeypatch, _chart_env):
        import carrac.auth.decorators as auth_decorators

        monkeypatch.setattr(auth_decorators, "get_request_tier", lambda: "free")
        monkeypatch.setattr(
            auth_decorators, "get_tier_limits",
            lambda tier="free": {"blocked_tfs": frozenset({"5m"}), "derivatives": False},
        )
        mock_get_cached = MagicMock(return_value=None)
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", mock_get_cached)

        flask_resp = auth_client.get("/api/chart/BTC?timeframe=5m")
        assert flask_resp.status_code == 200, flask_resp.data[:300]
        flask_used_tf = mock_get_cached.call_args[0][1]
        assert flask_used_tf.startswith("4h"), flask_used_tf

        mock_get_cached.reset_mock()
        fastapi_resp = asgi_client.get("/api/chart/BTC?timeframe=5m", headers=API_KEY_HEADERS)
        assert fastapi_resp.status_code == 200, fastapi_resp.text[:300]
        fastapi_used_tf = mock_get_cached.call_args[0][1]
        assert fastapi_used_tf.startswith("4h"), fastapi_used_tf

        assert flask_used_tf == fastapi_used_tf


# ---------------------------------------------------------------------------
# 8. Param passthrough — lite=1 / fmt=col / quant= / derivatives=
# ---------------------------------------------------------------------------

class TestParamPassthroughParity:
    @pytest.mark.parametrize(
        "query",
        [
            "timeframe=1D&lite=1",
            "timeframe=1D&fmt=col",
            "timeframe=1D&lite=1&fmt=col",
        ],
    )
    def test_param_combo_body_matches(self, auth_client, asgi_client, monkeypatch, _chart_env, query):
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: None)

        flask_resp = auth_client.get(f"/api/chart/BTC?{query}")
        fastapi_resp = asgi_client.get(f"/api/chart/BTC?{query}", headers=API_KEY_HEADERS)

        assert flask_resp.status_code == 200, flask_resp.data[:300]
        assert fastapi_resp.status_code == 200, fastapi_resp.text[:300]
        assert _strip_volatile(_flask_json(flask_resp)) == _strip_volatile(fastapi_resp.json())


# ---------------------------------------------------------------------------
# 9. fresh=1 bypasses the cache lookup on both paths
# ---------------------------------------------------------------------------

class TestFreshParamParity:
    def test_fresh_1_bypasses_cache_both_paths(self, auth_client, asgi_client, monkeypatch, _chart_env):
        mock_get_cached = MagicMock(return_value=(_make_cached_payload(), False))
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", mock_get_cached)
        mock_pool = MagicMock()
        monkeypatch.setattr("carrac.charts.routes._CHART_REFRESH_POOL", mock_pool)

        flask_resp = auth_client.get("/api/chart/BTC?timeframe=1D&fresh=1")
        assert flask_resp.status_code == 200, flask_resp.data[:300]
        mock_get_cached.assert_not_called()

        fastapi_resp = asgi_client.get("/api/chart/BTC?timeframe=1D&fresh=1", headers=API_KEY_HEADERS)
        assert fastapi_resp.status_code == 200, fastapi_resp.text[:300]
        mock_get_cached.assert_not_called()

        assert _strip_volatile(_flask_json(flask_resp)) == _strip_volatile(fastapi_resp.json())
