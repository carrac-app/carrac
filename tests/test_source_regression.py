"""A source that stops answering must be visible.

Example failure: Yahoo's ``CL=F`` returns exactly ONE row for any range asked,
while ``BZ=F``, ``GC=F`` and ``NG=F`` all return ~500 in the same run. Every
fetch succeeds, the one row is recent, the cache mtime keeps advancing. OIL is in ``WRONG_SCALE_ON_STOOQ``, so Yahoo is not a fallback
for it — it is the only source — and OIL is on the public brief.

Two guards, because that failure leaves two different marks:

1. ``check_fixed_range_fetch`` — a fixed-range request that comes back far
   smaller than last time is a SOURCE regression, whatever the data looks like.
2. ``CSVCache._reject_zero_ohlc_bars`` — the bar Yahoo actually served for
   OIL, ``0 / 0 / 0 / 87.06``, which matches no flat-bar guard because
   it is partially missing rather than flat.

Nothing here monkeypatches the unit under test: Redis and the alarm are both
injected at their boundaries.
"""
import datetime
import logging

import numpy as np
import pandas as pd
import pytest

from csv_cache import CSVCache
from data_sources import source_health


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.fixture
def redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(source_health, '_get_redis', lambda: r)
    return r


@pytest.fixture
def pages():
    """Captures what would have been sent, without stubbing the check itself."""
    return []


def _check(rows, pages, **kw):
    return source_health.check_fixed_range_fetch(
        'yahoo', 'OIL', 'daily:2y', rows,
        alarm=lambda *a: pages.append(a), **kw)


class TestFixedRangeFetchGuard:
    def test_a_row_count_collapse_from_504_to_1_pages(self, redis, pages):
        """504 rows, then 1."""
        assert _check(504, pages) is False, 'the first fetch has no baseline'
        assert pages == []

        assert _check(1, pages) is True
        assert len(pages) == 1
        assert pages[0][3] == 1 and pages[0][4] == 504

    def test_a_normal_fetch_is_silent(self, redis, pages):
        _check(504, pages)
        for rows in (504, 503, 505, 500):
            assert _check(rows, pages) is False
        assert pages == []

    def test_it_pages_once_at_the_transition_not_every_cycle(self, redis, pages):
        """The baseline is the PREVIOUS fetch, not an all-time high. A source
        that stays collapsed compares 1 against 1 and goes quiet — an all-time
        high-water mark would page forever the day a source legitimately
        shortens its history, which is the alarm-fatigue shape."""
        _check(504, pages)
        _check(1, pages)
        for _ in range(5):
            _check(1, pages)
        assert len(pages) == 1

    def test_recovery_is_silent_and_rearms(self, redis, pages):
        _check(504, pages)
        _check(1, pages)
        assert _check(504, pages) is False, 'a recovery must not page'
        assert len(pages) == 1

        _check(1, pages)
        assert len(pages) == 2, 'a relapse must page again'

    def test_a_short_series_has_no_baseline_to_lose(self, redis, pages):
        """A newly listed instrument legitimately has a handful of bars.
        Calling that a collapse would page on every new symbol."""
        _check(5, pages)
        _check(1, pages)
        assert pages == []

    def test_no_redis_degrades_to_silence_not_to_noise(self, monkeypatch, pages):
        monkeypatch.setattr(source_health, '_get_redis', lambda: None)
        assert _check(504, pages) is False
        assert _check(1, pages) is False
        assert pages == []

    def test_a_broken_redis_never_costs_the_caller_its_fetch(self, monkeypatch, pages):
        class _Broken:
            def get(self, key):
                raise RuntimeError('boom')

            def setex(self, *a):
                raise RuntimeError('boom')

        monkeypatch.setattr(source_health, '_get_redis', lambda: _Broken())
        assert _check(1, pages) is False

    def test_different_request_shapes_do_not_compare(self, redis, pages):
        source_health.check_fixed_range_fetch('yahoo', 'OIL', 'daily:2y', 504,
                                              alarm=lambda *a: pages.append(a))
        source_health.check_fixed_range_fetch('yahoo', 'OIL', 'hourly:500h', 20,
                                              alarm=lambda *a: pages.append(a))
        assert pages == []

    def test_the_alarm_reaches_a_human(self, monkeypatch, caplog):
        import carrac.notifications.ntfy as ntfy_mod

        sent = []
        monkeypatch.setattr(ntfy_mod, 'send_ntfy',
                            lambda **kw: sent.append(kw) or True)
        with caplog.at_level(logging.ERROR):
            source_health._alarm('yahoo', 'OIL', 'daily:2y', 1, 504)

        assert len(sent) == 1, 'log-only is the alarm nobody reads'
        assert 'OIL' in sent[0]['body']
        assert any(r.levelno >= logging.ERROR for r in caplog.records)


class TestItIsWiredToALiveCaller:
    def test_yahoo_fetch_daily_calls_it(self):
        """The fixed-range client. Stooq deliberately does NOT call this — its
        row count varies by design, so it would page on normal operation."""
        from pathlib import Path

        src = Path('data_sources/yahoo.py').read_text()
        assert 'check_fixed_range_fetch' in src
        body = src[src.index('def fetch_daily('):src.index('def fetch_hourly(')]
        assert "check_fixed_range_fetch('yahoo', symbol, 'daily:2y', len(df))" in body
        assert "range': '2y'" in body, (
            'the guard assumes a fixed range; if the request stopped being '
            'fixed, the comparison stopped being valid'
        )
        assert 'check_fixed_range_fetch' not in Path('data_sources/stooq.py').read_text()


# ---------------------------------------------------------------------------
# The bar Yahoo actually served
# ---------------------------------------------------------------------------

def _frame(rows):
    """rows: (date, open, high, low, close, volume)."""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({
        'open': [r[1] for r in rows], 'high': [r[2] for r in rows],
        'low': [r[3] for r in rows], 'close': [r[4] for r in rows],
        'volume': [r[5] for r in rows],
    }, index=idx)


def _day(offset):
    return (datetime.date.today() - datetime.timedelta(days=offset)).isoformat()


def _real(offsets):
    # Distinct volumes per bar: identical ones would trip the earlier
    # carried-volume beacon and make these fixtures noisy for an unrelated
    # reason.
    return [(_day(o), 80.0, 82.0, 79.0, 81.0, 1000.0 + o) for o in offsets]


class TestZeroOhlcBarsAreRefused:
    def test_the_real_oil_bar(self, tmp_path, caplog):
        """A real CL=F bar: open 0.0, high 0.0, low 0.0, close 87.06, volume 0."""
        cache = CSVCache(cache_dir=str(tmp_path))
        cache.save('OIL', _frame(_real([5, 4, 3])), is_crypto=True)

        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        poisoned = _frame(_real([5, 4, 3])
                          + [(_day(2), 0.0, 0.0, 0.0, 87.06, 0.0)])
        cache.save('OIL', poisoned, is_crypto=True)

        on_disk = cache.load('OIL')
        assert pd.Timestamp(_day(2)) not in on_disk.index, (
            'a bar with no open, high or low was persisted as a session'
        )
        assert any('malformed-bar' in r.getMessage() for r in caplog.records)

    def test_it_is_invisible_to_the_synthetic_guard(self):
        """Why this needs its own check: synthetic_bar_mask requires
        open == high == low == close, and 0/0/0/87.06 satisfies none of it."""
        from carrac.staleness import synthetic_bar_mask

        frame = _frame([(_day(2), 0.0, 0.0, 0.0, 87.06, 0.0)])
        assert not bool(np.asarray(synthetic_bar_mask(frame)).any())

    def test_a_healthy_bar_is_untouched(self, tmp_path):
        cache = CSVCache(cache_dir=str(tmp_path))
        cache.save('OIL', _frame(_real([5, 4, 3])), is_crypto=True)
        cache.save('OIL', _frame(_real([5, 4, 3, 2])), is_crypto=True)
        assert len(cache.load('OIL')) == 4

    def test_todays_partial_bar_survives(self, tmp_path):
        """The session in progress can briefly publish an incomplete bar.
        Refusing it would lose real data, exactly as it would for the earlier
        flat zero-volume kline at 00:00 UTC."""
        cache = CSVCache(cache_dir=str(tmp_path))
        cache.save('OIL', _frame(_real([5, 4, 3])), is_crypto=True)
        cache.save('OIL', _frame(_real([5, 4, 3])
                                 + [(_day(0), 0.0, 0.0, 0.0, 87.06, 0.0)]),
                   is_crypto=True)
        assert pd.Timestamp(_day(0)) in cache.load('OIL').index

    def test_a_row_already_on_disk_is_never_truncated(self, tmp_path):
        """Removing accumulated residue is the purge script's job, run by hand
        after a dry run — never a side effect of an unrelated save. Dropping it
        here would also punch a hole, which is the synthetic-bar defect."""
        cache = CSVCache(cache_dir=str(tmp_path))
        path = cache.get_path('OIL', 'daily')
        path.write_text(
            'timestamp,open,high,low,close,volume\n'
            + ''.join(f'{d},80.0,82.0,79.0,81.0,1000.0\n' for d in
                      (_day(5), _day(4), _day(3)))
            + f'{_day(2)},0.0,0.0,0.0,87.06,0.0\n')

        cache.save('OIL', _frame(_real([5, 4, 3])
                                 + [(_day(2), 0.0, 0.0, 0.0, 87.06, 0.0)]),
                   is_crypto=True)
        assert pd.Timestamp(_day(2)) in cache.load('OIL').index

    def test_a_negative_quoting_series_is_untouched(self, tmp_path, caplog):
        """The clause that makes ``low <= 0`` safe to enforce at all.

        US10Y/US30Y/US5Y are Yahoo-routed YIELDS (^TNX, ^TYX, ^FVX), and a
        yield can legitimately print at or below zero — European rates did for
        years. Without requiring ``close > 0`` this guard would refuse every
        bar of a negative-rate series as malformed, which is a far worse defect
        than the one it fixes: it would silently stop collecting an entire
        instrument.
        """
        cache = CSVCache(cache_dir=str(tmp_path))
        healthy = [(_day(o), 0.4, 0.5, 0.3, 0.4, 0.0) for o in (5, 4, 3)]
        cache.save('US10Y', _frame(healthy), is_crypto=True)

        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        negative = healthy + [(_day(2), -0.05, 0.02, -0.11, -0.04, 0.0)]
        cache.save('US10Y', _frame(negative), is_crypto=True)

        assert pd.Timestamp(_day(2)) in cache.load('US10Y').index, (
            'a real negative-yield session was refused as a malformed bar'
        )
        assert not any('malformed-bar' in r.getMessage() for r in caplog.records)

    def test_an_all_zero_row_belongs_to_the_older_guard(self, tmp_path, caplog):
        """This guard deliberately does not claim ``close == 0``. An all-zero
        row is flat OHLC with zero volume, so it is already the earlier synthetic-bar guard's — and
        two guards refusing one row would make the log say the wrong thing
        about why. The row must still be refused; only the reason differs."""
        cache = CSVCache(cache_dir=str(tmp_path))
        cache.save('OIL', _frame(_real([5, 4, 3])), is_crypto=True)

        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        cache.save('OIL', _frame(_real([5, 4, 3])
                                 + [(_day(2), 0.0, 0.0, 0.0, 0.0, 0.0)]),
                   is_crypto=True)

        assert pd.Timestamp(_day(2)) not in cache.load('OIL').index
        reasons = [r.getMessage() for r in caplog.records]
        assert any('synthetic-bar' in m for m in reasons)
        assert not any('malformed-bar' in m for m in reasons)
