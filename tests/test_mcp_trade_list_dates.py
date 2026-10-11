"""trade_list's date window must work on Postgres rows.

On Postgres ``opened_at`` comes back as a ``datetime``; on SQLite (the test
DB) it is a string.  The ``to_date`` filter compared the row value to an ISO
string, which raises ``TypeError`` on Postgres — an automated end-of-day
``trade_list(from_date=..., to_date=...)`` call crashes in production.  The DB collaborator is stubbed with both row shapes;
``trade_list`` itself is never monkeypatched.
"""

from datetime import datetime

import pytest

from carrac.mcp import tools  # noqa: F401 — ensures registration
from carrac.mcp.tools.journal import trade_list


@pytest.fixture(autouse=True)
def _bind_user(bind_mcp_user):
    return bind_mcp_user


def _stub_db(monkeypatch, closed_rows, open_rows=()):
    import carrac.content_db as cdb

    class _Db:
        def get_trade_history(self, user_id, days=30, limit=50, asset=None):
            return [dict(r) for r in closed_rows]

        def get_open_trades(self, user_id, asset=None):
            return [dict(r) for r in open_rows]

    monkeypatch.setattr(cdb, 'get_content_db', lambda: _Db())


def _row(tid, opened_at, closed_at=None):
    return {'id': tid, 'asset': 'BTC', 'direction': 'short',
            'opened_at': opened_at, 'closed_at': closed_at}


class TestPostgresDatetimeRows:
    def test_to_date_does_not_raise_on_datetime_rows(self, app, monkeypatch):
        _stub_db(monkeypatch, [
            _row(1, datetime(2026, 8, 29, 10, 0), datetime(2026, 8, 29, 18, 0)),
            _row(2, datetime(2026, 8, 30, 9, 0), datetime(2026, 8, 30, 12, 0)),
        ])
        with app.app_context():
            out = trade_list({'from_date': '2026-08-29', 'to_date': '2026-08-29',
                              'status': 'all'})
        assert [t['id'] for t in out['trades']] == [1]

    def test_open_trades_before_from_date_are_excluded(self, app, monkeypatch):
        _stub_db(monkeypatch, [], open_rows=[
            _row(3, datetime(2026, 8, 1, 10, 0)),
            _row(4, datetime(2026, 8, 29, 10, 0)),
        ])
        with app.app_context():
            out = trade_list({'from_date': '2026-08-29', 'to_date': '2026-08-29',
                              'status': 'open'})
        assert [t['id'] for t in out['trades']] == [4]


class TestSqliteStringRows:
    def test_date_only_to_date_includes_the_whole_day(self, app, monkeypatch):
        _stub_db(monkeypatch, [
            _row(1, '2026-08-29 10:00:00', '2026-08-29 18:00:00'),
            _row(2, '2026-08-30 09:00:00', '2026-08-30 12:00:00'),
        ])
        with app.app_context():
            out = trade_list({'to_date': '2026-08-29', 'status': 'closed'})
        assert [t['id'] for t in out['trades']] == [1]

    def test_explicit_time_bound_is_honoured(self, app, monkeypatch):
        _stub_db(monkeypatch, [
            _row(1, '2026-08-29T10:00:00', '2026-08-29T18:00:00'),
            _row(2, '2026-08-29T15:00:00', '2026-08-29T18:00:00'),
        ])
        with app.app_context():
            out = trade_list({'to_date': '2026-08-29T12:00:00', 'status': 'closed'})
        assert [t['id'] for t in out['trades']] == [1]
