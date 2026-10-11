"""The weekend-bar pair must group in the error tracker.

Both tripwire lines must log the symbol and date as %-args with a constant
template. Baking them into the message (an f-string) makes the error tracker, which
groups the logging integration's events on the log TEMPLATE, open one issue
per symbol per weekend date. With a constant template there is one issue per
line type with a rising count.

For an ESTABLISHED weekday-only series — the case below, GOLD/SILVER with 90
days of history — the drop that follows 'about to gain' a few lines later
always removes exactly the same rows, so 'about to gain' would report a write
that never happens. It does not fire for this established case; only
'dropped' does. The first test class below covers that. The
second covers the one case where a row genuinely IS kept — a series too
short for trades_weekends() to judge — which is where 'about to gain' still
fires, and still must group the same way.
"""

import logging

import pandas as pd
import pytest

import csv_cache
from csv_cache import CSVCache


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


def _bars(idx):
    idx = pd.to_datetime(list(idx))
    return pd.DataFrame({'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0, 'volume': 0},
                        index=idx)


def _weekday_series():
    return _bars(pd.bdate_range(start='2026-01-05', periods=90))


def test_the_dropped_line_groups_across_symbols_established_series(tmp_path, fake_redis, caplog):
    """Established weekday-only series (90 days): a later change removed the
    'about to gain' line here entirely (it never survived to a write), so
    only 'dropped' fires — and it must still group across symbols."""
    caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
    cache = CSVCache(cache_dir=str(tmp_path))
    for symbol, sunday in (('GOLD', '2026-08-09'), ('SILVER', '2026-08-16')):
        clean = _weekday_series()
        cache.save(symbol, clean)
        cache.save(symbol, pd.concat([clean, _bars([sunday])]))

    gain = [r for r in caplog.records if 'about to gain' in r.getMessage()]
    dropped = [r for r in caplog.records if 'phantom weekend row' in r.getMessage()]
    assert len(gain) == 0, (
        "An established series must not page a write that never "
        f"happens: {[r.getMessage() for r in gain]}"
    )
    assert len(dropped) == 2, 'both symbols must still trip the drop'

    assert len({r.msg for r in dropped}) == 1, [r.msg for r in dropped]
    for r in dropped:
        assert 'GOLD' not in r.msg and 'SILVER' not in r.msg and '2026-08' not in r.msg, r.msg
        assert r.args, 'symbol and dates must travel as %-args'


def test_the_about_to_gain_line_groups_across_symbols_short_series(tmp_path, fake_redis, caplog):
    """The one case that change left 'about to gain' firing in: a series too
    short for trades_weekends() to judge, where the row is genuinely kept.
    Must still group the same way as before."""
    caplog.set_level(logging.ERROR, logger='carrac.csv_cache')
    cache = CSVCache(cache_dir=str(tmp_path))
    for symbol, sunday in (('GOLD', '2026-08-02'), ('SILVER', '2026-08-09')):
        cache.save(symbol, _bars(['2026-07-30', '2026-07-31']))  # Thu, Fri — too short
        cache.save(symbol, _bars(['2026-07-30', '2026-07-31', sunday]))

    gain = [r for r in caplog.records if 'about to gain' in r.getMessage()]
    dropped = [r for r in caplog.records if 'phantom weekend row' in r.getMessage()]
    assert len(gain) == 2, 'both symbols must still trip the (kept) gain line'
    assert len(dropped) == 0, 'a too-short-to-judge series is never dropped'

    assert len({r.msg for r in gain}) == 1, [r.msg for r in gain]
    for r in gain:
        assert 'GOLD' not in r.msg and 'SILVER' not in r.msg and '2026-08' not in r.msg, r.msg
        assert r.args, 'symbol and dates must travel as %-args'
