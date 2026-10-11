"""Imminent high-impact macro-event reader for the Scanner.

quant.events is populated by quant/collectors/event_calendar.py (FOMC, CPI,
NFP, ECB, BoJ, options-expiry, plus FRED-sourced PCE/PPI/GDP releases — see
SCHEDULED_EVENTS / FRED_RELEASE_EVENTS there). Impact is a plain TEXT column
with two values in practice: 'high' (FOMC/CPI/NFP/PCE) and 'medium' (ECB/BoJ/
options expiry) — this module only cares about 'high'.

quant.events.ts is a DATE column (see quant/schema.py) — there is no
intraday time-of-day in the data. So "hours_until" is derived from the
calendar-day difference, not a real clock time:
  - event date == today          -> hours_until = 0.0   (flagged all day —
                                     we can't tell if the release already
                                     happened this morning or is still ahead)
  - event date == today + 1 day  -> hours_until = 24.0
  - event date == today + N days -> hours_until = N * 24.0
Past events (date < today) are never returned.

Cheap + resilient by design:
  - Results are cached in-process for CACHE_TTL_SECONDS, so a Scanner pass
    over ~337 assets triggers at most ONE query (callers should still only
    call this once per score_all_assets pass — the cache is a backstop, not
    a substitute for that discipline).
  - Any DB error is swallowed and returns None — the Scanner must never break
    because the event calendar is unavailable.

DB access follows the same convention as carrac/mcp/tools/market.py's
market_get_calendar: the quant connection pool from carrac.db_pool
(``get_quant_conn``, RealDictCursor rows).
"""

import logging
import time as _time
from datetime import date, datetime, timezone

from carrac.db_pool import get_quant_conn as get_conn

logger = logging.getLogger('carrac.scanner.event_risk')

HIGH_IMPACT = 'high'

CACHE_TTL_SECONDS = 600  # 10 minutes

# TTL cache keyed by the requested `hours` window. Scanner passes always use
# the same window per process, so in practice this holds a single entry.
_cache = {}  # hours -> (result_or_None, cached_at_monotonic)


def _query_imminent_high_impact_event(hours):
    """Hit quant.events for the nearest upcoming HIGH-impact event within
    `hours` hours. Returns a result dict or None. Any DB error -> None."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ts, title, impact
                FROM quant.events
                WHERE impact = %s AND ts >= CURRENT_DATE
                ORDER BY ts ASC
                LIMIT 5
                """,
                (HIGH_IMPACT,),
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("[event_risk] quant.events query failed: %s", e)
        return None

    today = datetime.now(timezone.utc).date()

    for row in rows:
        event_date = row['ts']
        if isinstance(event_date, datetime):
            event_date = event_date.date()
        if not isinstance(event_date, date):
            continue
        if event_date < today:
            continue
        days_until = (event_date - today).days
        hours_until = float(days_until * 24)
        if hours_until > hours:
            # rows are ORDER BY ts ASC — once one misses the window every
            # later row misses it too.
            break
        return {
            'title': row['title'],
            'ts': event_date.isoformat(),
            'hours_until': hours_until,
            'impact': row['impact'],
        }
    return None


def get_imminent_event(hours=36):
    """Return the nearest HIGH-impact quant.events row within `hours` hours
    from now, as {title, ts, hours_until, impact}, or None if there is no
    such event or the lookup failed.

    TTL-cached in-process (CACHE_TTL_SECONDS) so repeated calls within the
    same Scanner pass never re-hit the DB.
    """
    now_mono = _time.monotonic()
    cached = _cache.get(hours)
    if cached is not None:
        result, cached_at = cached
        if now_mono - cached_at < CACHE_TTL_SECONDS:
            return result

    result = _query_imminent_high_impact_event(hours)
    _cache[hours] = (result, now_mono)
    return result
