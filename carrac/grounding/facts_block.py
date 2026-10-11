"""Ground-truth facts block — the authoritative current-market snapshot
injected into every LLM system prompt.

Split TTL strategy:
- Prices (BTC, SPX, DXY): 5 min — they move intraday
- Macro (Fed funds, CPI, yields from FRED): 1 h — FRED collector is daily
- FOMC calendar: 24 h — static between meetings

Data is pulled from already-cached sources (quant.macro Postgres table,
/api/chart-internal Redis cache, data_sources.fred in-memory). A full
build typically completes in <50 ms warm, <200 ms cold. Never issues
external HTTP calls on the hot path — any miss falls back to stale data
and marks the corresponding field as 'unavailable'.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

log = logging.getLogger("carrac.grounding.facts")

# ----- TTLs (seconds) ---------------------------------------------------
_TTL_PRICE = 300     # 5 min
_TTL_MACRO = 3600    # 1 h
_TTL_CALENDAR = 86400  # 24 h

# ----- Process-wide cache ----------------------------------------------
_cache_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}


def _cache_get(key: str, ttl: int) -> Optional[Any]:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["fetched_at"]) < ttl:
            return entry["value"]
    return None


def _cache_set(key: str, value: Any) -> None:
    with _cache_lock:
        _cache[key] = {"value": value, "fetched_at": time.time()}


# ----- Individual collectors -------------------------------------------

def _fetch_macro() -> Dict[str, Any]:
    """Pull FRED snapshot (Fed funds, yields) from the already-cached client."""
    cached = _cache_get("macro", _TTL_MACRO)
    if cached is not None:
        return cached

    out: Dict[str, Any] = {}
    try:
        from data_sources.fred import get_fred_client  # late import avoids app-start cost
        fred = get_fred_client()
        ff = fred.get_fed_funds() or {}
        y10 = fred.get_yield_10y() or {}
        spread = fred.get_yield_spread() or {}
        dxy = fred.get_dxy() or {}
        if ff.get("value") is not None:
            out["fed_funds_effective_pct"] = ff["value"]
            out["fed_funds_as_of"] = ff.get("date")
        if y10.get("value") is not None:
            out["yield_10y_pct"] = y10["value"]
        if spread.get("value") is not None:
            out["yield_10y_minus_2y"] = spread["value"]
        if dxy.get("value") is not None:
            out["dxy_broad"] = dxy["value"]
    except Exception as e:  # network off, FRED down, etc.
        log.warning("FRED fetch failed for facts block: %s", e)

    _cache_set("macro", out)
    return out


def _fetch_quant_macro() -> Dict[str, Any]:
    """Pull latest FED_FUNDS + FOMC calendar from the local quant Postgres.

    Prefer the quant DB — nightly collector keeps it fresh and it survives
    FRED outages.
    """
    cached = _cache_get("quant_macro", _TTL_MACRO)
    if cached is not None:
        return cached

    out: Dict[str, Any] = {}
    try:
        import psycopg2  # type: ignore
        url = os.environ.get("QUANT_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            _cache_set("quant_macro", out)
            return out

        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                # Try quant schema first, then public
                for schema in ("quant", "public"):
                    try:
                        cur.execute(
                            f"SELECT metric, value, ts FROM {schema}.macro "
                            "WHERE metric IN ('FED_FUNDS','YIELD_10Y','YIELD_2Y','CPI','DXY') "
                            "AND ts >= (NOW() - INTERVAL '60 days') "
                            "ORDER BY ts DESC"
                        )
                        rows = cur.fetchall()
                        if rows:
                            # Keep only latest per metric
                            seen = set()
                            for metric, value, ts in rows:
                                if metric in seen:
                                    continue
                                seen.add(metric)
                                key = metric.lower() + "_latest"
                                out[key] = float(value)
                                out[metric.lower() + "_as_of"] = ts.strftime("%Y-%m-%d") if ts else None

                            # 'CPI' above is the raw CPIAUCSL INDEX
                            # LEVEL (~333), not a YoY %. Compute the actual YoY
                            # delta from the observation ~12 months prior (same
                            # "offset 12 monthly obs" convention as
                            # quant.collectors.event_calendar._fmt_cpi) and
                            # expose it as a SEPARATE field so cpi_latest keeps
                            # its raw-level meaning for other callers.
                            if "cpi_latest" in out:
                                try:
                                    cur.execute(
                                        f"SELECT value FROM {schema}.macro "
                                        "WHERE metric = 'CPI' "
                                        "ORDER BY ts DESC LIMIT 1 OFFSET 12"
                                    )
                                    prior = cur.fetchone()
                                    if prior and prior[0]:
                                        prior_val = float(prior[0])
                                        if prior_val:
                                            out["cpi_yoy"] = (out["cpi_latest"] / prior_val - 1) * 100
                                except psycopg2.Error:
                                    conn.rollback()
                            break
                    except psycopg2.Error:
                        conn.rollback()
                        continue
    except Exception as e:
        log.debug("Quant macro fetch failed (non-fatal): %s", e)

    _cache_set("quant_macro", out)
    return out


def _fetch_fomc() -> Dict[str, Any]:
    """Find the next FOMC meeting date."""
    cached = _cache_get("fomc", _TTL_CALENDAR)
    if cached is not None:
        return cached

    out: Dict[str, Any] = {"next_fomc": None, "last_fomc": None}
    try:
        import psycopg2  # type: ignore
        url = os.environ.get("QUANT_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if url:
            with psycopg2.connect(url) as conn:
                with conn.cursor() as cur:
                    for schema in ("quant", "public"):
                        try:
                            cur.execute(
                                f"SELECT ts FROM {schema}.events "
                                "WHERE event_type='FOMC' AND ts >= NOW() "
                                "ORDER BY ts ASC LIMIT 1"
                            )
                            row = cur.fetchone()
                            if row:
                                out["next_fomc"] = row[0].strftime("%Y-%m-%d")
                            cur.execute(
                                f"SELECT ts FROM {schema}.events "
                                "WHERE event_type='FOMC' AND ts < NOW() "
                                "ORDER BY ts DESC LIMIT 1"
                            )
                            row = cur.fetchone()
                            if row:
                                out["last_fomc"] = row[0].strftime("%Y-%m-%d")
                            break
                        except psycopg2.Error:
                            conn.rollback()
                            continue
    except Exception as e:
        log.debug("FOMC calendar fetch failed (non-fatal): %s", e)

    _cache_set("fomc", out)
    return out


def _fetch_prices() -> Dict[str, Any]:
    """Pull latest close for BTC, SPX, DXY from chart-internal cache."""
    cached = _cache_get("prices", _TTL_PRICE)
    if cached is not None:
        return cached

    out: Dict[str, Any] = {}
    try:
        import requests  # already in requirements
        base = os.environ.get("DASHBOARD_BASE_URL", "http://localhost:5000")
        api_key = os.environ.get("DASHBOARD_API_KEY", "")
        headers = {"X-Internal-Key": api_key} if api_key else {}
        for sym, key in (("BTC", "btc_usd"), ("SPX", "spx"), ("DXY", "dxy_spot")):
            try:
                r = requests.get(
                    f"{base}/api/chart-internal/{sym}?timeframe=1D",
                    headers=headers, timeout=5,
                )
                if r.ok:
                    j = r.json()
                    bars = j.get("candles") or j.get("data") or []
                    if bars:
                        last = bars[-1]
                        close = last.get("close") or last.get("c")
                        if close is not None:
                            out[key] = float(close)
            except Exception:
                continue
    except Exception as e:
        log.debug("Price fetch failed for facts block: %s", e)

    _cache_set("prices", out)
    return out


def _fetch_dxy_spot() -> Dict[str, Any]:
    """Real ICE DXY (~99) from the platform's OHLCV DB — the value shown on the
    charts and in the morning report.

    DISTINCT from ``dxy_broad`` (FRED ``DTWEXBGS``, the Fed broad trade-weighted
    index, ~118, base 2006=100). The two were historically conflated under the
    label "DXY"; this collector provides the ICE index so the facts block can
    report the same DXY users see on the platform.

    Sourced from in-process services (available in the web/MCP app context, the
    one that serves ``carrac://facts/current``). Returns ``{}`` when services
    aren't initialized (e.g. the standalone morning_engine process) or the
    symbol is missing — a graceful no-op, never an exception on the hot path.
    """
    cached = _cache_get("dxy_spot", _TTL_PRICE)
    if cached is not None:
        return cached

    out: Dict[str, Any] = {}
    try:
        from carrac.services import get_services
        df = get_services().db.get_ohlcv_data("DXY", limit=1)
        if df is not None and len(df):
            out["dxy_spot"] = float(df["close"].iloc[-1])
    except Exception as e:
        log.debug("DXY spot fetch failed (non-fatal): %s", e)

    _cache_set("dxy_spot", out)
    return out


# ----- Public API -------------------------------------------------------

def build_facts_dict(*, include_prices: bool = True) -> Dict[str, Any]:
    """Return a structured facts dict. Used by hallucination_scan to
    cross-check regex matches against authoritative values.
    """
    now = datetime.now(timezone.utc)
    facts: Dict[str, Any] = {
        "today": now.strftime("%Y-%m-%d"),
        "today_weekday": now.strftime("%A"),
        "now_utc_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    facts.update(_fetch_quant_macro())
    fred = _fetch_macro()
    # quant DB takes precedence, FRED fills gaps
    for k, v in fred.items():
        facts.setdefault(k, v)
    facts.update(_fetch_fomc())
    if include_prices:
        facts.update(_fetch_prices())
        # Real ICE DXY (~99) — overrides any stale chart-internal dxy_spot and
        # is reported as "DXY" instead of the broad index (dxy_broad).
        facts.update(_fetch_dxy_spot())
    return facts


def _fmt_rate(val: Optional[float]) -> str:
    if val is None:
        return "unavailable"
    return f"{val:.2f}%"


def _fmt_range_from_effective(eff: Optional[float]) -> str:
    """DFF is the effective rate; the target range is typically eff±0.125%
    but we display the canonical 25bp band around the nearest 0.25 grid.
    """
    if eff is None:
        return "unavailable"
    # Round effective to nearest 0.125, then target range is [x-0.125, x+0.125]
    # For display, pick the 25bp band that contains eff.
    lower = round((eff - 0.125) * 4) / 4
    upper = lower + 0.25
    return f"{lower:.2f}–{upper:.2f}%"


def _yield_curve(f: Dict[str, Any]) -> tuple:
    """Return ``(y10, y2, spread)`` where the spread ALWAYS closes on the
    two yields as they will be PRINTED.

    The three values arrive from different feeds with independent as-of
    dates: both yields come from the quant ``macro`` table
    (``_fetch_quant_macro``, latest row per metric within 60 days), while
    the spread came from FRED's separately-published T10Y2Y series
    (``_fetch_macro``). Rendering them side by side produced arithmetic
    that did not close — the displayed spread could not be derived from the
    displayed yields, and it tended to match the NEXT trading day's spread.

    So whenever both yields are present the spread is derived from them.
    It is derived from the ROUNDED values deliberately: the reader checks
    the subtraction against the two 2dp figures on the page, so that is the
    subtraction that has to be right. FRED's T10Y2Y is used only as a
    fallback when a yield is missing and there is nothing to derive from —
    in which case no yield pair is shown beside it to contradict it.

    Shared by facts_block() and render_macro_facts_strip() so the two
    cannot drift apart.
    """
    y10 = f.get("yield_10y_latest") or f.get("yield_10y_pct")
    y2 = f.get("yield_2y_latest")
    if y10 is not None and y2 is not None:
        return y10, y2, round(round(y10, 2) - round(y2, 2), 2)
    return y10, y2, f.get("yield_10y_minus_2y")


def facts_block(*, include_prices: bool = True, facts: Optional[Dict[str, Any]] = None) -> str:
    """Return the human-readable facts block to prepend to LLM prompts.

    If GROUNDING_ENABLED=0, returns an empty string (backward-compat bypass).
    If the underlying data lookups fail entirely, still returns the date
    and a note that macro data is unavailable so the model knows not to
    substitute from memory.
    """
    if os.environ.get("GROUNDING_ENABLED", "1") == "0":
        return ""

    f = facts if facts is not None else build_facts_dict(include_prices=include_prices)

    lines = [
        "=== CURRENT MARKET FACTS (verified live from platform data) ===",
        f"Today's date: {f['today']} ({f['today_weekday']})",
    ]

    ff = f.get("fed_funds_latest") or f.get("fed_funds_effective_pct")
    ff_date = f.get("fed_funds_as_of")
    if ff is not None:
        lines.append(f"Fed funds effective rate: {ff:.2f}% (target range ≈ {_fmt_range_from_effective(ff)}) — as of {ff_date or 'latest available'}")
    else:
        lines.append("Fed funds rate: UNAVAILABLE — do not state a value. Acknowledge uncertainty.")

    next_fomc = f.get("next_fomc")
    last_fomc = f.get("last_fomc")
    if next_fomc:
        lines.append(f"Next FOMC meeting: {next_fomc} (HAS NOT YET OCCURRED — do not state any decision)")
    if last_fomc:
        lines.append(f"Last FOMC meeting: {last_fomc}")

    y10, y2, spread = _yield_curve(f)
    if y10 is not None:
        lines.append(f"10Y Treasury yield: {y10:.2f}%")
    if y2 is not None:
        lines.append(f"2Y Treasury yield: {y2:.2f}%")
    if spread is not None:
        lines.append(f"10Y-2Y spread: {spread:+.2f}%")

    cpi_yoy = f.get("cpi_yoy")
    if cpi_yoy is not None:
        lines.append(f"CPI YoY (latest print): {cpi_yoy:.2f}%")

    # ICE DXY (~99) — the dollar index shown on the charts and in the report;
    # reported as "DXY". The FRED broad trade-weighted index (dxy_broad /
    # dxy_latest = DTWEXBGS, ~118, base 2006=100) is a DIFFERENT index and is
    # NEVER reported under the "DXY" label — that conflation made the facts
    # block contradict the platform's own DXY.
    dxy = f.get("dxy_spot")
    if dxy is not None:
        lines.append(f"DXY (ICE U.S. Dollar Index): {dxy:.2f}")
    usd_broad = f.get("dxy_broad") or f.get("dxy_latest")
    if usd_broad is not None:
        lines.append(f"Broad trade-weighted USD index (Fed, 2006=100): {usd_broad:.2f}")

    if include_prices:
        btc = f.get("btc_usd")
        spx = f.get("spx")
        if btc is not None:
            lines.append(f"Bitcoin: ${btc:,.0f}")
        if spx is not None:
            lines.append(f"S&P 500: {spx:,.0f}")

    lines.append("=== END OF FACTS BLOCK ===")
    return "\n".join(lines)


def render_macro_facts_strip(facts: Optional[Dict[str, Any]] = None) -> str:
    """User-facing Macro Facts strip for the free report.

    Unlike facts_block() (which carries LLM-only scaffolding such as
    "do not state a value"), this renders a clean, human-readable strip from
    the SAME authoritative build_facts_dict() data. Deterministic — never
    generated by a model. Any missing field is omitted (a gap is shown as
    absence, never a guess).
    """
    f = facts if facts is not None else build_facts_dict(include_prices=True)
    if not f:
        return ""

    bits = []

    ff = f.get("fed_funds_latest") or f.get("fed_funds_effective_pct")
    if ff is not None:
        bits.append(f"Fed funds {ff:.2f}% (target {_fmt_range_from_effective(ff)})")

    next_fomc = f.get("next_fomc")
    if next_fomc:
        try:
            d0 = datetime.strptime(f.get("today", ""), "%Y-%m-%d").date()
            d1 = datetime.strptime(next_fomc, "%Y-%m-%d").date()
            days = (d1 - d0).days
            bits.append(f"next FOMC in {days}d" if days >= 0 else f"next FOMC {next_fomc}")
        except (ValueError, TypeError):
            bits.append(f"next FOMC {next_fomc}")

    y10, y2, spread = _yield_curve(f)
    if y10 is not None and y2 is not None:
        s = f" (spread {spread:+.2f})" if spread is not None else ""
        bits.append(f"10Y {y10:.2f}% / 2Y {y2:.2f}%{s}")
    elif y10 is not None:
        bits.append(f"10Y {y10:.2f}%")

    dxy = f.get("dxy_spot")
    if dxy is not None:
        bits.append(f"DXY {dxy:.2f}")

    cpi_yoy = f.get("cpi_yoy")
    if cpi_yoy is not None:
        bits.append(f"last CPI {cpi_yoy:.2f}% YoY")

    btc = f.get("btc_usd")
    if btc is not None:
        bits.append(f"BTC ${btc:,.0f}")
    spx = f.get("spx")
    if spx is not None:
        bits.append(f"SPX {spx:,.0f}")

    if not bits:
        return ""

    as_of = f.get("today", "")
    header = "**Macro facts**" + (f" · as of {as_of}" if as_of else "")
    return f"{header}\n" + " · ".join(bits) + "\n_source: FRED / platform data_"
