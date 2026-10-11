"""Route wiring: the strangler list, the cache policy, and day-change
being anchored to the curve rather than to per-asset 24h moves."""

from datetime import date, timedelta

import pytest

import carrac.asgi as asgi_mod
from carrac.cache_headers import get_api_cache_header


def test_portfolio_paths_are_registered_with_the_strangler():
    # A FastAPI route absent from MIGRATED_PATHS is never reached.
    assert '/api/portfolio/summary' in asgi_mod.MIGRATED_PATHS
    assert '/api/portfolio/history' in asgi_mod.MIGRATED_PATHS


def test_portfolio_paths_have_a_private_cache_policy():
    assert 'private' in get_api_cache_header('/api/portfolio/summary', 'GET')
    assert 'private' in get_api_cache_header('/api/portfolio/history', 'GET')


def test_router_exposes_both_endpoints():
    from carrac.fastapi_app.routers import portfolio
    routes = {r.path for r in portfolio.router.routes}
    assert routes == {'/api/portfolio/summary', '/api/portfolio/history'}


def test_router_is_included_in_the_app():
    # Under FastAPI 0.138, fastapi_app.routes is mostly _IncludedRouter proxies
    # with no `.path` attribute (see tests/test_strangler_guard.py) — a
    # plain {r.path for r in ...} comprehension AttributeErrors on those before
    # it can even check membership. Use Starlette's own matcher instead, same
    # fix already established in that file.
    from starlette.routing import Match
    from carrac.fastapi_app.main import fastapi_app

    def _served(path: str) -> bool:
        scope = {'type': 'http', 'path': path, 'method': 'GET',
                  'headers': [], 'query_string': b'', 'root_path': ''}
        return any(r.matches(scope)[0] == Match.FULL for r in fastapi_app.routes)

    assert _served('/api/portfolio/summary')
    assert _served('/api/portfolio/history')


def test_day_change_is_book_value_minus_previous_close(monkeypatch):
    from carrac.fastapi_app.routers import portfolio

    curve = [{'date': '2026-01-01', 'book_value': 200.0, 'cost_basis': 200.0,
              'excluded_count': 0},
             {'date': '2026-01-02', 'book_value': 220.0, 'cost_basis': 200.0,
              'excluded_count': 0}]
    change = portfolio._day_change(book_value=240.0, curve=curve, excluded=[])
    assert change == (20.0, 9.09)


def test_day_change_is_null_when_there_is_no_prior_day():
    from carrac.fastapi_app.routers import portfolio
    assert portfolio._day_change(
        book_value=240.0, curve=[], excluded=[]) == (None, None)


def test_day_change_avoids_divide_by_zero_on_an_empty_prior_book():
    from carrac.fastapi_app.routers import portfolio
    curve = [{'date': '2026-01-01', 'book_value': 0.0, 'cost_basis': 0.0,
              'excluded_count': 0}]
    assert portfolio._day_change(
        book_value=240.0, curve=curve, excluded=[]) == (240.0, None)


def test_day_change_is_unknown_when_yesterdays_point_excluded_a_position():
    # Today is priced live from the crypto cache, yesterday from the CSV
    # daily cache — independent coverage gaps. If yesterday's point
    # couldn't value every position, its book_value understates the true
    # prior book (0 in the worst case), and comparing today's full book
    # against that partial number would report the entire book as one
    # day's move. Must come back unknown, not a large wrong number.
    from carrac.fastapi_app.routers import portfolio
    curve = [{'date': '2026-01-01', 'book_value': 0.0, 'cost_basis': 0.0,
              'excluded_count': 1}]
    assert portfolio._day_change(
        book_value=50000.0, curve=curve, excluded=[]) == (None, None)


def test_day_change_is_unknown_when_todays_summary_excluded_a_position():
    # The other direction. The guard above covers a gap in
    # YESTERDAY's curve point. The same error arrives from the summary side:
    # if today cannot price a position but yesterday's curve could, today's
    # book drops by that position's full value against a complete baseline
    # and the whole holding is reported as one day's move.
    from carrac.fastapi_app.routers import portfolio
    curve = [{'date': '2026-01-01', 'book_value': 50000.0,
              'cost_basis': 40000.0, 'excluded_count': 0}]
    excluded = [{'asset': 'NVDA', 'reason': 'no_price'}]
    assert portfolio._day_change(
        book_value=10000.0, curve=curve, excluded=excluded) == (None, None)


def test_day_change_is_computed_when_both_sides_are_complete():
    from carrac.fastapi_app.routers import portfolio
    curve = [{'date': '2026-01-01', 'book_value': 200.0, 'cost_basis': 200.0,
              'excluded_count': 0}]
    assert portfolio._day_change(
        book_value=240.0, curve=curve, excluded=[]) == (40.0, 20.0)


def test_summary_route_passes_the_exclusion_list_to_the_guard():
    """What calls this — a guard wired to nothing is decoration.

    Pins the call site: the route must hand summary['excluded'] to
    _day_change, not just compute it and drop it on the floor (which is
    exactly what it did before).
    """
    import inspect
    from carrac.fastapi_app.routers import portfolio

    source = inspect.getsource(portfolio.portfolio_summary)
    assert "_day_change(" in source
    assert "summary['excluded']" in source or 'summary["excluded"]' in source


# ---------------------------------------------------------------------------
# _load — closed-trade window correctness
#
# get_trade_history's WHERE opened_at > cutoff silently drops a trade that
# was opened long before the requested window but closed inside it, even
# though it was open — and contributing to book value — for part of the
# window. There is no finite opened_at cutoff that is safe (a position open
# at `start` could have been opened arbitrarily long ago), so _load must
# fetch closed trades by closed_at instead. These tests pin that with a fake
# DB standing in for carrac.content_db, so they run without a real
# database.
# ---------------------------------------------------------------------------

class _FakeDB:
    """Minimal drawing-db stand-in for _load-level tests."""

    def __init__(self, open_trades=None, closed_trades=None):
        self._open = open_trades or []
        self._closed = closed_trades or []

    def get_open_trades(self, user_id):
        return self._open

    def get_closed_trades_since(self, user_id, since, limit=5000):
        return self._closed[:limit]

    def get_trade_history(self, *args, **kwargs):
        # _load must not fall back to this: it's opened_at-bounded and would
        # silently drop exactly the trade these tests are pinning.
        raise AssertionError(
            'get_trade_history is opened_at-bounded and must not be used by '
            '_load — use get_closed_trades_since instead'
        )


def _flat_price_lookup(symbols, loader=None):
    def price_at(symbol, day):
        return 120.0
    return price_at


def test_load_includes_a_trade_opened_long_before_the_window_but_closed_inside_it(monkeypatch):
    from carrac.fastapi_app.routers import portfolio
    import carrac.composite.assemblers as asm

    today = date.today()
    opened_at = (today - timedelta(days=400)).isoformat() + 'T00:00:00'
    closed_at = (today - timedelta(days=10)).isoformat() + 'T00:00:00'
    trade = {'id': 1, 'asset': 'BTC', 'direction': 'long', 'entry_price': 100.0,
              'size': 2.0, 'account_tag': 'spot',
              'opened_at': opened_at, 'closed_at': closed_at}

    monkeypatch.setattr(asm, '_get_content_db', lambda: _FakeDB(closed_trades=[trade]))
    monkeypatch.setattr(asm, '_get_prices', lambda: {})
    monkeypatch.setattr('carrac.portfolio.prices.build_price_lookup', _flat_price_lookup)

    loaded = portfolio._load(user_id=1, days=90)

    close_day = today - timedelta(days=10)
    still_open_day = (close_day - timedelta(days=1)).isoformat()
    row = next(r for r in loaded['curve'] if r['date'] == still_open_day)
    # size(2.0) * flat price(120.0) — the trade must be present and valued
    # on a day it was genuinely open, despite having been opened 400 days
    # before the 90-day window even starts.
    assert row['book_value'] == pytest.approx(240.0)


def test_load_marks_truncated_when_the_closed_trade_cap_is_hit(monkeypatch):
    from carrac.fastapi_app.routers import portfolio
    import carrac.composite.assemblers as asm

    template = {'asset': 'BTC', 'direction': 'long', 'entry_price': 1.0,
                'size': 1.0, 'account_tag': 'spot',
                'opened_at': '2020-01-01T00:00:00', 'closed_at': '2026-01-01T00:00:00'}
    closed = [dict(template, id=i) for i in range(portfolio.MAX_CLOSED_TRADES)]

    monkeypatch.setattr(asm, '_get_content_db', lambda: _FakeDB(closed_trades=closed))
    monkeypatch.setattr(asm, '_get_prices', lambda: {})
    monkeypatch.setattr('carrac.portfolio.prices.build_price_lookup', _flat_price_lookup)

    loaded = portfolio._load(user_id=1, days=90)
    assert loaded['truncated'] is True


def test_load_is_not_truncated_under_the_cap(monkeypatch):
    from carrac.fastapi_app.routers import portfolio
    import carrac.composite.assemblers as asm

    monkeypatch.setattr(asm, '_get_content_db', lambda: _FakeDB(closed_trades=[]))
    monkeypatch.setattr(asm, '_get_prices', lambda: {})
    monkeypatch.setattr('carrac.portfolio.prices.build_price_lookup', _flat_price_lookup)

    loaded = portfolio._load(user_id=1, days=90)
    assert loaded['truncated'] is False
