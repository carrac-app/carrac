"""csv_cache per-symbol tripwires must group in the error tracker.

The refused-write tripwires (three of them) and the backdated-fill line
(all in this file) were f-strings with the symbol (and often the offending
dates) baked into the message. The error tracker groups a logging-integration event on
the log TEMPLATE, so each one opened a fresh issue per symbol per date
instead of one issue per line — one of them alone opened 13 separate issues
in one week. The two weekend-bar lines in the same file follow the same
rule: symbol/dates travel as
%-args, so ``record.msg`` (what the error tracker groups on) stays constant across
symbols while ``record.getMessage()`` still renders the full text a human
reads.

The carried-volume survival line (same file, a few lines from the
backdated-fill line below) is deliberately EXCLUDED and left as an
f-string on purpose. The
last test class here pins that it has NOT been touched.
"""
import datetime
import logging

import pandas as pd
import pytest

import csv_cache
from csv_cache import CSVCache


def _day(offset):
    return (datetime.date.today() - datetime.timedelta(days=offset)).isoformat()


def _frame(rows):
    df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df.set_index('timestamp')


def _real(offsets, base=80.0):
    # Distinct volumes per bar so the carried-volume beacon (and
    # the synthetic guard) stay quiet for an unrelated reason.
    return [(_day(o), base, base + 2, base - 1, base + 1, 1000.0 + o) for o in offsets]


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(csv_cache, '_get_redis', lambda: r)
    return r


def _hits(caplog, level, marker):
    return [r for r in caplog.records if r.levelno == level and marker in r.getMessage()]


class TestSyntheticRefusalGroupsAcrossSymbols:
    def test_template_constant_rendered_message_carries_the_symbol(
            self, tmp_path, fake_redis, caplog):
        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        cache = CSVCache(cache_dir=str(tmp_path))
        for symbol in ('ZTESTA', 'ZTESTB'):
            history = _frame(_real(range(10, 3, -1)))
            cache.save(symbol, history, is_crypto=True)
            poisoned = pd.concat([history,
                                  _frame([(_day(2), 100.0, 100.0, 100.0, 100.0, 0.0)])])
            cache.save(symbol, poisoned, is_crypto=True)

        hits = _hits(caplog, logging.ERROR, 'synthetic-bar: refused')
        assert len(hits) == 2, 'both symbols must still trip the guard'
        assert len({r.msg for r in hits}) == 1, 'must group under ONE template'
        for r in hits:
            assert 'ZTESTA' not in r.msg and 'ZTESTB' not in r.msg, r.msg
            assert r.args, 'symbol must travel as a %-arg, not be baked in'
        assert 'ZTESTA' in hits[0].getMessage()
        assert 'ZTESTB' in hits[1].getMessage()


class TestZeroLegRefusalGroupsAcrossSymbols:
    def test_template_constant_rendered_message_carries_the_symbol(
            self, tmp_path, fake_redis, caplog):
        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        cache = CSVCache(cache_dir=str(tmp_path))
        for symbol in ('OIL', 'GC'):
            history = _frame(_real([5, 4, 3]))
            cache.save(symbol, history, is_crypto=True)
            poisoned = pd.concat([history,
                                  _frame([(_day(2), 0.0, 0.0, 0.0, 87.06, 0.0)])])
            cache.save(symbol, poisoned, is_crypto=True)

        hits = _hits(caplog, logging.ERROR, 'malformed-bar: refused')
        assert len(hits) == 2
        assert len({r.msg for r in hits}) == 1
        for r in hits:
            assert 'OIL' not in r.msg and 'GC' not in r.msg, r.msg
            assert r.args
        assert 'OIL' in hits[0].getMessage()
        assert 'GC' in hits[1].getMessage()


class TestOpenOutsideRangeGroupsAcrossSymbols:
    """If the symbol is baked into the message, ``record.msg`` for AM and TSLA
    differ (each carries its own symbol and offending date) and this
    assertion fails — the exact shape of 13 separate error-tracker issues opened in
    one week."""

    def test_template_has_no_symbol_rendered_message_does(
            self, tmp_path, fake_redis, caplog):
        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        cache = CSVCache(cache_dir=str(tmp_path))
        for symbol in ('AM', 'TSLA'):
            history = _frame(_real([5, 4, 3]))
            cache.save(symbol, history, is_crypto=True)
            poisoned = pd.concat([
                history,
                _frame([(_day(0), 22.42, 22.18, 22.0199, 22.07, 28721.0)]),
            ])
            cache.save(symbol, poisoned, is_crypto=True)

        hits = _hits(caplog, logging.ERROR, 'open-outside-range: refused')
        assert len(hits) == 2, 'both symbols must still trip the guard'

        # The template (record.msg) must be IDENTICAL across symbols — this
        # is what the error tracker groups on. Symbol and date go in as log
        # arguments, not into the template, so every record shares one msg.
        assert len({r.msg for r in hits}) == 1, (
            f"expected ONE shared template, got {len({r.msg for r in hits})}: "
            f"{[r.msg for r in hits]}"
        )
        for r in hits:
            assert 'AM' not in r.msg and 'TSLA' not in r.msg, (
                f"symbol baked into the template (not a %-arg): {r.msg!r}"
            )
            assert r.args, 'symbol/dates must travel as %-args'

        # The rendered text — what a human reads in the error tracker's detail pane —
        # still names the right symbol.
        assert 'AM' in hits[0].getMessage()
        assert 'TSLA' in hits[1].getMessage()


class TestBackdatedFillGroupsAcrossSymbols:
    def test_template_constant_rendered_message_carries_the_symbol(
            self, tmp_path, fake_redis, caplog):
        caplog.set_level(logging.WARNING, logger='carrac.csv_cache')
        cache = CSVCache(cache_dir=str(tmp_path))
        for symbol in ('CORN', 'WHEAT'):
            cache.save(symbol, _frame(_real([10, 9, 7, 6])), is_crypto=True)
            cache.save(symbol, _frame(_real([10, 9, 8, 7, 6])), is_crypto=True)

        hits = _hits(caplog, logging.WARNING, 'backdated fill')
        assert len(hits) == 2, 'both symbols must still record the interior fill'
        assert len({r.msg for r in hits}) == 1
        for r in hits:
            assert 'CORN' not in r.msg and 'WHEAT' not in r.msg, r.msg
            assert r.args
        assert 'CORN' in hits[0].getMessage()
        assert 'WHEAT' in hits[1].getMessage()


class TestCarriedVolumeLineIsUntouched:
    """Pending decision (error-path re-scope) — this grouping change must NOT
    convert this line. Pinned so a future pass does not fix it by accident."""

    def test_carried_volume_line_is_still_an_fstring(self, tmp_path, fake_redis, caplog):
        caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
        cache = CSVCache(cache_dir=str(tmp_path))
        # Days 5 and 4 ago share one volume: a settled, recent session that
        # repeats the prior session's volume.
        carried = _frame([
            (_day(6), 80.0, 82.0, 79.0, 81.0, 1006.0),
            (_day(5), 80.0, 82.0, 79.0, 81.0, 500.0),
            (_day(4), 80.0, 82.0, 79.0, 81.0, 500.0),
            (_day(3), 80.0, 82.0, 79.0, 81.0, 1003.0),
        ])
        cache.save('CORN', carried, is_crypto=True)
        # The carried value is now on disk. It must SURVIVE a refetch to
        # trip the (re-scoped) error path.
        cache.save('CORN', carried, is_crypto=True)

        hits = _hits(caplog, logging.ERROR, 'carried volume SURVIVED')
        assert len(hits) == 1
        # Unlike the other four, the symbol IS baked into record.msg here —
        # confirming the grouping change left this one exactly as it found it.
        assert 'CORN' in hits[0].msg
