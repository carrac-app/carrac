"""Route-level coverage for source_briefing.

Covers:
  1. Each save_* route (morning/general/weekly/afternoon/week_ahead) forwards
     an incoming `source_briefing` field through to the DB row.
  2. An oversized source_briefing is dropped (not persisted) WITHOUT failing
     the save — the report content is what matters; the debug briefing is a
     nice-to-have that must never block a save.
  3. Read-access decision: no route returns `source_briefing` in its JSON —
     it's queryable by psql only. Pins the routes that already
     cherry-pick fields off the report row.
"""

from __future__ import annotations

import pytest


def _admin_session(client):
    with client.session_transaction() as sess:
        sess['user_id'] = 1
        sess['email'] = 'admin@example.com'
        sess['is_admin'] = True
        sess['tier'] = 'admin'
    return client


_ROUTES = [
    ('/api/report/morning', 'morning'),
    ('/api/report/general', 'general'),
    ('/api/report/weekly', 'weekly'),
    ('/api/report/afternoon', 'afternoon'),
    ('/api/report/week_ahead', 'week_ahead'),
]


@pytest.mark.usefixtures("notifications")
class TestSaveRoutesForwardSourceBriefing:
    """Saving a report pings the operator via Telegram
    (carrac/reports/routes.py), so posting to these routes is a real send
    path. `notifications` captures it rather than letting it reach a phone."""

    @pytest.mark.parametrize('route,report_type', _ROUTES)
    def test_source_briefing_persisted_via_route(self, client, app, route, report_type):
        _admin_session(client)
        briefing = f"## Facts\nBTC 69109\n\n## Report type: {report_type}\n"
        resp = client.post(route, json={
            'content': f'## The Story\n{report_type} content.\n',
            'assets': ['BTC'],
            'generation_time_ms': 5,
            'source_briefing': briefing,
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        report_id = resp.get_json()['report_id']

        from carrac.content_db import get_content_db
        row = get_content_db().get_report_by_id(report_id)
        assert row['source_briefing'] == briefing

    @pytest.mark.parametrize('route,report_type', _ROUTES)
    def test_missing_source_briefing_still_saves(self, client, app, route, report_type):
        """No source_briefing in the payload at all must still save fine —
        nullable, backward compatible with any caller that doesn't send it."""
        _admin_session(client)
        resp = client.post(route, json={
            'content': f'## The Story\n{report_type} no briefing.\n',
            'assets': [],
            'generation_time_ms': 1,
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        report_id = resp.get_json()['report_id']

        from carrac.content_db import get_content_db
        row = get_content_db().get_report_by_id(report_id)
        assert row.get('source_briefing') is None


class TestOversizedSourceBriefingDropped:
    def test_oversized_briefing_dropped_save_still_succeeds(self, client, app):
        _admin_session(client)
        huge = 'x' * 400_000  # over the 300KB cap
        resp = client.post('/api/report/general', json={
            'content': '## The Story\nContent must survive.\n',
            'assets': [],
            'generation_time_ms': 1,
            'source_briefing': huge,
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        report_id = resp.get_json()['report_id']

        from carrac.content_db import get_content_db
        row = get_content_db().get_report_by_id(report_id)
        assert row['content'] == '## The Story\nContent must survive.\n'
        assert row.get('source_briefing') is None


class TestSourceBriefingNotExposedOnReadRoutes:
    """Read-access decision: source_briefing is queryable by
    psql only — no route surfaces it, admin or otherwise."""

    def _seed_afternoon(self):
        from carrac.content_db import get_content_db
        return get_content_db().save_report(
            content='Afternoon content.', assets_included=[],
            generation_time_ms=1, report_type='afternoon',
            source_briefing='SECRET_BRIEFING_MARKER',
        )

    def _seed_morning_recent(self):
        from carrac.content_db import get_content_db
        return get_content_db().save_report(
            content='Morning content.', assets_included=[],
            generation_time_ms=1, report_type='morning',
            source_briefing='SECRET_BRIEFING_MARKER',
        )

    def test_afternoon_latest_omits_source_briefing(self, client, app):
        self._seed_afternoon()
        _admin_session(client)
        resp = client.get('/api/report/afternoon/latest')
        assert resp.status_code == 200
        body = resp.get_json() or {}
        assert 'source_briefing' not in body
        assert 'SECRET_BRIEFING_MARKER' not in (body.get('content') or '')

    def test_morning_recent_omits_source_briefing(self, client, app):
        self._seed_morning_recent()
        _admin_session(client)
        resp = client.get('/api/report/morning/recent')
        assert resp.status_code == 200
        body = resp.get_json() or []
        assert body, 'expected at least one report'
        for r in body:
            assert 'source_briefing' not in r
