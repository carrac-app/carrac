"""widget_tokens.revoked is a Postgres BOOLEAN.

Failure mode: ``POST /api/widget/tokens`` returns 500 with
``operator does not exist: boolean = integer`` if
``create_widget_token`` compares ``revoked = 0`` or ``revoke_widget_token``
assigns ``SET revoked = 1`` — integer literals against a Postgres BOOLEAN
column (schema: ``revoked BOOLEAN DEFAULT FALSE``). SQLite treats booleans
as 0/1 so the whole suite stays green while the feature is 100% broken on
prod — the classic SQLite-CI-vs-Postgres gap.

CI is SQLite-only, so it CANNOT reproduce the runtime error. This guard
instead pins the invariant at the SQL-literal level: widget_tokens boolean
predicates must use ``FALSE``/``TRUE``, never ``0``/``1``. Plus a functional
create→list→revoke cycle (behavioural sanity, passes on SQLite either way).
"""

from __future__ import annotations

import inspect
import re


import user_db as user_db_module


def _src(method_name: str) -> str:
    return inspect.getsource(getattr(user_db_module.UserDatabase, method_name))


def test_create_widget_token_uses_boolean_false_not_integer():
    src = _src("create_widget_token")
    assert "revoked = 0" not in src, (
        "widget_tokens.revoked is a Postgres BOOLEAN — 'revoked = 0' raises "
        "'operator does not exist: boolean = integer' on prod"
    )
    assert re.search(r"revoked\s*=\s*FALSE", src), (
        "the active-token count must compare revoked = FALSE"
    )


def test_revoke_widget_token_uses_boolean_true_not_integer():
    src = _src("revoke_widget_token")
    assert "revoked = 1" not in src, (
        "widget_tokens.revoked is a Postgres BOOLEAN — 'SET revoked = 1' is "
        "rejected in production (column is boolean, expression is integer)"
    )
    assert re.search(r"SET\s+revoked\s*=\s*TRUE", src), (
        "revoke must assign revoked = TRUE"
    )


def test_widget_token_create_list_revoke_cycle(app):
    """End-to-end on the test DB: create → appears in list → revoke → gone."""
    from user_db import user_db

    uid = user_db.create_user("widget-cycle@test.dev", "StrongPass!123",
                              tos_accepted=True)
    assert uid

    token_id = user_db.create_widget_token(uid, "hash_" + "a" * 56, "ab12",
                                           name="iPhone")
    assert token_id, "create_widget_token should return a new id"

    tokens = user_db.list_widget_tokens(uid)
    assert any(t.get("name") == "iPhone" for t in tokens)
    assert all(not t.get("revoked") for t in tokens)

    assert user_db.revoke_widget_token(token_id, uid) is True

    tokens_after = user_db.list_widget_tokens(uid)
    live = [t for t in tokens_after if not t.get("revoked")]
    assert not live, "revoked token must not remain active"
