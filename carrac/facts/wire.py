"""Wire helpers: build the ctx dict for build_asset_facts + store in Redis.

Three exported functions:
  build_facts_ctx  — pure (no I/O, no clock): assembles the ctx from
                     already-computed precompute data.
  store_asset_facts — calls build_asset_facts + stores the bundle as JSON
                      in Redis. Non-fatal: returns None on any error.
  get_asset_facts  — read the cached facts bundle for a symbol from Redis.
                     Returns the dict or None. Non-fatal.

Helper:
  map_volume_metrics — maps db.calculate_volume_metrics output to the
                       bundle's volume shape.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)


def _numpy_default(obj):
    """Minimal numpy-safe JSON default (avoids a circular import via redis_cache)."""
    try:
        import numpy as np
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except ImportError:
        pass
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")




def _derive_tf_stats(df: "pd.DataFrame") -> dict:
    """Derive atr14, sma20/50/200 series, and last candle from an OHLCV DataFrame.

    Pure, no I/O. Shared helper used for both daily and weekly blocks.
    Returns dict with: atr14, sma20_series, sma50_series, sma200_series, candle.
    """
    n = len(df)
    close = df["close"] if n > 0 else pd.Series(dtype=float)

    # --- ATR14 (true range, 14-period rolling mean) ---
    if n >= 15:
        high = df["high"]
        low = df["low"]
        prev_c = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_c).abs(), (low - prev_c).abs()],
            axis=1,
        ).max(axis=1)
        _atr14_raw = tr.rolling(14).mean().iloc[-1]
        atr14 = float(_atr14_raw) if pd.notna(_atr14_raw) else 0.0
    else:
        atr14 = 0.0

    # --- SMA series (rolling mean, NaN → None, length = n) ---
    def _sma(period: int) -> list:
        if n < 1:
            return []
        s = close.rolling(period).mean()
        return [None if pd.isna(v) else float(v) for v in s]

    sma20_series = _sma(20)
    sma50_series = _sma(50)
    sma200_series = _sma(200)

    # --- Candle from the last row ---
    candle: dict | None = None
    if n >= 1:
        last = df.iloc[-1]
        prev_close_val = float(df["close"].iloc[-2]) if n >= 2 else None
        candle = {
            "o": float(last["open"]),
            "h": float(last["high"]),
            "l": float(last["low"]),
            "c": float(last["close"]),
            "prev_close": prev_close_val,
        }

    return {
        "atr14": atr14,
        "sma20_series": sma20_series,
        "sma50_series": sma50_series,
        "sma200_series": sma200_series,
        "candle": candle,
    }


def build_facts_ctx(
    symbol: str,
    daily_df: "pd.DataFrame",
    latest_price: float,
    *,
    key_levels: dict | None = None,
    ma_levels: dict | None = None,
    rsi: dict | None = None,
    volume: dict | None = None,
    zones: dict | None = None,
    monthly_cpr: dict | None = None,
    daily_closes: list | None = None,
    as_of: str | None = None,
    weekly_df: "pd.DataFrame | None" = None,
    weekly_rsi: dict | None = None,
    funding_series: "list[float] | None" = None,
    smart_money_bias: "float | None" = None,
    data_stale: dict | None = None,
) -> dict:
    """Assemble the build_asset_facts ctx from precompute's per-asset data.

    Pure (no I/O, no clock — as_of is passed in).

    ``data_stale`` is a block from ``describe_staleness``. When it says the
    history is dead, build_asset_facts emits it INSTEAD of the derived facts
    rather than alongside them.
    Derives atr14 + sma20/50/200 series + candle from daily_df via
    _derive_tf_stats. When weekly_df is a non-empty DataFrame the same
    derivation is applied and ctx["weekly"] is populated; callers that omit
    weekly_df are unaffected (back-compat).
    """
    n = len(daily_df)
    close = daily_df["close"] if n > 0 else pd.Series(dtype=float)

    # --- Daily stats (ATR14, SMA series, candle) via shared helper ---
    _daily_stats = _derive_tf_stats(daily_df)

    # --- Levels: merge key_levels + relevant ma scalars (skip None values) ---
    levels: dict = {}
    if key_levels:
        for k, v in key_levels.items():
            if v is not None:
                levels[k] = v
    if ma_levels:
        for k, v in ma_levels.items():
            if v is not None:
                levels[k] = v

    # --- daily_closes: passed in or derived from df ---
    if daily_closes is None:
        daily_closes = [float(v) for v in close] if n > 0 else []

    daily_block: dict = {
        "ohlc_df": daily_df if n > 0 else None,
        **_daily_stats,
        "volume": volume or {},
        "rsi": rsi or {},
    }

    ctx: dict = {
        "price": latest_price,
        "as_of": as_of,
        "daily": daily_block,
        "levels": levels,
        "zones": zones,
        "monthly_cpr": monthly_cpr or {},
        "key_levels": key_levels or {},
        "daily_closes": daily_closes,
        "funding_series": funding_series,
        "smart_money_bias": smart_money_bias,
        "data_stale": data_stale,
    }

    # --- Optional weekly block ---
    if weekly_df is not None and len(weekly_df) > 0:
        _w_stats = _derive_tf_stats(weekly_df)
        ctx["weekly"] = {
            "ohlc_df": weekly_df,
            **_w_stats,
            "volume": {},
            "rsi": weekly_rsi or {},
        }

    return ctx


def get_asset_facts(symbol: str, r=None) -> dict | None:
    """Read the cached facts bundle for a symbol. Returns the dict or None. Non-fatal."""
    try:
        from carrac.redis_cache import get_redis
        raw = (r or get_redis()).get(f"facts:bundle:{symbol}")
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def store_asset_facts(symbol: str, ctx: dict, r: Any | None = None) -> dict | None:
    """Build and cache the per-asset facts bundle.

    Calls build_asset_facts(symbol, ctx), serialises to JSON, and stores at
    ``facts:bundle:<symbol>`` (TTL 3600 s).  Returns the bundle dict, or None
    on any failure (non-fatal — never raises).
    """
    try:
        from carrac.facts.bundle import build_asset_facts
        from carrac.redis_cache import get_redis

        bundle = build_asset_facts(symbol, ctx)

        client = r if r is not None else get_redis()
        if client is not None:
            client.set(
                f"facts:bundle:{symbol}",
                json.dumps(bundle, default=_numpy_default, allow_nan=False),
                ex=3600,
            )
        return bundle
    except Exception as exc:  # noqa: BLE001
        log.warning("store_asset_facts for %s failed (non-fatal): %s", symbol, exc)
        return None
