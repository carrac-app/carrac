"""Daily-close lookup for curve reconstruction, backed by the CSV cache.

Reads cache ONLY. ``database.get_ohlcv_data`` performs delta downloads over
the network, which must never happen once per asset inside a page render.
A symbol missing from cache yields no price, which the reconstruction
already reports as an exclusion rather than a zero.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Optional

logger = logging.getLogger('carrac.portfolio.prices')


def _default_loader(symbol: str):
    from csv_cache import CSVCache
    return CSVCache().load(symbol, timeframe='daily')


def _to_close_map(frame) -> dict:
    """``{date: close}`` from an OHLCV frame, or ``{}`` if unusable."""
    try:
        column = 'Close' if 'Close' in frame.columns else 'close'
        out = {}
        for stamp, value in frame[column].items():
            if value is None:
                continue
            out[stamp.date()] = float(value)
        return out
    except Exception:
        logger.warning('unusable OHLC frame; treating as no prices', exc_info=True)
        return {}


def build_price_lookup(symbols: list[str], loader: Callable[[str], object] | None = None):
    """Load each symbol's daily closes once, return a ``(symbol, day)`` lookup."""
    load = loader or _default_loader
    table: dict[str, dict] = {}

    for symbol in symbols:
        key = (symbol or '').upper()
        if not key or key in table:
            continue
        frame = load(key)
        table[key] = _to_close_map(frame) if frame is not None else {}

    def price_at(symbol: str, day: date) -> Optional[float]:
        return table.get((symbol or '').upper(), {}).get(day)

    return price_at
