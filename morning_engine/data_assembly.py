"""Data assembly — fetch chart data, context, screenshots from dashboard APIs."""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

from .config import (
    CHART_PAIRS, DASHBOARD_API_KEY, DASHBOARD_BASE_URL, ENABLE_MORNING_REPORT,
)

log = logging.getLogger("morning_engine.data")


# --------------------------------------------------------------------------
# Min-N gate for conditional return engine results
# --------------------------------------------------------------------------

def passes_min_n(result, min_n=25):
    """Return True only when a conditional query result has enough instances.

    Never surface a stat below min_n — avoids reporting noise from tiny
    historical samples. Always phrase conditional stats as "in N similar
    instances…" (not as a probability).

    Args:
        result: dict returned by query_conditional / /api/quant/conditional
        min_n: minimum instance count (default 25)

    Returns:
        bool
    """
    return bool(result) and int(result.get("instances", 0)) >= min_n


# --------------------------------------------------------------------------
# HTTP helpers with retry
# --------------------------------------------------------------------------


def _fmt_num(value, spec):
    """Format a number, or 'n/a' when the API handed back None."""
    try:
        return format(value, spec) if value is not None else 'n/a'
    except (TypeError, ValueError):
        return 'n/a'


def _api(path, timeout=15, retries=2):
    """Fetch JSON from dashboard API with retry + exponential backoff."""
    url = f"{DASHBOARD_BASE_URL}{path}"
    # Use X-Internal-Key for server-to-server calls so the credential is
    # never emitted as a ?key= URL query param (which lands in access logs).
    headers = {"X-Internal-Key": DASHBOARD_API_KEY}
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            if attempt < retries:
                delay = 2 ** attempt  # 1s, 2s
                log.debug("API retry %d/%d for %s (%.0fs delay): %s",
                          attempt + 1, retries, path, delay, e)
                time.sleep(delay)
    log.warning("API fetch failed %s after %d attempts: %s", path, retries + 1, last_err)
    return {"error": str(last_err)}


# --------------------------------------------------------------------------
# Chart data (structured text + raw data for signal extraction)
# --------------------------------------------------------------------------

def _format_asset_tf(data, asset, tf):
    """Format one asset/TF into structured text lines."""
    lines = []
    ohlcv = data.get("ohlcv", [])
    volume = data.get("volume", [])
    rsi = data.get("rsi", [])
    macd = data.get("macd", {})
    cpr = data.get("cpr", {})
    ind = data.get("indicators", {})

    if not ohlcv:
        return f"### {asset} — {tf}\nData unavailable: {data.get('error', 'empty')}\n"

    # Defensive bar access — schema drift or zero-volume halts can produce
    # bars missing keys; degrade gracefully rather than crash the report.
    try:
        last = ohlcv[-1]
        prev = ohlcv[-2] if len(ohlcv) > 1 else last
        price = last["close"]
        bar_chg = (price - prev["close"]) / prev["close"] * 100 if prev.get("close") else 0
        week_ref = ohlcv[-6] if len(ohlcv) >= 6 else ohlcv[0]
        week_chg = (price - week_ref["close"]) / week_ref["close"] * 100 if week_ref.get("close") else 0
        dt = datetime.fromtimestamp(last["time"], tz=timezone.utc).strftime("%Y-%m-%d")
        # "1 bar" is a claim about TIME. If a cache is missing a bar,
        # bar-over-bar spans two days, and the free public brief would print
        # "Gold surged +5.6% today" off a pair that covers two sessions.
        #
        # Deliberately NOT a threshold-based guard. Distinguishing a real hole
        # from a weekend or a public holiday needs a trading calendar we do not
        # have, and a naive day-count would fire on every holiday until it was
        # ignored. Instead the span is stated as a FACT whenever
        # it is longer than a day: no calibration, no false positives, and the
        # model cannot call a two-day move "today" when the two dates are
        # printed next to it. A Fri->Mon bar honestly reads "spans 3 days".
        span_note = ""
        try:
            _prev_dt = datetime.fromtimestamp(prev["time"], tz=timezone.utc)
            _span = (datetime.fromtimestamp(last["time"], tz=timezone.utc) - _prev_dt).days
            # Fix 4: scoped to daily-scale timeframes.
            # 1D/4H bars are naturally ~1 calendar day (or less) apart, so a
            # >1-day gap is a genuine fact worth stating. 1W/3D/1M
            # bars are SEVERAL days apart BY CONSTRUCTION — the same check
            # would fire on every single weekly row ("spans 7 days" on every
            # week), which is noise, not a finding. `tf` is already a
            # parameter of this function; no threading needed.
            if prev is not last and _span > 1 and tf.upper() not in ("1W", "3D", "1M"):
                span_note = f", {_prev_dt.strftime('%Y-%m-%d')}->{dt} spans {_span} days"
        except (KeyError, TypeError, ValueError, OSError):
            span_note = ""
    except (KeyError, TypeError, ValueError) as e:
        return f"### {asset} — {tf}\nData unavailable: malformed bar ({e})\n"

    lines.append(f"### {asset} — {tf} ({dt})")
    # "(5 bars)" alone read as interchangeable with the RS
    # Rankings table's "7d Return" — a DIFFERENT window on a DIFFERENT data
    # pull, so the two disagreed by 1.3-2.3pp per asset on the same report.
    # "5 trading bars" makes the unit explicit so the two can't be read as
    # restating the same fact (see also the RS table's "7-bar Return" label).
    lines.append(f"Price: {price:,.2f} | Chg: {bar_chg:+.1f}% (1 bar{span_note}) | {week_chg:+.1f}% (5 trading bars)")
    lines.append(f"OHLC: O={last['open']:,.2f} H={last['high']:,.2f} L={last['low']:,.2f} C={last['close']:,.2f}")

    # RSI
    if rsi:
        rv = rsi[-1]["value"]
        zone = "OVERSOLD" if rv < 30 else ("OVERBOUGHT" if rv > 70 else "neutral")
        lines.append(f"RSI(14): {rv:.1f} ({zone})")

    # MACD
    ml = macd.get("line", [])
    ms = macd.get("signal", [])
    mh = macd.get("histogram", [])
    if ml and ms and mh:
        mlv, msv, mhv = ml[-1]["value"], ms[-1]["value"], mh[-1]["value"]
        prev_h = mh[-2]["value"] if len(mh) > 1 else 0
        if mhv > 0:
            mom = "bullish expansion" if mhv > prev_h else "bullish contraction"
        else:
            mom = "bearish expansion" if mhv < prev_h else "bearish contraction"
        lines.append(f"MACD: Line={mlv:,.2f} Signal={msv:,.2f} Hist={mhv:,.2f} ({mom})")

    # Monthly CPR — levels can be present-but-None (upstream calc gaps);
    # skip those rather than crash the report or print a
    # fake 0.00 level the LLM would treat as real.
    if cpr and cpr.get("pivot") is not None and cpr.get("tc") is not None and cpr.get("bc") is not None:
        pivot, tc, bc = cpr["pivot"], cpr["tc"], cpr["bc"]
        pos = "ABOVE TC" if price > tc else ("BELOW BC" if price < bc else "INSIDE CPR")
        lines.append(f"Monthly CPR: Pivot={pivot:,.2f} TC={tc:,.2f} BC={bc:,.2f} | Price {pos}")
        s_parts = [f"{k.upper()}={cpr[k]:,.2f}" for k in ("s1", "s2", "s3") if cpr.get(k) is not None]
        r_parts = [f"{k.upper()}={cpr[k]:,.2f}" for k in ("r1", "r2", "r3") if cpr.get(k) is not None]
        if s_parts or r_parts:
            lines.append("  " + " ".join(s_parts) + (" | " if s_parts and r_parts else "") + " ".join(r_parts))
        history = cpr.get("history", [])
        if history:
            hs = []
            for h in history[:5]:
                ps = h.get("periodStart", 0)
                if ps and h.get("pivot") is not None:
                    mn = datetime.fromtimestamp(ps, tz=timezone.utc).strftime("%b")
                    hs.append(f"{mn}={h['pivot']:,.0f}")
            if hs:
                lines.append(f"  CPR History: {' | '.join(hs)}")

    # EMA Ribbon
    ema_ribbon = ind.get("ema_ribbon", {})
    fast = ema_ribbon.get("fast", [])
    slow = ema_ribbon.get("slow", [])
    fill = ema_ribbon.get("fill", [])
    if fast and slow and fill:
        fv, sv = fast[-1]["value"], slow[-1]["value"]
        trend = fill[-1]["trend"].upper()
        pv = "above both" if price > max(fv, sv) else ("below both" if price < min(fv, sv) else "between")
        lines.append(f"EMA Ribbon: Fast={fv:,.2f} Slow={sv:,.2f} Trend={trend} (price {pv})")

    # SMA 20/200
    sma_data = ind.get("smaLines", {})
    sma20 = sma_data.get("sma20", [])
    sma50 = sma_data.get("sma50", [])
    sma200 = sma_data.get("sma200", [])
    if sma20 or sma200:
        parts = []
        if sma20:
            s20 = sma20[-1]["value"]
            parts.append(f"SMA20={s20:,.2f} ({(price - s20) / s20 * 100:+.1f}%)")
        if sma200:
            s200 = sma200[-1]["value"]
            parts.append(f"SMA200={s200:,.2f} ({(price - s200) / s200 * 100:+.1f}%)")
        cross_fast = sma50 or sma20  # conventional 50/200 cross; fall back to 20 if sma50 absent
        if cross_fast and sma200:
            cross = "golden cross" if cross_fast[-1]["value"] > sma200[-1]["value"] else "death cross"
            parts.append(f"[{cross}]")
        lines.append(f"SMA: {' '.join(parts)}")

    # Weekly MAs
    wmas = ind.get("weeklyMAs", {})
    wp = []
    for key in ["wma50", "wma100", "wma200"]:
        arr = wmas.get(key, [])
        if arr:
            wp.append(f"{key.upper()}={arr[-1]['value']:,.2f}")
    if wp:
        lines.append(f"Weekly MAs: {' | '.join(wp)}")

    # Bull Market Band
    bmb = ind.get("bullMarketBand", {})
    ema21w = bmb.get("ema21w", [])
    sma20w = bmb.get("sma20w", [])
    if ema21w and sma20w:
        e21, s20 = ema21w[-1]["value"], sma20w[-1]["value"]
        bp = "ABOVE" if price > max(e21, s20) else ("BELOW" if price < min(e21, s20) else "INSIDE")
        lines.append(f"Bull Band: EMA21W={e21:,.2f} SMA20W={s20:,.2f} | Price {bp}")

    # Volume — last CLOSED candle vs the 20-bar median; omit entirely when the
    # feed is unreliable (junk/forward-fill commodity volume → bogus ratios).
    if volume:
        from morning_engine.candle_utils import volume_ratio_closed, tf_to_seconds
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        vr = volume_ratio_closed(volume, timeframe_seconds=tf_to_seconds(tf), now_ms=now_ms, period=20)
        if vr.get("reliable") and vr["avg"]:
            lv, avg = vr["current_volume"], vr["avg"]
            vp = (lv - avg) / avg * 100
            lines.append(f"Volume: {lv:,.0f} (vs 20-bar median {avg:,.0f} = {vp:+.1f}%)")

    # Recent 5 bars
    if len(ohlcv) >= 6:
        chgs = []
        for i in range(-6, -1):
            c = ohlcv[i + 1]
            ref = ohlcv[i]
            pct = (c["close"] - ref["close"]) / ref["close"] * 100 if ref["close"] else 0
            chgs.append(f"{pct:+.1f}%")
        lines.append(f"Recent 5 bars: [{', '.join(chgs)}]")

    # 20-bar range
    r20 = ohlcv[-20:] if len(ohlcv) >= 20 else ohlcv
    h20 = max(c["high"] for c in r20)
    l20 = min(c["low"] for c in r20)
    rpos = (price - l20) / (h20 - l20) * 100 if h20 != l20 else 50
    lines.append(f"{asset} {tf} 20-bar range: {l20:,.2f} — {h20:,.2f} (position: {rpos:.0f}%)")

    # Extended indicators (high-value subset, only notable signals)
    ext = data.get("indicators_ext", {})
    if ext:
        parts = []
        ttm = ext.get("ttm_squeeze")
        if ttm and ttm.get("active"):
            parts.append(f"TTM_SQZ active ({ttm['bars']} bars, mom {ttm['momentum']})")
        psar = ext.get("psar")
        if psar:
            flip_str = " FLIPPED" if psar.get("flip") else ""
            parts.append(f"PSAR {psar['direction']} at {psar['value']:,.2f}{flip_str}")
        st = ext.get("supertrend")
        if st:
            flip_str = " FLIPPED" if st.get("flip") else ""
            parts.append(f"SuperTrend {st['direction']} at {st['value']:,.2f}{flip_str}")
        ichi = ext.get("ichimoku")
        if ichi:
            parts.append(f"Ichimoku {ichi['cloud_position']}")
            if ichi.get("tk_cross") != "none":
                parts.append(f"TK cross {ichi['tk_cross']}")
        boll = ext.get("boll")
        if boll and boll.get("position") != "inside":
            parts.append(f"BB {boll['position']} (%B={boll['pct_b']})")
        elif boll and boll.get("bandwidth_pct", 100) < 5:
            parts.append(f"BB squeeze (BW={boll['bandwidth_pct']:.1f}%)")
        stoch = ext.get("stoch")
        if stoch and stoch.get("zone") != "neutral":
            parts.append(f"Stoch {stoch['zone']} (K={stoch['k']})")
        stoch_rsi = ext.get("stoch_rsi")
        if stoch_rsi and stoch_rsi.get("zone") != "neutral":
            parts.append(f"StochRSI {stoch_rsi['zone']} (K={stoch_rsi['k']})")
        obv = ext.get("obv")
        if obv and obv.get("divergence") != "none":
            parts.append(f"OBV {obv['divergence']} divergence")
        afib = ext.get("auto_fib")
        if afib:
            parts.append(f"AutoFib {afib['trend']} ({afib['price_position']})")
        if parts:
            lines.append(f"Ext: {' | '.join(parts)}")

    lines.append("")
    return "\n".join(lines)


def fetch_chart_data(chart_pairs=None):
    """Fetch and format chart data for all asset/TF pairs in parallel.

    Returns (formatted_text, raw_data_dict).
    raw_data_dict: {(asset, tf): api_response_dict} for signal extraction.
    """
    pairs = chart_pairs or CHART_PAIRS
    asset_tfs = {}
    for pair in pairs:
        asset, tf = pair.split("/")
        asset_tfs.setdefault(asset, []).append(tf)

    results = {}
    t0 = time.monotonic()

    def _fetch_one(asset, tf):
        data = _api(f"/api/chart-internal/{asset}?timeframe={tf}")
        return asset, tf, data

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = []
        for asset, tfs in asset_tfs.items():
            for tf in tfs:
                futs.append(pool.submit(_fetch_one, asset, tf))
        for fut in as_completed(futs):
            asset, tf, data = fut.result()
            results[(asset, tf)] = data

    elapsed = time.monotonic() - t0

    # Format output
    lines = [f"# Chart Data — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    for asset, tfs in asset_tfs.items():
        lines.append(f"## {asset}")
        lines.append("")
        for tf in tfs:
            data = results.get((asset, tf), {"error": "not fetched"})
            if "error" in data and "ohlcv" not in data:
                lines.append(f"### {asset} — {tf}\nFETCH FAILED: {data['error']}\n")
            else:
                lines.append(_format_asset_tf(data, asset, tf))

    log.info("Chart data: %d pairs in %.1fs", len(pairs), elapsed)
    return "\n".join(lines), results


# --------------------------------------------------------------------------
# Context data — single preassembly endpoint
# --------------------------------------------------------------------------

def _registry_rs_only(rs_data):
    """Drop non-registry symbols from an RS-rankings payload.

    ``rs_data`` is ``{'assets': [{'symbol': ..., ...}, ...]}`` from either
    ``/api/engine/morning-context``'s ``_fetch_rs`` or
    ``/api/relative-strength``; both use that shape. Anything unexpected is
    returned untouched rather than silently emptied — a shape change should
    show up as an unfiltered ranking someone notices, not as a vanished
    section nobody can explain.

    Same rule as carrac/reports/pulse.py::_pick_biggest_mover, which is where
    it was already enforced for the Pulse cockpit. If the import fails the
    payload is passed through unchanged: losing the whole Relative Strength
    section is a worse failure than an over-inclusive one.
    """
    if not isinstance(rs_data, dict) or not isinstance(rs_data.get("assets"), list):
        return rs_data
    # One rule, one place: carrac/facts/bands.py::build_rotation shares this
    # allowlist, since a watchlist symbol would otherwise ride the Rotation
    # strip onto the public page. public_symbol_allowlist() returns None (never an empty
    # set) when the registry is unreadable, so both callers pass through
    # instead of blanking their section.
    from carrac.assets.registry import public_symbol_allowlist
    allowed = public_symbol_allowlist()
    if allowed is None:
        return rs_data
    kept = [r for r in rs_data["assets"]
            if str((r or {}).get("symbol", "")).upper() in allowed]
    dropped = len(rs_data["assets"]) - len(kept)
    if dropped:
        log.info("RS rankings: dropped %d non-registry symbol(s) from shared context", dropped)
    return {**rs_data, "assets": kept}


def _derivatives_ts_label(ts):
    """Render a derivatives-snapshot timestamp as 'YYYY-MM-DD HH:MM UTC'.

    Returns '' rather than a guess when the value is missing or
    unparseable — a wrong stamp on a number is worse than no stamp, because
    it invites the reader to trust a reconciliation that never happened.
    """
    if not ts:
        return ''
    try:
        from datetime import timezone as _tz
        if hasattr(ts, 'astimezone'):
            aware = ts if ts.tzinfo else ts.replace(tzinfo=_tz.utc)
            return aware.astimezone(_tz.utc).strftime('%Y-%m-%d %H:%M UTC')
        return str(ts)[:16] + ' UTC'
    except Exception as e:  # noqa: BLE001
        log.warning("derivatives ts label unavailable: %s", e)
        return ''


def _format_rs_registry_only(rs_data):
    """RS-rankings formatter that applies the registry filter first.

    The last unfixed sibling of that leak. The preassembly path
    below builds this section as
    ``_format_json_compact(_registry_rs_only(rs_data))``, but the LEGACY
    FALLBACK task map wired ``_format_json_compact`` bare — so whenever the
    preassembly endpoint failed and the fallback ran, every non-registry
    symbol a user happens to track (TSLA, META) rode the RS Rankings
    table straight into the shared public report. Exactly the leak
    was filed for, surviving on the path nobody looks at because it only
    executes when something else is already broken.

    Existing as a named formatter rather than a lambda is deliberate: the
    task map is a dict of ``(path, formatter)`` pairs, and a bare
    ``_format_json_compact`` there is indistinguishable at a glance from a
    correctly-filtered one. A named function makes the filtered case the
    thing you read.
    """
    return _format_json_compact(_registry_rs_only(rs_data))


def _format_signal_diff_line(c):
    """Render one signal-diff change as a display line for the LLM/report.

    Sibling fix: appends an explicit asset-class tag when the source
    carried one (see carrac/reports/routes.py::_tag_signal_diff_asset_class)
    so a report can never narrate a registry equity/commodity (SPX, GOLD) as
    part of a "crypto alts" cluster. Shared by fetch_context_data's inline
    render and _format_signal_diff_legacy so the two consumers of this data
    (preassembly + the /api/report/diff fallback) cannot drift apart.
    """
    tag = ""
    if "is_crypto" in c:
        tag = " (crypto)" if c.get("is_crypto") else " (non-crypto)"
    return f"  [{c.get('significance', '')}] {c.get('symbol', '')}{tag}: {c.get('change_type', '')}"


def fetch_context_data():
    """Fetch all context data via the preassembly endpoint.

    Returns dict of section_name -> formatted text, ready for prompt injection.
    """
    t0 = time.monotonic()
    data = _api("/api/engine/morning-context", timeout=90, retries=2)

    if "error" in data and "data" not in data:
        log.error("Preassembly endpoint failed: %s — falling back to individual calls", data["error"])
        return _fetch_context_data_fallback()

    availability = data.get("availability", {})
    sources = data.get("data", {})

    ok_count = sum(1 for v in availability.values() if v == "ok")
    log.info("Context preassembly: %d/%d sources OK in %.1fs",
             ok_count, len(availability), time.monotonic() - t0)

    # Format each source into text for prompt injection
    sections = {}

    # Alerts — human-readable summary (fired / approaching / active watchlist)
    alerts_data = sources.get("alerts", {})
    sections["alerts"] = _format_alerts_summary(alerts_data)

    # Positions
    positions_data = sources.get("positions", {})
    sections["positions"] = _format_json_compact(positions_data)

    # Signal diff
    diff_data = sources.get("signal_diff", {})
    changes = diff_data.get("changes", [])
    if changes:
        diff_lines = [_format_signal_diff_line(c) for c in changes]
        sections["signal_diff"] = "\n".join(diff_lines)
    else:
        sections["signal_diff"] = "No significant signal changes."

    # Relative strength — registry chart assets ONLY.
    #
    # /api/engine/morning-context serves every report type from one call, and
    # its _fetch_rs returns _compute_rs_rankings() whole. That ranking is
    # computed over the RS BENCHMARK set, which includes watchlist-only
    # symbols. Left unfiltered, watchlist symbols would reach the free public
    # /brief in the RS Rankings table.
    #
    # The sibling path already enforces the rule —
    # carrac/reports/pulse.py::_pick_biggest_mover, whose docstring says these
    # symbols "must never headline the shared Pulse cockpit". Applied here
    # because this is the shared boundary: build_shared_briefing_parts()
    # documents itself as data "common to all reports", with user-specific
    # context added afterwards by build_user_briefing(). A watchlist symbol in
    # the shared half is a contract violation regardless of who consumes it.
    rs_data = sources.get("rs_rankings", {})
    sections["rs_rankings"] = _format_json_compact(_registry_rs_only(rs_data))

    # Sentiment
    sentiment_data = sources.get("sentiment", {})
    sections["sentiment"] = _format_json_compact(sentiment_data)

    # Sentiment history
    sh_data = sources.get("sentiment_history", {})
    sections["sentiment_history"] = _format_json_compact(sh_data)

    # History context
    hc_data = sources.get("history_context", {})
    sections["history_context"] = _format_json_compact(hc_data)

    # Yesterday's report
    yesterday = sources.get("yesterday_report", {})
    sections["yesterday_report"] = _format_yesterday(yesterday)

    # Pass availability flags for prompt injection
    sections["_availability"] = availability

    return sections


def fetch_quant_context():
    """Fetch Quant Engine data for the morning report.

    Returns dict with 'quant' key containing formatted text block.
    Never throws — returns fallback text on failure.
    """
    try:
        snap = _api('/api/quant/snapshot', timeout=15, retries=2)
        if 'error' in snap and 'regime' not in snap:
            log.warning("Quant snapshot fetch failed: %s", snap.get('error'))
            return {'quant': 'Quant Engine data unavailable.'}
        snap = _strip_deribit_for_publication(snap)
    except Exception as e:
        log.warning("Quant snapshot exception: %s", e)
        return {'quant': 'Quant Engine data unavailable.'}

    lines = []

    # Regime — with cycle positioning + persistence (stay_prob/converged)
    regime = snap.get('regime')
    if regime:
        conf = round(regime.get('probability', 0) * 100)
        desc = ''
        descs = regime.get('descriptions', {})
        if descs and regime.get('state') in descs:
            desc = f" — {descs[regime['state']]}"
        state = (regime.get('state') or 'unknown').lower()
        dur = regime.get('duration_days', 0) or 0
        # This is 1/(1-p_stay) from the HMM transition matrix — the
        # MEAN of a geometric distribution, not a median, and not historical
        # (it's implied by the CURRENT transition odds, which shift day to
        # day). carrac/facts/bands.py::_compute_regime_clock computes a
        # genuinely historical, genuinely median run length from completed
        # regime runs and renders it in the SAME report's regime banner
        # ("day X of a typical ~Y-day run") — both saying "historical
        # median" would label two different numbers on the same page. Renamed here
        # only (the dict key regime_hist_median_days has other consumers —
        # see quant/api/routes.py — so it is left alone).
        hist_median = (snap.get('regime_hist_median_days') or {}).get(state)
        phase_str = ''
        if hist_median and dur:
            frac = dur / hist_median
            if frac < 0.3:
                phase_str = (f" — early in {state} regime "
                             f"({dur}d elapsed vs ~{hist_median}d expected duration, from current transition odds)")
            elif frac < 0.7:
                phase_str = (f" — mid-cycle {state} regime "
                             f"({dur}d of ~{hist_median}d expected duration, from current transition odds)")
            elif frac < 1.0:
                phase_str = (f" — mature {state} regime "
                             f"({dur}d of ~{hist_median}d expected duration, from current transition odds)")
            else:
                phase_str = (f" — late-stage {state} regime "
                             f"({dur}d vs ~{hist_median}d expected duration, from current transition odds, overdue)")
        dur_tail = f"{dur} days since {regime.get('since', '?')}"
        lines.append(f"Regime: {state.upper()} ({conf}% confidence, {dur_tail}){desc}{phase_str}")
        # Regime persistence from HMM transition matrix diagonal
        stay_prob = regime.get('stay_prob')
        converged = regime.get('converged')
        if stay_prob is not None:
            persist_pct = round(stay_prob * 100)
            conv_str = f", model converged: {'yes' if converged else 'no'}" if converged is not None else ""
            lines.append(
                f"Regime persistence: {persist_pct}% daily probability of remaining "
                f"{state.upper()}{conv_str}"
            )

    # Anomalies. This fed the LLM metric/zscore/percentile only \u2014
    # no date \u2014 so a 21-23-day-stale JAPAN_10Y was written up as "active" in
    # every afternoon/general edition covering it. Every anomaly now carries
    # its own date, with an explicit "Nd old" callout past
    # anomaly_filter.DISPLAY_STALE_DAYS \u2014 the LLM cannot present stale
    # material as current when the staleness is right there in the fact it
    # was given. Same helper as report_blocks.build_anomaly_pulse_block (the
    # sibling consumer), so the threshold cannot drift between the two.
    anomalies = snap.get('anomalies', [])
    if anomalies:
        from quant.anomaly_filter import format_anomaly_date_suffix
        top8 = anomalies[:8]
        anom_strs = []
        for a in top8:
            name = a['metric']
            if a.get('asset', '_GLOBAL_') != '_GLOBAL_':
                name += f":{a['asset']}"
            pct = a.get('percentile')
            pct_str = f", p{pct:.0f}" if pct is not None else ""
            date_suffix = format_anomaly_date_suffix(a)
            anom_strs.append(f"{name} ({a['zscore']:+.1f}\u03C3{pct_str}){date_suffix}")
        lines.append(f"Anomalies ({snap.get('anomaly_count', len(anomalies))} extreme): {', '.join(anom_strs)}")
        macro_hits = [a for a in anomalies[:8] if a.get('domain') in ('macro', 'sentiment')]
        if macro_hits:
            names = ', '.join(a['metric'] for a in macro_hits[:4])
            lines.append(f"(Macro/sentiment anomalies worth weaving into the dollar/rates narrative: {names})")

    # Top correlations — filter to FDR-significant for prose rigor
    corrs = snap.get('top_correlations', [])
    if corrs:
        # Prefer significant=true; if none, fall back to all (so report doesn't go blank)
        sig_corrs = [c for c in corrs if c.get('significant') is True]
        chosen = sig_corrs if sig_corrs else corrs
        corr_strs = []
        for c in chosen[:5]:
            stab = c.get('stability')
            stab_str = f", \u0394={stab:.2f}" if stab is not None else ""
            q = c.get('q_value')
            q_str = f", q={q:.3f}" if q is not None else ""
            corr_strs.append(f"{c['metric']} {c['correlation']:+.2f}{stab_str}{q_str}")
        label = "significant only, q<0.05" if sig_corrs else "all, incl. non-significant"
        lines.append(f"Top BTC correlations (60d Spearman, \u0394=stability, {label}): "
                     f"{', '.join(corr_strs)}")

    # Lead-lag (daily) — with leader z, projected move, association band
    # p_corrected annotation: signals with p_corrected > 0.1 are labelled weak
    # (multiple-comparisons-corrected permutation p-value from lag scan).
    ll_signals = snap.get('leadlag_signals', [])
    if ll_signals:
        top3 = ll_signals[:3]
        ll_strs = []
        for ll in top3:
            lag = ll['lag_days']
            if lag < 0:
                direction = f"{ll['leader']} leads BTC by {abs(lag)}d"
            elif lag > 0:
                direction = f"BTC leads {ll['leader']} by {lag}d"
            else:
                direction = f"{ll['leader']} moves with BTC (0d lag)"
            parts = [f"r={ll['correlation']:+.3f}"]
            lz = ll.get('leader_z')
            proj = ll.get('projected_move_z')
            if lz is not None and proj is not None:
                parts.append(f"leader {lz:+.2f}σ → BTC projected {proj:+.2f}σ")
            ci = ll.get('projected_ci_z')
            if ci and len(ci) == 2:
                parts.append(f"assoc. band [{ci[0]:+.2f}σ, {ci[1]:+.2f}σ]")
            stable = ll.get('persistent')
            if stable is not None:
                parts.append("stable" if stable else "unstable")
            # Integrity annotation: flag signals whose lag-scan p-value is weak
            pc = ll.get('p_corrected')
            nl = ll.get('n_lags_tested')
            if pc is not None and nl is not None:
                parts.append(f"p_scan={pc:.3f}/{nl}lags")
                if pc > 0.1:
                    parts.append("(weak after lag-scan correction)")
            ll_strs.append(f"{direction} ({', '.join(parts)})")
        lines.append(f"Lead-lag (daily): {'; '.join(ll_strs)}")
        lines.append(
            "(Lead-lag: projected move = r × leader_z, a linear association estimate. "
            "Band = 95% CI of the correlation, not a forecast interval. "
            "Best lag selected by exploratory scan — treat as hypothesis; "
            "stable = signal held across sub-windows.)"
        )

    # Lead-lag (weekly — smoother macro signals)
    ll_weekly = snap.get('leadlag_signals_weekly', [])
    if ll_weekly:
        top3 = ll_weekly[:3]
        wll_strs = []
        for ll in top3:
            lag = ll['lag_days']
            if lag < 0:
                direction = f"{ll['leader']} leads BTC by {abs(lag)}d"
            elif lag > 0:
                direction = f"BTC leads {ll['leader']} by {lag}d"
            else:
                direction = f"{ll['leader']} moves with BTC"
            wll_strs.append(f"{direction} (r={ll['correlation']:+.3f})")
        lines.append(f"Lead-lag (weekly): {'; '.join(wll_strs)}")

    # Transmission chains — enriched per-link detail
    chains = snap.get('chains', [])
    if chains:
        lines.append("")
        lines.append("=== Transmission Chains ===")
        for ch in chains:
            chain_name = ch.get('chain', '')
            strength = ch.get('chain_strength', 0)
            if strength <= 0:
                continue
            try:
                cd = _api(f'/api/quant/chain?chain={chain_name}', timeout=5, retries=1)
                if 'error' not in cd and cd.get('links'):
                    link_strs = []
                    for link in cd['links']:
                        r = link.get('correlation')
                        if r is None:
                            continue
                        persist = "persistent" if link.get('persistent') else "NOT persistent"
                        lag = link.get('best_lag', 0)
                        lag_str = f", {lag}d lag" if lag else ""
                        link_strs.append(
                            f"{link['from']}→{link['to']} r={r:+.3f} ({persist}{lag_str})")
                    wk = cd.get('weakest_link') or {}
                    lines.append(
                        f"{chain_name}: {' | '.join(link_strs)} | "
                        f"Overall: {_fmt_num(strength, '.3f')} | "
                        f"Weakest: {wk.get('from', '?')}→{wk.get('to', '?')} "
                        f"r={_fmt_num(wk.get('r'), '+.4f')}")
                else:
                    wk = ch.get('weakest_link') or {}
                    lines.append(
                        f"Chain [{chain_name}] strength={_fmt_num(strength, '.3f')}: "
                        f"weakest {wk.get('from', '?')}→{wk.get('to', '?')} "
                        f"r={_fmt_num(wk.get('r'), '.4f')}")
            except Exception as e:
                # Name the values — a bare exception text hides a
                # None correlation while every chain loses its detail.
                log.warning(
                    "Chain detail fetch failed for %s (strength=%r weakest=%r): %s",
                    chain_name, strength, ch.get('weakest_link'), e)
                wk = ch.get('weakest_link') or {}
                lines.append(
                    f"Chain [{chain_name}] strength={_fmt_num(strength, '.3f')}: "
                    f"weakest {wk.get('from', '?')}→{wk.get('to', '?')} "
                    f"r={_fmt_num(wk.get('r'), '.4f')}")

    # Chain trend delta — 5-day strength trend for narrative
    if chains:
        chain_trends = []
        for ch in chains:
            chain_name = ch.get('chain', '')
            if not chain_name:
                continue
            try:
                hist = _api(f'/api/quant/chain-history?chain={chain_name}&days=5',
                            timeout=5, retries=1)
                if hist and len(hist) >= 2:
                    today_strength = hist[0]['strength']
                    avg_5d = sum(h['strength'] for h in hist) / len(hist)
                    delta = today_strength - avg_5d
                    if delta > 0.01:
                        direction = "strengthening"
                    elif delta < -0.01:
                        direction = "weakening"
                    else:
                        direction = "stable"
                    chain_trends.append(
                        f"{chain_name}: {direction} ({delta:+.3f} vs 5d avg, "
                        f"today={today_strength:.3f}, avg={avg_5d:.3f})")
            except Exception:
                pass
        if chain_trends:
            lines.append("")
            lines.append("=== Chain Trends (5-day delta) ===")
            for t in chain_trends:
                lines.append(t)

    # Anomaly cluster
    cluster = snap.get('anomaly_cluster')
    if cluster and cluster.get('extreme_count', 0) >= 3:
        metrics = [f"{m.get('metric', m)} ({m.get('zscore', 0):+.1f}\u03C3)"
                   for m in (cluster.get('metrics', []))[:5]]
        lines.append(f"Anomaly cluster: {cluster['extreme_count']} extreme across "
                     f"{cluster.get('domains_affected', '?')} domains: {', '.join(metrics)}")

    # Per-asset quant context — ETH and SOL
    # BTC is already covered above. For the other two focus assets, fetch
    # compact snapshots so the briefing can reason about alt-specific
    # correlations / lead-lag rather than treating everything as BTC.
    per_asset_lines = []
    for alt in ('ETH', 'SOL'):
        try:
            alt_snap = _api(f'/api/quant/snapshot?asset={alt}', timeout=10, retries=1)
        except Exception:
            continue
        if not alt_snap or 'error' in alt_snap:
            continue
        # Top-3 significant correlations
        alt_corrs = alt_snap.get('top_correlations', []) or []
        sig = [c for c in alt_corrs if c.get('significant') is True]
        chosen = sig if sig else alt_corrs
        if chosen:
            parts = []
            for c in chosen[:3]:
                q = c.get('q_value')
                q_str = f" q={q:.3f}" if q is not None else ''
                parts.append(f"{c['metric']} {c['correlation']:+.2f}{q_str}")
            per_asset_lines.append(f"{alt} correlations (60d): " + ', '.join(parts))
        # Top-3 lead-lag with projected move
        alt_ll = alt_snap.get('leadlag_signals', []) or []
        if alt_ll:
            parts = []
            for ll in alt_ll[:3]:
                lag = ll['lag_days']
                if lag < 0:
                    direction = f"{ll['leader']} leads {alt} by {abs(lag)}d"
                elif lag > 0:
                    direction = f"{alt} leads {ll['leader']} by {lag}d"
                else:
                    direction = f"{ll['leader']} ~ {alt}"
                proj = ll.get('projected_move_z')
                proj_str = f", projected {proj:+.2f}σ" if proj is not None else ''
                parts.append(f"{direction} (r={ll['correlation']:+.2f}{proj_str})")
            per_asset_lines.append(f"{alt} lead-lag: " + '; '.join(parts))
    if per_asset_lines:
        lines.append("")
        lines.append("=== Per-Asset Quant Context (alts) ===")
        lines.extend(per_asset_lines)

    # Conditional queries — 2 hardcoded + up to 3 dynamic from anomalies
    cond_queries = [
        ('Risk-off', 'conditions=VIX:gt:2,HY_SPREAD:gt:1.5&target=BTC&horizon=30'),
        ('Euphoria', 'conditions=FEAR_GREED:gt:2,FUNDING_BTC:gt:1.5&target=BTC&horizon=30'),
    ]

    # Dynamic queries from today's top anomalies
    hardcoded_metrics = {'VIX', 'HY_SPREAD', 'FEAR_GREED', 'FUNDING_BTC'}
    sorted_anoms = sorted(anomalies, key=lambda a: abs(a.get('zscore', 0)), reverse=True)
    for a in sorted_anoms:
        if len(cond_queries) >= 5:
            break
        metric = a.get('metric', '')
        if metric in hardcoded_metrics:
            continue
        z = a.get('zscore', 0)
        if abs(z) < 3.0:
            continue
        op = 'gt' if z > 0 else 'lt'
        threshold = round(abs(z) - 0.5, 1)
        label = f"{metric} extreme ({z:+.1f}\u03C3)"
        params = f"conditions={metric}:{op}:{threshold}&target=BTC&horizon=30"
        cond_queries.append((label, params))

    # Run all conditional queries in parallel
    def _fetch_conditional(label_params):
        label, params = label_params
        try:
            return label, _api(f'/api/quant/conditional?{params}', timeout=10, retries=1)
        except Exception:
            return label, None

    with ThreadPoolExecutor(max_workers=5) as pool:
        cond_results = list(pool.map(_fetch_conditional, cond_queries))

    for label, cond in cond_results:
        if not cond or 'error' in cond or cond.get('instances', 0) <= 5:
            continue
        fr = cond.get('forward_return', {})
        bl = cond.get('unconditional_baseline', {})
        med = fr.get('median') if fr else None
        bl_med = bl.get('median') if bl else None
        active = " [ACTIVE NOW]" if cond.get('current_match') else ""
        if med is not None and bl_med is not None:
            edge = med - bl_med
            hit = fr.get('hit_rate')
            p25 = fr.get('p25')
            p75 = fr.get('p75')
            parts = [f"{label} scenario ({cond['instances']} instances){active}:"]
            parts.append(f"median {med * 100:+.1f}% vs baseline {bl_med * 100:+.1f}% (edge: {edge * 100:+.1f}%)")
            if hit is not None:
                parts.append(f"hit rate: {hit * 100:.0f}%")
            if p25 is not None and p75 is not None:
                parts.append(f"IQR: [{p25 * 100:+.1f}%, {p75 * 100:+.1f}%]")
            lines.append(' | '.join(parts))
        elif med is not None:
            lines.append(f"{label} scenario ({cond['instances']} instances){active}: "
                         f"BTC 30d median = {med * 100:+.1f}%")

    # -----------------------------------------------------------------------
    # [for What Would Change the Picture section] Multi-asset base rates +
    # nearest stacked S/R zones (observed). Extends the BTC-only base rates
    # above to SPX and GOLD (macro targets), and adds the nearest confluence
    # zone for each key asset as an OBSERVED level cluster (these
    # feed observational bullets, not prescriptive trigger levels).
    # All base-rate lines are gated on passes_min_n (N>=25).
    # -----------------------------------------------------------------------
    scenario_lines = []

    # Multi-asset macro conditionals: BTC (crypto conditions), SPX + GOLD (macro)
    multi_asset_queries = [
        # BTC with crypto-specific conditions (funding + fear&greed z-scores)
        ('BTC', 'bull', 'conditions=FEAR_GREED:gt:1,FUNDING_BTC:gt:1&target=BTC&horizon=30'),
        ('BTC', 'bear', 'conditions=VIX:gt:2,FEAR_GREED:lt:-1&target=BTC&horizon=30'),
        # SPX with macro conditions (VIX stress + HY spread widening)
        ('SPX', 'risk-off', 'conditions=VIX:gt:2,HY_SPREAD:gt:1.5&target=SPX&horizon=30'),
        ('SPX', 'risk-on', 'conditions=VIX:lt:-1,HY_SPREAD:lt:-1&target=SPX&horizon=30'),
        # GOLD with macro conditions (DXY strength + risk-off VIX)
        ('GOLD', 'risk-off', 'conditions=VIX:gt:2,DXY:gt:1&target=GOLD&horizon=30'),
        ('GOLD', 'dollar-weak', 'conditions=DXY:lt:-1.5,HY_SPREAD:gt:1&target=GOLD&horizon=30'),
    ]

    def _fetch_multi_cond(args):
        asset, direction, params = args
        try:
            return asset, direction, _api(f'/api/quant/conditional?{params}', timeout=10, retries=1)
        except Exception:
            return asset, direction, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        multi_results = list(pool.map(_fetch_multi_cond, multi_asset_queries))

    for asset, direction, cond in multi_results:
        if not passes_min_n(cond):
            continue
        fr = cond.get('forward_return', {})
        bl = cond.get('unconditional_baseline', {})
        med = fr.get('median') if fr else None
        bl_med = bl.get('median') if bl else None
        hit = fr.get('hit_rate')
        n = cond.get('instances', 0)
        active_tag = " [ACTIVE NOW]" if cond.get('current_match') else ""
        if med is not None and bl_med is not None:
            edge = med - bl_med
            meaning = (
                f"i.e. historically {asset} moved ~{med * 100:+.1f}% at 30d vs "
                f"baseline {bl_med * 100:+.1f}% (edge: {edge * 100:+.1f}%)"
            )
            hit_str = f", hit-rate {hit * 100:.0f}%" if hit is not None else ""
            scenario_lines.append(
                f"{asset} ({direction}) | in {n} similar instances: "
                f"median {med * 100:+.1f}%{hit_str}{active_tag} — {meaning}"
            )
        elif med is not None:
            scenario_lines.append(
                f"{asset} ({direction}) | in {n} similar instances: "
                f"30d median {med * 100:+.1f}%{active_tag}"
            )

    # Confluence zones: nearest support/resistance for BTC, SPX, GOLD
    # as nearest stacked S/R zones (observed) for the observational bullets
    _confluence_assets = ['BTC', 'SPX', 'GOLD']
    confluence_zone_lines = []
    # Keep every level behind these zones, not just the two the
    # briefing renders. The report cites CPR pivots, bands and swing points for
    # these assets, and without the computed set those claims have no retained
    # source — which is why an earlier GEX claim was undiagnosable.
    confluence_levels = {}
    for sym in _confluence_assets:
        try:
            cf_data = _api(f'/api/confluence?symbol={sym}', timeout=8, retries=1)
            if not cf_data or 'error' in cf_data:
                log.warning("scenario confluence fetch failed for %s", sym)
                continue
            price = cf_data.get('price') or 0
            if not price:
                continue
            from morning_engine.confluence_block import _all_grounded_levels
            confluence_levels[sym] = _all_grounded_levels(cf_data) + [float(price)]
            # Nearest support (highest support below price) and resistance (lowest above)
            supports = cf_data.get('supports') or []
            resistances = cf_data.get('resistances') or []
            below = [s for s in supports if s.get('price', 0) and s['price'] < price]
            nearest_sup = max(below, key=lambda s: s['price']) if below else None
            above = [r for r in resistances if r.get('price', 0) and r['price'] > price]
            nearest_res = min(above, key=lambda r: r['price']) if above else None
            zone_parts = []
            if nearest_sup:
                dist_pct = abs(price - nearest_sup['price']) / price * 100
                zone_parts.append(
                    f"support {nearest_sup['price']:,.2f} ({nearest_sup.get('label_friendly') or nearest_sup.get('label','')},"
                    f" tier={nearest_sup.get('tier','?')}, {dist_pct:.1f}% below)"
                )
            if nearest_res:
                dist_pct = abs(nearest_res['price'] - price) / price * 100
                zone_parts.append(
                    f"resistance {nearest_res['price']:,.2f} ({nearest_res.get('label_friendly') or nearest_res.get('label','')},"
                    f" tier={nearest_res.get('tier','?')}, {dist_pct:.1f}% above)"
                )
            if zone_parts:
                confluence_zone_lines.append(
                    f"{sym} nearest confluence zones (price={price:,.2f}): {'; '.join(zone_parts)}"
                )
        except Exception:
            pass

    # Week's key event as a calendar note (highest-impact event this week) —
    # names what it touches (vol/attention/data), not a directional catalyst.
    key_event_str = None
    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        week_out_str = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
        events_data = _api(f'/api/quant/events?from={today_str}&to={week_out_str}',
                           timeout=8, retries=1)
        if isinstance(events_data, list) and events_data:
            # Prefer high-impact events
            impact_rank = {'high': 0, 'medium': 1, 'low': 2}
            sorted_evts = sorted(events_data,
                                 key=lambda e: impact_rank.get(e.get('impact'), 3))
            key_evt = sorted_evts[0]
            expected_str = f", expected={key_evt.get('expected')}" if key_evt.get('expected') else ""
            key_event_str = (
                f"{key_evt.get('title','?')} ({key_evt.get('ts','?')}, impact={key_evt.get('impact','?')}{expected_str})"
            )
    except Exception:
        pass

    # Emit the compact [for What Would Change the Picture section] block
    if scenario_lines or confluence_zone_lines or key_event_str:
        lines.append("")
        lines.append("[for What Would Change the Picture section]")
        if scenario_lines:
            lines.append("Multi-asset base rates (min-N≥25 gate — phrase as 'in N similar instances'):")
            lines.extend(scenario_lines)
        if confluence_zone_lines:
            lines.append("Nearest stacked S/R zones (observed):")
            lines.extend(confluence_zone_lines)
        if key_event_str:
            lines.append(f"Week's key event (calendar note — vol/attention/data picture): {key_event_str}")
        lines.append(
            "Instruction: use these as observed level clusters + analogue base rates "
            "for the ## What Would Change the Picture section — observations only, no "
            "triggers/conditionals, no directional lean. Never phrase as a probability."
        )

    # Historical analogues
    try:
        analogues = _api('/api/quant/analogues?target=BTC&top=3', timeout=10, retries=1)
        if isinstance(analogues, dict) and 'analogues' in analogues:
            analogue_list = analogues['analogues']
        elif isinstance(analogues, list):
            analogue_list = analogues
        else:
            analogue_list = []
        if analogue_list:
            a_strs = []
            for a in analogue_list[:3]:
                sim = a.get('similarity', 0)
                fr = a.get('forward_return', {})
                r7 = fr.get('7d')
                r30 = fr.get('30d')
                r90 = fr.get('90d')
                rets = []
                if r7 is not None:
                    rets.append(f"7d:{r7 * 100:+.1f}%")
                if r30 is not None:
                    rets.append(f"30d:{r30 * 100:+.1f}%")
                if r90 is not None:
                    rets.append(f"90d:{r90 * 100:+.1f}%")
                a_strs.append(f"{a['date']} (sim={sim:.2f}): {', '.join(rets)}")
            lines.append(f"Historical analogues (cosine similarity on z-score vector): {'; '.join(a_strs)}")
    except Exception:
        pass

    # MOVE bond-volatility line (daily)
    # bond_vol: {level, zscore, percentile, 5d_change, label}
    # label='unknown' means nightly normalize hasn't run yet — emit level + 5d_change only.
    # Missing level → skip entirely.
    bond_vol = snap.get('bond_vol')
    if bond_vol and bond_vol.get('level') is not None:
        bv_level = bond_vol['level']
        bv_label = bond_vol.get('label', 'unknown')
        bv_5d = bond_vol.get('5d_change')
        bv_pct = bond_vol.get('percentile')
        bv_z = bond_vol.get('zscore')
        bv_5d_str = f", 5d change {bv_5d:+.1f}" if bv_5d is not None else ""
        if bv_label == 'unknown':
            # No z-score yet — give level and 5d change only
            lines.append(
                f"Bond volatility (MOVE): {bv_level:.1f}{bv_5d_str} "
                f"(z-score/percentile not yet available)"
            )
        else:
            pct_str = f", {bv_pct:.0f}th percentile" if bv_pct is not None else ""
            z_str = f" (z={bv_z:+.2f})" if bv_z is not None else ""
            lines.append(
                f"Bond volatility (MOVE): {bv_level:.1f}{z_str}{pct_str} "
                f"— {bv_label}{bv_5d_str}"
            )

    # Yesterday's macro events with FRED-sourced actuals
    try:
        yesterday_date = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_events = _api(f'/api/quant/events?from={yesterday_date}&to={yesterday_date}',
                                timeout=10, retries=1)
        if isinstance(yesterday_events, list):
            with_actuals = [e for e in yesterday_events if e.get('actual') is not None]
            if with_actuals:
                y_strs = []
                for e in with_actuals[:8]:
                    impact_tag = f" [{e.get('impact', '').upper()}]" if e.get('impact') else ''
                    expected_str = f" expected={e.get('expected') or '—'}" if e.get('expected') else ''
                    y_strs.append(f"{e['title']}{impact_tag}:{expected_str} actual={e['actual']}")
                lines.append(f"Yesterday's releases (FRED actuals): {'; '.join(y_strs)}")
    except Exception:
        pass

    # Upcoming macro events (next 7 days)
    try:
        from morning_engine.calendar_block import render_calendar_table, CALENDAR_WINDOW_DAYS
        today = datetime.now().strftime('%Y-%m-%d')
        week_out = (datetime.now() + timedelta(days=CALENDAR_WINDOW_DAYS)).strftime('%Y-%m-%d')
        events = _api(f'/api/quant/events?from={today}&to={week_out}', timeout=10, retries=1)
        if isinstance(events, list) and events:
            # Render the grounded events as an authoritative table the report must
            # reproduce verbatim — NOT a prose summary it can "complete" from memory.
            lines.append(render_calendar_table(events[:8], window_days=CALENDAR_WINDOW_DAYS))
        elif isinstance(events, list):
            lines.append(f"_No major scheduled events in the next {CALENDAR_WINDOW_DAYS} days._")
    except Exception:
        pass

    text = '\n'.join(lines) if lines else 'Quant Engine: no data available.'
    # 'confluence_levels' is additive — callers that only read
    # 'quant' are unaffected. It carries retained evidence, not prompt text.
    return {'quant': text, 'confluence_levels': confluence_levels}


def fetch_market_breadth():
    """Fetch /api/market/breadth and format as a compact one-liner for prompt injection.

    Returns a non-empty string on success, empty string on failure (always tolerant).

    Example output:
      "Market breadth: 84/214 oversold (39%), 3 overbought, 33% above 50-day,
       61 new 20-day lows — label: washed-out."
    """
    try:
        data = _api('/api/market/breadth', timeout=10, retries=1)
        if not isinstance(data, dict) or 'label' not in data:
            log.debug("fetch_market_breadth: unexpected response shape")
            return ''
        label = data.get('label', 'unknown')
        universe = data.get('universe_size', 0)

        oversold = data.get('oversold', {})
        ov_count = oversold.get('count', 0)
        ov_pct = oversold.get('pct', 0.0)

        overbought = data.get('overbought', {})
        ob_count = overbought.get('count', 0)

        above_sma50 = data.get('above_sma50', {})
        above_pct = above_sma50.get('pct', 0.0)

        new_lows = data.get('new_20d_lows', {})
        lows_count = new_lows.get('count', 0)

        new_highs = data.get('new_20d_highs', {})
        highs_count = new_highs.get('count', 0)

        parts = [
            f"{ov_count}/{universe} oversold ({ov_pct:.0f}%)",
            f"{ob_count} overbought",
            f"{above_pct:.0f}% above 50-day",
        ]
        if lows_count > 0:
            parts.append(f"{lows_count} new 20-day lows")
        if highs_count > 0:
            parts.append(f"{highs_count} new 20-day highs")
        return f"Market breadth: {', '.join(parts)} — label: {label}."
    except Exception as e:
        log.debug("fetch_market_breadth failed: %s", e)
        return ''


def fetch_cot():
    """Fetch /api/quant/cot and format as a compact block for prompt injection.

    Returns a non-empty string (capped ~1,200 chars) on success.
    Returns empty string on failure (always tolerant).

    Block format (stretched series first, then up to 5 more by |z|):
      Smart-money positioning (CFTC COT, week of YYYY-MM-DD):
      BTC large-spec long% 84% — 100th pct, z=+2.4, +6.6pp WoW — STRETCHED
      GOLD spec net +2.4σ, 89th pct, +1200 WoW
      ...
      (stretched_count: 2 of 18 series)
    """
    try:
        data = _api('/api/quant/cot', timeout=12, retries=1)
        if not isinstance(data, dict) or 'series' not in data:
            log.debug("fetch_cot: unexpected response shape")
            return ''
        series = data.get('series', [])
        if not series:
            return ''
        data_date = data.get('data_date', 'unknown')
        stretched_count = data.get('stretched_count', 0)

        _TYPE_LABELS = {
            'SPEC_NET': 'spec net',
            'COMM_NET': 'comm net',
            'SPEC_LONG_PCT': 'large-spec long%',
        }

        def _fmt_series(s):
            market = s.get('market', '?')
            stype = s.get('type', '?')
            type_label = _TYPE_LABELS.get(stype, stype.lower())
            value = s.get('value')
            zscore = s.get('zscore')
            percentile = s.get('percentile')
            wow = s.get('wow_change')
            stretched = s.get('stretched', False)

            parts = [f"{market} {type_label}"]
            if value is not None:
                parts.append(f"{value:,.0f}" if abs(value) >= 1 else f"{value:.2f}")
            if zscore is not None:
                parts.append(f"z={zscore:+.1f}σ")
            if percentile is not None:
                parts.append(f"{percentile:.0f}th pct")
            if wow is not None:
                # WoW unit depends on type: SPEC_LONG_PCT is pp, others are contracts
                unit = "pp" if stype == 'SPEC_LONG_PCT' else ""
                parts.append(f"{wow:+.1f}{unit} WoW")
            suffix = " — STRETCHED" if stretched else ""
            return " | ".join(parts) + suffix

        # Stretched series first, then remaining by |z| (up to 6 total)
        stretched = [s for s in series if s.get('stretched')]
        non_stretched = [s for s in series if not s.get('stretched')]
        ordered = stretched + non_stretched
        top6 = ordered[:6]

        lines = [f"Smart-money positioning (CFTC COT, week of {data_date}):"]
        for s in top6:
            lines.append("  " + _fmt_series(s))
        lines.append(f"  (stretched: {stretched_count} of {len(series)} series)")

        text = '\n'.join(lines)
        if len(text) > 1200:
            text = text[:1200].rstrip() + "\n  …(truncated)"
        return text
    except Exception as e:
        log.debug("fetch_cot failed: %s", e)
        return ''


def _fetch_context_data_fallback():
    """Legacy fallback: fetch context via individual API calls (if preassembly fails)."""
    log.warning("Using legacy fallback for context data (individual API calls)")
    sections = {}

    def _fetch(name, path, formatter=None):
        data = _api(path, timeout=20, retries=1)
        if formatter:
            return name, formatter(data)
        return name, json.dumps(data, indent=2) if not isinstance(data, str) else data

    tasks = {
        "alerts": ("/api/alerts?active=false", _format_json_compact),
        "positions": ("/api/drawings/positions", _format_json_compact),
        "plays": ("/api/plays/context", _format_plays),
        "signal_diff": ("/api/report/diff", _format_signal_diff_legacy),
        # Registry-filtered, matching the preassembly path above.
        "rs_rankings": ("/api/relative-strength", _format_rs_registry_only),
        "sentiment": ("/api/market-sentiment", _format_json_compact),
        "sentiment_history": ("/api/sentiment-history?hours=24", _format_json_compact),
        "history_context": ("/api/history/context?days=14", _format_json_compact),
        "yesterday_report": ("/api/report/morning/latest", _format_yesterday),
    }

    # Operator-scoped sections require a per-user context the shared admin key
    # lacks (→ 401). They feed only the operator morning report; skip the fetch
    # (keep empty keys so downstream consumers don't KeyError) when the morning
    # report is disabled.
    if not ENABLE_MORNING_REPORT:
        for _k in ("alerts", "positions", "plays", "history_context"):
            sections[_k] = ""
            tasks.pop(_k, None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {}
        for name, (path, fmt) in tasks.items():
            futs[pool.submit(_fetch, name, path, fmt)] = name
        for fut in as_completed(futs):
            try:
                name, text = fut.result()
                sections[name] = text
            except Exception as e:
                name = futs[fut]
                sections[name] = f"Unavailable: {e}"
                log.warning("Context fetch failed for %s: %s", name, e)

    return sections


def _format_plays(data):
    """Format plays context for the morning report personal section."""
    if isinstance(data, dict) and "error" in data:
        return ""
    plays = data.get("plays", []) if isinstance(data, dict) else []
    if not plays:
        return ""

    lines = [f"## Active Plays ({len(plays)})\n"]
    for p in plays:
        status = p.get("status", "?")
        title = p.get("title", "Untitled")
        ptype = p.get("type", "directional")
        approach = p.get("approach", "single")
        direction = p.get("direction", "?")
        asset = p.get("primary_asset", "?")
        conds_met = p.get("conditions_met", "0/0")

        header = f"**{title}** ({ptype}, {approach}, {status})"
        lines.append(header)

        thesis = p.get("thesis", "")
        if thesis:
            lines.append(f"Thesis: {thesis}")

        logic = p.get("conditions_logic", "all")
        lines.append(f"Direction: {direction}, Asset: {asset}, Conditions: {conds_met} ({logic.upper()})")

        # Per-condition detail so AI can reference specific conditions
        cond_details = p.get("conditions_detail", [])
        if cond_details:
            for cd in cond_details:
                met_str = "MET" if cd.get("met") else "NOT MET"
                label = cd.get("label", cd.get("type", "?"))
                evidence = cd.get("evidence", "")
                line = f"  - [{met_str}] {label}"
                if evidence:
                    line += f" — {evidence}"
                lines.append(line)

        action_plan = p.get("action_plan", "")
        if action_plan:
            lines.append(f"Action plan: {action_plan}")

        inv = p.get("invalidation")
        if inv and isinstance(inv, dict):
            inv_params = inv.get("params", inv)
            inv_label = inv.get("label") or ""
            if not inv_label:
                inv_type = inv.get("type", "?")
                if inv_type == "price_level":
                    inv_label = f"Price {inv_params.get('relation', '?')} ${inv_params.get('level', '?')}"
                else:
                    inv_label = inv_type.replace("_", " ")
            inv_met = "MET" if inv.get("met") else "not met"
            inv_evidence = inv.get("evidence", "")
            inv_line = f"Invalidation: {inv_label} ({inv_met})"
            if inv_evidence:
                inv_line += f" — {inv_evidence}"
            lines.append(inv_line)

        expiry = p.get("expiry")
        if expiry:
            lines.append(f"Expiry: {expiry}")

        timeframe = p.get("timeframe")
        if timeframe:
            lines.append(f"Timeframe: {timeframe}")

        lines.append("")

    return "\n".join(lines)


def _format_json_compact(data):
    """Format JSON data compactly — no nested indentation."""
    if isinstance(data, dict) and "error" in data:
        return f"Unavailable: {data['error']}"
    return json.dumps(data, separators=(",", ":"))


def _format_alerts_summary(data):
    """Render alerts as a narrative block for the briefing prompt.

    Shape:
      TRIGGERED IN LAST 24H (N):
        - BTC above $65,000 at 08:14Z (price was $64,980)
        - ETH: indicator consensio → bullish at 07:02Z
      ACTIVE WATCHLIST (M):
        - BTC above $72,000 — 12% distance
        - ETH below $3,200 — 4% distance
      PAUSED (K)
    Empty sections are omitted. Returns 'No alerts.' when nothing is set.
    """
    if isinstance(data, dict) and "error" in data:
        return f"Unavailable: {data['error']}"
    alerts = data.get("alerts", []) if isinstance(data, dict) else data
    if not alerts:
        return "No alerts configured."

    try:
        from datetime import datetime, timedelta, timezone
    except Exception:
        return json.dumps(data, separators=(",", ":"))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    triggered_recent = []
    active_watch = []
    paused = 0

    for a in alerts:
        active = a.get("active", True)
        ts = a.get("triggered_at")
        fired_recently = False
        if ts:
            try:
                fired_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if fired_dt.tzinfo is None:
                    fired_dt = fired_dt.replace(tzinfo=timezone.utc)
                if fired_dt >= cutoff:
                    fired_recently = True
                    triggered_recent.append((fired_dt, a))
            except Exception:
                pass
        if not active:
            paused += 1
            continue
        if not fired_recently:
            active_watch.append(a)

    lines = []

    if triggered_recent:
        triggered_recent.sort(key=lambda kv: kv[0], reverse=True)
        lines.append(f"TRIGGERED IN LAST 24H ({len(triggered_recent)}):")
        for fired_dt, a in triggered_recent[:8]:
            lines.append(f"  - {_describe_alert(a, fired=True, fired_dt=fired_dt)}")

    if active_watch:
        lines.append(f"ACTIVE WATCHLIST ({len(active_watch)}):")
        for a in active_watch[:10]:
            lines.append(f"  - {_describe_alert(a, fired=False)}")
        if len(active_watch) > 10:
            lines.append(f"  ... +{len(active_watch) - 10} more")

    if paused:
        lines.append(f"PAUSED: {paused}")

    if not lines:
        return "No alerts configured."
    return "\n".join(lines)


def _describe_alert(a, fired=False, fired_dt=None):
    """Render a single alert as one line of human text."""
    atype = a.get("alert_type", "price")
    sym = a.get("symbol") or a.get("metric") or a.get("chain") or "?"
    note = a.get("note") or ""
    note_suffix = f" — {note}" if note else ""

    if fired and fired_dt is not None:
        ts_str = fired_dt.strftime("%H:%MZ")
        tp = a.get("triggered_price")
        price_suffix = f" (price was ${tp:,.2f})" if tp else ""
        if atype == "price":
            cond = a.get("condition", "")
            price = a.get("price", 0)
            return f"{sym} {cond} ${price:,.2f} at {ts_str}{price_suffix}{note_suffix}"
        if atype == "indicator":
            return (
                f"{sym}: indicator {a.get('indicator', '?')} → "
                f"{a.get('target_state', '?')} at {ts_str}{note_suffix}"
            )
        if atype == "formula":
            expr = a.get("expression", "")
            return f"{sym} formula `{expr[:60]}` at {ts_str}{note_suffix}"
        if atype == "zscore":
            return (
                f"{sym} z-score {a.get('condition', '')} "
                f"{a.get('threshold', '?')}σ at {ts_str}{note_suffix}"
            )
        if atype == "regime_transition":
            return f"Macro regime change at {ts_str}{note_suffix}"
        if atype == "derivatives_regime":
            return f"Derivatives regime change at {ts_str}{note_suffix}"
        if atype == "chain_strength_breach":
            return (
                f"Transmission chain '{sym}' {a.get('condition', 'below')} "
                f"{a.get('threshold', '?')} at {ts_str}{note_suffix}"
            )
        if atype == "anomaly_cluster_active":
            return f"Multi-domain anomaly cluster active at {ts_str}{note_suffix}"
        if atype == "conditional_query_match":
            return (
                f"Conditional match for {a.get('target', 'BTC')} at "
                f"{ts_str}{note_suffix}"
            )
        return f"{sym} {atype} at {ts_str}{note_suffix}"

    # Active (not fired) description
    if atype == "price":
        cond = a.get("condition", "")
        price = a.get("price", 0)
        return f"{sym} {cond} ${price:,.2f}{note_suffix}"
    if atype == "indicator":
        return (
            f"{sym} indicator {a.get('indicator', '?')} "
            f"→ {a.get('target_state', '?')}{note_suffix}"
        )
    if atype == "formula":
        expr = a.get("expression", "")
        return f"{sym} formula `{expr[:80]}`{note_suffix}"
    if atype == "zscore":
        return (
            f"{sym} z-score {a.get('condition', '')} "
            f"{a.get('threshold', '?')}σ{note_suffix}"
        )
    if atype == "regime_transition":
        return f"Macro regime transition (armed){note_suffix}"
    if atype == "derivatives_regime":
        return f"Derivatives regime transition (armed){note_suffix}"
    if atype == "chain_strength_breach":
        return (
            f"Chain '{sym}' {a.get('condition', 'below')} "
            f"{a.get('threshold', '?')}{note_suffix}"
        )
    if atype == "anomaly_cluster_active":
        return (
            f"Anomaly cluster ≥{a.get('min_metrics', 3)}× "
            f"across ≥{a.get('min_domains', 2)}{note_suffix}"
        )
    if atype == "conditional_query_match":
        return f"Conditional query → {a.get('target', 'BTC')}{note_suffix}"
    return f"{sym} {atype}{note_suffix}"


def _format_signal_diff_legacy(data):
    """Format signal diff into readable text (legacy individual-call format)."""
    if isinstance(data, dict) and "error" in data:
        return "Signal diff unavailable."
    if isinstance(data, dict) and "changes" in data:
        changes = data["changes"]
        if not changes:
            return "No significant signal changes."
        lines = [_format_signal_diff_line(c) for c in changes]
        return "\n".join(lines)
    if isinstance(data, list):
        lines = []
        for item in data:
            sig = item.get("significance", "")
            asset = item.get("asset", "")
            msg = item.get("message", item.get("change", ""))
            lines.append(f"  [{sig}] {asset}: {msg}")
        return "\n".join(lines) if lines else "No significant signal changes."
    return json.dumps(data, separators=(",", ":"))


def _format_yesterday(data):
    """Extract exec summary from yesterday's report."""
    if isinstance(data, dict) and data.get("summary"):
        return f"Yesterday's Executive Summary:\n{data['summary']}"
    if isinstance(data, dict) and data.get("content"):
        # Truncate to first 2000 chars
        return f"Yesterday's Report (truncated):\n{data['content'][:2000]}"
    return "No previous report available."


# --------------------------------------------------------------------------
# Deribit options data
# --------------------------------------------------------------------------

def _strip_deribit_for_publication(snap):
    """Strip Deribit-derived content from a snapshot bound for a PUBLISHED report.

    The root cause the first four fixes all missed.

    build_snapshot() is correctly gated — but the gate asks "may this CALLER
    see it", and this caller is the report engine, which reaches the API with
    ``X-Internal-Key``. carrac/auth/decorators.py::get_request_tier resolves any valid
    key to ``admin``, so deribit_visible() returns True and the gate opens
    exactly as designed. The engine then writes what it saw into the `general`
    report, published at /brief with no login.

    So the gate conflates two different questions: "is an operator LOOKING at
    this data" and "is an operator PROCESS generating a public artifact". A
    published report is public no matter who ran the job.

    This is the second question, answered at the only place that knows it: the
    report assembly. Unconditional — not caller-dependent — because everything
    that flows through here ends up in a shared or public report. The operator
    still gets Deribit through the admin MCP tools and the API directly; they
    just do not get it laundered through something anonymous visitors read.

    Filters on the metric SETS directly rather than reusing the request-scoped
    helpers in quant/api/routes.py — those short-circuit on deribit_visible(),
    which is True here by construction, and forcing it shut would mean mutating
    a module global in a multi-threaded app.
    """
    if not isinstance(snap, dict):
        return snap
    try:
        from carrac.compliance.deribit_gate import (
            DERIBIT_COLUMNS, deribit_leadlag_metrics)
        gated = set(deribit_leadlag_metrics()) | set(DERIBIT_COLUMNS)
    except Exception as e:  # noqa: BLE001 — loud: this is a licensing control
        log.error("Deribit publication filter unavailable (%s) — dropping quant "
                  "detail rather than risk publishing gated data", e)
        for k in ('top_correlations', 'leadlag_signals', 'leadlag_signals_weekly',
                  'chains', 'anomalies'):
            snap[k] = []
        snap['anomaly_count'] = 0
        return snap

    def _named(entries, key):
        return [e for e in (entries or [])
                if (e.get(key) or '') not in gated
                and (e.get(key) or '').lower() not in gated]

    snap['top_correlations'] = _named(snap.get('top_correlations'), 'metric')
    snap['leadlag_signals'] = _named(snap.get('leadlag_signals'), 'leader')
    snap['leadlag_signals_weekly'] = _named(snap.get('leadlag_signals_weekly'), 'leader')

    # This named only ONE of the two
    # Deribit-backed domains and leaned on a BTC-only metric set, so
    # SKEW_25D_ETH / GEX_ETH / OPTIONS_IV_ETH survived into the briefing the
    # writer narrates — which is what the BARRED-PROSE alarm has been firing
    # on. Now the same rule the API path uses, unconditionally: the engine
    # authenticates as admin, so a caller-gated filter is a no-op here.
    from carrac.compliance.deribit_gate import (
        strip_gated_anomalies, strip_gated_anomaly_cluster)
    snap['anomalies'] = strip_gated_anomalies(_named(snap.get('anomalies'), 'metric'))
    snap['anomaly_count'] = len(snap['anomalies'])
    if snap.get('anomaly_cluster'):
        snap['anomaly_cluster'] = strip_gated_anomaly_cluster(snap['anomaly_cluster'])

    try:
        from quant.engine.config import TRANSMISSION_CHAINS

        def _chain_ok(c):
            cdef = TRANSMISSION_CHAINS.get(c.get('chain')) or {}
            return not any(m in gated
                           for pair in (cdef.get('links') or []) for m in pair)

        snap['chains'] = [c for c in (snap.get('chains') or []) if _chain_ok(c)]
    except Exception as e:  # noqa: BLE001
        log.error("Chain registry unreadable (%s) — dropping chains", e)
        snap['chains'] = []

    return snap


# ── The permitted-data derivatives block ───────────────────────
#
# fetch_options_data() above is Deribit-only by construction and stays gated
# to the operator. This is what the SHARED reports (general, afternoon,
# weekly) actually get instead: the same job — tell the reader how the
# derivatives market is positioned — built on licence-permitted columns only.
#
# It is NOT a rename of the Deribit block, and the difference is the whole point.
# The Deribit section was a VOLATILITY read: implied vol, dealer gamma, term
# structure, the basis. Funding, open interest and long/short are a
# POSITIONING read: who is crowded, what they are paying to stay there, and
# whether the crowd is building or unwinding. Narrating these numbers in the
# Deribit vocabulary would turn a licensing fix into a subtler accuracy problem,
# so this block never emits a volatility word and the prompts say why.
#
# Coverage is wider than what was lost: the Deribit block only ever queried
# BTC and ETH, these columns are populated across the perpetual universe.

#: The ONLY columns this fetch may read. Verified against the licensing gate
#: at call time rather than trusted — see _permitted_deriv_columns_ok().
_PERMITTED_DERIV_COLUMNS = ("funding_rate_avg", "oi_usd", "long_short_ratio")

#: Binance perpetual funding settles every 8 hours, so a daily average of the
#: prints annualises at x3 x365. Named rather than inlined because it is used
#: for both the annualised rate and the funding-flow estimate.
_FUNDING_INTERVALS_PER_DAY = 3

#: History behind the percentile context. Long enough that "highest funding in
#: three months" means something; short enough to still describe this regime.
_DERIV_PERCENTILE_DAYS = 90

#: Comparison point for the build/unwind read.
_DERIV_LOOKBACK_DAYS = 7

#: Above/below these percentiles an asset is called out as an extreme.
_DERIV_HOT_PCTL = 90
_DERIV_COLD_PCTL = 10

#: Open-interest move over the lookback that counts as a build or an unwind.
_DERIV_OI_MOVE_PCT = 15

#: Binance's default interest-rate component of the funding formula, per 8h.
#: When the premium index sits near zero the rate settles HERE, so this value
#: is the neutral setting of the market, not a reading about it.
#:
#: This is load-bearing, and the first two production renders both got it wrong. Most
#: alt perpetuals sit on exactly 0.0001 for weeks; their 90-day history is
#: mostly BELOW it, so the current value scores a 92nd-95th percentile — and
#: the block duly reported seven assets as "longs paying up to hold the
#: position" on a day when not one asset's funding was above baseline at all
#: (BTC +0.0058%, ETH +0.0093%). A percentile alone cannot tell a premium from
#: a default, so the rank is necessary and not sufficient: funding is only a
#: crowding signal when it is above this line.
_BINANCE_BASELINE_FUNDING = 0.0001

#: Within this fraction of the baseline, funding IS the baseline.
_BASELINE_TOLERANCE = 0.05


def _licensing_alarm(msg: str) -> None:
    """Raise a human-reaching alarm for a licensing-control failure.

    Reaches the error tracker, not just a log line. This fires only when the derivatives fetch is
    about to read or publish something the licensing policy does not permit, which is
    exactly the class of problem that must not sit unread in a logfile.
    """
    log.error("LICENSING ALARM: %s", msg)
    try:
        import sentry_sdk
        sentry_sdk.capture_message(msg, level="error")
    except Exception:  # noqa: BLE001
        # log.error above already fired; this is not a silent loss.
        log.warning("licensing alarm: Sentry capture failed", exc_info=True)


def _permitted_deriv_columns_ok(columns) -> bool:
    """True when every named column is licence-permitted. Fails CLOSED.

    The guard exists because the cheapest way to reintroduce that leak is to
    add one more column to the SELECT above — `basis_pct` sits in the same
    table, one row down in the schema, and reads like positioning data. This
    checks the SELECT list against the licensing gate's own lists instead of
    trusting the author, and an unimportable gate module resolves to False
    rather than to "nothing is barred".
    """
    try:
        from carrac.compliance.deribit_gate import (
            DERIBIT_COLUMNS, PERMITTED_DERIVATIVES_COLUMNS)
    except Exception as e:  # noqa: BLE001
        _licensing_alarm(
            f"Permitted-derivatives fetch cannot import the licensing gate "
            f"({e}) — withholding the block rather than publishing unchecked "
            f"columns.")
        return False

    cols = set(columns)
    barred = cols & set(DERIBIT_COLUMNS)
    unlisted = cols - set(PERMITTED_DERIVATIVES_COLUMNS)
    if not barred and not unlisted:
        return True

    _licensing_alarm(
        f"Permitted-derivatives fetch would read columns the licensing policy does not "
        f"permit — barred: {sorted(barred) or 'none'}; not on the permitted "
        f"list: {sorted(unlisted) or 'none'}. The block is withheld. Either "
        f"the SELECT gained a column or deribit_gate's lists moved.")
    return False


def _pctl_rank(history, value):
    """Mid-rank percentile of `value` within `history` (0-100), or None.

    MID-rank, not share-at-or-below, and the difference is not academic here.
    Binance's default funding rate is exactly 0.0001, and most alt perpetuals
    sit on it for weeks at a time. Share-at-or-below counts every tie as
    "below", so a value equal to its own 90-day mode scored the 100th
    percentile — and the first render against prod data duly announced that
    eight of ten assets had funding at a three-month extreme when every one of
    them was simply at the default. A read that confident and that wrong is
    worse than no read.

    Averaging the strictly-below and at-or-below ranks puts a value tied with
    most of its history in the MIDDLE of that history, which is what it is.

    Needs a real sample: under 20 observations a "97th percentile" is an
    artefact of the window, so this returns None and the caller prints
    nothing rather than something impressive and meaningless.
    """
    vals = [v for v in history if v is not None]
    if value is None or len(vals) < 20:
        return None
    below = sum(1 for v in vals if v < value)
    at_or_below = sum(1 for v in vals if v <= value)
    return round(100.0 * (below + at_or_below) / (2.0 * len(vals)))


def _ordinal(n):
    """1 -> 1st, 2 -> 2nd, 11 -> 11th. The first prod render said "1th"."""
    if n is None:
        return "—"
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fmt_usd_compact(v):
    """$8.46B / $817M / $28.8M — OI spans three orders of magnitude."""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def _fmt_pct_signed(v, places=1):
    return "—" if v is None else f"{v:+.{places}f}%"


def fetch_permitted_derivatives():
    """Positioning read built only on licence-permitted derivatives columns.

    Returns a ``(markdown, claims)`` tuple — the same idiom as
    ``fetch_options_data`` — so the caller can hand ``claims`` straight to
    ``morning_engine.engine._grounded_field_tripwire`` and have
    the published prose checked against the DB row it was rendered from.

    UNCONDITIONAL: no ``deribit_visible()`` check, deliberately. This block
    contains no licensed data, so there is no caller to gate on — and the
    reports it feeds are published at /brief with no login, which is what
    made a caller-gated rule the wrong shape for it.

    Two guards, both loud, both able to withhold the block:

    * ``_permitted_deriv_columns_ok`` checks the SELECT list against
      ``deribit_gate``'s own permitted/barred lists before the query runs.
    * ``alert_if_barred_prose`` checks the rendered markdown for the NARRATED
      form of any barred metric before it is returned, so a future edit that
      reintroduces volatility vocabulary here is caught at the source rather
      than by the render-time scrub silently cleaning up after it forever.

    Returns ``("", [])`` when a guard trips or there is no data — an empty
    string, not a placeholder note, for the reason ``fetch_options_data``
    documents: a note invites the writer model to narrate its own absence,
    which reads as a fault to a stranger.
    """
    if not _permitted_deriv_columns_ok(_PERMITTED_DERIV_COLUMNS):
        return "", []

    try:
        from carrac.db_pool import get_quant_conn as get_conn

        since = (datetime.now() - timedelta(days=_DERIV_PERCENTILE_DAYS)).date()
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ts, asset, funding_rate_avg, oi_usd, long_short_ratio
                FROM quant.derivatives
                WHERE ts >= %s
                ORDER BY asset, ts
                """,
                (since,),
            )
            rows = cur.fetchall() or []
    except Exception as e:  # noqa: BLE001
        # Not silent: the caller reports this into the report's own data-source
        # health panel, and a missing positioning block is visible in the
        # published artifact.
        log.error("Permitted-derivatives fetch failed: %s", e, exc_info=True)
        return "", []

    by_asset = {}
    for r in rows:
        by_asset.setdefault(r["asset"], []).append(r)
    if not by_asset:
        log.warning("Permitted-derivatives fetch: no rows in the last %dd",
                    _DERIV_PERCENTILE_DAYS)
        return "", []

    assets = []
    for asset, series in by_asset.items():
        # The newest bar that actually CARRIES a permitted value, not simply
        # the newest bar.
        #
        # quant.derivatives is shared. quant/collectors/deribit_options.py runs
        # at 00:00/06:00/12:00/18:00 UTC and upserts a row for BTC and ETH keyed
        # on today's date with ONLY its own columns, leaving funding, OI and
        # long/short NULL; quant/collectors/binance_daily.py fills those at
        # 00:15. Taking series[-1] therefore drops BTC and ETH — the two assets
        # that matter most — for any window where the Deribit row exists and the
        # Binance one does not: the 15-minute gap every night, and the whole day
        # whenever binance_daily fails or loses the flock.
        #
        # That failure is SILENT. The alts still render, so the block is
        # non-empty and data_sources["permitted_derivatives"] still reads "ok",
        # while every asset whose newest bar has this shape withholds the section.
        latest = None
        for row in reversed(series):
            if (row.get("funding_rate_avg") is not None
                    or row.get("oi_usd") is not None
                    or row.get("long_short_ratio") is not None):
                latest = row
                break
        if latest is None:
            continue
        funding = _safe_float(latest.get("funding_rate_avg"))
        oi = _safe_float(latest.get("oi_usd"))
        ls = _safe_float(latest.get("long_short_ratio"))

        # The comparison row: the newest bar at least _DERIV_LOOKBACK_DAYS
        # old. Falls back to the oldest available rather than skipping the
        # delta, so a young series still says something.
        cutoff = latest["ts"] - timedelta(days=_DERIV_LOOKBACK_DAYS)
        prior = None
        for r in series:
            if r["ts"] <= cutoff:
                prior = r
        prior = prior or (series[0] if len(series) > 1 else None)

        oi_prev = _safe_float(prior.get("oi_usd")) if prior else None
        ls_prev = _safe_float(prior.get("long_short_ratio")) if prior else None
        oi_chg = ((oi - oi_prev) / oi_prev * 100.0
                  if oi is not None and oi_prev else None)
        ls_chg = ((ls - ls_prev) / ls_prev * 100.0
                  if ls is not None and ls_prev else None)

        assets.append({
            "asset": asset,
            "ts": latest["ts"],
            "funding": funding,
            "funding_ann": (funding * _FUNDING_INTERVALS_PER_DAY * 365 * 100.0
                            if funding is not None else None),
            "funding_pctl": _pctl_rank(
                [_safe_float(r.get("funding_rate_avg")) for r in series], funding),
            "oi": oi,
            "oi_chg": oi_chg,
            "ls": ls,
            "ls_chg": ls_chg,
            "ls_pctl": _pctl_rank(
                [_safe_float(r.get("long_short_ratio")) for r in series], ls),
            # funding x OI x intervals = what the long side pays the short
            # side per day at the current rate. This is the economically
            # meaningful combination of the two columns. (`funding_oi_ratio`,
            # a phantom column nothing ever wrote, was removed from the
            # formula engine and the gate lists.)
            "funding_flow": (funding * oi * _FUNDING_INTERVALS_PER_DAY
                             if funding is not None and oi is not None else None),
        })

    if not assets:
        log.warning("Permitted-derivatives fetch: rows present but no "
                    "permitted values in the latest bar of any asset")
        return "", []

    assets.sort(key=lambda a: (a["oi"] is None, -(a["oi"] or 0)))
    latest_ts = max(a["ts"] for a in assets)
    age_days = (datetime.now().date() - latest_ts).days

    lines = [
        "## Perpetual-Futures Positioning",
        "",
        "Source: Binance perpetual futures — funding rate, open interest and "
        "the global long/short account ratio. This is a POSITIONING read, not "
        "a volatility one: it says who is crowded and what they pay to stay "
        "there. No implied-volatility, options or futures-basis reading is "
        "available; do not describe these numbers in those terms.",
        "",
        f"Latest bar: {latest_ts}"
        + (f" — {age_days}d old, treat as stale" if age_days >= 2 else "")
        + ". Funding is the daily average of the 8-hourly prints; annualised "
          f"= rate x {_FUNDING_INTERVALS_PER_DAY} x 365. Percentiles are "
          f"against this asset's own last {_DERIV_PERCENTILE_DAYS} days. "
          f"Changes are vs {_DERIV_LOOKBACK_DAYS} days ago.",
        "",
        "| Asset | Funding 8h | Funding annualised | Funding %ile | "
        "Open interest | OI 7d | Long/short | L/S %ile | L/S 7d |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for a in assets:
        # An asset whose own bar is older than the newest one is labelled, so a
        # half-collected day reads as a half-collected day rather than as
        # today's positioning.
        stale_days = (latest_ts - a["ts"]).days
        label = a["asset"] + (f" ({stale_days}d old)" if stale_days else "")
        lines.append(
            f"| {label} "
            f"| {('%+.4f%%' % (a['funding'] * 100)) if a['funding'] is not None else '—'} "
            f"| {_fmt_pct_signed(a['funding_ann'])} "
            f"| {a['funding_pctl'] if a['funding_pctl'] is not None else '—'} "
            f"| {_fmt_usd_compact(a['oi'])} "
            f"| {_fmt_pct_signed(a['oi_chg'])} "
            f"| {('%.2f' % a['ls']) if a['ls'] is not None else '—'} "
            f"| {a['ls_pctl'] if a['ls_pctl'] is not None else '—'} "
            f"| {_fmt_pct_signed(a['ls_chg'])} |"
        )

    # Deterministic extremes. A classified regime would invent a label;
    # this gives the writer the same kind of hook without inventing one —
    # every line below is a percentile of that asset's own history.
    extremes = []
    baseline_assets = []
    unrankable = []
    baseline_ann = _BINANCE_BASELINE_FUNDING * _FUNDING_INTERVALS_PER_DAY * 365 * 100.0
    for a in assets:
        f = a["funding"]
        pctl = a["funding_pctl"]
        if f is not None and pctl is None:
            # Off baseline but with too little history to rank. Saying nothing
            # would let the "no extremes" line below claim this asset is at
            # baseline when it is not — an asset can sit at +1346%
            # annualised on one observation, and the line would
            # otherwise report "no tracked asset's funding is meaningfully off
            # baseline". State the reading and the fact that it cannot be
            # judged, rather than judging it or hiding it.
            if abs(f - _BINANCE_BASELINE_FUNDING) > _BINANCE_BASELINE_FUNDING * _BASELINE_TOLERANCE:
                unrankable.append(a)
        if f is None:
            pass
        elif abs(f - _BINANCE_BASELINE_FUNDING) <= _BINANCE_BASELINE_FUNDING * _BASELINE_TOLERANCE:
            # Sitting on the default. Reported once, collectively, as the
            # absence of a signal — which is what it is.
            baseline_assets.append(a["asset"])
        elif f < 0:
            extremes.append(
                f"- {a['asset']}: funding is NEGATIVE at "
                f"{_fmt_pct_signed(a['funding_ann'])} annualised — the short "
                f"side is paying the long side to keep the position open.")
        elif f > _BINANCE_BASELINE_FUNDING and pctl is not None and pctl >= _DERIV_HOT_PCTL:
            extremes.append(
                f"- {a['asset']}: funding {_fmt_pct_signed(a['funding_ann'])} "
                f"annualised, above Binance's {baseline_ann:.2f}% baseline and "
                f"at the {_ordinal(pctl)} percentile of its last "
                f"{_DERIV_PERCENTILE_DAYS} days — longs are paying a real "
                f"premium to hold the position.")
        elif pctl is not None and pctl <= _DERIV_COLD_PCTL:
            extremes.append(
                f"- {a['asset']}: funding {_fmt_pct_signed(a['funding_ann'])} "
                f"annualised, the {_ordinal(pctl)} percentile and below "
                f"Binance's {baseline_ann:.2f}% baseline — next to no premium "
                f"is being paid by either side.")
        if a["ls_pctl"] is not None and a["ls_pctl"] >= _DERIV_HOT_PCTL:
            extremes.append(
                f"- {a['asset']}: long/short ratio {a['ls']:.2f}, the "
                f"{_ordinal(a['ls_pctl'])} percentile of its last "
                f"{_DERIV_PERCENTILE_DAYS} days — retail accounts are "
                f"unusually one-sided long.")
        elif a["ls_pctl"] is not None and a["ls_pctl"] <= _DERIV_COLD_PCTL:
            extremes.append(
                f"- {a['asset']}: long/short ratio {a['ls']:.2f}, the "
                f"{_ordinal(a['ls_pctl'])} percentile — the long side is "
                f"unusually thin.")

    for a in unrankable:
        extremes.append(
            f"- {a['asset']}: funding {_fmt_pct_signed(a['funding_ann'])} "
            f"annualised, off Binance's {baseline_ann:.2f}% baseline — but this "
            f"series has under 20 days of history, so there is no basis to say "
            f"whether that is unusual for it.")

    if baseline_assets:
        extremes.append(
            f"- Funding is sitting on Binance's baseline setting "
            f"({baseline_ann:+.2f}% annualised) for "
            f"{', '.join(baseline_assets)} — that is the rate's default when "
            f"the perpetual tracks spot, so it says no premium is being paid "
            f"either way in those books, not that positioning is stretched.")

    # Open interest gets a BREADTH line, not one bullet per asset. Leverage
    # builds and unwinds are market-wide far more often than not — the first
    # production render produced eight near-identical OI bullets, which buries the
    # one asset moving against the tape. Naming the direction once and the
    # exception explicitly is the more useful read and the shorter one.
    movers = [a for a in assets
              if a["oi_chg"] is not None and abs(a["oi_chg"]) >= _DERIV_OI_MOVE_PCT]
    ups = [a for a in movers if a["oi_chg"] > 0]
    downs = [a for a in movers if a["oi_chg"] < 0]
    for side, label in ((ups, "building"), (downs, "unwinding")):
        if not side:
            continue
        if len(side) >= 3:
            lo = min(a["oi_chg"] for a in side)
            hi = max(a["oi_chg"] for a in side)
            against = downs if label == "building" else ups
            extremes.append(
                f"- Open interest is {label} across the board: {len(side)} of "
                f"{len(assets)} tracked perpetuals moved "
                f"{_fmt_pct_signed(lo)} to {_fmt_pct_signed(hi)} over "
                f"{_DERIV_LOOKBACK_DAYS} days"
                + (f", against {', '.join(a['asset'] for a in against)}, which "
                   f"went the other way." if against else "."))
        else:
            for a in side:
                extremes.append(
                    f"- {a['asset']}: open interest {_fmt_pct_signed(a['oi_chg'])} "
                    f"over {_DERIV_LOOKBACK_DAYS} days to "
                    f"{_fmt_usd_compact(a['oi'])} — positions are {label}.")

    if extremes:
        lines += ["", "### Positioning extremes", ""] + extremes
    else:
        judged = sum(1 for a in assets if a["funding_pctl"] is not None
                     or a["ls_pctl"] is not None)
        lines += ["", "### Positioning extremes", "",
                  f"- None found among the {judged} of {len(assets)} tracked "
                  f"assets with enough history to rank: their funding sits on "
                  f"Binance's {baseline_ann:+.2f}% baseline, their long/short "
                  f"ratios are inside their own {_DERIV_COLD_PCTL}th-"
                  f"{_DERIV_HOT_PCTL}th percentile bands, and no open-interest "
                  f"move exceeded {_DERIV_OI_MOVE_PCT}% over "
                  f"{_DERIV_LOOKBACK_DAYS} days."]

    flows = [a for a in assets if a["funding_flow"]]
    if flows:
        total = sum(a["funding_flow"] for a in flows)
        biggest = max(flows, key=lambda a: abs(a["funding_flow"]))
        lines += [
            "",
            f"Funding flow at the current rate: about "
            f"{_fmt_usd_compact(abs(total))} per day across the tracked "
            f"perpetuals, "
            + ("paid by longs to shorts" if total >= 0
               else "paid by shorts to longs")
            + f"; the largest single leg is {biggest['asset']} at "
              f"{_fmt_usd_compact(abs(biggest['funding_flow']))} per day.",
        ]

    markdown = "\n".join(lines)

    # Grounding claims, from the same rows the markdown renders.
    # Deliberately narrow: only labels a stated number can be matched to
    # unambiguously. `funding` is excluded on purpose — a report saying
    # "BTC funding at +0.006% (about +6.4% annualised)" puts two correct
    # numbers next to the same word, and the scanner takes the nearest one.
    # An unearned alarm is what an ignored alarm costs.
    claims = []
    for a in assets:
        if a["oi"]:
            claims.append({
                "asset": a["asset"], "field_label_regex": r"open\s+interest",
                "truth": a["oi"], "tol": 0.02, "unit": "usd",
            })
        if a["ls"]:
            claims.append({
                "asset": a["asset"],
                "field_label_regex": r"long[-/\s]short(?:\s+(?:ratio|account))?",
                "truth": a["ls"], "tol": 0.02, "unit": "ratio",
            })
        if a["funding_ann"]:
            claims.append({
                "asset": a["asset"],
                "field_label_regex": r"annuali[sz]ed",
                "truth": a["funding_ann"], "tol": 1.0, "unit": "points",
            })

    # Second guard: the rendered block itself. Unconditional and loud — if a
    # future edit narrates a barred metric here, this says so at the source
    # instead of leaving the render-time scrub to hide it forever.
    try:
        from carrac.compliance.deribit_gate import alert_if_barred_prose
        if alert_if_barred_prose(markdown, context="permitted-derivatives block"):
            return "", []
    except Exception as e:  # noqa: BLE001
        _licensing_alarm(
            f"Permitted-derivatives block could not be prose-checked ({e}) — "
            f"withheld rather than published unchecked.")
        return "", []

    return markdown, claims


def fetch_options_data():
    """Fetch enriched derivatives data from Quant DB + classify regime.

    Returns a ``(markdown, claims)`` tuple — same idiom as
    ``format_compact_asset_lines``. ``markdown`` is the structured text for
    the LLM to narrate, exactly as before. ``claims`` is a list of
    grounded-field dicts built from the SAME DB row the markdown renders —
    GEX flip point, max pain and DVOL per currency, plus BTC regime
    confidence — for
    carrac.grounding.hallucination_scan.scan_grounded_field_mismatch /
    morning_engine.engine._grounded_field_tripwire to check the generated
    report against post-publish (a BTC GEX flip point was once
    published 2.6x off with nothing to catch it). ``claims`` is always ``[]``
    when the gate withholds the block, when the DB fetch fails and the
    fallback path is used (no DB row to ground against), or when a
    particular metric is absent from the row.

    Falls back to live Deribit API if DB query fails.

    Every column this function selects — dvol, skew_25d, gex_net,
    basis_pct, the whole options complex — is Deribit-sourced, and Deribit's
    ToS bars redistributing it. There is nothing here to filter: the block is
    barred end to end, so the gate is on the whole function.

    This matters because the callers are the SHARED reports (general,
    afternoon, weekly). `general` is published at /brief with no login, so
    this path was publishing Deribit-derived prose to anonymous visitors —
    the door left open when the MCP tools, formula vars
    and templates were gated but not the report pipeline.

    deribit_visible() fails closed: the reports run from cron with neither an
    MCP caller nor a Flask request, so it resolves False and the block is
    withheld. Returns "" rather than a placeholder note on purpose — a note
    would invite the writer model to narrate its own absence, which reads as
    a fault to a stranger.
    """
    from carrac.compliance.deribit_gate import deribit_visible
    if not deribit_visible():
        return "", []

    try:
        from carrac.db_pool import get_quant_conn as get_conn
        from quant.engine.derivatives_regime import classify_derivatives_regime

        with get_conn() as conn:
            cur = conn.cursor()

            lines = ["## Derivatives Regime Analysis"]
            claims: list = []

            # Get Fed Funds rate for benchmark
            fed_funds = None
            try:
                cur.execute("""
                    SELECT value FROM quant.macro
                    WHERE metric = 'FED_FUNDS'
                    ORDER BY ts DESC LIMIT 1
                """)
                row = cur.fetchone()
                if row and row.get('value'):
                    fed_funds = float(row['value'])
            except Exception:
                pass

            btc_metrics = None

            for currency in ['BTC', 'ETH']:
                cur.execute("""
                    SELECT options_put_call_ratio, options_oi_usd, options_max_pain,
                           options_iv_atm, dvol, basis_pct, skew_25d,
                           iv_7d, iv_30d, iv_90d, term_structure_slope,
                           gex_net, gex_flip_price, iv_rv_premium, ts
                    FROM quant.derivatives
                    WHERE asset = %s
                    ORDER BY ts DESC LIMIT 1
                """, (currency,))
                row = cur.fetchone()

                if not row:
                    lines.append(f"\n### {currency}: Data unavailable")
                    continue

                # Get spot price from the options data or index
                spot = None
                try:
                    import requests as req
                    resp = req.get(
                        'https://www.deribit.com/api/v2/public/get_index_price',
                        params={'index_name': f'{currency.lower()}_usd'},
                        timeout=10
                    )
                    spot = resp.json().get('result', {}).get('index_price')
                except Exception:
                    pass

                pc_ratio = _safe_float(row.get('options_put_call_ratio'))
                max_pain = _safe_float(row.get('options_max_pain'))
                iv_atm = _safe_float(row.get('options_iv_atm'))
                dvol = _safe_float(row.get('dvol'))
                basis_apr = _safe_float(row.get('basis_pct'))
                skew = _safe_float(row.get('skew_25d'))
                iv_7d = _safe_float(row.get('iv_7d'))
                iv_30d = _safe_float(row.get('iv_30d'))
                iv_90d = _safe_float(row.get('iv_90d'))
                term_slope = _safe_float(row.get('term_structure_slope'))
                gex_net = _safe_float(row.get('gex_net'))
                gex_flip = _safe_float(row.get('gex_flip_price'))
                iv_rv = _safe_float(row.get('iv_rv_premium'))

                # Grounded-field claims from this SAME row, for
                # the post-publish scan_grounded_field_mismatch check. Price-
                # like fields use a 2% fractional tolerance; DVOL (already a
                # small points-scale number) uses an absolute ±2-point one.
                if gex_flip is not None:
                    claims.append({
                        "asset": currency, "field_label_regex": r"GEX\s+flip\s+point",
                        "truth": gex_flip, "tol": 0.02, "unit": "usd",
                    })
                if max_pain is not None:
                    claims.append({
                        "asset": currency, "field_label_regex": r"max\s+pain",
                        "truth": max_pain, "tol": 0.02, "unit": "usd",
                    })
                if dvol is not None:
                    claims.append({
                        "asset": currency,
                        # Not the "Dvol Btc–Rvol Btc" lead-lag ratio row —
                        # a bare \\bDVOL\\b would match it and compare a ratio
                        # against implied vol points.
                        "field_label_regex": r"\bDVOL\b(?![\w\s]{0,12}[–—-]\s*Rvol)",
                        "truth": dvol, "tol": 2, "unit": "points",
                    })

                # Classify regime (BTC only — primary market)
                if currency == 'BTC':
                    btc_metrics = {
                        'basis_apr': basis_apr,
                        'skew_25d': skew,
                        'dvol': dvol,
                        'iv_rv_premium': iv_rv,
                        'put_call_ratio': pc_ratio,
                        'term_slope': term_slope,
                        'gex_net': gex_net,
                        'gex_flip_price': gex_flip,
                        'spot': spot,
                    }
                    regime = classify_derivatives_regime(btc_metrics)
                    # The "ALSO FLAGGED, UNVERIFIED" case: the same report
                    # that mis-stated the GEX flip point also stated a regime
                    # confidence the live classifier reportedly disagreed
                    # with. confidence is a 0-1 fraction; the rendered line
                    # (and the LLM's own narration) states it as a percent,
                    # so ground it in percentage points with a ±5pt band.
                    if regime.get('confidence') is not None:
                        claims.append({
                            "asset": "BTC", "field_label_regex": r"confidence",
                            "truth": float(regime['confidence']) * 100,
                            "tol": 5, "unit": "points",
                        })
                    lines.append(f"\n### Current Regime: \"{regime.get('regime_name', 'Unknown')}\" ({regime.get('confidence', 0):.0%} confidence)")
                    if regime.get('secondary_name'):
                        lines.append(f"Secondary: \"{regime['secondary_name']}\" ({regime.get('secondary_confidence', 0):.0%})")
                    lines.append(f"Description: {regime.get('description', '')}")

                lines.append(f"\n### {currency} Derivatives Snapshot" + (f" (spot ~${spot:,.0f})" if spot else ""))

                # Futures basis with Treasury benchmark
                if basis_apr is not None:
                    benchmark = ""
                    if fed_funds is not None:
                        if basis_apr < fed_funds:
                            benchmark = f" -- BELOW Fed Funds {fed_funds:.2f}% (no carry trade incentive)"
                        else:
                            benchmark = f" -- ABOVE Fed Funds {fed_funds:.2f}% (carry trade attractive)"
                    lines.append(f"- Futures Basis APR: {basis_apr:+.2f}%{benchmark}")

                # Skew
                if skew is not None:
                    skew_label = "puts more expensive -- defensive positioning" if skew > 3 else \
                                 "calls more expensive -- bullish demand" if skew < -3 else "balanced"
                    lines.append(f"- 25-Delta Skew: {skew:+.1f}% ({skew_label})")

                # Volatility
                vol_parts = []
                if dvol is not None:
                    vol_parts.append(f"DVOL: {dvol:.1f}")
                if iv_atm is not None:
                    vol_parts.append(f"ATM IV: {iv_atm:.1f}%")
                if iv_rv is not None:
                    iv_rv_label = "options overpriced vs realized" if iv_rv > 5 else \
                                  "options underpriced -- DANGER" if iv_rv < -5 else "fairly priced"
                    vol_parts.append(f"IV-RV Premium: {iv_rv:+.1f}% ({iv_rv_label})")
                if vol_parts:
                    lines.append(f"- {' | '.join(vol_parts)}")

                # Put/Call + Max Pain
                pc_parts = []
                if pc_ratio is not None:
                    pc_parts.append(f"Put/Call Ratio: {pc_ratio:.3f}")
                if max_pain is not None:
                    pain_vs_spot = ""
                    if spot:
                        diff_pct = ((max_pain - spot) / spot) * 100
                        pain_vs_spot = f" ({diff_pct:+.1f}% from spot)"
                    pc_parts.append(f"Max Pain: ${max_pain:,.0f}{pain_vs_spot}")
                if pc_parts:
                    lines.append(f"- {' | '.join(pc_parts)}")

                # Term structure
                if iv_7d is not None or iv_30d is not None or iv_90d is not None:
                    ts_parts = []
                    if iv_7d is not None:
                        ts_parts.append(f"7d={iv_7d:.0f}%")
                    if iv_30d is not None:
                        ts_parts.append(f"30d={iv_30d:.0f}%")
                    if iv_90d is not None:
                        ts_parts.append(f"90d={iv_90d:.0f}%")
                    ts_label = ""
                    if term_slope is not None:
                        if term_slope > 0.03:
                            ts_label = " (contango -- normal, calm market)"
                        elif term_slope < -0.03:
                            ts_label = " (BACKWARDATION -- near-term fear elevated)"
                        else:
                            ts_label = " (flat)"
                    lines.append(f"- Term Structure: {' -> '.join(ts_parts)}{ts_label}")

                # GEX
                #
                # This row is whatever the collector wrote most
                # recently (ORDER BY ts DESC LIMIT 1), so the ~06:06 general
                # report and the ~12:31 afternoon report legitimately read
                # DIFFERENT snapshots — measured 3.1% apart on 07-31 (66,920
                # vs 69,023) and 3.3% on 08-05. Unlabelled, that reads as the
                # two reports contradicting each other on the same day, and
                # after a 2.6x-wrong GEX was once published, a reader
                # has no way to tell drift from a repeat of that bug.
                #
                # Pinning both reports to one snapshot was the other option
                # and is worse: the afternoon report exists to be more
                # current, and freezing it to the morning's numbers would
                # make it quietly stale. Stamping the reading is what makes
                # the two figures reconcilable — same idiom as the Active
                # Extremes "(as of ...)" stamps.
                if gex_net is not None:
                    gex_sign = "positive (dealers DAMPEN moves)" if gex_net > 0 else "negative (dealers AMPLIFY moves)"
                    gex_line = f"- GEX: Net {gex_sign}, ${abs(gex_net):,.0f}"
                    if gex_flip is not None:
                        flip_rel = ""
                        if spot:
                            if spot > gex_flip:
                                flip_rel = " (spot ABOVE flip -- long gamma zone)"
                            else:
                                flip_rel = " (spot BELOW flip -- short gamma zone, moves amplified)"
                        gex_line += f" | Flip Point: ${gex_flip:,.0f}{flip_rel}"
                    stamp = _derivatives_ts_label(row.get('ts'))
                    if stamp:
                        gex_line += f" [snapshot {stamp}]"
                    lines.append(gex_line)

            # Add regime narrative seed and context for LLM
            if btc_metrics:
                regime = classify_derivatives_regime(btc_metrics)
                if regime.get('narrative_seed'):
                    lines.append("\n### Regime Narrative Seed")
                    lines.append(regime['narrative_seed'])
                if regime.get('implications'):
                    lines.append("\n### Implications")
                    for imp in regime['implications']:
                        lines.append(f"- {imp}")
                if regime.get('transitions'):
                    lines.append("\n### What to Watch (Regime Transitions)")
                    for tr in regime['transitions']:
                        lines.append(f"- {tr}")
                if regime.get('historical_analog'):
                    lines.append(f"\n### Historical Analogs: {regime['historical_analog']}")

            return "\n".join(lines), claims

    except Exception as e:
        log.warning("Enriched derivatives fetch failed, falling back to live API: %s", e)
        # No DB row available on the fallback path, so nothing to ground a
        # per-field claim against — claims is always [] here.
        return _fetch_options_data_fallback(), []


def _fetch_options_data_fallback():
    """Fallback: fetch basic options data directly from Deribit API."""
    try:
        btc = requests.get(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option",
            timeout=15
        ).json()
        eth = requests.get(
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=ETH&kind=option",
            timeout=15
        ).json()

        lines = ["## Options Data (Deribit) [FALLBACK - enriched data unavailable]"]
        for currency, data in [("BTC", btc), ("ETH", eth)]:
            result = data.get("result", [])
            if not result:
                lines.append(f"\n### {currency}: Data unavailable")
                continue
            total_call_oi = sum(r.get("open_interest", 0) for r in result if "C" in r.get("instrument_name", ""))
            total_put_oi = sum(r.get("open_interest", 0) for r in result if "P" in r.get("instrument_name", ""))
            pc_ratio = total_put_oi / total_call_oi if total_call_oi else 0
            mark_price = result[0].get("underlying_price", 0) if result else 0
            lines.append(f"\n### {currency} (spot ~{mark_price:,.0f})")
            lines.append(f"Put/Call OI Ratio: {pc_ratio:.3f}")
            lines.append(f"Total Call OI: {total_call_oi:,.0f} | Put OI: {total_put_oi:,.0f}")
        return "\n".join(lines)
    except Exception as e:
        log.warning("Deribit fallback fetch failed: %s", e)
        return "Options data unavailable."


def _safe_float(val):
    """Convert Decimal/None to float safely."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def format_compact_asset_lines(chart_raw, summary_symbols):
    """Format non-spotlight assets as compact one-liners for Market Snapshot.

    Args:
        chart_raw: dict of {(asset, tf): api_response_dict}
        summary_symbols: list of asset symbols to format compactly

    Returns:
        (text, range_pctile_map) tuple.
        text: multi-line string with one line per asset — this is what the
        Market Snapshot Table 1 "Range" column is generated from.
        range_pctile_map: {symbol: int 0-100} — the grounded 90-day price
        percentile actually handed to the model for each asset, for the
        publish-time grounding scan to check the generated table against
        (carrac.grounding.hallucination_scan.scan_range_pctile_mismatch).

    This must not compute its own 20-bar high/low RANGE POSITION —
    a different metric, over a different window, than the 90-day
    CLOSE PERCENTILE (``price_percentile``) that ``carrac/facts/bundle.py``
    computes and the SAME report page renders deterministically elsewhere
    (the Cross-Asset Heat strip, morning_engine.report_blocks.
    build_cross_asset_heat_block). The model is told to use this line's
    numbers verbatim, so a divergent metric would be copied faithfully as the
    WRONG figure. Using the identical ``price_percentile`` function on the
    identical window (90 daily closes) makes this line agree with the rest
    of the page by construction instead of by luck.
    """
    from .ta_facts import price_percentile

    lines = []
    range_pctile_map: dict[str, int] = {}
    for symbol in summary_symbols:
        # Use daily data if available, fall back to any timeframe. The
        # percentile below is only computed off real DAILY closes — a
        # weekly-bar fallback would silently change the window's meaning
        # (90 weekly bars ≈ 1.7 years, not 90 days), so it is omitted
        # (never guessed) whenever daily data isn't the source in use.
        data = chart_raw.get((symbol, "1D"))
        used_daily = data is not None
        if data is None:
            data = chart_raw.get((symbol, "1W"))
        if not data:
            continue

        ohlcv = data.get("ohlcv", [])
        if not ohlcv:
            continue

        last = ohlcv[-1]
        price = last["close"]

        # Trend from EMA ribbon
        ind = data.get("indicators", {})
        ema_ribbon = ind.get("ema_ribbon", {})
        fill = ema_ribbon.get("fill", [])
        trend = fill[-1]["trend"].upper() if fill else "?"

        # RSI
        rsi_arr = data.get("rsi", [])
        rsi = f"{rsi_arr[-1]['value']:.0f}" if rsi_arr else "?"

        # Range — 90-day price percentile (SAME metric + window as the
        # deterministic range_pctile.pct rendered elsewhere on this report).
        pct = price_percentile([c.get("close") for c in ohlcv], 90) if used_daily else None
        if pct is not None:
            range_pctile_map[symbol] = pct
            rpos = f"{pct}%"
        else:
            rpos = "n/a"

        # Key signal (most notable)
        cpr = data.get("cpr", {})
        sma_data = ind.get("smaLines", {})
        sma50 = sma_data.get("sma50", [])
        sma20 = sma_data.get("sma20", [])
        sma200 = sma_data.get("sma200", [])
        key_signal = ""
        cross_fast = sma50 or sma20  # conventional 50/200 cross; fall back to 20 if sma50 absent
        if cross_fast and sma200:
            cross = "golden cross" if cross_fast[-1]["value"] > sma200[-1]["value"] else "death cross"
            key_signal = cross
        if cpr and cpr.get("pivot"):
            tc, bc = cpr["tc"], cpr["bc"]
            pos = "Above TC" if price > tc else ("Below BC" if price < bc else "Inside CPR")
            key_signal = f"{pos}" + (f", {key_signal}" if key_signal else "")

        lines.append(f"{symbol}: ${price:,.2f} | {trend} | RSI {rsi} | Range {rpos} | {key_signal}")

    return "\n".join(lines), range_pctile_map


# Indicator name → signal keys mapping (shared between personal sections and per-user reports)
INDICATOR_SIGNAL_MAP = {
    'RSI': ['rsi', 'rsi_zone', 'rsi_divergence'],
    'RSI_DIV': ['rsi_divergence'],
    'MACD': ['macd_momentum', 'macd_above_zero'],
    'TTM_SQUEEZE': ['ttm_squeeze', 'ttm_squeeze_bars', 'ttm_squeeze_momentum'],
    'BBW_PCT': ['bbw_compression', 'bbw_percentile'],
    'ATR_PCT': ['atr_low_vol', 'atr_percentile'],
    'NR7': ['nr7_active'],
    'WILLIAMS_R': ['williams_r_zone'],
    'CCI': ['cci_zone'],
    'STOCH': ['stoch_zone', 'stoch_k'],
    'STOCH_RSI': ['stoch_rsi_zone'],
    'EMA_RIBBON': ['ema_ribbon_trend', 'ema_ribbon_position'],
    'CONSENSIO': ['consensio_signal'],
    'ALLIGATOR': ['alligator_state'],
    'ICHIMOKU': ['ichimoku_cloud', 'ichimoku_tk_cross'],
    'BOLL': ['boll_position', 'boll_squeeze'],
    'KELTNER': ['keltner_position'],
    'DONCHIAN': ['donchian_breakout'],
    'PSAR': ['psar_direction', 'psar_flip'],
    'HULL_MA': ['hull_ma_dir'],
    'EMA_CROSS': ['ema_cross_state', 'ema_cross_signal'],
    'SUPER_TREND': ['supertrend_dir', 'supertrend_flip'],
    'ALMA': ['alma_dir'],
    'KAMA': ['kama_dir'],
    'CPR': ['cpr_status', 'cpr_width'],
    'AUTO_FIB': ['auto_fib_trend', 'auto_fib_position'],
    'SR_CHANNELS': ['sr_channel_count', 'sr_nearest_type', 'sr_nearest_level'],
    'FVG': ['fvg_bullish_count', 'fvg_bearish_count', 'fvg_nearest_type'],
    'VOL': ['volume_signal', 'volume_ratio'],
    'OBV': ['obv_trend', 'obv_divergence'],
    'MA': ['ma_stack', 'ma_cross'],
    'WEEKLY_MA': ['wma50', 'wma100', 'wma200'],
    'BULL_BAND': ['bull_band'],
}

_NEUTRAL_VALS = {None, "none", "normal", "neutral", "flat"}


def _build_indicator_focus(user_report_indicators, signal_data):
    """Build indicator focus section from user's selected indicators."""
    per_asset = signal_data.get("per_asset", {})
    if not per_asset or not user_report_indicators:
        return ""
    ind_names = [i for i in user_report_indicators if i in INDICATOR_SIGNAL_MAP]
    if not ind_names:
        return ""
    lines = ["## Indicator Focus"]
    lines.append(f"This trader monitors: {', '.join(ind_names)}")
    lines.append("Analyze through these indicators specifically.\n")
    lines.append("Current states:")
    for asset_key in sorted(per_asset.keys()):
        for tf_key in sorted(per_asset[asset_key].keys()):
            sigs = per_asset[asset_key][tf_key]
            relevant = {}
            for ind_id in ind_names:
                for sig_key in INDICATOR_SIGNAL_MAP.get(ind_id, []):
                    val = sigs.get(sig_key)
                    if val not in _NEUTRAL_VALS:
                        relevant[sig_key] = val
            if relevant:
                parts = [f"{k}={v}" for k, v in relevant.items()]
                lines.append(f"  {asset_key} {tf_key}: {', '.join(parts)}")
    return "\n".join(lines) + "\n"


def _build_asset_focus(user_report_assets, chart_raw, signal_data):
    """Build asset focus section with on-demand fetch for missing assets."""
    if not user_report_assets:
        return "", []

    MAX_FETCHES = 10
    fetched = 0
    asset_names = [item.get("asset", "") for item in user_report_assets if item.get("asset")]
    lines = ["## Asset Focus"]
    lines.append(f"This trader's SPOTLIGHT ASSETS: {', '.join(asset_names)}")
    lines.append("These get deep narrative analysis in Asset Spotlight.\n")
    lines.append("Current states:")

    for item in user_report_assets:
        asset = item.get("asset", "").upper()
        tfs = item.get("timeframes", [])
        if not asset or not tfs:
            continue
        for tf in tfs:
            if (asset, tf) in chart_raw:
                pa = signal_data.get("per_asset", {}).get(asset, {}).get(tf, {})
                notable = {k: v for k, v in pa.items()
                           if k not in ("asset", "tf", "status", "price")
                           and v not in _NEUTRAL_VALS}
                if notable:
                    parts = [f"{k}={v}" for k, v in list(notable.items())[:15]]
                    lines.append(f"  {asset} {tf}: {', '.join(parts)}")
            elif fetched < MAX_FETCHES:
                try:
                    data = _api(f"/api/chart-internal/{asset}?timeframe={tf}", timeout=10)
                    if data and "ohlcv" in data and not data.get("error"):
                        fetched += 1
                        from .signal_extractor import extract_all_signals as _extract
                        extra = _extract({(asset, tf): data})
                        pa = extra.get("per_asset", {}).get(asset, {}).get(tf, {})
                        notable = {k: v for k, v in pa.items()
                                   if k not in ("asset", "tf", "status", "price")
                                   and v not in _NEUTRAL_VALS}
                        if notable:
                            parts = [f"{k}={v}" for k, v in list(notable.items())[:15]]
                            lines.append(f"  {asset} {tf}: {', '.join(parts)}")
                except Exception as e:
                    log.debug("Asset focus fetch failed for %s/%s: %s", asset, tf, e)

    if len(lines) <= 4:  # No actual signal data beyond headers
        return "", asset_names
    return "\n".join(lines) + "\n", asset_names


def build_user_briefing(shared_briefing_parts, chart_raw, signal_data,
                         user_report_assets, user_report_indicators,
                         user_context_dict, spotlight_max=5):
    """Build a user-specific briefing by filtering shared data to user's preferences.

    Args:
        shared_briefing_parts: list of strings from _build_shared_briefing_parts()
        chart_raw: dict of {(asset, tf): api_response_dict}
        signal_data: full signal extraction output
        user_report_assets: list of {"asset": "SOL", "timeframes": ["1D", "4H"]}
        user_report_indicators: list of indicator IDs like ["RSI", "MACD"]
        user_context_dict: dict with keys:
            trader_profile, plays_text, alerts, positions,
            yesterday_personal, market_regime, top5_text,
            signal_summary, signal_conflicts
        spotlight_max: maximum spotlight assets

    Returns:
        str: assembled briefing text for the user message
    """
    from .signal_extractor import select_spotlight_assets

    parts = list(shared_briefing_parts)

    # Spotlight assets from user's report_assets
    asset_names = [a.get("asset", "").upper() for a in (user_report_assets or []) if a.get("asset")]
    all_signals = signal_data.get("per_asset", {})
    spotlight, summary = select_spotlight_assets(
        all_signals,
        user_report_assets=asset_names if asset_names else None,
        max_spotlight=spotlight_max,
    )
    compact_text, _ = format_compact_asset_lines(chart_raw, summary)

    parts.append(
        f"## SPOTLIGHT ASSETS: {', '.join(spotlight)}\n"
        f"These are this trader's focused assets. Give them deep narrative analysis.\n"
    )
    if compact_text:
        parts.append(f"## Non-Spotlight Asset Summary\n{compact_text}\n")

    # Indicator focus
    ind_focus = _build_indicator_focus(user_report_indicators, signal_data)
    if ind_focus:
        parts.append(ind_focus)

    # Asset focus (with on-demand fetch for missing assets)
    asset_focus, _ = _build_asset_focus(user_report_assets, chart_raw, signal_data)
    if asset_focus:
        parts.append(asset_focus)

    # Personal context — quarantine all user-controlled fields with XML-tag
    # wrappers + injection-pattern neutralization (see grounding.escape).
    from carrac.grounding import escape_user_text as _escape
    ctx = user_context_dict
    if ctx.get("trader_profile"):
        parts.append(f"## Trader Profile\n{_escape(ctx['trader_profile'], 'trader_profile')}\n")
    if ctx.get("plays_text"):
        # plays_text is pre-built with per-play escaping by engine.py::_personal_plays_block; wrap
        # the whole block to provide a second containment boundary.
        parts.append(f"{_escape(ctx['plays_text'], 'user_plays', max_chars=8000)}\n")
    if ctx.get("alerts"):
        parts.append(f"## Alerts\n{_escape(ctx['alerts'], 'user_alerts', max_chars=8000)}\n")
    if ctx.get("positions"):
        parts.append(f"## Positions\n{_escape(ctx['positions'], 'user_positions', max_chars=8000)}\n")
    if ctx.get("yesterday_personal"):
        parts.append(
            f"## Yesterday's Personal Report\n"
            f"{_escape(ctx['yesterday_personal'][:3000], 'previous_report', max_chars=3200)}\n"
        )
    if ctx.get("quant_refresh_note"):
        # Pre-formatted refresh notice from _ensure_fresh_quant — already
        # contains its own ## heading, no escape (system-generated).
        parts.append(ctx["quant_refresh_note"])

    return "\n".join(p for p in parts if p)


# ----------------------------------------------------------------------
# Week-Ahead briefing data assembly
# ----------------------------------------------------------------------

def assemble_week_ahead_context():
    """Assemble all data needed for the Sunday Week-Ahead shared briefing.

    Pulls: 7-day forward economic calendar and latest scanner verdicts.
    Returns a dict of formatted strings ready for prompt injection.

    A "derivatives regime trajectory" block (Deribit-derived skew/IV/GEX
    snapshots out of the quant DB) is deliberately absent. That data
    walks around the licensing gate through the quant-DB door, and the
    render-time scrub would silently delete the narrated section from
    every Sunday briefing anyway. Removed, not gated: the report type never
    shows a reader a word of it.
    """
    from datetime import datetime as _dt, timedelta, timezone as _tz
    out = {}

    # Catalyst calendar — next 7 days
    try:
        from carrac.db_pool import get_quant_conn as get_conn
        from_d = _dt.now(_tz.utc).strftime('%Y-%m-%d')
        to_d = (_dt.now(_tz.utc) + timedelta(days=7)).strftime('%Y-%m-%d')
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT ts, event_type, title, impact FROM quant.events '
                'WHERE ts BETWEEN %s AND %s ORDER BY ts',
                (from_d, to_d),
            )
            rows = cur.fetchall()
        if rows:
            lines = ["| Date | Event | Type | Impact |", "|------|-------|------|--------|"]
            for r in rows:
                ts = r['ts'].isoformat() if hasattr(r['ts'], 'isoformat') else str(r['ts'])
                lines.append(f"| {ts[:10]} | {r['title']} | {r['event_type']} | {r['impact']} |")
            out['catalyst_calendar'] = "\n".join(lines)
        else:
            out['catalyst_calendar'] = "_No scheduled events in the next 7 days._"
    except Exception as e:
        out['catalyst_calendar'] = f"_Calendar unavailable: {e}_"

    # Scanner snapshot — top setups across chart_assets
    try:
        from carrac.caches import get_cached_scanner
        from carrac.assets.registry import chart_assets
        focus = set(chart_assets())
        scan = get_cached_scanner() or {}
        results = scan.get('results') or []
        approaching = []
        for r in results:
            sym = r.get('symbol')
            if sym not in focus:
                continue
            best_v = r.get('best_verdict') or ''
            long_t = (r.get('long') or {}).get('total') or 0
            short_t = (r.get('short') or {}).get('total') or 0
            if best_v in ('STRONG_LONG', 'STRONG_SHORT') or max(long_t, short_t) >= 70:
                approaching.append(
                    f"- {sym}: {best_v} (long={long_t}/short={short_t}) @ ${r.get('price')}"
                )
        if approaching:
            out['setup_watch'] = "\n".join(approaching[:15])
        else:
            out['setup_watch'] = "_No high-conviction setups approaching trigger._"
    except Exception as e:
        out['setup_watch'] = f"_Scanner unavailable: {e}_"

    out['_fetched_at'] = _dt.now(_tz.utc).isoformat()
    return out


def build_week_ahead_shared_briefing(week_ctx):
    """Compose the shared (non-personalized) Week-Ahead briefing parts."""
    # COT positioning is weekly-cadence data — inject into Week-Ahead so the
    # Week Setup and Macro Themes sections can reference stretched readings.
    cot_text = fetch_cot()
    parts = [
        f"# Week-Ahead Briefing — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n",
        f"## Catalyst Calendar (next 7 days)\n{week_ctx.get('catalyst_calendar', '')}\n",
        f"## Setup Watch (scanner-approaching triggers)\n{week_ctx.get('setup_watch', '')}\n",
        # No derivatives section (licensing gate), see assemble_week_ahead_context.
        (f"## Smart-Money Positioning (CFTC COT)\n{cot_text}\n" if cot_text else ""),
    ]
    return [p for p in parts if p]


# --------------------------------------------------------------------------
# Daily continuity data assembly
# --------------------------------------------------------------------------

def fetch_signal_flips():
    """Fetch signal flips from /api/report/diff and format as compact text.

    Returns a list of formatted flip strings like:
      "[HIGH] BTC: 1D signal flip (bullish)"
    Returns empty list if no changes or fetch fails.
    """
    try:
        data = _api('/api/report/diff', timeout=10, retries=1)
        changes = data.get('changes', []) if isinstance(data, dict) else []
        if not changes:
            return []
        lines = []
        for c in changes:
            sig = c.get('significance', 'medium').upper()
            sym = c.get('symbol', '?')
            ctype = c.get('change_type', '?')
            lines.append(f"[{sig}] {sym}: {ctype}")
        return lines
    except Exception as e:
        log.debug("fetch_signal_flips failed: %s", e)
        return []


def fetch_yesterday_regime(quant_context):
    """Extract yesterday's regime from today's quant snapshot history endpoint.

    The quant snapshot already contains the current regime. To get yesterday's
    we hit /api/quant/regime-history?days=2 which returns the last 2 regime
    entries — much cheaper than re-parsing a stored report.

    Returns a dict: {state, probability, since} or None on failure.
    """
    try:
        history = _api('/api/quant/regime-history?days=2', timeout=8, retries=1)
        if not isinstance(history, list) or len(history) < 2:
            return None
        # history is ordered desc (newest first); index 1 = yesterday
        yesterday_entry = history[1]
        return {
            'state': yesterday_entry.get('state') or yesterday_entry.get('regime'),
            'probability': yesterday_entry.get('probability') or yesterday_entry.get('confidence'),
            'since': yesterday_entry.get('since') or yesterday_entry.get('ts'),
        }
    except Exception as e:
        log.debug("fetch_yesterday_regime failed: %s", e)
        return None


def fetch_yesterday_events_with_actuals():
    """Fetch yesterday's macro events with FRED-filled actuals.

    Uses the existing /api/quant/events endpoint with from=yesterday&to=yesterday.
    Returns a list of event dicts with keys: title, ts, actual, impact.
    Only returns events that have a non-null actual value.
    """
    try:
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        events = _api(f'/api/quant/events?from={yesterday}&to={yesterday}', timeout=10, retries=1)
        if not isinstance(events, list):
            return []
        # Only keep events with actuals filled in
        with_actuals = [e for e in events if e.get('actual') is not None]
        return with_actuals
    except Exception as e:
        log.debug("fetch_yesterday_events_with_actuals failed: %s", e)
        return []


def fetch_yesterday_report(report_type='general'):
    """Fetch yesterday's report of the given type from the DB.

    Uses /api/report/morning/recent?type=<t>&limit=2 to grab recent reports and
    pick the one from yesterday. Returns the report dict or None.

    Two invariants:

    1. The default is 'general', the live daily type, matching
       carrac/reports/prior_day.py's ordering. The 'morning' type no longer
       writes, so defaulting to it would never match the date filter below and
       the function would return None on every call, leaving the Since
       Yesterday self-grade silently empty in run_morning AND both
       run_dispatch paths.

    2. The requested report_type is forwarded to the endpoint. The endpoint's
       DEFAULT type is 'morning' (see get_recent_morning_reports), so an
       unforwarded parameter would never fetch a general report.

    Returning None stays the honest answer for "yesterday produced nothing" —
    format_since_yesterday_payload() renders that as an absence rather than
    substituting an older report.
    """
    try:
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

        recent = _api(f'/api/report/morning/recent?type={report_type}&limit=2',
                      timeout=8, retries=1)

        if not isinstance(recent, list):
            return None

        # Find yesterday's report (created_at starts with yesterday_str)
        for r in recent:
            created = str(r.get('created_at', ''))[:10]
            if created == yesterday_str:
                return r

        # If exactly 1 report and it's today's, the second must be yesterday's
        # but wasn't returned; return None to signal "no yesterday report".
        return None
    except Exception as e:
        log.debug("fetch_yesterday_report failed for %s: %s", report_type, e)
        return None


def fetch_todays_general_report():
    """Fetch TODAY's (UTC) 'general' report.

    report_type='morning' no longer generates (ENABLE_MORNING_REPORT is
    false); 'general' is the live morning-equivalent read. run_afternoon
    needs THIS SAME-DAY report — not "yesterday's", and not whatever the
    most-recent 'morning' row happens to be (which would be silently
    months stale via get_latest_report(report_type='morning') with no date
    filter — see carrac/reports/routes.py _fetch_yesterday()).

    Returns the report dict (with 'content', 'created_at', 'id') or None
    when today's general report hasn't been saved yet (afternoon running
    ahead of morning, or morning failed outright) — callers must treat that
    as "no morning ground truth available" rather than fabricating one.
    """
    try:
        today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        recent = _api('/api/report/morning/recent?type=general&limit=1', timeout=8, retries=1)
        if not isinstance(recent, list) or not recent:
            return None
        report = recent[0]
        created = str(report.get('created_at', ''))[:10]
        if created != today_str:
            return None
        return report
    except Exception as e:
        log.debug("fetch_todays_general_report failed: %s", e)
        return None


def format_since_yesterday_payload(signal_flips, yesterday_regime, quant_context,
                                    yesterday_report=None, chart_raw=None):
    """Assemble the data payload for the Since Yesterday section.

    Returns a dict with all the structured data the LLM prompt needs:
      - has_data: bool — False if nothing meaningful is available
      - flips_text: formatted signal flip lines
      - regime_change: dict or None (today vs yesterday)
      - yesterday_report_excerpt: relevant section text from yesterday
      - today_prices: dict of current prices for grading

    Designed to be serialised as a flat text block for prompt injection.
    """
    payload = {'has_data': False}

    # --- Signal flips ---
    if signal_flips:
        payload['flips_text'] = '\n'.join(f'  {f}' for f in signal_flips[:8])
        payload['has_data'] = True
    else:
        payload['flips_text'] = '  (no significant flips since yesterday)'

    # --- Regime change ---
    today_regime = None
    quant_text = quant_context.get('quant', '') if quant_context else ''
    # Parse today's regime from the already-fetched quant text
    m = re.search(r'Regime: (\w+) \((\d+)%', quant_text)
    if m:
        today_regime = {'state': m.group(1).upper(), 'probability': int(m.group(2))}

    if yesterday_regime and today_regime:
        yday_state = str(yesterday_regime.get('state', '')).upper()
        today_state = today_regime['state']
        if yday_state and yday_state != today_state:
            payload['regime_change'] = (
                f"REGIME FLIP: {yday_state} → {today_state} "
                f"(today {today_regime['probability']}% confidence)"
            )
            payload['has_data'] = True
        else:
            payload['regime_change'] = (
                f"Regime unchanged: {today_state} "
                f"({today_regime['probability']}% confidence; "
                f"was {yesterday_regime.get('probability', '?')}% yesterday)"
            )
    else:
        payload['regime_change'] = 'Regime history unavailable.'

    # --- Yesterday report excerpt for self-grading ---
    if yesterday_report:
        content = yesterday_report.get('content', '') or ''
        _raw_dt = yesterday_report.get('created_at') or yesterday_report.get('generation_date')
        if _raw_dt:
            try:
                from datetime import datetime as _dt
                _d = _dt.fromisoformat(str(_raw_dt).replace('Z', '+00:00'))
                payload['yesterday_date'] = f"{_d.strftime('%Y-%m-%d')} ({_d.strftime('%A')})"
            except (ValueError, TypeError):
                payload['yesterday_date'] = str(_raw_dt)[:10]
        # Extract the most relevant section for grading:
        # For morning: Scenarios section captures the main calls.
        # For general: TL;DR captures the main thesis.
        excerpt = ''
        for section_name in ('Scenarios', 'TL;DR', 'The Story'):
            # Simple section extraction: find "## Section" and grab until next "## "
            pattern = re.compile(
                rf'^## {re.escape(section_name)}\s*\n(.*?)(?=^## |\Z)',
                re.MULTILINE | re.DOTALL | re.IGNORECASE,
            )
            sm = pattern.search(content)
            if sm:
                excerpt = sm.group(1).strip()[:600]
                break
        if excerpt:
            payload['yesterday_excerpt'] = excerpt
            payload['has_data'] = True
        else:
            payload['yesterday_excerpt'] = ''
    else:
        payload['yesterday_excerpt'] = ''

    # --- Today's closing prices for self-grading ---
    today_prices = {}
    if chart_raw:
        for (asset, tf), data in chart_raw.items():
            if tf == '1D':
                ohlcv = data.get('ohlcv', [])
                if ohlcv:
                    today_prices[asset] = ohlcv[-1].get('close')
    payload['today_prices'] = today_prices

    return payload


def format_since_yesterday_block(payload):
    """Render the since-yesterday payload as a prompt-injection text block.

    Returns a string ready to be inserted into the LLM briefing.
    Returns empty string if payload has no meaningful data.
    """
    if not payload.get('has_data'):
        return ''

    lines = ['## Since Yesterday (Daily Continuity)']
    lines.append('')

    if payload.get('yesterday_date'):
        lines.append(f"### Prior report: {payload['yesterday_date']}")
        lines.append('')

    lines.append('### Signal Flips Since Last Report')
    lines.append(payload.get('flips_text', '  (none)'))
    lines.append('')

    lines.append('### Regime')
    lines.append(f"  {payload.get('regime_change', 'Unavailable.')}")
    lines.append('')

    if payload.get('yesterday_excerpt'):
        lines.append("### Yesterday's Main Call (for self-grading)")
        lines.append(payload['yesterday_excerpt'])
        lines.append('')

    if payload.get('today_prices'):
        price_parts = []
        for asset, price in list(payload['today_prices'].items())[:6]:
            if price is not None:
                price_parts.append(f"{asset}=${price:,.2f}")
        if price_parts:
            lines.append(f"### Today's Prices: {', '.join(price_parts)}")
            lines.append('')

    return '\n'.join(lines)


