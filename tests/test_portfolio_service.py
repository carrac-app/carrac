import pytest

from carrac.portfolio.service import (
    SPOT_ACCOUNT_TAG,
    classify,
    exclusion_reason,
    value_position,
)


def _spot(**over):
    base = {'id': 1, 'asset': 'BTC', 'direction': 'long', 'entry_price': 50000.0,
            'size': 0.5, 'account_tag': SPOT_ACCOUNT_TAG}
    base.update(over)
    return base


def _futures(**over):
    base = {'id': 2, 'asset': 'ETH', 'direction': 'short', 'entry_price': 3000.0,
            'size': 2.0, 'account_tag': None}
    base.update(over)
    return base


def test_classify_reads_account_tag():
    assert classify(_spot()) == 'spot'
    assert classify(_futures()) == 'futures'
    assert classify({'account_tag': 'SPOT'}) == 'spot'  # case-insensitive


def test_spot_contributes_market_value_to_book():
    v = value_position(_spot(), 60000.0)
    assert v['kind'] == 'spot'
    assert v['cost_basis'] == pytest.approx(25000.0)
    assert v['market_value'] == pytest.approx(30000.0)
    assert v['unrealized_pnl_usd'] == pytest.approx(5000.0)
    # Spot contributes its full market value to the book.
    assert v['book_contribution'] == pytest.approx(30000.0)


def test_futures_contributes_only_pnl_not_notional():
    # Short 2 ETH from 3000, price falls to 2800 -> +400 profit.
    v = value_position(_futures(), 2800.0)
    assert v['kind'] == 'futures'
    # Futures never add notional to the book, and carry no cost basis.
    assert v['cost_basis'] == pytest.approx(0.0)
    assert v['market_value'] == pytest.approx(0.0)
    assert v['unrealized_pnl_usd'] == pytest.approx(400.0)
    assert v['book_contribution'] == pytest.approx(400.0)


def test_long_futures_loses_when_price_falls():
    v = value_position(_futures(direction='long'), 2800.0)
    assert v['unrealized_pnl_usd'] == pytest.approx(-400.0)
    assert v['book_contribution'] == pytest.approx(-400.0)


def test_book_contribution_minus_cost_basis_is_pnl():
    # The identity the value curve depends on: the gap between the book-value
    # line and the cost-basis line IS unrealised P&L, for both kinds.
    for trade, price in ((_spot(), 60000.0), (_futures(), 2800.0)):
        v = value_position(trade, price)
        assert v['book_contribution'] - v['cost_basis'] == pytest.approx(
            v['unrealized_pnl_usd'])


def test_unvaluable_trades_return_none_with_a_reason():
    assert value_position(_spot(size=None), 60000.0) is None
    assert exclusion_reason(_spot(size=None), 60000.0) == 'no_size'

    assert value_position(_spot(), None) is None
    assert exclusion_reason(_spot(), None) == 'no_price'

    assert value_position(_spot(size=0), 60000.0) is None
    assert exclusion_reason(_spot(size=0), 60000.0) == 'no_size'


def test_valuable_trade_has_no_exclusion_reason():
    assert exclusion_reason(_spot(), 60000.0) is None


from carrac.portfolio.service import allocate, summarize


def test_summarize_splits_book_into_its_two_parts():
    trades = [_spot(), _futures()]
    prices = {'BTC': 60000.0, 'ETH': 2800.0}
    s = summarize(trades, prices)

    assert s['spot_market_value'] == pytest.approx(30000.0)
    assert s['futures_unrealized_pnl'] == pytest.approx(400.0)
    assert s['book_value'] == pytest.approx(30400.0)
    assert s['cost_basis'] == pytest.approx(25000.0)
    assert s['unrealized_pnl_usd'] == pytest.approx(5400.0)
    assert s['unrealized_pnl_pct'] == pytest.approx(21.6)
    assert s['position_count'] == 2
    assert s['spot_count'] == 1
    assert s['futures_count'] == 1
    assert s['excluded'] == []


def test_summarize_reports_excluded_trades_rather_than_dropping_them():
    trades = [_spot(), _futures(id=9, size=None)]
    s = summarize(trades, {'BTC': 60000.0, 'ETH': 2800.0})

    # The valued total ignores it...
    assert s['book_value'] == pytest.approx(30000.0)
    assert s['position_count'] == 1
    # ...but it is reported, with a machine-readable reason.
    assert s['excluded'] == [{'id': 9, 'asset': 'ETH', 'reason': 'no_size'}]


def test_summarize_missing_price_is_an_exclusion_not_a_zero():
    s = summarize([_spot()], {})
    assert s['book_value'] == pytest.approx(0.0)
    assert s['excluded'] == [{'id': 1, 'asset': 'BTC', 'reason': 'no_price'}]


def test_summarize_empty_book_has_no_divide_by_zero():
    s = summarize([], {})
    assert s['book_value'] == 0
    # None, not 0 — an undefined percentage (no cost basis to divide by),
    # not a genuine zero return. Matches _day_change's convention for the
    # same "no valid denominator" case.
    assert s['unrealized_pnl_pct'] is None
    assert s['excluded'] == []


def test_summarize_futures_only_book_has_undefined_pnl_pct_not_zero():
    # Futures carry no cost basis (service.py: "no cost basis and no market
    # value — only mark-to-market P&L"), so a futures-only book divides by
    # zero the same way an empty book does. It must not render as a
    # confident "+0.00%" next to a real, nonzero unrealized_pnl_usd.
    s = summarize([_futures()], {'ETH': 2800.0})
    assert s['unrealized_pnl_usd'] != 0
    assert s['cost_basis'] == 0
    assert s['unrealized_pnl_pct'] is None


def test_allocation_weights_use_magnitude_so_losers_are_not_negative_slices():
    # A losing long-futures position must still occupy a donut slice.
    trades = [_spot(), _futures(direction='long')]  # futures P&L = -400
    valued = summarize(trades, {'BTC': 60000.0, 'ETH': 2800.0})['positions']
    a = allocate(valued)

    assert [row['asset'] for row in a['by_asset']] == ['BTC', 'ETH']
    assert a['by_asset'][0]['value'] == pytest.approx(30000.0)
    assert a['by_asset'][1]['value'] == pytest.approx(400.0)
    assert a['by_asset'][0]['weight_pct'] == pytest.approx(98.68, abs=0.01)
    assert sum(r['weight_pct'] for r in a['by_asset']) == pytest.approx(100.0)

    assert a['by_kind']['spot'] == pytest.approx(30000.0)
    assert a['by_kind']['futures'] == pytest.approx(400.0)
    assert a['by_direction']['long'] == pytest.approx(30400.0)
    assert a['by_direction']['short'] == pytest.approx(0.0)


def test_allocation_of_empty_book_is_empty_not_an_error():
    a = allocate([])
    assert a['by_asset'] == []
    assert a['by_kind'] == {'spot': 0.0, 'futures': 0.0}


def test_allocation_merges_multiple_lots_of_the_same_asset():
    trades = [_spot(id=1), _spot(id=2, size=0.25)]
    valued = summarize(trades, {'BTC': 60000.0})['positions']
    a = allocate(valued)
    assert len(a['by_asset']) == 1
    assert a['by_asset'][0]['value'] == pytest.approx(45000.0)
