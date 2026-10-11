"""
Rolling MFE/MAE refresh hook for open trades.

``refresh_open_trade_mfe_mae()`` is called by the precompute loop every
~60 s.  It finds all open trades across all users, applies a per-trade
5-minute Redis cooldown gate so each trade is recomputed at most once per
5 minutes regardless of how often the hook fires, then calls
``calculate_and_store_mfe_mae`` for each qualifying trade.

Design constraints:
- FAIL OPEN: Redis unavailability never blocks computation.
- Per-trade try/except so one bad trade never aborts the batch.
- Skips rows where compute returns ``{'error': ...}``.
- Never raises out of the function — all exceptions are logged and swallowed.
"""

import logging

logger = logging.getLogger('carrac.journal.open_mfe')

_COOLDOWN_KEY_PREFIX = 'mcd:open_mfe:'
_COOLDOWN_TTL_SEC = 300  # 5 minutes


def refresh_open_trade_mfe_mae() -> None:
    """Refresh rolling MFE/MAE for all open trades (cross-user).

    Called from ``carrac.precompute._precompute_loop`` on every iteration
    (roughly every 60 s).  The per-trade Redis gate limits actual kline
    fetches to one per 5-minute window per trade so network pressure stays
    bounded even with many open positions.
    """
    try:
        _run()
    except Exception as exc:
        logger.warning(f"refresh_open_trade_mfe_mae: unexpected top-level error: {exc}")


def _run() -> None:
    # Fetch all open trade IDs cross-user (RAII pool conn, mirroring
    # indicator_state.get_bot_briefing pattern so no pool slot is pinned).
    try:
        from carrac.db_pool import get_conn as _pool_get_conn, adapt_query
        with _pool_get_conn() as conn:
            rows = conn.execute(adapt_query(
                'SELECT id FROM trade_journal WHERE closed_at IS NULL'
            )).fetchall()
    except Exception as exc:
        logger.debug(f"open_mfe_hook: DB query failed: {exc}")
        return

    if not rows:
        return

    # Shared ContentDatabase instance for all update calls this iteration.
    try:
        from carrac.content_db import get_content_db
        db = get_content_db()
    except Exception as exc:
        logger.debug(f"open_mfe_hook: get_content_db failed: {exc}")
        return

    # Redis gate — fail open: if Redis is unavailable, compute without gating.
    try:
        from carrac.caches import _get_redis
        r = _get_redis()
    except Exception:
        r = None

    from carrac.journal.mfe_mae import calculate_and_store_mfe_mae

    processed = skipped = errors = 0
    for row in rows:
        trade_id = row['id']
        try:
            _refresh_one(db, r, trade_id, calculate_and_store_mfe_mae)
            processed += 1
        except _SkipTrade:
            skipped += 1
        except Exception as exc:
            errors += 1
            logger.debug(f"open_mfe_hook: trade {trade_id} failed: {exc}")

    if processed or errors:
        logger.debug(
            f"open_mfe_hook: processed={processed} skipped={skipped} errors={errors}"
        )


class _SkipTrade(Exception):
    """Raised internally when a trade should be skipped (cooldown hit)."""


def _refresh_one(db, r, trade_id: int, compute_fn) -> None:
    """Gate + compute for a single trade."""
    key = f"{_COOLDOWN_KEY_PREFIX}{trade_id}"

    # Check cooldown gate (skip if key exists in Redis)
    if r is not None:
        try:
            if r.exists(key):
                raise _SkipTrade()
        except _SkipTrade:
            raise
        except Exception:
            pass  # Redis unavailable → fail open, proceed with compute

    result = compute_fn(db, trade_id)

    # Gate the next attempt regardless of outcome — a successful compute OR a
    # no-data skip (non-crypto / delisted symbols on the crypto-only kline
    # fetcher) both set the cooldown, so we don't re-fetch every ~60s. A raised
    # exception bypasses this (caller retries next cycle), which is the right
    # behaviour for transient failures.
    if r is not None:
        try:
            r.set(key, '1', ex=_COOLDOWN_TTL_SEC)
        except Exception:
            pass  # Non-fatal — next iteration will just recompute early

    # Skip error results silently (cooldown already set above)
    if isinstance(result, dict) and result.get('error'):
        logger.debug(f"open_mfe_hook: trade {trade_id} compute error: {result['error']}")
        return
