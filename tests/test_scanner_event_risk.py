"""Tests for carrac/scanner/event_risk.py.

Covers:
  - imminent HIGH-impact event within the window -> returned.
  - no HIGH-impact event within the window -> None.
  - a nearer LOW/medium-impact event does not surface (impact filter).
  - past events are never returned.
  - any DB error -> None (scanner must never be affected).
  - TTL cache: a second call within the TTL does not re-query the DB.

Pure monkeypatch of carrac.db_pool.get_quant_conn as re-exported into the module under
test — no live DB. Mirrors the FakeConn/FakeCursor pattern used in
tests/test_event_actuals.py.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from carrac.scanner import event_risk as er


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeCursor:
    """Simulates:
        SELECT ts, title, impact FROM quant.events
        WHERE impact = %s AND ts >= CURRENT_DATE
        ORDER BY ts ASC LIMIT 5
    against an in-memory list of {'ts' (date), 'title', 'impact'} rows.
    """

    def __init__(self, events):
        self._events = events
        self._result = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        impact = params[0] if params else None
        rows = [e for e in self._events if e['impact'] == impact]
        rows.sort(key=lambda e: e['ts'])
        self._result = rows[:5]

    def fetchall(self):
        return self._result


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cursor


class ExplodingConn:
    """Simulates a DB connection failure — raised on entering the `with` block."""

    def __enter__(self):
        raise RuntimeError("connection refused")

    def __exit__(self, *a):
        return False


def _today_utc():
    """The same 'today' the code under test uses.

    Not `date.today()`, which is LOCAL time, while
    `event_risk.get_imminent_event` computes `datetime.now(timezone.utc).date()`
    and the SQL filters on Postgres `CURRENT_DATE` (the prod server runs UTC).
    On a UTC+2 machine the two disagree between 00:00 and 02:00 local, so every
    offset in this file would be silently one day larger than intended during
    that window and tests would fail for two hours a night.

    Keep this UTC-based. Production is consistently UTC end to end; it was the
    fixture that was wrong, not the code.
    """
    return datetime.now(timezone.utc).date()


def _row(days_offset, title='FOMC Rate Decision', impact='high'):
    ts = _today_utc() + timedelta(days=days_offset)
    return {'ts': ts, 'title': title, 'impact': impact}


@pytest.fixture(autouse=True)
def _clear_cache():
    """event_risk.py caches in-process — reset around every test."""
    er._cache.clear()
    yield
    er._cache.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetImminentEvent:
    def test_high_impact_event_within_window_returned(self, monkeypatch):
        events = [_row(1, title='FOMC Rate Decision', impact='high')]
        monkeypatch.setattr(er, 'get_conn', lambda: FakeConn(FakeCursor(events)))

        result = er.get_imminent_event(hours=36)

        assert result is not None
        assert result['title'] == 'FOMC Rate Decision'
        assert result['impact'] == 'high'
        assert result['hours_until'] == 24.0

    def test_no_event_within_window_returns_none(self, monkeypatch):
        # 5 days out = 120h, outside the 36h window
        events = [_row(5, title='CPI Release', impact='high')]
        monkeypatch.setattr(er, 'get_conn', lambda: FakeConn(FakeCursor(events)))

        result = er.get_imminent_event(hours=36)

        assert result is None

    def test_low_impact_event_ignored(self, monkeypatch):
        # Medium-impact ECB tomorrow, high-impact CPI two days out. Since the
        # DB-side filter is impact='high', ECB never surfaces even though
        # it's nearer in time.
        events = [
            _row(1, title='ECB Rate Decision', impact='medium'),
            _row(2, title='CPI Release', impact='high'),
        ]
        monkeypatch.setattr(er, 'get_conn', lambda: FakeConn(FakeCursor(events)))

        result = er.get_imminent_event(hours=72)

        assert result is not None
        assert result['title'] == 'CPI Release'

    def test_past_event_not_returned(self, monkeypatch):
        events = [_row(-1, title='Yesterday CPI', impact='high')]
        monkeypatch.setattr(er, 'get_conn', lambda: FakeConn(FakeCursor(events)))

        result = er.get_imminent_event(hours=36)

        assert result is None

    def test_today_event_returns_zero_hours(self, monkeypatch):
        events = [_row(0, title='FOMC Rate Decision', impact='high')]
        monkeypatch.setattr(er, 'get_conn', lambda: FakeConn(FakeCursor(events)))

        result = er.get_imminent_event(hours=36)

        assert result is not None
        assert result['hours_until'] == 0.0

    def test_db_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(er, 'get_conn', lambda: ExplodingConn())

        result = er.get_imminent_event(hours=36)

        assert result is None

    def test_ttl_cache_hits_once(self, monkeypatch):
        events = [_row(0, title='NFP Release', impact='high')]
        cursor = FakeCursor(events)
        calls = {'n': 0}

        def fake_get_conn():
            calls['n'] += 1
            return FakeConn(cursor)

        monkeypatch.setattr(er, 'get_conn', fake_get_conn)

        first = er.get_imminent_event(hours=36)
        second = er.get_imminent_event(hours=36)

        assert first == second
        assert calls['n'] == 1, "second call within the TTL must not re-query the DB"

    def test_cache_expiry_triggers_requery(self, monkeypatch):
        """After the TTL window elapses, a new query is issued."""
        events = [_row(0, title='NFP Release', impact='high')]
        cursor = FakeCursor(events)
        calls = {'n': 0}

        def fake_get_conn():
            calls['n'] += 1
            return FakeConn(cursor)

        monkeypatch.setattr(er, 'get_conn', fake_get_conn)

        fake_time = {'t': 1000.0}
        monkeypatch.setattr(er._time, 'monotonic', lambda: fake_time['t'])

        er.get_imminent_event(hours=36)
        assert calls['n'] == 1

        fake_time['t'] += er.CACHE_TTL_SECONDS + 1
        er.get_imminent_event(hours=36)
        assert calls['n'] == 2, "cache must expire after CACHE_TTL_SECONDS"


class TestUtcInvariance:
    """Pins UTC invariance.

    `get_imminent_event` derives hours_until from a calendar-day difference in
    UTC, so its answer must not depend on what time of day it is asked. That
    invariant holds in production -- Postgres runs UTC and the module uses
    datetime.now(timezone.utc) -- and a fixture using local-time `date.today()`
    would make the SUITE quietly disagree with the code for two hours a night
    on a UTC+2 machine.

    Testing a whole-day invariant at whichever hour CI happens to run is how
    that survived. These sweep the clock instead.
    """

    @pytest.mark.parametrize('hour', range(24))
    def test_hours_until_is_stable_at_every_hour_of_day(self, monkeypatch, hour):
        frozen = datetime(2026, 3, 17, hour, 41, tzinfo=timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz else frozen.replace(tzinfo=None)

        monkeypatch.setattr(er, 'datetime', _FrozenDatetime)

        events = [{'ts': frozen.date() + timedelta(days=1),
                   'title': 'FOMC Rate Decision', 'impact': 'high'}]
        monkeypatch.setattr(er, 'get_conn', lambda: FakeConn(FakeCursor(events)))

        result = er.get_imminent_event(hours=36)

        assert result is not None, f'event one day out vanished at {hour:02d}:00 UTC'
        assert result['hours_until'] == 24.0, f'drifted at {hour:02d}:00 UTC'

    @pytest.mark.parametrize('hour', (0, 1, 22, 23))
    def test_today_and_yesterday_classified_correctly_near_midnight(self, monkeypatch, hour):
        """The hours either side of midnight UTC are where a local/UTC mix-up shows."""
        frozen = datetime(2026, 3, 17, hour, 41, tzinfo=timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz else frozen.replace(tzinfo=None)

        monkeypatch.setattr(er, 'datetime', _FrozenDatetime)

        today_evt = {'ts': frozen.date(), 'title': 'Today CPI', 'impact': 'high'}
        past_evt = {'ts': frozen.date() - timedelta(days=1),
                    'title': 'Yesterday CPI', 'impact': 'high'}

        monkeypatch.setattr(er, 'get_conn', lambda: FakeConn(FakeCursor([today_evt])))
        assert er.get_imminent_event(hours=36)['hours_until'] == 0.0

        er._cache.clear()
        monkeypatch.setattr(er, 'get_conn', lambda: FakeConn(FakeCursor([past_evt])))
        assert er.get_imminent_event(hours=36) is None


def test_fixture_uses_utc_not_local_time():
    """A `date.today()` CALL here would make the fixture depend on local time, not UTC.

    Parsed rather than grepped, so the explanation above can name the offending
    idiom without the guard tripping over its own prose.
    """
    import ast

    tree = ast.parse(open(__file__).read())
    offenders = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == 'today'
        and isinstance(n.func.value, ast.Name) and n.func.value.id == 'date'
    ]
    assert not offenders, (
        f'date.today() called at line(s) {offenders}: that is LOCAL time, while '
        'event_risk works in UTC end to end. Use _today_utc().'
    )
