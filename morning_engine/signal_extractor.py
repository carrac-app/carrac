"""Signal extraction — deterministic Python pre-processing of chart data.

Converts raw OHLCV + indicator data into structured signal JSON.
Run once per report, shared across all users and the Grok report prompt.
"""

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("morning_engine.signals")

# Primary timeframe per asset (use for cross-asset comparison)
PRIMARY_TF = "1D"


def extract_all_signals(chart_raw, context=None):
    """Extract signals from all chart data.

    Args:
        chart_raw: dict of {(asset, tf): api_response_dict} from fetch_chart_data()
        context: optional dict with sentiment, funding, etc. from fetch_context_data()

    Returns:
        dict with:
            'per_asset': {asset: {tf: signal_dict, ...}},
            'market_regime': str,
            'regime_components': dict,
            'signal_conflicts': [list of conflict dicts],
            'summary_json': str (compact JSON for prompt injection)
    """
    per_asset = {}
    all_signals = {}  # {asset: {tf: signals}} for cross-asset analysis

    # Group raw data by asset
    asset_tfs = {}
    for (asset, tf), data in chart_raw.items():
        asset_tfs.setdefault(asset, {})[tf] = data

    for asset, tfs in asset_tfs.items():
        per_asset[asset] = {}
        all_signals[asset] = {}
        for tf, data in tfs.items():
            # Wrap per-asset extraction so a single malformed bar (missing key,
            # None ohlcv element, schema drift) can't kill the whole report.
            try:
                signals = _extract_asset_tf_signals(data, asset, tf)
            except (KeyError, TypeError, ValueError, IndexError) as e:
                import logging as _lg
                _lg.getLogger("morning_engine").warning(
                    "Signal extraction failed for %s/%s: %s — emitting degraded stub",
                    asset, tf, e,
                )
                signals = {"asset": asset, "tf": tf, "status": "extraction_error",
                           "error": str(e)[:120]}
            per_asset[asset][tf] = signals
            all_signals[asset][tf] = signals

    # Cross-asset analysis
    regime, regime_components = _classify_market_regime(all_signals, context)
    conflicts = _detect_signal_conflicts(all_signals, context)

    # On-chain signal classification
    onchain_signals = classify_onchain_signals(context)

    # Build compact summary for prompt injection
    summary = _build_signal_summary(all_signals, regime, regime_components, conflicts)

    # Add on-chain classification to summary if any are NOTABLE
    if onchain_signals:
        notable_onchain = {k: v for k, v in onchain_signals.items()
                          if v.get("priority") == "NOTABLE"}
        if notable_onchain:
            summary["onchain_notable"] = {
                k: {"value": v["value"], "reason": v.get("reason", "")}
                for k, v in notable_onchain.items()
            }

    return {
        "per_asset": per_asset,
        "market_regime": regime,
        "regime_components": regime_components,
        "signal_conflicts": conflicts,
        "summary_json": json.dumps(summary, separators=(",", ":")),
        "onchain_signals": onchain_signals,
    }


def _extract_asset_tf_signals(data, asset, tf):
    """Extract signals for one asset/timeframe pair."""
    signals = {"asset": asset, "tf": tf}
    ohlcv = data.get("ohlcv", [])

    if not ohlcv:
        signals["status"] = "no_data"
        return signals

    signals["status"] = "ok"
    last = ohlcv[-1]
    price = last["close"]
    signals["price"] = price

    # --- MA Stack ---
    ind = data.get("indicators", {})
    sma_data = ind.get("smaLines", {})
    sma20_arr = sma_data.get("sma20", [])
    sma50_arr = sma_data.get("sma50", [])
    sma200_arr = sma_data.get("sma200", [])
    sma20 = sma20_arr[-1]["value"] if sma20_arr else None
    sma50 = sma50_arr[-1]["value"] if sma50_arr else None
    sma200 = sma200_arr[-1]["value"] if sma200_arr else None

    if sma20 and sma200:
        if price > sma20 > sma200:
            signals["ma_stack"] = "bullish"
        elif price < sma20 < sma200:
            signals["ma_stack"] = "bearish"
        elif price > sma200:
            signals["ma_stack"] = "bullish_partial"
        else:
            signals["ma_stack"] = "bearish_partial"
        signals["sma20"] = round(sma20, 2)
        signals["sma200"] = round(sma200, 2)
        # Death/golden cross uses the conventional 50/200 pair (falls back to 20 if sma50 absent).
        cross_fast = sma50 if sma50 else sma20
        signals["ma_cross"] = "golden_cross" if cross_fast > sma200 else "death_cross"
    elif sma20:
        signals["ma_stack"] = "above_sma20" if price > sma20 else "below_sma20"
        signals["sma20"] = round(sma20, 2)

    # --- RSI ---
    rsi_arr = data.get("rsi", [])
    if rsi_arr:
        rsi_val = rsi_arr[-1]["value"]
        signals["rsi"] = round(rsi_val, 1)
        if rsi_val < 30:
            signals["rsi_zone"] = "oversold"
        elif rsi_val > 70:
            signals["rsi_zone"] = "overbought"
        elif rsi_val < 40:
            signals["rsi_zone"] = "weak"
        elif rsi_val > 60:
            signals["rsi_zone"] = "strong"
        else:
            signals["rsi_zone"] = "neutral"

        # RSI divergence detection (need 20+ bars)
        if len(rsi_arr) >= 20 and len(ohlcv) >= 20:
            signals["rsi_divergence"] = _detect_rsi_divergence(ohlcv, rsi_arr)

    # --- MACD ---
    macd = data.get("macd", {})
    ml = macd.get("line", [])
    ms = macd.get("signal", [])
    mh = macd.get("histogram", [])
    if ml and ms and mh:
        hist_val = mh[-1]["value"]
        prev_hist = mh[-2]["value"] if len(mh) > 1 else 0
        line_val = ml[-1]["value"]

        if hist_val > 0:
            signals["macd_momentum"] = "bullish_expansion" if hist_val > prev_hist else "bullish_contraction"
        else:
            signals["macd_momentum"] = "bearish_expansion" if hist_val < prev_hist else "bearish_contraction"

        signals["macd_above_zero"] = line_val > 0
        signals["macd_histogram"] = round(hist_val, 4)

    # --- CPR ---
    cpr = data.get("cpr", {})
    if cpr and cpr.get("pivot"):
        pivot, tc, bc = cpr["pivot"], cpr["tc"], cpr["bc"]
        if price > tc:
            signals["cpr_status"] = "above_tc"
        elif price < bc:
            signals["cpr_status"] = "below_bc"
        else:
            signals["cpr_status"] = "inside"

        width_pct = (tc - bc) / pivot * 100 if pivot else 0
        if width_pct < 0.5:
            signals["cpr_width"] = "narrow"
        elif width_pct > 2.0:
            signals["cpr_width"] = "wide"
        else:
            signals["cpr_width"] = "normal"
        signals["cpr_width_pct"] = round(width_pct, 2)
        signals["cpr_pivot"] = round(pivot, 2)

    # --- EMA Ribbon ---
    ema_ribbon = ind.get("ema_ribbon", {})
    fast = ema_ribbon.get("fast", [])
    slow = ema_ribbon.get("slow", [])
    fill = ema_ribbon.get("fill", [])
    if fast and slow and fill:
        fv, sv = fast[-1]["value"], slow[-1]["value"]
        trend = fill[-1]["trend"].upper()
        signals["ema_ribbon_trend"] = trend
        if price > max(fv, sv):
            signals["ema_ribbon_position"] = "above_both"
        elif price < min(fv, sv):
            signals["ema_ribbon_position"] = "below_both"
        else:
            signals["ema_ribbon_position"] = "between"

    # --- Volume ---
    from morning_engine.candle_utils import volume_ratio_closed, tf_to_seconds
    volume = data.get("volume", [])
    if volume:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        vr = volume_ratio_closed(volume, timeframe_seconds=tf_to_seconds(tf), now_ms=now_ms, period=20)
        # Only emit a volume signal when the data is reliable — junk/forward-fill
        # commodity volume (Yahoo) would otherwise produce bogus "16x"/"0.02x" claims.
        if vr.get("reliable") and vr["ratio"] is not None:
            ratio = vr["ratio"]
            signals["volume_ratio"] = round(ratio, 2)
            signals["volume_candle_complete"] = vr["candle_complete"]
            if ratio > 1.5:
                signals["volume_signal"] = "high"
            elif ratio < 0.7:
                signals["volume_signal"] = "low"
            else:
                signals["volume_signal"] = "normal"

    # --- Range position ---
    r20 = ohlcv[-20:] if len(ohlcv) >= 20 else ohlcv
    h20 = max(c["high"] for c in r20)
    l20 = min(c["low"] for c in r20)
    signals["range_position"] = round((price - l20) / (h20 - l20) * 100, 0) if h20 != l20 else 50

    # --- Bar momentum (5-bar cumulative %) ---
    if len(ohlcv) >= 6:
        ref = ohlcv[-6]["close"]
        signals["bar_momentum_5"] = round((price - ref) / ref * 100, 2) if ref else 0

    # --- Weekly MAs (if available) ---
    wmas = ind.get("weeklyMAs", {})
    for key in ["wma50", "wma100", "wma200"]:
        arr = wmas.get(key, [])
        if arr:
            signals[key] = round(arr[-1]["value"], 2)

    # --- Bull Market Band ---
    bmb = ind.get("bullMarketBand", {})
    ema21w = bmb.get("ema21w", [])
    sma20w = bmb.get("sma20w", [])
    if ema21w and sma20w:
        e21, s20 = ema21w[-1]["value"], sma20w[-1]["value"]
        if price > max(e21, s20):
            signals["bull_band"] = "above"
        elif price < min(e21, s20):
            signals["bull_band"] = "below"
        else:
            signals["bull_band"] = "inside"

    # --- Extended Indicators (from indicators_ext snapshot) ---
    ext = data.get("indicators_ext", {})
    if ext:
        # TTM Squeeze
        ttm = ext.get("ttm_squeeze")
        if ttm:
            signals["ttm_squeeze"] = "active" if ttm.get("active") else "inactive"
            if ttm.get("active"):
                signals["ttm_squeeze_bars"] = ttm.get("bars", 0)
                signals["ttm_squeeze_momentum"] = ttm.get("momentum", "neutral")

        # Ichimoku
        ichi = ext.get("ichimoku")
        if ichi:
            signals["ichimoku_cloud"] = ichi.get("cloud_position", "unknown")
            tk = ichi.get("tk_cross", "none")
            if tk != "none":
                signals["ichimoku_tk_cross"] = tk

        # PSAR
        psar = ext.get("psar")
        if psar:
            signals["psar_direction"] = psar.get("direction", "unknown")
            if psar.get("flip"):
                signals["psar_flip"] = True

        # SuperTrend
        st = ext.get("supertrend")
        if st:
            signals["supertrend_dir"] = st.get("direction", "unknown")
            if st.get("flip"):
                signals["supertrend_flip"] = True

        # Bollinger Bands
        boll = ext.get("boll")
        if boll:
            signals["boll_position"] = boll.get("position", "inside")
            if boll.get("bandwidth_pct", 100) < 5:
                signals["boll_squeeze"] = True

        # Stochastic
        stoch = ext.get("stoch")
        if stoch and stoch.get("zone") != "neutral":
            signals["stoch_zone"] = stoch["zone"]
            signals["stoch_k"] = stoch.get("k")

        # StochRSI
        stoch_rsi = ext.get("stoch_rsi")
        if stoch_rsi and stoch_rsi.get("zone") != "neutral":
            signals["stoch_rsi_zone"] = stoch_rsi["zone"]

        # OBV
        obv = ext.get("obv")
        if obv:
            signals["obv_trend"] = obv.get("trend", "flat")
            if obv.get("divergence") != "none":
                signals["obv_divergence"] = obv["divergence"]

        # Auto Fibonacci
        afib = ext.get("auto_fib")
        if afib:
            signals["auto_fib_trend"] = afib.get("trend", "unknown")
            signals["auto_fib_position"] = afib.get("price_position", "unknown")

        # Keltner
        kelt = ext.get("keltner")
        if kelt and kelt.get("position") != "inside":
            signals["keltner_position"] = kelt["position"]

        # BBW Percentile
        bbw = ext.get("bbw_pct")
        if bbw and bbw.get("compression"):
            signals["bbw_compression"] = True
            signals["bbw_percentile"] = bbw.get("percentile")

        # ATR Percentile
        atr_pct = ext.get("atr_pct")
        if atr_pct and atr_pct.get("low_volatility"):
            signals["atr_low_vol"] = True
            signals["atr_percentile"] = atr_pct.get("percentile")

        # Williams %R
        wr = ext.get("williams_r")
        if wr and wr.get("zone") != "neutral":
            signals["williams_r_zone"] = wr["zone"]

        # CCI
        cci = ext.get("cci")
        if cci and cci.get("zone") != "neutral":
            signals["cci_zone"] = cci["zone"]

        # Donchian
        don = ext.get("donchian")
        if don and don.get("breakout"):
            signals["donchian_breakout"] = don["breakout"]

        # Hull MA
        hull = ext.get("hull_ma")
        if hull:
            signals["hull_ma_dir"] = hull.get("direction", "flat")

        # ALMA
        alma = ext.get("alma")
        if alma:
            signals["alma_dir"] = alma.get("direction", "flat")

        # KAMA
        kama = ext.get("kama")
        if kama:
            signals["kama_dir"] = kama.get("direction", "flat")

        # EMA Cross
        ema_x = ext.get("ema_cross")
        if ema_x:
            signals["ema_cross_state"] = ema_x.get("state", "unknown")
            if ema_x.get("cross"):
                signals["ema_cross_signal"] = ema_x["cross"]

        # Alligator
        gator = ext.get("alligator")
        if gator:
            signals["alligator_state"] = gator.get("state", "mixed")

        # Consensio
        cons = ext.get("consensio")
        if cons:
            signals["consensio_signal"] = cons.get("signal", 0)

        # S/R Channels
        sr = ext.get("sr_channels")
        if sr:
            # Just report count and nearest
            signals["sr_channel_count"] = len(sr)
            nearest = min(sr, key=lambda x: abs(x["level"] - price))
            signals["sr_nearest_type"] = nearest["type"]
            signals["sr_nearest_level"] = nearest["level"]
            signals["sr_nearest_strength"] = nearest["strength"]

        # FVG
        fvg = ext.get("fvg")
        if fvg:
            signals["fvg_bullish_count"] = fvg.get("bullish_count", 0)
            signals["fvg_bearish_count"] = fvg.get("bearish_count", 0)
            signals["fvg_nearest_type"] = fvg.get("nearest_type")

        # NR7
        nr7 = ext.get("nr7")
        if nr7 and nr7.get("active"):
            signals["nr7_active"] = True

    return signals


def _detect_rsi_divergence(ohlcv, rsi_arr, lookback=10):
    """Detect RSI divergence by comparing recent swing highs/lows.

    Returns: 'bullish', 'bearish', 'hidden_bullish', 'hidden_bearish', or 'none'
    """
    if len(ohlcv) < lookback * 2 or len(rsi_arr) < lookback * 2:
        return "none"

    # Get last N bars of price lows and RSI
    recent_prices = [c["close"] for c in ohlcv[-lookback * 2:]]
    recent_rsi = [r["value"] for r in rsi_arr[-lookback * 2:]]

    # Find two most recent swing lows (for bullish divergence)
    half = len(recent_prices) // 2
    first_half_low_idx = min(range(half), key=lambda i: recent_prices[i])
    second_half_low_idx = min(range(half, len(recent_prices)), key=lambda i: recent_prices[i])

    price_low1 = recent_prices[first_half_low_idx]
    price_low2 = recent_prices[second_half_low_idx]
    rsi_low1 = recent_rsi[first_half_low_idx]
    rsi_low2 = recent_rsi[second_half_low_idx]

    # Regular bullish: price lower low, RSI higher low
    if price_low2 < price_low1 and rsi_low2 > rsi_low1:
        return "bullish"

    # Hidden bullish: price higher low, RSI lower low
    if price_low2 > price_low1 and rsi_low2 < rsi_low1:
        return "hidden_bullish"

    # Find two most recent swing highs (for bearish divergence)
    first_half_high_idx = max(range(half), key=lambda i: recent_prices[i])
    second_half_high_idx = max(range(half, len(recent_prices)), key=lambda i: recent_prices[i])

    price_high1 = recent_prices[first_half_high_idx]
    price_high2 = recent_prices[second_half_high_idx]
    rsi_high1 = recent_rsi[first_half_high_idx]
    rsi_high2 = recent_rsi[second_half_high_idx]

    # Regular bearish: price higher high, RSI lower high
    if price_high2 > price_high1 and rsi_high2 < rsi_high1:
        return "bearish"

    # Hidden bearish: price lower high, RSI higher high
    if price_high2 < price_high1 and rsi_high2 > rsi_high1:
        return "hidden_bearish"

    return "none"


def _classify_market_regime(all_signals, context=None):
    """Classify the current market regime from cross-asset signals.

    Rules-based classification using DXY, GOLD_QQQ, QQQ_SPX, funding rates.

    Returns:
        (regime_name, components_dict)
    """
    components = {}
    scores = {
        "risk_on": 0,
        "risk_off": 0,
        "commodity_rotation": 0,
        "stagflation": 0,
    }

    # DXY direction
    dxy = _get_primary_signals(all_signals, "DXY")
    if dxy:
        if dxy.get("bar_momentum_5", 0) > 0.5:
            scores["risk_off"] += 2
            components["dxy"] = "rising (risk-off)"
        elif dxy.get("bar_momentum_5", 0) < -0.5:
            scores["risk_on"] += 1
            components["dxy"] = "falling (risk-on)"
        else:
            components["dxy"] = "flat"

    # GOLD_QQQ ratio direction
    gold_qqq = _get_primary_signals(all_signals, "GOLD_QQQ")
    if gold_qqq:
        mom = gold_qqq.get("bar_momentum_5", 0)
        if mom > 1.0:
            scores["commodity_rotation"] += 2
            scores["risk_off"] += 1
            components["gold_vs_qqq"] = f"gold outperforming ({mom:+.1f}%)"
        elif mom < -1.0:
            scores["risk_on"] += 2
            components["gold_vs_qqq"] = f"qqq outperforming ({mom:+.1f}%)"
        else:
            components["gold_vs_qqq"] = "neutral"

    # QQQ_SPX ratio direction (tech leadership)
    qqq_spx = _get_primary_signals(all_signals, "QQQ_SPX")
    if qqq_spx:
        mom = qqq_spx.get("bar_momentum_5", 0)
        if mom > 0.5:
            scores["risk_on"] += 1
            components["tech_leadership"] = f"tech leading ({mom:+.1f}%)"
        elif mom < -0.5:
            scores["commodity_rotation"] += 1
            components["tech_leadership"] = f"tech lagging ({mom:+.1f}%)"
        else:
            components["tech_leadership"] = "neutral"

    # MAGS_SPX (mega-cap vs broad market)
    mags_spx = _get_primary_signals(all_signals, "MAGS_SPX")
    if mags_spx:
        mom = mags_spx.get("bar_momentum_5", 0)
        components["megacap_vs_broad"] = f"{'mega-cap leading' if mom > 0 else 'breadth improving'} ({mom:+.1f}%)"

    # SPX direction
    spx = _get_primary_signals(all_signals, "SPX")
    if spx:
        rsi = spx.get("rsi", 50)
        mom = spx.get("bar_momentum_5", 0)
        if mom > 1.0 and rsi > 50:
            scores["risk_on"] += 2
            components["spx"] = f"bullish (RSI {rsi:.0f}, {mom:+.1f}%)"
        elif mom < -1.0 or rsi < 40:
            scores["risk_off"] += 2
            components["spx"] = f"bearish (RSI {rsi:.0f}, {mom:+.1f}%)"
        else:
            components["spx"] = f"neutral (RSI {rsi:.0f})"

    # BTC direction
    btc = _get_primary_signals(all_signals, "BTC")
    if btc:
        rsi = btc.get("rsi", 50)
        mom = btc.get("bar_momentum_5", 0)
        components["btc"] = f"RSI {rsi:.0f}, {mom:+.1f}%"

    # OIL (commodity shock detection)
    oil = _get_primary_signals(all_signals, "OIL")
    if oil:
        mom = oil.get("bar_momentum_5", 0)
        if abs(mom) > 3.0:
            scores["commodity_rotation"] += 2
            scores["stagflation"] += 1
            components["oil"] = f"{'surging' if mom > 0 else 'crashing'} ({mom:+.1f}%)"
        elif mom > 1.0:
            scores["commodity_rotation"] += 1
            components["oil"] = f"rising ({mom:+.1f}%)"

    # GOLD direction
    gold = _get_primary_signals(all_signals, "GOLD")
    if gold:
        mom = gold.get("bar_momentum_5", 0)
        if mom > 1.0:
            scores["risk_off"] += 1
            scores["commodity_rotation"] += 1
            components["gold"] = f"rising ({mom:+.1f}%)"

    # Funding rates from context
    if context:
        sentiment = context.get("sentiment", "{}")
        if isinstance(sentiment, str):
            try:
                sentiment = json.loads(sentiment)
            except Exception:
                sentiment = {}
        sent_data = sentiment.get("sentiment", {}) if isinstance(sentiment, dict) else {}
        funding = sent_data.get("funding_rate", {})
        if isinstance(funding, dict) and funding.get("rate") is not None:
            rate = funding["rate"]
            components["btc_funding"] = f"{rate * 100:.4f}%"
            if rate > 0.0001:
                scores["risk_on"] += 1
            elif rate < -0.0001:
                scores["risk_off"] += 1

    # Determine primary regime
    max_score = max(scores.values()) if scores.values() else 0
    if max_score <= 2:
        regime = "transitional"
    else:
        regime = max(scores, key=scores.get)

    components["scores"] = scores
    log.info("Market regime: %s (scores: %s)", regime, scores)

    return regime, components


def _get_primary_signals(all_signals, asset):
    """Get the primary timeframe signals for an asset."""
    asset_data = all_signals.get(asset, {})
    return asset_data.get(PRIMARY_TF, asset_data.get("1D", {}))


def _detect_signal_conflicts(all_signals, context=None):
    """Detect pairs of contradicting signals across assets and timeframes.

    Returns list of conflict dicts with resolution suggestions.
    """
    conflicts = []

    # 1. BTC funding vs price direction
    btc = _get_primary_signals(all_signals, "BTC")
    if btc and context:
        sentiment_raw = context.get("sentiment", "{}")
        if isinstance(sentiment_raw, str):
            try:
                sentiment_raw = json.loads(sentiment_raw)
            except Exception:
                sentiment_raw = {}
        sent_data = sentiment_raw.get("sentiment", {}) if isinstance(sentiment_raw, dict) else {}
        funding = sent_data.get("funding_rate", {})
        if isinstance(funding, dict) and funding.get("rate") is not None:
            rate = funding["rate"]
            btc_mom = btc.get("bar_momentum_5", 0)
            if rate > 0.0001 and btc_mom < -1.0:
                conflicts.append({
                    "pair": ("BTC funding positive", "BTC price falling"),
                    "type": "funding_price_divergence",
                    "implication": "Long squeeze risk — overleveraged longs with declining price",
                    "resolution": "Short-term bearish, watch for funding reset via liquidation cascade",
                })
            elif rate < -0.0001 and btc_mom > 1.0:
                conflicts.append({
                    "pair": ("BTC funding negative", "BTC price rising"),
                    "type": "funding_price_divergence",
                    "implication": "Short squeeze setup — shorts paying while price rises",
                    "resolution": "Bullish momentum, potential for squeeze acceleration",
                })

    # 2. HTF vs LTF trend conflicts (per asset)
    for asset in all_signals:
        if "_" in asset:
            continue  # Skip ratio pairs
        sigs = all_signals[asset]
        weekly = sigs.get("1W", {})
        daily = sigs.get("1D", {})

        if not weekly or not daily:
            continue

        w_rsi = weekly.get("rsi_zone", "neutral")
        d_rsi = daily.get("rsi_zone", "neutral")
        w_macd = weekly.get("macd_momentum", "")
        d_macd = daily.get("macd_momentum", "")

        # Weekly bearish but daily bullish
        if w_rsi in ("oversold", "weak") and d_rsi in ("overbought", "strong"):
            conflicts.append({
                "pair": (f"{asset} weekly RSI {w_rsi}", f"{asset} daily RSI {d_rsi}"),
                "type": "htf_ltf_divergence",
                "implication": f"{asset} daily strength within a weak weekly structure — potential bull trap",
                "resolution": "HTF takes priority; daily strength may be a counter-trend bounce",
            })
        elif w_rsi in ("overbought", "strong") and d_rsi in ("oversold", "weak"):
            conflicts.append({
                "pair": (f"{asset} weekly RSI {w_rsi}", f"{asset} daily RSI {d_rsi}"),
                "type": "htf_ltf_divergence",
                "implication": f"{asset} daily pullback within a strong weekly trend — potential pullback within the trend",
                "resolution": "HTF takes priority; daily weakness may be a pullback within the weekly trend",
            })

        # MACD direction conflicts
        if w_macd and d_macd:
            w_bull = "bullish" in w_macd
            d_bull = "bullish" in d_macd
            if w_bull and not d_bull:
                conflicts.append({
                    "pair": (f"{asset} weekly MACD bullish", f"{asset} daily MACD bearish"),
                    "type": "macd_tf_conflict",
                    "implication": f"{asset} short-term momentum fading while weekly structure holds",
                    "resolution": "Watch for daily MACD to re-align with weekly; pause until confirmation",
                })

    # 3. Gold rising while DXY rising (unusual — implies real fear)
    gold = _get_primary_signals(all_signals, "GOLD")
    dxy = _get_primary_signals(all_signals, "DXY")
    if gold and dxy:
        gold_mom = gold.get("bar_momentum_5", 0)
        dxy_mom = dxy.get("bar_momentum_5", 0)
        if gold_mom > 1.0 and dxy_mom > 0.5:
            conflicts.append({
                "pair": ("Gold rising", "DXY rising"),
                "type": "safe_haven_dual",
                "implication": "Both safe havens bid — indicates genuine fear or crisis hedging",
                "resolution": "Risk-off likely to intensify; crypto and equities vulnerable",
            })

    return conflicts


def classify_onchain_signals(context):
    """Classify on-chain metrics as NOTABLE or NORMAL for priority weighting.

    Args:
        context: dict from fetch_context_data() with 'sentiment' key.

    Returns:
        dict with metric classifications: {metric: {"value": ..., "priority": "NOTABLE"/"NORMAL", "reason": ...}}
    """
    result = {}

    if not context:
        return result

    sentiment_raw = context.get("sentiment", "{}")
    if isinstance(sentiment_raw, str):
        try:
            sentiment_raw = json.loads(sentiment_raw)
        except Exception:
            return result

    sent_data = sentiment_raw.get("sentiment", {}) if isinstance(sentiment_raw, dict) else {}

    # Fear & Greed Index
    fg = sent_data.get("fear_greed", {})
    if isinstance(fg, dict) and fg.get("value") is not None:
        fg_val = fg["value"]
        if fg_val < 20:
            result["fear_greed"] = {"value": fg_val, "priority": "NOTABLE", "reason": "extreme_fear"}
        elif fg_val > 80:
            result["fear_greed"] = {"value": fg_val, "priority": "NOTABLE", "reason": "extreme_greed"}
        else:
            result["fear_greed"] = {"value": fg_val, "priority": "NORMAL"}

    # MVRV Z-Score
    mvrv = sent_data.get("mvrv_zscore", {})
    if isinstance(mvrv, dict) and mvrv.get("zscore") is not None:
        z = mvrv["zscore"]
        if z < -1.5 or z > 3.0:
            result["mvrv"] = {"value": z, "priority": "NOTABLE", "reason": "extreme_zone"}
        else:
            result["mvrv"] = {"value": z, "priority": "NORMAL"}

    # Funding Rate
    funding = sent_data.get("funding_rate", {})
    if isinstance(funding, dict) and funding.get("rate") is not None:
        rate = funding["rate"]
        if rate > 0.0005 or rate < -0.0001:
            result["funding_rate"] = {"value": rate, "priority": "NOTABLE",
                                      "reason": "spike" if rate > 0 else "negative"}
        else:
            result["funding_rate"] = {"value": rate, "priority": "NORMAL"}

    # Open Interest
    oi = sent_data.get("open_interest", {})
    if isinstance(oi, dict) and oi.get("change_pct") is not None:
        oi_chg = oi["change_pct"]
        if abs(oi_chg) > 5:
            result["open_interest"] = {"value": oi_chg, "priority": "NOTABLE",
                                       "reason": f"{'surge' if oi_chg > 0 else 'drop'} ({oi_chg:+.1f}%)"}
        else:
            result["open_interest"] = {"value": oi_chg, "priority": "NORMAL"}

    notable_count = sum(1 for v in result.values() if v.get("priority") == "NOTABLE")
    if notable_count > 0:
        log.info("On-chain classification: %d NOTABLE out of %d metrics", notable_count, len(result))

    return result


def _build_signal_summary(all_signals, regime, regime_components, conflicts):
    """Build a compact signal summary dict for prompt injection."""
    summary = {
        "market_regime": regime,
        "regime_why": {},
        "asset_signals": {},
        "conflicts": [],
    }

    # Regime explanation — only include non-trivial components
    for key, val in regime_components.items():
        if key != "scores" and val and val != "neutral" and val != "flat":
            summary["regime_why"][key] = val

    # Per-asset signal summary (primary timeframe only, compact)
    for asset, tfs in all_signals.items():
        primary = tfs.get(PRIMARY_TF, tfs.get("1D", {}))
        if not primary or primary.get("status") == "no_data":
            continue

        asset_sum = {}
        # `range_position` is deliberately NOT in this list.
        #
        # It is a 20-bar high/low position (computed above in this function). The
        # Market Snapshot "Range" column is a 90-day CLOSE PERCENTILE, handed
        # to the model verbatim by morning_engine.data_assembly.
        # format_compact_asset_lines and checked at publish time by
        # carrac.grounding.hallucination_scan.scan_range_pctile_mismatch.
        #
        # Emitting both puts two different "range" numbers for the same asset
        # in one prompt, and the model tends to copy this one: the published
        # table then prints e.g. 9% against a grounded 43%, and the prose
        # reasons off the wrong figure ("sitting at just 9% of its 20-bar range").
        #
        # The compact line (see the docstring in data_assembly.py) already
        # carries the grounded figure, so leaving this field in the SOURCE
        # would keep the ambiguity alive. The model still gets a range figure —
        # the grounded one — from that line.
        for key in ("ma_stack", "rsi", "rsi_zone", "rsi_divergence",
                     "macd_momentum", "cpr_status", "cpr_width",
                     "ema_ribbon_trend", "ema_ribbon_position",
                     "volume_signal", "volume_ratio", "ma_cross",
                     "bar_momentum_5", "bull_band"):
            val = primary.get(key)
            if val is not None and val != "none" and val != "normal":
                asset_sum[key] = val

        if asset_sum:
            summary["asset_signals"][asset] = asset_sum

    # Conflict summary
    for c in conflicts:
        summary["conflicts"].append({
            "type": c["type"],
            "signals": list(c["pair"]),
            "resolution": c["resolution"],
        })

    return summary


def select_spotlight_assets(all_signals, user_report_assets=None, max_spotlight=5):
    """Select which assets get narrative depth vs summary table treatment.

    Args:
        all_signals: dict of {asset: {tf: signal_dict}} from extract_all_signals()
        user_report_assets: optional list of asset symbols from user preferences
        max_spotlight: maximum spotlight assets (default 5)

    Returns:
        (spotlight_list, summary_list) — two lists of asset symbols
    """
    # All unique assets from signal data (exclude ratios from spotlight).
    # Ratios sourced from carrac.assets.registry (single source of truth).
    from carrac.assets.registry import ratio_set
    ratio_assets = ratio_set()
    all_assets = sorted(set(
        asset for asset in all_signals
        if asset not in ratio_assets
    ))

    if user_report_assets:
        # User-defined spotlight list — respect it, cap at max
        spotlight = [a for a in user_report_assets if a in all_signals][:max_spotlight]
    else:
        # Auto-select: top movers + RSI extremes + divergences
        spotlight = []

        # Rank by 5-bar cumulative momentum (use PRIMARY_TF = "1D")
        movers = []
        for asset in all_assets:
            tfs = all_signals.get(asset, {})
            daily = tfs.get(PRIMARY_TF, {})
            change = abs(daily.get("bar_momentum_5", 0) or 0)
            rsi = daily.get("rsi")
            # Boost assets with RSI extremes or divergences
            if rsi is not None and (rsi < 30 or rsi > 70):
                change += 3.0
            if daily.get("rsi_divergence") and daily["rsi_divergence"] != "none":
                change += 5.0
            movers.append((asset, change))

        movers.sort(key=lambda x: x[1], reverse=True)
        for asset, _ in movers:
            if len(spotlight) >= max_spotlight:
                break
            if asset not in spotlight:
                spotlight.append(asset)

    summary = [a for a in all_assets if a not in spotlight]
    # Always include ratios in summary
    summary.extend(sorted(a for a in ratio_assets if a in all_signals))

    log.info("Spotlight assets: %s | Summary: %d assets", spotlight, len(summary))
    return spotlight, summary
