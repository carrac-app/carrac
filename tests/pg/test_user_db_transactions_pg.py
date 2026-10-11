"""UserDatabase transaction handling on real Postgres.

On Postgres a failed statement aborts the whole transaction, and every later
statement on that connection fails until it is rolled back. UserDatabase
keeps one connection per thread, so a transaction left open or aborted by one
call is inherited by the next call on the same thread. These tests check the
connection's own transaction status straight after each call, before
anything else gets a chance to clean up.
"""

from __future__ import annotations

import secrets

import psycopg2
import pytest
from psycopg2.extensions import TRANSACTION_STATUS_IDLE
from werkzeug.security import generate_password_hash

PASSWORD = "StrongPass!123"


def _mk_user(udb, email=None) -> int:
    uid = udb.create_user(email or f"tx-{secrets.token_hex(4)}@test.dev", PASSWORD,
                          tos_accepted=True)
    assert uid
    return uid


def _status(udb) -> int:
    return udb._local.pg_conn.info.transaction_status


def test_failed_statement_does_not_poison_the_next_call(udb):
    _mk_user(udb)
    raw = udb._local.pg_conn

    with pytest.raises(psycopg2.Error):
        with udb._tx() as conn:
            conn.execute("SELECT * FROM no_such_table")

    assert udb._local.pg_conn is raw, "the same pooled connection must stay in use"
    assert _status(udb) == TRANSACTION_STATUS_IDLE, "the aborted transaction was left open"
    assert _mk_user(udb), "the next write on the same connection must succeed"


def test_a_method_that_swallows_a_failure_leaves_the_connection_clean(udb):
    _mk_user(udb)
    assert udb.get_user_by_id("not-an-id") is None  # invalid input for an integer column
    assert _status(udb) == TRANSACTION_STATUS_IDLE


@pytest.mark.parametrize("read", [
    lambda udb, uid: udb.get_user_by_id(uid),
    lambda udb, uid: udb.get_preferences(uid),
    lambda udb, uid: udb.count_waitlist(),
    lambda udb, uid: udb.get_waitlist(),
    lambda udb, uid: udb.pick_next_waitlist(5),
])
def test_reads_do_not_leave_a_transaction_open(udb, read):
    uid = _mk_user(udb)
    read(udb, uid)
    assert _status(udb) == TRANSACTION_STATUS_IDLE


def test_read_inside_paddle_unit_does_not_commit_partial_work(udb, pg_conn):
    uid = _mk_user(udb)
    event_id = f"evt_{secrets.token_hex(6)}"

    def work():
        udb.set_paddle_customer_id(uid, "ctm_partial", _commit=False)
        assert udb.get_user_by_id(uid) is not None
        raise RuntimeError("handler failed after a read")

    with pytest.raises(RuntimeError):
        udb.process_paddle_event_atomic(event_id, uid, work)

    assert _status(udb) == TRANSACTION_STATUS_IDLE
    with pg_conn.cursor() as cur:
        cur.execute("SELECT paddle_customer_id FROM users WHERE id = %s", (uid,))
        assert cur.fetchone()[0] is None
        cur.execute("SELECT 1 FROM processed_payments WHERE payment_id = %s", (event_id,))
        assert cur.fetchone() is None


def test_add_ai_credits_creates_the_credit_row(udb):
    uid = _mk_user(udb)
    assert udb.add_ai_credits(uid, 500) is True
    assert udb.get_ai_balance(uid) == 500


def test_authenticate_survives_a_failed_password_rehash(udb, pg_conn):
    """A legacy hash is upgraded on login. If that UPDATE fails, the login
    still succeeds and is stamped: the upgrade has its own transaction."""
    email = f"rehash-{secrets.token_hex(4)}@test.dev"
    uid = _mk_user(udb, email)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                    (generate_password_hash(PASSWORD, method="pbkdf2:sha256"), uid))
        cur.execute("ALTER TABLE users ADD CONSTRAINT block_rehash "
                    "CHECK (password_hash NOT LIKE '$argon2%%') NOT VALID")
    try:
        user = udb.authenticate(email, PASSWORD)
    finally:
        udb._get_conn()  # clears an aborted transaction so its locks cannot block the DROP
        with pg_conn.cursor() as cur:
            cur.execute("ALTER TABLE users DROP CONSTRAINT block_rehash")

    assert user and user["id"] == uid
    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_login, password_hash FROM users WHERE id = %s", (uid,))
        last_login, stored = cur.fetchone()
    assert last_login is not None
    assert stored.startswith("pbkdf2:"), "the refused upgrade must not have been written"


def test_sequence_repair_survives_a_failure_on_a_later_table(udb, pg_conn):
    """A failure on one table must not undo the repair of the tables before it."""
    _mk_user(udb)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT setval('users_id_seq', 1, false)")  # behind MAX(id) = 1
        cur.execute("ALTER SEQUENCE invites_id_seq RENAME TO invites_id_seq_parked")
    try:
        udb._fix_pg_sequences()
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("ALTER SEQUENCE invites_id_seq_parked RENAME TO invites_id_seq")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT nextval('users_id_seq')")
        assert cur.fetchone()[0] == 2
