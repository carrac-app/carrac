"""Per-request timing + cache HIT/MISS logging for chart endpoints.

Tests that every chart response emits exactly one greppable log line of the form:
    chart <SYMBOL> tf=<TF> <hit|miss> <N>ms

Coverage
--------
Flask  : GET /api/chart/<symbol>  (carrac.charts.routes.get_chart_data)
FastAPI: GET /api/chart-internal/<symbol>  (carrac.fastapi_app.routers.chart_internal)
"""
from __future__ import annotations

import asyncio
import re
from typing import Iterator
from unittest.mock import MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

LOG_PATTERN = re.compile(r"chart \w+ tf=\S+ (hit|miss) \d+ms")

_MOCK_PAYLOAD = {
    "ohlcv": [{"time": 1700000000, "open": 30000.0, "high": 31000.0, "low": 29000.0, "close": 30500.0}],
    "volume": [],
    "rsi": [],
    "macd": [],
    "symbol": "BTC",
    "timeframe": "1D",
    "cpr": None,
    "weekly_cpr": None,
    "daily_cpr": None,
    "indicators": {},
    "indicators_ext": {},
}


# ---------------------------------------------------------------------------
# asgi_client fixture (mirrors tests/fastapi/conftest.py, declared here so the
# test file is self-contained and doesn't require being inside tests/fastapi/).
# ---------------------------------------------------------------------------

@pytest.fixture
def asgi_app(app):  # noqa: ARG001 — depends on Flask app fixture for env init
    """Return the strangler ASGI app composed of FastAPI + Flask."""
    from carrac.asgi import asgi_app as _app
    return _app


@pytest.fixture
def asgi_client(asgi_app) -> Iterator[httpx.Client]:
    """Sync httpx.Client driving the strangler ASGI app."""
    transport = ASGITransport(app=asgi_app)
    async_client = httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    )

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
# Mock helpers (Flask chart miss path)
# ---------------------------------------------------------------------------

def _make_mock_df(n=50):
    """Small deterministic OHLCV DataFrame."""
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
    """Return a MagicMock OHLCVDatabase stub sufficient for get_chart_data."""
    db = MagicMock()
    db.is_curated.return_value = True
    db.validate_symbol.return_value = None
    db._is_crypto.return_value = True
    db.get_multi_timeframe_data.return_value = fake_tf_data
    db.get_ltf_multi_timeframe_data.return_value = {
        "4h": mock_df, "1h": mock_df, "2h": mock_df,
    }
    db.get_dynamic_ohlcv.return_value = mock_df
    # Attributes accessed by _determine_ws_venue
    db.binance_map = {"BTC": True}  # makes is_crypto+binance_map hit → ws_venue='binance'
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


def _make_chart_hit_response():
    """Minimal cached-chart dict (simulates a cache hit)."""
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
        "_server_ts": 1700000000,
    }


# ---------------------------------------------------------------------------
# Flask  /api/chart/<symbol>
# ---------------------------------------------------------------------------


class TestFlaskChartTimingLog:
    """Timing log emitted on /api/chart/<symbol>."""

    def test_cache_miss_emits_timing_log(self, auth_client, monkeypatch, caplog):
        """A cache-miss request must emit a 'miss' timing log line at INFO."""
        import logging

        mock_df = _make_mock_df()
        fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
        mock_db = _make_mock_db(mock_df, fake_tf_data)
        mock_consensio = _make_mock_consensio()
        mock_rsi = MagicMock()
        mock_rsi.calculate_series.return_value = None
        mock_cpr = MagicMock()
        mock_cpr.calculate_monthly_cpr.return_value = None

        # Force cache miss
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: None)
        # Suppress cache write
        monkeypatch.setattr("carrac.charts.routes._set_cached_chart", lambda *a, **kw: None)
        # Inject our stub db/indicators
        monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)
        monkeypatch.setattr("carrac.charts.routes._get_consensio", lambda: mock_consensio)
        monkeypatch.setattr("carrac.charts.routes._get_rsi_calc", lambda: mock_rsi)
        monkeypatch.setattr("carrac.charts.routes._get_cpr_calc", lambda: mock_cpr)
        # Stub compute_chart_indicators so heavy logic doesn't run
        monkeypatch.setattr(
            "carrac.charts.routes.compute_chart_indicators",
            lambda *a, **kw: {},
            raising=False,
        )
        # Stub session computation
        monkeypatch.setattr(
            "carrac.charts.routes.compute_session_data",
            lambda *a, **kw: None,
            raising=False,
        )

        with caplog.at_level(logging.INFO, logger="carrac.charts"):
            resp = auth_client.get("/api/chart/BTC?timeframe=1D")

        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.data[:300]}"
        )
        _assert_timing_log(caplog, "BTC", "miss")

    def test_cache_hit_emits_timing_log(self, auth_client, monkeypatch, caplog):
        """A cache-hit request must emit a 'hit' timing log line at INFO."""
        import logging

        # The handler calls _get_db() + db.is_curated() before reaching the
        # cache lookup, so we must stub the db even for a hit test.
        mock_df = _make_mock_df()
        fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
        mock_db = _make_mock_db(mock_df, fake_tf_data)
        monkeypatch.setattr("carrac.charts.routes._get_db", lambda: mock_db)

        cached = _make_chart_hit_response()
        # get_cached_chart now returns (payload, is_fresh) — True here
        # exercises the unchanged fresh-hit path (see test_chart_swr.py for stale).
        monkeypatch.setattr("carrac.charts.routes._get_cached_chart", lambda *a, **kw: (cached, True))
        # maybe_compact is lazy-imported inside the hit branch; stub at source
        monkeypatch.setattr(
            "carrac.formatters.maybe_compact",
            lambda payload, _fn: payload,
            raising=False,
        )

        with caplog.at_level(logging.INFO, logger="carrac.charts"):
            resp = auth_client.get("/api/chart/BTC?timeframe=1D")

        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.data[:300]}"
        )
        _assert_timing_log(caplog, "BTC", "hit")


# ---------------------------------------------------------------------------
# FastAPI  /api/chart-internal/<symbol>
# ---------------------------------------------------------------------------


class TestFastAPIChartTimingLog:
    """Timing log emitted on /api/chart-internal/<symbol>."""

    def test_chart_internal_miss_emits_timing_log(self, asgi_client, monkeypatch, caplog):
        """A request to /api/chart-internal emits a timing log (miss path, mocked service)."""
        import logging

        monkeypatch.setenv("SCREENSHOT_API_KEY", "test-timing-key")

        # Chart-internal now consults the shared chart cache (:int
        # keys) — another test in this process may have populated BTC's entry,
        # turning this request into a hit/stale and breaking the miss
        # assertion. Force the miss path deterministically.
        monkeypatch.setattr(
            "carrac.fastapi_app.routers.chart_internal.get_cached_chart",
            lambda *a, **kw: None,
        )
        with patch(
            "carrac.charts.service.build_chart_payload",
            return_value=_MOCK_PAYLOAD,
        ):
            with caplog.at_level(logging.INFO, logger="carrac.fastapi.chart_internal"):
                resp = asgi_client.get(
                    "/api/chart-internal/BTC",
                    headers={"X-Internal-Key": "test-timing-key"},
                )

        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        _assert_timing_log(caplog, "BTC", "miss", logger_name="carrac.fastapi.chart_internal")


# ---------------------------------------------------------------------------
# Log-assertion helper
# ---------------------------------------------------------------------------


def _assert_timing_log(
    caplog,
    symbol: str,
    expected_verdict: str,  # 'hit' or 'miss'
    logger_name: str = "carrac.charts",
) -> None:
    """Assert caplog contains a matching timing record."""
    records = [r for r in caplog.records if r.name == logger_name]
    matching = [r for r in records if LOG_PATTERN.search(r.getMessage())]
    assert matching, (
        f"No 'chart … tf=… (hit|miss) …ms' log record found in {logger_name}. "
        f"All {logger_name} records: {[r.getMessage() for r in records]}"
    )
    msg = matching[0].getMessage()
    assert symbol.upper() in msg.upper(), (
        f"Symbol '{symbol}' not in log message: {msg}"
    )
    assert expected_verdict in msg, (
        f"Expected '{expected_verdict}' in log message, got: {msg}"
    )
