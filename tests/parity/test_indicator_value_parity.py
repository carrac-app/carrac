"""Indicator-value parity tests.

Covers three of the indicator-parity audit fixes:

  1. SMA-seeded EMA (``_ema_pine_compat``, the Pine-compat seed pattern) matches a
     hand-computed reference for a short series, and the canonical
     ``carrac.charts.indicators`` copy matches the replicated
     ``consensio.indicators.ema_ribbon`` copy exactly (the latter is a local
     replica, not an import, to avoid consensio depending on the carrac app
     layer — this test is the parity guard for that duplication).
  2. ``indicators_extended._bollinger_bands`` width matches
     ``consensio.indicators.bollinger.BollingerBandCalculator`` for the same
     input — both now use population std (``ddof=0``, the population-std fix).
  3. The inline monthly CPR R3/S3 in ``carrac/charts/routes.py`` (both the
     lite and full-mode code paths) matches
     ``consensio.indicators.cpr.CPRCalculator``'s cumulative-range convention
     (R3 = R2 + (H-L), S3 = S2 - (H-L)), not the classic floor-pivot formula
     (R3 = 2P + H - 2L).

Mirrors the Flask-chart mocking pattern from test_chart_macd_param.py /
test_chart_quant_tier_gate.py (test env has empty symbol maps, so the db +
consensio + indicator calcs are monkeypatched to deterministic stubs).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# 1. EMA seed parity
# ---------------------------------------------------------------------------

class TestEmaSeedParity:
    """_ema_pine_compat (SMA-seeded EMA) matches a hand-computed reference."""

    def test_matches_hand_computed_reference_linear_ramp(self):
        """10-bar linear ramp [1..10], span=3, alpha=0.5. Hand-computed:
        seed = mean(1,2,3) = 2.0 at index 2 (bars 0-1 are NaN, pre-seed);
        index 3: 4*0.5 + 2*0.5 = 3.0; index 4: 5*0.5 + 3*0.5 = 4.0; ...
        index 9: 10*0.5 + 8*0.5 = 9.0 (each step lags close by exactly 1)."""
        from carrac.charts.indicators import _ema_pine_compat

        close = pd.Series(np.arange(1, 11, dtype=float))
        result = _ema_pine_compat(close, 3)

        expected = [None, None, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
        for i, exp in enumerate(expected):
            if exp is None:
                assert np.isnan(result.iloc[i]), f"bar {i} should be NaN (pre-seed)"
            else:
                assert abs(result.iloc[i] - exp) < 1e-9, (
                    f"bar {i}: got {result.iloc[i]}, expected {exp}"
                )

    def test_matches_hand_computed_reference_second_series(self):
        """Independent cross-check at a different scale: [10,20,30,40,50], span=3.
        seed = mean(10,20,30) = 20.0 at index 2; index 3: 40*.5+20*.5 = 30.0;
        index 4: 50*.5+30*.5 = 40.0."""
        from carrac.charts.indicators import _ema_pine_compat

        close = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
        result = _ema_pine_compat(close, 3)

        assert abs(result.iloc[2] - 20.0) < 1e-9
        assert abs(result.iloc[3] - 30.0) < 1e-9
        assert abs(result.iloc[4] - 40.0) < 1e-9

    def test_consensio_and_carrac_share_one_ema_helper(self):
        """carrac.charts.indicators re-exports consensio's single definition."""
        from carrac.charts.indicators import _ema_pine_compat as canonical
        from consensio.indicators.ema_ribbon import _ema_pine_compat as shared

        assert canonical is shared


# ---------------------------------------------------------------------------
# 2. Bollinger Band width (ddof) parity
# ---------------------------------------------------------------------------

class TestBollingerWidthParity:
    """indicators_extended._bollinger_bands matches BollingerBandCalculator
    (consensio/indicators/bollinger.py, the ddof=0 fix) for the same
    input — both now use population std (ddof=0), not pandas' sample std
    default (ddof=1)."""

    def test_bb_upper_lower_basis_match(self):
        """Both round for display (indicators_extended to 2dp, bollinger.py
        to 4dp) so compare with a tolerance that absorbs the coarser rounding,
        not bit-for-bit — the underlying ddof=0 std must be the same either
        way, so a mismatch beyond rounding noise means the ddof fix diverged."""
        from carrac.charts.indicators_extended import _bollinger_bands
        from consensio.indicators.bollinger import BollingerBandCalculator

        rng = np.random.default_rng(7)
        n = 80
        dates = pd.date_range(end="2025-01-01", periods=n, freq="D")
        close = 100 + np.cumsum(rng.standard_normal(n))
        df = pd.DataFrame({
            "open": close, "high": close + 1, "low": close - 1, "close": close,
            "volume": rng.uniform(100, 1000, n),
        }, index=dates)

        ext = _bollinger_bands(df, period=20, mult=2.0)
        can = BollingerBandCalculator(period=20, std_dev=2.0).calculate(df)

        assert ext is not None and can is not None
        assert abs(ext["upper"] - can["upper"]) < 0.01, (ext["upper"], can["upper"])
        assert abs(ext["lower"] - can["lower"]) < 0.01, (ext["lower"], can["lower"])
        assert abs(ext["basis"] - can["middle"]) < 0.01, (ext["basis"], can["middle"])

    def test_bb_width_diverges_from_sample_std_reference(self):
        """Confirms the fix is non-trivial: ddof=0 width measurably differs
        from a ddof=1 (pandas default) reference on the same input."""
        close_arr = 100 + np.cumsum(np.random.default_rng(3).standard_normal(60))
        df = pd.DataFrame({
            "open": close_arr, "high": close_arr + 1, "low": close_arr - 1,
            "close": close_arr, "volume": np.full(60, 500.0),
        }, index=pd.date_range(end="2025-01-01", periods=60, freq="D"))

        from carrac.charts.indicators_extended import _bollinger_bands
        ext = _bollinger_bands(df, period=20, mult=2.0)

        sample_std = df["close"].rolling(20).std(ddof=1).iloc[-1]  # old (wrong) behavior
        pop_std = df["close"].rolling(20).std(ddof=0).iloc[-1]
        assert abs(sample_std - pop_std) > 1e-6, "test input didn't actually exercise ddof difference"
        # The fixed helper's upper band must match the ddof=0 basis, not ddof=1
        # (0.01 tolerance absorbs _bollinger_bands' 2-decimal display rounding).
        basis = df["close"].rolling(20).mean().iloc[-1]
        assert abs(ext["upper"] - (basis + pop_std * 2.0)) < 0.01
        assert abs(ext["upper"] - (basis + sample_std * 2.0)) > 0.01


# ---------------------------------------------------------------------------
# 3. Monthly CPR R3/S3 convention parity
# ---------------------------------------------------------------------------

def _make_cpr_mock_df():
    """120-bar random-walk OHLCV (Sept 2024 - Jan 5 2025) with every December
    2024 bar's high/low/close overwritten to H=110, L=90, C=100 — so the
    "previous month" (relative to the Jan 2025 last bar) has exactly the
    H/L/C the expected-value example uses (R3=140, S3=60). All
    other days keep realistic random-walk data so unrelated computations in
    the route (EMA ribbon, RSI, etc.) don't hit degenerate all-constant
    input."""
    n = 120
    dates = pd.date_range(end="2025-01-05", periods=n, freq="D")
    rng = np.random.default_rng(0)
    close = 30000 + np.cumsum(rng.standard_normal(n) * 100)
    df = pd.DataFrame(
        {
            "open": close - 10,
            "high": close + 20,
            "low": close - 20,
            "close": close,
            "volume": rng.uniform(100, 1000, n),
        },
        index=dates,
    )
    dec_mask = (df.index.year == 2024) & (df.index.month == 12)
    assert dec_mask.sum() > 0, "test setup error: no December 2024 bars"
    df.loc[dec_mask, "high"] = 110.0
    df.loc[dec_mask, "low"] = 90.0
    df.loc[dec_mask, "close"] = 100.0
    return df


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
        "1D": {"position": 0, "signal": 0},
        "3D": {"position": 0, "signal": 0},
        "1W": {"position": 0, "signal": 0},
        "total": {"positions": 0},
    }
    return consensio


@pytest.fixture
def _cpr_chart_env(monkeypatch):
    mock_df = _make_cpr_mock_df()
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


class TestMonthlyCPRParity:
    """routes.py's inline monthly CPR (both the lite and full-mode blocks)
    uses the same cumulative-range R3/S3 convention as CPRCalculator,
    for H=110/L=90/C=100 → R3=140, S3=60."""

    def test_canonical_cpr_calculator_reference(self):
        """Ground truth from the canonical calculator itself."""
        from consensio.indicators.cpr import CPRCalculator
        result = CPRCalculator()._calculate_cpr_levels(110.0, 90.0, 100.0, 100.0, None, 'monthly')
        assert result["r3"] == 140.0
        assert result["s3"] == 60.0

    def test_routes_lite_mode_matches_cpr_calculator(self, auth_client, _cpr_chart_env):
        from consensio.indicators.cpr import CPRCalculator
        expected = CPRCalculator()._calculate_cpr_levels(110.0, 90.0, 100.0, 100.0, None, 'monthly')

        resp = auth_client.get("/api/chart/BTC?timeframe=1D&lite=1")
        assert resp.status_code == 200, resp.data[:300]
        body = resp.get_json()
        assert body["cpr"] is not None
        assert body["cpr"]["r3"] == expected["r3"] == 140.0
        assert body["cpr"]["s3"] == expected["s3"] == 60.0

    def test_routes_full_mode_matches_cpr_calculator(self, auth_client, _cpr_chart_env):
        from consensio.indicators.cpr import CPRCalculator
        expected = CPRCalculator()._calculate_cpr_levels(110.0, 90.0, 100.0, 100.0, None, 'monthly')

        resp = auth_client.get("/api/chart/BTC?timeframe=1D")
        assert resp.status_code == 200, resp.data[:300]
        body = resp.get_json()
        assert body["cpr"] is not None
        assert body["cpr"]["r3"] == expected["r3"] == 140.0
        assert body["cpr"]["s3"] == expected["s3"] == 60.0
