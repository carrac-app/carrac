"""Deterministic per-asset base-rate history ("History says") for spotlight
cards, decided as Option A.

WHY THIS IS ITS OWN ENGINE PATH (not query_conditional): see
carrac/facts/bands.py's module docstring.
``quant/engine/conditional.py::query_conditional`` only resolves
``quant.zscores`` metrics (macro/sentiment/derivatives_*/onchain/price_vol
domains) — it has NO per-asset RSI z-score and NO per-asset range-percentile
z-score for any asset, so the planned condition vocabulary (RSI zone,
range-percentile quartile) cannot be expressed through it. This module
instead computes BOTH conditions itself, directly from ``quant.price_daily``
daily closes, in pure Python — no LLM anywhere, same bulletproof discipline
as ``carrac/facts/bands.py`` and ``carrac/facts/event_study.py`` (this
module's sibling for the Docket's own-history line).

SELF-CONSISTENCY RULE: "today's condition" and every historical day's
condition are derived from the exact same single computed RSI/percentile
series (one pass over one continuous close-price array) — never from the
app-layer facts bundle (``bundle['rsi']['zone']`` / ``bundle['range_pctile']
['pct']``, computed in ``carrac/precompute.py`` + ``carrac/indicator_eval.py``
+ ``morning_engine/ta_facts.py`` on a possibly-stale precompute cycle).
Mixing "today" from one pipeline with "history" from another would risk
comparing apples to oranges (subtly different RSI/percentile math or
staleness) — recomputing both ends from the same array rules that out by
construction.

REUSE, NOT REINVENT (to avoid drift onto a second definition of the same
thing):
  - asset/source resolution: ``quant.engine.config.TARGET_ASSETS`` — the
    SAME map ``carrac/facts/event_study.py`` and the cluster-spotlight
    builder (``carrac/reports/routes.py``) already use for price_daily
    lookups. An asset outside this map returns None without touching the DB.
  - RSI-14: ``consensio.indicators.rsi.RSICalculator`` — the canonical
    Wilder RSI implementation already used by the chart engine and pinned by
    ``tests/test_consensio_golden.py``. Not reimplemented here.
  - RSI zone thresholds (oversold <30 / overbought >70): ``carrac.
    indicator_eval.rsi_zone`` — the single source of truth for that
    classification (its own docstring: "adding a new indicator's
    classification logic here automatically makes it available to BOTH
    evaluators").
  - Range-percentile RULE (window=90, latest excluded from its own
    comparison window, ``count_le / window * 100`` rounded, clamped 0..100):
    the exact rule ``morning_engine/ta_facts.py::price_percentile`` already
    implements (same rule that produces ``bundle['range_pctile']['pct']``).
    Reimplemented here as a SERIES (one value per day, not just the latest)
    for performance — a naive "call price_percentile() once per prefix"
    would be O(n²) over a ~9,000-row Yahoo-backed series (SPX/GOLD/DXY back
    to 2000) — but ``tests/test_base_rates.py`` pins parity against
    ``price_percentile()`` directly so the two can never quietly diverge.

CONTRACT (already shipped, guarded, in the template — see
``templates/partials/_facts_modules.html``'s ``.histline`` and
``tests/test_report_editorial_template.py``'s history_says tests):
    {"n": int, "horizon_days": 7, "median_move_pct": float,
     "condition_label": str} | None

Two condition templates ONLY, per the plan:
  1. RSI zone match — "defined" only at the extremes (oversold/overbought);
     a neutral RSI is NOT a condition (matching a generic ~40% of history
     tells a reader nothing distinctive).
  2. Range-percentile quartile match — "defined" whenever the percentile
     itself is computable (>= 91 daily closes), regardless of which
     quartile — this is what makes the headline line free-by-design (it
     renders for almost every spotlight card, not just at RSI extremes).
Combine both when both are defined for the asset today (AND — a historical
day must match BOTH to count); otherwise use whichever one is defined. When
NEITHER is defined (insufficient depth AND a neutral RSI), the card gets no
line — never an estimate.

The last ``horizon_days`` days are excluded from the match pool because they
have no computable +N-trading-day forward move yet — this falls out
naturally from the eligibility bound (`i + horizon_days <= n - 1`), no
separate exclusion step needed. ``median_move_pct`` is SIGNED (not absolute
— unlike ``event_study.py``'s "median absolute next-day move"), matching the
shipped template's ``"%+.1f"`` formatting. n < 25 -> None, never an estimate.
Never raises.

Public API:
    history_says(symbol, min_n=25, horizon_days=7, conn=None, today=None)
        -> dict | None   — live compute, no caching.
    get_history_says(symbol, today=None, conn=None, r=None)
        -> dict | None   — Redis-cached wrapper (key ``histsays:{sym}:
        {date}``, TTL 6h) — this is the one ``carrac/reports/routes.py``
        calls, so the cluster-spotlight gather doesn't recompute the full
        RSI/percentile series against quant.price_daily on every /brief hit
        within the same day (page itself is cached 600s; this covers the
        cross-request, cross-day-boundary case).
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

try:
    from carrac.db_pool import get_quant_conn  # noqa: F401
except ImportError:  # pragma: no cover — quant not installed in some envs
    get_quant_conn = None  # type: ignore[assignment]

# asset -> (price_daily asset symbol, source) — reused from the conditional
# engine's own target-asset map, same as carrac/facts/event_study.py, so
# this module can never drift onto a different asset/source pairing.
try:
    from quant.engine.config import TARGET_ASSETS as _TARGET_ASSETS
except ImportError:  # pragma: no cover — quant not installed in some envs
    _TARGET_ASSETS = {}

from carrac.indicator_eval import rsi_zone
from consensio.indicators.rsi import RSICalculator

_RSI_PERIOD = 14
_PCTILE_WINDOW = 90
_CACHE_TTL_SECONDS = 6 * 3600

_QUARTILE_LABELS = {
    1: "bottom-quartile range",
    2: "lower-mid quartile range",
    3: "upper-mid quartile range",
    4: "top-quartile range",
}
_ZONE_LABELS = {
    "oversold": "RSI oversold",
    "overbought": "RSI overbought",
}


# ══════════════════════════════════════════════════════════════════════════
# PURE helpers
# ══════════════════════════════════════════════════════════════════════════

def _rsi_series(closes: list[float]) -> list[Optional[float]]:
    """PURE. Wilder RSI-14 for every index (None during warm-up), via the
    canonical ``consensio.indicators.rsi.RSICalculator`` — not reimplemented
    here."""
    n = len(closes)
    if n < _RSI_PERIOD + 1:
        return [None] * n
    df = pd.DataFrame({"close": closes})
    series = RSICalculator(period=_RSI_PERIOD).calculate_series(df)
    return [None if pd.isna(v) else float(v) for v in series]


def _range_pctile_series(closes: list[float], window: int = _PCTILE_WINDOW) -> list[Optional[int]]:
    """PURE. Series form of ``morning_engine.ta_facts.price_percentile``'s
    rule: index i's percentile rank of ``closes[i]`` against the PRECEDING
    ``window`` closes (``closes[i]`` itself excluded from its own comparison
    window) — ``count_le / window * 100``, rounded, clamped 0..100. None
    where fewer than ``window`` preceding closes exist. Same rule as
    ``price_percentile()``, computed for every index in one pass instead of
    one value at a time (parity pinned in tests/test_base_rates.py)."""
    n = len(closes)
    out: list[Optional[int]] = [None] * n
    for i in range(window, n):
        window_vals = closes[i - window:i]
        latest = closes[i]
        count_le = sum(1 for v in window_vals if v <= latest)
        pct = count_le / window * 100
        out[i] = max(0, min(100, round(pct)))
    return out


def _quartile(pct: Optional[int]) -> Optional[int]:
    """PURE. Bucket a 0..100 percentile into quartile 1 (bottom, [0,25)) ..
    quartile 4 (top, [75,100]). None passes through as None."""
    if pct is None:
        return None
    if pct < 25:
        return 1
    if pct < 50:
        return 2
    if pct < 75:
        return 3
    return 4


def _condition_label(zone: Optional[str], quartile: Optional[int]) -> Optional[str]:
    """PURE. 'RSI oversold + bottom-quartile range' when both are defined;
    just the one that is when only one is; None when neither is."""
    zone_label = _ZONE_LABELS.get(zone) if zone else None
    quartile_label = _QUARTILE_LABELS.get(quartile) if quartile else None
    if zone_label and quartile_label:
        return f"{zone_label} + {quartile_label}"
    return zone_label or quartile_label


def _compute_history_says(closes: list[float], min_n: int = 25,
                           horizon_days: int = 7) -> Optional[dict]:
    """PURE. See module docstring for the full contract. ``closes``: daily
    closes, oldest -> newest, no gaps expected (caller filters NULLs at the
    SQL layer). The LAST element is "today"."""
    n = len(closes)
    if n < horizon_days + 1:
        return None

    rsi_raw = _rsi_series(closes)
    rsi_cond = [rsi_zone(v) for v in rsi_raw]
    # Only the extremes count as a "defined" RSI condition — a neutral RSI
    # isn't a distinctive condition to base-rate against (see docstring).
    rsi_cond = [z if z in ("oversold", "overbought") else None for z in rsi_cond]

    pct_series = _range_pctile_series(closes)
    quart_series = [_quartile(p) for p in pct_series]

    today_idx = n - 1
    today_zone = rsi_cond[today_idx]
    today_quartile = quart_series[today_idx]

    label = _condition_label(today_zone, today_quartile)
    if label is None:
        return None  # neither condition defined today -> no line

    moves: list[float] = []
    last_eligible = n - 1 - horizon_days  # needs closes[i + horizon_days] to exist
    for i in range(0, last_eligible + 1):
        if today_zone is not None and rsi_cond[i] != today_zone:
            continue
        if today_quartile is not None and quart_series[i] != today_quartile:
            continue
        base = closes[i]
        if not base:
            continue
        fwd = closes[i + horizon_days]
        moves.append((fwd - base) / base * 100)

    if len(moves) < min_n:
        return None

    return {
        "n": len(moves),
        "horizon_days": horizon_days,
        "median_move_pct": round(statistics.median(moves), 1),
        "condition_label": label,
    }


# ══════════════════════════════════════════════════════════════════════════
# DB reader + public API
# ══════════════════════════════════════════════════════════════════════════

def _fetch_closes(conn, asset: str, source: str, end) -> list[float]:
    """Execute the price_daily read against an already-open connection.
    No lower start bound — a base-rate statistic wants as much history as
    is available, not a fixed recent window (unlike event_study.py, whose
    purpose is a recent-events count)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT close FROM quant.price_daily WHERE asset = %s AND source = %s "
        "AND ts <= %s AND close IS NOT NULL ORDER BY ts",
        (asset, source, end),
    )
    return [float(r["close"]) for r in cur.fetchall()]


def history_says(symbol: str, min_n: int = 25, horizon_days: int = 7,
                  conn=None, today=None) -> Optional[dict]:
    """Live (uncached) compute — see module docstring for the contract.

    Parameters
    ----------
    symbol : a key of quant.engine.config.TARGET_ASSETS. An unrecognized
             symbol returns None without touching the DB.
    conn   : optional pre-existing DB connection (quant Postgres); when
             None, obtains an INDEPENDENT one via carrac.db_pool.get_quant_conn() —
             same idiom as every other band/study reader in this package.
    today  : optional override for "today" (a date); defaults to UTC today.

    Returns None on any error, on an unrecognized symbol, or when fewer
    than ``min_n`` historical days matched today's condition.
    """
    try:
        asset_source = _TARGET_ASSETS.get(symbol)
        if asset_source is None:
            return None
        price_asset, source = asset_source

        _today = today or datetime.now(timezone.utc).date()

        if conn is not None:
            closes = _fetch_closes(conn, price_asset, source, _today)
        else:
            with get_quant_conn() as c:
                closes = _fetch_closes(c, price_asset, source, _today)

        return _compute_history_says(closes, min_n=min_n, horizon_days=horizon_days)
    except Exception as exc:  # noqa: BLE001
        log.warning("history_says(%s) failed (non-fatal): %s", symbol, exc)
        return None


def _cache_key(symbol: str, today) -> str:
    return f"histsays:{symbol}:{today.isoformat()}"


def get_history_says(symbol: str, today=None, conn=None, r=None) -> Optional[dict]:
    """Redis-cached wrapper around ``history_says()`` — this is the entry
    point ``carrac/reports/routes.py`` calls.

    Cache key ``histsays:{symbol}:{date}``, TTL 6h: the cluster-spotlight
    gather runs on every /brief render (page itself is cached 600s, but that
    TTL is far shorter than "recompute the full RSI/percentile series
    against quant.price_daily"), so morning-page admins hitting the page
    repeatedly across a day shouldn't each pay that cost. Both a real result
    and a None result are cached (a None-gate outcome doesn't change within
    the same day either) — this mirrors carrac/facts/wire.py's
    store_asset_facts/get_asset_facts JSON-over-Redis idiom.

    Non-fatal: any Redis error (unavailable client, serialization failure)
    falls through to a live (uncached) ``history_says()`` call — same
    contract as every other cached band in this codebase.
    """
    _today = today or datetime.now(timezone.utc).date()
    key = _cache_key(symbol, _today)

    client = r
    if client is None:
        try:
            from carrac.redis_cache import get_redis
            client = get_redis()
        except Exception:  # noqa: BLE001
            client = None

    if client is not None:
        try:
            raw = client.get(key)
            if raw is not None:
                return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("get_history_says(%s) cache read failed (non-fatal): %s", symbol, exc)

    result = history_says(symbol, conn=conn, today=_today)

    if client is not None:
        try:
            client.set(key, json.dumps(result), ex=_CACHE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("get_history_says(%s) cache write failed (non-fatal): %s", symbol, exc)

    return result
