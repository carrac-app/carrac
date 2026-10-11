"""Persist the briefing that produced each shared report.

Covers:
  1. save_report() round-trips source_briefing (Postgres upsert path and
     SQLite INSERT-OR-REPLACE path share the same INSERT column list, so a
     SQLite round-trip exercises the same wiring).
  2. The column is nullable and existing rows (or any
     save that omits the kwarg) are unaffected — content/metadata/etc. are
     untouched, source_briefing is simply NULL.
  3. prune_old_source_briefings() clears only rows older than the retention
     window, leaving content and everything else intact.
  4. The personalized-briefing decision (skip) is enforced structurally:
     save_personal_section() has no source_briefing parameter at all.
"""

from __future__ import annotations

import inspect

import pytest


@pytest.fixture
def content_db(tmp_path, monkeypatch):
    """Return a ContentDatabase backed by an isolated SQLite file."""
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    import carrac.db_pool as _dp
    _dp._db_type = None
    if hasattr(_dp._sqlite_local, 'conn') and _dp._sqlite_local.conn is not None:
        try:
            _dp._sqlite_local.conn.close()
        except Exception:
            pass
        _dp._sqlite_local.conn = None

    from carrac.content_db import ContentDatabase
    db_path = str(tmp_path / 'drawings.db')
    db = ContentDatabase(db_path=db_path)
    yield db

    if hasattr(_dp._sqlite_local, 'conn') and _dp._sqlite_local.conn is not None:
        try:
            _dp._sqlite_local.conn.close()
        except Exception:
            pass
        _dp._sqlite_local.conn = None


class TestSaveReportRoundtripsSourceBriefing:
    def test_source_briefing_persisted_and_readable(self, content_db):
        db = content_db
        briefing = "## Facts\nBTC 69109\n\n## Calendar\nCPI 08:30\n"
        report_id = db.save_report(
            content='## The Story\nBTC steady.\n',
            assets_included=['BTC'],
            generation_time_ms=1000,
            report_type='morning',
            source_briefing=briefing,
        )
        fetched = db.get_report_by_id(report_id)
        assert fetched is not None
        assert fetched['source_briefing'] == briefing
        # content untouched by the new column
        assert fetched['content'] == '## The Story\nBTC steady.\n'

        latest = db.get_latest_report(report_type='morning')
        assert latest['source_briefing'] == briefing

    def test_idempotent_resave_updates_source_briefing(self, content_db):
        """Second same-day save (cron retry) replaces source_briefing along
        with content — same upsert semantics as every other column."""
        db = content_db
        db.save_report(
            content='v1', assets_included=[], generation_time_ms=1,
            report_type='afternoon', source_briefing='briefing v1',
        )
        db.save_report(
            content='v2', assets_included=[], generation_time_ms=1,
            report_type='afternoon', source_briefing='briefing v2',
        )
        reports = db.get_all_reports(report_type='afternoon')
        assert len(reports) == 1
        latest = db.get_latest_report(report_type='afternoon')
        assert latest['content'] == 'v2'
        assert latest['source_briefing'] == 'briefing v2'


class TestSourceBriefingNullable:
    def test_omitted_source_briefing_is_none(self, content_db):
        """A save that doesn't pass source_briefing (e.g. an old caller, or
        any future save site that never sends one) must succeed exactly as
        before — nullable, backward compatible."""
        db = content_db
        report_id = db.save_report(
            content='## The Story\nNo briefing captured.\n',
            assets_included=['ETH'],
            generation_time_ms=500,
            report_type='weekly',
        )
        fetched = db.get_report_by_id(report_id)
        assert fetched is not None
        assert fetched.get('source_briefing') is None
        assert fetched['content'] == '## The Story\nNo briefing captured.\n'

    def test_pre_existing_row_without_briefing_unaffected(self, content_db):
        """A row that predates this column (simulated: inserted with the
        column implicitly NULL) still reads back fine on every other field."""
        db = content_db
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO reports
                (report_type, content, assets_included, generation_time_ms, generation_date)
            VALUES (?, ?, ?, ?, ?)
        ''', ('week_ahead', 'legacy content', '["BTC"]', 42, '2026-01-01'))
        conn.commit()
        row_id = cursor.lastrowid

        fetched = db.get_report_by_id(row_id)
        assert fetched is not None
        assert fetched['content'] == 'legacy content'
        assert fetched.get('source_briefing') is None


class TestPruneOldSourceBriefings:
    def _insert_backdated(self, db, *, report_type, generation_date, content,
                          source_briefing):
        """Insert a row directly with an explicit (old) created_at/generation_date,
        bypassing save_report's idempotency-per-day upsert so an "old" and a
        "fresh" row of the same report_type can coexist in one test."""
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO reports
                (report_type, content, assets_included, generation_time_ms,
                 generation_date, source_briefing, created_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', '-120 days'))
        ''', (report_type, content, '[]', 0, generation_date, source_briefing))
        conn.commit()
        return cursor.lastrowid

    def test_prune_clears_only_rows_older_than_window(self, content_db):
        db = content_db
        old_id = self._insert_backdated(
            db, report_type='morning', generation_date='2026-01-01',
            content='old content', source_briefing='old briefing (120d ago)',
        )
        new_id = db.save_report(
            content='new content', assets_included=[], generation_time_ms=1,
            report_type='afternoon', source_briefing='fresh briefing (today)',
        )

        pruned = db.prune_old_source_briefings(retention_days=90)
        assert pruned == 1

        old_row = db.get_report_by_id(old_id)
        assert old_row['source_briefing'] is None
        assert old_row['content'] == 'old content'  # content NEVER touched

        new_row = db.get_report_by_id(new_id)
        assert new_row['source_briefing'] == 'fresh briefing (today)'
        assert new_row['content'] == 'new content'

    def test_prune_is_noop_when_nothing_old(self, content_db):
        db = content_db
        db.save_report(
            content='fresh', assets_included=[], generation_time_ms=1,
            report_type='general', source_briefing='fresh briefing',
        )
        pruned = db.prune_old_source_briefings(retention_days=90)
        assert pruned == 0

    def test_prune_never_raises_on_bad_input(self, content_db, monkeypatch):
        """Defensive: prune_old_source_briefings must never propagate an
        exception into the worker's daily housekeeping loop."""
        db = content_db

        def _boom(*a, **kw):
            raise RuntimeError('simulated DB failure')

        monkeypatch.setattr(db, '_get_conn', _boom)
        assert db.prune_old_source_briefings() == 0


class TestPersonalizedBriefingDeliberatelySkipped:
    """Judgement call: personalized per-user briefings (trader
    profile, positions, plays/journal text) are more sensitive than the
    shared market briefing and are deliberately NOT persisted. Enforced
    structurally — save_personal_section has no source_briefing param."""

    def test_save_personal_section_has_no_source_briefing_param(self, content_db):
        sig = inspect.signature(content_db.save_personal_section)
        assert 'source_briefing' not in sig.parameters, (
            "save_personal_section must never gain a source_briefing param — "
            "personalized briefings (positions/journal/trader profile) are "
            "deliberately not persisted; see the docstrings on "
            "save_personal_section, dashboard_api.save_personal_section, and "
            "the save_personal_section route."
        )

    def test_personal_section_round_trip_unaffected(self, content_db):
        """Sanity: personal sections still work exactly as before — this
        change adds nothing to that path."""
        db = content_db
        report_id = db.save_report(
            content='shared content', assets_included=[], generation_time_ms=1,
            report_type='morning', source_briefing='shared briefing',
        )
        db.save_personal_section(
            report_id=report_id, user_id=99, content='personal content',
        )
        section = db.get_personal_section(report_id, 99)
        assert section is not None
        assert section['content'] == 'personal content'
        # No source_briefing key ever appears on a personal section row.
        assert 'source_briefing' not in section
