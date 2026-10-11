"""Public /api/chart/<symbol> ?macd= opt-in overlay.

The public, keyless chart endpoint optionally includes a top-level ``macd``
series (line/signal/histogram) via ``?macd=1`` (also ``?macd=true``) so the
public TUI client can stop calling /api/chart-internal.

Contract verified here:
* Without the param → NO ``macd`` key (response unchanged for existing
  consumers: web app, curl bridge, MCP).
* With ``?macd=1`` → ``macd`` present with the same dict sub-shape
  /api/chart-internal ships: {'line': [...], 'signal': [...], 'histogram': [...]}
  where each point is {'time': int, 'value': float}.
* ``?macd=true`` is also accepted.

Mirrors the Flask-chart mocking pattern from test_chart_timing_log.py
(test env has empty symbol maps, so the db + consensio + indicator calcs are
monkeypatched to deterministic stubs).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Mock helpers (copied from test_chart_timing_log.py's Flask chart miss path)
# ---------------------------------------------------------------------------

def _make_mock_df(n=120):
    """Deterministic OHLCV DataFrame with enough bars for MACD(12,26,9) (>=35)."""
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


@pytest.fixture
def _chart_env(monkeypatch):
    """Patch the chart route's collaborators with deterministic stubs.

    Returns the mock df so a test can compute the expected MACD itself.
    """
    mock_df = _make_mock_df()
    fake_tf_data = {"1D": mock_df, "3D": mock_df, "1W": mock_df, "1M": mock_df}
    mock_db = _make_mock_db(mock_df, fake_tf_data)
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
    return mock_df


def _get_json(resp):
    import json
    return json.loads(resp.data)


class TestChartMacdParam:
    def test_no_param_omits_macd(self, auth_client, _chart_env):
        """Default response (no ?macd) must NOT carry a top-level macd key."""
        resp = auth_client.get("/api/chart/BTC?timeframe=1D")
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)
        assert "macd" not in body, "macd must be absent unless explicitly requested"

    def test_macd_1_includes_macd(self, auth_client, _chart_env):
        """?macd=1 adds the macd dict with line/signal/histogram arrays."""
        resp = auth_client.get("/api/chart/BTC?timeframe=1D&macd=1")
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)
        assert "macd" in body, "macd must be present when ?macd=1"
        macd = body["macd"]
        assert isinstance(macd, dict)
        for key in ("line", "signal", "histogram"):
            assert key in macd, f"macd missing '{key}'"
            assert isinstance(macd[key], list)
        assert macd["line"], "macd line should have points for a >=35-bar series"
        pt = macd["line"][0]
        assert set(pt.keys()) == {"time", "value"}
        assert isinstance(pt["time"], int)
        assert isinstance(pt["value"], float)

    def test_macd_true_accepted(self, auth_client, _chart_env):
        """?macd=true is accepted as an alias for 1."""
        resp = auth_client.get("/api/chart/BTC?timeframe=1D&macd=true")
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)
        assert "macd" in body
        assert isinstance(body["macd"], dict)

    def test_macd_matches_shared_helper(self, auth_client, _chart_env):
        """The macd the route ships equals compute_macd_series on the same df.

        Guarantees the public route reuses the same code path /api/chart-internal
        uses (compute_chart_indicators -> compute_macd_series).
        """
        from carrac.charts.indicators import compute_macd_series

        mock_df = _chart_env
        # The route tails df to `days` (default 5000 for HTF) — with 120 bars the
        # tail is a no-op, so the expected series is computed on the full df.
        expected = compute_macd_series(mock_df, symbol="BTC")

        resp = auth_client.get("/api/chart/BTC?timeframe=1D&macd=1")
        body = _get_json(resp)
        assert body["macd"] == expected

    def test_lite_macd_included(self, auth_client, _chart_env):
        """?macd=1 also works on the lite (preloader) path."""
        resp = auth_client.get("/api/chart/BTC?timeframe=1D&lite=1&macd=1")
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)
        assert "macd" in body
        assert isinstance(body["macd"], dict)
        assert "line" in body["macd"]

    def test_lite_no_param_omits_macd(self, auth_client, _chart_env):
        """Lite response without the param stays unchanged (no macd)."""
        resp = auth_client.get("/api/chart/BTC?timeframe=1D&lite=1")
        assert resp.status_code == 200, resp.data[:300]
        body = _get_json(resp)
        assert "macd" not in body


class TestMacdSignalHistogramRegression:
    """Signal EMA handling with a NaN-warmup input.

    macd_line carries a 25-bar leading-NaN warmup (EMA-26). _ema_pine_compat
    seeds from the valid values only, so signal and histogram are populated for
    every macd consumer (public ?macd=1 and /api/chart-internal alike).
    """

    def test_route_ships_populated_signal_and_histogram(self, auth_client, _chart_env):
        resp = auth_client.get("/api/chart/BTC?timeframe=1D&macd=1")
        assert resp.status_code == 200, resp.data[:300]
        macd = _get_json(resp)["macd"]
        assert macd["line"], "line should have points"
        assert macd["signal"], "signal must not be empty"
        assert macd["histogram"], "histogram must not be empty"
        # line starts after the EMA-26 warmup; the signal needs 9 valid macd
        # values to seed, so it starts exactly 8 bars after the line does.
        assert len(macd["signal"]) == len(macd["line"]) - 8
        assert len(macd["histogram"]) == len(macd["signal"])
        # histogram == line - signal at matching timestamps (2dp rounding slack)
        line_by_ts = {p["time"]: p["value"] for p in macd["line"]}
        sig_by_ts = {p["time"]: p["value"] for p in macd["signal"]}
        for p in macd["histogram"]:
            assert p["time"] in line_by_ts and p["time"] in sig_by_ts
            assert abs((line_by_ts[p["time"]] - sig_by_ts[p["time"]]) - p["value"]) < 0.02


class TestEmaPineCompatLeadingNaN:
    """_ema_pine_compat must skip leading NaNs when seeding — Pine's ta.ema
    starts at the first non-na bar of its source series."""

    def test_leading_nans_skipped_for_seed(self):
        from carrac.charts.indicators import _ema_pine_compat

        s = pd.Series([np.nan] * 25 + list(np.linspace(1.0, 10.0, 75)))
        out = _ema_pine_compat(s, 9)
        # First valid input index is 25 → seed lands at 25 + 9 - 1 = 33.
        assert out.iloc[:33].isna().all()
        assert out.iloc[33:].notna().all()
        assert abs(out.iloc[33] - s.iloc[25:34].mean()) < 1e-9

    def test_clean_series_behavior_unchanged(self):
        from carrac.charts.indicators import _ema_pine_compat

        rng = np.random.default_rng(1)
        s = pd.Series(rng.standard_normal(100).cumsum() + 50.0)
        out = _ema_pine_compat(s, 12)
        # Identical to the original algorithm for NaN-free input: the seed is
        # the SMA(12) of the first 12 bars, defined from index 11 onward.
        assert out.iloc[:11].isna().all()
        assert out.iloc[11:].notna().all()
        assert abs(out.iloc[11] - s.iloc[:12].mean()) < 1e-9

    def test_all_nan_or_short_valid_tail_returns_all_nan(self):
        from carrac.charts.indicators import _ema_pine_compat

        all_nan = pd.Series([np.nan] * 40)
        assert _ema_pine_compat(all_nan, 9).isna().all()
        # Only 5 valid values after the NaN run — fewer than span.
        short_tail = pd.Series([np.nan] * 35 + [1.0, 2.0, 3.0, 4.0, 5.0])
        assert _ema_pine_compat(short_tail, 9).isna().all()
