"""check_subscription_expiry with the expiry column as Postgres returns it.

SQLite hands back `subscription_expires_at` as an ISO string, Postgres as a
`datetime`. The expiry check must downgrade an expired subscription either
way; the tests below give it Postgres-shaped rows on the SQLite suite.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from user_db import SUBSCRIPTION_GRACE_DAYS


class _PostgresShapedRows:
    """Wraps a connection so the expiry column comes back as a datetime."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return _PostgresShapedCursor(self._conn.execute(sql, params))


class _PostgresShapedCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        row = dict(row)
        value = row.get('subscription_expires_at')
        if isinstance(value, str):
            row['subscription_expires_at'] = datetime.fromisoformat(value)
        return row


@pytest.fixture
def db(tmp_path, rebind_db):
    return rebind_db(tmp_path / 'db')


def _premium_user_expiring_at(db, monkeypatch, expires_at):
    uid = db.create_user('expiry@example.com', 'StrongPass!123', tos_accepted=True)
    with db._tx() as conn:
        conn.execute(
            "UPDATE users SET tier = 'premium', subscription_expires_at = ? WHERE id = ?",
            (expires_at.isoformat(), uid),
        )
    real_tx = db._tx

    @contextmanager
    def postgres_shaped_tx(*args, **kwargs):
        with real_tx(*args, **kwargs) as conn:
            yield _PostgresShapedRows(conn)

    monkeypatch.setattr(db, '_tx', postgres_shaped_tx)
    return uid


def test_expired_beyond_grace_is_downgraded(db, monkeypatch):
    expired = datetime.now(timezone.utc) - timedelta(days=SUBSCRIPTION_GRACE_DAYS + 5)
    uid = _premium_user_expiring_at(db, monkeypatch, expired)

    status = db.check_subscription_expiry(uid)

    assert status['expired'] is True
    monkeypatch.undo()
    assert db.get_user_by_id(uid)['tier'] == 'free'


def test_active_subscription_reports_an_iso_expiry(db, monkeypatch):
    expires = datetime.now(timezone.utc) + timedelta(days=10)
    uid = _premium_user_expiring_at(db, monkeypatch, expires)

    status = db.check_subscription_expiry(uid)

    assert status['expired'] is False
    assert status['tier'] == 'premium'
    assert status['days_remaining'] >= 9
    assert status['expires_at'] == expires.isoformat()
