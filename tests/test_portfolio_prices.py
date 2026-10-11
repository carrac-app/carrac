from datetime import date

import pandas as pd
import pytest

from carrac.portfolio.prices import build_price_lookup


def _frame(rows, column='close'):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in rows])
    return pd.DataFrame({column: [c for _, c in rows]}, index=idx)


def test_looks_up_a_close_by_calendar_day():
    frames = {'BTC': _frame([('2026-01-01', 100.0), ('2026-01-02', 110.0)])}
    price_at = build_price_lookup(['BTC'], loader=frames.get)
    assert price_at('BTC', date(2026, 1, 1)) == pytest.approx(100.0)
    assert price_at('BTC', date(2026, 1, 2)) == pytest.approx(110.0)


def test_absent_day_returns_none_so_the_caller_can_carry_forward():
    frames = {'BTC': _frame([('2026-01-01', 100.0)])}
    price_at = build_price_lookup(['BTC'], loader=frames.get)
    assert price_at('BTC', date(2026, 1, 5)) is None


def test_symbol_missing_from_cache_yields_none_rather_than_raising():
    price_at = build_price_lookup(['NOPE'], loader=lambda s: None)
    assert price_at('NOPE', date(2026, 1, 1)) is None


def test_symbol_lookup_is_case_insensitive():
    frames = {'BTC': _frame([('2026-01-01', 100.0)])}
    price_at = build_price_lookup(['btc'], loader=frames.get)
    assert price_at('btc', date(2026, 1, 1)) == pytest.approx(100.0)
    assert price_at('BTC', date(2026, 1, 1)) == pytest.approx(100.0)


def test_uppercase_close_column_is_accepted():
    frames = {'BTC': _frame([('2026-01-01', 100.0)], column='Close')}
    price_at = build_price_lookup(['BTC'], loader=frames.get)
    assert price_at('BTC', date(2026, 1, 1)) == pytest.approx(100.0)


def test_each_symbol_is_loaded_exactly_once():
    calls = []

    def loader(symbol):
        calls.append(symbol)
        return _frame([('2026-01-01', 100.0)])

    price_at = build_price_lookup(['BTC', 'BTC'], loader=loader)
    price_at('BTC', date(2026, 1, 1))
    price_at('BTC', date(2026, 1, 1))
    assert calls == ['BTC']


def test_zero_close_is_real_data_not_missing():
    frames = {'BTC': _frame([('2026-01-01', 0.0)])}
    price_at = build_price_lookup(['BTC'], loader=frames.get)
    result = price_at('BTC', date(2026, 1, 1))
    assert result == 0.0
    assert result is not None


def test_a_broken_frame_degrades_to_no_prices_not_an_exception():
    price_at = build_price_lookup(['BTC'], loader=lambda s: object())
    assert price_at('BTC', date(2026, 1, 1)) is None
