"""The newest cached row is written once and never corrected.

Example: a stored GOLD close of 4320.70 vs upstream's settled 4242.00
(+1.86%) — an intraday price frozen at first write. Rows behind the tail
matched upstream exactly, so only the newest bar was wrong.

Root cause: ``get_ohlcv_data``'s normal (non-gap-repair) delta path passed
``fetch_from = last_cached_date`` straight into ``self.stooq.fetch()``. Stooq's
own ``fetch(start_date=X)`` queries from ``X + 1 day`` onward
(data_sources/stooq.py::fetch — deliberately exclusive, a normal delta should not
re-ask for what it already has). So the tail's own row was never inside the
requested window, and could never be corrected no matter how many deltas ran
— not a merge-preference bug (``keep='last'`` already prefers new data), a
"the new frame never contained that date at all" bug.

Tests exercise ``get_ohlcv_data`` end to end with a stubbed cache + stooq
client — no monkeypatching of the merge/fetch_from logic itself.
"""
from datetime import datetime

import pandas as pd

import database
from database import OHLCVDatabase


# A Thursday: an ordinary weekday tail, not a weekend edge case.
TAIL = pd.Timestamp('2026-08-06')
STALE_CLOSE = 4320.70   # the wrong, intraday-frozen value
SETTLED_CLOSE = 4242.00  # upstream's real settled close


def _weekday_tail(days_ago):
    """The most recent BUSINESS day at least *days_ago* days back.

    Do not write ``now() - N days`` here. ``_cached_frame`` lays
    its rows on ``pd.bdate_range``, so a weekend tail silently snaps back to
    the previous Friday: the date the test then asserts on is not in the frame
    at all and ``.loc`` raises KeyError. That is a guaranteed failure every
    Saturday and Sunday (e.g. ``now() - 3`` landing on a Saturday).

    This is the same trap documented for
    ``test_no_gap_no_stale_reason_still_no_fetch_while_fresh`` below, which
    pins the clock.

    Snapping backwards only ever makes the tail older, so ``days_old >= 2``
    (the delta-fetch trigger in database.py::get_ohlcv_data) still holds for any
    ``days_ago >= 2``.
    """
    anchor = pd.Timestamp.now().normalize() - pd.Timedelta(days=days_ago)
    return pd.bdate_range(end=anchor, periods=1)[0]


def _cached_frame(tail=TAIL, stale_close=STALE_CLOSE):
    """A clean, gapless weekday tail ending at *tail*, with the LAST row set
    to a deliberately wrong ("still intraday") close."""
    dates = pd.bdate_range(end=tail, periods=10)
    closes = [100.0 + i for i in range(len(dates) - 1)] + [stale_close]
    return pd.DataFrame({
        'open': closes, 'high': [c + 1 for c in closes],
        'low': [c - 1 for c in closes], 'close': closes, 'volume': 0,
    }, index=dates)


class _StubCache:
    def __init__(self, df):
        self._df = df
        self.saved = None

    def load(self, symbol, timeframe='daily'):
        return self._df

    def save(self, symbol, df, timeframe='daily', allow_shrink=False, is_crypto=False):
        self.saved = df


class _RealisticStooqStub:
    """Mimics data_sources/stooq.py's real fetch() semantics: start_date is
    EXCLUSIVE (queries from start_date + 1 day), and it hands back whatever
    upstream currently has for [effective_start, tail] — including the
    settled tail value, since upstream has already closed the book on it."""

    def __init__(self, tail=TAIL, settled_close=SETTLED_CLOSE):
        self.tail = tail
        self.settled_close = settled_close
        self.calls = []

    def fetch(self, symbol, start_date=None):
        self.calls.append(start_date)
        if start_date is None:
            eff_start = self.tail - pd.Timedelta(days=730)
        else:
            eff_start = start_date + pd.Timedelta(days=1)   # stooq.py::fetch
        if eff_start > self.tail:
            return pd.DataFrame()   # nothing upstream inside the window asked for
        idx = pd.bdate_range(start=eff_start, end=self.tail)
        if idx.empty:
            return pd.DataFrame()
        closes = [200.0] * (len(idx) - 1) + [self.settled_close]
        return pd.DataFrame({
            'open': closes, 'high': [c + 1 for c in closes],
            'low': [c - 1 for c in closes], 'close': closes, 'volume': 0,
        }, index=idx)


def _real_stooq_maps():
    """StooqClient builds its maps in __init__, so instantiate one to read them."""
    from data_sources.circuit_breaker import CircuitBreaker
    from data_sources.stooq import StooqClient
    client = StooqClient(CircuitBreaker('stooq_test_maps'), yahoo_client=None)
    return {n: getattr(client, n) for n in
            ('stock_map', 'etf_map', 'commodity_map', 'index_map', 'forex_map')}


def _make_db(cached_df, stooq_stub, monkeypatch):
    monkeypatch.setattr(database, '_get_cc_redis', lambda: None)
    db = OHLCVDatabase.__new__(OHLCVDatabase)
    db.cache = _StubCache(cached_df)
    db.stooq = stooq_stub
    # The equity path is gated on explicit map membership, so the
    # bare __new__ object needs the REAL maps — GOLD is in commodity_map. These
    # are the genuine article (StooqClient's own dicts), not a stand-in for the
    # predicate under test.
    for _name, _map in _real_stooq_maps().items():
        setattr(db, _name, _map)
    monkeypatch.setattr(OHLCVDatabase, '_is_crypto_metric', lambda self, s: False, raising=False)
    monkeypatch.setattr(OHLCVDatabase, '_is_crypto', lambda self, s: False, raising=False)
    return db


def test_stale_tail_value_gets_corrected_by_the_next_fetch(monkeypatch):
    """The concrete production bug: a cache whose tail is old enough to trigger a
    normal delta fetch (days_old >= 2, no interior gap) must come back with
    the SETTLED value for that date, not keep serving the frozen one."""
    tail = _weekday_tail(3)
    cached = _cached_frame(tail=tail)
    stooq = _RealisticStooqStub(tail=tail)
    db = _make_db(cached, stooq, monkeypatch)

    out = db.get_ohlcv_data('GOLD')

    assert stooq.calls, "no fetch was attempted at all"
    got = out.loc[pd.Timestamp(tail), 'close']
    assert got == SETTLED_CLOSE, (
        f"tail row still shows {got} (the stale value) instead of the settled "
        f"{SETTLED_CLOSE} — fetch_from={stooq.calls[-1]} excluded the tail "
        f"from the refetch window"
    )


def test_historical_rows_behind_the_tail_are_untouched(monkeypatch):
    tail = _weekday_tail(3)
    cached = _cached_frame(tail=tail)
    stooq = _RealisticStooqStub(tail=tail)
    db = _make_db(cached, stooq, monkeypatch)

    out = db.get_ohlcv_data('GOLD')

    for ts in cached.index[:-1]:
        assert out.loc[ts, 'close'] == cached.loc[ts, 'close'], (
            f"historical row {ts.date()} changed from "
            f"{cached.loc[ts, 'close']} to {out.loc[ts, 'close']}"
        )


def test_no_gap_no_stale_reason_still_no_fetch_while_fresh(monkeypatch):
    """Sanity check the fixture: a genuinely fresh (<2d), gapless tail must
    still take the existing short-circuit and NOT fetch at all — this fix
    does not touch the fetch CADENCE, only what a fetch that already happens
    asks for.

    The clock is PINNED, and must stay pinned. Reading
    ``pd.Timestamp.now().normalize() - 1 day`` would fail every Sunday and
    Monday: ``_cached_frame`` lays its rows on ``pd.bdate_range``, so a Sunday
    "yesterday" silently snaps back to Friday and a Monday one back to the
    previous Friday. That makes the tail 2-3 days old, ``days_old < 2`` at
    the delta-fetch trigger in database.py::get_ohlcv_data is correctly False, and the fetch this test forbids is
    the RIGHT behaviour. The assertion was about which weekday CI ran on, not
    about cadence. With weekday-only data a "<2d old" tail does not exist on a
    Sunday at all, so pinning is the only way to state the intended scenario.
    """
    # TAIL is a Thursday, so "the next day" is a Friday and the tail is
    # genuinely 1 day old — the case this test is actually about.
    frozen_now = (TAIL + pd.Timedelta(days=1)).to_pydatetime()

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now

    monkeypatch.setattr(database, 'datetime', _FrozenDatetime)

    tail = TAIL
    cached = _cached_frame(tail=tail)
    stooq = _RealisticStooqStub(tail=tail)
    db = _make_db(cached, stooq, monkeypatch)

    out = db.get_ohlcv_data('GOLD')

    assert stooq.calls == [], "cadence changed: a <2d-old gapless tail must not fetch"
    assert out.loc[pd.Timestamp(tail), 'close'] == STALE_CLOSE


def test_shrink_guard_still_refuses_a_collapsed_write(tmp_path):
    """The tail-correction change must not weaken the existing shrink guard —
    a write that would replace an established cache with far fewer rows is
    still rejected."""
    from csv_cache import CSVCache

    cache = CSVCache(cache_dir=str(tmp_path))
    big = pd.DataFrame({'close': range(200)},
                       index=pd.bdate_range(end=TAIL, periods=200))
    cache.save('GOLD', big)

    tiny = pd.DataFrame({'close': range(5)},
                        index=pd.bdate_range(end=TAIL, periods=5))
    cache.save('GOLD', tiny)   # no allow_shrink — must be refused

    got = cache.load('GOLD')
    assert len(got) == 200, f"shrink guard let a 5-row frame replace 200 (now {len(got)})"


def test_fetch_from_reaches_at_least_one_day_before_the_tail():
    """Direct proof of the fetch_from widening, independent of any particular
    upstream stub's behaviour."""
    import inspect
    src = inspect.getsource(OHLCVDatabase.get_ohlcv_data)
    assert "fetch_from = last_cached_date - timedelta(days=1)" in src, (
        "fetch_from no longer compensates for stooq.fetch()'s +1-day "
        "exclusive start — the tail can silently stop being refetched again"
    )
