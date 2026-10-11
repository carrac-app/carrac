"""
AI context builder — shared macro context + per-asset viewport descriptions.

Shared context: refreshes every 15 min from ResponseCache, ~800 tokens.
Viewport context: per-asset data from internal Python functions, ~600-900 tokens.
  Includes: indicators from ResponseCache + MACD, SMAs, recent candles, volume trend from OHLCV.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger('carrac.ai.context')

# Shared context cache (module-level singletons). Keep base and premium
# variants separate so Premium-only derivatives data never leaks into a
# free/member prompt. Refreshes every SHARED_TTL.
_shared_context_base = {'text': '', 'updated_at': 0}
_shared_context_premium = {'text': '', 'updated_at': 0}
SHARED_TTL = 900  # 15 minutes

# Tiers allowed to see premium content blocks (derivatives regime, etc.)
from carrac.tiers import PREMIUM_TIERS as _PREMIUM_TIERS


def _includes_premium(tier: Optional[str]) -> bool:
    return (tier or 'free') in _PREMIUM_TIERS


def get_shared_context(user_tier: Optional[str] = None) -> str:
    """Get shared macro context, refreshing if stale.

    Everyone below admin receives the base context. Only the OPERATOR gets the
    derivatives regime block.

    It must not key on ``_includes_premium``: that would hand a paying PREMIUM
    customer "futures APR +8.2%, skew -4.5%, GEX negative" in their AI
    prompt. Premium is a customer, not the operator, and Deribit's ToS draws
    the line at redistribution — not at who paid us.

    The cache key is narrowed with the gate on purpose. Selecting the premium
    cache while gating on admin would let an operator request populate a shared
    entry that a premium request then reads — the exact cache-poisoning shape
    once found in ``build_snapshot``.
    """
    premium = _includes_premium(user_tier) and (user_tier or '') == 'admin'
    cache = _shared_context_premium if premium else _shared_context_base

    now = time.time()
    if now - cache['updated_at'] < SHARED_TTL and cache['text']:
        return cache['text']

    try:
        text = _build_shared_context(include_premium=premium)
        cache['text'] = text
        cache['updated_at'] = now
        return text
    except Exception as e:
        logger.error(f"Shared context build error: {e}")
        return cache['text']  # return stale if available


def _build_shared_context(include_premium: bool = False) -> str:
    """Build shared macro context from ResponseCache + sentiment data.

    When include_premium is False, derivatives regime is omitted so
    free/member prompts don't receive Premium-only data.
    """
    from carrac.precompute import response_cache

    lines = []

    # BTC + ETH state from cached HTF data
    cached = response_cache.get('htf')
    if cached:
        for symbol in ('BTC', 'ETH', 'SPX', 'GOLD'):
            asset = _find_asset(cached, symbol)
            if asset:
                lines.append(_summarize_asset(asset))

        # Market regime summary
        assets = cached.get('assets', [])
        if assets:
            bullish = sum(1 for a in assets if a.get('consensio', {}).get('total', {}).get('positions', 0) >= 4)
            bearish = sum(1 for a in assets if a.get('consensio', {}).get('total', {}).get('positions', 0) <= 1)
            total = len(assets)
            lines.append(f"\nMarket breadth: {bullish}/{total} bullish, {bearish}/{total} bearish")

    # Market Regime
    try:
        from carrac.caches import get_cached_regime
        regime = get_cached_regime()
        if regime and regime.get('regime') != 'N/A':
            lines.append(f"Market Regime: {regime['regime']} (score {regime['score']})")
    except Exception:
        pass

    # Derivatives Regime — OPERATOR ONLY. Deribit-barred columns;
    # the caller has already been narrowed to admin in get_shared_context.
    if include_premium:
        try:
            from carrac.db_pool import get_quant_conn as get_conn
            # classify_asset_row owns the raw-column →
            # classifier-key mapping; raw names dropped three features.
            from quant.engine.derivatives_regime import classify_asset_row
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT basis_pct, skew_25d, dvol, iv_rv_premium,
                           options_put_call_ratio, term_structure_slope,
                           gex_net, gex_flip_price
                    FROM quant.derivatives WHERE asset = 'BTC'
                    ORDER BY ts DESC LIMIT 1
                """)
                row = cur.fetchone()
                regime = None
                if row and row.get('dvol'):
                    metrics = {k: float(v) for k, v in row.items() if v is not None}
                    regime = classify_asset_row(metrics)
                if regime:
                    parts = [f"Derivatives Regime: {regime['regime_name']} ({regime['confidence']:.0%})"]
                    if metrics.get('basis_pct') is not None:
                        parts.append(f"futures APR {metrics['basis_pct']:+.1f}%")
                    if metrics.get('skew_25d') is not None:
                        parts.append(f"skew {metrics['skew_25d']:+.1f}%")
                    if metrics.get('gex_net') is not None:
                        parts.append(f"GEX {'positive' if metrics['gex_net'] > 0 else 'negative'}")
                    lines.append(", ".join(parts))
        except Exception:
            pass

    # Fear & Greed
    try:
        from carrac.analysis import fetch_fear_greed_index
        fg = fetch_fear_greed_index()
        if fg and fg.get('value'):
            lines.append(f"Fear & Greed: {fg['value']} ({fg['classification']})")
    except Exception:
        pass

    # Sentiment from content_db history
    try:
        from carrac.content_db import get_content_db
        db = get_content_db()
        summary = db.get_sentiment_daily_summary(days=3)
        if summary:
            latest = summary[-1]
            parts = []
            if latest.get('funding_rate') is not None:
                parts.append(f"BTC funding: {latest['funding_rate']:.4f}%")
            if latest.get('btc_long_pct') is not None:
                parts.append(f"long ratio: {latest['btc_long_pct']:.0f}%")
            if latest.get('mvrv_zscore') is not None:
                parts.append(f"MVRV Z: {latest['mvrv_zscore']:.2f}")
            if parts:
                lines.append("Sentiment: " + ", ".join(parts))
    except Exception:
        pass

    return "\n".join(lines) if lines else ""


def _find_asset(cached_data: dict, symbol: str) -> Optional[dict]:
    """Find an asset in cached response data."""
    for a in cached_data.get('assets', []):
        if a.get('symbol') == symbol:
            return a
    return None


def _summarize_asset(asset: dict) -> str:
    """Summarize a single asset for shared context (~100 tokens)."""
    sym = asset['symbol']
    price = asset.get('latest_price', 0)
    cons = asset.get('consensio', {})
    total = cons.get('total', {})
    positions = total.get('positions', 0)
    max_pos = total.get('max_positions', 6)

    parts = [f"{sym}: ${price:,.2f}"]

    # Consensio signals
    signals = []
    for tf in ('1D', '3D', '1W'):
        sig = cons.get(tf, {}).get('signal', '')
        if sig:
            signals.append(f"{tf}={sig}")
    if signals:
        parts.append(f"consensio [{', '.join(signals)}] ({positions}/{max_pos})")

    # RSI
    rsi = asset.get('weekly_rsi')
    if rsi is not None:
        parts.append(f"wRSI={rsi:.1f}")

    # EMA Ribbon
    ema_ribbon = asset.get('ema_ribbon')
    if ema_ribbon and ema_ribbon.get('trend'):
        parts.append(f"EMA Ribbon={ema_ribbon['trend']}")

    # ADX
    adx = asset.get('adx')
    if adx and adx.get('adx') is not None:
        parts.append(f"ADX={adx['adx']:.0f}")

    # Funding (crypto only)
    fr = asset.get('funding_rate')
    if fr is not None:
        parts.append(f"funding={fr:.4f}%")

    # CPR
    cpr = asset.get('cpr')
    if cpr and cpr.get('price_position'):
        parts.append(f"CPR={cpr['price_position']}")

    # Realtime price change
    rt = asset.get('realtime')
    if rt and rt.get('change_pct') is not None:
        parts.append(f"24h={rt['change_pct']:+.1f}%")

    return " | ".join(parts)


def _detailed_asset_summary(asset: dict, timeframe: str) -> str:
    """Detailed single-asset summary for viewport context."""
    parts = []
    price = asset.get('latest_price', 0)
    parts.append(f"Price: ${price:,.2f}")

    # 24h change
    rt = asset.get('realtime')
    if rt and rt.get('change_pct') is not None:
        parts.append(f"24h change: {rt['change_pct']:+.1f}%")

    # Consensio details
    cons = asset.get('consensio', {})
    for tf in ('1D', '3D', '1W'):
        tf_data = cons.get(tf, {})
        if tf_data:
            sig = tf_data.get('signal', '?')
            pos = tf_data.get('positions', 0)
            max_pos = tf_data.get('max_positions', 3)
            parts.append(f"Consensio {tf}: signal={sig}, positions={pos}/{max_pos}")

    # RSI
    rsi = asset.get('weekly_rsi')
    if rsi is not None:
        parts.append(f"Weekly RSI: {rsi:.1f}")

    # RSI divergences
    div = asset.get('rsi_divergence', {})
    for tf_key in ('daily', 'weekly'):
        d = div.get(tf_key)
        if d and d.get('type'):
            parts.append(f"RSI divergence ({tf_key}): {d['type']}")

    # EMA Ribbon
    ema_ribbon = asset.get('ema_ribbon')
    if ema_ribbon:
        parts.append(f"EMA Ribbon: trend={ema_ribbon.get('trend', '?')}, position={ema_ribbon.get('position', '?')}, "
                     f"fast_ema=${ema_ribbon.get('fast_ema', 0):,.2f}, slow_ema=${ema_ribbon.get('slow_ema', 0):,.2f}, "
                     f"gap={ema_ribbon.get('ema_gap_pct', 0):.1f}%"
                     + (", converging" if ema_ribbon.get('is_converging') else "")
                     + (", squeeze" if ema_ribbon.get('is_squeeze') else ""))

    # ADX
    adx = asset.get('adx')
    if adx:
        parts.append(f"ADX: {adx.get('adx', 0):.1f} ({adx.get('regime', '?')}), "
                     f"+DI: {adx.get('plus_di', 0):.1f}, -DI: {adx.get('minus_di', 0):.1f}")

    # Bollinger Bands (with position %)
    bb = asset.get('bb')
    if bb:
        upper = bb.get('upper', 0)
        lower = bb.get('lower', 0)
        mid = bb.get('middle', 0)
        bb_range = upper - lower
        bb_pos = ((price - lower) / bb_range * 100) if bb_range > 0 else 50
        parts.append(f"BB: upper=${upper:,.2f}, mid=${mid:,.2f}, lower=${lower:,.2f}, "
                     f"squeeze={bb.get('squeeze_pct', 0):.0f}%, position={bb_pos:.0f}%"
                     + (", in squeeze" if bb.get('is_squeeze') else ""))

    # CPR (with distances and S/R levels)
    cpr = asset.get('cpr')
    if cpr and cpr.get('pivot'):
        pivot = cpr['pivot']
        tc = cpr.get('tc', 0)
        bc = cpr.get('bc', 0)
        pivot_dist = (price - pivot) / pivot * 100 if pivot else 0
        parts.append(f"Monthly CPR: pivot=${pivot:,.2f} ({pivot_dist:+.1f}% from price), "
                     f"TC=${tc:,.2f}, BC=${bc:,.2f}, "
                     f"position={cpr.get('price_position', '?')}, level={cpr.get('price_level', '?')}")
        # S/R levels
        sr_parts = []
        for level_name in ('r1', 'r2', 'r3', 's1', 's2', 's3'):
            val = cpr.get(level_name)
            if val and price:
                dist = (val - price) / price * 100
                sr_parts.append(f"{level_name.upper()}=${val:,.2f}({dist:+.1f}%)")
        if sr_parts:
            parts.append(f"CPR S/R: {', '.join(sr_parts)}")

    # Volume (enriched with actual numbers)
    vol = asset.get('volume_metrics')
    if vol and vol.get('current_volume') is not None:
        cur = vol['current_volume']
        avg = vol.get('volume_ma_20')
        ratio = vol.get('ratio', 0)
        if cur and avg and ratio > 0.01:
            above = "above avg" if vol.get('is_above_average') else "below avg"
            parts.append(f"Volume: {cur:,.0f} (20-bar avg: {avg:,.0f}, {ratio:.2f}x, {above})")
        elif cur:
            parts.append(f"Volume: {cur:,.0f} (average data unavailable)")
    elif vol:
        ratio = vol.get('ratio', 0)
        if ratio and ratio > 0.01:
            parts.append(f"Volume: {ratio:.2f}x avg"
                         + (" (above avg)" if vol.get('is_above_average') else " (below avg)"))

    # Funding rate (crypto)
    fr = asset.get('funding_rate')
    if fr is not None:
        sentiment = "longs paying" if fr > 0.01 else ("shorts paying" if fr < -0.01 else "neutral")
        parts.append(f"Funding rate: {fr:.4f}% ({sentiment})")

    # Structure
    struct = asset.get('structure')
    if struct:
        parts.append(f"Structure: {struct.get('structure_trend', '?')}, "
                     f"last pivot={struct.get('last_pivot_type', '?')}, "
                     f"last high={struct.get('last_high_type', '?')}, last low={struct.get('last_low_type', '?')}")

    return "\n".join(parts)


def _build_price_action_context(asset: str, timeframe: str) -> str:
    """Build recent price action, MACD, SMA distances, volume trend from OHLCV."""
    try:
        from carrac.services import get_services
        import pandas as pd

        db = get_services().db
        if db is None:
            return ""

        # Get data for the requested timeframe
        if timeframe in ('1h', '4h'):
            tf_data = db.get_ltf_multi_timeframe_data(asset)
            df = tf_data.get(timeframe, pd.DataFrame())
        else:
            tf_data = db.get_multi_timeframe_data(asset)
            tf_key = timeframe if timeframe in tf_data else '1D'
            df = tf_data.get(tf_key, pd.DataFrame())

        if df.empty or len(df) < 30:
            return ""

        lines = []
        price = float(df['close'].iloc[-1])

        # Last 5 candles changes
        if len(df) >= 6:
            changes = []
            for i in range(-5, 0):
                prev_c = float(df['close'].iloc[i - 1])
                cur_c = float(df['close'].iloc[i])
                pct = (cur_c - prev_c) / prev_c * 100 if prev_c else 0
                changes.append(f"{pct:+.1f}%")
            lines.append(f"Last 5 candles: [{', '.join(changes)}]")

        # 20-bar range position
        recent_20 = df.tail(20)
        high_20 = float(recent_20['high'].max())
        low_20 = float(recent_20['low'].min())
        if high_20 != low_20:
            pos = (price - low_20) / (high_20 - low_20) * 100
            lines.append(f"20-bar range: ${low_20:,.2f} - ${high_20:,.2f} (position: {pos:.0f}%)")

        # SMA distances
        sma_parts = []
        for period in (20, 50, 200):
            if len(df) >= period:
                sma_val = float(df['close'].rolling(period).mean().iloc[-1])
                if not pd.isna(sma_val) and sma_val > 0:
                    dist = (price - sma_val) / sma_val * 100
                    sma_parts.append(f"SMA{period}=${sma_val:,.2f}({dist:+.1f}%)")
        if sma_parts:
            lines.append(f"SMAs: {', '.join(sma_parts)}")

        # Golden/death cross
        if len(df) >= 200:
            sma50 = float(df['close'].rolling(50).mean().iloc[-1])
            sma200 = float(df['close'].rolling(200).mean().iloc[-1])
            if not pd.isna(sma50) and not pd.isna(sma200):
                lines.append(f"SMA50/200: {'golden cross' if sma50 > sma200 else 'death cross'}")

        # MACD (12, 26, 9)
        if len(df) >= 35:
            ema12 = df['close'].ewm(span=12, adjust=False).mean()
            ema26 = df['close'].ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            histogram = macd_line - signal_line

            ml = float(macd_line.iloc[-1])
            ms = float(signal_line.iloc[-1])
            mh = float(histogram.iloc[-1])
            prev_h = float(histogram.iloc[-2])

            if mh > 0 and mh > prev_h:
                momentum = "bullish expansion"
            elif mh > 0:
                momentum = "bullish contraction"
            elif mh < 0 and mh < prev_h:
                momentum = "bearish expansion"
            else:
                momentum = "bearish contraction"
            lines.append(f"MACD: line={ml:,.2f} signal={ms:,.2f} hist={mh:,.2f} ({momentum})")

        # ATR(14) — Average True Range for volatility context
        if len(df) >= 15:
            high = df['high'].astype(float)
            low = df['low'].astype(float)
            close_s = df['close'].astype(float)
            prev_close = close_s.shift(1)
            tr = pd.concat([
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs()
            ], axis=1).max(axis=1)
            atr_val = float(tr.rolling(14).mean().iloc[-1])
            if not pd.isna(atr_val) and price > 0:
                atr_pct = atr_val / price * 100
                lines.append(f"ATR(14): ${atr_val:,.2f} ({atr_pct:.1f}%)")

        # Volume trend (last 5 vs prior 5 bars)
        if 'volume' in df.columns and len(df) >= 10:
            recent_5 = float(df['volume'].iloc[-5:].mean())
            prior_5 = float(df['volume'].iloc[-10:-5].mean())
            if prior_5 > 0:
                vol_trend = (recent_5 - prior_5) / prior_5 * 100
                direction = "rising" if vol_trend > 10 else ("falling" if vol_trend < -10 else "flat")
                lines.append(f"Volume trend: {direction} ({vol_trend:+.1f}% last 5 vs prior 5 bars)")

        return "\n".join(lines) if lines else ""

    except Exception as e:
        logger.debug(f"Price action context error: {e}")
        return ""


def _format_position_drawing(d, current_price=None) -> str:
    """Extract entry/SL/TP from a position drawing's metadata.points.

    KLineChart position overlay structure:
      points[0] = entry price
      points[1] = stop loss
      points[2] = outer TP boundary (the max zone edge, NOT a real trading target)
      extendData.tps = list of actual intermediate TP prices (user-placed targets)

    If extendData.tps exists, those are the real TPs. points[2] is just the zone boundary.
    If current_price provided, enriches with live P&L, distance to SL/TP, and trade age.
    """
    meta = d.get('metadata') or {}
    if isinstance(meta, str):
        import json as _json
        try:
            meta = _json.loads(meta)
        except (ValueError, TypeError):
            meta = {}

    points = meta.get('points', [])
    entry = d.get('price_level') or (points[0].get('value') if points else None)
    if not entry or len(points) < 2:
        return None

    dtype = d.get('drawing_type', '')
    label = d.get('label', '')

    # Determine direction: points[1] = SL, so if SL < entry → LONG
    if dtype in ('long_position', 'longPosition'):
        direction = 'LONG'
    elif dtype in ('short_position', 'shortPosition'):
        direction = 'SHORT'
    elif len(points) >= 2:
        sl_point = points[1].get('value', 0)
        direction = 'LONG' if sl_point < entry else 'SHORT'
    else:
        above = any(p.get('value', 0) > entry for p in points[1:])
        direction = 'LONG' if above else 'SHORT'

    # Extract SL from points[1]
    sl_val = points[1].get('value') if len(points) >= 2 else None

    # Extract TPs: extendData.tps (intermediate targets) + points[2] (zone boundary TP)
    explicit_tps = meta.get('tps') or d.get('tps') or []
    tp_vals = [v for v in explicit_tps if isinstance(v, (int, float))]
    # Always include points[2] (the outer zone boundary) as a TP if present
    if len(points) >= 3:
        outer_tp = points[2].get('value')
        if outer_tp is not None and outer_tp not in tp_vals:
            tp_vals.append(outer_tp)

    # Sort TPs by distance from entry (nearest first)
    tp_vals.sort(key=lambda v: abs(v - entry))

    parts = [f"- {direction} position"]
    if label:
        parts[0] += f" \"{label}\""
    parts[0] += f": Entry ${entry:,.2f}"

    if sl_val is not None:
        sl_pct = (sl_val - entry) / entry * 100
        parts.append(f"SL ${sl_val:,.2f} ({sl_pct:+.2f}%)")
    for i, tp in enumerate(tp_vals):
        tp_pct = (tp - entry) / entry * 100
        tp_label = f"TP{i+1}" if len(tp_vals) > 1 else "TP"
        parts.append(f"{tp_label} ${tp:,.2f} ({tp_pct:+.2f}%)")
    if sl_val is not None and tp_vals and sl_val != entry:
        # R:R based on first (nearest) TP
        rr = abs(tp_vals[0] - entry) / abs(entry - sl_val)
        parts.append(f"R:R {rr:.2f}")
        if len(tp_vals) > 1:
            rr_max = abs(tp_vals[-1] - entry) / abs(entry - sl_val)
            parts.append(f"max R:R {rr_max:.2f}")

    # Enrich with current price context (live P&L, distance to SL/TP)
    if current_price and entry:
        pnl_pct = (current_price - entry) / entry * 100
        if direction == 'SHORT':
            pnl_pct = -pnl_pct
        if abs(pnl_pct) < 0.05:
            state = 'AT ENTRY'
        elif pnl_pct > 0:
            state = 'IN PROFIT'
        else:
            state = 'IN DRAWDOWN'
        parts.append(f"Current ${current_price:,.2f} ({pnl_pct:+.1f}% {state})")
        # Distance from current price to SL and TP1
        if sl_val:
            if direction == 'LONG':
                dist_sl = (current_price - sl_val) / current_price * 100
            else:
                dist_sl = (sl_val - current_price) / current_price * 100
            parts.append(f"dist-to-SL {dist_sl:.1f}%")
        if tp_vals:
            if direction == 'LONG':
                dist_tp1 = (tp_vals[0] - current_price) / current_price * 100
            else:
                dist_tp1 = (current_price - tp_vals[0]) / current_price * 100
            parts.append(f"dist-to-TP1 {dist_tp1:.1f}%")

    # Trade age from entry timestamp
    entry_ts = points[0].get('timestamp') if points else None
    if entry_ts:
        import time as _time
        now = _time.time()
        ts = entry_ts / 1000 if entry_ts > 1e12 else entry_ts
        age_sec = now - ts
        if 0 < age_sec < 365 * 86400:  # sanity: positive and < 1 year
            if age_sec < 3600:
                parts.append(f"age {int(age_sec / 60)}m")
            elif age_sec < 86400:
                parts.append(f"age {age_sec / 3600:.0f}h")
            else:
                parts.append(f"age {age_sec / 86400:.1f}d")

    return " | ".join(parts)


def _format_drawings(drawings: list, current_price: float = None) -> str:
    """Format user drawings as context text."""
    lines = [f"\nUser drawings ({len(drawings)}):"]
    for d in drawings[:15]:  # limit to prevent token bloat
        dtype = d.get('drawing_type', '?')

        # Position drawings: extract full entry/SL/TP
        if dtype in ('position', 'long_position', 'short_position',
                      'longPosition', 'shortPosition'):
            pos_desc = _format_position_drawing(d, current_price)
            if pos_desc:
                lines.append(pos_desc)
                continue

        label = d.get('label', '')
        price = d.get('price_level')
        upper = d.get('price_level_upper')

        desc = f"- {dtype}"
        if label:
            # escape_user_text wraps the value in <drawing_label>…
            from carrac.grounding import escape_user_text as _esc
            desc += f" {_esc(label, 'drawing_label', max_chars=120)}"
        if price is not None:
            desc += f" @ ${price:,.2f}"
        if upper is not None:
            desc += f" - ${upper:,.2f}"
        lines.append(desc)
    if len(drawings) > 15:
        lines.append(f"  ... and {len(drawings) - 15} more")
    return "\n".join(lines)


def _format_scenarios(db, scenarios: list, current_price: float = None) -> str:
    """Format user scenarios with their child drawings."""
    lines = [f"\nScenarios ({len(scenarios)}):"]
    for s in scenarios[:5]:
        meta = s.get('metadata_parsed') or {}
        if isinstance(s.get('metadata'), str):
            import json
            try:
                meta = json.loads(s['metadata'])
            except (json.JSONDecodeError, TypeError):
                pass
        if meta.get('archived'):
            continue
        # Scenario name + child drawing labels are user-authored
        # free text. escape_user_text wraps in <scenario_name>…</scenario_name>.
        from carrac.grounding import escape_user_text as _esc
        label = s.get('label', '') or meta.get('name', f"Scenario #{s['id']}")
        lines.append(f"- {_esc(str(label), 'scenario_name', max_chars=120)}")

        children = db.drawing_get_scenario_children(s['id'])
        for c in children[:8]:
            ctype = c.get('drawing_type', '?')
            # Position drawings: extract full entry/SL/TP
            if ctype in ('position', 'long_position', 'short_position',
                          'longPosition', 'shortPosition'):
                pos_desc = _format_position_drawing(c, current_price)
                if pos_desc:
                    lines.append('  ' + pos_desc.lstrip('- '))
                    continue
            clabel = c.get('label', '')
            cprice = c.get('price_level')
            desc = f"  {ctype}"
            if clabel:
                desc += f" {_esc(clabel, 'drawing_label', max_chars=120)}"
            if cprice is not None:
                desc += f" @ ${cprice:,.2f}"
            lines.append(desc)
    return "\n".join(lines)


def _build_report_context(asset: str) -> str:
    """Extract asset-specific + macro context from latest morning and afternoon reports."""
    try:
        from carrac.db_pool import get_conn as _pool_get_conn

        # Fetch both report rows inside a single RAII connection so no pool
        # slot stays pinned.
        with _pool_get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT summary, content, created_at FROM reports "
                "WHERE report_type='morning' ORDER BY created_at DESC LIMIT 1"
            )
            morning = cursor.fetchone()
            cursor.execute(
                "SELECT content, created_at FROM reports "
                "WHERE report_type='afternoon' ORDER BY created_at DESC LIMIT 1"
            )
            afternoon = cursor.fetchone()

        sections = []

        # --- Morning report: summary or content ---
        if morning:
            text = morning['summary'] or morning['content'] or ''
            if text:
                # Extract asset-specific lines + macro context
                asset_upper = asset.upper()
                # Macro keywords always relevant
                macro_kw = ('macro', 'regime', 'dxy', 'fear', 'greed', 'cpi', 'fomc',
                            'fed', 'inflation', 'recession', 'catalyst', 'nfib',
                            'risk-off', 'risk-on', 'rotation', 'capitulation')
                relevant = []
                for line in text.split('\n'):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    line_lower = stripped.lower()
                    # Include if mentions the asset or is macro context
                    if asset_upper.lower() in line_lower or any(kw in line_lower for kw in macro_kw):
                        relevant.append(stripped)

                if relevant:
                    # Cap at ~1200 chars to stay under budget
                    combined = '\n'.join(relevant)
                    if len(combined) > 1200:
                        combined = combined[:1200] + '...'
                    sections.append(
                        # str() first: Postgres/RealDictCursor hands back a
                        # real datetime for created_at, not subscriptable —
                        f"--- Morning Analysis ({str(morning['created_at'])[:10]}) ---\n"
                        f"<morning_analysis>\n{combined}\n</morning_analysis>"
                    )

        # --- Afternoon delta report ---
        if afternoon and afternoon['content']:
            text = afternoon['content']
            asset_upper = asset.upper()
            relevant = []
            for line in text.split('\n'):
                stripped = line.strip()
                if not stripped:
                    continue
                line_lower = stripped.lower()
                # Session summary always relevant, plus asset-specific lines
                if (asset_upper.lower() in line_lower or
                        stripped.startswith('### 1.') or
                        'session' in line_lower and 'summary' in line_lower):
                    relevant.append(stripped)

            if relevant:
                combined = '\n'.join(relevant)
                if len(combined) > 800:
                    combined = combined[:800] + '...'
                sections.append(
                    f"--- Afternoon Update ({str(afternoon['created_at'])[:10]}) ---\n"
                    f"<afternoon_delta>\n{combined}\n</afternoon_delta>"
                )

        return '\n\n'.join(sections) if sections else ''

    except Exception as e:
        logger.debug(f"Report context error: {e}")
        return ''


def _format_alerts(alerts: list) -> str:
    """Format active alerts as context text."""
    lines = [f"\nActive alerts ({len(alerts)}):"]
    for a in alerts[:10]:
        cond = a.get('condition', '?')
        price = a.get('price', 0)
        label = a.get('label', '')
        desc = f"- {cond} ${price:,.2f}"
        if label:
            # Alert label is user-authored free text — neutralize inline before prompt
            from carrac.grounding import neutralize_inline
            desc += f" ({neutralize_inline(label, max_chars=120)})"
        lines.append(desc)
    return "\n".join(lines)


def build_trader_profile_context(user_id: int, feature: str = 'chat') -> str:
    """Build trader profile as rich behavioral AI instructions (~500-700 tokens).

    Transforms structured checkbox/select data into natural-language paragraphs
    with actionable AI instructions (e.g. "when this trader seems eager to enter,
    challenge the timing"). Same data, much richer output.

    feature: the AI feature being used. For active trading features (validate,
    challenge, checklist), HODLer-specific data is excluded.
    """
    lines = []

    # 1. Stated profile — rich behavioral paragraphs
    try:
        from user_db import user_db
        prefs = user_db.get_preferences(user_id)
        profile = prefs.get('trading_profile', {})
        if profile:
            # ── Label maps ──
            style_desc = {
                'hodl': 'a long-term holder who buys and holds through volatility',
                'swing': 'a swing trader catching multi-day to multi-week moves',
                'day': 'a day trader who opens and closes positions within the same day',
                'scalp': 'a scalper trading very short timeframes with tight risk management',
            }
            holding_desc = {
                'minutes': 'minutes', 'hours': 'hours',
                'days_to_weeks': 'days to weeks', 'weeks_to_months': 'weeks to months',
                'months_plus': 'months or longer',
            }
            freq_desc = {
                'constantly': 'constantly throughout the day',
                'few_times_daily': 'a few times daily',
                'daily': 'once daily', 'few_times_weekly': 'a few times per week',
            }
            entry_desc = {
                'htf_trend_aligned': 'higher timeframe trend alignment — always lead analysis with the weekly/daily direction',
                'key_level_test': 'price reaction at a significant support/resistance level',
                'indicator_confluence': 'confluence across multiple indicators',
                'volume_confirmation': 'volume confirmation validating the move',
                'pullback_to_ma': 'pullback to moving average support',
                'breakout_retest': 'breakout followed by a retest of the broken level',
                'divergence': 'divergence between price and indicators',
                'funding_rate_extreme': 'extreme funding rates signaling crowded positioning',
                'sentiment_extreme': 'extreme fear/greed readings',
                'catalyst_required': 'a specific news catalyst or event',
                'pattern_completion': 'a fully formed chart pattern',
            }
            exit_desc = {
                'fixed_target': 'pre-set take-profit targets',
                'trailing_stop': 'trailing stops that lock in profit as price moves',
                'indicator_signal': 'indicator signals (e.g., RSI overbought)',
                'time_based': 'time-based exits after a set holding period',
                'scale_out': 'scaling out at multiple levels',
                'invalidation': 'cutting positions when the thesis is invalidated',
                'technical_level_target': 'exits at the next major technical level',
                'momentum_exit': 'exits when momentum reverses',
            }
            rr_lbl = {'1_to_1': '1:1', '1.5_to_1': '1.5:1', '2_to_1': '2:1', '3_to_1': '3:1', '5_plus': '5:1+'}
            rpt_lbl = {
                'under_half_pct': 'under 0.5%', 'half_to_1_pct': '0.5–1%',
                '1_to_2_pct': '1–2%', '2_to_3_pct': '2–3%',
                '3_to_5_pct': '3–5%', 'over_5_pct': 'over 5%', 'no_fixed': 'variable',
            }
            pos_lbl = {'1_to_2': '1–2', '3_to_5': '3–5', '6_to_10': '6–10', 'unlimited': 'unlimited'}
            dd_lbl = {'under_5': 'under 5%', '5_to_10': '5–10%', '10_to_20': '10–20%', '20_to_50': '20–50%', 'any': 'any level'}
            setup_desc = {
                'breakout': 'breakout entries', 'pullback': 'pullbacks within trends',
                'reversal': 'reversals at extremes', 'range': 'range-bound trading',
                'trend_follow': 'trend following', 'mean_revert': 'mean reversion',
            }
            # The trader's OWN self-reported habits. The model mirrors them
            # back against the trader's own stated rules — it never tells
            # them what to do with a position.
            emotional_ai = {
                'hesitation_on_setups': 'They say they hesitate on valid setups. When the conditions they set themselves are met, say so plainly.',
                'fomo_early_entry': 'They say they tend to enter too early (FOMO). When they seem eager, point out which of their own conditions are not yet met.',
                'revenge_trading': 'They say they are prone to revenge trading after losses. If a new idea follows a loss quickly, name that pattern.',
                'overconfidence_after_wins': 'They say they get overconfident after wins. After a win streak, compare any new idea against the sizing rule in their own profile.',
                'overtrading_frustration': 'They say boredom leads to overtrading. If no setup meets their criteria, say so plainly.',
                'holding_losers': 'They say they hold losing positions too long. When a position is underwater, restate the exit rule from their own plan.',
                'watching_winners_reverse': 'They say they watch winners reverse. When a position is in profit, note how far it has come and where their own plan says it ends.',
                'moving_stops': 'They say they move stops emotionally. When stops come up, restate the stop level from their own plan.',
            }

            # ── Active features filter ──
            active_features = ('validate', 'challenge', 'checklist')
            raw_styles = profile.get('styles', [])
            if feature in active_features:
                raw_styles = [s for s in raw_styles if s != 'hodl']

            # ── Paragraph 1: Identity ──
            if raw_styles or profile.get('risk') or profile.get('experience'):
                parts = []
                if raw_styles:
                    descs = [style_desc.get(s, s) for s in raw_styles]
                    parts.append(f"This trader is {' and '.join(descs)}")
                risk = profile.get('risk', '')
                if risk:
                    parts.append(f"with {risk} risk tolerance")
                holding = profile.get('holding_period')
                if holding:
                    parts.append(f"holding positions for {holding_desc.get(holding, holding)}")
                freq = profile.get('check_frequency')
                if freq:
                    parts.append(f"checking charts {freq_desc.get(freq, freq)}")
                exp = profile.get('experience')
                focus = profile.get('focus')
                if exp or focus:
                    extras = []
                    if exp:
                        extras.append(f"{exp} experience")
                    if focus and focus != 'both':
                        extras.append(f"focused on {focus}")
                    elif focus == 'both':
                        extras.append("trading both crypto and stocks")
                    parts.append(f"with {' and '.join(extras)}")
                lines.append('. '.join([p.rstrip('.') for p in parts if p]) + '.')

            # ── Paragraph 2: Entry & Exit Methodology ──
            entries = profile.get('entry_conditions', [])
            exits = profile.get('exit_strategy', [])
            if entries or exits:
                para = []
                if entries:
                    numbered = ', '.join(f"({i+1}) {entry_desc.get(e, e.replace('_', ' '))}" for i, e in enumerate(entries))
                    para.append(f"Their entry methodology requires: {numbered}. They won't enter without these conditions met.")
                if exits:
                    exit_strs = [exit_desc.get(e, e.replace('_', ' ')) for e in exits]
                    para.append(f"For exits, they use {', '.join(exit_strs)}.")

                # Risk parameters
                risk_parts = []
                rr = profile.get('typical_rr')
                if rr:
                    risk_parts.append(f"Target: {rr_lbl.get(rr, rr)} reward-to-risk")
                rpt = profile.get('risk_per_trade')
                if rpt:
                    risk_parts.append(f"risking {rpt_lbl.get(rpt, rpt)} per trade")
                mp = profile.get('max_concurrent_positions')
                if mp:
                    risk_parts.append(f"across {pos_lbl.get(mp, mp)} concurrent positions")
                dd = profile.get('max_drawdown_comfort')
                if dd:
                    risk_parts.append(f"comfortable with {dd_lbl.get(dd, dd)} portfolio drawdown")
                if risk_parts:
                    para.append(', '.join(risk_parts) + '.')

                lines.append('\n' + ' '.join(para))

            # ── Paragraph 3: Technical Toolkit ──
            setups = profile.get('setups', [])
            indicators = profile.get('trusted_indicators', [])
            patterns = profile.get('trusted_patterns', [])
            if setups or indicators or patterns:
                para = []
                if setups:
                    setup_strs = [setup_desc.get(s, s.replace('_', ' ')) for s in setups]
                    para.append(f"Preferred setups: {', '.join(setup_strs)}.")
                if indicators:
                    # trusted_indicators is user-authored free text — neutralize inline before prompt
                    from carrac.grounding import neutralize_inline
                    escaped_indicators = ', '.join(neutralize_inline(str(i), max_chars=80) for i in indicators)
                    para.append(f"They trust {escaped_indicators} as core indicators. When analyzing charts, emphasize these indicators and call out when they agree or diverge.")
                if patterns:
                    pattern_strs = [p.replace('_', ' ') for p in patterns]
                    strict = profile.get('pattern_strictness', '').replace('_', ' ')
                    para.append(f"They look for {', '.join(pattern_strs)} patterns{f' with {strict} strictness' if strict else ''}.")
                usage = profile.get('indicator_usage', '').replace('_', ' ')
                if usage and usage != 'dont use':
                    para.append(f"Indicator role: {usage}.")
                lines.append('\n' + ' '.join(para))

            # ── Paragraph 4: Psychology & Behavioral Notes ──
            emotional = profile.get('emotional_patterns', [])
            routine = profile.get('pre_trading_routine', '')
            stress = profile.get('stress_response', '')
            if emotional or routine or stress:
                para = []
                if emotional:
                    para.append("Self-identified behavioral patterns:")
                    for e in emotional:
                        ai_instruction = emotional_ai.get(e)
                        if ai_instruction:
                            para.append(ai_instruction)
                if routine:
                    para.append(f"Pre-trading routine: {routine.replace('_', ' ')}.")
                if stress:
                    para.append(f"Under stress they: {stress.replace('_', ' ')}.")
                lines.append('\n' + ' '.join(para))

            # ── Paragraph 5: Market Adaptation ──
            trend = profile.get('trend_approach', '').replace('_', ' ')
            vol_adj = profile.get('volatility_adjustment', '').replace('_', ' ')
            fund_bal = profile.get('fundamental_balance', '').replace('_', ' ')
            depth = profile.get('analysis_depth', '').replace('_', ' ')
            adapt_parts = []
            if trend:
                adapt_parts.append(f"trend approach: {trend}")
            if vol_adj:
                adapt_parts.append(f"in high volatility: {vol_adj}")
            if fund_bal:
                adapt_parts.append(f"analysis style: {fund_bal}")
            if depth:
                adapt_parts.append(f"analysis depth: {depth}")
            if adapt_parts:
                lines.append('\n' + 'Market adaptation: ' + '; '.join(adapt_parts) + '.')

            # ── Paragraph 6: Current Market View ──
            bias = profile.get('current_bias')
            if bias:
                bias_str = bias.replace('_', ' ')
                horizon = profile.get('bias_horizon', '').replace('_', ' ')
                reasoning = profile.get('bias_reasoning', '')
                line = f"Current market view: {bias_str}"
                if horizon:
                    line += f" on {horizon} horizon"
                line += '.'
                if reasoning:
                    from carrac.grounding import escape_user_text
                    line += f" Reasoning:\n{escape_user_text(reasoning, 'bias_reasoning', max_chars=1000)}"
                lines.append('\n' + line)

            # ── Edge / notes (user free-text) — quarantine to block injection ──
            if profile.get('notes'):
                from carrac.grounding import escape_user_text
                lines.append(
                    f"\nTrading edge (in their words):\n"
                    f"{escape_user_text(profile['notes'], 'trader_notes', max_chars=2000)}"
                )

            # ── HODLer specific (skip for active trading features) ──
            if 'hodl' in profile.get('styles', []) and feature not in active_features:
                hodl_parts = []
                hs = profile.get('hodl_strategy', '').replace('_', ' ').upper()
                if hs:
                    hodl_parts.append(f"strategy: {hs}")
                core = profile.get('hodl_core_assets', [])
                if core:
                    # hodl_core_assets is user-authored free text — neutralize inline before prompt
                    from carrac.grounding import neutralize_inline
                    escaped_core = ', '.join(neutralize_inline(str(a), max_chars=60) for a in core)
                    hodl_parts.append(f"core holdings (never sell): {escaped_core}")
                hbc = profile.get('hodl_buy_conditions')
                if hbc:
                    # hodl_buy_conditions is user free-text from
                    # the trader profile form. Quarantine before prompt.
                    from carrac.grounding import escape_user_text
                    hodl_parts.append(
                        f"buy conditions: {escape_user_text(hbc, 'hodl_buy_conditions', max_chars=400)}"
                    )
                if hodl_parts:
                    lines.append(f"\nHODLer profile: {'; '.join(hodl_parts)}.")

            # ── AI preferences ──
            ai_parts = []
            verbosity = profile.get('ai_verbosity')
            if verbosity and verbosity != 'balanced':
                ai_parts.append(f"prefers {verbosity} responses")
            if profile.get('ai_devil_advocate'):
                ai_parts.append("wants devil's advocate challenges")
            if profile.get('ai_risk_warnings'):
                ai_parts.append("expects explicit risk callouts")
            focus_areas = profile.get('report_focus_areas', [])
            if focus_areas:
                ai_parts.append(f"report focus: {', '.join(a.replace('_', ' ') for a in focus_areas)}")
            if ai_parts:
                lines.append(f"\nAI preferences: {'; '.join(ai_parts)}.")

    except Exception:
        pass

    # 2. Journal reality check (revealed preferences)
    try:
        from carrac.content_db import get_content_db
        db = get_content_db()
        analytics = db.get_journal_analytics(user_id, days=90)
        if analytics and analytics.get('trades', 0) >= 5:
            parts = []
            parts.append(f"{analytics['trades']} trades (90d)")
            parts.append(f"WR: {analytics['win_rate']}%")
            if analytics.get('expectancy') is not None:
                parts.append(f"expectancy: {analytics['expectancy']:+.2f}R")
            if analytics.get('profit_factor') is not None:
                parts.append(f"PF: {analytics['profit_factor']}")

            # Direction performance
            by_dir = analytics.get('by_direction', {})
            for d in ('long', 'short'):
                dd = by_dir.get(d, {})
                if dd.get('trades', 0) >= 3:
                    parts.append(f"{d}s: {dd['win_rate']}% WR ({dd['trades']})")

            # Best/worst setups
            by_setup = analytics.get('by_setup', {})
            if len(by_setup) > 1:
                sorted_setups = sorted(by_setup.items(), key=lambda x: x[1].get('win_rate', 0), reverse=True)
                best = sorted_setups[0]
                worst = sorted_setups[-1]
                if best[1].get('trades', 0) >= 3:
                    parts.append(f"best setup: {best[0]} ({best[1]['win_rate']}% WR)")
                if worst[1].get('trades', 0) >= 3 and worst[0] != best[0]:
                    parts.append(f"worst setup: {worst[0]} ({worst[1]['win_rate']}% WR)")

            # Current streak
            streak = analytics.get('current_streak', {})
            if streak.get('count', 0) >= 3:
                parts.append(f"current streak: {streak['count']}{streak['type'][0].upper()}")

            lines.append("Journal stats: " + ", ".join(parts))
    except Exception:
        pass

    return "\n".join(lines) if lines else ""


def get_user_scenarios(user_id: int, asset: str = None) -> list:
    """Get user's non-archived scenarios for the scenario selector UI."""
    try:
        from carrac.content_db import get_content_db
        import json
        db = get_content_db()
        scenarios = db.drawing_list(
            asset=asset, drawing_type='scenario', user_id=user_id
        )
        result = []
        for s in scenarios:
            meta = {}
            if isinstance(s.get('metadata'), str):
                try:
                    meta = json.loads(s['metadata'])
                except (json.JSONDecodeError, TypeError):
                    pass
            if meta.get('archived'):
                continue
            result.append({
                'id': s['id'],
                'asset': s.get('asset', ''),
                'timeframe': s.get('timeframe', ''),
                'label': s.get('label', '') or meta.get('name', f"Scenario #{s['id']}"),
            })
        return result
    except Exception as e:
        logger.error(f"get_user_scenarios error: {e}")
        return []


def get_asset_list() -> list:
    """Get list of all available asset symbols for the UI selector."""
    try:
        from carrac.precompute import response_cache
        cached = response_cache.get('htf')
        if cached:
            return [a['symbol'] for a in cached.get('assets', []) if a.get('symbol')]
    except Exception:
        pass

    try:
        from database import OHLCVDatabase
        db = OHLCVDatabase()
        return db.get_all_symbols()
    except Exception:
        return []
