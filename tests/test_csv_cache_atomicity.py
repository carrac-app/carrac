"""Pins CSVCache against torn reads / non-atomic writes.

The failure mode: ``CSVCache.save`` opening the cache file with mode ``'w'``
(which truncates *before* ``flock`` is acquired) while ``CSVCache.load`` takes
no lock at all. A reader landing mid-write gets a
valid CSV *prefix* -- pandas parses it happily, no exception -- so the caller
sees a frame that silently loses its tail. ``_get_crypto_ohlcv`` then delta-tops
that short frame and writes it back, welding the hole into the file permanently.
"""

import multiprocessing as mp
import threading
import time

import pandas as pd

from csv_cache import CSVCache


def _frame(rows: int, start: str = "2017-08-17") -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=rows, freq="D", name="timestamp")
    return pd.DataFrame(
        {
            "open": range(rows),
            "high": range(rows),
            "low": range(rows),
            "close": range(rows),
            "volume": range(rows),
        },
        index=idx,
    )


def _writer_loop(cache_dir: str, rows: int, seconds: float) -> None:
    cache = CSVCache(cache_dir=cache_dir)
    df = _frame(rows)
    deadline = time.time() + seconds
    while time.time() < deadline:
        cache.save("BTC", df)


def test_save_is_atomic_under_concurrent_readers(tmp_path):
    """A reader must never observe a truncated prefix of the cache file.

    Reproduces the production failure: while one process rewrites BTC_daily.csv,
    another reads it and gets a short frame with no error raised.
    """
    cache_dir = str(tmp_path)
    rows = 3259  # roughly the real BTC_daily.csv row count
    cache = CSVCache(cache_dir=cache_dir)
    cache.save("BTC", _frame(rows))

    writer = mp.Process(target=_writer_loop, args=(cache_dir, rows, 3.0))
    writer.start()
    try:
        short_reads = []
        deadline = time.time() + 3.0
        while time.time() < deadline:
            df = cache.load("BTC")
            if df is not None and len(df) != rows:
                short_reads.append(len(df))
        assert not short_reads, (
            f"torn read: observed {len(short_reads)} truncated frames "
            f"(row counts e.g. {sorted(set(short_reads))[:5]}); expected always {rows}"
        )
    finally:
        writer.join(timeout=10)
        if writer.is_alive():  # pragma: no cover - safety net
            writer.terminate()
            writer.join(timeout=5)


def test_save_never_leaves_file_empty(tmp_path):
    """The file must always be readable; 'w' truncation left a 0-byte window."""
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save("ETH", _frame(500))
    path = cache.get_path("ETH")

    writer = mp.Process(target=_writer_loop, args=(str(tmp_path), 500, 2.0))
    writer.start()
    try:
        empties = 0
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if path.exists() and path.stat().st_size == 0:
                empties += 1
        assert empties == 0, f"cache file was observed 0 bytes {empties} times mid-write"
    finally:
        writer.join(timeout=10)
        if writer.is_alive():  # pragma: no cover - safety net
            writer.terminate()
            writer.join(timeout=5)


class _StubCache:
    def __init__(self, df):
        self._df = df
        self.saved = None

    def load(self, symbol, timeframe="daily"):
        return self._df

    def save(self, symbol, df, timeframe="daily", allow_shrink=False, is_crypto=False):
        self.saved = df


def test_short_cache_is_rebuilt_not_gap_welded(monkeypatch):
    """A cache whose tail is older than the delta window must not be topped up.

    The production failure: a torn read yielded 346 rows ending 2023-05-26; the
    delta path fetched only its 30-day cap and concat'd, producing a 376-row
    frame with a 1129-day hole that was then persisted.
    """
    from database import OHLCVDatabase

    stale_tail = pd.Timestamp("2023-05-26")
    cached = _frame(346, start="2022-06-15")
    cached.index = pd.date_range(end=stale_tail, periods=346, freq="D", name="timestamp")

    db = OHLCVDatabase.__new__(OHLCVDatabase)
    db.cache = _StubCache(cached)

    requested = {}

    def fake_fetch(symbol, days=365):
        requested["days"] = days
        # Upstream (Binance) always has the continuous series.
        return _frame(days, start=str((pd.Timestamp.now() - pd.Timedelta(days=days - 1)).date()))

    db._fetch_crypto_data = fake_fetch
    monkeypatch.setattr(OHLCVDatabase, "_is_crypto", lambda self, s: True, raising=False)
    monkeypatch.setattr(OHLCVDatabase, "_is_crypto_metric", lambda self, s: False, raising=False)

    out = db._get_crypto_ohlcv("BTC")

    assert requested["days"] > 1000, (
        f"expected a full rebuild for a >1000d stale tail, got days={requested['days']}"
    )
    gaps = out.index.to_series().diff().dropna()
    assert gaps.max() <= pd.Timedelta(days=2), (
        f"result still contains a {gaps.max()} hole — gap was welded in"
    )
    saved = db.cache.saved
    assert saved is not None, "rebuilt frame must be persisted"
    sgaps = saved.index.to_series().diff().dropna()
    assert sgaps.max() <= pd.Timedelta(days=2), "a gapped frame was written back to disk"


# --- shrink guard -----------------------------------------------------------
# A cache must never be replaced by a dramatically smaller frame. That is the
# signature of a lost/failed read upstream: _get_crypto_ohlcv delta-topping a
# torn frame (3259 -> 376 rows), the cold-cache branch firing because load()
# returned None on a transient error (3259 -> 1503), or the WS writers doing
# `df = new_row` when load() returns None (thousands of rows -> 1).

def test_save_refuses_catastrophic_shrink(tmp_path):
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save("BTC", _frame(3259))

    cache.save("BTC", _frame(376))  # the real corrupt write

    got = cache.load("BTC")
    assert len(got) == 3259, (
        f"shrink guard let a {376}-row frame replace a 3259-row cache (now {len(got)})"
    )


def test_save_refuses_single_row_overwrite(tmp_path):
    """The ccxt_ws / hyperliquid_ws `df = new_row` path when load() returns None."""
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save("ETH", _frame(5000), timeframe="1h")

    cache.save("ETH", _frame(1), timeframe="1h")

    assert len(cache.load("ETH", "1h")) == 5000


def test_save_allows_normal_growth_and_small_trims(tmp_path):
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save("SOL", _frame(1000))

    cache.save("SOL", _frame(1001))
    assert len(cache.load("SOL")) == 1001

    # the 5000-row tail cap and similar minor trims must still go through
    cache.save("SOL", _frame(950))
    assert len(cache.load("SOL")) == 950


def test_save_allows_shrink_when_explicit(tmp_path):
    """Fixed-window replacements (crypto metrics 365d) opt in."""
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save("TOTAL", _frame(2000))

    cache.save("TOTAL", _frame(365), allow_shrink=True)
    assert len(cache.load("TOTAL")) == 365


def test_shrink_guard_does_not_block_bootstrap(tmp_path):
    """A tiny existing cache must not lock out a legitimate rewrite."""
    cache = CSVCache(cache_dir=str(tmp_path))
    cache.save("NEW", _frame(5))
    cache.save("NEW", _frame(2))
    assert len(cache.load("NEW")) == 2


def test_failed_rebuild_backs_off_instead_of_refetching_1500d(monkeypatch):
    """A permanently-stale symbol must not retry a full rebuild on every access.

    KAS is 48d stale and delisted: the rebuild fetch returns empty, the cache
    stays stale, so without a backoff every single request re-attempts a
    1500-day upstream fetch forever.
    """
    import database
    from database import OHLCVDatabase

    stale = pd.date_range(end=pd.Timestamp.now().normalize() - pd.Timedelta(days=48),
                          periods=883, freq="D", name="timestamp")
    cached = _frame(883)
    cached.index = stale

    db = OHLCVDatabase.__new__(OHLCVDatabase)
    db.cache = _StubCache(cached)

    calls = []

    def dead_source(symbol, days=365):
        calls.append(days)
        return pd.DataFrame()  # delisted: source has nothing

    db._fetch_crypto_data = dead_source

    store = {}
    monkeypatch.setattr(database, "_is_rebuild_backed_off",
                        lambda s: s.upper() in store, raising=False)
    monkeypatch.setattr(database, "_set_rebuild_backoff",
                        lambda s: store.__setitem__(s.upper(), 1), raising=False)

    for _ in range(5):
        db._get_crypto_ohlcv("KAS")

    assert len(calls) == 1, (
        f"expected 1 rebuild attempt then backoff, got {len(calls)}: {calls}"
    )


# --- derived-index contamination -------------------------------------------
# TOTAL/TOTAL2/TOTAL3/BTC.D/USDT.D/OTHERS.D are summed from constituent caches
# via a `.tail(days)` window. That counts ROWS, not DAYS, so a constituent with
# an interior hole contributes dates from years back and the derived index
# inherits a gap. Example: constituents with ~500-day holes drag the market-cap
# frame years back, and a 270-day orphan block of old dates ends up in every
# dominance series.

def test_constituent_window_is_date_based_not_row_based():
    from database import OHLCVDatabase

    today = pd.Timestamp.now().normalize()
    # A holed constituent, shaped like the real KCS: an old block, a long
    # hole, then a fresh tail. 365 ROWS reaches back past the hole.
    old = pd.date_range(end=today - pd.Timedelta(days=900), periods=300, freq="D")
    fresh = pd.date_range(end=today, periods=95, freq="D")
    idx = old.append(fresh)
    df = pd.DataFrame({"close": range(len(idx))}, index=idx)

    assert df.tail(365).index[0] < today - pd.Timedelta(days=365), (
        "fixture is wrong: row-based tail should reach past the window"
    )

    window = OHLCVDatabase._constituent_window(df, 365)

    assert not window.empty
    assert window.index[0] >= today - pd.Timedelta(days=366), (
        f"window still reaches back to {window.index[0].date()} — a holed "
        f"constituent can still drag the derived index backwards"
    )
    assert window.index[-1] == df.index[-1], "must keep the fresh tail"


def test_constituent_window_drops_a_constituent_with_nothing_recent():
    """A stale/delisted constituent must contribute nothing, not old rows."""
    from database import OHLCVDatabase

    today = pd.Timestamp.now().normalize()
    idx = pd.date_range(end=today - pd.Timedelta(days=800), periods=400, freq="D")
    df = pd.DataFrame({"close": range(len(idx))}, index=idx)

    assert OHLCVDatabase._constituent_window(df, 365).empty


def test_constituent_window_passes_healthy_data_through():
    from database import OHLCVDatabase

    today = pd.Timestamp.now().normalize()
    idx = pd.date_range(end=today, periods=1500, freq="D")
    df = pd.DataFrame({"close": range(1500)}, index=idx)

    w = OHLCVDatabase._constituent_window(df, 365)
    assert 360 <= len(w) <= 367, f"expected ~365 days, got {len(w)}"
    assert w.index[-1] == today


# --- single-flight -----------------------------------------------------------
# Without a lock the same 1500-day Binance fetch runs several times: once per
# app process and once in the worker, all doing identical upstream work and
# racing to write one file.

class _FakeRedis:
    """Minimal SET NX EX / GET / DELETE, enough for the single-flight lock."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


def _stale_db(days_stale=48, rows=883):
    from database import OHLCVDatabase

    idx = pd.date_range(end=pd.Timestamp.now().normalize() - pd.Timedelta(days=days_stale),
                        periods=rows, freq="D", name="timestamp")
    cached = _frame(rows)
    cached.index = idx
    db = OHLCVDatabase.__new__(OHLCVDatabase)
    db.cache = _StubCache(cached)
    return db


def test_loser_does_not_duplicate_an_expensive_fetch(monkeypatch):
    """Winner never finishes: loser waits out the timeout, serves stale, no fetch."""
    import database

    fake = _FakeRedis()
    monkeypatch.setattr(database, "_get_cc_redis", lambda: fake)
    monkeypatch.setattr(database, "_is_rebuild_backed_off", lambda s: False)
    monkeypatch.setattr(database, "_FETCH_SLOT_WAIT", 1)
    monkeypatch.setattr(database, "_FETCH_SLOT_POLL", 0.05)

    # Another process already holds the slot for KAS.
    assert database._try_acquire_fetch_slot("KAS", "rebuild")

    db = _stale_db()
    calls = []
    db._fetch_crypto_data = lambda symbol, days=365: calls.append(days) or pd.DataFrame()

    out = db._get_crypto_ohlcv("KAS")

    assert calls == [], f"loser duplicated the upstream fetch: {calls}"
    assert out is not None and not out.empty, "loser must still be served the stale cache"


def test_loser_picks_up_the_winners_result(monkeypatch):
    """Winner finishes mid-wait: loser must return the winner's fuller data."""
    import database

    fake = _FakeRedis()
    monkeypatch.setattr(database, "_get_cc_redis", lambda: fake)
    monkeypatch.setattr(database, "_is_rebuild_backed_off", lambda s: False)
    monkeypatch.setattr(database, "_FETCH_SLOT_WAIT", 5)
    monkeypatch.setattr(database, "_FETCH_SLOT_POLL", 0.05)

    database._try_acquire_fetch_slot("KAS", "rebuild")

    db = _stale_db()
    calls = []
    db._fetch_crypto_data = lambda symbol, days=365: calls.append(days) or pd.DataFrame()

    # The winner completes shortly after we start waiting: it writes a fuller
    # series and frees the slot.
    rebuilt = _frame(1500)
    rebuilt.index = pd.date_range(end=pd.Timestamp.now().normalize(), periods=1500,
                                  freq="D", name="timestamp")

    def winner_finishes():
        time.sleep(0.2)
        db.cache._df = rebuilt
        database._release_fetch_slot("KAS", "rebuild")

    t = threading.Thread(target=winner_finishes)
    t.start()
    try:
        out = db._get_crypto_ohlcv("KAS")
    finally:
        t.join(timeout=5)

    assert calls == [], f"loser fetched anyway: {calls}"
    assert len(out) == 1500, (
        f"loser returned {len(out)} rows — should have picked up the winner's 1500"
    )


def test_winner_performs_the_fetch_and_frees_the_slot(monkeypatch):
    import database

    fake = _FakeRedis()
    monkeypatch.setattr(database, "_get_cc_redis", lambda: fake)
    monkeypatch.setattr(database, "_is_rebuild_backed_off", lambda s: False)

    db = _stale_db()
    calls = []

    def fetch(symbol, days=365):
        calls.append(days)
        idx = pd.date_range(end=pd.Timestamp.now().normalize(), periods=days, freq="D",
                            name="timestamp")
        out = _frame(days)
        out.index = idx
        return out

    db._fetch_crypto_data = fetch
    db._get_crypto_ohlcv("KAS")

    assert len(calls) == 1, f"expected exactly one fetch, got {calls}"
    assert database._try_acquire_fetch_slot("KAS", "rebuild"), (
        "slot was not released after the winner finished"
    )


def test_single_flight_fails_open_without_redis(monkeypatch):
    """No Redis must never mean no data."""
    import database

    monkeypatch.setattr(database, "_get_cc_redis", lambda: None)
    monkeypatch.setattr(database, "_is_rebuild_backed_off", lambda s: False)

    db = _stale_db()
    calls = []

    def fetch(symbol, days=365):
        calls.append(days)
        idx = pd.date_range(end=pd.Timestamp.now().normalize(), periods=days, freq="D",
                            name="timestamp")
        out = _frame(days)
        out.index = idx
        return out

    db._fetch_crypto_data = fetch
    out = db._get_crypto_ohlcv("KAS")

    assert len(calls) == 1, "with no Redis the fetch must still happen"
    assert not out.empty
