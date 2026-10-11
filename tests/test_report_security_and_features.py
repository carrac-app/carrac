"""Tests for report security fixes and new features.

Covers:
  1. By-ID archive views sanitize operator content for non-admin.
  2. get_report_by_date DB layer + report_get_diff MCP tool happy path.
  3. Staleness banners: old weekly/week_ahead/morning reports show age context;
     fresh reports do not.

x_candidates coverage on the general/non-admin path does not belong here:
`/api/report/morning/latest` is FastAPI-migrated (strangler MIGRATED_PATHS),
so a Flask-client suite would exercise a dead, shadowed handler. Live coverage is
tests/fastapi/test_reports_x_candidates.py (drives the real handler via
asgi_client).
"""

from __future__ import annotations

import json

import pytest

from carrac.content_db import ContentDatabase


# ── shared fixtures ────────────────────────────────────────────────────────

_MORNING_WITH_POSITIONS = (
    "## The Story\nMacro narrative; nothing operator-private here.\n\n"
    "## Action Dashboard\n"
    "\U0001f534 **BTC Position Risk** — Short entered at $75,736 with stop $79,872.\n\n"
    "## Market Snapshot\n| Asset | Price |\n| BTC | 71287 |\n"
)

_WEEKLY_WITH_PLAYS = (
    "## 1. Week in Review\nBTC opened the week at $73,413.\n\n"
    "## 4. What Worked / What Didn't\n"
    "- The OIL $95-break play stopped out.\n\n"
    "## 5. Play Review\n"
    "- Play #18 OIL Supply-Shock: thesis intact, re-armed.\n\n"
    "## 7. Next Week Setup\nWatch the 59.5k shelf.\n"
)


# ── 1. By-ID archive view sanitization ────────────────────────────────────

class TestByIdViewSanitization:
    """Non-admin fetching /report/morning/<id> or /report/weekly/<id> must
    not see position fingerprints ("entry $", "stop $") or operator sections."""

    def _seed_morning(self, app):
        from carrac.content_db import get_content_db
        return get_content_db().save_report(
            content=_MORNING_WITH_POSITIONS, assets_included=[],
            generation_time_ms=0, report_type='morning')

    def _seed_weekly(self, app):
        from carrac.content_db import get_content_db
        return get_content_db().save_report(
            content=_WEEKLY_WITH_PLAYS, assets_included=[],
            generation_time_ms=0, report_type='weekly')

    def test_non_admin_morning_by_id_no_position_fingerprints(self, client, app):
        rid = self._seed_morning(app)
        with client.session_transaction() as sess:
            sess['user_id'] = 99
            sess['email'] = 'member@example.com'
            sess['tier'] = 'member'
        resp = client.get(f'/report/morning/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'entry $' not in body
        assert 'stop $' not in body
        assert '75,736' not in body
        assert '79,872' not in body
        assert 'Action Dashboard' not in body

    def test_non_admin_morning_by_id_keeps_neutral_sections(self, client, app):
        rid = self._seed_morning(app)
        with client.session_transaction() as sess:
            sess['user_id'] = 99
            sess['email'] = 'member@example.com'
            sess['tier'] = 'member'
        resp = client.get(f'/report/morning/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # Market Snapshot and The Story survive the sanitizer.
        assert 'Market Snapshot' in body
        assert '71287' in body

    def test_admin_morning_by_id_sees_full_content(self, client, app):
        rid = self._seed_morning(app)
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'admin@example.com'
            sess['tier'] = 'admin'
        resp = client.get(f'/report/morning/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'Action Dashboard' in body
        assert '75,736' in body

    def test_non_admin_weekly_by_id_no_play_review(self, client, app):
        rid = self._seed_weekly(app)
        with client.session_transaction() as sess:
            sess['user_id'] = 98
            sess['email'] = 'member2@example.com'
            sess['tier'] = 'member'
        resp = client.get(f'/report/weekly/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'Play Review' not in body
        assert "What Worked / What Didn't" not in body
        assert 'Play #18' not in body
        # neutral sections survive
        assert 'Week in Review' in body
        assert 'Next Week Setup' in body

    def test_non_admin_week_ahead_by_id_sanitized(self, client, app):
        from carrac.content_db import get_content_db
        rid = get_content_db().save_report(
            content=_MORNING_WITH_POSITIONS, assets_included=[],
            generation_time_ms=0, report_type='week_ahead')
        with client.session_transaction() as sess:
            sess['user_id'] = 97
            sess['email'] = 'member3@example.com'
            sess['tier'] = 'member'
        resp = client.get(f'/report/week_ahead/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert '75,736' not in body
        assert '79,872' not in body
        assert 'Action Dashboard' not in body

    def test_latest_view_redirects(self, client, app):
        from carrac.content_db import get_content_db
        wid = get_content_db().save_report(
            content=_WEEKLY_WITH_PLAYS, assets_included=[],
            generation_time_ms=0, report_type='weekly')
        aid = get_content_db().save_report(
            content=_MORNING_WITH_POSITIONS, assets_included=[],
            generation_time_ms=0, report_type='week_ahead')
        with client.session_transaction() as sess:
            sess['user_id'] = 96
            sess['email'] = 'member4@example.com'
            sess['tier'] = 'member'
        resp = client.get('/report/weekly/latest')
        assert resp.status_code == 302
        assert resp.headers['Location'].endswith(f'/report/weekly/{wid}')
        resp = client.get('/report/week_ahead/latest')
        assert resp.status_code == 302
        assert resp.headers['Location'].endswith(f'/report/week_ahead/{aid}')

    def test_save_general_persists_metadata(self, client, app, monkeypatch):
        import carrac.reports.routes as rmod
        # save_general_report now gates on verify_admin_key (shared admin key
        # only — a per-user key must not pass); patch that, not verify_api_key.
        monkeypatch.setattr(rmod, 'verify_admin_key', lambda k: True)
        resp = client.post(
            '/api/report/general',
            json={'content': '## TL;DR\nhello world general report',
                  'metadata': {'x_candidates': ['BTC RSI at 24, lowest of the cycle.']}},
            headers={'X-Internal-Key': 'test'})
        assert resp.status_code == 200
        rid = resp.get_json()['report_id']
        from carrac.content_db import get_content_db
        row = get_content_db().get_report_by_id(rid)
        meta = json.loads(row['metadata'])
        assert meta['x_candidates'] == ['BTC RSI at 24, lowest of the cycle.']

    def test_admin_weekly_by_id_sees_full_content(self, client, app):
        rid = self._seed_weekly(app)
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['email'] = 'admin@example.com'
            sess['tier'] = 'admin'
        resp = client.get(f'/report/weekly/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'Play Review' in body
        assert 'Play #18' in body


# ── 2. get_report_by_date + report_get_diff happy path ────────────────────────

class TestGetReportByDate:
    """DB layer: get_report_by_date returns the right row for the date."""

    def _make_db(self, tmp_path):
        return ContentDatabase(str(tmp_path / 'test.db'))

    def test_returns_row_for_correct_date(self, tmp_path):
        db = self._make_db(tmp_path)
        rid = db.save_report(
            content='## The Story\nABC\n',
            assets_included=['BTC'],
            generation_time_ms=100,
            report_type='morning',
        )
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        row = db.get_report_by_date(report_type='morning', date=today)
        assert row is not None
        assert row['id'] == rid
        assert 'ABC' in row['content']

    def test_returns_none_for_wrong_date(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save_report(
            content='## The Story\nABC\n',
            assets_included=['BTC'],
            generation_time_ms=100,
            report_type='morning',
        )
        row = db.get_report_by_date(report_type='morning', date='1999-01-01')
        assert row is None

    def test_returns_none_for_wrong_type(self, tmp_path):
        db = self._make_db(tmp_path)
        db.save_report(
            content='## The Story\nABC\n',
            assets_included=[],
            generation_time_ms=0,
            report_type='morning',
        )
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        row = db.get_report_by_date(report_type='weekly', date=today)
        assert row is None


class TestReportDiffMcpTool:
    """report_get_diff MCP tool resolves dates and returns structural diff."""

    def _seed_two_reports(self, tmp_path):
        from datetime import datetime, timezone

        db = ContentDatabase(str(tmp_path / 'test.db'))
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        content_a = "## The Story\nOld macro.\n\n## Market Snapshot\n| BTC | 60000 |\n"
        content_b = (
            "## The Story\nNew macro.\n\n"
            "## Market Snapshot\n| BTC | 65000 |\n\n"
            "## Extra Section\nBrand new.\n"
        )
        # save_report is idempotent per type+day, so use two different types
        # to simulate two distinct rows on the same day.  For a same-type diff
        # we patch the DB.
        return db, content_a, content_b, today

    def test_report_diff_happy_path(self, monkeypatch, tmp_path):
        from carrac.mcp import tools  # noqa: F401 — ensure registration
        from carrac.mcp.registry import TOOL_REGISTRY

        db, content_a, content_b, today = self._seed_two_reports(tmp_path)

        class _FakeDB:
            def get_report_by_date(self, report_type, date):
                if date == '2026-01-01':
                    return {'content': content_a, 'html': None, 'created_at': '2026-01-01', 'id': 1}
                if date == '2026-01-02':
                    return {'content': content_b, 'html': None, 'created_at': '2026-01-02', 'id': 2}
                return None

        from carrac import content_db as cdb
        monkeypatch.setattr(cdb, 'get_content_db', lambda: _FakeDB())

        import carrac.mcp.runtime as rt
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'admin')

        fn = TOOL_REGISTRY['report_get_diff']['handler']
        result = fn({'date_a': '2026-01-01', 'date_b': '2026-01-02', 'type': 'morning'})

        assert result['date_a'] == '2026-01-01'
        assert result['date_b'] == '2026-01-02'
        # "Extra Section" only appears in date_b → sections_added
        assert 'Extra Section' in result['sections_added']
        # Common sections like "The Story" and "Market Snapshot" exist in both
        assert isinstance(result['sections_changed'] + result['sections_unchanged'], list)
        assert result['total_chars_a'] > 0
        assert result['total_chars_b'] > 0

    def test_report_diff_missing_date_raises(self, monkeypatch):
        from carrac.mcp import tools  # noqa: F401
        from carrac.mcp.registry import TOOL_REGISTRY
        from carrac.mcp.protocol import ToolError

        class _EmptyDB:
            def get_report_by_date(self, report_type, date):
                return None

        from carrac import content_db as cdb
        monkeypatch.setattr(cdb, 'get_content_db', lambda: _EmptyDB())

        import carrac.mcp.runtime as rt
        monkeypatch.setattr(rt, 'get_mcp_caller_tier', lambda: 'admin')

        fn = TOOL_REGISTRY['report_get_diff']['handler']
        with pytest.raises(ToolError):
            fn({'date_a': '2000-01-01', 'date_b': '2000-01-02', 'type': 'morning'})


# ── 4. Staleness banners ──────────────────────────────────────────────────────

def _backdate_report(db, report_id: int, days_ago: int) -> None:
    """UPDATE the created_at timestamp to ``days_ago`` days in the past (SQLite only)."""
    conn = db._get_conn()
    conn.execute(
        "UPDATE reports SET created_at = datetime('now', ?) WHERE id = ?",
        (f'-{days_ago} days', report_id),
    )
    conn.commit()


def _member_session(client, user_id: int, email: str):
    """Set a member-tier session on the client."""
    with client.session_transaction() as sess:
        sess['user_id'] = user_id
        sess['email'] = email
        sess['tier'] = 'member'


class TestStalenessBanner:
    """Staleness banners appear for stale reports and are absent for fresh ones."""

    # -- weekly --

    def test_old_weekly_shows_amber_banner(self, client, app):
        from carrac.content_db import get_content_db
        db = get_content_db()
        rid = db.save_report(content=_WEEKLY_WITH_PLAYS, assets_included=[],
                             generation_time_ms=0, report_type='weekly')
        _backdate_report(db, rid, days_ago=5)
        _member_session(client, 50, 'stale-weekly@example.com')
        resp = client.get(f'/report/weekly/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert '<div class="staleness-banner amber">' in body
        assert 'now underway or past' in body

    def test_fresh_weekly_no_banner(self, client, app):
        from carrac.content_db import get_content_db
        db = get_content_db()
        rid = db.save_report(content=_WEEKLY_WITH_PLAYS, assets_included=[],
                             generation_time_ms=0, report_type='weekly')
        # No backdate — created just now (age 0 days)
        _member_session(client, 51, 'fresh-weekly@example.com')
        resp = client.get(f'/report/weekly/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # The CSS class name appears in the <style> block; check no <div> is emitted
        assert '<div class="staleness-banner' not in body

    # -- week_ahead --

    def test_old_week_ahead_shows_amber_banner(self, client, app):
        from carrac.content_db import get_content_db
        db = get_content_db()
        rid = db.save_report(content='## Catalysts\nSome catalysts.', assets_included=[],
                             generation_time_ms=0, report_type='week_ahead')
        _backdate_report(db, rid, days_ago=4)
        _member_session(client, 52, 'stale-wa@example.com')
        resp = client.get(f'/report/week_ahead/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert '<div class="staleness-banner amber">' in body
        assert 'already occurred' in body

    def test_fresh_week_ahead_no_banner(self, client, app):
        from carrac.content_db import get_content_db
        db = get_content_db()
        rid = db.save_report(content='## Catalysts\nSome catalysts.', assets_included=[],
                             generation_time_ms=0, report_type='week_ahead')
        _member_session(client, 53, 'fresh-wa@example.com')
        resp = client.get(f'/report/week_ahead/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert '<div class="staleness-banner' not in body

    # -- morning --

    def test_old_morning_shows_gray_banner(self, client, app):
        from carrac.content_db import get_content_db
        db = get_content_db()
        rid = db.save_report(content=_MORNING_WITH_POSITIONS, assets_included=[],
                             generation_time_ms=0, report_type='morning')
        _backdate_report(db, rid, days_ago=3)
        _member_session(client, 54, 'stale-morning@example.com')
        resp = client.get(f'/report/morning/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert '<div class="staleness-banner gray">' in body
        assert 'Archived report from' in body

    def test_fresh_morning_no_banner(self, client, app):
        from carrac.content_db import get_content_db
        db = get_content_db()
        rid = db.save_report(content=_MORNING_WITH_POSITIONS, assets_included=[],
                             generation_time_ms=0, report_type='morning')
        _member_session(client, 55, 'fresh-morning@example.com')
        resp = client.get(f'/report/morning/{rid}')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert '<div class="staleness-banner' not in body
