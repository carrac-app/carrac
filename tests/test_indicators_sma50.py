"""smaLines must carry sma50 (for the conventional 50/200 death cross)."""
import pandas as pd
from carrac.charts.indicators import compute_chart_indicators


def test_sma50_present_in_smalines():
    n = 260  # >= 220 so the SMA block runs; >= 200 so sma200 has points
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    close = pd.Series(range(1, n + 1), index=idx, dtype=float)
    df = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": [1.0] * n},
        index=idx,
    )
    out = compute_chart_indicators(df, None, "TEST")
    sma = out["indicators"]["smaLines"]
    assert "sma50" in sma and len(sma["sma50"]) > 0
    # the existing keys are unchanged
    assert "sma20" in sma and "sma200" in sma
    # each sma50 point keeps the {time, value} shape
    assert set(sma["sma50"][0].keys()) == {"time", "value"}
