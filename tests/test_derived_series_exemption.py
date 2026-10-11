""""Is this shape legitimate?" is answered by reference data.

A bar that is flat OHLC with zero volume records no trading. Four guards act on
that predicate, and all four needed the same escape hatch: **when EVERY row in a
series looks synthetic, that shape is the series' normal encoding** — the derived
ratio feeds (ALTBTC, TOTAL3BTC) publish one computed value a day exactly that way
and are perfectly alive.

That escape hatch was inferred FROM THE BARS, and inference from bars is what this
test exists to stop. A series that is
all-synthetic *from its first row* is exempted by shape, so it can never be
detected.

So the rule changes shape → reference data:

    the all-rows-look-synthetic exemption applies ONLY to a series the routing
    map says is derived.

One cell of the matrix moves, and it is the defect cell:

| series                          | before   | after    |
|---------------------------------|----------|----------|
| derived, all rows synthetic      | exempt   | exempt   |
| derived, some real rows          | filtered | filtered |
| NOT derived, all rows synthetic  | exempt   | **CAUGHT** |
| NOT derived, some real rows      | filtered | filtered |

On the real universe only the entirely synthetic caches (ALTBTC and TOTAL3BTC)
are all-synthetic, and both are ``derived``. No non-derived file is
all-synthetic, so the changed cell is empty on real data and the rule is a
no-op there while being strictly tighter for the future.

The answer comes from ``instrument_master.derive_authoritative_source`` — a pure
lookup over the routing-map snapshot — NOT from the ``_instrument_master.json``
store. The store's own ``authoritative_source`` field is computed by that same
function, so reading the file would be reading a cached copy of the same answer
with three extra failure modes (missing, empty, stale). A pure function cannot be
any of those, which is why the reference-data degradation branch has no counterpart here.

The residual risk therefore lives in exactly one place: the snapshot drifting from
the real map it mirrors. Two tests below pin it, and they are the "if this breaks,
how would I find out" answer for the whole change.
"""
import ast
import datetime
import logging
from pathlib import Path

import pandas as pd
import pytest

import csv_cache
import instrument_master as im
from csv_cache import CSVCache

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------

class TestIsDerivedSeries:
    def test_a_computed_ratio_feed_is_derived(self):
        assert im.is_derived_series('ALTBTC') is True
        assert im.is_derived_series('TOTAL3BTC') is True
        assert im.is_derived_series('TOTAL') is True

    def test_a_real_instrument_is_not(self):
        assert im.is_derived_series('BTC') is False
        assert im.is_derived_series('AGIX') is False
        assert im.is_derived_series('AAPL') is False
        assert im.is_derived_series('GOLD') is False

    def test_the_mangled_on_disk_stem_answers_the_same_as_the_ticker(self):
        """``csv_cache.get_path`` writes BTC.D as ``BTC_D_daily.csv``, so the
        guards see the mangled stem while the routing map knows the dotted form.
        Answering 'unknown' for the stem is the BTC.D false-orphan bug in a
        third disguise, and here it would silently drop BTC.D's exemption."""
        assert im.derive_authoritative_source('BTC_D') == 'unknown'  # the trap
        assert im.is_derived_series('BTC.D') is True
        assert im.is_derived_series('BTC_D') is True
        assert im.is_derived_series('OTHERS_D') is True
        assert im.is_derived_series('USDT_D') is True

    def test_it_is_case_insensitive(self):
        assert im.is_derived_series('altbtc') is True
        assert im.is_derived_series('btc_d') is True

    def test_an_unknown_or_absent_symbol_is_not_derived(self):
        """The honest default. 'I have never heard of this' is not evidence that
        every one of its bars is a legitimate computed value."""
        assert im.is_derived_series('NOSUCHTICKER') is False
        assert im.is_derived_series('') is False
        assert im.is_derived_series(None) is False


# ---------------------------------------------------------------------------
# Drift — the snapshot against the maps it mirrors
# ---------------------------------------------------------------------------

def _database_crypto_metric_map_keys():
    """The literal keys of ``database.py::OHLCVDatabase.crypto_metric_map``.

    Read with ``ast`` rather than by constructing the class: that __init__ opens
    a database connection, and a drift test that needs prod infrastructure to run
    is a drift test that stops running.
    """
    tree = ast.parse((REPO_ROOT / 'database.py').read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Attribute)
                    and target.attr == 'crypto_metric_map'
                    and isinstance(node.value, ast.Dict)):
                return {ast.literal_eval(k) for k in node.value.keys}
    raise AssertionError(
        'could not find `self.crypto_metric_map = {...}` in database.py — the '
        'drift test has lost sight of what it guards, which is worse than the '
        'drift it was written to catch'
    )


class TestTheSnapshotDoesNotDriftFromTheMaps:
    """Everything above rests on ``_CRYPTO_METRIC_SYMBOLS`` being an accurate
    copy of the real routing map. Nothing pinned that until now.

    The failure it prevents is quiet and expensive: add a twelfth derived feed to
    ``database.py`` and forget this snapshot, and the new feed is not 'derived',
    so the write guard starts refusing its bars and the read guard declares it
    dead — a live feed deleted by a guard, which is the worst outcome in this
    whole arc.
    """

    def test_the_instrument_master_snapshot_matches_database_py(self):
        assert im._CRYPTO_METRIC_SYMBOLS == _database_crypto_metric_map_keys(), (
            'instrument_master._CRYPTO_METRIC_SYMBOLS has drifted from '
            'database.py::crypto_metric_map. Every derived-series guard reads the '
            'snapshot, so a series missing from it loses its exemption and its '
            'bars start being refused.'
        )

    def test_the_cron_hardcoded_list_matches_the_snapshot(self):
        """The monitor cannot import either map — it is stdlib-only so it still
        runs when the app is down (the host has no pandas outside the container).
        So the rule is necessarily written a third time, in on-disk stem form.
        This test is what keeps that copy honest; the test suite has pandas and
        can import both sides, even though the cron cannot."""
        from scripts.cache_integrity_check import DERIVED_SERIES

        expected = {im._cache_stem(t) for t in im._CRYPTO_METRIC_SYMBOLS}
        assert DERIVED_SERIES == expected, (
            'scripts/cache_integrity_check.py::DERIVED_SERIES has drifted from '
            'instrument_master._CRYPTO_METRIC_SYMBOLS'
        )


# ---------------------------------------------------------------------------
# Guard 1 — the read guard (carrac/precompute.py::_last_real_bar)
# ---------------------------------------------------------------------------

NOW = datetime.datetime(2026, 8, 14, 12, 0)


def _all_flat_but_advancing(periods=40, end=None):
    """The ALTBTC shape: every row flat and volumeless, the VALUE advancing
    daily. Byte-identical to fabrication; the feed is alive."""
    vals = [5.0e-06 + i * 1e-09 for i in range(periods)]
    idx = pd.date_range(end=end or pd.Timestamp(NOW.date()), periods=periods, freq='D')
    return pd.DataFrame(
        {'open': vals, 'high': vals, 'low': vals, 'close': vals,
         'volume': [0] * periods},
        index=idx,
    )


def _all_flat_and_frozen(periods=40, end=None):
    """The AGIX shape from row one: flat, volumeless AND not moving. A dead
    upstream pair repeating its last known price forever."""
    idx = pd.date_range(end=end or pd.Timestamp(NOW.date()), periods=periods, freq='D')
    return pd.DataFrame(
        {'open': 1.5, 'high': 1.5, 'low': 1.5, 'close': 1.5, 'volume': 0},
        index=idx,
    )


class TestTheReadGuard:
    def test_a_derived_feed_encoded_this_way_is_still_alive(self):
        """The exemption that must survive. ALTBTC moved 5.0735e-06 ->
        5.1199e-06 -> 5.0967e-06 across the last three days while looking
        synthetic on every row."""
        from carrac.precompute import _is_history_stale

        assert _is_history_stale(_all_flat_but_advancing(), 'ALTBTC', now=NOW) is False

    def test_the_same_shape_on_a_real_instrument_is_now_caught(self):
        """THE POINT OF THIS CHANGE. Identical bytes, different symbol. Under the old
        shape rule this series was exempt and therefore undetectable no matter
        how long it ran — the mechanism that hides a dead feed indefinitely."""
        from carrac.precompute import _is_history_stale

        assert _is_history_stale(_all_flat_and_frozen(), 'AGIX', now=NOW) is True

    def test_an_advancing_but_undeclared_series_is_also_caught(self):
        """Deliberately the uncomfortable case: the values ADVANCE, so this looks
        like a live derived feed — but nothing routes it as one. A shape rule
        would rescue it; the honest answer is that Carrac has no
        record of it being a computed series, and a guard must not infer that
        from the bars. If a real feed ever lands here the fix is to route it,
        not to soften the guard."""
        from carrac.precompute import _is_history_stale

        assert _is_history_stale(_all_flat_but_advancing(), 'RATIOFEED', now=NOW) is True

    def test_a_derived_feed_with_real_bars_is_still_filtered(self):
        """'derived' exempts the ALL-synthetic case only. It must not become a
        blanket 'never check this symbol' — BTC.D's feed can die and be papered
        over by injected candles like any other."""
        from carrac.precompute import _is_history_stale

        real = pd.DataFrame(
            {'open': 60.0, 'high': 61.0, 'low': 59.0, 'close': 60.5, 'volume': 5000.0},
            index=pd.date_range(end=pd.Timestamp('2026-06-01'), periods=40, freq='D'),
        )
        dead_tail = _all_flat_and_frozen(
            periods=70, end=pd.Timestamp(NOW.date()))
        df = pd.concat([real, dead_tail]).sort_index()

        assert _is_history_stale(df, 'BTC.D', now=NOW) is True, (
            'a derived series whose real bars stopped was rescued by its own '
            'exemption — the exemption is for the encoding, not the symbol'
        )

    def test_the_symbol_is_required(self):
        """A caller that cannot say which symbol it holds cannot be answered.
        Defaulting would pick one of the two wrong answers silently: rescue a
        dead feed, or declare a live one dead. TypeError is the honest outcome."""
        from carrac.precompute import _is_history_stale

        with pytest.raises(TypeError):
            _is_history_stale(_all_flat_and_frozen())


class TestTheLiveCallersPassTheSymbol:
    """The parameter is only load-bearing if the two real call sites
    actually fill it — this is the 'built but never connected' check."""

    def test_precompute_passes_the_symbol_to_describe_staleness(self):
        src = (REPO_ROOT / 'carrac' / 'precompute.py').read_text()
        assert "describe_staleness(tf_data['1D'], symbol)" in src, (
            'carrac/precompute.py stopped naming the symbol it is judging'
        )

    def test_database_passes_the_symbol_to_is_history_stale(self):
        src = (REPO_ROOT / 'database.py').read_text()
        assert '_is_history_stale(df_base, symbol)' in src, (
            'database.py stopped naming the symbol it is judging'
        )


# ---------------------------------------------------------------------------
# Guard 2 — the write guard (csv_cache.py::_reject_persisted_synthetic_bars)
# ---------------------------------------------------------------------------

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


class TestTheWriteGuard:
    def test_a_derived_feed_is_written_untouched(self, tmp_path, fake_redis, today):
        cache = CSVCache(cache_dir=str(tmp_path))
        frame = _all_flat_but_advancing(periods=40, end=today)
        cache.save('ALTBTC', frame, is_crypto=True)

        assert len(cache.load('ALTBTC')) == len(frame), (
            'a derived ratio feed was mistaken for fabrication and deleted'
        )

    def test_an_all_synthetic_frame_for_a_real_instrument_is_refused(
            self, tmp_path, fake_redis, today, caplog):
        """The write-side half of the AGIX mechanism: the very first save of a
        series that is fabrication end to end. Nothing on disk to compare
        against, every row flat — a shape-only rule would wave the whole frame through."""
        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        cache = CSVCache(cache_dir=str(tmp_path))
        frame = _all_flat_and_frozen(periods=40, end=today)
        # Stand-in symbol. AGIX is on
        # instrument_master.LIFECYCLE_OVERRIDES, so CSVCache.save refuses the
        # write outright and this guard never gets to run.
        # The rule under test is not symbol-specific.
        cache.save('SOL', frame, is_crypto=True)

        on_disk = cache.load('SOL')
        past = [d for d in on_disk.index if d.normalize() < today] if len(on_disk) else []
        assert not past, (
            'a frame that records no trading anywhere was persisted as history '
            'for a symbol nothing declares to be a computed series'
        )
        assert any('synthetic' in r.message.lower() for r in caplog.records), (
            'the refusal must be loud enough to reach Bugsink'
        )

    def test_todays_bar_still_survives_for_a_real_instrument(
            self, tmp_path, fake_redis, today):
        """The exemption that has nothing to do with this change and must not move: at
        00:00 UTC a genuine kline for the session in progress is flat with zero
        volume. BTC carries one every night."""
        cache = CSVCache(cache_dir=str(tmp_path))
        real = pd.DataFrame(
            {'open': 100.0, 'high': 102.0, 'low': 98.0, 'close': 101.0,
             'volume': 5000.0},
            index=pd.date_range(end=today - pd.Timedelta(days=1), periods=40),
        )
        flat_today = pd.DataFrame(
            {'open': 101.0, 'high': 101.0, 'low': 101.0, 'close': 101.0,
             'volume': 0.0},
            index=[today],
        )
        cache.save('BTC', pd.concat([real, flat_today]), is_crypto=True)

        assert today in cache.load('BTC').index, "today's opening kline was discarded"


# ---------------------------------------------------------------------------
# Guard 3 — the purge script (scripts/purge_synthetic_bars.py)
# ---------------------------------------------------------------------------

class TestThePurgeScript:
    def test_a_derived_feed_is_fully_exempt(self):
        from scripts.purge_synthetic_bars import trailing_synthetic_rows

        frame = _all_flat_but_advancing(periods=70, end=pd.Timestamp('2026-08-10'))
        victims = trailing_synthetic_rows(frame, pd.Timestamp('2026-08-11'), 'ALTBTC')
        assert len(victims) == 0, 'purging this would delete the entire feed'

    def test_the_same_frame_for_a_real_instrument_is_purgeable(self):
        """This script DELETES history, so the change is stated twice: the
        exemption is gone for undeclared symbols, and today's bar still lives."""
        from scripts.purge_synthetic_bars import trailing_synthetic_rows

        today = pd.Timestamp('2026-08-11')
        frame = _all_flat_and_frozen(periods=70, end=today)
        victims = trailing_synthetic_rows(frame, today, 'AGIX')
        assert len(victims) == 69
        assert today not in victims


# ---------------------------------------------------------------------------
# Guard 4 — the monitor (scripts/cache_integrity_check.py)
# ---------------------------------------------------------------------------

class TestTheMonitor:
    """The cron's copy of the rule. It reaches the same verdict through its own
    stdlib path, which is the whole reason the drift tests above exist."""

    @staticmethod
    def _write(tmp_path, symbol, rows, today=datetime.date(2026, 8, 14)):
        lines = ['timestamp,open,high,low,close,volume']
        for days_ago, o, h, low, c, v in rows:
            d = today - datetime.timedelta(days=days_ago)
            lines.append(f'{d},{o},{h},{low},{c},{v}')
        (tmp_path / f'{symbol}_daily.csv').write_text('\n'.join(lines) + '\n')

    @staticmethod
    def _manifest(tmp_path, symbols):
        import json
        (tmp_path / '_tracked_symbols.json').write_text(json.dumps({
            'symbols': list(symbols),
            'written_at': datetime.datetime(2026, 8, 14, 6, 0).isoformat(),
        }))

    def _findings(self, tmp_path):
        from scripts.cache_integrity_check import scan
        findings, _counts = scan(data_dir=str(tmp_path))
        return findings

    def test_a_derived_feed_encoded_this_way_is_silent(self, tmp_path):
        rows = [(i, 5e-06, 5e-06, 5e-06, 5e-06, 0) for i in range(100)]
        self._write(tmp_path, 'ALTBTC', rows)
        self._manifest(tmp_path, ['ALTBTC'])

        assert [f for f in self._findings(tmp_path) if 'NO-REAL-BARS' in f] == []

    def test_a_real_instrument_with_no_real_bars_anywhere_is_reported(self, tmp_path):
        """The monitor's version of the blind spot. The file runs to today and
        every check that reasons about DATES says it is healthy — because it is
        being updated. What it is being updated WITH is the finding."""
        rows = [(i, 1.5, 1.5, 1.5, 1.5, 0) for i in range(100)]
        self._write(tmp_path, 'DEADCOIN', rows)
        self._manifest(tmp_path, ['DEADCOIN'])

        hits = [f for f in self._findings(tmp_path) if 'NO-REAL-BARS' in f]
        assert len(hits) == 1, (
            'a series in which not one row records trading was reported healthy'
        )
        assert hits[0].startswith('DEADCOIN: NO-REAL-BARS')

    def test_a_series_with_real_bars_is_silent(self, tmp_path):
        rows = [(i, 1.5, 1.53, 1.47, 1.51, 5000.0) for i in range(100)]
        self._write(tmp_path, 'LIVECOIN', rows)
        self._manifest(tmp_path, ['LIVECOIN'])

        assert [f for f in self._findings(tmp_path) if 'NO-REAL-BARS' in f] == []
