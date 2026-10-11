"""Reconstruct the daily book-value curve from trade history.

Deliberately NOT a nightly snapshot table: replaying trades against stored
daily closes means the curve has full history the moment the feature ships,
and there is no cron that can silently stop running.

``price_at`` is injected rather than imported so this module stays pure and
testable without touching the CSV cache or the network.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Optional

from carrac.portfolio.service import value_position


def _as_date(value) -> Optional[date]:
    """Parse the ISO-ish timestamps stored in ``trade_journal``."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace(' ', 'T')
    try:
        return datetime.fromisoformat(text[:19]).date()
    except ValueError:
        return None


def _open_on(trade: dict, day: date) -> bool:
    opened = _as_date(trade.get('opened_at'))
    if opened is None or opened > day:
        return False
    closed = _as_date(trade.get('closed_at'))
    # A position is open on its close date (e.g., closed at 18:00 on day X is still
    # open for daily valuation at end-of-day X), symmetric with opened <= day.
    return closed is None or closed >= day


def reconstruct(trades: list[dict], price_at: Callable[[str, date], Optional[float]],
                start: date, end: date) -> list[dict]:
    """Daily book value and cost basis across ``[start, end]`` inclusive.

    A position with no usable close for a given day is counted in that day's
    ``excluded_count`` rather than being valued at zero, so the UI can say
    the curve is incomplete instead of drawing a dip that never happened.
    """
    curve: list[dict] = []
    last_close: dict[str, float] = {}

    day = start
    while day <= end:
        book_value = 0.0
        cost_basis = 0.0
        excluded = 0

        for trade in trades:
            symbol = (trade.get('asset') or '').upper()

            # Refresh the carry-forward cache even for days this trade is not
            # open on — a later day may need this close.
            close = price_at(symbol, day)
            if close is not None:
                last_close[symbol] = close

            if not _open_on(trade, day):
                continue

            usable = close if close is not None else last_close.get(symbol)
            valued = value_position(trade, usable)
            if valued is None:
                excluded += 1
                continue

            book_value += valued['book_contribution']
            cost_basis += valued['cost_basis']

        curve.append({
            'date': day.isoformat(),
            'book_value': book_value,
            'cost_basis': cost_basis,
            'excluded_count': excluded,
        })
        day += timedelta(days=1)

    return curve
