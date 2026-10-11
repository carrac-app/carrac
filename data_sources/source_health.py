"""Is the source still answering the question we asked?

Every cache guard Carrac has asks whether the DATA is healthy. None of them
asks whether the SOURCE is. The two are different, and the gap hides failures
like these:

* Yahoo's ``CL=F`` (WTI front month, the only source OIL has,
  because OIL is in ``WRONG_SCALE_ON_STOOQ`` and never reaches Stooq) can return
  exactly ONE row for any range asked, for hours at a time, while ``BZ=F``, ``GC=F``, ``NG=F`` and the dated ``CLZ26.NYM``
  all return the normal ~500. Every fetch still succeeds, the one row it
  returns is recent, and the cache mtime keeps advancing — so nothing is
  stale, nothing errors, and no staleness guard can see it. OIL is on the public
  brief.
* A source that stopped, invisible to the staleness guards for
  the same reason.

One 'this source is answering with far less than its baseline' guard covers
both, so it is built once, here.

WHAT THIS IS NOT. It is not a universal wrapper. The invariant only holds for
a FIXED-RANGE request — one that asks for the same window every time, so the
row count is expected to be stable. ``YahooClient.fetch_daily`` qualifies: it
ignores its ``start_date`` argument and always asks for ``range=2y``
(data_sources/yahoo.py, and the mechanism the carried-volume guard turned on). Stooq's
``fetch`` does not — it computes a start date from the cache tail, so its row
count legitimately varies from 700 to 2 between calls, and pointing this at it
would page on normal operation. Adding a caller is one line; asserting the
request is fixed-range is the caller's job, which is why the shape is a
parameter rather than something guessed here.

EDGE-TRIGGERED BY CONSTRUCTION. The baseline is the PREVIOUS successful
fetch's row count, not an all-time high-water mark. So a collapse pages once,
at the transition; a source that stays collapsed compares 1 against 1 and goes
quiet; and a recovery is an increase, which is silent. That is deliberate —
an all-time high-water mark would page every cycle forever the day a source
legitimately shortens its history, which is how a guard becomes noise that trains people to ignore it.
"""

import logging

logger = logging.getLogger('carrac.data_sources.source_health')

_BASELINE_PREFIX = 'mcd:fetchrows:'
# Long enough to survive a promote and a quiet weekend, short enough that a
# symbol Carrac stops fetching does not keep a stale baseline forever.
_BASELINE_TTL = 30 * 24 * 3600

# Below this, a "collapse" is not distinguishable from a series that is simply
# short (a newly listed instrument, a symbol whose history starts recently).
MIN_BASELINE_ROWS = 30

# Example: CL=F going 504 -> 1 rows is 0.2% of its baseline.
# A settle-boundary or holiday cannot move a 2-year window by half, so the two
# populations are not close and the exact value is not load-bearing.
COLLAPSE_RATIO = 0.5


def _get_redis():
    """Same Redis singleton as the rest of the cache layer."""
    try:
        from carrac.redis_cache import get_redis
        return get_redis()
    except Exception:
        return None


def check_fixed_range_fetch(source: str, symbol: str, request_shape: str,
                            rows: int, alarm=None) -> bool:
    """Compare a fixed-range fetch against the previous one. True if it collapsed.

    ``request_shape`` names the window asked for (e.g. ``'daily:2y'``) and is
    part of the key: a client that ever asks for two different ranges must not
    compare one against the other.

    Never raises and never blocks the fetch — a fault in a health check must
    not cost the caller its data. Returns False when there is no usable
    baseline yet, which is also what a missing Redis produces: this guard
    degrades to silence, so it is a second line of defence and not the only
    one. The primary remains the staleness checks, which do eventually notice
    a tail that stops advancing — just days later, which is the whole reason
    this exists.

    ``alarm`` is injectable so tests exercise the real decision without
    stubbing this function or reaching the network.
    """
    try:
        r = _get_redis()
        if r is None:
            return False
        key = f'{_BASELINE_PREFIX}{source}:{symbol.upper()}:{request_shape}'
        raw = r.get(key)
        try:
            baseline = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            baseline = None

        # Record the new observation FIRST, unconditionally. Whatever happens
        # below, the next call must compare against what actually came back —
        # otherwise a collapse that pages once would page again on every
        # subsequent fetch, having never updated its own baseline.
        try:
            r.setex(key, _BASELINE_TTL, str(int(rows)))
        except Exception:  # noqa: BLE001
            pass

        if baseline is None or baseline < MIN_BASELINE_ROWS:
            return False
        if rows >= baseline * COLLAPSE_RATIO:
            return False

        (alarm or _alarm)(source, symbol, request_shape, rows, baseline)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("source-health check failed for %s/%s: %s", source, symbol, e)
        return False


def _alarm(source: str, symbol: str, request_shape: str, rows: int,
           baseline: int) -> None:
    """Page a human. The source is up and answering — with much less."""
    msg = (
        f"source regression: {source} returned {rows} row(s) for "
        f"{symbol} ({request_shape}), against {baseline} on the previous fetch. "
        f"The request succeeded, so no circuit breaker tripped and nothing "
        f"looks stale — the cache tail will keep advancing on whatever came "
        f"back. Check whether the source still carries this instrument before "
        f"assuming the cache is at fault; if the symbol has no second source "
        f"(anything in WRONG_SCALE_ON_STOOQ is Yahoo-only), its history can "
        f"neither be extended nor repaired while this lasts."
    )
    logger.error(msg)   # -> the error tracker via the Sentry logging integration
    try:
        from carrac.notifications.ntfy import send_ntfy
        send_ntfy(
            body=msg,
            title="⚠️ Carrac source regression",
            priority="4",
            tags="satellite",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("source-regression ntfy failed (swallowed): %s", e)
