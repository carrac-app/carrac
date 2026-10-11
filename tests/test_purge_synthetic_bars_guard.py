"""The purge's exemptions, pinned.

This script deletes history. The weekend purge's dry run once proposed removing
6,784 legitimate rows because one exemption was doing all the work; these tests
exist so the same cannot happen here quietly.
"""
import pandas as pd

from scripts.purge_synthetic_bars import trailing_synthetic_rows

TODAY = pd.Timestamp('2026-08-11')


def _real(dates, base=100.0):
    return pd.DataFrame({
        'open': base, 'high': base + 2, 'low': base - 2, 'close': base + 1,
        'volume': 5000.0,
    }, index=pd.to_datetime(list(dates)))


def _synthetic(dates, price=100.0):
    return pd.DataFrame({
        'open': price, 'high': price, 'low': price, 'close': price,
        'volume': 0.0,
    }, index=pd.to_datetime(list(dates)))


def test_the_agix_shape_is_removed():
    real = _real(pd.date_range('2026-06-01', '2026-07-30'))
    tail = _synthetic(pd.date_range('2026-07-31', '2026-08-08'))
    victims = trailing_synthetic_rows(pd.concat([real, tail]), TODAY, 'AGIX')
    assert len(victims) == 9
    assert str(victims.min().date()) == '2026-07-31'
    assert str(victims.max().date()) == '2026-08-08'


def test_todays_bar_survives():
    """A real kline for the session in progress — not ours to delete."""
    real = _real(pd.date_range('2026-06-01', '2026-08-10'))
    frame = pd.concat([real, _synthetic([TODAY])])
    assert len(trailing_synthetic_rows(frame, TODAY, 'BTC')) == 0


def test_a_derived_ratio_series_is_fully_exempt():
    """Every row matches the predicate and the feed is alive. Purging it would
    delete ALTBTC and TOTAL3BTC entirely."""
    idx = pd.date_range('2026-06-01', '2026-08-10')
    frame = _synthetic(idx)
    frame['close'] = [5e-06 + i * 1e-08 for i in range(len(idx))]
    frame['open'] = frame['high'] = frame['low'] = frame['close']
    assert len(trailing_synthetic_rows(frame, TODAY, 'ALTBTC')) == 0


def test_an_interior_synthetic_row_is_never_removed():
    """Removing it would create an interior hole — the synthetic-bar defect."""
    frame = _real(pd.date_range('2026-06-01', '2026-08-10'))
    frame.iloc[20] = [100.0, 100.0, 100.0, 100.0, 0.0]
    assert len(trailing_synthetic_rows(frame, TODAY, 'BTC')) == 0


def test_an_interior_run_below_a_real_tail_is_never_removed():
    """The feed died, then came back. The gap is history now; only a run that
    reaches the end of the series is fabrication still in force."""
    frame = pd.concat([
        _real(pd.date_range('2026-06-01', '2026-06-30')),
        _synthetic(pd.date_range('2026-07-01', '2026-07-15')),
        _real(pd.date_range('2026-07-16', '2026-08-10')),
    ])
    assert len(trailing_synthetic_rows(frame, TODAY, 'BTC')) == 0


def test_a_healthy_series_loses_nothing():
    frame = _real(pd.date_range('2026-06-01', '2026-08-10'))
    assert len(trailing_synthetic_rows(frame, TODAY, 'BTC')) == 0


def test_an_empty_frame_is_safe():
    assert len(trailing_synthetic_rows(pd.DataFrame(), TODAY, 'BTC')) == 0
