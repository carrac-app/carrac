"""1h/4h double data-fetch elimination.

Tests that for tf in ['1h', '4h']:
- get_ltf_multi_timeframe_data is called once
- get_multi_timeframe_data is NOT called as a fresh source round-trip
  when the daily data can be served from the same cached call

We mock both db functions and assert call counts, verifying no redundant
uncached source round-trip occurs.

The service tests cover carrac/charts/service.py (the ASGI route's logic);
the route test drives carrac/charts/routes.py (the Flask handler) through the
test client with the db mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pandas as pd
import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ohlcv_df(n=200, freq="h"):
    """Return a minimal OHLCV DataFrame indexed by DatetimeIndex."""
    rng = np.random.default_rng(0)
    dates = pd.date_range(end="2026-01-01", periods=n, freq=freq)
    close = 50000 + np.cumsum(rng.standard_normal(n) * 100)
    return pd.DataFrame(
        {
            "open": close - rng.uniform(0, 50, n),
            "high": close + rng.uniform(0, 100, n),
            "low": close - rng.uniform(0, 100, n),
            "close": close,
            "volume": rng.uniform(1000, 50000, n),
        },
        index=dates,
    )


def _make_daily_df(n=500):
    return _make_ohlcv_df(n, freq="D")


# ---------------------------------------------------------------------------
# service.py 1h — get_multi_timeframe_data must NOT be called
#         as a completely independent uncached round-trip
# ---------------------------------------------------------------------------


def test_service_1h_does_not_double_fetch(monkeypatch):
    """build_chart_payload(symbol, timeframe='1h') — get_multi_timeframe_data
    is called AT MOST ONCE (cache-first). A second independent call to the
    same function (which would be a fresh DB round-trip) is not acceptable.
    """
    from carrac.charts import service as svc

    daily_df = _make_daily_df()
    hourly_df = _make_ohlcv_df(2000, freq="h")

    mock_db = MagicMock()
    mock_db._is_crypto.return_value = True
    mock_db.get_ltf_multi_timeframe_data.return_value = {"1h": hourly_df, "4h": hourly_df}
    mock_db.get_multi_timeframe_data.return_value = {"1D": daily_df, "3D": daily_df, "1W": daily_df}

    mock_rsi_calc = MagicMock()
    mock_rsi_calc.calculate_series.return_value = pd.Series(dtype=float)

    mock_services = MagicMock()
    mock_services.db = mock_db
    mock_services.rsi_calc = mock_rsi_calc

    with patch.object(svc, "get_services", return_value=mock_services), \
         patch("carrac.charts.service.compute_chart_indicators",
               return_value={"rsi": [], "macd": [], "indicators": {}}), \
         patch("carrac.charts.indicators_extended.compute_indicator_snapshot",
               return_value={}), \
         patch.object(svc, "_get_cached_subhourly", return_value=None), \
         patch.object(svc, "_set_cached_subhourly"):

        payload = svc.build_chart_payload("BTC", timeframe="1h", days=200)

    # LTF fetch must happen
    mock_db.get_ltf_multi_timeframe_data.assert_called_once()

    # get_multi_timeframe_data may be called, but ONLY ONCE (to get daily for CPR/MAs)
    # — not twice as independent round-trips
    mtf_call_count = mock_db.get_multi_timeframe_data.call_count
    assert mtf_call_count <= 1, (
        f"get_multi_timeframe_data called {mtf_call_count} times for 1h tf; "
        "expected at most 1 (single cache-first fetch for daily data)"
    )

    assert "ohlcv" in payload
    assert payload["timeframe"] == "1h"


def test_service_4h_does_not_double_fetch(monkeypatch):
    """build_chart_payload(symbol, timeframe='4h') — same constraint as 1h."""
    from carrac.charts import service as svc

    daily_df = _make_daily_df()
    hourly_df = _make_ohlcv_df(2000, freq="h")

    mock_db = MagicMock()
    mock_db._is_crypto.return_value = True
    mock_db.get_ltf_multi_timeframe_data.return_value = {"1h": hourly_df, "4h": hourly_df}
    mock_db.get_multi_timeframe_data.return_value = {"1D": daily_df, "3D": daily_df, "1W": daily_df}

    mock_rsi_calc = MagicMock()
    mock_rsi_calc.calculate_series.return_value = pd.Series(dtype=float)

    mock_services = MagicMock()
    mock_services.db = mock_db
    mock_services.rsi_calc = mock_rsi_calc

    with patch.object(svc, "get_services", return_value=mock_services), \
         patch("carrac.charts.service.compute_chart_indicators",
               return_value={"rsi": [], "macd": [], "indicators": {}}), \
         patch("carrac.charts.indicators_extended.compute_indicator_snapshot",
               return_value={}), \
         patch.object(svc, "_get_cached_subhourly", return_value=None), \
         patch.object(svc, "_set_cached_subhourly"):

        payload = svc.build_chart_payload("BTC", timeframe="4h", days=200)

    mock_db.get_ltf_multi_timeframe_data.assert_called_once()
    mtf_call_count = mock_db.get_multi_timeframe_data.call_count
    assert mtf_call_count <= 1, (
        f"get_multi_timeframe_data called {mtf_call_count} times for 4h tf; "
        "expected at most 1"
    )
    assert payload["timeframe"] == "4h"


# ---------------------------------------------------------------------------
# routes.py Flask handler — same constraint
#         (The service.py and routes.py share the same structure; test both)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tf", ["1h", "4h"])
def test_routes_intraday_does_not_double_fetch(auth_client, monkeypatch, tf):
    """Flask GET /api/chart/<symbol>?timeframe=<1h|4h>, driven through the real route
    handler: one intraday fetch and at most one daily (weekly-MA) fetch."""

    daily_df = _make_daily_df()
    hourly_df = _make_ohlcv_df(2000, freq="h")

    mock_db = MagicMock()
    mock_db.is_curated.return_value = True
    mock_db.validate_symbol.return_value = None
    mock_db._is_crypto.return_value = True
    mock_db.get_ltf_multi_timeframe_data.return_value = {"1h": hourly_df, "4h": hourly_df}
    mock_db.get_multi_timeframe_data.return_value = {
        "1D": daily_df, "3D": daily_df, "1W": daily_df, "1M": daily_df}
    mock_db.binance_map = {"BTC": True}
    mock_db.stock_map = {}
    mock_db.etf_map = {}
    mock_db.hyperliquid.is_supported.return_value = False
    mock_db.ccxt.is_supported.return_value = False

    mock_consensio = MagicMock()
    mock_consensio.calculate_multi_timeframe.return_value = {
        "1D": {"position": 1, "signal": 1},
        "3D": {"position": 0, "signal": 0},
        "1W": {"position": -1, "signal": -1},
        "total": {"positions": 0},
    }
    mock_rsi = MagicMock()
    mock_rsi.calculate_series.return_value = None
    mock_cpr = MagicMock()
    mock_cpr.calculate_monthly_cpr.return_value = None

    for name, value in (
        ("_get_cached_chart", lambda *a, **kw: None),
        ("_set_cached_chart", lambda *a, **kw: None),
        ("_get_cached_subhourly", lambda *a, **kw: None),
        ("_set_cached_subhourly", lambda *a, **kw: None),
        ("_get_db", lambda: mock_db),
        ("_get_consensio", lambda: mock_consensio),
        ("_get_rsi_calc", lambda: mock_rsi),
        ("_get_cpr_calc", lambda: mock_cpr),
    ):
        monkeypatch.setattr(f"carrac.charts.routes.{name}", value)

    resp = auth_client.get(f"/api/chart/BTC?timeframe={tf}")

    assert resp.status_code == 200, resp.data[:300]
    assert resp.get_json()["timeframe"] == tf
    mock_db.get_ltf_multi_timeframe_data.assert_called_once()
    mtf_call_count = mock_db.get_multi_timeframe_data.call_count
    assert mtf_call_count <= 1, (
        f"get_multi_timeframe_data called {mtf_call_count} times for {tf}; "
        "expected at most 1"
    )
