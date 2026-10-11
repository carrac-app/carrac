"""Weekly and monthly bars must be labelled by their OPEN, like every other
timeframe and like Binance/TradingView.

pandas' default ``resample('1W')`` labels a week by its Sunday END, which
would stamp the in-progress week with a future date; the frontend live-tick
code skips future-stamped bars and would write live prices into the PREVIOUS
week's close. Labelling by open avoids both.
"""
import pandas as pd
import pytest

from database import OHLCVDatabase


def _daily(start='2026-08-17', end='2026-09-05'):
    idx = pd.date_range(start, end, freq='D', name='timestamp')
    n = len(idx)
    return pd.DataFrame({
        'open': range(n), 'high': range(n), 'low': range(n),
        'close': range(n), 'volume': [1] * n,
    }, index=idx)


@pytest.fixture
def db():
    return OHLCVDatabase.__new__(OHLCVDatabase)  # resample needs no init state


def test_weekly_bars_are_labelled_by_monday_open(db):
    w = db.resample_to_timeframe(_daily(), '1W')
    assert list(w.index.strftime('%Y-%m-%d')) == ['2026-08-17', '2026-08-24', '2026-08-31']
    assert all(w.index.dayofweek == 0), 'weekly bars must open on Monday'


def test_weekly_bar_contents_unchanged_by_label(db):
    w = db.resample_to_timeframe(_daily(), '1W')
    # Mon Aug 24 .. Sun Aug 30 are daily rows 7..13
    row = w.loc['2026-08-24']
    assert (row['open'], row['high'], row['low'], row['close'], row['volume']) == (7, 13, 7, 13, 7)


def test_monthly_bars_are_labelled_by_first_of_month(db):
    m = db.resample_to_timeframe(_daily('2026-07-01', '2026-09-05'), '1M')
    assert list(m.index.strftime('%Y-%m-%d')) == ['2026-07-01', '2026-08-01', '2026-09-01']


def test_last_htf_bar_never_stamped_in_the_future(db):
    today = pd.Timestamp('2026-09-05')
    for tf in ('1W', '1M'):
        out = db.resample_to_timeframe(_daily(end=str(today.date())), tf)
        assert out.index[-1] <= today, f'{tf} last bar {out.index[-1]} is after {today}'
