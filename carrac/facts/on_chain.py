"""Batched on-chain data fetch for the facts engine.

Retrieves the latest on-chain metric values for all assets in a SINGLE query,
to avoid N+1 fetches inside the per-asset precompute loop.

Public API
----------
fetch_smart_money_bias(conn=None) -> dict[str, float]
    Returns {asset: value} for the latest SMART_MONEY_BIAS per asset.
    Non-fatal: any exception → returns {}.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _fetch_bias(conn) -> dict[str, float]:
    """Execute the smart-money-bias query against an already-open connection."""
    cur = conn.cursor()
    cur.execute(
        "SELECT asset, value FROM quant.onchain "
        "WHERE metric = 'SMART_MONEY_BIAS' "
        "AND ts = (SELECT MAX(ts) FROM quant.onchain WHERE metric = 'SMART_MONEY_BIAS') "
        "ORDER BY asset"
    )
    return {row["asset"]: float(row["value"]) for row in cur.fetchall()}


def fetch_smart_money_bias(conn=None) -> dict[str, float]:
    """{asset: bias_value} for the latest SMART_MONEY_BIAS in quant.onchain.

    Uses a single batched query over quant.onchain to avoid N+1 fetches
    inside the per-asset precompute loop.

    Parameters
    ----------
    conn : optional pre-existing DB connection; when None, obtains one
           via carrac.db_pool.get_quant_conn() (used as a context manager).

    Returns
    -------
    dict mapping asset symbol to the latest SMART_MONEY_BIAS float value.
    Returns {} on any exception (non-fatal).
    """
    try:
        if conn is not None:
            return _fetch_bias(conn)
        from carrac.db_pool import get_quant_conn as get_conn  # noqa: PLC0415
        with get_conn() as c:
            return _fetch_bias(c)
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch_smart_money_bias failed (non-fatal): %s", exc)
        return {}
