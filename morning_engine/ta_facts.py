"""Pure, deterministic TA-fact checks for the morning engine facts bundle.

All functions are side-effect-free: no I/O, no datetime.now, no randomness.
Same inputs always produce the same output.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _tol(atr14: float, price: float) -> float:
    """Tolerance band: scales with volatility, floored at 0.25% of price."""
    return max(0.3 * atr14, 0.0025 * price)


# ---------------------------------------------------------------------------
# Function 1 — level_streaks
# ---------------------------------------------------------------------------

def _bar_interaction(row_high: float, row_low: float, row_close: float,
                     level: float, tol: float, is_support: bool) -> str | None:
    """Classify a single bar's interaction with a level.

    Returns one of "rejecting", "testing", "defending"/"capped", or None
    (no interaction / clean break).
    """
    if is_support:
        # No interaction: never came near OR decisively broke below
        if row_low > level + tol or row_close < level - tol:
            return None
        # Rejecting: wicked below AND reclaimed
        if row_low < level - tol and row_close > level + tol:
            return "rejecting"
        # Testing: closed inside the band
        if abs(row_close - level) <= tol:
            return "testing"
        # Defending: touched band but closed cleanly above
        if row_close > level + tol:
            return "defending"
    else:
        # Resistance
        # No interaction: never came near OR decisively broke above
        if row_high < level - tol or row_close > level + tol:
            return None
        # Rejecting: wicked above AND closed back below
        if row_high > level + tol and row_close < level - tol:
            return "rejecting"
        # Testing: closed inside the band
        if abs(row_close - level) <= tol:
            return "testing"
        # Capped: touched band but closed cleanly below
        if row_close < level - tol:
            return "capped"
    return None


def level_streaks(ohlc_df: pd.DataFrame, levels: dict, atr14: float,
                  price: float) -> list[dict]:
    """Count consecutive interacting bars (newest→oldest) for each level.

    Returns a list of dicts (one per level with streak >= 2):
        {"level": name, "price": level_price, "bars": n, "type": newest_type}

    Parameters
    ----------
    ohlc_df : DataFrame with columns high, low, close. Oldest row first.
    levels  : {name: price}. None values are skipped.
    atr14   : 14-period ATR for tolerance calculation.
    price   : current price.
    """
    if ohlc_df is None or ohlc_df.empty:
        return []

    results = []
    highs = ohlc_df["high"].tolist()
    lows = ohlc_df["low"].tolist()
    closes = ohlc_df["close"].tolist()
    n_bars = len(closes)

    for name, lvl in levels.items():
        if lvl is None:
            continue
        tol = _tol(atr14, price)
        is_support = lvl <= price

        # Walk newest → oldest
        streak = 0
        newest_type: str | None = None
        for i in range(n_bars - 1, -1, -1):
            interaction = _bar_interaction(
                highs[i], lows[i], closes[i], lvl, tol, is_support
            )
            if interaction is None:
                break
            streak += 1
            if newest_type is None:
                newest_type = interaction

        if streak >= 2:
            results.append({
                "level": name,
                "price": lvl,
                "bars": streak,
                "type": newest_type,
            })

    return results


# ---------------------------------------------------------------------------
# Function 2 — decisive_closes
# ---------------------------------------------------------------------------

def decisive_closes(ohlc_df: pd.DataFrame, levels: dict, atr14: float,
                    price: float) -> list[dict]:
    """Identify levels that the latest close has decisively moved beyond.

    For each level, the latest close must be outside the tolerance band.
    Returns dicts: {"level": name, "direction": "above"/"below",
                    "confirmed": True, "bars_ago": n}

    bars_ago = count of consecutive most-recent bars whose close is on
    the same side as the latest close (stopping at first bar on the other side).
    """
    if ohlc_df is None or ohlc_df.empty:
        return []

    closes = ohlc_df["close"].tolist()
    n_bars = len(closes)
    results = []

    for name, lvl in levels.items():
        if lvl is None:
            continue
        tol = _tol(atr14, price)
        c = closes[-1]

        # Latest close is inside the tolerance band → skip
        if abs(c - lvl) <= tol:
            continue

        direction = "above" if c > lvl else "below"

        # Count consecutive bars on the same side (newest→oldest)
        bars_ago = 0
        for i in range(n_bars - 1, -1, -1):
            bar_close = closes[i]
            if direction == "above" and bar_close > lvl:
                bars_ago += 1
            elif direction == "below" and bar_close < lvl:
                bars_ago += 1
            else:
                break

        results.append({
            "level": name,
            "direction": direction,
            "confirmed": True,
            "bars_ago": bars_ago,
        })

    return results


# ---------------------------------------------------------------------------
# Function 3 — candle_geometry
# ---------------------------------------------------------------------------

def candle_geometry(o: float, h: float, l: float, c: float,
                    prev_close: float | None = None) -> dict:
    """Return pure OHLC geometry metrics and a pattern label.

    Pattern evaluation order (first match wins):
      1. marubozu  — body_pct > 0.9
      2. hammer    — lower_wick_ratio >= 2, upper_wick_ratio <= 0.5, close_in_range >= 0.6
      3. inverted_hammer — upper_wick_ratio >= 2, lower_wick_ratio <= 0.5, close_in_range <= 0.4
      4. doji      — body_pct < 0.1
      5. None
    """
    rng = h - l
    if rng <= 0:
        return {
            "body": 0.0,
            "body_pct": 0.0,
            "upper_wick": 0.0,
            "lower_wick": 0.0,
            "upper_wick_ratio": 0.0,
            "lower_wick_ratio": 0.0,
            "close_in_range": 0.0,
            "gap_pct": 0.0,
            "pattern_label": None,
        }

    body = abs(c - o)
    body_pct = body / rng
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    denom = body if body > 0 else rng
    upper_wick_ratio = upper_wick / denom
    lower_wick_ratio = lower_wick / denom
    close_in_range = (c - l) / rng
    gap_pct = ((o - prev_close) / prev_close * 100) if prev_close else 0.0

    # Pattern label — first match wins
    if body_pct > 0.9:
        pattern_label = "marubozu"
    elif lower_wick_ratio >= 2 and upper_wick_ratio <= 0.5 and close_in_range >= 0.6:
        pattern_label = "hammer"
    elif upper_wick_ratio >= 2 and lower_wick_ratio <= 0.5 and close_in_range <= 0.4:
        pattern_label = "inverted_hammer"
    elif body_pct < 0.1:
        pattern_label = "doji"
    else:
        pattern_label = None

    return {
        "body": round(body, 6),
        "body_pct": round(body_pct, 6),
        "upper_wick": round(upper_wick, 6),
        "lower_wick": round(lower_wick, 6),
        "upper_wick_ratio": round(upper_wick_ratio, 6),
        "lower_wick_ratio": round(lower_wick_ratio, 6),
        "close_in_range": round(close_in_range, 6),
        "gap_pct": round(gap_pct, 6),
        "pattern_label": pattern_label,
    }


# ---------------------------------------------------------------------------
# Function 4 — cluster_distances
# ---------------------------------------------------------------------------

def cluster_distances(zones: Any, price: float) -> dict:
    """Find nearest support/resistance zones and their distances from price.

    Parameters
    ----------
    zones : dict with "supports" / "resistances" keys, or "all" key,
            or a bare list (items with price < current → support).
    price : current price.

    Returns
    -------
    {
      "nearest_support_dist_pct": float | None,
      "nearest_resistance_dist_pct": float | None,
      "clusters": [{"price", "side", "members", "count", "dist_pct"}, ...]
                  sorted by abs(dist_pct) ascending
    }
    """
    supports: list[dict] = []
    resistances: list[dict] = []

    if isinstance(zones, list):
        for z in zones:
            zp = z.get("price") if isinstance(z, dict) else float(z)
            if zp is None:
                continue
            if zp < price:
                supports.append(z if isinstance(z, dict) else {"price": zp})
            elif zp > price:
                resistances.append(z if isinstance(z, dict) else {"price": zp})
    elif isinstance(zones, dict):
        if "all" in zones:
            for z in zones["all"]:
                zp = z.get("price") if isinstance(z, dict) else float(z)
                if zp is None:
                    continue
                if zp < price:
                    supports.append(z if isinstance(z, dict) else {"price": zp})
                elif zp > price:
                    resistances.append(z if isinstance(z, dict) else {"price": zp})
        else:
            supports = [z for z in zones.get("supports", []) if z.get("price") is not None]
            resistances = [z for z in zones.get("resistances", []) if z.get("price") is not None]

    # Nearest support = largest price strictly below current price
    below = [z for z in supports if z.get("price", 0) < price]
    nearest_sup = max(below, key=lambda z: z["price"]) if below else None

    # Nearest resistance = smallest price strictly above current price
    above = [z for z in resistances if z.get("price", 0) > price]
    nearest_res = min(above, key=lambda z: z["price"]) if above else None

    def _zone_entry(z: dict, side: str) -> dict:
        zp = z["price"]
        dist_pct = round((zp - price) / price * 100, 4)
        # compute_confluences emits members as list-of-dicts {code,name,...};
        # coerce to a plain list of code strings so "+".join(members) works downstream.
        raw = z.get("members", []) or []
        members = [(m.get("code") if isinstance(m, dict) else m) for m in raw]
        members = [m for m in members if m]   # drop None / empty strings
        return {
            "price": zp,
            "side": side,
            "members": members,
            "count": z.get("count", len(raw) or 1),
            "dist_pct": dist_pct,
        }

    sup_dist = None
    res_dist = None
    clusters = []

    if nearest_sup is not None:
        entry = _zone_entry(nearest_sup, "support")
        sup_dist = entry["dist_pct"]
        clusters.append(entry)

    if nearest_res is not None:
        entry = _zone_entry(nearest_res, "resistance")
        res_dist = entry["dist_pct"]
        clusters.append(entry)

    clusters.sort(key=lambda e: abs(e["dist_pct"]))

    return {
        "nearest_support_dist_pct": sup_dist,
        "nearest_resistance_dist_pct": res_dist,
        "clusters": clusters,
    }


# ---------------------------------------------------------------------------
# Function 5 — ma_stack
# ---------------------------------------------------------------------------

def _stack_state_at(price_val: float, sma20_val: float | None,
                    sma50_val: float | None, sma200_val: float | None) -> str:
    """Compute stack state for a single bar's values.

    Returns "insufficient" when any MA is None (not enough history to classify).
    """
    if any(v is None for v in (sma20_val, sma50_val, sma200_val)):
        return "insufficient"

    if price_val > sma20_val > sma50_val > sma200_val:
        return "bullish"
    if price_val < sma20_val < sma50_val < sma200_val:
        return "bearish"
    if price_val > sma200_val:
        return "bullish_partial"
    return "bearish_partial"


def ma_stack(close_series, sma20_series, sma50_series, sma200_series) -> dict:
    """Characterise the MA stack alignment and how long it has held.

    Parameters
    ----------
    close_series, sma20_series, sma50_series, sma200_series :
        list or pd.Series, oldest→newest, same length.

    Returns
    -------
    {"state": str, "bars_held": int, "cross": str}
    """
    def _to_list(s) -> list:
        if s is None:
            return []
        if isinstance(s, pd.Series):
            return s.tolist()
        return list(s)

    closes = _to_list(close_series)
    sma20s = _to_list(sma20_series)
    sma50s = _to_list(sma50_series)
    sma200s = _to_list(sma200_series)

    n = len(closes)
    if n == 0:
        return {"state": "bearish_partial", "bars_held": 0, "cross": "none"}

    # Helper: safe index into a series (returns None if out of range or None value)
    def _get(series: list, idx: int) -> float | None:
        if idx < 0 or idx >= len(series):
            return None
        v = series[idx]
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)

    last_price = _get(closes, n - 1)
    last_sma20 = _get(sma20s, n - 1)
    last_sma50 = _get(sma50s, n - 1)
    last_sma200 = _get(sma200s, n - 1)

    current_state = _stack_state_at(last_price, last_sma20, last_sma50, last_sma200)

    # cross: sma50 vs sma200 on last two bars (computed before any early return)
    cross = "none"
    if n >= 2:
        s50_prev = _get(sma50s, n - 2)
        s200_prev = _get(sma200s, n - 2)
        s50_cur = last_sma50
        s200_cur = last_sma200
        if all(v is not None for v in (s50_prev, s200_prev, s50_cur, s200_cur)):
            if s50_prev <= s200_prev and s50_cur > s200_cur:
                cross = "golden_cross"
            elif s50_prev >= s200_prev and s50_cur < s200_cur:
                cross = "death_cross"

    # Insufficient: not enough MA history to classify the stack direction
    if current_state == "insufficient":
        return {"state": "insufficient", "bars_held": 0, "cross": cross}

    # bars_held: walk newest→oldest counting bars in the same state.
    # "insufficient" bars (None MAs) break the streak just like any other mismatch.
    bars_held = 0
    for i in range(n - 1, -1, -1):
        p = _get(closes, i)
        m20 = _get(sma20s, i)
        m50 = _get(sma50s, i)
        m200 = _get(sma200s, i)
        if p is None:
            break
        state_i = _stack_state_at(p, m20, m50, m200)
        if state_i != current_state:
            break
        bars_held += 1

    return {"state": current_state, "bars_held": bars_held, "cross": cross}


# ---------------------------------------------------------------------------
# Function — price_percentile
# ---------------------------------------------------------------------------

def funding_streak(funding_series: list[float], window_days: int = 90) -> dict | None:
    """Funding streak + percentile rank.

    Parameters
    ----------
    funding_series : daily funding rate values, oldest→newest.
    window_days    : max look-back for the percentile comparison window (default 90).

    Returns
    -------
    {"streak_days": int, "sign": str, "latest": float, "percentile": int, "window_days": int}
    or None when there are fewer than 2 usable values or the latest value is 0.

    Pure and deterministic. Wraps body in try/except → None on any error.
    """
    try:
        # Filter to usable finite numeric values (drop None, NaN, ±inf, non-numeric).
        # Guarding against ±inf prevents json.dumps(allow_nan=False) from failing
        # when the bundle is serialised in store_asset_facts.
        usable = [
            v for v in (funding_series or [])
            if isinstance(v, (int, float)) and math.isfinite(v)
        ]

        if len(usable) < 2:
            return None

        latest = usable[-1]

        if latest == 0:
            return None

        sign = "positive" if latest > 0 else "negative"

        # Streak: walk newest → oldest counting consecutive same-sign values.
        # A zero or opposite-sign value breaks the streak.
        streak_days = 0
        for v in reversed(usable):
            if (sign == "positive" and v > 0) or (sign == "negative" and v < 0):
                streak_days += 1
            else:
                break

        # Percentile: rank latest within the preceding usable values, capped to window_days.
        preceding = usable[:-1]  # all values before latest
        window = preceding[-window_days:] if len(preceding) > window_days else preceding
        n = len(window)

        if n == 0:
            return None

        count_le = sum(1 for v in window if v <= latest)
        pct = count_le / n * 100
        percentile = max(0, min(100, round(pct)))

        return {
            "streak_days": streak_days,
            "sign": sign,
            "latest": float(latest),
            "percentile": percentile,
            "window_days": n,
        }
    except Exception:
        return None


def price_percentile(daily_closes: list[float], window: int = 90) -> int | None:
    """Percentile rank (0-100, int) of the LATEST close within the trailing
    ``window`` closes.  Returns ``None`` if fewer than ``window + 1`` usable
    closes are available.

    Convention: the LATEST close is ranked against the PRECEDING ``window``
    closes (the latest itself is EXCLUDED from the comparison window).

    Percentile = (count of window values ≤ latest) / window * 100, rounded
    to int, clamped 0..100.

    Parameters
    ----------
    daily_closes : list of floats, oldest→newest.  Non-numeric entries are
                   silently filtered out before the length check.
    window       : look-back length (default 90 calendar days ≈ 90 daily bars).
    """
    try:
        if not daily_closes:
            return None
        # Filter to usable numeric values only (keeps int/float, drops None/str/…)
        usable = [c for c in daily_closes if isinstance(c, (int, float)) and not (isinstance(c, float) and c != c)]
        if len(usable) < window + 1:
            return None
        window_vals = usable[-(window + 1):-1]   # the `window` closes before the latest
        latest = usable[-1]
        count_le = sum(1 for v in window_vals if v <= latest)
        pct = count_le / window * 100
        return max(0, min(100, round(pct)))
    except Exception:
        return None
