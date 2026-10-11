"""Tests for the historical event-date backfill so carrac.facts.
event_study's n>=8 gate can clear on real FRED release history.

Covers:
  1. _fetch_fred_release_dates — window filtering, failure -> [].
  2. _upsert_release_events — dedup semantics (rowcount=0 == already exists),
     empty input is a no-op, DB failure -> 0 (never raises).
  3. _count_past_events — reads the COUNT, failure -> 0 (never raises).
  4. _backfill_past_release_events — the core of the work:
     - per-type gating: sparse types get fetched, types that already clear
       _BACKFILL_MIN_PAST_ROWS are skipped WITHOUT a network call (cheap on
       every nightly re-run).
     - inserts land with the correct (ts, event_type, title, impact) shape.
     - the fetch window never reaches into today/future (it stays out of
       the future — that's the forward fetch's job).
     - a failure in one type's fetch doesn't block the others.
  5. fetch_fred_release_events — no API key -> 0, no calls; backfill runs
     even when the forward fetch finds nothing; a backfill exception never
     propagates out (non-fatal, matches every other collector path in this
     module); dedup on re-run (second call inserts 0 more).

Pure monkeypatched logic — no live DB, no live FRED calls.
"""
from __future__ import annotations

from datetime import date


# ---------------------------------------------------------------------------
# Shared fakes (mirrors the FakeConn/FakeCursor pattern in test_event_actuals.py)
# ---------------------------------------------------------------------------

class FakeCursor:
    """Records executed SQL/params; INSERT rowcount is 0 for keys already in
    `existing_keys` (simulating the WHERE NOT EXISTS dedup), else 1."""

    def __init__(self, existing_keys=None, count_by_type=None):
        self.existing_keys = existing_keys or set()
        self.count_by_type = count_by_type or {}
        self.rowcount = 0
        self.executed = []
        self._last_kind = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if 'COUNT(*)' in sql:
            self._last_kind = 'count'
            self._count_params = params
        elif 'INSERT INTO' in sql:
            self._last_kind = 'insert'
            ts, etype, title, impact, ts2, etype2 = params
            key = (ts2, etype2)
            self.rowcount = 0 if key in self.existing_keys else 1
            if self.rowcount:
                self.existing_keys.add(key)

    def fetchone(self):
        if self._last_kind == 'count':
            event_type = self._count_params[0]
            return {'cnt': self.count_by_type.get(event_type, 0)}
        return None


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        pass


# ---------------------------------------------------------------------------
# 1. _fetch_fred_release_dates
# ---------------------------------------------------------------------------

class TestFetchFredReleaseDates:
    def test_filters_to_window(self, monkeypatch):
        from quant.collectors import event_calendar as ec

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {'release_dates': [
                    {'date': '2023-12-31'},  # before window
                    {'date': '2024-01-15'},  # in window
                    {'date': '2024-06-30'},  # in window
                    {'date': '2026-01-01'},  # after window
                ]}

        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured['params'] = params
            return FakeResp()

        monkeypatch.setattr(ec.requests, 'get', fake_get)
        dates = ec._fetch_fred_release_dates(54, 'KEY', '2024-01-01', '2024-12-31')
        assert dates == ['2024-01-15', '2024-06-30']
        assert captured['params']['release_id'] == 54
        assert captured['params']['realtime_start'] == '2024-01-01'

    def test_returns_empty_on_failure(self, monkeypatch):
        from quant.collectors import event_calendar as ec

        def fake_get(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(ec.requests, 'get', fake_get)
        assert ec._fetch_fred_release_dates(54, 'KEY', '2024-01-01', '2024-12-31') == []


# ---------------------------------------------------------------------------
# 2. _upsert_release_events
# ---------------------------------------------------------------------------

class TestUpsertReleaseEvents:
    def test_empty_rows_is_noop(self, monkeypatch):
        from quant.collectors import event_calendar as ec
        called = []
        monkeypatch.setattr(ec, 'get_conn', lambda: called.append(1) or FakeConn(FakeCursor()))
        assert ec._upsert_release_events([]) == 0
        assert called == []  # never even opened a connection

    def test_dedups_on_ts_and_type(self, monkeypatch):
        from quant.collectors import event_calendar as ec
        cursor = FakeCursor(existing_keys={('2024-01-15', 'PCE')})
        monkeypatch.setattr(ec, 'get_conn', lambda: FakeConn(cursor))

        rows = [
            ('2024-01-15', 'PCE', 'Personal Income & Outlays (PCE)', 'high'),  # already exists
            ('2024-02-15', 'PCE', 'Personal Income & Outlays (PCE)', 'high'),  # new
        ]
        inserted = ec._upsert_release_events(rows)
        assert inserted == 1

    def test_db_failure_returns_zero(self, monkeypatch):
        from quant.collectors import event_calendar as ec

        class ExplodingConn:
            def __enter__(self):
                raise RuntimeError("db down")

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(ec, 'get_conn', lambda: ExplodingConn())
        rows = [('2024-01-15', 'PCE', 'title', 'high')]
        assert ec._upsert_release_events(rows) == 0


# ---------------------------------------------------------------------------
# 3. _count_past_events
# ---------------------------------------------------------------------------

class TestCountPastEvents:
    def test_returns_count(self, monkeypatch):
        from quant.collectors import event_calendar as ec
        cursor = FakeCursor(count_by_type={'PCE': 12})
        monkeypatch.setattr(ec, 'get_conn', lambda: FakeConn(cursor))
        assert ec._count_past_events('PCE', '2026-07-02') == 12

    def test_failure_returns_zero(self, monkeypatch):
        from quant.collectors import event_calendar as ec

        class ExplodingConn:
            def __enter__(self):
                raise RuntimeError("db down")

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(ec, 'get_conn', lambda: ExplodingConn())
        assert ec._count_past_events('PCE', '2026-07-02') == 0


# ---------------------------------------------------------------------------
# 4. _backfill_past_release_events
# ---------------------------------------------------------------------------

class TestBackfillPastReleaseEvents:
    def test_skips_types_that_already_clear_the_gate(self, monkeypatch):
        """No network call at all when every type already has >= min rows —
        this is what keeps the backfill cheap on every nightly re-run."""
        from quant.collectors import event_calendar as ec

        monkeypatch.setattr(ec, '_count_past_events',
                             lambda etype, before: ec._BACKFILL_MIN_PAST_ROWS)

        def boom(*a, **kw):
            raise AssertionError("network fetch should not be called")

        monkeypatch.setattr(ec, '_fetch_fred_release_dates', boom)
        inserted = ec._backfill_past_release_events('KEY', today=date(2026, 7, 2))
        assert inserted == 0

    def test_fetches_and_inserts_when_sparse(self, monkeypatch):
        from quant.collectors import event_calendar as ec

        monkeypatch.setattr(ec, '_count_past_events', lambda etype, before: 0)
        monkeypatch.setattr(ec, '_fetch_fred_release_dates',
                             lambda rid, key, lo, hi: ['2024-01-15', '2024-04-15'])

        recorded = {}

        def fake_upsert(rows):
            recorded['rows'] = rows
            return len(rows)

        monkeypatch.setattr(ec, '_upsert_release_events', fake_upsert)

        inserted = ec._backfill_past_release_events('KEY', today=date(2026, 7, 2))
        # 5 types (PCE, PPI, GDP + backfill-only CPI, NFP) x 2 dates each
        assert inserted == 10
        assert len(recorded['rows']) == 10
        etypes = {row[1] for row in recorded['rows']}
        assert etypes == {'PCE', 'PPI', 'GDP', 'CPI', 'NFP'}
        # Shape matches the merged (etype, title, impact) mapping
        _merged = {**ec.FRED_RELEASE_EVENTS, **ec.FRED_RELEASE_EVENTS_BACKFILL_ONLY}
        for ts, etype, title, impact in recorded['rows']:
            expected_etype, expected_title, expected_impact = next(
                v for v in _merged.values() if v[0] == etype
            )
            assert title == expected_title
            assert impact == expected_impact

    def test_per_type_independent_gating(self, monkeypatch):
        """PCE sparse, PPI and GDP already sufficient -> only PCE is fetched."""
        from quant.collectors import event_calendar as ec

        counts = {'PCE': 0, 'PPI': 20, 'GDP': 20, 'CPI': 20, 'NFP': 20}
        monkeypatch.setattr(ec, '_count_past_events', lambda etype, before: counts[etype])

        fetched_for = []

        def fake_fetch(rid, key, lo, hi):
            fetched_for.append(rid)
            return ['2024-01-15']

        monkeypatch.setattr(ec, '_fetch_fred_release_dates', fake_fetch)
        monkeypatch.setattr(ec, '_upsert_release_events', lambda rows: len(rows))

        ec._backfill_past_release_events('KEY', today=date(2026, 7, 2))
        # release_id 54 == PCE (see FRED_RELEASE_EVENTS)
        assert fetched_for == [54]

    def test_window_never_reaches_today_or_future(self, monkeypatch):
        """Pins no future dates: the backfill's hi bound is yesterday,
        and lo is years_back years before today — it never overlaps with the
        forward fetch's [today, today+window_days] territory."""
        from quant.collectors import event_calendar as ec

        monkeypatch.setattr(ec, '_count_past_events', lambda etype, before: 0)
        monkeypatch.setattr(ec, '_upsert_release_events', lambda rows: 0)

        seen_windows = []

        def fake_fetch(rid, key, lo, hi):
            seen_windows.append((lo, hi))
            return []

        monkeypatch.setattr(ec, '_fetch_fred_release_dates', fake_fetch)

        today = date(2026, 7, 2)
        ec._backfill_past_release_events('KEY', years_back=2, today=today)

        assert seen_windows, "expected at least one fetch for sparse types"
        for lo, hi in seen_windows:
            assert hi < today.isoformat()  # never today or later
            assert lo == '2024-07-02'      # exactly years_back years before today

    def test_one_type_failure_does_not_block_others(self, monkeypatch):
        """A blown-up fetch for one release_id must not stop the others from
        being backfilled — mirrors the module's existing tolerant-per-item
        pattern (fetch_fred_release_events already `continue`s per release)."""
        from quant.collectors import event_calendar as ec

        monkeypatch.setattr(ec, '_count_past_events', lambda etype, before: 0)

        def flaky_fetch(rid, key, lo, hi):
            if rid == 46:  # PPI
                raise RuntimeError("FRED hiccup")
            return ['2024-01-15']

        # _fetch_fred_release_dates itself already swallows exceptions and
        # returns [] in production; simulate that contract here directly
        # rather than re-testing requests-level plumbing.
        def safe_flaky_fetch(rid, key, lo, hi):
            try:
                return flaky_fetch(rid, key, lo, hi)
            except RuntimeError:
                return []

        monkeypatch.setattr(ec, '_fetch_fred_release_dates', safe_flaky_fetch)

        recorded = {}

        def fake_upsert(rows):
            recorded['rows'] = rows
            return len(rows)

        monkeypatch.setattr(ec, '_upsert_release_events', fake_upsert)

        inserted = ec._backfill_past_release_events('KEY', today=date(2026, 7, 2))
        etypes = {row[1] for row in recorded['rows']}
        # PPI contributed nothing, but didn't block the other four types
        assert etypes == {'PCE', 'GDP', 'CPI', 'NFP'}
        assert inserted == 4


# ---------------------------------------------------------------------------
# 5. fetch_fred_release_events (public entrypoint — the existing invocation
#    path: quant.collectors.fred_macro's nightly collect() and
#    morning_engine's afternoon refresh both call this unchanged)
# ---------------------------------------------------------------------------

class TestFetchFredReleaseEventsWithBackfill:
    def test_no_api_key_returns_zero_no_calls(self, monkeypatch):
        from quant.collectors import event_calendar as ec
        monkeypatch.delenv('FRED_API_KEY', raising=False)

        def boom(*a, **kw):
            raise AssertionError("should never be reached without an API key")

        monkeypatch.setattr(ec, '_fetch_fred_release_dates', boom)
        monkeypatch.setattr(ec, '_backfill_past_release_events', boom)
        assert ec.fetch_fred_release_events() == 0

    def test_backfill_runs_even_when_forward_finds_nothing(self, monkeypatch):
        from quant.collectors import event_calendar as ec
        monkeypatch.setenv('FRED_API_KEY', 'TESTKEY')

        monkeypatch.setattr(ec, '_fetch_fred_release_dates', lambda *a, **kw: [])
        monkeypatch.setattr(ec, '_backfill_past_release_events', lambda api_key: 3)

        assert ec.fetch_fred_release_events(window_days=45) == 3

    def test_forward_and_backfill_counts_combine(self, monkeypatch):
        from quant.collectors import event_calendar as ec
        monkeypatch.setenv('FRED_API_KEY', 'TESTKEY')

        monkeypatch.setattr(ec, '_fetch_fred_release_dates',
                             lambda rid, key, lo, hi: ['2026-08-01'])
        monkeypatch.setattr(ec, '_upsert_release_events', lambda rows: len(rows))
        monkeypatch.setattr(ec, '_backfill_past_release_events', lambda api_key: 5)

        # 3 release types forward (PCE/PPI/GDP), 1 date each = 3, + backfill 5
        assert ec.fetch_fred_release_events(window_days=45) == 8

    def test_backfill_exception_is_non_fatal(self, monkeypatch):
        """A raising backfill must never take down the forward fetch's
        result — matches the try/except wrapping every other side-effect in
        this collector (fill_event_actuals, fetch_fred_release_events itself
        from its own callers)."""
        from quant.collectors import event_calendar as ec
        monkeypatch.setenv('FRED_API_KEY', 'TESTKEY')

        monkeypatch.setattr(ec, '_fetch_fred_release_dates', lambda *a, **kw: [])

        def boom(api_key):
            raise RuntimeError("backfill blew up")

        monkeypatch.setattr(ec, '_backfill_past_release_events', boom)

        # Forward found nothing, backfill raised -> still returns cleanly (0),
        # never propagates.
        assert ec.fetch_fred_release_events(window_days=45) == 0

    def test_dedup_on_rerun(self, monkeypatch):
        """Re-running with the same upstream dates a second time inserts 0
        additional rows (the WHERE NOT EXISTS dedup already covers this at
        the SQL layer — this test exercises it through the real
        _upsert_release_events against a FakeConn that remembers state
        across both calls)."""
        from quant.collectors import event_calendar as ec
        monkeypatch.setenv('FRED_API_KEY', 'TESTKEY')

        cursor = FakeCursor()
        monkeypatch.setattr(ec, 'get_conn', lambda: FakeConn(cursor))
        monkeypatch.setattr(ec, '_fetch_fred_release_dates',
                             lambda rid, key, lo, hi: ['2026-08-01'])
        monkeypatch.setattr(ec, '_backfill_past_release_events', lambda api_key: 0)

        first = ec.fetch_fred_release_events(window_days=45)
        second = ec.fetch_fred_release_events(window_days=45)

        assert first == 3   # PCE + PPI + GDP, all new
        assert second == 0  # same three keys already exist -> rowcount 0 each
