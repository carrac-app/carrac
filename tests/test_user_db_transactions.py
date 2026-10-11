"""Transaction handling in UserDatabase: the `_tx` helper and its callers.

Every method runs its statements through `_tx`, which commits a block that
finishes and rolls back one that raises. These tests pin that contract on
SQLite; tests/pg/test_user_db_transactions_pg.py pins the Postgres side,
where a failed statement aborts the whole transaction.
"""

import pytest


@pytest.fixture
def db(tmp_path, rebind_db):
    return rebind_db(tmp_path / 'db')


def _user(db, email='tx@example.com'):
    uid = db.create_user(email, 'StrongPass!123', tos_accepted=True)
    assert uid
    return uid


def _paddle_customer_id(db, uid):
    row = db._get_conn().execute(
        'SELECT paddle_customer_id FROM users WHERE id = ?', (uid,)).fetchone()
    return row['paddle_customer_id']


def test_tx_commits_a_block_that_finishes(db):
    uid = _user(db)
    with db._tx() as conn:
        conn.execute('UPDATE users SET display_name = ? WHERE id = ?', ('Kept', uid))
    assert not db._get_conn().in_transaction
    assert db.get_user_by_id(uid)['display_name'] == 'Kept'


def test_tx_rolls_back_and_reraises_a_block_that_raises(db):
    uid = _user(db)
    with pytest.raises(RuntimeError, match='boom'):
        with db._tx() as conn:
            conn.execute('UPDATE users SET display_name = ? WHERE id = ?', ('Lost', uid))
            raise RuntimeError('boom')
    assert not db._get_conn().in_transaction
    assert db.get_user_by_id(uid)['display_name'] != 'Lost'


def test_read_inside_paddle_unit_does_not_commit_partial_work(db):
    """A read made by work_fn joins the unit instead of committing it.

    get_user_by_id does not commit the shared connection, so a webhook that
    wrote, read and then failed leaves no partial write behind and Paddle's
    retry cannot apply it twice.
    """
    uid = _user(db)

    def work():
        db.set_paddle_customer_id(uid, 'ctm_partial', _commit=False)
        assert db.get_user_by_id(uid) is not None
        raise RuntimeError('handler failed after a read')

    with pytest.raises(RuntimeError):
        db.process_paddle_event_atomic('evt_partial', uid, work)

    assert _paddle_customer_id(db, uid) is None
    assert db._get_conn().execute(
        'SELECT 1 FROM processed_payments WHERE payment_id = ?', ('evt_partial',)
    ).fetchone() is None


def test_paddle_unit_commits_work_and_marker_together(db):
    uid = _user(db)

    def work():
        assert db.get_user_by_id(uid) is not None
        db.set_paddle_customer_id(uid, 'ctm_ok', _commit=False)

    assert db.process_paddle_event_atomic('evt_ok', uid, work) is True
    assert _paddle_customer_id(db, uid) == 'ctm_ok'
    assert db.process_paddle_event_atomic('evt_ok', uid, work) is False
    assert not db._get_conn().in_transaction


def test_log_ai_usage_stores_the_trace_hashes(db):
    """The hash columns exist on both backends, so nothing falls back to an
    insert that drops them."""
    uid = _user(db)
    db.log_ai_usage(uid, 'chat', 'BTC', 10, 20, 1, 2, 'model-x',
                    prompt_sha256='p' * 64, response_sha256='r' * 64,
                    conversation_id='conv-1')
    row = db._get_conn().execute(
        'SELECT prompt_sha256, response_sha256, conversation_id FROM ai_usage_log '
        'WHERE user_id = ?', (uid,)).fetchone()
    assert (row['prompt_sha256'], row['response_sha256'], row['conversation_id']) == (
        'p' * 64, 'r' * 64, 'conv-1')


def test_add_ai_credits_raises_when_the_credit_row_cannot_be_created(db):
    """A failed credit-row INSERT raises instead of letting the following
    UPDATE match no row and return True."""
    uid = _user(db)
    conn = db._get_conn()
    conn.execute("CREATE TRIGGER block_credit_row BEFORE INSERT ON ai_credits "
                 "BEGIN SELECT RAISE(ABORT, 'credit row refused'); END")
    conn.commit()
    with pytest.raises(Exception, match='credit row refused'):
        db.add_ai_credits(uid, 500)
    assert db.get_ai_balance(uid) == 0
