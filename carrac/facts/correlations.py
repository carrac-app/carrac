"""Correlation-regime shift computation for the morning report.

Public API:
    _compute_shifts(today_rows, prior_rows, *, min_delta=0.25, top=6) -> list[dict]
        PURE function — no I/O, no datetime.now(), no randomness.
    get_correlation_shifts(window=60, top=6, min_delta=0.25, method='spearman', conn=None) -> list[dict]
        DB reader — wraps _compute_shifts; non-fatal (returns [] on any error).

Each result dict:
    {"a": str, "b": str, "prior": float, "today": float, "shift": float, "became_significant": bool}
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Module-level import for patchability in tests.
try:
    from carrac.db_pool import get_quant_conn as get_conn  # noqa: F401
except ImportError:  # pragma: no cover — quant not installed in some envs
    get_conn = None  # type: ignore[assignment]


# ──────────────────────────────────────────────────────────────────────────────
# Pure diff helper
# ──────────────────────────────────────────────────────────────────────────────

def _compute_shifts(
    today_rows: list[dict],
    prior_rows: list[dict],
    *,
    min_delta: float = 0.25,
    top: int = 6,
) -> list[dict]:
    """Compute week-over-week correlation shifts.

    Parameters
    ----------
    today_rows  : list of dicts with keys metric_a, metric_b, correlation,
                  q_value, significant  (the latest snapshot).
    prior_rows  : list of dicts with the same keys (~7 days earlier snapshot).
    min_delta   : minimum absolute shift to include (default 0.25).
    top         : maximum number of results to return, sorted by |shift| desc.

    Returns
    -------
    list of dicts: {a, b, prior, today, shift (2 dp), became_significant}

    PURE: no I/O, no datetime.now(), no randomness.
    """
    if not today_rows or not prior_rows:
        return []

    # Build prior lookup keyed (metric_a, metric_b)
    prior_lookup: dict[tuple[str, str], dict] = {}
    for row in prior_rows:
        key = (row["metric_a"], row["metric_b"])
        prior_lookup[key] = row

    def _is_significant(row: dict) -> bool:
        """Row is significant if significant flag is truthy OR q_value < 0.05."""
        if row.get("significant"):
            return True
        q = row.get("q_value")
        if q is not None and q < 0.05:
            return True
        return False

    candidates: list[dict] = []
    for row in today_rows:
        # Only rows that are significant TODAY
        if not _is_significant(row):
            continue
        key = (row["metric_a"], row["metric_b"])
        prior = prior_lookup.get(key)
        if prior is None:
            continue
        shift = float(row["correlation"]) - float(prior["correlation"])
        if abs(shift) < min_delta:
            continue
        # became_significant: uses _is_significant for both sides so that rows
        # admitted via q-value only (significant=None, q<0.05) are also tagged.
        candidates.append({
            "a": row["metric_a"],
            "b": row["metric_b"],
            "prior": float(prior["correlation"]),
            "today": float(row["correlation"]),
            "shift": round(shift, 2),
            "became_significant": _is_significant(row) and not _is_significant(prior),
        })

    # Sort by |shift| desc, take top N
    candidates.sort(key=lambda r: abs(r["shift"]), reverse=True)
    return candidates[:top]


# ──────────────────────────────────────────────────────────────────────────────
# DB reader
# ──────────────────────────────────────────────────────────────────────────────

def get_correlation_shifts(
    window: int = 60,
    top: int = 6,
    min_delta: float = 0.25,
    method: str = "spearman",
    conn=None,
) -> list[dict]:
    """Fetch the latest and ~7-days-prior correlation snapshots from quant.correlations,
    then call _compute_shifts.

    Non-fatal: returns [] on any error.
    No new storage: diffs directly against the retained quant.correlations table.
    """
    try:
        _conn = conn if conn is not None else get_conn()
        with _conn as c:
            cur = c.cursor()

            # Latest snapshot
            cur.execute(
                "SELECT MAX(ts) AS ts FROM quant.correlations "
                "WHERE window_days = %s AND method = %s",
                (window, method),
            )
            row = cur.fetchone()
            if row is None or row["ts"] is None:
                return []
            max_ts = row["ts"]

            # Prior snapshot (~7 days earlier; tolerates gaps)
            cur.execute(
                "SELECT MAX(ts) AS ts FROM quant.correlations "
                "WHERE window_days = %s AND method = %s "
                "AND ts <= %s - INTERVAL '7 days'",
                (window, method, max_ts),
            )
            row = cur.fetchone()
            if row is None or row["ts"] is None:
                return []
            prior_ts = row["ts"]

            # Fetch all rows for both timestamps
            cur.execute(
                "SELECT metric_a, metric_b, correlation, q_value, significant, ts "
                "FROM quant.correlations "
                "WHERE window_days = %s AND method = %s AND ts IN (%s, %s)",
                (window, method, max_ts, prior_ts),
            )
            all_rows = cur.fetchall()

        # quant.correlations is computed across the WHOLE metric
        # registry, Deribit-backed metrics included — so pairs like
        # "DVOL_BTC / RVOL_BTC" were being rendered on /brief, which is public
        # and unauthenticated. A correlation computed ON withheld data is still
        # derived from it; the licensing rule does not stop at the raw value.
        #
        # leadlag_metric_allowed() already existed for exactly this and was
        # wired only into carrac/mcp/tools/quant.py — the facts layer never
        # called it. Both live callers of this function
        # (morning_engine/report_blocks.py and carrac/reports/routes.py) are
        # shared/public surfaces, so filtering here covers both rather than
        # leaving the next caller to remember.
        #
        # Filtered BEFORE _compute_shifts so a dropped pair cannot consume one
        # of the `top` slots and silently shrink the module.
        from carrac.compliance.deribit_gate import leadlag_metric_allowed

        def _permitted(r):
            return (leadlag_metric_allowed(r["metric_a"])
                    and leadlag_metric_allowed(r["metric_b"]))

        all_rows = [r for r in all_rows if _permitted(r)]

        today_rows = [r for r in all_rows if r["ts"] == max_ts]
        prior_rows = [r for r in all_rows if r["ts"] == prior_ts]

        return _compute_shifts(today_rows, prior_rows, min_delta=min_delta, top=top)

    except Exception as exc:
        log.warning("get_correlation_shifts failed (non-fatal): %s", exc)
        return []
