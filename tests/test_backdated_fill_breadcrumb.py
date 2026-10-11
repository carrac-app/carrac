"""Back-dated interior fill breadcrumb.

Check G (scripts/cache_integrity_check.py) detects carried-forward backfill
bars cross-instrument the morning AFTER they land; nothing records the write
itself. Measured on real data: the grains (CORN/WHEAT/SOYBEANS) were
missing their Tuesday bar while already carrying Wednesday's — the
late Tuesday bar arrives via the gap-repair merge in database.py::get_ohlcv_data and, when
upstream fabricates it, repeats Monday's volume (the prior Monday already repeated the prior Friday's:
CORN 152745, WHEAT 86717, SOYBEANS 59). This guard is the write-side provenance
for that event: observation-only, never drops a row, because gap repair is a
feature — a REAL late bar filling a hole is exactly what we want.

Predicate: a NEW daily row strictly INSIDE the existing on-disk date range.
Tail appends (normal delta) and prepends (HistoryBackfiller extends history
backward) stay silent by construction, so normal operation never logs.

Real 2026 calendar dates: 08-03 Mon, 08-04 Tue, 08-05 Wed, 08-06 Thu,
08-07 Fri, 08-10 Mon, 08-11 Tue, 08-12 Wed.
"""
import datetime
import logging

import pandas as pd
import pytest

import csv_cache
from csv_cache import CSVCache


def _day(offset):
    """A date ``offset`` days before today, as an ISO string.

    The carried-volume ERROR path only looks at the last
    ``_CARRIED_SURVIVAL_DAYS``, so the tests for it cannot use the fixed
    August calendar the rest of this file documents — a hard-coded date walks
    out of the window as soon as the clock moves and the test would quietly
    stop asserting anything.
    """
    return (datetime.date.today() - datetime.timedelta(days=offset)).isoformat()


class _FakeRedis:
    """Only what _carried_already_reported touches."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.fixture
def no_redis(monkeypatch):
    """Dedup off. The outbound boundary is stubbed, not the unit under test —
    and None is a state production really reaches (Redis down), in which the
    guard is documented to fail toward reporting."""
    monkeypatch.setattr(csv_cache, '_get_redis', lambda: None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(csv_cache, '_get_redis', lambda: r)
    return r


def _bars(rows):
    """rows: list of (date, volume). Non-flat OHLC so the synthetic-bar guard
 Never touches these frames."""
    idx = pd.to_datetime([d for d, _ in rows])
    base = 100.0
    return pd.DataFrame({
        'open': base, 'high': base + 1, 'low': base - 1, 'close': base,
        'volume': [v for _, v in rows],
    }, index=idx)


def test_interior_fill_is_recorded(tmp_path, caplog):
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('CORN', _bars([('2026-08-03', 500), ('2026-08-04', 600),
                              ('2026-08-06', 700), ('2026-08-07', 800)]))

    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache.save('CORN', _bars([('2026-08-03', 500), ('2026-08-04', 600),
                              ('2026-08-05', 650),
                              ('2026-08-06', 700), ('2026-08-07', 800)]))

    msgs = [r.message for r in caplog.records
            if 'backdated fill' in r.message]
    assert msgs, "interior fill went unrecorded"
    assert 'CORN' in msgs[0] and '2026-08-05' in msgs[0]


def test_an_arriving_carried_volume_does_not_page(tmp_path, caplog, no_redis):
    """An arriving carried volume is deliberately NOT an ERROR case.

    A bar written mid-session repeats the prior volume and is repaired by the
    next fetch after settlement — YahooClient.fetch_daily always asks for
    range=2y, so keep='last' rewrites the overlap every time. Paging on
    arrival meant paging on healthy behaviour: 17 events across 9 symbols in a
    week, every one self-resolved.
    """
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('WHEAT', _bars([(_day(5), 500), (_day(4), 600),
                               (_day(2), 700), (_day(1), 800)]), is_crypto=True)

    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache.save('WHEAT', _bars([(_day(5), 500), (_day(4), 600),
                               (_day(3), 600),   # copies the prior volume
                               (_day(2), 700), (_day(1), 800)]), is_crypto=True)

    assert not [r for r in caplog.records if r.levelno == logging.ERROR], (
        'an arriving carried volume paged — it self-corrects on the next fetch'
    )
    assert any('backdated fill' in r.message
               for r in caplog.records), 'the provenance record was lost too'


def test_a_carried_volume_that_survives_a_refetch_pages(tmp_path, caplog, no_redis):
    """The condition that actually means something is wrong: the value was
    stored, the source was asked again, and it came back the same."""
    cache = CSVCache(cache_dir=str(tmp_path))
    carried = [(_day(5), 500), (_day(4), 600), (_day(3), 600),
               (_day(2), 700), (_day(1), 800)]
    cache.save('WHEAT', _bars(carried), is_crypto=True)

    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache.save('WHEAT', _bars(carried), is_crypto=True)   # the refetch

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, 'a surviving carried volume did not reach error level'
    assert _day(3) in errors[0].message
    assert 'carried volume SURVIVED' in errors[0].message


def test_a_self_corrected_value_goes_quiet(tmp_path, caplog, no_redis):
    """The eight of nine symbols that resolved themselves. The refetch brings
    a different volume, so there is nothing left to say."""
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('WHEAT', _bars([(_day(5), 500), (_day(4), 600), (_day(3), 600),
                               (_day(2), 700), (_day(1), 800)]), is_crypto=True)

    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache.save('WHEAT', _bars([(_day(5), 500), (_day(4), 600), (_day(3), 655),
                               (_day(2), 700), (_day(1), 800)]), is_crypto=True)

    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_an_old_carried_value_is_outside_the_window(tmp_path, caplog, no_redis):
    """Yahoo itself still serves a carried volume for 2024-09-09 and
    2025-10-13 on 8 of the 9 commodity series. Those are the source's own
    numbers: paging on them would never stop and a repair pass would never
    converge, so the window excludes them."""
    cache = CSVCache(cache_dir=str(tmp_path))
    old = [(_day(40), 500), (_day(39), 600), (_day(38), 600),
           (_day(37), 700), (_day(36), 800)]
    cache.save('WHEAT', _bars(old), is_crypto=True)

    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache.save('WHEAT', _bars(old), is_crypto=True)

    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_it_pages_once_per_symbol_and_date(tmp_path, caplog, fake_redis):
    """The precompute cycle rewrites these frames every ~30 minutes. Without
    dedup a genuine week-long carry would produce hundreds of identical
    events — the noise the re-scope exists to remove, reintroduced."""
    cache = CSVCache(cache_dir=str(tmp_path))
    carried = [(_day(5), 500), (_day(4), 600), (_day(3), 600),
               (_day(2), 700), (_day(1), 800)]
    cache.save('WHEAT', _bars(carried), is_crypto=True)

    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache.save('WHEAT', _bars(carried), is_crypto=True)
    cache.save('WHEAT', _bars(carried), is_crypto=True)
    cache.save('WHEAT', _bars(carried), is_crypto=True)

    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_zero_volume_fill_is_not_called_carried(tmp_path, caplog):
    """FX/DXY/PLATINUM publish volume 0 on every bar — 0 == 0 is the series'
    normal encoding, not the carried-forward signature (Check G's stated blind
    spot, kept deliberately). Still recorded as a plain fill."""
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('EURUSD', _bars([('2026-08-03', 0), ('2026-08-04', 0),
                                ('2026-08-06', 0), ('2026-08-07', 0)]))

    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache.save('EURUSD', _bars([('2026-08-03', 0), ('2026-08-04', 0),
                                ('2026-08-05', 0),
                                ('2026-08-06', 0), ('2026-08-07', 0)]))

    records = [r for r in caplog.records
               if 'backdated fill' in r.message]
    assert records, "zero-volume interior fill went unrecorded"
    assert records[0].levelno == logging.WARNING, \
        "0 == 0 must not read as the carried-forward signature"


def test_tail_append_stays_silent(tmp_path, caplog):
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('CORN', _bars([('2026-08-03', 500), ('2026-08-04', 600)]))

    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache.save('CORN', _bars([('2026-08-03', 500), ('2026-08-04', 600),
                              ('2026-08-05', 650)]))

    assert not any('backdated fill' in r.message
                   for r in caplog.records), "normal delta append must not log"


def test_backward_prepend_stays_silent(tmp_path, caplog):
    """The crypto HistoryBackfiller writes rows BEFORE the head, in chunks,
    all day long. Prepends are history extension, not hole fills."""
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('BTC', _bars([('2026-08-06', 700), ('2026-08-07', 800)]),
               is_crypto=True)

    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache.save('BTC', _bars([('2026-08-03', 500), ('2026-08-04', 600),
                             ('2026-08-06', 700), ('2026-08-07', 800)]),
               is_crypto=True)

    assert not any('backdated fill' in r.message
                   for r in caplog.records), "backward prepend must not log"


def test_resave_of_existing_rows_stays_silent(tmp_path, caplog):
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('CORN', _bars([('2026-08-03', 500), ('2026-08-04', 600),
                              ('2026-08-05', 650), ('2026-08-06', 700)]))

    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache.save('CORN', _bars([('2026-08-03', 500), ('2026-08-04', 600),
                              ('2026-08-05', 650), ('2026-08-06', 700)]))

    assert not any('backdated fill' in r.message
                   for r in caplog.records)


def test_cold_cache_full_history_stays_silent(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('CORN', _bars([('2026-08-03', 500), ('2026-08-05', 650),
                              ('2026-08-07', 800)]))

    assert not any('backdated fill' in r.message
                   for r in caplog.records), "first write of a series is not a fill"
