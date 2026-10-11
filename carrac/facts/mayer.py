"""
Mayer multiple — price / 200-week-MA and its historical percentile.

Pure & deterministic: works for any asset, not just BTC.
"""

from __future__ import annotations

import pandas as pd

# 200 weeks × 7 days/week
WINDOW = 1400


def mayer(daily_closes) -> dict | None:
    """Price / 200-week-MA ('Mayer multiple') + its historical percentile.

    daily_closes: sequence of DAILY closing prices, oldest first / newest last.
    200 weeks ≈ 1400 daily bars.  Returns None if fewer than 1400 bars.
    Pure & deterministic — works for any asset, not just BTC.

    Returns
    -------
    {"multiple": float (3 d.p.), "percentile": int 0-100}
    or None when data is insufficient.
    """
    closes = list(daily_closes)
    if len(closes) < WINDOW:
        return None

    series = pd.Series(closes, dtype=float)
    ma = series.rolling(WINDOW).mean()

    # Drop NaN (first WINDOW-1 bars) — valid range starts at index WINDOW-1
    valid_mask = ma.notna()
    prices = series[valid_mask]
    mas = ma[valid_mask]

    # Guard divide-by-zero: if any rolling MA in the valid window is zero return None
    if (mas == 0.0).any():
        return None

    multiples = prices / mas

    last_multiple = float(multiples.iloc[-1])

    # Percentile rank of last multiple within the full historical multiple series
    n = len(multiples)
    count_le = int((multiples <= last_multiple).sum())
    percentile = int(round(100 * count_le / n))

    return {
        "multiple": round(last_multiple, 3),
        "percentile": percentile,
    }


# Zone thresholds — kept identical to the legacy CoinMetrics fetch_mayer_multiple()
# so this is a drop-in replacement for the sentiment / weekly-signal shape.
def _mayer_zone(multiple: float) -> str:
    if multiple < 0.8:
        return "deep_discount"
    if multiple < 1.0:
        return "accumulation"
    if multiple < 2.0:
        return "normal"
    if multiple < 2.4:
        return "extended"
    return "bubble"


def mayer_card(daily_closes) -> dict:
    """Legacy-shaped Mayer dict computed from LOCAL OHLCV (no CoinMetrics).

    Drop-in replacement for the CoinMetrics-CSV ``fetch_mayer_multiple()``:
    returns the same keys the weekly-signal engine consumes
    ({'mayer_multiple', 'price', 'ma_200w', 'zone'}) plus 'percentile'.

    daily_closes: sequence of DAILY closing prices, oldest first / newest last
    (accepts a list or a pandas Series — e.g. ``df['close']``).

    Always returns a dict. When fewer than WINDOW bars are available it returns
    ``{'mayer_multiple': None, 'zone': 'unknown'}`` so callers can safely call
    ``.get('mayer_multiple')`` rather than guarding against None.
    """
    base = mayer(daily_closes)
    if base is None:
        return {"mayer_multiple": None, "zone": "unknown"}

    closes = list(daily_closes)
    price = float(closes[-1])
    ma_200w = sum(float(c) for c in closes[-WINDOW:]) / WINDOW
    multiple = base["multiple"]

    return {
        "mayer_multiple": multiple,
        "price": round(price, 2),
        "ma_200w": round(ma_200w, 2),
        "zone": _mayer_zone(multiple),
        "percentile": base["percentile"],
    }
