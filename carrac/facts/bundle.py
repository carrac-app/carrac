"""Per-asset facts bundle assembler.

Pure and deterministic: reads ctx + delegates to the ta_facts / mayer pure
checks. No I/O, no datetime.now, no Redis. The caller passes ``as_of`` in
via ctx. Missing inputs → field is omitted or set to None/empty; never crashes
on a partial ctx.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Imports from already-built pure modules
# ---------------------------------------------------------------------------
from morning_engine.ta_facts import (
    candle_geometry,
    cluster_distances,
    decisive_closes,
    funding_streak,
    level_streaks,
    ma_stack,
    price_percentile,
)
from carrac.facts.mayer import mayer
from carrac.staleness import history_is_dead


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_stretch(daily_closes) -> dict:
    """Return stretch sub-dict or {} on insufficient data."""
    if not daily_closes:
        return {}
    result = mayer(daily_closes)
    if result is None:
        return {}
    return {
        "mayer": result["multiple"],
        "mayer_pct": result["percentile"],
    }


def _build_clusters(zones, price: float) -> dict:
    """Return cluster_distances result, or an empty-clusters placeholder."""
    if not zones:
        return {
            "nearest_support_dist_pct": None,
            "nearest_resistance_dist_pct": None,
            "clusters": [],
        }
    try:
        return cluster_distances(zones, price)
    except Exception as exc:  # noqa: BLE001
        log.debug("cluster_distances error (swallowed): %s", exc)
        return {
            "nearest_support_dist_pct": None,
            "nearest_resistance_dist_pct": None,
            "clusters": [],
        }


def _build_tf_block(tf_ctx: dict, levels: dict, price: float | None) -> dict:
    """Build the streaks/decisive_close/ma_stack/candle block for one timeframe."""
    ohlc_df = tf_ctx.get("ohlc_df")
    atr14 = tf_ctx.get("atr14", 0.0)

    # Streaks + decisive closes
    if price:
        try:
            streaks = level_streaks(ohlc_df, levels, atr14, price)
        except Exception as exc:  # noqa: BLE001
            log.debug("level_streaks error (swallowed): %s", exc)
            streaks = []

        try:
            decisive = decisive_closes(ohlc_df, levels, atr14, price)
        except Exception as exc:  # noqa: BLE001
            log.debug("decisive_closes error (swallowed): %s", exc)
            decisive = []
    else:
        streaks = []
        decisive = []

    # MA stack
    try:
        close_col = ohlc_df["close"].tolist() if ohlc_df is not None and not ohlc_df.empty else []
        stack = ma_stack(
            close_col,
            tf_ctx.get("sma20_series", []),
            tf_ctx.get("sma50_series", []),
            tf_ctx.get("sma200_series", []),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("ma_stack error (swallowed): %s", exc)
        stack = {}

    # Candle geometry
    candle_raw = tf_ctx.get("candle")
    if candle_raw:
        try:
            candle = candle_geometry(
                candle_raw["o"],
                candle_raw["h"],
                candle_raw["l"],
                candle_raw["c"],
                candle_raw.get("prev_close"),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("candle_geometry error (swallowed): %s", exc)
            candle = {}
    else:
        candle = {}

    return {
        "streaks": streaks,
        "decisive_close": decisive,
        "ma_stack": stack,
        "candle": candle,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_asset_facts(symbol: str, ctx: dict) -> dict:
    """Assemble the deterministic per-asset facts bundle from precomputed ctx.

    Pure: reads ctx + calls the ta_facts / mayer pure checks.
    No I/O, no clock, no Redis.

    When the history is dead the bundle is the staleness block and
    nothing else. Every field below is a statement ABOUT PRICE BARS — with no
    recent bars they are not weaker readings, they are readings of a market
    that stopped. A frozen symbol can show rsi 6.1 "oversold" and seven confirmed decisive
    closes computed off four identical candles. Flagging that as stale
    while still shipping it would leave an LLM free to quote the numbers.
    """
    _data_stale = ctx.get("data_stale")
    if history_is_dead(_data_stale):
        return {
            "symbol": symbol,
            "as_of": ctx.get("as_of"),
            "data_stale": _data_stale,
        }

    price = ctx.get("price")
    daily_ctx = ctx.get("daily", {})
    levels = ctx.get("levels", {})

    # --- stretch (Mayer multiple) ---
    stretch = _build_stretch(ctx.get("daily_closes"))

    # --- price percentile (90-day range) ---
    _range_pctile: dict | None = None
    try:
        _pct_val = price_percentile(ctx.get("daily_closes") or [], 90)
        if _pct_val is not None:
            _range_pctile = {"pct": _pct_val, "window_days": 90}
    except Exception as exc:  # noqa: BLE001
        log.debug("range_pctile error (swallowed): %s", exc)

    # --- MA stack (daily close series + sma series) ---
    ohlc_df = daily_ctx.get("ohlc_df")
    try:
        close_col = ohlc_df["close"].tolist() if ohlc_df is not None and not ohlc_df.empty else []
        stack = ma_stack(
            close_col,
            daily_ctx.get("sma20_series", []),
            daily_ctx.get("sma50_series", []),
            daily_ctx.get("sma200_series", []),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("ma_stack error (swallowed): %s", exc)
        stack = {}

    # --- Candle geometry ---
    candle_raw = daily_ctx.get("candle")
    if candle_raw:
        try:
            candle = candle_geometry(
                candle_raw["o"],
                candle_raw["h"],
                candle_raw["l"],
                candle_raw["c"],
                candle_raw.get("prev_close"),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("candle_geometry error (swallowed): %s", exc)
            candle = {}
    else:
        candle = {}

    # --- Cluster distances ---
    zones = ctx.get("zones")
    if price:
        cluster_result = _build_clusters(zones, price)
    else:
        cluster_result = {
            "nearest_support_dist_pct": None,
            "nearest_resistance_dist_pct": None,
            "clusters": [],
        }

    # --- Level streaks + decisive closes (daily) ---
    atr14 = daily_ctx.get("atr14", 0.0)

    if price:
        try:
            streaks = level_streaks(ohlc_df, levels, atr14, price)
        except Exception as exc:  # noqa: BLE001
            log.debug("level_streaks error (swallowed): %s", exc)
            streaks = []

        try:
            decisive = decisive_closes(ohlc_df, levels, atr14, price)
        except Exception as exc:  # noqa: BLE001
            log.debug("decisive_closes error (swallowed): %s", exc)
            decisive = []
    else:
        streaks = []
        decisive = []

    # --- Timeframes ---
    weekly_ctx = ctx.get("weekly")
    timeframes = ["1D"]
    if weekly_ctx:
        timeframes = ["1D", "1W"]

    bundle: dict = {
        "symbol": symbol,
        "as_of": ctx.get("as_of"),
        "price": price,
        "stretch": stretch,
        "ma_stack": stack,
        "candle": candle,
        "volume": daily_ctx.get("volume", {}),
        "rsi": daily_ctx.get("rsi", {}),
        "levels": {
            "clusters": cluster_result["clusters"],
            "nearest_support_dist_pct": cluster_result["nearest_support_dist_pct"],
            "nearest_resistance_dist_pct": cluster_result["nearest_resistance_dist_pct"],
            "monthly_cpr": ctx.get("monthly_cpr", {}),
            "key_levels": ctx.get("key_levels", {}),
        },
        "streaks": streaks,
        "decisive_close": decisive,
        "timeframes": timeframes,
    }

    # A price-rejection flag lands here: the bars are current so every fact
    # above stands, but the quoted price is unconfirmed and the caller is told.
    if _data_stale:
        bundle["data_stale"] = _data_stale

    # --- Optional weekly block ---
    if weekly_ctx:
        bundle["weekly"] = _build_tf_block(weekly_ctx, levels, price)

    # --- range_pctile (omit entirely when None — never write null) ---
    if _range_pctile is not None:
        bundle["range_pctile"] = _range_pctile

    # --- funding streak + percentile (crypto-only; omit entirely when None) ---
    try:
        _funding_result = funding_streak(ctx.get("funding_series") or [])
        if _funding_result is not None:
            bundle["funding"] = _funding_result
    except Exception as exc:  # noqa: BLE001
        log.debug("funding_streak error (swallowed): %s", exc)

    # --- on_chain (smart_money_bias; crypto-only; omit entirely when None) ---
    _smart_money_bias = ctx.get("smart_money_bias")
    if _smart_money_bias is not None:
        bundle["on_chain"] = {"smart_money_bias": float(_smart_money_bias)}

    return bundle
