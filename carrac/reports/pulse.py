"""
pulse.py — build_pulse_payload() assembler for /report/pulse cockpit.

Reads already-cached data server-side (htf precompute cache, breadth,
regime, MOVE/bond_vol, RS rankings, calendar) and returns a compact dict
that the template inlines as JSON.  Each panel is wrapped in _safe() so
a cold/missing cache yields None for that panel instead of failing the
page.

Pure helpers (trend_label, nearest_cpr_level) are the shared formatters
used both here and mirrored in the JS gloss map (§14 principle).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared formatters  (§14: numbers + plain-English translation)
# ---------------------------------------------------------------------------

_TREND_MAP = {
    3: "strong up",
    2: "up",
    1: "mild up",
    0: "flat",
    -1: "mild down",
    -2: "down",
    -3: "strong down",
}

_CPR_KEYS = ("pivot", "tc", "bc", "s1", "s2", "s3", "r1", "r2", "r3")


def trend_label(signal) -> str:
    """Map a consensio signal (−3…+3) to a plain-English direction label."""
    try:
        s = int(signal or 0)
    except (TypeError, ValueError):
        s = 0
    return _TREND_MAP.get(s, "flat")


def nearest_cpr_level(price, cpr) -> dict | None:
    """Return the CPR level nearest to *price*.

    Returns a dict with keys:
        level_name   — e.g. "pivot", "r1", "s2"
        price        — the level's price value
        distance_pct — signed % distance from price to level
                       positive = price above level, negative = below
        side         — "support" if level is at or below price,
                       "resistance" if above
    Returns None if price or cpr is falsy / no valid levels found.
    """
    if not price or not cpr:
        return None
    cands = [(k, float(cpr[k])) for k in _CPR_KEYS if cpr.get(k)]
    if not cands:
        return None
    name, lvl = min(cands, key=lambda kv: abs(kv[1] - float(price)))
    dist_pct = (float(price) - lvl) / lvl * 100.0
    return {
        "level_name": name,
        "price": lvl,
        "distance_pct": round(dist_pct, 2),
        "side": "support" if lvl <= float(price) else "resistance",
    }


# ---------------------------------------------------------------------------
# Panel builders  (each wrapped by _safe in build_pulse_payload)
# ---------------------------------------------------------------------------

def _safe(fn, label):
    """Call fn(), returning None + a warning on any exception."""
    try:
        return fn()
    except Exception as exc:
        log.warning("pulse panel %s failed: %s", label, exc)
        return None


def _pick_biggest_mover(rankings, general_symbols):
    """Largest abs(7d return) among GENERAL (registry) assets only.

    The RS benchmark set includes watchlist-only symbols (e.g. TSLA) which
    must never headline the shared Pulse cockpit — only registry chart assets
    are eligible for the 'biggest mover' tile. Returns {symbol, ret_7d} or None.
    """
    gen = {str(s).upper() for s in (general_symbols or ())}
    pool = [r for r in (rankings or [])
            if str(r.get("symbol", "")).upper() in gen]
    if not pool:
        return None
    bm = max(pool, key=lambda r: abs(r.get("ret_7d") or 0))
    return {"symbol": bm.get("symbol"), "ret_7d": bm.get("ret_7d")}


def _build_weather() -> dict | None:
    """Regime · Breadth · MOVE · Fear&Greed · Biggest mover."""
    from carrac.caches import get_cached_regime
    from carrac.market.breadth import get_breadth
    from quant.api.routes import build_snapshot

    regime_data = get_cached_regime() or {}
    breadth_data = get_breadth() or {}
    snapshot = build_snapshot("BTC") or {}
    bond_vol = snapshot.get("bond_vol") or {}

    # Fear & Greed from snapshot sentiment
    sentiment = snapshot.get("sentiment") or {}
    fg_value = None
    fg_class = None
    for k, v in sentiment.items():
        if "fear" in k.lower() and "greed" in k.lower():
            fg_value = v.get("value")
            break
    # Also check macro for fear/greed
    if fg_value is None:
        macro = snapshot.get("macro") or {}
        for k, v in macro.items():
            if "fear" in k.lower() and "greed" in k.lower():
                fg_value = v.get("value")
                break

    # Fear & Greed classification from value
    if fg_value is not None:
        try:
            fv = float(fg_value)
            if fv <= 25:
                fg_class = "Extreme Fear"
            elif fv <= 45:
                fg_class = "Fear"
            elif fv <= 55:
                fg_class = "Neutral"
            elif fv <= 75:
                fg_class = "Greed"
            else:
                fg_class = "Extreme Greed"
        except (TypeError, ValueError):
            pass

    # Biggest mover from RS rankings (max abs(ret_7d)) — GENERAL universe only.
    # The RS benchmark set includes watchlist-only symbols (e.g. TSLA) that
    # must not headline the shared cockpit; restrict to registry chart assets.
    biggest_mover = None
    try:
        from carrac.charts.routes import _compute_rs_rankings
        from carrac.assets.registry import chart_pairs
        general = {p.split("/")[0] for p in chart_pairs()}
        biggest_mover = _pick_biggest_mover(_compute_rs_rankings() or [], general)
    except Exception as exc:
        log.debug("pulse: biggest_mover failed: %s", exc)

    return {
        "regime": {
            "label": regime_data.get("regime"),
            "score": regime_data.get("score"),
            "color": regime_data.get("color"),
            # §14 gloss
            "gloss": _regime_gloss(regime_data.get("regime")),
        },
        "breadth": {
            "label": breadth_data.get("label"),
            "oversold_pct": _nested(breadth_data, "oversold", "pct"),
            "overbought_pct": _nested(breadth_data, "overbought", "pct"),
            "above_sma50_pct": _nested(breadth_data, "above_sma50", "pct"),
            # §14 gloss
            "gloss": _breadth_gloss(breadth_data),
        },
        "bond_vol": {
            "label": bond_vol.get("label"),
            "level": bond_vol.get("level"),
            "percentile": bond_vol.get("percentile"),
            # §14 gloss
            "gloss": _move_gloss(bond_vol),
        },
        "fear_greed": {
            "value": fg_value,
            "classification": fg_class,
        },
        "biggest_mover": biggest_mover,
    }


def _nested(d, *keys):
    """Safe nested dict access."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _regime_gloss(regime: str | None) -> str:
    """Plain-English translation of a regime label."""
    if not regime:
        return ""
    rl = regime.lower()
    if "risk-on" in rl or "risk on" in rl:
        return "markets are in a broadly bullish phase"
    if "crisis" in rl:
        return "cross-asset stress — elevated caution"
    if "risk-off" in rl or "risk off" in rl:
        return "defensive rotation underway"
    if "transition" in rl:
        return "regime is shifting — signal ambiguity"
    return ""


def _breadth_gloss(breadth: dict) -> str:
    """Plain-English translation of breadth."""
    label = (breadth.get("label") or "").lower()
    oversold_pct = _nested(breadth, "oversold", "pct")
    if "washed" in label or "oversold" in label:
        return "most things are beaten down"
    if "overbought" in label:
        return "breadth is stretched to the upside"
    if oversold_pct is not None:
        try:
            if float(oversold_pct) > 40:
                return "majority of assets near short-term lows"
        except (TypeError, ValueError):
            pass
    return ""


def _move_gloss(bond_vol: dict) -> str:
    """Plain-English translation of MOVE/bond volatility."""
    label = (bond_vol.get("label") or "").lower()
    pct = bond_vol.get("percentile")
    if "stress" in label:
        return "bond-market fear is high"
    if "elevated" in label:
        return "bond volatility is above normal"
    if "calm" in label:
        return "bond markets are stable"
    if pct is not None:
        try:
            p = float(pct)
            if p >= 90:
                return "bond-market fear is high"
            if p >= 70:
                return "bond volatility is elevated"
            if p < 30:
                return "bond markets are calm"
        except (TypeError, ValueError):
            pass
    return ""


def _build_board() -> list | None:
    """Per-asset rows grouped by category.

    Primary source: htf precompute cache (consensio, weekly_rsi, cpr).
    Merged with RS rankings (rs_score, quadrant, ret_7d, rsi_14).
    Missing fields → None / "—".
    """
    from carrac.precompute import response_cache
    from carrac.assets.registry import categories

    # The htf precompute cache is shaped {'assets': [ {...}, ... ], 'count': N, ...}
    # — a LIST under 'assets', NOT a dict keyed by symbol. Build the symbol map.
    htf_raw = response_cache.get("htf") or {}
    htf_assets = htf_raw.get("assets") if isinstance(htf_raw, dict) else None
    htf = {
        a.get("symbol"): a
        for a in (htf_assets or [])
        if isinstance(a, dict) and a.get("symbol")
    }
    if not htf:
        log.warning("pulse: htf cache empty/unexpected shape — board will lack "
                    "price/trend/cpr (got keys: %s)",
                    list(htf_raw.keys()) if isinstance(htf_raw, dict) else type(htf_raw).__name__)
    cats = categories()

    # Build RS map by symbol
    rs_map: dict[str, dict] = {}
    try:
        from carrac.charts.routes import _compute_rs_rankings
        for row in (_compute_rs_rankings() or []):
            rs_map[row["symbol"]] = row
    except Exception as exc:
        log.debug("pulse: RS rankings failed: %s", exc)

    # Ratio synthetics that lack htf-cache data and would render as "—"
    _BOARD_EXCLUDE = frozenset({"GOLD_QQQ", "QQQ_SPX", "MAGS_SPX"})

    board = []
    for cat_name, symbols in cats.items():
        group_rows = []
        for symbol in symbols:
            if symbol in _BOARD_EXCLUDE:
                continue
            asset = htf.get(symbol) or {}
            price = asset.get("latest_price")
            consensio = asset.get("consensio") or {}

            # Daily / weekly consensio signals
            sig_1d = _nested(consensio, "1D", "signal")
            sig_1w = _nested(consensio, "1W", "signal")

            # Weekly RSI
            weekly_rsi_data = asset.get("weekly_rsi")
            weekly_rsi = None
            if isinstance(weekly_rsi_data, dict):
                weekly_rsi = weekly_rsi_data.get("value")
            elif weekly_rsi_data is not None:
                try:
                    weekly_rsi = float(weekly_rsi_data)
                except (TypeError, ValueError):
                    pass

            # Nearest CPR level
            cpr_data = asset.get("cpr") or {}
            nearest = nearest_cpr_level(price, cpr_data) if price else None

            # RS data
            rs = rs_map.get(symbol) or {}
            rs_score = rs.get("rs_score")
            rs_quadrant = rs.get("quadrant")
            ret_7d = rs.get("ret_7d")
            rsi_14 = rs.get("rsi_14")  # daily RSI from RS calc

            row = {
                "symbol": symbol,
                "price": price,
                # Trends
                "sig_1d": sig_1d,
                "trend_daily": trend_label(sig_1d),
                "sig_1w": sig_1w,
                "trend_weekly": trend_label(sig_1w),
                # RSI
                "rsi_daily": rsi_14,
                "rsi_daily_gloss": _rsi_gloss(rsi_14),
                "rsi_weekly": weekly_rsi,
                "rsi_weekly_gloss": _rsi_gloss(weekly_rsi),
                # Nearest CPR
                "nearest_level": nearest,
                # RS
                "rs_score": rs_score,
                "rs_quadrant": rs_quadrant,
                "ret_7d": ret_7d,
            }
            group_rows.append(row)
        if group_rows:
            board.append({"category": cat_name, "rows": group_rows})
    return board


def _rsi_gloss(rsi) -> str:
    """Plain-English translation of an RSI value."""
    if rsi is None:
        return ""
    try:
        v = float(rsi)
    except (TypeError, ValueError):
        return ""
    if v >= 70:
        return "overbought / stretched"
    if v >= 60:
        return "elevated momentum"
    if v <= 30:
        return "oversold / beaten down"
    if v <= 40:
        return "weak momentum"
    return "neutral"


def _build_calendar() -> list | None:
    """Economic calendar for the next 7 days (today + 7d)."""
    from carrac.db_pool import get_quant_conn as get_conn

    today = date.today()
    to = today + timedelta(days=7)
    rows = []
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, ts, event_type, title, impact, expected, actual, actual_filled_at "
                "FROM quant.events WHERE ts >= %s AND ts <= %s ORDER BY ts",
                (today.isoformat(), to.isoformat()),
            )
            for r in cur.fetchall():
                rows.append({
                    "id": r["id"],
                    "ts": str(r["ts"]),
                    "event_type": r["event_type"],
                    "title": r["title"],
                    "impact": r["impact"],
                    "expected": r["expected"],
                    "actual": r["actual"],
                    "actual_filled_at": str(r["actual_filled_at"]) if r["actual_filled_at"] else None,
                })
    except Exception as exc:
        log.warning("pulse calendar failed: %s", exc)
        return None
    return rows


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def build_pulse_payload() -> dict:
    """Assemble all cockpit panels.  Returns a dict with keys:
        weather   — regime / breadth / MOVE / F&G / biggest mover
        board     — list of {category, rows[]} per-asset groups
        calendar  — list of upcoming economic events (next 7d)
    Each value is None on failure (degradation, not a page crash).
    """
    return {
        "weather": _safe(_build_weather, "weather"),
        "board": _safe(_build_board, "board"),
        "calendar": _safe(_build_calendar, "calendar"),
    }
