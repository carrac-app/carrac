# tests/test_portfolio_live_payload.py
"""The live-positions payload must carry size, and must not change the
meaning of the published portfolio_heat.total_exposure field."""

import carrac.composite.assemblers as asm


def _patch(monkeypatch, trades, prices):
    monkeypatch.setattr(asm, '_get_content_db', lambda: type(
        'DB', (), {'get_open_trades': lambda self, uid, asset=None: trades})())
    monkeypatch.setattr(asm, '_get_prices', lambda: prices)
    # _get_scanner_by_symbol takes an optional `symbols` arg (assemblers.py::_get_scanner_by_symbol).
    monkeypatch.setattr(asm, '_get_scanner_by_symbol', lambda symbols=None: {})


def test_size_and_dollar_fields_reach_the_payload(monkeypatch):
    trades = [{'id': 1, 'asset': 'BTC', 'direction': 'long', 'entry_price': 50000.0,
               'stop_price': 48000.0, 'size': 0.5, 'account_tag': 'spot',
               'opened_at': '2026-01-01T00:00:00'}]
    _patch(monkeypatch, trades, {'BTC': {'price': 60000.0}})

    row = asm.assemble_live_positions(1)['positions'][0]
    assert row['size'] == 0.5
    assert row['account_tag'] == 'spot'
    assert row['notional'] == 30000.0
    assert row['cost_basis'] == 25000.0
    assert row['unrealized_pnl_usd'] == 5000.0


def test_existing_fields_are_untouched(monkeypatch):
    trades = [{'id': 1, 'asset': 'BTC', 'direction': 'long', 'entry_price': 50000.0,
               'stop_price': 48000.0, 'size': 0.5, 'account_tag': 'spot',
               'drawing_id': 12345, 'opened_at': '2026-01-01T00:00:00'}]
    _patch(monkeypatch, trades, {'BTC': {'price': 60000.0}})

    row = asm.assemble_live_positions(1)['positions'][0]
    for key in ('id', 'asset', 'direction', 'entry_price', 'current_price',
                'stop_price', 'unrealized_pnl_pct', 'current_rr', 'risk_status'):
        assert key in row, key
    assert row['unrealized_pnl_pct'] == 20.0
    # CARRAC-portfolio: promoted spot buys carry the drawing they came from,
    # so the sell path can close that trade rather than logging a new one.
    assert row['drawing_id'] == 12345


def test_total_exposure_keeps_its_published_count_meaning(monkeypatch):
    trades = [
        {'id': 1, 'asset': 'BTC', 'direction': 'long', 'entry_price': 50000.0,
         'size': 0.5, 'account_tag': 'spot', 'opened_at': '2026-01-01T00:00:00'},
        {'id': 2, 'asset': 'ETH', 'direction': 'short', 'entry_price': 3000.0,
         'size': 2.0, 'opened_at': '2026-01-01T00:00:00'},
    ]
    _patch(monkeypatch, trades, {'BTC': {'price': 60000.0}, 'ETH': {'price': 2800.0}})

    heat = asm.assemble_live_positions(1)['portfolio_heat']
    # Still a COUNT — MCP + OpenAPI depend on this being an integer count.
    assert heat['total_exposure'] == 2
    assert heat['long_count'] == 1
    assert heat['short_count'] == 1
    # The real dollar figure arrives as a NEW field.
    assert heat['notional_usd'] == 30000.0 + 5600.0


def test_a_trade_without_size_still_returns_a_row_with_nulls(monkeypatch):
    trades = [{'id': 1, 'asset': 'BTC', 'direction': 'long', 'entry_price': 50000.0,
               'size': None, 'opened_at': '2026-01-01T00:00:00'}]
    _patch(monkeypatch, trades, {'BTC': {'price': 60000.0}})

    row = asm.assemble_live_positions(1)['positions'][0]
    assert row['size'] is None
    assert row['notional'] is None
    assert row['unrealized_pnl_usd'] is None
    # The percentage view still works without size.
    assert row['unrealized_pnl_pct'] == 20.0


def test_zero_open_trades_still_has_the_full_heat_shape(monkeypatch):
    """A flat user (new signup, or anyone with no open positions) must get the
    same portfolio_heat shape as everyone else — including notional_usd — not
    a payload a naive `heat['notional_usd']` consumer would KeyError on."""
    _patch(monkeypatch, [], {})

    result = asm.assemble_live_positions(1)
    assert result['positions'] == []
    heat = result['portfolio_heat']
    assert heat['total_exposure'] == 0
    assert heat['long_count'] == 0
    assert heat['short_count'] == 0
    assert heat['notional_usd'] == 0


def test_notional_usd_sums_only_trades_with_a_valued_notional(monkeypatch):
    """Mixed book: one trade has size, one doesn't. notional_usd must sum only
    the valued row, not blow up or silently include the None row as 0 via a
    truthiness filter that would also (wrongly) exclude a real 0.0 notional."""
    trades = [
        {'id': 1, 'asset': 'BTC', 'direction': 'long', 'entry_price': 50000.0,
         'size': 0.5, 'opened_at': '2026-01-01T00:00:00'},
        {'id': 2, 'asset': 'ETH', 'direction': 'long', 'entry_price': 3000.0,
         'size': None, 'opened_at': '2026-01-01T00:00:00'},
    ]
    _patch(monkeypatch, trades, {'BTC': {'price': 60000.0}, 'ETH': {'price': 3100.0}})

    result = asm.assemble_live_positions(1)
    rows = {r['asset']: r for r in result['positions']}
    assert rows['BTC']['notional'] == 30000.0
    assert rows['ETH']['notional'] is None
    # Only the BTC row's notional is counted.
    assert result['portfolio_heat']['notional_usd'] == 30000.0
