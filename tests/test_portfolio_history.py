from datetime import date

import pytest

from carrac.portfolio.history import reconstruct


def _price_stub(table):
    """table: {symbol: {date: close}}"""
    def price_at(symbol, day):
        return table.get(symbol, {}).get(day)
    return price_at


def _spot(**over):
    base = {'id': 1, 'asset': 'BTC', 'direction': 'long', 'entry_price': 100.0,
            'size': 2.0, 'account_tag': 'spot',
            'opened_at': '2026-01-01T00:00:00', 'closed_at': None}
    base.update(over)
    return base


def test_curve_covers_every_day_in_the_window():
    prices = _price_stub({'BTC': {date(2026, 1, 1): 100.0,
                                  date(2026, 1, 2): 110.0,
                                  date(2026, 1, 3): 120.0}})
    curve = reconstruct([_spot()], prices, date(2026, 1, 1), date(2026, 1, 3))
    assert [r['date'] for r in curve] == ['2026-01-01', '2026-01-02', '2026-01-03']
    assert [r['book_value'] for r in curve] == pytest.approx([200.0, 220.0, 240.0])
    # Cost basis is flat while the position is simply held.
    assert [r['cost_basis'] for r in curve] == pytest.approx([200.0, 200.0, 200.0])


def test_position_absent_before_it_was_opened():
    prices = _price_stub({'BTC': {date(2026, 1, 1): 100.0, date(2026, 1, 2): 110.0}})
    trade = _spot(opened_at='2026-01-02T09:00:00')
    curve = reconstruct([trade], prices, date(2026, 1, 1), date(2026, 1, 2))
    assert curve[0]['book_value'] == pytest.approx(0.0)
    assert curve[1]['book_value'] == pytest.approx(220.0)


def test_position_absent_after_it_closed():
    prices = _price_stub({'BTC': {date(2026, 1, 1): 100.0, date(2026, 1, 2): 110.0}})
    trade = _spot(closed_at='2026-01-01T18:00:00')
    curve = reconstruct([trade], prices, date(2026, 1, 1), date(2026, 1, 2))
    assert curve[0]['book_value'] == pytest.approx(200.0)
    assert curve[1]['book_value'] == pytest.approx(0.0)


def test_futures_contributes_only_pnl_to_the_curve():
    prices = _price_stub({'ETH': {date(2026, 1, 1): 3000.0, date(2026, 1, 2): 2800.0}})
    short = {'id': 2, 'asset': 'ETH', 'direction': 'short', 'entry_price': 3000.0,
             'size': 2.0, 'account_tag': None,
             'opened_at': '2026-01-01T00:00:00', 'closed_at': None}
    curve = reconstruct([short], prices, date(2026, 1, 1), date(2026, 1, 2))
    assert [r['book_value'] for r in curve] == pytest.approx([0.0, 400.0])
    assert [r['cost_basis'] for r in curve] == pytest.approx([0.0, 0.0])


def test_missing_bar_carries_the_last_known_close_forward():
    # Weekend / holiday gap: 1 Jan then nothing until 3 Jan.
    prices = _price_stub({'BTC': {date(2026, 1, 1): 100.0, date(2026, 1, 3): 130.0}})
    curve = reconstruct([_spot()], prices, date(2026, 1, 1), date(2026, 1, 3))
    assert [r['book_value'] for r in curve] == pytest.approx([200.0, 200.0, 260.0])
    assert [r['excluded_count'] for r in curve] == [0, 0, 0]


def test_no_prior_close_excludes_the_position_and_says_so():
    # The bar only appears on day 3 — days 1 and 2 have nothing to carry.
    prices = _price_stub({'BTC': {date(2026, 1, 3): 130.0}})
    curve = reconstruct([_spot()], prices, date(2026, 1, 1), date(2026, 1, 3))
    assert [r['book_value'] for r in curve] == pytest.approx([0.0, 0.0, 260.0])
    assert [r['excluded_count'] for r in curve] == [1, 1, 0]


def test_trade_without_size_is_excluded_on_every_day_it_was_open():
    prices = _price_stub({'BTC': {date(2026, 1, 1): 100.0, date(2026, 1, 2): 110.0}})
    curve = reconstruct([_spot(size=None)], prices, date(2026, 1, 1), date(2026, 1, 2))
    assert [r['book_value'] for r in curve] == pytest.approx([0.0, 0.0])
    assert [r['excluded_count'] for r in curve] == [1, 1]


def test_empty_book_yields_a_flat_zero_curve_not_an_empty_list():
    curve = reconstruct([], _price_stub({}), date(2026, 1, 1), date(2026, 1, 2))
    assert [r['book_value'] for r in curve] == [0.0, 0.0]
    assert len(curve) == 2


def test_zero_price_is_real_data_not_missing():
    # A delisted or worthless asset priced at exactly 0.0 is legitimately valued,
    # not treated as missing data. Spot with entry=100, size=2 at price=0.0 has
    # book_value=0.0 (not excluded). This distinguishes it from no price at all.
    prices = _price_stub({'BTC': {date(2026, 1, 1): 0.0, date(2026, 1, 2): 50.0}})
    curve = reconstruct([_spot()], prices, date(2026, 1, 1), date(2026, 1, 2))
    # Day 1: price is 0.0, so book_value = size * price = 2.0 * 0.0 = 0.0
    # Day 2: price is 50.0, so book_value = 2.0 * 50.0 = 100.0
    assert [r['book_value'] for r in curve] == pytest.approx([0.0, 100.0])
    # Neither day should have exclusions — both have valid prices.
    assert [r['excluded_count'] for r in curve] == [0, 0]
