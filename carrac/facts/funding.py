"""Batched funding-history fetch for the facts engine.

Retrieves the trailing N-day funding rate history for all crypto assets in
a SINGLE query, to avoid N+1 fetches inside the per-asset precompute loop.

Public API
----------
fetch_funding_history(window_days=90, conn=None) -> dict[str, list[float]]
    Returns {asset: [v_oldest, ..., v_newest]} for the trailing window.
    Prefers funding_rate_cross_avg; falls back to funding_rate_avg.
    Non-fatal: any exception → returns {}.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _fetch(conn, window_days: int) -> dict[str, list[float]]:
    """Execute the funding-history query against an already-open connection.

    Reads rows by name (RealDictCursor shape: ts, asset, v).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT ts, asset, COALESCE(funding_rate_cross_avg, funding_rate_avg) AS v "
        "FROM quant.derivatives "
        f"WHERE ts >= CURRENT_DATE - INTERVAL '{int(window_days)} days' "
        "AND COALESCE(funding_rate_cross_avg, funding_rate_avg) IS NOT NULL "
        "ORDER BY asset, ts ASC"
    )
    out: dict[str, list[float]] = {}
    for row in cur.fetchall():
        out.setdefault(row["asset"], []).append(float(row["v"]))
    return out


def fetch_funding_history(window_days: int = 90, conn=None) -> dict[str, list[float]]:
    """{asset: [funding values oldest→newest]} for the trailing window.

    Prefers funding_rate_cross_avg, falls back to funding_rate_avg.

    Uses a single batched query over quant.derivatives to avoid N+1 fetches
    inside the per-asset precompute loop.

    Parameters
    ----------
    window_days : trailing calendar days to include (default 90).
    conn        : optional pre-existing DB connection; when None, obtains one
                  via carrac.db_pool.get_quant_conn() (used as a context manager).

    Returns
    -------
    dict mapping asset symbol to an ordered list of float values (oldest first).
    Returns {} on any exception (non-fatal).
    """
    try:
        if conn is not None:
            return _fetch(conn, window_days)
        from carrac.db_pool import get_quant_conn as get_conn  # noqa: PLC0415
        with get_conn() as c:
            return _fetch(c, window_days)
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch_funding_history failed (non-fatal): %s", exc)
        return {}
