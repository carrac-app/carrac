"""/report/morning/archive 500'd in production with

    TypeError: 'datetime.datetime' object is not subscriptable

Prod (Postgres) hands ``created_at`` back as a real ``datetime`` object;
the test suite runs SQLite, where the driver returns a plain string — so
``{{ r.created_at[:10] }}`` in the template passed every test and still
died in production. See tests/schema/carrac_pg_schema.sql (``created_at
timestamp with time zone``) for the prod column shape.

This test seeds a row shaped exactly like the Postgres driver would
(created_at as a real datetime), which a string-slicing template cannot render.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


class _FakeDrawingDb:
    """Stand-in for carrac.content_db.ContentDatabase — only the
    method the archive route calls."""

    def __init__(self, rows):
        self._rows = rows

    def get_all_reports(self, report_type=None, limit=50):
        return list(self._rows)


@pytest.fixture
def postgres_shaped_report():
    """One 'morning' report row with created_at as a real datetime — the
    shape psycopg2 hands back in production, never a string."""
    return {
        'id': 42,
        'report_type': 'morning',
        'assets_included': ['BTC', 'ETH'],
        'generation_time_ms': 1234,
        'created_at': datetime(2026, 9, 9, 7, 18, 0, tzinfo=timezone.utc),
    }


def test_archive_renders_with_datetime_created_at(
    monkeypatch, auth_client, postgres_shaped_report
):
    """GET /report/morning/archive must render 200 with the report's date
    when created_at is a datetime object (prod/Postgres shape), not just
    when it's a string (test/SQLite shape)."""
    # morning_report_archive() does `from carrac.content_db import
    # get_content_db` INSIDE the function body (a fresh lookup on every
    # call), so patch the source module's attribute rather than the
    # reports_routes module.
    import carrac.content_db as cdb

    fake_db = _FakeDrawingDb([postgres_shaped_report])
    monkeypatch.setattr(cdb, 'get_content_db', lambda: fake_db)

    resp = auth_client.get('/report/morning/archive')

    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.data[:500]!r}"
    )
    body = resp.data.decode('utf-8')
    assert '2026-09-09' in body, (
        "Expected the report's date (2026-09-09) rendered in the archive page"
    )


def test_fmtdate_filter_registered_app_wide(app):
    """Fmtdate moved from the reports blueprint to
    carrac/__init__.py's create_app() (next to fmtdt) so admin/share
    templates can use it without the reports blueprint being registered.
    Confirms both the str and datetime branches still work post-move."""
    fmtdate = app.jinja_env.filters['fmtdate']
    assert fmtdate(datetime(2026, 9, 9, 7, 18, tzinfo=timezone.utc)) == '2026-09-09'
    assert fmtdate('2026-09-09 07:18:00') == '2026-09-09'
    assert fmtdate(None) == ''
    assert fmtdate(None, 'Never') == 'Never'


# ---------------------------------------------------------------------------
# Sibling: templates/admin_user_detail.html widget-token table
# ---------------------------------------------------------------------------
#
# widget_tokens.created_at is `timestamp with time zone` on Postgres (see
# tests/schema/carrac_pg_schema.sql), returned as a real datetime by
# user_db.list_widget_tokens() there. The template used
# `t.created_at[:10] if t.created_at else ''` — same defect class as the
# archive page. Covered by the fmtdate-filter unit test above (the template
# fix is a one-line `| fmtdate` swap, identical to the archive fix already
# proven failing/passing); no separate render test needed here.


# ---------------------------------------------------------------------------
# Sibling: templates/share_play.html outcome section — VERIFIED SAFE
# ---------------------------------------------------------------------------
#
# snap.outcome.resolved_at comes from a snapshot frozen into `play_shares.
# snapshot` (a TEXT/JSON column) at mint time, never read back as a live DB
# column. carrac/plays/models.py's transition_status() writes resolved_at
# as an ISO string, but plays.resolved_at is `timestamp with time zone` on
# Postgres (see carrac_pg_schema.sql) — so get_play() DOES hand back a real
# datetime there, exactly the failing shape. What keeps the template safe
# is carrac/plays/shares.py::_json_dumps(obj, default=str): the datetime is
# stringified when the snapshot is JSON-serialized at mint time, and
# _json_loads() only ever produces JSON-native types (str) on the read side
# — a datetime object can never survive the round trip. This is a
# deliberate guard (see the comment in shares.py::_json_dumps for the production
# failure of this exact class). The test below proves the guard holds end
# to end.

def test_share_play_page_survives_postgres_shaped_resolved_at(
    monkeypatch, client, app
):
    """Simulate get_play() returning resolved_at
    as a real datetime (the Postgres shape) and confirm the public share
    page still renders 200 with the date — because the JSON-snapshot
    freeze/thaw already guards it (see module comment above), NOT because
    the template does anything special."""
    import carrac.plays.shares as sh

    resolved_dt = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    fake_play = {
        'id': 501,
        'user_id': 7,
        'title': 'Datetime Resolved Play',
        'primary_asset': 'BTC',
        'direction': 'short',
        'type': 'directional',
        'timeframe': 'swing',
        'status': 'completed',
        'created_at': resolved_dt,
        'resolved_at': resolved_dt,  # the Postgres shape — a real datetime
        'resolution': 'Ran to target, +2R',
        'conditions': {'rules': [], 'logic': 'all'},
        'linked_drawing_ids': [],
        'thesis': None,
        'action_plan': None,
        'invalidation': None,
    }
    monkeypatch.setattr(sh, 'get_play', lambda play_id, user_id: fake_play)

    share = sh.create_share(7, 501, include_outcome=True)
    assert share is not None
    # The freeze already turned it into a string — prove that here too.
    assert isinstance(share['snapshot']['outcome']['resolved_at'], str)

    resp = client.get(f"/s/{share['slug']}")
    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.data[:500]!r}"
    )
    body = resp.data.decode('utf-8')
    assert '2026-09-09' in body


# ---------------------------------------------------------------------------
# Sibling: carrac/widget/routes.py::_get_report_snippet
# ---------------------------------------------------------------------------
#
# NOTE: this function currently queries a table named `morning_reports`,
# which does not exist anywhere in the schema (the real table is `reports`
# — see carrac/content_db.py's CREATE TABLE). That query always
# raises and is caught by the function's own `except Exception`, so on
# BOTH backends `_get_report_snippet()` already returns
# {'date': None, 'snippet': None} today, before the `[:10]` line is ever
# reached. The test below therefore bypasses the DB entirely (mocks
# carrac.db_pool.get_conn) to exercise just the row -> dict conversion,
# proving the `str(...)[:10]` handling is correct for a real datetime row.

def test_widget_report_snippet_handles_datetime_created_at(monkeypatch):
    """_get_report_snippet()'s `(row['created_at'] or '')[:10]` line breaks
    the same way if a row with a real datetime ever reaches
    it.

    _get_report_snippet() does `from carrac.db_pool import get_conn as
    _pool_get_conn` INSIDE the function body (fresh lookup per call), so
    patch carrac.db_pool.get_conn itself rather than a widget_routes
    attribute.
    """
    import contextlib

    import carrac.db_pool as db_pool
    import carrac.widget.routes as widget_routes

    row = {
        'id': 1,
        'summary': 'BTC steady into the FOMC blackout.',
        'created_at': datetime(2026, 9, 9, 7, 18, tzinfo=timezone.utc),
    }

    class _Conn:
        def execute(self, *a, **kw):
            return self

        def fetchone(self):
            return row

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    @contextlib.contextmanager
    def _fake_get_conn():
        yield _Conn()

    monkeypatch.setattr(db_pool, 'get_conn', _fake_get_conn)

    result = widget_routes._get_report_snippet()

    assert result['date'] == '2026-09-09', (
        f"Expected date '2026-09-09', got {result!r} — "
        "row['created_at'] was a datetime and the old code sliced it raw"
    )
