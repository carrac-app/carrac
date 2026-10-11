"""Deterministic own-history event studies (R3 — see carrac/facts/bands.py's
module docstring for the bulletproof rule this module inherits verbatim).

``event_next_day_stats()`` answers, purely from data Carrac already
collects — ``quant.events`` (past macro-print dates) + ``quant.price_daily``
(daily closes) — "of the last N prints of this event type, what was the
median absolute next-trading-day move of <asset>?". No LLM, no invented
numbers: below the ``min_n`` gate the function returns None, never an
estimate. This is the sibling of ``carrac/facts/bands.py`` for the Docket's
``history`` field.

DENSITY CAVEAT: ``quant.events`` CPI/NFP/FOMC/ECB/BOJ/OPTIONS_EXPIRY rows
come from a HARDCODED 2026-and-2027-ONLY schedule
(``quant/collectors/event_calendar.py::SCHEDULED_EVENTS``); no 2024/2025
dates exist in it. PCE/PPI/GDP rows come from
``fetch_fred_release_events()`` (same file), which only discovers dates
``window_days`` (45) AHEAD of "today" on each nightly run — never
backfilled. So every high-impact event_type has only a handful of past
prints with a computable next-day move (CPI/NFP: 6 in 2026 at the time of
writing; FOMC: 4; PCE/PPI/GDP: 0-1), all BELOW the min_n=8 gate.
``event_next_day_stats()`` is therefore expected to return None for every
event_type on live data until history accrues. That is the n-gate working
as designed on immature history, not a bug — it starts passing naturally as
more 2026/2027 prints accrue.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

try:
    from carrac.db_pool import get_quant_conn  # noqa: F401
except ImportError:  # pragma: no cover — quant not installed in some envs
    get_quant_conn = None  # type: ignore[assignment]

# asset -> (price_daily asset symbol, source) — reused from the conditional
# engine's own target-asset map (quant/engine/conditional.py::TARGET_ASSETS)
# rather than re-derived, so this module can never drift onto a different
# asset/source pairing than query_conditional's forward-return lookups use.
try:
    from quant.engine.config import TARGET_ASSETS as _TARGET_ASSETS
except ImportError:  # pragma: no cover — quant not installed in some envs
    _TARGET_ASSETS = {}

# Tolerance (calendar days) for finding the "day-of" close and the following
# "next trading day" close around a print date — absorbs weekends/holidays.
# A print whose window has no close within this tolerance is SKIPPED, never
# estimated (bulletproof rule).
_MAX_GAP_DAYS = 4


def _closest_close_on_or_after(price_by_date: dict, start, max_gap_days: int = _MAX_GAP_DAYS):
    """PURE. First (date, close) at or after ``start`` within ``max_gap_days``
    calendar days (inclusive). ``price_by_date``: {date: close}. Returns
    (None, None) when nothing is found in that window — caller must skip
    this print, never estimate a substitute."""
    for offset in range(max_gap_days + 1):
        d = start + timedelta(days=offset)
        price = price_by_date.get(d)
        if price is not None:
            return d, price
    return None, None


def _compute_event_next_day_stats(event_dates: list, price_by_date: dict,
                                   min_n: int = 8) -> Optional[dict]:
    """PURE. For each past ``event_dates`` entry, find its own (gap-tolerant)
    close and the following trading day's (gap-tolerant) close; the move is
    ``abs((next_close - base_close) / base_close)``. A print whose base or
    next close isn't found within the gap tolerance is skipped — it does
    NOT count toward n. Returns ``{'n', 'median_abs_move_pct'}`` when at
    least ``min_n`` prints produced a computable move, else None (the n-gate
    — never estimate below it)."""
    moves = []
    for ts in event_dates:
        base_date, base_price = _closest_close_on_or_after(price_by_date, ts)
        if base_price is None or base_price == 0:
            continue
        _next_date, next_price = _closest_close_on_or_after(
            price_by_date, base_date + timedelta(days=1)
        )
        if next_price is None:
            continue
        moves.append(abs((next_price - base_price) / base_price) * 100)

    if len(moves) < min_n:
        return None
    return {
        "n": len(moves),
        "median_abs_move_pct": round(statistics.median(moves), 2),
    }


def _fetch_event_study_rows(conn, event_type: str, asset: str, source: str,
                             start, end) -> dict:
    """Execute the two event-study queries (past ``event_type`` dates +
    ``asset``/``source``'s daily closes over the same lookback window)
    against an already-open connection."""
    cur = conn.cursor()
    cur.execute(
        "SELECT ts FROM quant.events WHERE event_type = %s "
        "AND ts >= %s AND ts <= %s ORDER BY ts",
        (event_type, start, end),
    )
    event_dates = [r["ts"] for r in cur.fetchall()]

    cur.execute(
        "SELECT ts, close FROM quant.price_daily WHERE asset = %s AND source = %s "
        "AND ts >= %s AND ts <= %s AND close IS NOT NULL ORDER BY ts",
        (asset, source, start, end),
    )
    price_by_date = {r["ts"]: float(r["close"]) for r in cur.fetchall()}
    return {"event_dates": event_dates, "price_by_date": price_by_date}


def event_next_day_stats(event_type: str, asset: str = "SPX", min_n: int = 8,
                          lookback_days: int = 730, conn=None,
                          today=None) -> Optional[dict]:
    """Own-history event study: of the last ``lookback_days`` of
    ``event_type`` prints (``quant.events``), the median absolute
    next-trading-day move of ``asset`` (``quant.price_daily``). Deterministic
    SQL + pure post-processing — no LLM involvement, never estimated (see
    module docstring bulletproof rule + density caveat).

    Parameters
    ----------
    event_type : quant.events.event_type value, e.g. 'CPI', 'NFP', 'FOMC'.
    asset      : a key of quant.engine.config.TARGET_ASSETS (default 'SPX',
                 the Docket's target per the plan contract). An unrecognized
                 asset returns None without touching the DB.
    min_n      : n-gate — fewer than this many computable prints -> None.
    lookback_days : how far back (calendar days) to look for past prints.
    conn       : optional pre-existing DB connection (quant Postgres); when
                 None, obtains one via carrac.db_pool.get_quant_conn() (used as a context
                 manager) — an INDEPENDENT connection from whatever the
                 caller (e.g. carrac.facts.bands.build_docket) used for its
                 own query, matching every other band reader in this package
                 (each band/study gathers on its own connection; see
                 carrac/facts/bands.py's module docstring).
    today      : optional override for "today" (a date); defaults to UTC
                 today.

    Returns None on any error, on an unrecognized asset, or when fewer than
    ``min_n`` prints produced a computable next-day move.
    """
    try:
        asset_source = _TARGET_ASSETS.get(asset)
        if asset_source is None:
            return None
        price_asset, source = asset_source

        _today = today or datetime.now(timezone.utc).date()
        start = _today - timedelta(days=lookback_days)

        if conn is not None:
            data = _fetch_event_study_rows(conn, event_type, price_asset, source, start, _today)
        else:
            with get_quant_conn() as c:
                data = _fetch_event_study_rows(c, event_type, price_asset, source, start, _today)

        return _compute_event_next_day_stats(data["event_dates"], data["price_by_date"], min_n)
    except Exception as exc:  # noqa: BLE001
        log.warning("event_next_day_stats(%s) failed (non-fatal): %s", event_type, exc)
        return None
