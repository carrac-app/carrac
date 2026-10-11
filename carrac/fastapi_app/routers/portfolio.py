"""Portfolio read endpoints.

Thin handlers: fetch rows, delegate to the pure functions in
``carrac.portfolio``.

Auth uses ``auth_or_key`` + ``_require_user_id``, NOT ``require_login``.
``require_login`` 302-redirects to /login (see its docstring in
``carrac/fastapi_app/deps/auth.py``), which a ``fetch()`` would silently
follow and then try to parse the login page as JSON. These are JSON
endpoints, so they must reject with 401. This mirrors
``/api/positions/live`` in ``routers/composite.py``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from carrac.fastapi_app.deps.auth import RateCtxDep, auth_or_key
from carrac.fastapi_app.limiter import limiter
from carrac.fastapi_app.routers.composite import _require_user_id

logger = logging.getLogger('carrac.fastapi.portfolio')

router = APIRouter()

MAX_HISTORY_DAYS = 1825  # five years

# How far back the SUMMARY endpoint reconstructs just to read yesterday's
# close for day_change. Wide enough to carry a price forward across a long
# weekend + a holiday Monday (up to ~4 dead days) with margin; NOT the full
# history window — see _load's docstring for why widening this defeats the
# point of Fix 2 (CARRAC portfolio review).
DAY_CHANGE_LOOKBACK_DAYS = 7

# Bound on how many closed trades get_closed_trades_since() returns. Generous
# (an active account trading daily for years is still under this), but finite
# — if hit, _load reports truncated=True rather than silently understating
# the curve for the heaviest traders.
MAX_CLOSED_TRADES = 5000


def _day_change(book_value: float, curve: list, excluded: list) -> tuple:
    """Change against the previous daily close.

    Anchored to the curve on purpose: a weighted average of per-asset 24h
    moves would drift from the line drawn directly beneath the tile.

    ``curve[-1]`` (yesterday) is priced from the CSV daily cache, while
    ``book_value`` (today) is priced live from the crypto cache — two
    different sources that can each have coverage gaps independently. If
    yesterday's point couldn't value every position (``excluded_count`` >
    0), its ``book_value`` understates the true prior book — in the worst
    case it's 0 — and comparing today's full book against that partial
    number reports the whole book as one day's move. Unknown beats
    confidently wrong here, so this returns (None, None) rather than a
    number computed against a known-incomplete baseline.

    ``excluded`` guards the SAME error arriving from the other side: if TODAY cannot price a position but yesterday's curve
    could, ``excluded_count`` is 0, and today's book drops by that
    position's full value against a complete baseline. Symmetric problem,
    symmetric answer — unknown, not a large wrong number.
    """
    if not curve:
        return (None, None)
    if curve[-1].get('excluded_count'):
        return (None, None)
    if excluded:
        return (None, None)
    previous = curve[-1]['book_value']
    delta = round(book_value - previous, 2)
    if not previous:
        return (delta, None)
    return (delta, round(delta / abs(previous) * 100, 2))


def _load(user_id: int, days: int) -> dict:
    """All blocking I/O for both endpoints, run in a worker thread.

    ``days`` bounds the reconstructed curve, and therefore which closed
    trades can possibly matter to it: a trade contributes to the curve only
    if it was open at some point in ``[start, end-1]``. That is a question
    about ``closed_at`` (was it still open when the window began?), never
    about ``opened_at`` — a trade opened five years ago and closed three
    days ago was open for every one of those three days, regardless of how
    old it is. ``get_closed_trades_since(since=start)`` fetches exactly that
    set; ``get_trade_history``'s ``opened_at``-in-window filter would
    silently drop it.
    """
    from carrac.composite.assemblers import _get_content_db, _get_prices
    from carrac.portfolio.history import reconstruct
    from carrac.portfolio.prices import build_price_lookup
    from carrac.portfolio.service import allocate, summarize

    end = date.today()
    start = end - timedelta(days=days)

    db = _get_content_db()
    open_trades = db.get_open_trades(user_id)
    closed = db.get_closed_trades_since(user_id, since=start, limit=MAX_CLOSED_TRADES)
    truncated = len(closed) >= MAX_CLOSED_TRADES

    prices = {
        symbol.upper(): (data.get('price') or data.get('usd'))
        for symbol, data in (_get_prices() or {}).items()
    }

    summary = summarize(open_trades, prices)
    summary['allocation'] = allocate(summary['positions'])

    all_trades = list(open_trades) + list(closed)
    symbols = {(t.get('asset') or '').upper() for t in all_trades}
    price_at = build_price_lookup(sorted(s for s in symbols if s))

    # Curve up to YESTERDAY; today's point is the live book value, which is
    # fresher than any cached daily close.
    curve = reconstruct(all_trades, price_at, start, end - timedelta(days=1))
    return {'summary': summary, 'curve': curve, 'days': days, 'truncated': truncated}


@router.get('/api/portfolio/summary')
@limiter.limit('60/minute')
async def portfolio_summary(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
) -> JSONResponse:
    """Current book value, allocation, and today's change."""
    user_id = _require_user_id(auth)
    # Only the previous day's close is needed here (for _day_change) — not
    # the full history window. See DAY_CHANGE_LOOKBACK_DAYS.
    loaded = await asyncio.to_thread(_load, user_id, DAY_CHANGE_LOOKBACK_DAYS)
    summary = loaded['summary']
    usd, pct = _day_change(
        summary['book_value'], loaded['curve'], summary['excluded'])
    summary['day_change_usd'] = usd
    summary['day_change_pct'] = pct
    summary['truncated'] = loaded['truncated']
    # jsonable_encoder, not a bare dict: `positions[].opened_at` is a raw
    # trade_journal column, and Postgres returns `timestamp with time zone`
    # as a datetime while SQLite returns a string. JSONResponse serialises
    # with plain json.dumps, so the bare dict 500s in production and passes every
    # SQLite-backed test. The other 13 JSONResponse sites in
    # this package already encode; these two were the outliers.
    return JSONResponse(content=jsonable_encoder(summary))


@router.get('/api/portfolio/history')
@limiter.limit('30/minute')
async def portfolio_history(
    request: Request,
    auth: Annotated[dict, Depends(auth_or_key)],
    ctx: RateCtxDep,
    days: int = Query(default=90, ge=1, le=MAX_HISTORY_DAYS),
) -> JSONResponse:
    """Reconstructed daily book-value and cost-basis curve."""
    user_id = _require_user_id(auth)
    loaded = await asyncio.to_thread(_load, user_id, days)
    return JSONResponse(content=jsonable_encoder({
        'curve': loaded['curve'],
        'days': days,
        'truncated': loaded['truncated'],
    }))
