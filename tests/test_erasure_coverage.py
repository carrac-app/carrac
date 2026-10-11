"""Every table keyed to a user must have an erasure decision.

The recurring defect is drift, not a missing line: `anonymize_user` was correct
when written, then a new table such as `exchange_connections` lands and nobody
adds it, so encrypted exchange API credentials and a Hyperliquid wallet
address survive account deletion. Fixing such tables by hand, once, does not
stop the drift.

So this test does not check for two table names. It reads the REAL Postgres
schema and requires every `user_id` table to appear in exactly one of two
lists: erased, or deliberately retained with a stated reason. A new table is a
failing test, not a silent survivor.

Schema source is `tests/schema/carrac_pg_schema.sql` on purpose — the suite runs
SQLite, so a fixture-derived list would not see what prod actually has.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_SCHEMA = Path(__file__).parent / "schema" / "carrac_pg_schema.sql"
_USER_DB = Path(__file__).parent.parent / "user_db.py"


# Tables keyed to a user that anonymize_user deliberately does NOT delete.
# anonymize_user scrubs the USER RECORD, so rows left here point at an
# anonymous subject rather than an identifiable person. Anything holding live
# secret material or user-authored content does NOT belong in this list —
# anonymising the user does not make an encrypted API key any less of a key.
_RETAINED = {
    'processed_payments':
        'Payment/idempotency records. Retained for accounting and to keep '
        'refund + replay protection working; the payer is anonymised.',
    # ai_usage and ai_usage_log do not belong here: anonymize_user deletes
    # both by explicit DELETE below the tables_to_clear loop (the "30-day
    # cron" prunes data/ai-logs/ day-folders on disk, never the table).
    'audit_log':
        'Security/accountability trail (actor + subject ids, no FK). Retained '
        'as a legitimate-interest record of administrative action — an audit '
        'log a subject can erase is not an audit log. NOTE: before_json / '
        'after_json can carry pre-change snapshots of user fields, so the ROW '
        'is retained but its payload needs a scrubbing rule; tracked on '
        '1 row in production as of 2026-08-12.',
    'tier_config':
        'Global tier limits. updated_by names the ADMIN who last changed a '
        'plan, not a data subject in the row. Retained as configuration '
        'provenance; carries no personal data about the referenced user '
        'beyond their id.',
    'invites':
        'Invite graph (created_by = issuing admin, used_by = redeemer). '
        'Retained so redemption/attribution stays reconstructable after a '
        'redeemer erases. CAVEAT, not yet fixed: when type = "email" the '
        '`value` column holds the invitee address, which is personal data '
        'about someone who may never have signed up — that field needs '
        'nulling on erasure. Tracked as follow-up.',
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
    # Matching only a column named literally `user_id` would make a table
    # keyed to a user under ANY other name invisible to every assertion
    # below — audit_log (actor_user_id / subject_user_id), invites
    # (created_by / used_by) and tier_config (updated_by) would sail through
    # with no erasure decision while the suite stayed green. A guard that can
    # only see one spelling of the thing it guards is decoration.
    #
    # Two signals, because neither alone is complete:
    #   * a declared FOREIGN KEY to users(id) — the schema's own answer, and
    #     what catches invites.created_by / tier_config.updated_by;
    #   * any column matching \w*user_id — catches audit_log, whose
    #     actor_user_id / subject_user_id carry no FK constraint at all.
    #
    # NOT caught by either, deliberately: `waitlist`, which holds email + ip
    # with no reference to users. Non-account personal data is a separate
    # problem with a separate erasure route — this guard
    # would give false comfort if it pretended to cover it.
    by_fk = set(re.findall(
        r'ALTER TABLE ONLY (?:public\.)?"?(\w+)"?[^;]*?'
        r'REFERENCES (?:public\.)?"?users"?\s*\(', sql, re.S | re.I))
    by_column = {t for t, body in tables.items()
                 if re.search(r'^\s*"?\w*user_id"?\s', body, re.M | re.I)}
    return (by_fk | by_column) - {'users'}


def _erased_tables() -> set[str]:
    """Tables anonymize_user deletes — via EITHER of the two ways it says so.

    Parsing only `tables_to_clear` was the second blind spot.
    `anonymize_user` also issues explicit `DELETE FROM <t> WHERE user_id = ?`
    statements below that loop (ai_conversations, ai_usage_log, ai_usage,
    ai_messages). Those were invisible here, so `ai_usage` and `ai_usage_log`
    sat in `_RETAINED` — documented as deliberately kept — while the code
    deleted them, and `test_retained_list_has_no_stale_entries`, which exists
    precisely to catch that, could not see the contradiction.
    """
    src = _USER_DB.read_text()
    block = re.search(r'tables_to_clear = \[(.*?)\]', src, re.S)
    assert block, "tables_to_clear not found in user_db.py — erasure was refactored"
    names = set(re.findall(r"'(\w+)'", block.group(1)))
    assert len(names) > 20, f"only found {len(names)} tables; the parse is wrong"

    anon = re.search(r'def anonymize_user\(.*?\n    def ', src, re.S)
    body = anon.group(0) if anon else src[src.index('def anonymize_user('):]
    explicit = set(re.findall(r'DELETE FROM (\w+) WHERE user_id', body, re.I))
    assert explicit, (
        "no explicit `DELETE FROM <t> WHERE user_id` found inside "
        "anonymize_user — either erasure was refactored or this parse is "
        "blind again; do not let it return quietly"
    )
    return names | explicit


def test_schema_parse_is_not_silently_empty():
    """The guard's own guard. A regex that stops matching would turn every
    assertion below into a vacuous pass — the usual way a guard
    fails."""
    uid = _schema_user_id_tables()
    assert len(uid) > 30, f"expected dozens of user_id tables, got {len(uid)}"
    assert 'trade_journal' in uid and 'drawings' in uid


def test_every_user_table_has_an_erasure_decision():
    """A new user-keyed table must be erased or explicitly retained."""
    undecided = _schema_user_id_tables() - _erased_tables() - set(_RETAINED)
    assert not undecided, (
        "these tables are keyed to a user but appear in neither "
        f"anonymize_user's tables_to_clear nor the retained list: {sorted(undecided)}.\n"
        "Add them to tables_to_clear in user_db.py, or to _RETAINED here with a "
        "reason. Leaving a table out means its rows survive account deletion — "
        "which is how encrypted exchange credentials outlived erasure."
    )


def test_retained_list_has_no_stale_entries():
    """A retained table that no longer exists, or that is now also erased,
    means the reasons in _RETAINED have stopped describing reality."""
    uid = _schema_user_id_tables()
    erased = _erased_tables()
    for table in _RETAINED:
        assert table in uid, f"{table} is retained but no longer in the schema"
        assert table not in erased, (
            f"{table} is documented in _RETAINED as deliberately kept, but "
            "anonymize_user DELETES it — either via tables_to_clear or via an "
            "explicit `DELETE FROM ... WHERE user_id`. The retention reason is "
            "false. This is how ai_usage / ai_usage_log sat here for weeks "
            "claiming a cron prune that did not exist."
        )


def test_secret_bearing_tables_are_erased_not_retained():
    """Anonymising the user does not neutralise a stored credential. These
    must be DELETED, and must never be moved to _RETAINED."""
    erased = _erased_tables()
    for table in ('exchange_connections', 'api_keys', 'widget_tokens',
                  'password_reset_tokens', 'auth_providers'):
        assert table in erased, (
            f"{table} holds credential material and must be deleted on erasure, "
            "not merely detached from an anonymised user"
        )
        assert table not in _RETAINED


@pytest.mark.parametrize('table', ['exchange_connections',
                                   'exchange_import_suggestions',
                                   'monthly_goals'])
def test_erasure_covers_credentials_imports_and_goals(table):
    """Pins erasure of three user-data tables.

    exchange_connections stores encrypted API credentials and a Hyperliquid
    wallet address; exchange_import_suggestions holds imported trade data;
    monthly_goals is user-authored journal content whose sibling
    (journal_goals) is also covered. All three must be erased.
    """
    assert table in _erased_tables()
