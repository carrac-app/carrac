"""
Tests that save_report must be idempotent per (report_type, UTC day).
A second call for the same type and day must update, not insert a duplicate row.
"""

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


class TestMorningReportIdempotency:
    """One row per (report_type, day); second save updates, not inserts."""

    def test_second_save_same_day_updates_not_duplicates(self, content_db):
        """Two save_report calls for the same type on the same day → exactly 1 row."""
        db = content_db

        db.save_report(
            content='First version of morning report',
            assets_included=['BTC', 'ETH'],
            generation_time_ms=1000,
            report_type='morning',
        )
        db.save_report(
            content='Updated morning report (cron retry)',
            assets_included=['BTC', 'ETH', 'SOL'],
            generation_time_ms=1200,
            report_type='morning',
        )

        # get_all_reports omits content; use get_latest_report to verify content
        reports = db.get_all_reports(report_type='morning')
        assert len(reports) == 1, (
            f"Expected exactly 1 morning report row, found {len(reports)}"
        )

        # The /latest should return the updated content
        latest = db.get_latest_report(report_type='morning')
        assert latest is not None
        assert 'Updated' in latest['content'], (
            "Expected second save to update content of existing row"
        )

    def test_different_types_same_day_are_separate_rows(self, content_db):
        """morning + weekly on the same day must be independent rows."""
        db = content_db

        db.save_report(
            content='Morning report',
            assets_included=['BTC'],
            generation_time_ms=500,
            report_type='morning',
        )
        db.save_report(
            content='Weekly digest',
            assets_included=['BTC', 'ETH'],
            generation_time_ms=800,
            report_type='weekly',
        )

        morning = db.get_all_reports(report_type='morning')
        weekly = db.get_all_reports(report_type='weekly')
        assert len(morning) == 1
        assert len(weekly) == 1
