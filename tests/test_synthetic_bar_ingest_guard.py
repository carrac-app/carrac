"""A synthetic bar must never be persisted as HISTORY.

The read guard (`_last_real_bar`) stops a fabricated tail from being published.
This is the other half: stop it landing on disk in the first place, so the
residue cannot rebuild itself after the purge.

The distinction that makes this safe is the DATE:

* TODAY's flat zero-volume row is legitimate. At 00:00 UTC a real exchange
  kline for the new session genuinely is open=prev close, H=L=C=open, volume 0
  — BTC_daily.csv carries exactly that every night and it is corrected within
  the day. Dropping it would throw away real data.
* A flat zero-volume row dated in the PAST is not a session in progress. It is
  either an injected candle that got persisted, or a dead upstream pair
  repeating its last price. Neither is history.

Crypto is NOT exempt here (unlike the weekend guard): the five symbols this
was found on — AGIX, EOS, COLLAT, MKR, FTM — are all crypto.
"""
import logging

import pandas as pd
import pytest

import csv_cache
from csv_cache import CSVCache


def _real(dates, base=100.0):
    idx = pd.to_datetime(list(dates))
    return pd.DataFrame({
        'open': base, 'high': base + 2, 'low': base - 2, 'close': base + 1,
        'volume': 5000.0,
    }, index=idx)


def _synthetic(dates, price=100.0):
    idx = pd.to_datetime(list(dates))
    return pd.DataFrame({
        'open': price, 'high': price, 'low': price, 'close': price,
        'volume': 0.0,
    }, index=idx)


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def setex(self, key, ttl, value):
        self.store[key] = (ttl, value)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(csv_cache, '_get_redis', lambda: r)
    return r


@pytest.fixture
def today():
    return pd.Timestamp.today().normalize()


def test_a_past_dated_synthetic_row_is_not_written(tmp_path, fake_redis, today, caplog):
    """The AGIX mechanism: one fabricated row a day, forever."""
    caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
    cache = CSVCache(cache_dir=str(tmp_path))
    # Stand-in symbol. AGIX names the mechanism but is now curated-delisted, so
    # CSVCache.save refuses its writes before this guard is reached
    # The rule under test is not symbol-specific.
    history = _real(pd.date_range(end=today - pd.Timedelta(days=3), periods=40))
    cache.save('SOL', history, is_crypto=True)

    poisoned = pd.concat([history, _synthetic([today - pd.Timedelta(days=1)])])
    cache.save('SOL', poisoned, is_crypto=True)

    on_disk = cache.load('SOL')
    assert (today - pd.Timedelta(days=1)) not in on_disk.index, (
        "a flat zero-volume bar dated yesterday was persisted as history"
    )
    assert any('synthetic' in r.message.lower() for r in caplog.records), (
        "the drop must be loud enough to reach Bugsink — silence is how "
        "AGIX accumulated 761 of these unnoticed"
    )


def test_todays_synthetic_row_is_kept(tmp_path, fake_redis, today):
    """A real exchange kline for the session in progress looks exactly like a
    fabricated bar. BTC carries one every night; dropping it loses real data."""
    cache = CSVCache(cache_dir=str(tmp_path))
    history = _real(pd.date_range(end=today - pd.Timedelta(days=1), periods=40))
    frame = pd.concat([history, _synthetic([today])])
    cache.save('BTC', frame, is_crypto=True)

    assert today in cache.load('BTC').index, "today's opening kline was discarded"


def test_a_derived_ratio_series_is_written_untouched(tmp_path, fake_redis, today):
    """ALTBTC/TOTAL3BTC publish one computed value a day as OHLC with volume 0.
    Every row matches the predicate; the series is alive. Dropping any of it
    would delete the entire feed."""
    cache = CSVCache(cache_dir=str(tmp_path))
    idx = pd.date_range(end=today, periods=40)
    frame = _synthetic(idx)
    frame['close'] = [5e-06 + i * 1e-08 for i in range(len(idx))]
    frame['open'] = frame['high'] = frame['low'] = frame['close']
    cache.save('ALTBTC', frame, is_crypto=True)

    assert len(cache.load('ALTBTC')) == len(frame), (
        "a derived ratio feed was mistaken for fabrication and deleted"
    )


def test_synthetic_rows_already_on_disk_are_left_alone(tmp_path, fake_redis, today):
    """Only NEW rows are refused. Removing accumulated residue is the purge
    script's job, run by hand after a dry run — never a side effect of a save."""
    cache = CSVCache(cache_dir=str(tmp_path))
    history = _real(pd.date_range(end=today - pd.Timedelta(days=5), periods=40))
    residue = _synthetic(pd.date_range(end=today - pd.Timedelta(days=1), periods=4))
    seeded = pd.concat([history, residue])
    seeded.to_csv(cache.get_path('AGIX', 'daily'))

    cache.save('AGIX', seeded, is_crypto=True)

    on_disk = cache.load('AGIX')
    assert len(on_disk) == len(seeded), (
        "an unrelated save quietly deleted accumulated history"
    )


def test_a_flat_bar_with_real_volume_is_kept(tmp_path, fake_redis, today):
    """A genuine no-range session. One signal is not enough to call it fake."""
    cache = CSVCache(cache_dir=str(tmp_path))
    history = _real(pd.date_range(end=today - pd.Timedelta(days=2), periods=40))
    flat = _synthetic([today - pd.Timedelta(days=1)])
    flat['volume'] = 1234.0
    cache.save('SOMECOIN', pd.concat([history, flat]), is_crypto=True)

    assert (today - pd.Timedelta(days=1)) in cache.load('SOMECOIN').index


def test_an_interior_synthetic_row_is_left_alone(tmp_path, fake_redis, today):
    """Dropping an interior row would punch a HOLE in the series — the
    synthetic-bar defect, caused while fixing an earlier one. The accumulation
    mechanism only ever appends, so only a trailing run is ever refused.
    """
    cache = CSVCache(cache_dir=str(tmp_path))
    idx = pd.date_range(end=today - pd.Timedelta(days=1), periods=40)
    frame = _real(idx)
    # one flat, volumeless bar in the middle of otherwise real history
    frame.iloc[20] = [100.0, 100.0, 100.0, 100.0, 0.0]

    cache.save('SOMECOIN', frame, is_crypto=True)

    on_disk = cache.load('SOMECOIN')
    assert len(on_disk) == len(frame), "an interior row was dropped, creating a gap"
    assert idx[20] in on_disk.index
