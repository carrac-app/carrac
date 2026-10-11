"""Per-position valuation.

Book value is deliberately NOT the sum of notionals. Spot value is
``size * price`` because the asset is owned; a futures position's
contribution is its unrealised P&L, because its notional is not money the
user has. Summing notionals would overstate the book.

The invariant every consumer relies on:

    book_contribution - cost_basis == unrealized_pnl_usd

which is what makes the gap between the two lines on the value curve read
as P&L without a legend.
"""

from __future__ import annotations

SPOT_ACCOUNT_TAG = 'spot'


def classify(trade: dict) -> str:
    """``'spot'`` when the trade is tagged as spot, else ``'futures'``."""
    tag = (trade.get('account_tag') or '').strip().lower()
    return 'spot' if tag == SPOT_ACCOUNT_TAG else 'futures'


def exclusion_reason(trade: dict, current_price: float | None) -> str | None:
    """Why this trade cannot be valued, or ``None`` if it can be.

    Returned verbatim to the UI — an unvaluable trade is reported, never
    silently dropped from a total.
    """
    size = trade.get('size')
    if not size:
        return 'no_size'
    if not trade.get('entry_price'):
        return 'no_entry'
    if current_price is None:
        return 'no_price'
    return None


def value_position(trade: dict, current_price: float | None) -> dict | None:
    """Value one open trade. ``None`` when it cannot be valued."""
    if exclusion_reason(trade, current_price) is not None:
        return None

    size = float(trade['size'])
    entry = float(trade['entry_price'])
    price = float(current_price)
    kind = classify(trade)
    direction = (trade.get('direction') or 'long').lower()

    if kind == 'spot':
        cost_basis = size * entry
        market_value = size * price
        pnl = market_value - cost_basis
        book_contribution = market_value
    else:
        # Futures: no cost basis and no market value — only mark-to-market P&L.
        cost_basis = 0.0
        market_value = 0.0
        move = (price - entry) if direction == 'long' else (entry - price)
        pnl = move * size
        book_contribution = pnl

    return {
        'id': trade.get('id'),
        'asset': trade.get('asset'),
        'kind': kind,
        'direction': direction,
        'size': size,
        'entry_price': entry,
        'current_price': price,
        'cost_basis': cost_basis,
        'market_value': market_value,
        'unrealized_pnl_usd': pnl,
        'book_contribution': book_contribution,
        'opened_at': trade.get('opened_at'),
    }


def summarize(trades: list[dict], prices: dict) -> dict:
    """Value every open trade and roll it into one book summary.

    ``prices`` maps upper-case symbol to current price. A trade whose price
    or size is missing lands in ``excluded`` with a reason — it is never
    quietly treated as zero.
    """
    positions: list[dict] = []
    excluded: list[dict] = []

    for trade in trades:
        price = prices.get((trade.get('asset') or '').upper())
        valued = value_position(trade, price)
        if valued is None:
            excluded.append({
                'id': trade.get('id'),
                'asset': trade.get('asset'),
                'reason': exclusion_reason(trade, price),
            })
            continue
        positions.append(valued)

    spot_market_value = sum(
        p['market_value'] for p in positions if p['kind'] == 'spot')
    futures_pnl = sum(
        p['unrealized_pnl_usd'] for p in positions if p['kind'] == 'futures')
    book_value = spot_market_value + futures_pnl
    cost_basis = sum(p['cost_basis'] for p in positions)
    pnl = book_value - cost_basis

    return {
        'book_value': book_value,
        'cost_basis': cost_basis,
        'unrealized_pnl_usd': pnl,
        # None, not 0, when cost_basis is 0 — a futures-only book (no cost
        # basis at all) or an empty one has an undefined percentage, not a
        # zero one. `_day_change` already returns None for the identical
        # "no valid denominator" case; a payload mixing both conventions
        # (0 here, None there) is how this class of bug survives — the
        # frontend already treats null correctly via formatPctSuffix.
        'unrealized_pnl_pct': round(pnl / cost_basis * 100, 2) if cost_basis else None,
        'spot_market_value': spot_market_value,
        'futures_unrealized_pnl': futures_pnl,
        'position_count': len(positions),
        'spot_count': sum(1 for p in positions if p['kind'] == 'spot'),
        'futures_count': sum(1 for p in positions if p['kind'] == 'futures'),
        'positions': positions,
        'excluded': excluded,
    }


def allocate(valued: list[dict]) -> dict:
    """Group valued positions for the allocation charts.

    Weights use the MAGNITUDE of each contribution. A futures position that
    is currently down still occupies space in the book; a negative donut
    slice is not a thing.
    """
    by_asset: dict[str, float] = {}
    by_kind = {'spot': 0.0, 'futures': 0.0}
    by_direction = {'long': 0.0, 'short': 0.0}

    for p in valued:
        magnitude = abs(p['book_contribution'])
        asset = p['asset']
        by_asset[asset] = by_asset.get(asset, 0.0) + magnitude
        by_kind[p['kind']] = by_kind.get(p['kind'], 0.0) + magnitude
        direction = 'short' if p['direction'] == 'short' else 'long'
        by_direction[direction] = by_direction.get(direction, 0.0) + magnitude

    total = sum(by_asset.values())
    rows = [
        {
            'asset': asset,
            'value': value,
            'weight_pct': round(value / total * 100, 2) if total else 0,
        }
        for asset, value in by_asset.items()
    ]
    rows.sort(key=lambda r: r['value'], reverse=True)

    return {'by_asset': rows, 'by_kind': by_kind, 'by_direction': by_direction}
