"""Every table keyed to a user must have an EXPORT decision.

The sibling of `test_erasure_coverage.py`, and the same drift class. Erasure and export must both cover every user-keyed table, so a user exercising their
Art. 15 right-of-access gets a complete file.

privacy.html section 9 offers that export as the right-of-access path, so the
gap is a promise the product does not keep, not merely an incomplete feature.

As with erasure, this does NOT check for a list of table names. It reads the
REAL Postgres schema and requires every `user_id` table to be either exported or
explicitly export-exempt with a stated reason. A new table is a failing test,
not a silent omission.

Schema source is `tests/schema/carrac_pg_schema.sql` on purpose — the suite runs
SQLite, so a fixture-derived list would not see what prod actually has.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_SCHEMA = Path(__file__).parent / "schema" / "carrac_pg_schema.sql"
_USER_DB = Path(__file__).parent.parent / "user_db.py"


# Tables keyed to a user that export_user_data deliberately does NOT include.
#
# Two admissible reasons only: the table holds CREDENTIAL material (exporting it
# would hand a copy of a live secret to whoever holds the download), or it holds
# a SYSTEM/ACCOUNTING record that is about the account rather than authored by
# the user. User-authored content never belongs here — "we would rather not" is
# not a lawful basis under Art. 15.
_EXPORT_EXEMPT = {
    'api_keys':
        'Live API credentials. Exporting them would put working secrets in a '
        'file the user may email to themselves; they can list and rotate keys '
        'in the UI instead.',
    'password_reset_tokens':
        'Short-lived credential material. Exporting an unexpired token would '
        'be an account-takeover primitive.',
    'email_verifications':
        'Short-lived credential material, same reasoning as reset tokens.',
    'auth_providers':
        'Provider subject identifiers for social login — credential material. '
        'Nothing writes this table yet: social login is not connected.',
    'processed_payments':
        'Payment/idempotency accounting records. The user-facing billing '
        'history is Paddle, who is the merchant of record and holds the '
        'authoritative invoices.',
    'ai_deposits':
        'Internal AI credit accounting ledger, not user-authored content.',
    'user_tier_overrides':
        'Admin-applied tier grants. An operational record about the account, '
        'written by staff rather than the user.',
    'onboarding':
        'Onboarding step-completion flags. System state, not user content.',
}


def _schema_user_id_tables() -> set[str]:
    sql = _SCHEMA.read_text()
    tables = {
        m.group(1): m.group(2)
        for m in re.finditer(
            r'CREATE TABLE (?:IF NOT EXISTS )?(?:public\.)?"?(\w+)"?\s*\((.*?)\n\);',
            sql, re.S | re.I)
    }
    assert len(tables) > 40, (
        f"only parsed {len(tables)} tables from the schema — the CREATE TABLE "
        "regex has stopped matching, so this whole test is blind"
    )
    return {t for t, body in tables.items()
            if re.search(r'^\s*"?user_id"?\s', body, re.M | re.I)}


def _export_body() -> str:
    src = _USER_DB.read_text()
    m = re.search(r'def export_user_data\(self.*?(?=\n    def )', src, re.S)
    assert m, "export_user_data not found in user_db.py — it was renamed or moved"
    return m.group(0)


def _exported_tables() -> set[str]:
    """Tables the export actually reads.

    Two sources, because the function has two shapes: bespoke `SELECT ... FROM
    x` statements for the older sections, and a `export_content_tables` list
    driving a generic loop for the rest. Missing the second source would make
    every assertion below vacuous while looking like it passed — so the count
    assertion covers the union, and the list is required to parse non-empty.
    """
    body = _export_body()
    names = set(re.findall(r'FROM\s+(\w+)', body, re.I))

    block = re.search(r'export_content_tables = \[(.*?)\]', body, re.S)
    assert block, (
        "export_content_tables not found in export_user_data — the generic "
        "export loop was renamed or removed, so this guard is blind to it"
    )
    listed = set(re.findall(r"'(\w+)'", block.group(1)))
    assert len(listed) > 5, (
        f"only parsed {len(listed)} tables from export_content_tables; the "
        "parse is wrong"
    )

    names |= listed
    assert len(names) > 15, (
        f"only found {len(names)} tables in export_user_data; the parse is "
        "wrong and every assertion below is vacuous"
    )
    return names


def test_schema_parse_is_not_silently_empty():
    """The guard's own guard — a regex that stops matching turns every
    assertion below into a vacuous pass."""
    uid = _schema_user_id_tables()
    assert len(uid) > 30, f"expected dozens of user_id tables, got {len(uid)}"
    assert 'trade_journal' in uid and 'plays' in uid


def test_every_user_table_has_an_export_decision():
    """A new user-keyed table must be exported or explicitly exempt."""
    undecided = _schema_user_id_tables() - _exported_tables() - set(_EXPORT_EXEMPT)
    assert not undecided, (
        "these tables are keyed to a user but appear in neither "
        f"export_user_data nor the exempt list: {sorted(undecided)}.\n"
        "Add them to export_user_data in user_db.py, or to _EXPORT_EXEMPT here "
        "with a reason. Leaving a table out means a user exercising their "
        "right of access silently does not receive it."
    )


def test_exempt_list_has_no_stale_entries():
    """An exempt table that no longer exists, or that is now also exported,
    means the reasons in _EXPORT_EXEMPT have stopped describing reality."""
    uid = _schema_user_id_tables()
    exported = _exported_tables()
    for table in _EXPORT_EXEMPT:
        assert table in uid, f"{table} is export-exempt but no longer in the schema"
        assert table not in exported, (
            f"{table} is BOTH exported and listed in _EXPORT_EXEMPT — the "
            "exemption reason is now false"
        )


@pytest.mark.parametrize('table', ['api_keys', 'password_reset_tokens',
                                   'email_verifications', 'auth_providers'])
def test_credential_tables_are_never_exported(table):
    """The inverse of the erasure suite's secret-bearing test.

    Erasure must DELETE these; export must NOT INCLUDE them. Putting a live
    credential into a downloadable file is a different bug with the same root —
    treating a secret as if it were user content.
    """
    assert table not in _exported_tables(), (
        f"{table} holds credential material and must never appear in a data "
        "export"
    )


@pytest.mark.parametrize('table', ['plays', 'play_shares', 'backtest_runs',
                                   'strategies', 'context_recipes',
                                   'score_snapshots', 'monthly_goals',
                                   'journal_screenshots',
                                   'personal_report_sections',
                                   'satisfaction_ratings',
                                   'exchange_import_suggestions'])
def test_the_user_authored_tables_the_audit_found(table):
    """Pins the specific user-authored tables that must be exported.

    All of these are content the user created or that describes their own
    activity, which Art. 15/20 covers. Every one was missing from the export.
    """
    assert table in _exported_tables()


def test_exchange_connections_is_exported_without_the_secret():
    """The one table that is BOTH exportable and dangerous.

    The user is entitled to know which exchanges they connected, when, and how
    it is going. They are not entitled — and neither is anyone who gets hold of
    the file — to the encrypted credential blob. So this asserts on the columns,
    not merely on the table name: a bare `SELECT *` here would satisfy the
    coverage test above while leaking the exact material the erasure work spent a
    release removing from erasure.
    """
    body = _export_body()
    assert 'exchange_connections' in _exported_tables()

    # Take the NEAREST preceding SELECT, not the first one in the function —
    # a lazy `SELECT(.*?)FROM exchange_connections` spans every earlier query
    # and would assert against the wrong column list.
    marker = re.search(r'FROM\s+exchange_connections', body, re.I)
    assert marker, "exchange_connections is exported but its FROM was not found"
    head = body[:marker.start()]
    sel = head.rfind('SELECT')
    assert sel != -1, "no SELECT precedes FROM exchange_connections"
    cols = head[sel:]

    assert '*' not in cols, (
        "SELECT * on exchange_connections would export credentials_encrypted"
    )
    for forbidden in ('credentials_encrypted', 'cursor_data'):
        assert forbidden not in cols, (
            f"{forbidden} must not be exported — it is credential/sync material, "
            "not user content"
        )
    assert 'masked_identifier' in cols, (
        "the masked identifier is what makes the export useful to the user; "
        "if it was dropped, check it was not confused with the raw one"
    )
