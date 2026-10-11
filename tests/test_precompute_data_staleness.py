"""Pins that a delisted/frozen Binance ticker (dead order book) whose
synthetic-candle injection was rejected as implausible must not silently
serve corrupted-looking-fresh data — the Details panel needs a staleness
signal. Covers the `_is_history_stale` helper factored out of
`_compute_one_symbol` for testability.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd


def _daily_df(last_date, num_bars=10):
    """`num_bars` daily closes ending on `last_date` (inclusive)."""
    idx = pd.date_range(end=last_date, periods=num_bars, freq='D')
    return pd.DataFrame({
        'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0, 'volume': 10.0,
    }, index=idx)


def test_fresh_history_not_stale():
    from carrac.precompute import _is_history_stale
    now = datetime(2026, 7, 19, 12, 0, 0)
    df = _daily_df(last_date=pd.Timestamp(now.date()) - pd.Timedelta(days=1))
    assert _is_history_stale(df, 'BTC', now=now) is False


def test_history_older_than_5_days_is_stale():
    from carrac.precompute import _is_history_stale
    now = datetime(2026, 7, 19, 12, 0, 0)
    df = _daily_df(last_date=pd.Timestamp(now.date()) - pd.Timedelta(days=8))
    assert _is_history_stale(df, 'BTC', now=now) is True


def test_synthetic_today_bar_does_not_mask_staleness():
    """A synthetic candle at today's date (volume 0) must not hide the fact
    that the last REAL bar is old — the helper must look strictly before
    today."""
    from carrac.precompute import _is_history_stale
    now = datetime(2026, 7, 19, 12, 0, 0)
    today = pd.Timestamp(now.date())
    stale_history = _daily_df(last_date=today - pd.Timedelta(days=8))
    synthetic_bar = pd.DataFrame({
        'open': 7.03, 'high': 7.03, 'low': 7.03, 'close': 7.03, 'volume': 0.0,
    }, index=[today])
    df = pd.concat([stale_history, synthetic_bar])

    assert _is_history_stale(df, 'BTC', now=now) is True


def test_empty_df_is_stale():
    from carrac.precompute import _is_history_stale
    assert _is_history_stale(pd.DataFrame(), 'BTC') is True
    assert _is_history_stale(None, 'BTC') is True


def test_sanitize_floats_replaces_nan_and_inf():
    from carrac.precompute import _sanitize_floats
    dirty = {'a': float('nan'), 'b': {'c': float('inf'), 'd': 1.5},
             'e': [float('-inf'), 2, 'x'], 'f': None, 'g': 'NaN'}
    clean = _sanitize_floats(dirty)
    assert clean == {'a': None, 'b': {'c': None, 'd': 1.5},
                     'e': [None, 2, 'x'], 'f': None, 'g': 'NaN'}
    import json
    json.dumps(clean, allow_nan=False)


def test_market_structure_finite6_guards_nan():
    from consensio.indicators.market_structure import _finite6
    assert _finite6(float('nan')) is None
    assert _finite6(float('inf')) is None
    assert _finite6(None) is None
    assert _finite6('bogus') is None
    assert _finite6(1.23456789) == 1.234568
