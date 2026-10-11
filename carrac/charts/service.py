"""Runtime-agnostic chart payload builder.

The same heavy computation can be called from both the Flask delegating
wrapper and the FastAPI ``asyncio.to_thread`` handler without touching
``flask.request``, ``flask.g``, or ``flask.session``.

Public API
----------
``build_chart_payload(symbol, *, mode, timeframe, days) -> dict``
    Computes OHLCV, volume, indicators, CPR, and extended snapshots.
    Raises ``ChartNotFoundError`` when no data is available.
    All other exceptions propagate as-is — the caller is responsible for
    wrapping in an appropriate HTTP error response.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from carrac.charts.indicators import compute_chart_indicators
from carrac.caches import (
    get_cached_subhourly as _get_cached_subhourly,
    set_cached_subhourly as _set_cached_subhourly,
)
from carrac.services import get_services

logger = logging.getLogger("carrac.charts.service")


class ChartNotFoundError(Exception):
    """Raised when no OHLCV data is available for the requested symbol/TF."""


# ---------------------------------------------------------------------------
# CPR helpers (shared with carrac.charts.routes)
# ---------------------------------------------------------------------------


def _cpr_round(value, ref_price):
    if ref_price >= 100:
        return round(value, 2)
    elif ref_price >= 1:
        return round(value, 4)
    elif ref_price >= 0.01:
        return round(value, 6)
    else:
        return round(value, 8)


def _cpr_clamp_negative(value):
    """Return None for negative S/R levels — they're meaningless for price
    and cause KLineChart to auto-scale Y-axis into negative territory."""
    return value if value is not None and value > 0 else None


def _cpr_supports_with_logging(symbol, s1_raw, s2_raw, s3_raw, *,
                               prev_high, prev_low, prev_close, pivot):
    """Clamp non-positive CPR support levels to ``None`` and, when any level
    is actually clamped, emit ONE warning per payload build.

    A support level <= 0 is not a normal price geometry — it implies the
    previous-period H/L/C the pivot was built from is distorted (e.g. a
    corrupted synthetic-index summed series). The clamp is logged with the
    symbol, which levels were clamped, and the inputs so the upstream
    distortion is diagnosable.
    """
    raw = {"s1": s1_raw, "s2": s2_raw, "s3": s3_raw}
    out = {}
    clamped = {}
    for name, val in raw.items():
        c = _cpr_clamp_negative(val)
        out[name] = c
        if c is None and val is not None:
            clamped[name] = val
    if clamped:
        logger.warning(
            "CPR support(s) clamped to None for %s: %s "
            "(prevHigh=%s prevLow=%s prevClose=%s pivot=%s) — non-positive "
            "support implies a distorted previous-period series",
            symbol,
            ", ".join(f"{k}={v}" for k, v in clamped.items()),
            prev_high, prev_low, prev_close, pivot,
        )
    return out


def _calculate_cpr_history(daily_df, num_months=5):
    if daily_df.empty or len(daily_df) < 60:
        return []
    last_date = daily_df.index[-1]
    if not hasattr(last_date, "month"):
        last_date = pd.Timestamp(last_date)
    cur_year, cur_month = last_date.year, last_date.month
    hist_y = cur_year - 1 if cur_month == 1 else cur_year
    hist_m = 12 if cur_month == 1 else cur_month - 1

    history = []
    for _ in range(num_months):
        src_y = hist_y - 1 if hist_m == 1 else hist_y
        src_m = 12 if hist_m == 1 else hist_m - 1
        src_bars = daily_df[
            (daily_df.index.year == src_y) & (daily_df.index.month == src_m)
        ]
        apply_bars = daily_df[
            (daily_df.index.year == hist_y) & (daily_df.index.month == hist_m)
        ]
        if len(src_bars) > 0 and len(apply_bars) > 0:
            h = float(src_bars["high"].max())
            l = float(src_bars["low"].min())
            c = float(src_bars.iloc[-1]["close"])
            p = (h + l + c) / 3
            bc = (h + l) / 2
            tc = p * 2 - bc
            history.append(
                {
                    "pivot": _cpr_round(p, c),
                    "tc": _cpr_round(tc, c),
                    "bc": _cpr_round(bc, c),
                    "periodStart": int(apply_bars.index[0].timestamp()),
                    "periodEnd": int(apply_bars.index[-1].timestamp()),
                }
            )
        hist_y = hist_y - 1 if hist_m == 1 else hist_y
        hist_m = 12 if hist_m == 1 else hist_m - 1
    return history


def _calculate_weekly_cpr_history(daily_df, num_weeks=52):
    if daily_df.empty or len(daily_df) < 20:
        return []
    df = daily_df.copy()
    df["iso_week"] = df.index.isocalendar().week.astype(int) + df.index.year * 100
    weeks = sorted(df["iso_week"].unique())
    if len(weeks) < 3:
        return []
    history = []
    for i in range(len(weeks) - 2, max(len(weeks) - 2 - num_weeks, 0), -1):
        src_week = weeks[i - 1] if i > 0 else None
        apply_week = weeks[i]
        if src_week is None:
            continue
        src_bars = df[df["iso_week"] == src_week]
        apply_bars = df[df["iso_week"] == apply_week]
        if len(src_bars) > 0 and len(apply_bars) > 0:
            h, l, c = (
                float(src_bars["high"].max()),
                float(src_bars["low"].min()),
                float(src_bars.iloc[-1]["close"]),
            )
            p = (h + l + c) / 3
            bc = (h + l) / 2
            tc = p * 2 - bc
            history.append(
                {
                    "pivot": _cpr_round(p, c),
                    "tc": _cpr_round(tc, c),
                    "bc": _cpr_round(bc, c),
                    "periodStart": int(apply_bars.index[0].timestamp()),
                    "periodEnd": int(apply_bars.index[-1].timestamp()),
                }
            )
    return history


def _calculate_daily_cpr_history(daily_df, num_days=60):
    if daily_df.empty or len(daily_df) < 3:
        return []
    history = []
    end = len(daily_df) - 1
    start = max(1, end - num_days)
    for i in range(end, start, -1):
        src = daily_df.iloc[i - 1]
        apply_bar = daily_df.iloc[i]
        h, l, c = float(src["high"]), float(src["low"]), float(src["close"])
        p = (h + l + c) / 3
        bc = (h + l) / 2
        tc = p * 2 - bc
        history.append(
            {
                "pivot": _cpr_round(p, c),
                "tc": _cpr_round(tc, c),
                "bc": _cpr_round(bc, c),
                "periodStart": int(apply_bar.name.timestamp()),
                "periodEnd": int(apply_bar.name.timestamp()) + 86400,
            }
        )
    return history


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_chart_payload(
    symbol: str,
    *,
    mode: str = "htf",
    timeframe: Optional[str] = None,
    days: Optional[int] = None,
) -> dict:
    """Build the full chart JSON payload for ``symbol``.

    Parameters
    ----------
    symbol:
        Raw symbol string (will be uppercased internally).
    mode:
        ``'htf'`` (default) or ``'ltf'`` — selects the timeframe when
        ``timeframe`` is not specified.
    timeframe:
        Explicit timeframe string (``'1D'``, ``'4h'``, ``'5m'``, …).
        ``None`` lets ``mode`` decide.
    days:
        Maximum number of bars to return. ``None`` uses the per-TF default
        (25 000 for LTF, 5 000 for HTF).

    Returns
    -------
    dict
        Ready-to-serialize chart payload. Caller wraps in ``JSONResponse`` or
        ``jsonify``.

    Raises
    ------
    ChartNotFoundError
        When no OHLCV data is available for the requested symbol/timeframe.
    Exception
        Any other exception propagates — caller is responsible for 500 handling.
    """
    services = get_services()
    db = services.db
    rsi_calc = services.rsi_calc

    symbol_upper = symbol.upper()

    # ── Ratio symbols (BTC_SPX) ──────────────────────────────────────────
    if "_" in symbol_upper:
        parts = symbol_upper.split("_", 1)
        if len(parts) == 2:
            from carrac.charts.ratios import _build_ratio_df

            tf = timeframe or "1D"
            tf_map = {"1D": "1D", "3D": "3D", "1W": "1W", "1M": "1M"}
            tf = tf_map.get(tf.upper(), "1D")
            _days = days if days is not None else 1500
            ratio_df = _build_ratio_df(parts[0], parts[1], tf)
            if ratio_df.empty:
                raise ChartNotFoundError(f"No {tf} data for {parts[0]}/{parts[1]}")
            ratio_df = ratio_df.tail(_days)

            ohlcv = []
            for idx in ratio_df.index:
                row = ratio_df.loc[idx]
                ts = (
                    int(idx.timestamp())
                    if hasattr(idx, "timestamp")
                    else int(pd.Timestamp(idx).timestamp())
                )
                ohlcv.append(
                    {
                        "time": ts,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                    }
                )

            computed = compute_chart_indicators(
                ratio_df, None, symbol_upper, rsi_calc=None
            )
            from carrac.charts.indicators_extended import compute_indicator_snapshot

            try:
                indicators_ext = compute_indicator_snapshot(ratio_df, tf=tf)
            except Exception as ext_err:
                logger.warning(
                    "Extended indicators error for ratio %s: %s", symbol_upper, ext_err
                )
                indicators_ext = {}

            return {
                "ohlcv": ohlcv,
                "volume": [],
                "rsi": computed["rsi"],
                "macd": computed["macd"],
                "symbol": symbol_upper,
                "timeframe": tf,
                "cpr": None,
                "weekly_cpr": None,
                "daily_cpr": None,
                "indicators": computed["indicators"],
                "indicators_ext": indicators_ext,
            }

    # ── Standard symbol ──────────────────────────────────────────────────
    is_crypto = db._is_crypto(symbol_upper)

    requested_tf = timeframe
    tf_normalized: Optional[str] = None
    if requested_tf:
        tf_normalized = (
            requested_tf
            if requested_tf in ["1D", "3D", "1W", "1M"]
            else requested_tf.lower()
        )

    is_ltf = (
        tf_normalized in ["1m", "3m", "5m", "15m", "30m", "1h", "4h"]
        if tf_normalized
        else (mode == "ltf")
    )
    max_bars = 25000 if is_ltf else 5000
    _days = min(days if days is not None else max_bars, max_bars)

    ltf_data = None
    tf_data = None

    if requested_tf:
        if tf_normalized in ["1m", "3m", "5m", "15m", "30m"]:
            if is_crypto:
                df = _get_cached_subhourly(symbol_upper, tf_normalized)
                if df is None:
                    df = db.fetch_klines_for_chart(
                        symbol_upper, interval=tf_normalized, bars=_days
                    )
                    if not df.empty:
                        _set_cached_subhourly(symbol_upper, tf_normalized, df)
                actual_tf = tf_normalized
            else:
                tf_data = db.get_multi_timeframe_data(symbol_upper)
                df = tf_data.get("1D", pd.DataFrame())
                actual_tf = "1D"
        elif tf_normalized in ["1h", "4h"]:
            ltf_data = db.get_ltf_multi_timeframe_data(symbol_upper)
            df = ltf_data.get(tf_normalized, pd.DataFrame())
            actual_tf = tf_normalized
            tf_data = db.get_multi_timeframe_data(symbol_upper)
        else:
            tf_data = db.get_multi_timeframe_data(symbol_upper)
            df = tf_data.get(tf_normalized, pd.DataFrame())
            actual_tf = tf_normalized
    elif mode == "ltf" and is_crypto:
        ltf_data = db.get_ltf_multi_timeframe_data(symbol_upper)
        df = ltf_data.get("4h", pd.DataFrame())
        actual_tf = "4H"
        tf_data = db.get_multi_timeframe_data(symbol_upper)
    else:
        tf_data = db.get_multi_timeframe_data(symbol_upper)
        df = tf_data.get("1D", pd.DataFrame())
        actual_tf = "1D"

    if df is None or df.empty:
        raise ChartNotFoundError(f"No data found for {symbol}")

    df = df.tail(_days)

    # ── Build OHLCV + volume lists (vectorized — no iterrows) ────────────
    # Convert DatetimeIndex → int seconds via numpy (O(N) numpy instead of
    # O(N) Python loop). Output types are identical to the iterrows version:
    # int timestamps, float OHLC/volume — the frontend relies on these types.
    timestamps = df.index.astype("datetime64[s]").astype("int64").tolist()
    opens  = df["open"].astype(float).tolist()
    highs  = df["high"].astype(float).tolist()
    lows   = df["low"].astype(float).tolist()
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
        vol_vals  = df["volume"].astype(float).tolist()
        vol_notna = df["volume"].notna().tolist()
        up_color   = "rgba(38, 166, 154, 0.5)"
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

    # ── Monthly CPR ───────────────────────────────────────────────────────
    cpr_data = None
    try:
        if tf_data and not tf_data.get("1D", pd.DataFrame()).empty:
            daily_df = tf_data["1D"]
        else:
            daily_tf = db.get_multi_timeframe_data(symbol_upper)
            daily_df = (
                daily_tf.get("1D", pd.DataFrame()) if daily_tf else pd.DataFrame()
            )

        if not daily_df.empty and len(daily_df) >= 32:
            last_date = daily_df.index[-1]
            if not hasattr(last_date, "month"):
                last_date = pd.Timestamp(last_date)
            cur_year, cur_month = last_date.year, last_date.month
            prev_year, prev_month = (
                (cur_year - 1, 12) if cur_month == 1 else (cur_year, cur_month - 1)
            )

            prev_bars = daily_df[
                (daily_df.index.year == prev_year)
                & (daily_df.index.month == prev_month)
            ]
            if len(prev_bars) > 0:
                prev_high = float(prev_bars["high"].max())
                prev_low = float(prev_bars["low"].min())
                prev_close = float(prev_bars.iloc[-1]["close"])
                pivot = (prev_high + prev_low + prev_close) / 3
                bc = (prev_high + prev_low) / 2
                tc = pivot * 2 - bc
                cur_bars = daily_df[
                    (daily_df.index.year == cur_year)
                    & (daily_df.index.month == cur_month)
                ]
                if len(cur_bars) > 0:
                    period_start = int(cur_bars.index[0].timestamp())
                else:
                    period_start = int(
                        pd.Timestamp(
                            year=cur_year, month=cur_month, day=1
                        ).timestamp()
                    )
                _r = lambda v: _cpr_round(v, prev_close)
                _supports = _cpr_supports_with_logging(
                    symbol_upper,
                    _r(pivot * 2 - prev_high),
                    _r(pivot - (prev_high - prev_low)),
                    _r(pivot * 2 - (2 * prev_high - prev_low)),
                    prev_high=prev_high, prev_low=prev_low,
                    prev_close=prev_close, pivot=pivot,
                )
                cpr_data = {
                    "pivot": _r(pivot),
                    "tc": _r(tc),
                    "bc": _r(bc),
                    "r1": _r(pivot * 2 - prev_low),
                    "r2": _r(pivot + (prev_high - prev_low)),
                    "r3": _r(pivot * 2 + (prev_high - 2 * prev_low)),
                    "s1": _supports["s1"],
                    "s2": _supports["s2"],
                    "s3": _supports["s3"],
                    "prevHigh": _r(prev_high),
                    "prevLow": _r(prev_low),
                    "prevClose": _r(prev_close),
                    "periodStart": period_start,
                }
                cpr_history = _calculate_cpr_history(daily_df, 48)
                if cpr_history:
                    cpr_data["history"] = cpr_history
    except Exception as cpr_err:
        logger.warning("CPR calculation error for %s: %s", symbol, cpr_err)

    # ── Weekly + Daily CPR ────────────────────────────────────────────────
    weekly_cpr_data = None
    daily_cpr_data = None
    try:
        _daily = tf_data.get("1D", pd.DataFrame()) if tf_data else pd.DataFrame()
        if not _daily.empty:
            from consensio.indicators.cpr import CPRCalculator as _CprCalc

            _cpr = _CprCalc()
            _w = _cpr.calculate_weekly_cpr(_daily)
            if _w:
                _w["history"] = _calculate_weekly_cpr_history(_daily, 52)
                weekly_cpr_data = _w
            _d = _cpr.calculate_daily_cpr(_daily)
            if _d:
                _d["history"] = _calculate_daily_cpr_history(_daily, 60)
                daily_cpr_data = _d
    except Exception:
        pass

    # ── Indicators ────────────────────────────────────────────────────────
    computed = compute_chart_indicators(
        df, tf_data, symbol_upper, rsi_calc=rsi_calc
    )
    indicators = computed["indicators"]
    rsi_data = computed["rsi"]
    macd_data = computed["macd"]

    # ── Extended snapshot ─────────────────────────────────────────────────
    from carrac.charts.indicators_extended import compute_indicator_snapshot

    try:
        indicators_ext = compute_indicator_snapshot(df, tf=actual_tf)
    except Exception as ext_err:
        logger.warning(
            "Extended indicators error for %s: %s", symbol_upper, ext_err
        )
        indicators_ext = {}

    from carrac.caches import sanitize_nonfinite

    # Non-finite floats (NaN/Inf) are invalid JSON and hard-crash the
    # frontend's JSON.parse once cached — degrade them to null here, the
    # one builder every chart route and the cache warmer flow through.
    return sanitize_nonfinite({
        "ohlcv": ohlcv,
        "volume": volume,
        "rsi": rsi_data,
        "macd": macd_data,
        "symbol": symbol_upper,
        "timeframe": actual_tf,
        "cpr": cpr_data,
        "weekly_cpr": weekly_cpr_data,
        "daily_cpr": daily_cpr_data,
        "indicators": indicators,
        "indicators_ext": indicators_ext,
    })


__all__ = ["build_chart_payload", "ChartNotFoundError"]
