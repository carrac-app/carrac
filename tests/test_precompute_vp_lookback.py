"""Scanner confluence VP must use a RECENT window, not full history.

A full-history daily volume profile put BTC's POC at ~$21,324 / VAL ~$7,752
(years-old prices), which surfaced as useless S/R via scanner_get_confluence_zones.
The VP input is now windowed to the most recent `_VP_LOOKBACK_BARS` daily bars.
"""
from __future__ import annotations

import pandas as pd


def _synth_1d():
    """400 old bars clustered at ~$10k + 200 recent bars at ~$70k.

    Full-history VP → POC in the ~$10k regime (old bars dominate by count).
    Recent-window VP → POC in the ~$70k regime.
    """
    idx = pd.date_range("2020-01-01", periods=600, freq="D")
    base = dict(volume=1000.0, taker_buy_base=500.0)
    old = pd.DataFrame(
        {**base, "open": 1e4, "high": 1.05e4, "low": 0.95e4, "close": 1e4},
        index=idx[:400])
    recent = pd.DataFrame(
        {**base, "open": 7e4, "high": 7.2e4, "low": 6.8e4, "close": 7e4},
        index=idx[400:])
    return pd.concat([old, recent])


def test_vp_levels_use_recent_window_not_full_history():
    from carrac.precompute import _compute_vp_levels, _VP_LOOKBACK_BARS
    assert _VP_LOOKBACK_BARS <= 365, "lookback should be a recent window, not all history"
    vp = _compute_vp_levels(_synth_1d())
    assert vp is not None
    # POC/VAL must sit in the RECENT (~$70k) regime, never the early (~$10k) one.
    assert vp["poc"] > 50000, f"POC leaked old history: {vp['poc']}"
    assert vp["val"] > 50000, f"VAL leaked old history: {vp['val']}"


def test_vp_levels_handles_short_history():
    # Fewer bars than the window → uses what's available, no error.
    from carrac.precompute import _compute_vp_levels
    vp = _compute_vp_levels(_synth_1d().tail(40))
    assert vp is not None and vp["poc"] > 50000


def test_vp_levels_none_on_empty():
    from carrac.precompute import _compute_vp_levels
    assert _compute_vp_levels(pd.DataFrame()) is None
    assert _compute_vp_levels(None) is None
