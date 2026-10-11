"""A blank morning board must be loud.

build_morning_board() feeds the paid report page AND the public /brief,
and the worker caches its result for ~30 minutes as a successful warm.
Outer `except Exception: pass` blocks meant a cache-layer or DB failure
blanked the whole prices/consensio dict, the empty board was cached, and
both pages rendered without data — unlogged, for 30 minutes at a time.

The function must still never raise (a throw 500s the report page on the
cold-cache fallback path), so the contract is: degrade, but alarm.
"""
from unittest.mock import MagicMock

import pandas as pd

import carrac.reports.routes as rt


def _df():
    return pd.DataFrame({'close': [1.0, 2.0]})


def _healthy(monkeypatch):
    db = MagicMock()
    db.get_ohlcv_data.return_value = _df()
    db.get_multi_timeframe_data.return_value = {'1D': _df(), '3D': _df(), '1W': _df()}
    db.load_previous_snapshot.return_value = {}
    cons = MagicMock()
    cons.calculate_multi_timeframe.return_value = {
        '1D': {'signal': 1}, '3D': {'signal': 1}, '1W': {'signal': 1},
        'total': {'positions': 2},
    }
    monkeypatch.setattr(rt, '_get_db', lambda: db)
    monkeypatch.setattr(rt, '_get_consensio', lambda: cons)
    monkeypatch.setattr(rt, '_get_cached_crypto_prices', lambda: {})
    monkeypatch.setattr(rt, '_compute_ratio_consensio', lambda s: {'1D': 1})


def test_a_blanked_board_is_beaconed_but_still_returned(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError('simulated cache-layer outage')
    db = MagicMock()
    db.get_ohlcv_data.side_effect = _boom
    db.get_multi_timeframe_data.side_effect = _boom
    db.load_previous_snapshot.side_effect = _boom
    monkeypatch.setattr(rt, '_get_db', lambda: db)
    monkeypatch.setattr(rt, '_get_consensio', lambda: MagicMock())
    monkeypatch.setattr(rt, '_get_cached_crypto_prices', _boom)
    monkeypatch.setattr(rt, '_compute_ratio_consensio', lambda s: None)
    beacons = []
    monkeypatch.setattr(rt, '_board_beacon', lambda msg: beacons.append(msg))

    board = rt.build_morning_board()

    assert board == {'prices': {}, 'consensio': {}}, (
        'the function must degrade, not raise — a throw 500s the report page'
    )
    assert beacons, (
        'a blanked board was cached for 30 minutes with no alarm — this is '
        'the unlogged blank this alarm exists for'
    )
    assert 'prices=0' in beacons[-1] and 'consensio=0' in beacons[-1]


def test_a_healthy_board_raises_no_alarm(monkeypatch):
    _healthy(monkeypatch)
    beacons = []
    monkeypatch.setattr(rt, '_board_beacon', lambda msg: beacons.append(msg))

    board = rt.build_morning_board()

    assert board['prices'] and board['consensio']
    assert not beacons, 'a healthy build must stay silent — alarms that cry wolf get ignored'
