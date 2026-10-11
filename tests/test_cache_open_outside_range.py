"""Refuse a NEW daily row whose open sits outside its own
[low, high].

Observed failure: 13 tracked equities got a Monday bar written 1-110 minutes
after the NYSE open in which the open was FRIDAY's open (byte-exact in 12 of
13) while high/low/close came from the forming Monday session. The open is a
trade inside the session, so ``low <= open <= high`` holds at every moment of
a real session — a violation is never a partial bar, it is two sessions
stitched into one row, and every indicator reading it is wrong.

Unlike the zero-leg guard, this one judges TODAY's forming bar too:
that is precisely the bar that was poisoned, and a forming bar with a sane
open still passes untouched.

Tolerance is 0.1% of close — Check H's noise floor
(scripts/cache_integrity_check.py) — because FX feeds routinely leave the
high a rounding step below the open.
"""
import datetime
import logging

import pandas as pd

from csv_cache import CSVCache


def _day(offset):
    return (datetime.date.today() - datetime.timedelta(days=offset)).isoformat()


def _frame(rows):
    df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low',
                                     'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df.set_index('timestamp')


def _real(offsets):
    # Distinct volumes per bar so the carried-volume beacon stays
    # quiet for an unrelated reason.
    return [(_day(o), 80.0, 82.0, 79.0, 81.0, 1000.0 + o) for o in offsets]


class TestOpenOutsideRangeIsRefused:
    def test_the_real_am_bar_today(self, tmp_path, caplog):
        """A real AM bar: open 22.42 (= Friday's open), high 22.18, low
        22.0199, close 22.07 — open ABOVE the bar's own high, written as the
        same-day forming bar. The refusal must apply to today's date."""
        cache = CSVCache(cache_dir=str(tmp_path))
        cache.save('AM', _frame(_real([5, 4, 3])), is_crypto=False)

        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        poisoned = _frame(_real([5, 4, 3])
                          + [(_day(0), 22.42, 22.18, 22.0199, 22.07, 28721.0)])
        cache.save('AM', poisoned, is_crypto=False)

        on_disk = cache.load('AM')
        assert pd.Timestamp(_day(0)) not in on_disk.index, (
            'a bar whose open sits above its own high was persisted'
        )
        assert any('open-outside-range' in r.getMessage() for r in caplog.records)

    def test_open_below_low_is_refused_too(self, tmp_path, caplog):
        """A US equity: open 106.29, low 110.29 — open BELOW the low."""
        cache = CSVCache(cache_dir=str(tmp_path))
        cache.save('TSLA', _frame(_real([5, 4, 3])), is_crypto=False)

        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        poisoned = _frame(_real([5, 4, 3])
                          + [(_day(2), 106.29, 110.58, 110.29, 110.58, 12346.0)])
        cache.save('TSLA', poisoned, is_crypto=False)

        assert pd.Timestamp(_day(2)) not in cache.load('TSLA').index
        assert any('open-outside-range' in r.getMessage() for r in caplog.records)

    def test_a_healthy_forming_bar_is_untouched(self, tmp_path):
        """A partial same-day bar with a sane open is real data — keep it."""
        cache = CSVCache(cache_dir=str(tmp_path))
        cache.save('AM', _frame(_real([5, 4, 3])), is_crypto=False)
        cache.save('AM', _frame(_real([5, 4, 3])
                                + [(_day(0), 80.5, 81.0, 80.0, 80.8, 99.0)]),
                   is_crypto=False)
        assert pd.Timestamp(_day(0)) in cache.load('AM').index

    def test_fx_rounding_step_survives(self, tmp_path, caplog):
        """AUDUSD shape: high one rounding step below the open — inside the
        0.1%-of-close noise floor, must not be refused (flagging it would
        train everyone to ignore the guard)."""
        cache = CSVCache(cache_dir=str(tmp_path))
        healthy = [(_day(o), 0.676, 0.678, 0.671, 0.676, 0.0) for o in (5, 4, 3)]
        cache.save('AUDUSD', _frame(healthy), is_crypto=False)

        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        step = healthy + [(_day(2), 0.67633009, 0.67626059, 0.67153978,
                           0.67633009, 0.0)]
        cache.save('AUDUSD', _frame(step), is_crypto=False)

        assert pd.Timestamp(_day(2)) in cache.load('AUDUSD').index
        assert not any('open-outside-range' in r.getMessage() for r in caplog.records)

    def test_a_row_already_on_disk_is_never_truncated(self, tmp_path):
        """The 13 bars already written must stay repairable: the corrected row
        replaces the same date via keep='last', so a date already on disk is
        not judged — refusing it would block the self-repair."""
        cache = CSVCache(cache_dir=str(tmp_path))
        path = cache.get_path('AM', 'daily')
        path.write_text(
            'timestamp,open,high,low,close,volume\n'
            + ''.join(f'{d},80.0,82.0,79.0,81.0,{1000.0 + i}\n'
                      for i, d in enumerate((_day(5), _day(4), _day(3))))
            + f'{_day(2)},22.42,22.18,22.0199,22.07,28721.0\n')

        cache.save('AM', _frame(_real([5, 4, 3])
                                + [(_day(2), 22.42, 22.18, 22.0199, 22.07,
                                    28721.0)]),
                   is_crypto=False)
        assert pd.Timestamp(_day(2)) in cache.load('AM').index

    def test_a_negative_quoting_series_is_untouched(self, tmp_path, caplog):
        """Yields can print below zero; a negative close is not judged, same
        contract as the zero-leg guard."""
        cache = CSVCache(cache_dir=str(tmp_path))
        healthy = [(_day(o), 0.4, 0.5, 0.3, 0.4, 0.0) for o in (5, 4, 3)]
        cache.save('US10Y', _frame(healthy), is_crypto=False)

        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        negative = healthy + [(_day(2), 0.03, 0.02, -0.11, -0.04, 0.0)]
        cache.save('US10Y', _frame(negative), is_crypto=False)

        assert pd.Timestamp(_day(2)) in cache.load('US10Y').index
        assert not any('open-outside-range' in r.getMessage() for r in caplog.records)
