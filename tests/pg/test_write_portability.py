"""Write-surface SQL portability on real Postgres.

Prod runs Postgres; CI otherwise runs SQLite, which silently accepts SQL that
Postgres rejects (boolean=integer, dict binding, type coercion, txn-abort).
Such bugs can ship green (widget_tokens boolean=integer, journal goals
dict). These tests run the actual write-path DB methods against
a real Postgres loaded with the true prod schema, so that class of bug fails
in CI instead of in production.

Each test creates its own user; ``pg_clean`` truncates between tests.
"""

from __future__ import annotations

import json
import secrets



def _mk_user(udb) -> int:
    uid = udb.create_user(
        f"pg-{secrets.token_hex(4)}@test.dev", "StrongPass!123", tos_accepted=True
    )
    assert uid, "create_user must return a real id on Postgres"
    return uid


# ---------------------------------------------------------------------------
# Widget tokens — the boolean=integer bug.
# ---------------------------------------------------------------------------

def test_widget_token_create_list_revoke(udb, pg_conn):
    uid = _mk_user(udb)

    tid = udb.create_widget_token(uid, "hash_" + "a" * 56, "ab12", name="iPhone")
    assert tid, "create_widget_token returned None — the revoked column must be written as a boolean"

    tokens = udb.list_widget_tokens(uid)
    assert any(t.get("name") == "iPhone" for t in tokens)
    assert all(not t.get("revoked") for t in tokens)

    assert udb.revoke_widget_token(tid, uid) is True

    with pg_conn.cursor() as cur:
        cur.execute("SELECT revoked FROM widget_tokens WHERE id = %s", (tid,))
        assert cur.fetchone()[0] is True, "revoke must set the boolean column TRUE"


def test_widget_token_limit_enforced_on_pg(udb):
    """The 5-token limit counts via `revoked = FALSE` — must run on Postgres."""
    uid = _mk_user(udb)
    ids = [
        udb.create_widget_token(uid, "hash_" + secrets.token_hex(28), f"s{i:02d}",
                                name=f"tok{i}")
        for i in range(5)
    ]
    assert all(ids), "first five tokens must create cleanly"
    # 6th is refused by the count gate (returns None), not an error.
    assert udb.create_widget_token(uid, "hash_" + secrets.token_hex(28), "s99",
                                   name="over") is None


# ---------------------------------------------------------------------------
# Journal goals — the dict-not-serialised bug (JSON text column).
# ---------------------------------------------------------------------------

def test_journal_goals_upsert_roundtrip(cdb, udb, pg_conn):
    uid = _mk_user(udb)
    goals = {"target_r": 10, "max_trades_per_day": 3}

    gid = cdb.upsert_goals(uid, "2026-07", json.dumps(goals))
    assert gid

    # Upsert again (same month) must UPDATE, not duplicate.
    gid2 = cdb.upsert_goals(uid, "2026-07", json.dumps({"target_r": 8}))
    assert gid2 == gid

    with pg_conn.cursor() as cur:
        cur.execute("SELECT goals FROM journal_goals WHERE user_id = %s AND month = %s",
                    (uid, "2026-07"))
        rows = cur.fetchall()
    assert len(rows) == 1, "upsert must not create a duplicate row"
    assert json.loads(rows[0][0]) == {"target_r": 8}


# ---------------------------------------------------------------------------
# Journal trade / session, drawings, plays — broader write-surface on PG.
# ---------------------------------------------------------------------------

def test_journal_trade_open_close(cdb, udb):
    uid = _mk_user(udb)
    tid = cdb.log_trade_open(
        uid, "BTC", "1D", "long", 65000.0,
        target_price=75000.0, stop_price=60000.0, setup_type="breakout",
    )
    assert tid
    assert cdb.log_trade_close(tid, 70000.0, exit_reason="target") is True
    entry = cdb.get_journal_entry(tid)
    assert entry and entry.get("exit_price") == 70000.0


def test_drawing_save_and_read(cdb, udb):
    uid = _mk_user(udb)
    did = cdb.save_drawing(
        asset="BTCUSDT", timeframe="1D", drawing_type="horizontal_line",
        price_level=65000.0, label="pg-portability", user_id=uid,
    )
    assert did
    got = cdb.get_drawing_by_id(did)
    assert got and got.get("user_id") == uid and got.get("label") == "pg-portability"


def test_play_create_on_pg(udb):
    uid = _mk_user(udb)
    from carrac.plays.models import play_create

    play = play_create(
        uid, title="PG portability play", primary_asset="BTC",
        direction="long", thesis="exercise the plays INSERT on Postgres",
    )
    assert play and play.get("id") and play.get("title") == "PG portability play"


# ---------------------------------------------------------------------------
# GDPR erasure — anonymize_user. Sweep #6 flagged a PG txn-abort
# risk that SQLite can't reproduce; this runs it on real Postgres.
# ---------------------------------------------------------------------------

def test_anonymize_user_completes_on_pg(cdb, udb, pg_conn):
    uid = _mk_user(udb)
    # Seed some owned data across tables the erasure touches.
    udb.create_widget_token(uid, "hash_" + "c" * 56, "zz99", name="phone")
    cdb.upsert_goals(uid, "2026-07", json.dumps({"target_r": 5}))
    cdb.save_drawing(asset="BTCUSDT", timeframe="1D", drawing_type="horizontal_line",
                     price_level=1.0, label="x", user_id=uid)

    assert udb.anonymize_user(uid) is True, (
        "anonymize_user must complete on Postgres, not abort the transaction"
    )

    with pg_conn.cursor() as cur:
        cur.execute("SELECT email, is_active FROM users WHERE id = %s", (uid,))
        email, is_active = cur.fetchone()
    assert email == f"deleted-{uid}@deleted.carrac.cc"
    assert is_active is False
