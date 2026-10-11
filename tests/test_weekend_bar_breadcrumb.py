"""Weekend-bar write breadcrumb.

GOLD/SILVER/OIL/COPPER/NATGAS/PLATINUM/DXY (all weekday-only Yahoo/Stooq
instruments) can pick up a phantom Sunday-dated row in their daily CSV;
upstream has none of them. It is not caused by Yahoo .normalize() UTC shift,
ETF/spot multipliers, the display-only _inject_live_candle builder, or the
empty-fetch/rate-limit path. A breadcrumb at the cache write path records the
writer.

Real 2026 calendar dates used below (2026-08-05 is a Wednesday): 07-31 Fri, 08-01 Sat, 08-02 Sun,
08-03 Mon, 08-04 Tue, 08-05 Wed, 08-06 Thu.
"""
import json
import logging

import pandas as pd
import pytest

import csv_cache
from csv_cache import CSVCache


def _bars(dates, base=100.0):
    idx = pd.to_datetime(list(dates))
    return pd.DataFrame({
        'open': base, 'high': base + 1, 'low': base - 1, 'close': base, 'volume': 0,
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


def test_new_sunday_row_for_gold_fires_the_breadcrumb(tmp_path, fake_redis, caplog):
    caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('GOLD', _bars(['2026-07-30', '2026-07-31']))  # Thu, Fri — clean

    cache.save('GOLD', _bars(['2026-07-30', '2026-07-31', '2026-08-02', '2026-08-03']))

    key = 'mcd:weekendbar:GOLD'
    assert key in fake_redis.store, f"no breadcrumb written, got keys {list(fake_redis.store)}"
    ttl, payload = fake_redis.store[key]
    assert ttl == 14 * 24 * 3600
    data = json.loads(payload)
    assert data['symbol'] == 'GOLD'
    assert data['dates'] == ['2026-08-02']
    assert 'stack' in data and data['stack'], "breadcrumb must carry the full call stack"
    assert any('weekend-bar write' in r.message and 'GOLD' in r.message
               for r in caplog.records), "no logger.error breadcrumb (Bugsink path)"


def test_new_sunday_row_for_btc_is_exempt(tmp_path, fake_redis, caplog):
    """Crypto trades 24/7 — a weekend bar is correct, not a defect."""
    caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('BTC', _bars(['2026-07-30', '2026-07-31']), is_crypto=True)

    cache.save('BTC', _bars(['2026-07-30', '2026-07-31', '2026-08-02']), is_crypto=True)

    assert fake_redis.store == {}
    assert not any('weekend-bar write' in r.message for r in caplog.records)


def test_resaving_an_existing_weekend_row_does_not_refire(tmp_path, fake_redis, caplog):
    """Whatever wrote the Sunday bar the FIRST time is the thing we want the
    breadcrumb to catch. Every subsequent delta fetch that merely carries the
    same already-cached weekend row forward must stay silent, or the signal
    drowns in noise on every normal refresh."""
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('GOLD', _bars(['2026-07-31', '2026-08-02']))  # Sunday already lands here

    caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
    caplog.clear()
    fake_redis.store.clear()

    # A later, normal delta save re-includes the same Sunday row plus a new
    # legitimate weekday bar.
    cache.save('GOLD', _bars(['2026-07-31', '2026-08-02', '2026-08-03']))

    assert fake_redis.store == {}, f"re-fired on an already-cached weekend row: {fake_redis.store}"
    assert not any('weekend-bar write' in r.message for r in caplog.records)


def test_a_genuinely_new_weekend_row_after_prior_ones_still_fires(tmp_path, fake_redis):
    """Only the specific NEW weekend date is exempt-by-existing, not the whole
    symbol once it has ever carried one — every Sunday is a fresh event."""
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('GOLD', _bars(['2026-07-31', '2026-08-02']))
    fake_redis.store.clear()

    cache.save('GOLD', _bars(['2026-07-31', '2026-08-02', '2026-08-03', '2026-08-09']))

    key = 'mcd:weekendbar:GOLD'
    assert key in fake_redis.store
    _, payload = fake_redis.store[key]
    data = json.loads(payload)
    assert data['dates'] == ['2026-08-09']
    assert '2026-08-02' not in data['dates'], "the already-known Sunday must not be re-reported"


def test_weekday_only_save_never_fires(tmp_path, fake_redis, caplog):
    caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('SILVER', _bars(['2026-07-30', '2026-07-31', '2026-08-03', '2026-08-04']))

    assert fake_redis.store == {}
    assert not any('weekend-bar' in r.message for r in caplog.records)


def test_hourly_timeframe_is_not_checked(tmp_path, fake_redis):
    """This is scoped to DAILY CSV frames; an hourly cache legitimately
    carries every timestamp including weekend hours for some instruments and
    is out of scope here."""
    cache = CSVCache(cache_dir=str(tmp_path))
    idx = pd.date_range('2026-08-01', periods=48, freq='h')  # spans the Sat/Sun
    df = pd.DataFrame({'open': 1, 'high': 1, 'low': 1, 'close': 1, 'volume': 0}, index=idx)

    cache.save('GOLD', df, timeframe='1h')

    assert fake_redis.store == {}


def test_breadcrumb_capture_never_blocks_the_save(tmp_path, monkeypatch):
    """Observation only: a breadcrumb failure must
    never stop the write it is observing."""
    def _boom():
        raise RuntimeError("redis unreachable")
    monkeypatch.setattr(csv_cache, '_get_redis', _boom)

    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('GOLD', _bars(['2026-07-31', '2026-08-02']))

    out = cache.load('GOLD')
    assert out is not None and len(out) == 2


# ─────────────────────────────────────────────────────────────────────────
# The writer, found, and the phantom row now dropped
#
# The breadcrumb armed above identifies the writer: affected symbols
# (GOLD, SILVER, OIL, COPPER, NATGAS, PLATINUM, DXY, AUDUSD, NZDUSD, USDCAD)
# each record a stack ending at the SAME line —
#
#     database.py   in get_ohlcv_data   self.cache.save(symbol, combined_df)
#     csv_cache.py  in save             self._check_weekend_bar_write(...)
#
# reached from three different callers (precompute's asset loop,
# _compute_rs_rankings, and the pulse payload builder). The row comes back in
# `self.stooq.fetch(...)` and is merged and saved verbatim.
#
# Observation alone is not enough, but a blunt "drop weekend rows
# from non-crypto" is exactly the mistake the purge script's second exemption
# exists to prevent: in production it would destroy thousands of legitimate rows
# across dozens of symbols, every FX major included. FX genuinely trades Sunday
# evening. So the drop reuses the SAME two-condition rule the purge uses, and
# imports it rather than restating it.
# ─────────────────────────────────────────────────────────────────────────

def _weekday_series(n=90, start='2026-01-05'):
    """A weekday-only instrument with enough history to judge — GOLD-shaped."""
    idx = pd.bdate_range(start=start, periods=n)
    return _bars(idx)


def _weekend_trading_series(n=90, start='2026-01-05'):
    """A 24/7 or FX-shaped instrument: every calendar day present (~28% weekend)."""
    idx = pd.date_range(start=start, periods=n, freq='D')
    return _bars(idx)


def test_phantom_weekend_row_is_dropped_for_a_weekday_only_instrument(tmp_path, fake_redis):
    cache = CSVCache(cache_dir=str(tmp_path))
    clean = _weekday_series()
    cache.save('GOLD', clean)

    phantom = pd.concat([clean, _bars(['2026-08-09'])])  # Sunday
    cache.save('GOLD', phantom)

    on_disk = cache.load('GOLD')
    assert pd.Timestamp('2026-08-09') not in on_disk.index, (
        "the Sunday row upstream never had was written to GOLD's history"
    )
    assert len(on_disk) == len(clean), "only the phantom row should have been removed"


def test_a_weekend_trading_series_keeps_its_weekend_rows(tmp_path, fake_redis):
    """The 6,784-row mistake: FX majors trade Sunday evening and must be left alone."""
    cache = CSVCache(cache_dir=str(tmp_path))
    fx = _weekend_trading_series()
    cache.save('AUDUSD', fx)

    grown = pd.concat([fx, _bars([fx.index.max() + pd.Timedelta(days=1)])])
    cache.save('AUDUSD', grown)

    on_disk = cache.load('AUDUSD')
    assert len(on_disk) == len(grown), "a real FX weekend row was deleted as a phantom"


def test_crypto_is_untouched(tmp_path, fake_redis):
    cache = CSVCache(cache_dir=str(tmp_path))
    btc = _weekend_trading_series()
    cache.save('BTC', btc, is_crypto=True)
    assert len(cache.load('BTC')) == len(btc)


def test_a_weekend_row_already_on_disk_is_not_retroactively_purged(tmp_path, fake_redis):
    """Dropping is for NEW rows only. Deleting accumulated history is the purge
    script's job, run by hand after a dry run — not a side effect of a save."""
    cache = CSVCache(cache_dir=str(tmp_path))
    seeded = pd.concat([_weekday_series(), _bars(['2026-08-09'])])
    # Seed PAST the writer — these rows stand for history accumulated before
    # the drop existed, which is exactly the state production is in. Going through
    # save() could not produce it, because save() now refuses to write them.
    seeded.to_csv(cache.get_path('GOLD', 'daily'))
    assert pd.Timestamp('2026-08-09') in cache.load('GOLD').index

    cache.save('GOLD', seeded)
    assert pd.Timestamp('2026-08-09') in cache.load('GOLD').index, (
        "an existing weekend row was silently deleted by an unrelated save"
    )


def _long_series(n_weeks, with_sundays):
    """A frame long enough for trades_weekends() to actually judge."""
    import datetime
    dates, d = [], datetime.date(2025, 1, 6)   # a Monday
    for _ in range(n_weeks):
        for off in range(5):                    # Mon-Fri
            dates.append((d + datetime.timedelta(days=off)).isoformat())
        if with_sundays:
            dates.append((d + datetime.timedelta(days=6)).isoformat())
        d += datetime.timedelta(days=7)
    return dates


def test_a_confirmed_weekend_trader_is_silent_not_paged(tmp_path, fake_redis, caplog):
    """The beacon must fire AFTER the trades_weekends() check; otherwise every
    FX major pages to the error tracker every Sunday for a row the very next line
    deliberately keeps.

    Those rows are real: AUDUSD, USDHUF, EURUSD, GBPUSD and USDJPY all carry a
    genuine Sunday range every week. Only the CURRENT Sunday is flat, because FX opens ~21:00
    UTC and the bar is a session-in-progress placeholder. An alarm that is
    never actionable is the one that teaches you to ignore the others.
    """
    caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
    cache = CSVCache(cache_dir=str(tmp_path))
    hist = _long_series(30, with_sundays=True)
    cache.save('AUDUSD', _bars(hist))
    fake_redis.store.clear()
    caplog.clear()

    import datetime
    nxt = (datetime.date.fromisoformat(hist[-1]) + datetime.timedelta(days=7)).isoformat()
    assert datetime.date.fromisoformat(nxt).weekday() == 6
    cache.save("AUDUSD", _bars(hist + [nxt]))

    assert fake_redis.store == {}, f"paged for a legitimate weekend row: {fake_redis.store}"
    assert not any('weekend-bar' in r.message for r in caplog.records)
    written = pd.read_csv(tmp_path / 'AUDUSD_daily.csv', index_col=0)
    assert nxt in written.index.astype(str).tolist(), "the legitimate row was dropped"


def test_a_weekday_only_instrument_still_pages_and_is_dropped(tmp_path, fake_redis, caplog):
    """The other half: silencing the FX case must not silence the real one.

    For an ESTABLISHED weekday-only series the 'about to gain'
    breadcrumb (and its Redis key) no longer fires — see
    test_the_beacon_no_longer_claims_the_writer_is_unknown below for why —
    because the drop a few lines later always removes exactly those rows, so
    it would report a write that never happens (~30ms apart, one drop for
    every gain). The
    'dropped' line still pages (it still reaches the error tracker at ERROR) and the
    row is still correctly kept off disk — that half of this test's name is
    unaffected."""
    caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
    cache = CSVCache(cache_dir=str(tmp_path))
    hist = _long_series(30, with_sundays=False)
    cache.save('GOLD', _bars(hist))
    fake_redis.store.clear()
    caplog.clear()

    import datetime
    sunday = (datetime.date.fromisoformat(hist[-1]) + datetime.timedelta(days=2)).isoformat()
    assert datetime.date.fromisoformat(sunday).weekday() == 6
    cache.save('GOLD', _bars(hist + [sunday]))

    assert 'mcd:weekendbar:GOLD' not in fake_redis.store, (
        "An established weekday-only series must not page the "
        "'about to gain' breadcrumb — every one of these rows is dropped, "
        "so nothing here ever reaches a write"
    )
    assert any('weekend-bar dropped:' in r.message and 'GOLD' in r.message
               for r in caplog.records), "the drop itself must still page"
    written = pd.read_csv(tmp_path / 'GOLD_daily.csv', index_col=0)
    assert sunday not in written.index.astype(str).tolist(), "phantom row was written"


def test_the_beacon_no_longer_claims_the_writer_is_unknown(tmp_path, fake_redis, caplog):
    """The text must not say 'writer not yet identified' when the writer
    is known; a stale claim like that sends a reader on a chase that is
    already finished.

    This beacon is confined to the one case where the row actually
    survives to the write — a series too short for trades_weekends() to
    judge — so this uses a short GOLD history (same shape as
    test_new_sunday_row_for_gold_fires_the_breadcrumb above) rather than the
    established 30-week one; on an established series the beacon no longer
    fires at all (see test_a_weekday_only_instrument_still_pages_and_is_dropped)."""
    caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save('GOLD', _bars(['2026-07-30', '2026-07-31']))  # Thu, Fri — too short to judge
    caplog.clear()
    cache.save('GOLD', _bars(['2026-07-30', '2026-07-31', '2026-08-02', '2026-08-03']))

    msgs = ' '.join(r.message for r in caplog.records)
    assert 'writer not yet identified' not in msgs
    assert 'database.py get_ohlcv_data' in msgs, "the beacon should name the writer it knows"
