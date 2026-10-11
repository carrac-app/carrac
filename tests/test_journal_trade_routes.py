"""Journal trade routes with no first-party caller: reopen and screenshots.

They are API-only (documented in the OpenAPI document), so these tests are
what keeps them working.
"""


import pytest

from carrac.content_db import get_content_db
from carrac.db_pool import adapt_query, get_conn

UID = 70912
OTHER_UID = 70913


@pytest.fixture
def journal_client(client):
    with client.session_transaction() as sess:
        sess['user_id'] = UID
        sess['email'] = 'journal-routes@example.com'
        sess['tier'] = 'member'
    return client


@pytest.fixture
def closed_trade():
    db = get_content_db()
    tid = db.log_trade_open(
        user_id=UID, asset='BTC', timeframe='1D', direction='long',
        entry_price=100.0, stop_price=90.0, target_price=130.0,
    )
    db.log_trade_close(tid, exit_price=110.0)
    return tid


def test_reopen_returns_a_closed_trade_to_open(journal_client, closed_trade):
    db = get_content_db()
    assert db.get_journal_entry(closed_trade)['closed_at']

    resp = journal_client.post(f'/api/journal/trades/{closed_trade}/reopen')

    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True}
    assert not db.get_journal_entry(closed_trade)['closed_at']


def test_reopen_refuses_another_users_trade(journal_client):
    tid = get_content_db().log_trade_open(
        user_id=OTHER_UID, asset='ETH', timeframe='1D', direction='long', entry_price=1.0)
    assert journal_client.post(f'/api/journal/trades/{tid}/reopen').status_code == 404


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    return tmp_path


@pytest.fixture
def pool_screenshots_table():
    """The pool connection the screenshot routes write through is a separate
    SQLite file from the content database in tests, so give it the table."""
    with get_conn() as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS journal_screenshots ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, journal_id INTEGER NOT NULL, '
            'user_id INTEGER NOT NULL, screenshot_type VARCHAR(10) NOT NULL, '
            'file_path TEXT NOT NULL, captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, '
            'ai_analysis TEXT)')
        conn.execute('DELETE FROM journal_screenshots')


def _insert_pool_screenshot(user_id, rel_path):
    with get_conn() as conn:
        cur = conn.execute(
            adapt_query('INSERT INTO journal_screenshots '
                        '(journal_id, user_id, screenshot_type, file_path) VALUES (?, ?, ?, ?)'),
            (1, user_id, 'entry', rel_path))
        return cur.lastrowid


def test_screenshot_attach_hands_the_trade_to_the_renderer(journal_client, closed_trade, monkeypatch):
    seen = {}

    def capture(db, journal_id, user_id, asset, timeframe, screenshot_type='context'):
        seen.update(journal_id=journal_id, user_id=user_id, asset=asset, type=screenshot_type)
        return {'screenshot_id': 7, 'file_path': 'journal-screenshots/x.jpg', 'type': screenshot_type}

    monkeypatch.setattr('carrac.journal.screenshots.capture_journal_screenshot', capture)

    resp = journal_client.post(f'/api/journal/trades/{closed_trade}/screenshot', json={'type': 'entry'})

    assert resp.status_code == 201
    assert resp.get_json()['screenshot_id'] == 7
    assert seen == {'journal_id': closed_trade, 'user_id': UID, 'asset': 'BTC', 'type': 'entry'}


def test_screenshot_attach_rejects_an_unknown_type(journal_client, closed_trade, monkeypatch):
    def capture(*args, **kwargs):
        raise AssertionError('must not render for an invalid type')

    monkeypatch.setattr('carrac.journal.screenshots.capture_journal_screenshot', capture)
    resp = journal_client.post(f'/api/journal/trades/{closed_trade}/screenshot', json={'type': 'selfie'})
    assert resp.status_code == 400


def test_screenshot_list_returns_the_trades_rows(journal_client, closed_trade):
    conn = get_content_db()._get_conn()
    conn.execute(
        adapt_query('INSERT INTO journal_screenshots '
                    '(journal_id, user_id, screenshot_type, file_path) VALUES (?, ?, ?, ?)'),
        (closed_trade, UID, 'exit', 'journal-screenshots/a.jpg'))
    conn.commit()

    listed = journal_client.get(f'/api/journal/trades/{closed_trade}/screenshots').get_json()

    assert [(s['screenshot_type'], s['file_path']) for s in listed] == [('exit', 'journal-screenshots/a.jpg')]


def test_screenshot_delete_removes_the_row_and_the_file(
        journal_client, data_dir, pool_screenshots_table):
    rel = 'journal-screenshots/70912/1/entry_1.jpg'
    stored = data_dir / rel
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b'jpg')
    sid = _insert_pool_screenshot(UID, rel)

    assert journal_client.delete(f'/api/journal/screenshots/{sid}').status_code == 200

    assert not stored.exists()
    with get_conn() as conn:
        assert conn.execute(
            adapt_query('SELECT 1 FROM journal_screenshots WHERE id = ?'), (sid,)).fetchone() is None


def test_screenshot_delete_refuses_another_users_screenshot(
        journal_client, data_dir, pool_screenshots_table):
    sid = _insert_pool_screenshot(OTHER_UID, 'journal-screenshots/x.jpg')
    assert journal_client.delete(f'/api/journal/screenshots/{sid}').status_code == 404
    with get_conn() as conn:
        assert conn.execute(
            adapt_query('SELECT 1 FROM journal_screenshots WHERE id = ?'), (sid,)).fetchone() is not None
