"""Candle-completion helpers for the report pipeline.

The daily report is generated in the early UTC hours, so an asset's most
recent candle may still be in progress (BTC trades 24/7; equities/metals may
show the prior session). Volume-vs-average comparisons must use the last
CLOSED candle or they produce artifacts like "0.01x average".
"""
from typing import List, Dict, Any, Optional


def _time_ms(entry: Dict[str, Any]) -> Optional[int]:
    """Return the candle open time in milliseconds.

    Production series from carrac/charts/service.py store ``time`` as Unix
    seconds (~1.7B today).  Any value below 10_000_000_000 is treated as
    seconds and multiplied by 1000 (real Unix seconds stay under that until
    ~2286); values at or above it are already in ms and returned as-is.
    """
    for k in ("time", "timestamp", "t", "ts"):
        v = entry.get(k)
        if v is None:
            continue
        v = int(v)
        # Values < 10_000_000_000 are Unix seconds (covers real dates to year ~2286);
        # values >= 10_000_000_000 are already in ms.
        return v if v >= 10_000_000_000 else v * 1000  # normalize s -> ms
    return None


def last_closed_index(series: List[Dict[str, Any]], timeframe_seconds: int, now_ms: int) -> int:
    """Index of the last CLOSED candle. Falls back to len-1 when timestamps absent.

    A candle whose open time is ``t`` (ms) closes at ``t + timeframe_seconds * 1000``.
    ``now_ms`` is always in true Unix milliseconds (from ``datetime.now().timestamp()*1000``).
    Series times are normalised to ms via :func:`_time_ms`.
    """
    if not series:
        return -1
    last_t = _time_ms(series[-1])
    if last_t is None or not now_ms:
        return len(series) - 1
    closed = now_ms >= last_t + timeframe_seconds * 1000
    return len(series) - 1 if closed else len(series) - 2


def _median(vals):
    s = sorted(vals)
    n = len(s)
    return (s[n // 2] + s[(n - 1) // 2]) / 2 if n else None


def _volume_series_reliable(prior, cur, med) -> bool:
    """Some feeds (Yahoo commodity futures) interleave junk / forward-fill volume —
    near-zero placeholders mixed with real values — which corrupts any ratio.
    Treat the window as UNRELIABLE when it is junk-dominated or the last closed
    candle is wildly off the median (so callers can drop the volume claim)."""
    if not prior or not med or med <= 0:
        return False
    near_zero = sum(1 for v in prior if v < 0.05 * med)
    if near_zero / len(prior) > 0.25:        # placeholder/forward-fill dominated
        return False
    if cur < 0.1 * med or cur > 20 * med:    # last candle implausibly off → junk/partial
        return False
    return True


def volume_ratio_closed(series, timeframe_seconds, now_ms, period: int = 20) -> Dict[str, Any]:
    """Volume of the last closed candle / MEDIAN of the `period` candles before it.

    Median (not mean) so junk/forward-fill values can't skew the baseline. The
    ``reliable`` flag is False when the volume data looks corrupt — callers should
    then omit the volume claim entirely rather than cite a garbage ratio.
    """
    if not series:
        return {"ratio": None, "current_volume": None, "avg": None,
                "candle_complete": False, "used_index": -1, "reliable": False}
    idx = last_closed_index(series, timeframe_seconds, now_ms)
    complete = idx == len(series) - 1
    if idx < 0:
        idx = len(series) - 1
    cur = float(series[idx]["value"])
    prior = [float(s["value"]) for s in series[max(0, idx - period):idx]]
    avg = _median(prior)
    ratio = round(cur / avg, 2) if avg else None
    return {"ratio": ratio, "current_volume": cur, "avg": avg,
            "candle_complete": complete, "used_index": idx,
            "reliable": _volume_series_reliable(prior, cur, avg)}


_TF_SECONDS = {
    '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
    '1h': 3600, '2h': 7200, '4h': 14400,
    '1d': 86400, '1w': 604800,
}


def tf_to_seconds(tf: str, default: int = 86400) -> int:
    """Seconds per candle for a timeframe label (e.g. '4H', '1D', '1W').

    Used so the closed-candle volume check is correct for 4H/1W series too, not
    just the daily report path. Unknown labels fall back to one day.
    """
    if not tf:
        return default
    return _TF_SECONDS.get(str(tf).lower(), default)
