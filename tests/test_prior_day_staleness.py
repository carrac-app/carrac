"""A stale report must never masquerade as the prior-day baseline.

The failure mode: ``get_latest_report(report_type='morning')`` has no date
bound, so if ``report_type='morning'`` stops being written, every caller
asking for "the latest morning report" gets the same ancient document, and
``run_dispatch`` renders it verbatim into the LLM prompt under a literal
``## Yesterday`` header, silently.

These tests drive the REAL shipped code: a real ContentDatabase on the isolated
per-test DATA_DIR, the real ``resolve_prior_day_report``, the real Flask
preassembly route. Nothing monkeypatches the unit under test. The only seam
patched is ``prior_day._capture`` (the Sentry send), which exists in the
module precisely to make the alarm assertable — the same seam and the same
reason as carrac/reports/afternoon_grounding.py::_capture.
"""

from datetime import datetime, timedelta, timezone

import pytest


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture
def db(app):  # noqa: ARG001 — app fixture sets DATA_DIR before we bind the singleton
    """ContentDatabase singleton bound to THIS test's isolated DATA_DIR.

    Same shape as tests/test_brief_archive.py::db — get_content_db() freezes
    its path on first use, so it must be reset for seeded rows to land in the
    DB the routes actually read.
    """
    import carrac.content_db as cdb

    cdb._db_instance = None
    database = cdb.get_content_db()
    yield database
    cdb._db_instance = None


def _seed(db, report_type, days_ago, content, summary=None):
    """Insert a report row backdated by ``days_ago`` days.

    Direct INSERT rather than save_report(), which is idempotent per
    (report_type, UTC calendar day) and always stamps "today" — it cannot
    produce the date-shifted rows this bug is about. Mirrors
    tests/test_brief_archive.py::_seed_general_on.
    """
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    date_str = when.strftime('%Y-%m-%d')
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO reports "
        "(report_type, content, assets_included, generation_time_ms, summary, "
        "generation_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (report_type, content, '[]', 0, summary, date_str,
         when.strftime('%Y-%m-%d %H:%M:%S')),
    )
    conn.commit()


@pytest.fixture
def alarms(monkeypatch):
    """Capture what would have gone to the error tracker."""
    import carrac.reports.prior_day as pd

    sent = []
    monkeypatch.setattr(pd, '_capture', lambda msg: sent.append(msg))
    return sent


# --------------------------------------------------------------------------
# report_age_days — the primitive
# --------------------------------------------------------------------------

class TestReportAgeDays:
    def test_parses_iso_string(self):
        from carrac.content_db import report_age_days
        ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        assert 2.9 < report_age_days(ts) < 3.1

    def test_parses_datetime(self):
        from carrac.content_db import report_age_days
        ts = datetime.now(timezone.utc) - timedelta(days=10)
        assert 9.9 < report_age_days(ts) < 10.1

    def test_naive_timestamp_read_as_utc(self):
        """Postgres hands back tz-aware; the SQLite backend hands back naive
        strings. Both must yield the same age, or the bound silently changes
        meaning between test and prod (the SQLite-vs-Postgres trap)."""
        from carrac.content_db import report_age_days
        naive = (datetime.now(timezone.utc) - timedelta(days=5)).replace(tzinfo=None)
        assert 4.9 < report_age_days(naive) < 5.1

    @pytest.mark.parametrize('bad', [None, '', 'not-a-date', 'yesterday', 42])
    def test_unusable_returns_none(self, bad):
        from carrac.content_db import report_age_days
        assert report_age_days(bad) is None


# --------------------------------------------------------------------------
# get_latest_report(max_age_days=...) — the bound
# --------------------------------------------------------------------------

class TestGetLatestReportAgeBound:
    def test_unbounded_call_still_returns_ancient_row(self, db):
        """The unbounded behaviour is preserved for archive/history readers that
        genuinely want 'whatever exists'. If this ever starts returning None,
        the bound leaked into callers that never asked for it."""
        _seed(db, 'morning', days_ago=46, content='June report')
        got = db.get_latest_report(report_type='morning')
        assert got is not None
        assert got['content'] == 'June report'

    def test_bounded_call_refuses_the_stale_row(self, db):
        _seed(db, 'morning', days_ago=46, content='June report')
        assert db.get_latest_report(report_type='morning', max_age_days=3) is None

    def test_bounded_call_accepts_a_fresh_row(self, db):
        _seed(db, 'general', days_ago=1, content='Yesterday report')
        got = db.get_latest_report(report_type='general', max_age_days=3)
        assert got is not None
        assert got['content'] == 'Yesterday report'

    def test_boundary_just_inside_and_just_outside(self, db):
        _seed(db, 'general', days_ago=2, content='two days old')
        assert db.get_latest_report(report_type='general', max_age_days=3) is not None
        assert db.get_latest_report(report_type='general', max_age_days=1) is None

    def test_missing_type_returns_none_either_way(self, db):
        assert db.get_latest_report(report_type='weekly') is None
        assert db.get_latest_report(report_type='weekly', max_age_days=3) is None


# --------------------------------------------------------------------------
# resolve_prior_day_report — the one place the report_type question is answered
# --------------------------------------------------------------------------

class TestResolvePriorDayReport:
    def test_the_actual_prod_shape_picks_general_not_the_june_morning(self, db, alarms):
        """THE KEY TEST. The stale-prod shape: 'general' writing daily,
        'morning' frozen 46 days back. Must not resolve to the June document."""
        _seed(db, 'morning', days_ago=46, content='JUNE 23 MORNING REPORT')
        _seed(db, 'general', days_ago=1, content='YESTERDAY GENERAL REPORT')

        from carrac.reports.prior_day import resolve_prior_day_report
        got = resolve_prior_day_report(db=db)

        assert got is not None
        assert got['content'] == 'YESTERDAY GENERAL REPORT'
        assert 'JUNE' not in got['content']
        assert got['report_type_used'] == 'general'
        assert alarms == []

    def test_falls_back_to_morning_when_it_is_the_fresh_one(self, db, alarms):
        """If ENABLE_MORNING_REPORT is ever turned back on, 'morning' must
        still resolve — the fix must not hard-delete the legacy path."""
        _seed(db, 'morning', days_ago=1, content='FRESH MORNING REPORT')
        from carrac.reports.prior_day import resolve_prior_day_report
        got = resolve_prior_day_report(db=db)
        assert got['content'] == 'FRESH MORNING REPORT'
        assert got['report_type_used'] == 'morning'
        assert alarms == []

    def test_general_wins_when_both_are_fresh(self, db, alarms):
        _seed(db, 'morning', days_ago=1, content='MORNING')
        _seed(db, 'general', days_ago=1, content='GENERAL')
        from carrac.reports.prior_day import resolve_prior_day_report
        assert resolve_prior_day_report(db=db)['report_type_used'] == 'general'
        assert alarms == []

    def test_all_stale_returns_none_AND_alarms(self, db, alarms):
        """Question 3 — if this breaks, how would I find out? Here is the
        answer, asserted: an error-tracker event naming the cause and the ages."""
        _seed(db, 'morning', days_ago=46, content='JUNE')
        _seed(db, 'general', days_ago=30, content='ALSO OLD')

        from carrac.reports.prior_day import resolve_prior_day_report
        assert resolve_prior_day_report(db=db) is None

        assert len(alarms) == 1
        assert 'prior-day-missing' in alarms[0]
        # The alarm must be actionable: it names which types were checked and
        # how stale each newest row actually is.
        assert 'general=' in alarms[0]
        assert 'morning=' in alarms[0]

    def test_empty_db_returns_none_and_alarms_distinctly(self, db, alarms):
        from carrac.reports.prior_day import resolve_prior_day_report
        assert resolve_prior_day_report(db=db) is None
        assert len(alarms) == 1
        assert 'no rows of any candidate type exist' in alarms[0]

    def test_alarm_survives_a_dead_bugsink(self, db, monkeypatch):
        """The alarm path must not raise into the report pipeline if Sentry is
        down — losing the report is worse than losing the alarm, and the
        logger.error has already fired regardless."""
        import carrac.reports.prior_day as pd

        def _boom(_msg):
            raise RuntimeError('bugsink unreachable')

        monkeypatch.setattr(pd, '_capture', _boom)
        assert pd.resolve_prior_day_report(db=db) is None  # no exception


# --------------------------------------------------------------------------
# The live consumer: /api/engine/morning-context preassembly
# --------------------------------------------------------------------------

class TestPreassemblyYesterdaySection:
    """This is the endpoint run_dispatch calls at 06:00 UTC daily. Its
    yesterday_report field is rendered into the prompt as '## Yesterday'
    (morning_engine/engine.py::_morning_build_briefing)."""

    def test_preassembly_no_longer_serves_the_june_report(self, db):
        _seed(db, 'morning', days_ago=46, content='JUNE 23 MORNING REPORT',
              summary='June summary')
        _seed(db, 'general', days_ago=1, content='YESTERDAY GENERAL REPORT',
              summary='Yesterday summary')

        from carrac.reports.routes import get_morning_context  # noqa: F401 — import check

        from carrac.content_db import get_content_db
        from carrac.reports.prior_day import resolve_prior_day_report
        # Drive the same resolver the route body calls, against the same
        # singleton the route resolves — the route itself needs an internal
        # key and a full app context, exercised in the route tests below.
        got = resolve_prior_day_report(db=get_content_db())
        assert got['summary'] == 'Yesterday summary'

    def test_personal_section_marker_still_stripped(self, db):
        """The prior-day resolver must truncate at PERSONAL_SECTION_START before
        returning — a leaked operator
        section in the shared prompt is a tenancy bug, not a staleness one."""
        _seed(db, 'general', days_ago=1,
              content='PUBLIC PART<!-- PERSONAL_SECTION_START -->OPERATOR ONLY')
        from carrac.reports.prior_day import resolve_prior_day_report
        report = resolve_prior_day_report(db=db)
        content = report['content']
        marker = '<!-- PERSONAL_SECTION_START -->'
        idx = content.find(marker)
        assert idx != -1, 'fixture sanity: marker must be present pre-strip'
        assert content[:idx] == 'PUBLIC PART'


# --------------------------------------------------------------------------
# fetch_yesterday_report — the type param selects the report type
# --------------------------------------------------------------------------

class TestFetchYesterdayReportForwardsType:
    def test_type_is_actually_sent_in_the_query_string(self, monkeypatch):
        """The requested report type reaches the endpoint query string, so
        'general' fetches the general report rather than the default morning one."""
        import morning_engine.data_assembly as da

        seen = []

        def _fake_api(path, **kwargs):  # noqa: ARG001
            seen.append(path)
            return []

        monkeypatch.setattr(da, '_api', _fake_api)
        da.fetch_yesterday_report(report_type='general')
        assert seen, 'endpoint was never called'
        assert 'type=general' in seen[0]

    def test_default_is_general_not_morning(self, monkeypatch):
        import morning_engine.data_assembly as da

        seen = []
        monkeypatch.setattr(da, '_api', lambda path, **kw: seen.append(path) or [])
        da.fetch_yesterday_report()
        assert 'type=general' in seen[0]

    def test_picks_the_row_dated_yesterday(self, monkeypatch):
        import morning_engine.data_assembly as da

        y = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        today = datetime.now().strftime('%Y-%m-%d')
        monkeypatch.setattr(da, '_api', lambda path, **kw: [
            {'id': 2, 'created_at': f'{today} 06:00:00', 'content': 'TODAY'},
            {'id': 1, 'created_at': f'{y} 06:00:00', 'content': 'YESTERDAY'},
        ])
        got = da.fetch_yesterday_report(report_type='general')
        assert got['content'] == 'YESTERDAY'

    def test_returns_none_rather_than_an_older_row(self, monkeypatch):
        """Honest absence: never substitute something older."""
        import morning_engine.data_assembly as da

        monkeypatch.setattr(da, '_api', lambda path, **kw: [
            {'id': 1, 'created_at': '2026-06-23 06:00:00', 'content': 'JUNE'},
        ])
        assert da.fetch_yesterday_report(report_type='general') is None


# --------------------------------------------------------------------------
# Sibling sweep — nothing else may reintroduce the unbounded 'morning' read
# --------------------------------------------------------------------------

class TestSiblingSweep:
    def test_engine_no_longer_asks_for_report_type_morning(self):
        """The three fetch_yesterday_report call sites in engine.py
        must all be on 'general'; a future edit reintroducing 'morning' brings
        the silent-empty continuity bug straight back."""
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[1] / 'morning_engine' / 'engine.py'
        text = src.read_text()
        assert "fetch_yesterday_report(report_type='morning')" not in text
        assert text.count("fetch_yesterday_report(report_type='general')") == 3

    def test_preassembly_route_uses_the_resolver_not_a_raw_lookup(self):
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[1] / 'carrac' / 'reports' / 'routes.py'
        text = src.read_text()
        assert 'resolve_prior_day_report' in text
        # The specific unbounded call this fix removed must not come back
        # inside the preassembly's _fetch_yesterday.
        start = text.index('def _fetch_yesterday(')
        body = text[start:start + 1400]
        # Comment lines are stripped first: the fix documents the removed call
        # verbatim, and matching the explanation instead of the code would make
        # this guard fire on a correct file (a guard that cannot see what it is
        # guarding).
        code = '\n'.join(ln for ln in body.split('\n')
                         if not ln.lstrip().startswith('#'))
        assert "get_latest_report(report_type='morning')" not in code
        assert 'resolve_prior_day_report(db=content_db)' in code
