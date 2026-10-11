"""Async-offload coverage for ``carrac/fastapi_app/routers/journal.py``.

The journal router's ``async def`` handlers call blocking SQLite/Postgres
methods on ``ContentDatabase`` (via ``carrac.content_db.get_content_db``)
and raw ``carrac.db_pool.get_conn`` connections directly, with no
``asyncio.to_thread`` offload. A blocking call inside an ``async def``
handler stalls the uvicorn event loop for EVERY concurrent request, not
just the caller's.

Recipe (offloading blocking calls): the ``asgi_client`` fixture
(``tests/fastapi/conftest.py``) drives the strangler ASGI app via
``run_until_complete`` on the TEST's own thread, so the event-loop thread
ident equals ``threading.get_ident()`` inside the test body. Each test
monkeypatches the narrowest ``ContentDatabase`` method (or
``carrac.db_pool.get_conn`` for the two raw-SQL handlers) the endpoint
under test touches, records the thread the DB call actually executed on,
and asserts that thread is DIFFERENT from the test/event-loop thread —
proof the work was offloaded via ``asyncio.to_thread``.

Auth: ``journal_user``-gated CRUD (trades/sessions/playbooks/checklists/
goals) is exercised by overriding the ``journal_user`` dependency on the
live ``carrac.fastapi_app.fastapi_app`` singleton (the same instance the
strangler mounts) — mirrors the pattern in
``tests/fastapi/test_journal_ai_gate.py::_login_as``. The trade-share
sub-routes use ``auth_or_key`` instead, so those tests reuse the
``member_key`` fixture from ``conftest.py`` (real per-user API key),
matching ``tests/fastapi/test_trade_shares_asgi.py``.
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from carrac.content_db import ContentDatabase, get_content_db


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def login_as():
    """Override ``journal_user`` on the live FastAPI singleton for one test.

    ``carrac.asgi.asgi_app`` mounts this exact ``fastapi_app`` object, so
    overriding a dependency here is visible through the ``asgi_client``
    fixture's full strangler dispatch (ExactPath/PrefixPath + real
    ``classify_request``/rate-limit chain still run for real).
    """
    from carrac.fastapi_app import fastapi_app
    from carrac.fastapi_app.routers.journal import journal_user

    def _do(user: dict) -> dict:
        fastapi_app.dependency_overrides[journal_user] = lambda: user
        return user

    yield _do
    fastapi_app.dependency_overrides.pop(journal_user, None)


def _user(uid: int) -> dict:
    return {"id": uid, "email": f"u{uid}@example.com", "is_admin": False, "tier": "member"}


# ---------------------------------------------------------------------------
# Thread-recording helpers
# ---------------------------------------------------------------------------


def _wrap_db_method(monkeypatch: pytest.MonkeyPatch, method_name: str) -> dict:
    """Record the thread ``ContentDatabase.<method_name>`` executes on while
    still delegating to the REAL implementation (so return values / DB state
    stay realistic)."""
    orig = getattr(ContentDatabase, method_name)
    calls: dict = {}

    def recording(self, *a, **kw):
        calls["thread"] = threading.get_ident()
        calls["count"] = calls.get("count", 0) + 1
        return orig(self, *a, **kw)

    monkeypatch.setattr(ContentDatabase, method_name, recording)
    return calls


def _stub_db_method(monkeypatch: pytest.MonkeyPatch, method_name: str, return_value) -> dict:
    """Replace ``ContentDatabase.<method_name>`` with a canned return value,
    recording the thread it executed on. Use when the handler under test
    doesn't need real DB state (e.g. an ownership check short-circuiting
    to a 404)."""
    calls: dict = {}

    def recording(self, *a, **kw):
        calls["thread"] = threading.get_ident()
        return return_value

    monkeypatch.setattr(ContentDatabase, method_name, recording)
    return calls


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Stand-in for the raw ``carrac.db_pool.get_conn`` connection used by
    the two raw-SQL journal handlers (``bulk_update_trades``,
    ``delete_checklist``).

    The test environment runs SQLite (``DB_TYPE=sqlite``), where
    ``ContentDatabase`` (``drawings.db``, owns ``trade_journal`` /
    ``setup_checklists``) and the "main pool" ``carrac.db_pool.get_conn``
    (``carrac.db``) are two SEPARATE physical files — a real pool
    connection 404s with "no such table" here. In production (Postgres)
    both share one database/pool, so this split is SQLite-test-only. This
    fake sidesteps that split while still proving the pool-based call runs
    off the event-loop thread.
    """

    def __init__(self, select_rows):
        self._select_rows = select_rows

    def execute(self, query, params=None):
        return _FakeCursor(self._select_rows)

    def commit(self):
        pass


@contextlib.contextmanager
def _fake_pool_conn(monkeypatch: pytest.MonkeyPatch, select_rows: list):
    """Record the thread ``carrac.db_pool.get_conn`` executed on, yielding a
    ``_FakeConn`` instead of a real connection (see ``_FakeConn`` docstring
    for why). The two raw-SQL journal handlers import ``get_conn`` fresh
    (``from carrac.db_pool import get_conn as _pool_get_conn``) inside the
    handler body on every call, so patching the module attribute before the
    request is dispatched is picked up."""
    from carrac import db_pool

    calls: dict = {}

    @contextlib.contextmanager
    def recording(*a, **kw):
        calls["thread"] = threading.get_ident()
        calls["count"] = calls.get("count", 0) + 1
        yield _FakeConn(select_rows)

    monkeypatch.setattr(db_pool, "get_conn", recording)
    yield calls


# ---------------------------------------------------------------------------
# Trades — create / update / close / get-entry / delete
# ---------------------------------------------------------------------------


def test_create_trade_offloads_log_trade_open(asgi_client, login_as, monkeypatch):
    monkeypatch.setattr("carrac.integrations.on_trade_opened", lambda *a, **kw: None)
    monkeypatch.setattr("carrac.scanner.snapshot.capture_score_snapshot", lambda *a, **kw: None)
    calls = _wrap_db_method(monkeypatch, "log_trade_open")
    login_as(_user(90601))

    resp = asgi_client.post(
        "/api/journal/trades",
        json={"asset": "BTC", "entry_price": 50000.0, "direction": "long"},
    )
    assert resp.status_code == 201, resp.text
    assert "id" in resp.json()
    assert calls.get("thread") is not None, "log_trade_open was never called"
    assert calls["thread"] != threading.get_ident(), (
        "log_trade_open ran on the event-loop thread — not offloaded"
    )


def test_update_trade_offloads_update_trade(asgi_client, login_as, monkeypatch):
    uid = 90602
    db = get_content_db()
    trade_id = db.log_trade_open(
        user_id=uid, asset="BTC", timeframe="1D", direction="long", entry_price=100.0,
    )
    login_as(_user(uid))
    calls = _wrap_db_method(monkeypatch, "trade_update")

    resp = asgi_client.patch(f"/api/journal/trades/{trade_id}", json={"notes": "solid setup"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert calls.get("thread") is not None, "trade_update was never called"
    assert calls["thread"] != threading.get_ident(), (
        "trade_update ran on the event-loop thread — not offloaded"
    )


def test_close_trade_offloads_log_trade_close(asgi_client, login_as, monkeypatch):
    uid = 90603
    db = get_content_db()
    trade_id = db.log_trade_open(
        user_id=uid, asset="ETH", timeframe="1D", direction="long", entry_price=2000.0,
    )
    login_as(_user(uid))
    monkeypatch.setattr(
        "carrac.journal.mfe_mae.calculate_and_store_mfe_mae", lambda *a, **kw: {"error": "skip"}
    )
    monkeypatch.setattr("carrac.scanner.snapshot.capture_score_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr("carrac.integrations.on_trade_closed", lambda *a, **kw: None)
    calls = _wrap_db_method(monkeypatch, "log_trade_close")

    resp = asgi_client.post(
        f"/api/journal/trades/{trade_id}/close",
        json={"exit_price": 2200.0, "exit_reason": "tp_hit"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert calls.get("thread") is not None, "log_trade_close was never called"
    assert calls["thread"] != threading.get_ident(), (
        "log_trade_close ran on the event-loop thread — not offloaded"
    )


def test_delete_trade_offloads_get_entry_and_delete(asgi_client, login_as, monkeypatch):
    uid = 90604
    db = get_content_db()
    trade_id = db.log_trade_open(
        user_id=uid, asset="SOL", timeframe="1D", direction="long", entry_price=20.0,
    )
    login_as(_user(uid))
    entry_calls = _wrap_db_method(monkeypatch, "get_journal_entry")
    delete_calls = _wrap_db_method(monkeypatch, "trade_delete")

    resp = asgi_client.delete(f"/api/journal/trades/{trade_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert entry_calls.get("thread") is not None, "get_journal_entry was never called"
    assert entry_calls["thread"] != threading.get_ident(), (
        "get_journal_entry (ownership check) ran on the event-loop thread — not offloaded"
    )
    assert delete_calls.get("thread") is not None, "trade_delete was never called"
    assert delete_calls["thread"] != threading.get_ident(), (
        "trade_delete ran on the event-loop thread — not offloaded"
    )


# ---------------------------------------------------------------------------
# Trades — bulk (raw db_pool.get_conn shape)
# ---------------------------------------------------------------------------


def test_bulk_trades_offloads_pool_select(asgi_client, login_as, monkeypatch):
    login_as(_user(90605))
    # select_rows echoes back the requested (nonexistent) id as "owned" so
    # db.delete_trades_bulk() — a REAL ContentDatabase call against the
    # (schema-complete) drawings.db — runs as a harmless no-op (0 affected).
    with _fake_pool_conn(monkeypatch, [{"id": 999999}]) as calls:
        resp = asgi_client.post(
            "/api/journal/trades/bulk",
            json={"trade_ids": [999999], "action": "delete"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert calls.get("thread") is not None, "db_pool.get_conn was never called"
    assert calls["thread"] != threading.get_ident(), (
        "the ownership SELECT ran on the event-loop thread — not offloaded"
    )


# ---------------------------------------------------------------------------
# Trade shares — create / update / revoke (auth_or_key)
# ---------------------------------------------------------------------------


def test_create_trade_share_offloads_ownership_check(asgi_client, member_key, monkeypatch):
    headers, _uid = member_key
    calls = _stub_db_method(monkeypatch, "get_journal_entry", None)

    resp = asgi_client.post("/api/journal/trades/999999/share", headers=headers, json={})
    assert resp.status_code == 404, resp.text
    assert calls.get("thread") is not None, "get_journal_entry was never called"
    assert calls["thread"] != threading.get_ident(), (
        "_owned_closed_trade_or_raise ran on the event-loop thread — not offloaded"
    )


def test_update_trade_share_offloads_shares_update(asgi_client, member_key, monkeypatch):
    headers, _uid = member_key
    calls: dict = {}

    def fake_update(trade_id, user_id, **kw):
        calls["thread"] = threading.get_ident()
        return None

    monkeypatch.setattr("carrac.plays.shares.update_trade_share", fake_update)

    resp = asgi_client.patch("/api/journal/trades/999999/share", headers=headers, json={})
    assert resp.status_code == 404, resp.text
    assert calls.get("thread") is not None, "shares.update_trade_share was never called"
    assert calls["thread"] != threading.get_ident(), (
        "update_trade_share ran on the event-loop thread — not offloaded"
    )


def test_revoke_trade_share_offloads_shares_revoke(asgi_client, member_key, monkeypatch):
    headers, _uid = member_key
    calls: dict = {}

    def fake_revoke(trade_id, user_id):
        calls["thread"] = threading.get_ident()
        return 0

    monkeypatch.setattr("carrac.plays.shares.revoke_trade_share", fake_revoke)

    resp = asgi_client.delete("/api/journal/trades/999999/share", headers=headers)
    assert resp.status_code == 200, resp.text
    assert calls.get("thread") is not None, "shares.revoke_trade_share was never called"
    assert calls["thread"] != threading.get_ident(), (
        "revoke_trade_share ran on the event-loop thread — not offloaded"
    )


# ---------------------------------------------------------------------------
# Sessions — create / update / delete
# ---------------------------------------------------------------------------


def test_create_session_offloads_create_session(asgi_client, login_as, monkeypatch):
    login_as(_user(90606))
    calls = _wrap_db_method(monkeypatch, "create_session")

    resp = asgi_client.post("/api/journal/sessions", json={"date": "2026-05-10"})
    assert resp.status_code == 201, resp.text
    assert calls.get("thread") is not None, "create_session was never called"
    assert calls["thread"] != threading.get_ident(), (
        "create_session ran on the event-loop thread — not offloaded"
    )


def test_update_session_offloads_update_session(asgi_client, login_as, monkeypatch):
    uid = 90607
    db = get_content_db()
    session_id = db.create_session(uid, "2026-05-11", "default", None, None)
    login_as(_user(uid))
    calls = _wrap_db_method(monkeypatch, "update_session")

    resp = asgi_client.patch(f"/api/journal/sessions/{session_id}", json={"post_notes": "ok"})
    assert resp.status_code == 200, resp.text
    assert calls.get("thread") is not None, "update_session was never called"
    assert calls["thread"] != threading.get_ident(), (
        "update_session ran on the event-loop thread — not offloaded"
    )


def test_delete_session_offloads_delete_session(asgi_client, login_as, monkeypatch):
    uid = 90608
    db = get_content_db()
    session_id = db.create_session(uid, "2026-05-12", "default", None, None)
    login_as(_user(uid))
    calls = _wrap_db_method(monkeypatch, "delete_session")

    resp = asgi_client.delete(f"/api/journal/sessions/{session_id}")
    assert resp.status_code == 200, resp.text
    assert calls.get("thread") is not None, "delete_session was never called"
    assert calls["thread"] != threading.get_ident(), (
        "delete_session ran on the event-loop thread — not offloaded"
    )


# ---------------------------------------------------------------------------
# Playbooks — create / update / delete
# ---------------------------------------------------------------------------


def test_create_playbook_offloads_create_playbook(asgi_client, login_as, monkeypatch):
    login_as(_user(90609))
    calls = _wrap_db_method(monkeypatch, "create_playbook")

    resp = asgi_client.post("/api/journal/playbooks", json={"name": "Breakout"})
    assert resp.status_code == 201, resp.text
    assert calls.get("thread") is not None, "create_playbook was never called"
    assert calls["thread"] != threading.get_ident(), (
        "create_playbook ran on the event-loop thread — not offloaded"
    )


def test_update_playbook_offloads_update_playbook(asgi_client, login_as, monkeypatch):
    uid = 90610
    db = get_content_db()
    playbook_id = db.create_playbook(user_id=uid, name="Reversal")
    login_as(_user(uid))
    calls = _wrap_db_method(monkeypatch, "update_playbook")

    resp = asgi_client.patch(f"/api/journal/playbooks/{playbook_id}", json={"description": "desc"})
    assert resp.status_code == 200, resp.text
    assert calls.get("thread") is not None, "update_playbook was never called"
    assert calls["thread"] != threading.get_ident(), (
        "update_playbook ran on the event-loop thread — not offloaded"
    )


def test_delete_playbook_offloads_delete_playbook(asgi_client, login_as, monkeypatch):
    uid = 90611
    db = get_content_db()
    playbook_id = db.create_playbook(user_id=uid, name="Scalp")
    login_as(_user(uid))
    calls = _wrap_db_method(monkeypatch, "delete_playbook")

    resp = asgi_client.delete(f"/api/journal/playbooks/{playbook_id}")
    assert resp.status_code == 200, resp.text
    assert calls.get("thread") is not None, "delete_playbook was never called"
    assert calls["thread"] != threading.get_ident(), (
        "delete_playbook ran on the event-loop thread — not offloaded"
    )


# ---------------------------------------------------------------------------
# Checklists — upsert (ContentDatabase) / delete (raw db_pool.get_conn)
# ---------------------------------------------------------------------------


def test_upsert_checklist_offloads_upsert_checklist(asgi_client, login_as, monkeypatch):
    login_as(_user(90612))
    calls = _wrap_db_method(monkeypatch, "upsert_checklist")

    resp = asgi_client.post(
        "/api/journal/checklists",
        json={"setup_type": "breakout", "checklist_items": ["Confirm volume"]},
    )
    assert resp.status_code == 200, resp.text
    assert calls.get("thread") is not None, "upsert_checklist was never called"
    assert calls["thread"] != threading.get_ident(), (
        "upsert_checklist ran on the event-loop thread — not offloaded"
    )


def test_delete_checklist_offloads_pool_conn(asgi_client, login_as, monkeypatch):
    uid = 90613
    login_as(_user(uid))

    # The ownership-check SELECT is faked to return a matching row so the
    # handler proceeds to DELETE (see _FakeConn docstring for why a real
    # pool connection can't see setup_checklists in the SQLite test env).
    with _fake_pool_conn(monkeypatch, [{"id": 1}]) as calls:
        resp = asgi_client.delete("/api/journal/checklists/1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert calls.get("thread") is not None, "db_pool.get_conn was never called"
    assert calls["thread"] != threading.get_ident(), (
        "delete_checklist's SELECT/DELETE ran on the event-loop thread — not offloaded"
    )
    # SELECT ownership check + DELETE — both pool calls must be offloaded.
    assert calls.get("count", 0) == 2


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


def test_set_goals_offloads_upsert_goals(asgi_client, login_as, monkeypatch):
    login_as(_user(90614))
    # Stubbed (not delegated to the real impl): ContentDatabase.upsert_goals
    # binds `goals` as a raw sqlite3 parameter, and the FastAPI GoalRequest
    # model passes it through as a dict — sqlite3 can't bind a dict
    # (pre-existing data-layer mismatch, unrelated to the
    # async-offload scope). Stubbing isolates this test to what it's meant
    # to prove: the DB call runs off the event-loop thread.
    calls = _stub_db_method(monkeypatch, "upsert_goals", 1)

    resp = asgi_client.post(
        "/api/journal/goals",
        json={"month": "2026-05", "goals": {"target_r": 10}},
    )
    assert resp.status_code == 200, resp.text
    assert calls.get("thread") is not None, "upsert_goals was never called"
    assert calls["thread"] != threading.get_ident(), (
        "upsert_goals ran on the event-loop thread — not offloaded"
    )
