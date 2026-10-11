"""Tests for the real user_db.UserDatabase (production auth DB).

Unlike a test against a parallel in-test UserDatabase copy, this hits
the real module via a tmp sqlite file so production code paths are covered.

Covers:
- User creation (normal, admin auto-flag, duplicate rejection)
- Argon2id hashing + authentication (correct, wrong password,
  inactive user, nonexistent user)
- Legacy pbkdf2 / scrypt upgrade path (write legacy hash, verify it
  authenticates, confirm upgrade to argon2 on success)
- Password update
- Email verification flow
- Password reset token issue + consume
- Per-user API key creation + verification + revoke
- GDPR anonymize
- Watchlist / preferences round-trip
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh UserDatabase instance backed by a tmp SQLite file."""
    # Force SQLite even if DB_TYPE=postgresql is set in the env.
    monkeypatch.setenv("DB_TYPE", "sqlite")
    # Isolate ADMIN_EMAIL so we don't accidentally flag real test users as admin.
    monkeypatch.setenv("ADMIN_EMAIL", "admin-test@example.com")

    # Import fresh so module-level ADMIN_EMAIL picks up the patched env.
    import importlib
    import user_db as ud_module
    importlib.reload(ud_module)
    db_path = tmp_path / "users.db"
    db = ud_module.UserDatabase(db_path=str(db_path))

    # Schema columns added by later migrations are not in _init_sqlite_schema.
    # Add them defensively so anonymize_user / GDPR paths exercise without
    # alembic having to run here.
    conn = db._get_conn()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE users ADD COLUMN trader_profile TEXT")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE users ADD COLUMN subscription_expires_at TIMESTAMP")
    except Exception:
        pass
    conn.commit()
    return db


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def test_create_user_returns_id(db):
    uid = db.create_user("alice@example.com", "StrongPass!123", display_name="Alice", tos_accepted=True)
    assert isinstance(uid, int) and uid > 0


def test_create_duplicate_rejected(db):
    first = db.create_user("dup@example.com", "StrongPass!123", tos_accepted=True)
    second = db.create_user("dup@example.com", "OtherPass!456", tos_accepted=True)
    assert first is not None
    assert second is None


def test_admin_email_auto_flagged(db):
    # ADMIN_EMAIL is patched to admin-test@example.com in the fixture.
    uid = db.create_user("admin-test@example.com", "AdminPass!123", tos_accepted=True)
    assert uid
    user = db.get_user_by_id(uid)
    assert user and user.get("is_admin") == 1 or user.get("is_admin") is True


def test_non_admin_email_not_admin(db):
    uid = db.create_user("regular@example.com", "StrongPass!123", tos_accepted=True)
    user = db.get_user_by_id(uid)
    assert not user.get("is_admin")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_authenticate_success(db):
    db.create_user("auth@example.com", "StrongPass!123", tos_accepted=True)
    result = db.authenticate("auth@example.com", "StrongPass!123")
    assert result is not None
    assert result["email"] == "auth@example.com"


def test_authenticate_wrong_password(db):
    db.create_user("wrong@example.com", "StrongPass!123", tos_accepted=True)
    assert db.authenticate("wrong@example.com", "NopeNotIt!") is None


def test_authenticate_nonexistent(db):
    # Should not leak presence — returns None either way, and still exercises
    # the fake-verify path for timing safety.
    assert db.authenticate("ghost@example.com", "whatever") is None


def test_authenticate_inactive_user(db):
    uid = db.create_user("inactive@example.com", "StrongPass!123", tos_accepted=True)
    # Directly mark inactive
    conn = db._get_conn()
    conn.cursor().execute("UPDATE users SET is_active = 0 WHERE id = ?", (uid,))
    conn.commit()
    assert db.authenticate("inactive@example.com", "StrongPass!123") is None


def test_authenticate_case_insensitive_email(db):
    db.create_user("Mixed@Example.com", "StrongPass!123", tos_accepted=True)
    assert db.authenticate("mixed@example.com", "StrongPass!123") is not None
    assert db.authenticate("MIXED@EXAMPLE.COM", "StrongPass!123") is not None


# ---------------------------------------------------------------------------
# Legacy hash upgrade (pbkdf2 → argon2 on successful login)
# ---------------------------------------------------------------------------

def test_legacy_pbkdf2_still_authenticates_and_upgrades(db):
    """User whose password was hashed with the legacy pbkdf2 scheme should
    (a) still log in with the correct password and
    (b) get their hash transparently rewritten to argon2 afterwards."""
    from werkzeug.security import generate_password_hash
    # Seed a user with a pbkdf2 hash directly
    legacy_hash = generate_password_hash("LegacyPass!123", method="pbkdf2:sha256")
    conn = db._get_conn()
    conn.cursor().execute(
        "INSERT INTO users (email, password_hash, display_name, is_admin) VALUES (?, ?, ?, 0)",
        ("legacy@example.com", legacy_hash, "Legacy"),
    )
    conn.cursor().execute(
        "INSERT INTO user_settings (user_id, watchlist_data, preferences) "
        "SELECT id, '{}', '{}' FROM users WHERE email = ?",
        ("legacy@example.com",),
    )
    conn.commit()

    # Authenticate succeeds
    assert db.authenticate("legacy@example.com", "LegacyPass!123") is not None

    # Hash should now be argon2 (starts with $argon2)
    cur = db._get_conn().cursor()
    cur.execute("SELECT password_hash FROM users WHERE email = ?", ("legacy@example.com",))
    row = cur.fetchone()
    assert row["password_hash"].startswith("$argon2"), "legacy hash should be upgraded to argon2"


# ---------------------------------------------------------------------------
# Password update
# ---------------------------------------------------------------------------

def test_update_password_allows_new_password(db):
    uid = db.create_user("pw@example.com", "OldPass!123", tos_accepted=True)
    assert db.update_password(uid, "NewStrongPass!456") is True
    assert db.authenticate("pw@example.com", "OldPass!123") is None
    assert db.authenticate("pw@example.com", "NewStrongPass!456") is not None


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

def test_email_verified_default_false(db):
    uid = db.create_user("verify@example.com", "StrongPass!123", tos_accepted=True)
    # Non-admin users are not auto-verified; admin is.
    assert db.is_email_verified(uid) in (False, 0)


def test_email_verified_can_be_set(db):
    uid = db.create_user("verify2@example.com", "StrongPass!123", tos_accepted=True)
    # Note: set_email_verified's return value currently reflects the
    # email_verifications.rowcount (whether any pending tokens were cleared),
    # not whether the users.email_verified flag flipped. So assert against
    # the observable post-condition, not the return value.
    db.set_email_verified(uid)
    assert bool(db.is_email_verified(uid)) is True


# ---------------------------------------------------------------------------
# Password reset tokens
# ---------------------------------------------------------------------------

def test_password_reset_token_roundtrip(db):
    import hashlib
    uid = db.create_user("reset@example.com", "StrongPass!123", tos_accepted=True)
    raw = "token-" + os.urandom(8).hex()
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    assert db.create_password_reset_token(uid, token_hash, ip_address="127.0.0.1") is True
    # A fresh token validates and resolves to the user.
    found = db.validate_password_reset_token(token_hash)
    assert found is not None and found.get("user_id") == uid


def test_password_reset_token_is_single_use(db):
    """A consumed reset token can never be validated again."""
    import hashlib
    uid = db.create_user("single-use@example.com", "StrongPass!123", tos_accepted=True)
    token_hash = hashlib.sha256(os.urandom(16)).hexdigest()
    assert db.create_password_reset_token(uid, token_hash) is True

    # First validate + use succeeds.
    assert db.validate_password_reset_token(token_hash) is not None
    assert db.use_password_reset_token(token_hash) is True

    # After use, the same token must no longer validate (single-use).
    assert db.validate_password_reset_token(token_hash) is None
    # Re-using an already-used token reports no row updated.
    assert db.use_password_reset_token(token_hash) is False


def test_password_reset_token_expires(db):
    """An expired reset token is rejected even if unused."""
    import hashlib
    from datetime import datetime, timedelta, timezone
    from user_db import adapt_query

    uid = db.create_user("expired-token@example.com", "StrongPass!123", tos_accepted=True)
    token_hash = hashlib.sha256(os.urandom(16)).hexdigest()
    assert db.create_password_reset_token(uid, token_hash) is True

    # Force the stored token's expiry into the past.
    conn = db._get_conn()
    cur = conn.cursor()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    cur.execute(
        adapt_query("UPDATE password_reset_tokens SET expires_at = ? WHERE token_hash = ?"),
        (past, token_hash),
    )
    conn.commit()

    # Unused but expired → rejected.
    assert db.validate_password_reset_token(token_hash) is None


def test_password_reset_new_token_invalidates_prior(db):
    """Issuing a new reset token retires the previous one."""
    import hashlib
    uid = db.create_user("reissue@example.com", "StrongPass!123", tos_accepted=True)
    first = hashlib.sha256(os.urandom(16)).hexdigest()
    second = hashlib.sha256(os.urandom(16)).hexdigest()
    assert db.create_password_reset_token(uid, first) is True
    assert db.create_password_reset_token(uid, second) is True

    # The first token is retired the moment a second is minted.
    assert db.validate_password_reset_token(first) is None
    assert db.validate_password_reset_token(second) is not None


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def test_api_key_create_and_verify(db):
    import hashlib
    uid = db.create_user("apikey@example.com", "StrongPass!123", tos_accepted=True)
    raw_token = "cvx_k_" + os.urandom(16).hex()
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    assert db.create_api_key(uid, token_hash, token_suffix=raw_token[-4:], name="test")
    info = db.verify_api_key_token(token_hash)
    assert info is not None
    assert info.get("user_id") == uid


def test_api_key_wrong_hash_rejected(db):
    assert db.verify_api_key_token("a" * 64) is None


def test_api_key_internal_flag_defaults_false(db):
    import hashlib
    uid = db.create_user("apikey-default@example.com", "StrongPass!123", tos_accepted=True)
    raw_token = "cvx_k_" + os.urandom(16).hex()
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    assert db.create_api_key(uid, token_hash, token_suffix=raw_token[-4:], name="test")
    info = db.verify_api_key_token(token_hash)
    assert info is not None
    assert info.get("is_internal") is False


def test_api_key_internal_flag_true_when_created_internal(db):
    import hashlib
    uid = db.create_user("apikey-internal@example.com", "StrongPass!123", tos_accepted=True)
    raw_token = "cvx_k_" + os.urandom(16).hex()
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    assert db.create_api_key(
        uid, token_hash, token_suffix=raw_token[-4:], name="agent", is_internal=True
    )
    info = db.verify_api_key_token(token_hash)
    assert info is not None
    assert info.get("is_internal") is True


# ---------------------------------------------------------------------------
# Watchlist / preferences
# ---------------------------------------------------------------------------

def test_watchlist_roundtrip(db):
    uid = db.create_user("wl@example.com", "StrongPass!123", tos_accepted=True)
    symbols = {"favorites": ["BTC", "ETH", "SPX"], "pinned": []}
    db.save_watchlist(uid, symbols)
    got = db.get_watchlist(uid)
    assert got.get("favorites") == ["BTC", "ETH", "SPX"]


def test_preferences_roundtrip(db):
    uid = db.create_user("prefs@example.com", "StrongPass!123", tos_accepted=True)
    db.save_preferences(uid, {"theme": "dark"})
    got = db.get_preferences(uid)
    assert got.get("theme") == "dark"


# ---------------------------------------------------------------------------
# Anonymise (GDPR)
# ---------------------------------------------------------------------------

def test_anonymize_user_blocks_login(db):
    uid = db.create_user("gdpr@example.com", "StrongPass!123", tos_accepted=True)
    assert db.anonymize_user(uid) is True
    # After anonymise, email is scrambled / inactive → login fails.
    assert db.authenticate("gdpr@example.com", "StrongPass!123") is None


def test_anonymize_user_removes_api_keys(db):
    import hashlib
    uid = db.create_user("gdprkey@example.com", "StrongPass!123", tos_accepted=True)
    raw_token = "cvx_k_" + os.urandom(16).hex()
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    db.create_api_key(uid, token_hash, token_suffix=raw_token[-4:], name="x")
    assert db.verify_api_key_token(token_hash) is not None
    db.anonymize_user(uid)
    assert db.verify_api_key_token(token_hash) is None


def _conn_that_fails_on(real_get_conn, needle, exc):
    """Wrap the real connection so one DELETE raises. Stubs the unit's DEPENDENCY
    (its DB handle), not the unit — anonymize_user itself runs unmodified."""
    class _Cursor:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            if needle in sql:
                raise exc
            return (self._inner.execute(sql, params) if params is not None
                    else self._inner.execute(sql))

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _Conn:
        def __init__(self, inner):
            self._inner = inner

        def cursor(self):
            return _Cursor(self._inner.cursor())

        def __getattr__(self, name):
            return getattr(self._inner, name)

    return lambda: _Conn(real_get_conn())


def test_anonymize_user_reports_a_failed_delete_instead_of_claiming_success(db, monkeypatch):
    """Every DELETE in anonymize_user was wrapped in
    `except Exception: pass`, so a partial erasure still returned True and
    logged "all personal data deleted". The caller
    (carrac/auth/routes.py::delete_account) turns False into a 500 the user actually
    sees — it was simply never given one. Silence on a legally-binding path
    is the swallowed-exception shape.
    """
    uid = db.create_user("gdprfail@example.com", "StrongPass!123", tos_accepted=True)
    monkeypatch.setattr(
        db, '_get_conn',
        _conn_that_fails_on(db._get_conn, 'DELETE FROM api_keys',
                            RuntimeError('disk I/O error')),
    )
    assert db.anonymize_user(uid) is False, (
        "a failed DELETE must not be reported as a successful erasure"
    )


def test_anonymize_user_still_tolerates_a_genuinely_absent_table(db, monkeypatch):
    """The control, and the reason the blanket except existed in the first
    place. Several tables in tables_to_clear are created by other modules
    against their own SQLite file, so they legitimately do not exist for this
    fixture. A missing table is not a failed erasure — there is nothing there
    to erase — and treating it as one would turn every existing anonymise test
    red and break real dev/test flows.
    """
    uid = db.create_user("gdprmissing@example.com", "StrongPass!123", tos_accepted=True)
    monkeypatch.setattr(
        db, '_get_conn',
        _conn_that_fails_on(db._get_conn, 'DELETE FROM api_keys',
                            RuntimeError('no such table: api_keys')),
    )
    assert db.anonymize_user(uid) is True


def test_delete_from_waitlist_clears_both_stores(db, tmp_path, monkeypatch):
    """`waitlist` holds an email + IP and has no user_id, so
    anonymize_user could not reach it even in principle and no delete route
    existed anywhere. The same address is mirrored to
    data/private/beta_waitlist.jsonl, which nothing pruned or erased either.
    Clearing one and leaving the other is indistinguishable from doing nothing.
    """
    import json as _json
    monkeypatch.setenv('DATA_DIR', str(tmp_path))

    db.add_to_waitlist('subject@example.com', source='test', ip='1.2.3.4')
    db.add_to_waitlist('bystander@example.com', source='test', ip='5.6.7.8')

    path = tmp_path / 'private' / 'beta_waitlist.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _json.dumps({'ts': 'x', 'email': 'subject@example.com', 'ip': '1.2.3.4'}) + '\n'
        + _json.dumps({'ts': 'y', 'email': 'bystander@example.com', 'ip': '5.6.7.8'}) + '\n'
    )

    result = db.delete_from_waitlist('subject@example.com')
    assert result == {'rows': 1, 'file_entries': 1}

    emails = [r['email'] for r in db.get_waitlist()]
    assert 'subject@example.com' not in emails
    assert 'bystander@example.com' in emails, "erasure took out an unrelated subject"

    remaining = path.read_text()
    assert 'subject@example.com' not in remaining
    assert 'bystander@example.com' in remaining, "the file rewrite dropped a bystander"


def test_account_deletion_also_clears_the_waitlist_trace(db, tmp_path, monkeypatch):
    """The live caller. An erasure route with nothing calling it is the most
    common defect in this codebase — so the common real case is wired up:
    someone joins the waitlist, later signs up, then deletes their account.
    Their address is in three places and all three must go.
    """
    import json as _json
    monkeypatch.setenv('DATA_DIR', str(tmp_path))

    email = 'joined-then-signed-up@example.com'
    db.add_to_waitlist(email, source='test', ip='1.2.3.4')
    path = tmp_path / 'private' / 'beta_waitlist.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({'ts': 'x', 'email': email, 'ip': '1.2.3.4'}) + '\n')

    uid = db.create_user(email, "StrongPass!123", tos_accepted=True)
    assert db.anonymize_user(uid) is True

    assert email not in [r['email'] for r in db.get_waitlist()], (
        "account deletion left the waitlist row behind"
    )
    assert email not in path.read_text(), (
        "account deletion left the JSONL mirror behind"
    )


def test_anonymize_user_clears_newly_added_gdpr_tables(db):
    """anonymize_user only cleared a short hardcoded table list.
    Because it UPDATEs (not DELETEs) the users row, ON DELETE CASCADE never
    fires, so any user_id-bearing table NOT in that list kept personal data
    after "erasure". This exercises the tables that were added to the
    list AND that live in user_db.py's own SQLite schema (so they share this
    fixture's connection/db file): satisfaction_ratings, user_tier_overrides,
    custom_formulas, strategies, context_recipes.

    NOT seeded here (noted, not a gap in the fix — just untestable from this
    fixture): plays, play_shares, backtest_runs, api_usage_daily,
    personal_report_sections, score_snapshots, journal_goals. Those tables
    are created by OTHER modules against their OWN SQLite connection in
    dev/test (carrac.db_pool's shared carrac.db for plays/play_shares/
    backtest_runs; carrac/content_db.py's drawings.db for
    personal_report_sections/score_snapshots/journal_goals; api_usage_daily
    has no SQLite-side creation at all, Postgres/alembic only) — a
    physically different file from this fixture's tmp users.db, so rows
    seeded there wouldn't even be reachable by db.anonymize_user's cursor.
    In production Postgres all tables live in one database, so the same
    code path does reach and clear them there.

    Also covers the age_attested_ip/age_attested_at IP-retention gap.
    """
    uid = db.create_user("gdpr-multitable@example.com", "StrongPass!123", tos_accepted=True)
    assert uid

    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO satisfaction_ratings (user_id, rating, comment) VALUES (?, ?, ?)",
        (uid, 5, "great app"),
    )
    cur.execute(
        "INSERT INTO user_tier_overrides (user_id, override_json, reason) VALUES (?, ?, ?)",
        (uid, '{"max_plays": 999}', "manual bump"),
    )
    cur.execute(
        "INSERT INTO custom_formulas (id, user_id, name, formula) VALUES (?, ?, ?, ?)",
        ("cf-1", uid, "My Formula", "close - open"),
    )
    cur.execute(
        "INSERT INTO strategies (id, user_id, name, entry_formula, exit_formula) VALUES (?, ?, ?, ?, ?)",
        ("strat-1", uid, "My Strategy", "rsi < 30", "rsi > 70"),
    )
    cur.execute(
        "INSERT INTO context_recipes (user_id, name, blocks) VALUES (?, ?, ?)",
        (uid, "My Recipe", "[]"),
    )
    cur.execute(
        "UPDATE users SET age_attested_ip = ?, age_attested_at = ? WHERE id = ?",
        ("203.0.113.9", "2026-01-01T00:00:00", uid),
    )
    conn.commit()

    seeded_tables = (
        "satisfaction_ratings", "user_tier_overrides", "custom_formulas",
        "strategies", "context_recipes",
    )
    for table in seeded_tables:
        cur.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (uid,))
        assert cur.fetchone()["c"] == 1, f"seed failed for {table}"

    result = db.anonymize_user(uid)
    assert result is True

    for table in seeded_tables:
        cur.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (uid,))
        assert cur.fetchone()["c"] == 0, f"{table} still has rows after anonymize_user"

    cur.execute(
        "SELECT is_active, email, age_attested_ip, age_attested_at FROM users WHERE id = ?",
        (uid,),
    )
    row = cur.fetchone()
    assert row["is_active"] in (0, False)
    assert row["email"] == f"deleted-{uid}@deleted.carrac.cc"
    assert row["age_attested_ip"] is None
    assert row["age_attested_at"] is None


def test_anonymize_user_clears_exchange_credentials_and_monthly_goals(db):
    """Encrypted exchange credentials must not survive account deletion.

    `exchange_connections` must be in anonymize_user's table list, otherwise a
    deleted account keeps its encrypted API key and Hyperliquid wallet
    address. `monthly_goals` sits alongside its sibling `journal_goals` and
    must be cleared too.

    These tables are created by other modules against their own connections in
    dev/test, so they are CREATED HERE on the fixture's connection on purpose:
    without a real table the DELETE hits user_db's `except Exception: pass`
    SQLite branch and the assertion below would pass while erasing nothing.
    The pre-anonymise count is the control that keeps this honest.
    """
    uid = db.create_user("gdpr-exchange@example.com", "StrongPass!123", tos_accepted=True)
    assert uid

    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS exchange_connections (
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, exchange TEXT,
        credentials_encrypted TEXT NOT NULL, wallet_address TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS exchange_import_suggestions (
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, payload TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS monthly_goals (
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, month TEXT, target TEXT)""")

    secret = "gAAAAAB-encrypted-api-key-blob"
    cur.execute(
        "INSERT INTO exchange_connections (user_id, exchange, credentials_encrypted, wallet_address)"
        " VALUES (?, ?, ?, ?)",
        (uid, "hyperliquid", secret, "0xdeadbeef"),
    )
    cur.execute(
        "INSERT INTO exchange_import_suggestions (user_id, payload) VALUES (?, ?)",
        (uid, '{"symbol": "BTC"}'),
    )
    cur.execute(
        "INSERT INTO monthly_goals (user_id, month, target) VALUES (?, ?, ?)",
        (uid, "2026-08", "R > 2"),
    )
    conn.commit()

    seeded = ("exchange_connections", "exchange_import_suggestions", "monthly_goals")
    for table in seeded:
        cur.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (uid,))
        assert cur.fetchone()["c"] == 1, f"seed failed for {table} — test would be vacuous"

    assert db.anonymize_user(uid) is True

    for table in seeded:
        cur.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (uid,))
        assert cur.fetchone()["c"] == 0, f"{table} still has rows after anonymize_user"

    # The credential-leak case, as opposed to mere tidiness: the encrypted blob must be gone from the database entirely.
    cur.execute("SELECT COUNT(*) AS c FROM exchange_connections WHERE credentials_encrypted = ?",
                (secret,))
    assert cur.fetchone()["c"] == 0, "encrypted exchange credentials survived erasure"


def test_export_records_failed_ai_sections_as_incomplete(db):
    """A failed read of the AI usage or credits section must be listed in
    `_incomplete`, not silently exported as an empty section."""
    uid = db.create_user("export-ai-fail@example.com", "StrongPass!123",
                         tos_accepted=True)
    conn = db._get_conn()
    cur = conn.cursor()
    cur.execute("DROP TABLE ai_usage_log")
    cur.execute("DROP TABLE ai_credits")
    conn.commit()

    export = db.export_user_data(uid)
    failed = set(export.get('_incomplete', {}).get('tables_that_failed', []))
    assert {'ai_usage', 'ai_credits'} <= failed


def test_export_user_data_includes_the_content_tables(db):
    """The export must actually RETURN the rows, not just name
    the tables.

    tests/test_export_coverage.py parses source and so proves only that a table
    is mentioned. That is exactly the guard that can pass while exporting
    nothing: several of these tables are created by OTHER modules against their
    own SQLite file in dev/test (carrac.db_pool's carrac.db for plays/
    play_shares/backtest_runs; content_db.py's drawings.db for
    personal_report_sections/score_snapshots), so they are simply absent from
    this fixture's connection. `export_user_data` swallows a missing table into
    an empty list, which reads identically to "the user had none".

    So this creates them ON THE FIXTURE CONNECTION, seeds a row each, and
    asserts a NON-ZERO pre-count before exporting. Without that pre-count
    assertion the whole test would pass vacuously against zero rows — which is
    a failure mode that was explicitly warned about.
    """
    uid = db.create_user("export-coverage@example.com", "StrongPass!123",
                         tos_accepted=True)
    assert uid

    conn = db._get_conn()
    cur = conn.cursor()

    # Minimal stand-ins for the tables this fixture cannot see. Column sets are
    # deliberately small — the export does SELECT *, so shape does not matter,
    # only that rows keyed to user_id exist and come back.
    seeded = {
        'plays': "CREATE TABLE IF NOT EXISTS plays (id TEXT PRIMARY KEY, user_id INTEGER, title TEXT)",
        'play_shares': "CREATE TABLE IF NOT EXISTS play_shares (id TEXT PRIMARY KEY, user_id INTEGER, play_id TEXT)",
        'backtest_runs': "CREATE TABLE IF NOT EXISTS backtest_runs (id TEXT PRIMARY KEY, user_id INTEGER, name TEXT)",
        'score_snapshots': "CREATE TABLE IF NOT EXISTS score_snapshots (id TEXT PRIMARY KEY, user_id INTEGER, score INTEGER)",
        'monthly_goals': "CREATE TABLE IF NOT EXISTS monthly_goals (id TEXT PRIMARY KEY, user_id INTEGER, goal TEXT)",
        'journal_screenshots': "CREATE TABLE IF NOT EXISTS journal_screenshots (id TEXT PRIMARY KEY, user_id INTEGER, path TEXT)",
        'personal_report_sections': "CREATE TABLE IF NOT EXISTS personal_report_sections (id TEXT PRIMARY KEY, user_id INTEGER, body TEXT)",
        'exchange_import_suggestions': "CREATE TABLE IF NOT EXISTS exchange_import_suggestions (id TEXT PRIMARY KEY, user_id INTEGER, symbol TEXT)",
    }
    for table, ddl in seeded.items():
        cur.execute(ddl)
        cur.execute(f"INSERT INTO {table} (id, user_id) VALUES (?, ?)",
                    (f"{table}-1", uid))

    # Tables that DO live in user_db's own schema.
    cur.execute("INSERT INTO strategies (id, user_id, name, entry_formula, exit_formula) "
                "VALUES (?, ?, ?, ?, ?)",
                ("exp-strat", uid, "S", "rsi < 30", "rsi > 70"))
    cur.execute("INSERT INTO context_recipes (user_id, name, blocks) VALUES (?, ?, ?)",
                (uid, "R", "[]"))
    cur.execute("INSERT INTO satisfaction_ratings (user_id, rating, comment) VALUES (?, ?, ?)",
                (uid, 4, "fine"))

    # exchange_connections carries the credential blob — seed it WITH a secret
    # so the assertion below is meaningful rather than vacuous.
    cur.execute(
        "CREATE TABLE IF NOT EXISTS exchange_connections ("
        "id TEXT PRIMARY KEY, user_id INTEGER, exchange TEXT, label TEXT, "
        "credentials_encrypted TEXT, masked_identifier TEXT, status TEXT, "
        "consecutive_errors INTEGER, last_error TEXT, last_sync_at TIMESTAMP, "
        "last_sync_trades_imported INTEGER, cursor_data TEXT, "
        "created_at TIMESTAMP, updated_at TIMESTAMP)")
    cur.execute(
        "INSERT INTO exchange_connections "
        "(id, user_id, exchange, label, credentials_encrypted, masked_identifier, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("exp-conn", uid, "binance", "Main", "SUPER-SECRET-BLOB", "ab***yz", "active"))
    conn.commit()

    checked = list(seeded) + ['strategies', 'context_recipes',
                              'satisfaction_ratings', 'exchange_connections']

    # THE PRE-COUNT. Without this the export assertions below could all pass
    # against zero seeded rows and prove nothing.
    for table in checked:
        n = cur.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?",
                        (uid,)).fetchone()[0]
        assert n > 0, f"seeding {table} did not take — the rest of this test would be vacuous"

    export = db.export_user_data(uid)

    for table in checked:
        assert table in export, f"{table} missing from the export entirely"
        assert export[table], (
            f"{table} exported as empty despite {table} having rows — a user "
            "exercising right of access would silently not receive it"
        )

    # Sections this fixture genuinely cannot provide (drawings, journal_*, ...
    # live in other SQLite files in dev) ARE expected to be reported as failed
    # here — that reporting is the point. What must NOT appear is anything we
    # seeded above: if a table we proved has rows lands in this list, the export
    # is failing on data it can actually see.
    failed = set(export.get('_incomplete', {}).get('tables_that_failed', []))
    assert not (failed & set(checked)), (
        f"tables with seeded rows were reported as failed: {sorted(failed & set(checked))}"
    )

    # The one table that is both exportable and dangerous.
    conn_rows = export['exchange_connections']
    assert conn_rows[0].get('masked_identifier') == "ab***yz"
    blob = json.dumps(export)
    assert 'SUPER-SECRET-BLOB' not in blob, (
        "the encrypted credential blob was written into the user's data export"
    )
    assert 'credentials_encrypted' not in conn_rows[0]


def test_anonymize_user_reports_a_failed_email_preread_instead_of_skipping_waitlist(db, monkeypatch):
    """The email pre-read feeds
    the waitlist scrub, which is keyed by ADDRESS, not user_id. When the
    pre-read threw, original_email stayed '' — the waitlist pass was skipped
    with nothing recorded, and anonymize_user still returned True: a silent
    partial erasure on the one path with legal weight."""
    uid = db.create_user("gdprpreread@example.com", "StrongPass!123", tos_accepted=True)

    def _boom(_uid):
        raise RuntimeError('transient DB blip during email pre-read')
    monkeypatch.setattr(db, 'get_user_by_id', _boom)

    assert db.anonymize_user(uid) is False, (
        "a failed email pre-read leaves the waitlist un-scrubbed — reporting "
        "True tells the user their data is gone when some of it is not"
    )


def test_anonymize_user_with_readable_empty_email_is_not_a_failure(db, monkeypatch):
    """The ALLOWED branch: a pre-read that SUCCEEDS and finds no address has
    nothing to scrub — that must stay a successful erasure, or every legacy
    row without an email would 500 on deletion forever."""
    uid = db.create_user("gdprnoemail@example.com", "StrongPass!123", tos_accepted=True)
    monkeypatch.setattr(db, 'get_user_by_id', lambda _uid: {'email': ''})
    assert db.anonymize_user(uid) is True


def test_set_email_verified_reports_success_without_pending_tokens(tmp_path, monkeypatch):
    """True when the user row was updated, even with no verification token to retire."""
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    from user_db import UserDatabase
    db = UserDatabase(str(tmp_path / 'users.db'))
    uid = db.create_user('verify@example.com', 'Verify', 'Str0ng!Passw0rd#x')
    assert db.set_email_verified(uid) is True
    assert db.is_email_verified(uid) is True
    assert db.set_email_verified(999999) is False
