"""The report delta header must price Carrac's own ratio symbols via the ratio
builder, not by handing the combined token to get_ohlcv_data (the
equity guard refuses e.g. "GOLD_BTC" as a bogus ticker on every report view —
the 205-event the error tracker issue).
"""
from unittest.mock import MagicMock

import pandas as pd

from carrac.reports import routes as reports_routes


class _DB:
    def __init__(self):
        self.asked = []

    def get_ohlcv_data(self, asset, limit=None):
        self.asked.append(asset)
        if '_' in asset:
            raise RuntimeError('equity-path refused')
        return pd.DataFrame({'close': [101.0]})


def test_ratio_symbol_is_priced_by_the_ratio_builder(monkeypatch):
    seen = {}

    def fake_ratio(num, den, tf):
        seen['args'] = (num, den, tf)
        return pd.DataFrame({'close': [0.25, 0.5]})

    monkeypatch.setattr(reports_routes, '_build_ratio_df', fake_ratio)
    db = _DB()
    price = reports_routes._live_price_for_delta('GOLD_BTC', {}, db)
    assert price == 0.5
    assert seen['args'] == ('GOLD', 'BTC', '1D')
    assert db.asked == [], 'the combined token must never reach get_ohlcv_data'


def test_plain_symbol_still_uses_ohlcv(monkeypatch):
    db = _DB()
    assert reports_routes._live_price_for_delta('AAPL', {}, db) == 101.0
    assert db.asked == ['AAPL']


def test_cached_crypto_price_wins():
    db = _DB()
    assert reports_routes._live_price_for_delta('BTC', {'BTC': {'price': 5.0}}, db) == 5.0
    assert db.asked == []


def _mtf_df():
    return pd.DataFrame({
        'open': [1.0, 1.0], 'high': [1.0, 1.0], 'low': [1.0, 1.0],
        'close': [1.0, 1.0], 'volume': [1.0, 1.0],
    })


class _BoardDB(_DB):
    """Same equity-path-refusal behaviour as _DB, plus the multi-timeframe
    surface build_morning_board's ratio machinery needs."""

    def get_multi_timeframe_data(self, symbol):
        return {'1D': _mtf_df(), '3D': _mtf_df(), '1W': _mtf_df()}

    def resample_to_timeframe(self, df, tf):
        return df

    def load_previous_snapshot(self):
        return {}


def test_build_morning_board_prices_loop_never_sends_a_ratio_token_to_ohlcv(monkeypatch):
    """build_morning_board()'s PRICES loop (not just the
    delta-header helper) must route ratio entries through the ratio builder,
    not db.get_ohlcv_data — that trips the equity-path refusal guard from the
    worker every ~30 min for every one of the 8 ratio entries.
    """
    db = _BoardDB()
    cons = MagicMock()
    cons.calculate_multi_timeframe.return_value = {
        '1D': {'signal': 1}, '3D': {'signal': 1}, '1W': {'signal': 1},
        'total': {'positions': 2},
    }
    monkeypatch.setattr(reports_routes, '_get_db', lambda: db)
    monkeypatch.setattr('carrac.charts.ratios._get_db', lambda: db)
    monkeypatch.setattr(reports_routes, '_get_consensio', lambda: cons)
    monkeypatch.setattr(reports_routes, '_get_cached_crypto_prices', lambda: {})

    board = reports_routes.build_morning_board()

    ratio_symbols = {a['symbol'] for a in reports_routes.MORNING_ASSETS if a.get('ratio')}
    assert ratio_symbols, 'sanity: MORNING_ASSETS must still carry ratio entries'

    bad = [sym for sym in db.asked if '_' in sym]
    assert not bad, f"ratio token(s) reached get_ohlcv_data / the equity path: {bad}"

    for sym in ratio_symbols:
        assert sym in board['prices'], f"{sym} never got priced by the ratio builder"
