"""Vectorized OHLCV serialization golden test.

Captures the CURRENT iterrows() output for a fixed fixture DataFrame,
then asserts the vectorized implementation produces byte-for-byte identical
output (same types: int timestamps, float OHLC, float volume).

The fixture uses a fixed seed and deterministic values so this test is
reproducible across runs and environments.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Fixed fixture DataFrame (deterministic, same seed every run)
# ---------------------------------------------------------------------------

def _make_fixture_df() -> pd.DataFrame:
    """Build a fixed-seed OHLCV DataFrame mimicking real chart data."""
    rng = np.random.default_rng(12345)
    n = 50
    dates = pd.date_range(start="2025-01-01", periods=n, freq="D", tz=None)
    close = 50000.0 + np.cumsum(rng.standard_normal(n) * 200)
    opens = close - rng.uniform(0, 50, n)
    highs = close + rng.uniform(10, 150, n)
    lows = close - rng.uniform(10, 150, n)
    volumes = rng.uniform(1000, 100000, n)
    # Introduce one NaN volume row to test the notna filter
    volumes[25] = np.nan
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": close, "volume": volumes},
        index=dates,
    )


# ---------------------------------------------------------------------------
# Reference implementation (current iterrows) — frozen snapshot
# ---------------------------------------------------------------------------

def _iterrows_ohlcv_volume(df: pd.DataFrame):
    """Current iterrows implementation from carrac/charts/service.py."""
    ohlcv = []
    volume = []
    for idx, row in df.iterrows():
        if hasattr(idx, "timestamp"):
            timestamp = int(idx.timestamp())
        else:
            timestamp = int(pd.Timestamp(idx).timestamp())

        ohlcv.append(
            {
                "time": timestamp,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        )

        if "volume" in row and pd.notna(row["volume"]):
            is_up = row["close"] >= row["open"]
            volume.append(
                {
                    "time": timestamp,
                    "value": float(row["volume"]),
                    "color": (
                        "rgba(38, 166, 154, 0.5)"
                        if is_up
                        else "rgba(239, 83, 80, 0.5)"
                    ),
                }
            )

    return ohlcv, volume


# ---------------------------------------------------------------------------
# Vectorized implementation (what service.py should produce after fix)
# ---------------------------------------------------------------------------

def _vectorized_ohlcv_volume(df: pd.DataFrame):
    """Vectorized implementation to replace iterrows in service.py."""
    # Timestamps — convert DatetimeIndex to int seconds
    timestamps = df.index.astype("datetime64[s]").astype("int64").tolist()

    opens = df["open"].astype(float).tolist()
    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    closes = df["close"].astype(float).tolist()

    ohlcv = [
        {
            "time": timestamps[i],
            "open": opens[i],
            "high": highs[i],
            "low": lows[i],
            "close": closes[i],
        }
        for i in range(len(timestamps))
    ]

    volume = []
    if "volume" in df.columns:
        vol_vals = df["volume"].astype(float).tolist()
        vol_notna = df["volume"].notna().tolist()
        up_color = "rgba(38, 166, 154, 0.5)"
        down_color = "rgba(239, 83, 80, 0.5)"
        volume = [
            {
                "time": timestamps[i],
                "value": vol_vals[i],
                "color": up_color if closes[i] >= opens[i] else down_color,
            }
            for i in range(len(timestamps))
            if vol_notna[i]
        ]

    return ohlcv, volume


# ---------------------------------------------------------------------------
# Golden equality test — vectorized == iterrows byte-for-byte
# ---------------------------------------------------------------------------


def test_vectorized_ohlcv_equals_iterrows_golden():
    """Vectorized and iterrows produce identical ohlcv lists."""
    df = _make_fixture_df()

    ref_ohlcv, _ref_vol = _iterrows_ohlcv_volume(df)
    vec_ohlcv, _vec_vol = _vectorized_ohlcv_volume(df)

    assert len(vec_ohlcv) == len(ref_ohlcv), "ohlcv length mismatch"
    for i, (ref, vec) in enumerate(zip(ref_ohlcv, vec_ohlcv)):
        assert ref == vec, f"ohlcv[{i}] mismatch: ref={ref} vec={vec}"


def test_vectorized_volume_equals_iterrows_golden():
    """Vectorized and iterrows produce identical volume lists (NaN row excluded)."""
    df = _make_fixture_df()

    _ref_ohlcv, ref_vol = _iterrows_ohlcv_volume(df)
    _vec_ohlcv, vec_vol = _vectorized_ohlcv_volume(df)

    assert len(vec_vol) == len(ref_vol), (
        f"volume length mismatch: ref={len(ref_vol)} vec={len(vec_vol)}"
    )
    for i, (ref, vec) in enumerate(zip(ref_vol, vec_vol)):
        assert ref == vec, f"volume[{i}] mismatch: ref={ref} vec={vec}"


# ---------------------------------------------------------------------------
# Type-exactness checks (int timestamps, float OHLC)
# ---------------------------------------------------------------------------


def test_vectorized_timestamps_are_int():
    """All 'time' values in ohlcv are Python int (not numpy int64 or float)."""
    df = _make_fixture_df()
    vec_ohlcv, _ = _vectorized_ohlcv_volume(df)
    for item in vec_ohlcv:
        assert isinstance(item["time"], int), (
            f"timestamp type is {type(item['time'])}, expected int"
        )


def test_vectorized_ohlc_are_float():
    """All open/high/low/close values are Python float."""
    df = _make_fixture_df()
    vec_ohlcv, _ = _vectorized_ohlcv_volume(df)
    for item in vec_ohlcv:
        for key in ("open", "high", "low", "close"):
            assert isinstance(item[key], float), (
                f"{key} type is {type(item[key])}, expected float"
            )


def test_vectorized_volume_value_is_float():
    """Volume 'value' fields are Python float."""
    df = _make_fixture_df()
    _, vec_vol = _vectorized_ohlcv_volume(df)
    for item in vec_vol:
        assert isinstance(item["value"], float), (
            f"volume value type is {type(item['value'])}, expected float"
        )


# ---------------------------------------------------------------------------
# NaN volume row is excluded from volume list
# ---------------------------------------------------------------------------


def test_nan_volume_row_excluded():
    """Row with NaN volume is excluded from volume list but present in ohlcv."""
    df = _make_fixture_df()
    n_total = len(df)
    n_nan_vol = int(df["volume"].isna().sum())  # 1 from fixture

    vec_ohlcv, vec_vol = _vectorized_ohlcv_volume(df)

    assert len(vec_ohlcv) == n_total
    assert len(vec_vol) == n_total - n_nan_vol


# ---------------------------------------------------------------------------
# service.py actual implementation matches golden
# ---------------------------------------------------------------------------


def test_service_build_chart_payload_ohlcv_matches_golden(monkeypatch):
    """The actual service.build_chart_payload produces ohlcv matching reference.

    We patch the DB and indicator calls so only the OHLCV serialization
    code is exercised — this ensures the vectorized implementation in
    service.py is consistent with the iterrows reference.
    """
    from carrac.charts import service as svc

    df = _make_fixture_df()

    # Reference output from the row-by-row iterrows implementation (the
    # golden baseline the vectorized path must match)
    ref_ohlcv, _ref_vol = _iterrows_ohlcv_volume(df)

    mock_db = MagicMock()
    mock_db._is_crypto.return_value = False
    mock_db.get_multi_timeframe_data.return_value = {"1D": df}

    mock_services = MagicMock()
    mock_services.db = mock_db
    mock_services.rsi_calc = MagicMock()

    with patch.object(svc, "get_services", return_value=mock_services), \
         patch("carrac.charts.service.compute_chart_indicators",
               return_value={"rsi": [], "macd": [], "indicators": {}}), \
         patch("carrac.charts.indicators_extended.compute_indicator_snapshot",
               return_value={}):
        payload = svc.build_chart_payload("AAPL", timeframe="1D", days=len(df))

    actual_ohlcv = payload["ohlcv"]

    assert len(actual_ohlcv) == len(ref_ohlcv), (
        f"ohlcv length: service={len(actual_ohlcv)} ref={len(ref_ohlcv)}"
    )
    for i, (ref, actual) in enumerate(zip(ref_ohlcv, actual_ohlcv)):
        # Check types match exactly
        assert type(actual["time"]) is type(ref["time"]), (
            f"[{i}] time type mismatch: {type(actual['time'])} vs {type(ref['time'])}"
        )
        # Check values match exactly
        assert actual == ref, f"ohlcv[{i}] mismatch: actual={actual} ref={ref}"
