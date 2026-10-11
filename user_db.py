"""
User Database Module - Multi-user support with invite-only registration

Tables:
- users: User accounts with password hashes
- invites: Email whitelist and invite codes
- user_settings: Per-user preferences (watchlists, alerts, etc.)

Security:
- WAL mode for better concurrency (SQLite)
- Timing-safe password comparison
- Proper connection handling
- Supports PostgreSQL and SQLite backends
"""

import sqlite3
import secrets
import string
import uuid
import os
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Optional, Dict, List
from werkzeug.security import check_password_hash
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# Python 3.12 deprecated sqlite3's default datetime adapter. Register
# explicit ISO-format adapters for datetime + date so callers can keep
# passing datetime objects without triggering DeprecationWarnings (and
# without behaviour change in stored values — ISO-8601 is what the old
# adapter produced).
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())
sqlite3.register_adapter(date, lambda d: d.isoformat())

_ph = PasswordHasher()
import threading
import logging
import json
from contextlib import contextmanager

from security import add_timing_noise
from carrac.db_pool import get_db_type, adapt_query, returning_id, get_last_id, handle_integrity_error
from carrac.site_url import support_email

logger = logging.getLogger('carrac.user_db')

# Admin email from environment variable (fallback to default for backwards compatibility)
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '')

SUBSCRIPTION_GRACE_DAYS = 3  # Paddle dunning window before hard downgrade


API_KEY_LAST_USED_THROTTLE = timedelta(hours=1)


def _last_used_stamp_sql(key_id):
    """(sql, params) that advance ``api_keys.last_used_at`` only when the
    current stamp is NULL or older than the throttle window — one statement,
    no read-before-write on the auth hot path."""
    now = datetime.now()
    return (
        adapt_query('UPDATE api_keys SET last_used_at = ? '
                    'WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)'),
        (now, key_id, now - API_KEY_LAST_USED_THROTTLE),
    )


class PreferencesWriteError(RuntimeError):
    """A user's preferences could not be stored."""


class UserDatabase:
    """User management with invite system. Supports PostgreSQL and SQLite."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.path.join(os.environ.get('DATA_DIR', 'data'), 'users.db')
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_database()

    def _release_pg_conn(self):
        """Return a stale PostgreSQL connection to the pool."""
        if hasattr(self._local, 'pg_conn') and self._local.pg_conn is not None:
            try:
                from carrac.db_pool import _init_pg_pool
                pool = _init_pg_pool()
                pool.putconn(self._local.pg_conn, close=True)
            except Exception:
                pass
            self._local.pg_conn = None
            self._local.pg_wrapped = None

    def _get_conn(self):
        """Get database connection (thread-local for both backends)."""
        if get_db_type() == 'postgresql':
            from carrac.db_pool import _init_pg_pool, PgConnWrapper
            need_new = (not hasattr(self._local, 'pg_conn') or
                        self._local.pg_conn is None or self._local.pg_conn.closed)
            if need_new:
                # Release stale connection back to pool before acquiring new one
                self._release_pg_conn()
                pool = _init_pg_pool()
                raw = pool.getconn()
                import psycopg2.extras
                raw.autocommit = False
                raw.cursor_factory = psycopg2.extras.RealDictCursor
                self._local.pg_conn = raw
                self._local.pg_wrapped = PgConnWrapper(raw)
            # Methods here end their transactions through _tx(), but code
            # outside this class also borrows the connection. An aborted
            # transaction (INERROR) rejects every statement until it is rolled
            # back, so clear it here rather than fail the next caller. An open
            # one (INTRANS) is left alone: it may be a caller's unit of work.
            from psycopg2.extensions import (TRANSACTION_STATUS_INERROR,
                                             TRANSACTION_STATUS_UNKNOWN)
            tx_status = self._local.pg_conn.info.transaction_status
            if tx_status in (TRANSACTION_STATUS_INERROR, TRANSACTION_STATUS_UNKNOWN):
                self._local.pg_conn.rollback()
            return self._local.pg_wrapped
        else:
            # Original SQLite path
            if not hasattr(self._local, 'conn') or self._local.conn is None:
                self._local.conn = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False,
                    timeout=30.0  # Wait up to 30s for locks
                )
                self._local.conn.row_factory = sqlite3.Row
                # Enable WAL mode for better concurrency
                self._local.conn.execute('PRAGMA journal_mode=WAL')
                self._local.conn.execute('PRAGMA synchronous=NORMAL')
                self._local.conn.execute('PRAGMA busy_timeout=30000')
            return self._local.conn

    @contextmanager
    def _tx(self, commit: bool = True):
        """Run a block as one transaction on this thread's connection.

        Commits when the block finishes and rolls back when it raises, then
        lets the exception through. Reads go through here too: on Postgres a
        SELECT opens a transaction like any other statement, and one left
        open, or aborted by a failed statement, would follow the connection
        into the next call made on this thread.

        With ``commit=False``, or inside ``process_paddle_event_atomic``, the
        block joins the transaction that is already open and neither commits
        nor rolls back: the owner of that transaction decides.
        """
        conn = self._get_conn()
        if not commit or getattr(self._local, 'atomic', False):
            yield conn
            return
        try:
            yield conn
            conn.commit()
        except BaseException:
            self._rollback(conn)
            raise

    def transaction(self):
        """The public form of ``_tx()`` for modules that keep their own tables
        in this database (see carrac/alerts/store.py)."""
        return self._tx()

    def _rollback(self, conn):
        """Roll back after a failed block. A connection that cannot roll back
        is broken; it is dropped so the next call opens a fresh one."""
        try:
            conn.rollback()
        except Exception as e:
            logger.warning("Rollback failed, dropping the connection: %s", e)
            if get_db_type() == 'postgresql':
                self._release_pg_conn()
            else:
                self._local.conn = None

    def _init_database(self):
        """Initialize database schema."""
        if get_db_type() == 'postgresql':
            # PostgreSQL schema is created by scripts/init_postgres.pgsql
            # (the alembic baseline) and the migrations after it.
            logger.info("PostgreSQL mode — schema managed externally")
            self._fix_pg_sequences()
            return
        self._init_sqlite_schema()

    def _fix_pg_sequences(self):
        """Fix PostgreSQL sequences that can desync after backup restores.

        If a sequence's nextval is <= MAX(id), INSERTs fail with duplicate PK.
        This runs once at startup and fixes any desynced sequences. Each table
        gets its own transaction: on Postgres a failed statement aborts the
        whole transaction, which would silently undo the fixes made before it.
        """
        for table in ('users', 'user_settings', 'invites'):
            seq = f'{table}_id_seq'
            try:
                with self._tx() as conn:
                    cur = conn.cursor()
                    cur.execute(f'SELECT MAX(id) FROM {table}')
                    max_id = cur.fetchone()['max'] or 0
                    cur.execute(f'SELECT last_value, is_called FROM {seq}')
                    row = cur.fetchone()
                    current = row['last_value'] if row['is_called'] else row['last_value'] - 1
                    if current < max_id:
                        cur.execute(f'ALTER SEQUENCE {seq} RESTART WITH %s', (max_id + 1,))
                        logger.warning("Fixed desynced sequence %s: was %d, table max %d, reset to %d",
                                       seq, current, max_id, max_id + 1)
            except Exception as e:
                logger.warning("Sequence check failed for %s (non-fatal): %s", seq, e)

    def _init_sqlite_schema(self):
        """Initialize SQLite schema (development/fallback)."""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                is_admin BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                invited_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP,
                FOREIGN KEY (invited_by) REFERENCES users(id)
            )
        ''')

        # Invites table (for both email whitelist and codes)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('email', 'code')),
                value TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                used_by INTEGER,
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (created_by) REFERENCES users(id),
                FOREIGN KEY (used_by) REFERENCES users(id)
            )
        ''')

        # Beta waitlist — deduped capture, status-tracked. Mirrors the
        # Postgres table in alembic/versions/20260529_waitlist.py.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS waitlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'waiting'
                    CHECK(status IN ('waiting', 'invited', 'registered')),
                source TEXT,
                cohort TEXT,
                invite_code TEXT,
                ip TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                invited_at TIMESTAMP,
                registered_at TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_waitlist_status ON waitlist(status, created_at)')

        # User settings table (watchlists, preferences)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                watchlist_data TEXT DEFAULT '{}',
                preferences TEXT DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')

        # Create index for faster lookups
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_invites_value ON invites(value)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)')

        # Migration: add tier column if missing
        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'tier' not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN tier TEXT NOT NULL DEFAULT 'free'")
            logger.info("Migrated users table: added 'tier' column")

        _ALLOWED_USER_COLUMNS = {'subscription_expires_at', 'subscription_payment_id', 'last_active', 'paddle_customer_id', 'paddle_subscription_id', 'flags', 'age_attested_at', 'age_attested_ip'}
        for col_name, col_type in [('subscription_expires_at', 'TIMESTAMP'), ('subscription_payment_id', 'TEXT'), ('last_active', 'TIMESTAMP'), ('paddle_customer_id', 'TEXT'), ('paddle_subscription_id', 'TEXT'), ('flags', "TEXT NOT NULL DEFAULT '{}'"), ('age_attested_at', 'TIMESTAMP'), ('age_attested_ip', 'TEXT')]:
            if col_name not in columns:
                if col_name not in _ALLOWED_USER_COLUMNS:
                    raise ValueError(f"Unexpected migration column: {col_name}")
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                    logger.info(f"Migrated users table: added '{col_name}' column")
                except sqlite3.OperationalError:
                    pass  # Column already added by another worker

        # AI credit system tables
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_credits (
                user_id INTEGER PRIMARY KEY,
                balance_cents INTEGER NOT NULL DEFAULT 0,
                total_deposited_cents INTEGER NOT NULL DEFAULT 0,
                total_spent_cents INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                feature TEXT NOT NULL,
                asset TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_cents INTEGER DEFAULT 0,
                charged_cents INTEGER DEFAULT 0,
                model TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                prompt_sha256 TEXT,
                response_sha256 TEXT,
                conversation_id TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_usage_user ON ai_usage_log(user_id, created_at)')
        usage_columns = {r[1] for r in cursor.execute('PRAGMA table_info(ai_usage_log)').fetchall()}
        for col in ('prompt_sha256', 'response_sha256', 'conversation_id'):
            if col not in usage_columns:
                cursor.execute(f"ALTER TABLE ai_usage_log ADD COLUMN {col} TEXT")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                crypto_amount TEXT,
                crypto_currency TEXT,
                tx_hash TEXT,
                nowpayments_payment_id TEXT,
                status TEXT DEFAULT 'confirmed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_payments (
                payment_id TEXT PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER
            )
        ''')

        # AI conversation history
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT DEFAULT 'New Chat',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_conv_user ON ai_conversations(user_id, updated_at DESC)')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                feature TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES ai_conversations(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_msg_conv ON ai_messages(conversation_id, created_at)')

        # Migration: add metadata columns to ai_conversations if missing
        cursor.execute("PRAGMA table_info(ai_conversations)")
        conv_cols = [row[1] for row in cursor.fetchall()]
        for col, typedef in [
            ('primary_asset', 'TEXT'),
            ('timeframe', 'TEXT'),
            ('page_context', 'TEXT'),
            ('summary', 'TEXT'),
            ('message_count', 'INTEGER DEFAULT 0'),
        ]:
            col_name = col.split()[0]
            if col_name not in conv_cols:
                cursor.execute(f"ALTER TABLE ai_conversations ADD COLUMN {col} {typedef}")
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ai_conv_cleanup ON ai_conversations(updated_at)')

        # Migration: add token_count to ai_messages if missing
        cursor.execute("PRAGMA table_info(ai_messages)")
        msg_cols = [row[1] for row in cursor.fetchall()]
        if 'token_count' not in msg_cols:
            cursor.execute("ALTER TABLE ai_messages ADD COLUMN token_count INTEGER")

        # Widget tokens — per-user, read-only, scoped to /api/widget/summary
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS widget_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                token_suffix TEXT NOT NULL,
                name TEXT DEFAULT 'Default',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP,
                revoked BOOLEAN DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_widget_tokens_hash ON widget_tokens(token_hash)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_widget_tokens_user ON widget_tokens(user_id)')

        # API keys — per-user, tier-inherited, for REST API and MCP access
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                token_suffix TEXT NOT NULL,
                name TEXT DEFAULT 'Default',
                scope TEXT DEFAULT 'full',
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP,
                revoked BOOLEAN DEFAULT 0,
                is_internal BOOLEAN DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(token_hash)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id)')

        # Feedback table (bug reports & feature requests)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_email TEXT,
                type TEXT NOT NULL CHECK(type IN ('bug', 'feature')),
                area TEXT,
                description TEXT NOT NULL,
                expected TEXT,
                actual TEXT,
                how_should_work TEXT,
                extra TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')

        # Satisfaction ratings (monthly survey)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS satisfaction_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_satisfaction_user ON satisfaction_ratings(user_id, created_at DESC)')

        # Migration: add email_verified column if missing.
        # The PRAGMA-then-ALTER pattern is racy under pytest-xdist where
        # parallel workers share the same SQLite file: each worker reads
        # the column list, sees the column missing, then they all try to
        # ALTER. One wins, the rest crash. Catch the duplicate-column
        # OperationalError to make the migration idempotent.
        cursor.execute("PRAGMA table_info(users)")
        user_columns = [row[1] for row in cursor.fetchall()]
        if 'email_verified' not in user_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT 0")
                cursor.execute("UPDATE users SET email_verified = 1 WHERE id > 0")
                logger.info("Migrated users table: added 'email_verified' column, grandfathered existing users")
            except sqlite3.OperationalError as e:
                if 'duplicate column' not in str(e).lower():
                    raise
        if 'deleted_at' not in user_columns:
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN deleted_at TIMESTAMP")
                logger.info("Migrated users table: added 'deleted_at' column")
            except sqlite3.OperationalError as e:
                if 'duplicate column' not in str(e).lower():
                    raise
        # TOS columns — Postgres has these in init_postgres.pgsql; mirror in SQLite
        # so create_user works in tests too.
        for tos_col, tos_type in [('tos_accepted_at', 'TIMESTAMP'),
                                   ('tos_accepted_ip', 'TEXT'),
                                   ('tos_accepted_ua', 'TEXT')]:
            if tos_col not in user_columns:
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {tos_col} {tos_type}")
                    logger.info(f"Migrated users table: added '{tos_col}' column")
                except sqlite3.OperationalError:
                    pass

        # Password reset tokens
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ip_address TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_prt_token_hash ON password_reset_tokens(token_hash)')

        # Email verifications
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS email_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ev_token_hash ON email_verifications(token_hash)')

        # Mirrors the Postgres auth_providers table (social login links). Nothing
        # writes it yet; it exists so the erasure pass runs the same on both backends.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS auth_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                provider TEXT NOT NULL CHECK (provider IN ('google','facebook','apple')),
                provider_uid TEXT NOT NULL,
                email_at_link TEXT,
                email_verified BOOLEAN DEFAULT 0,
                display_name TEXT,
                raw_profile TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login_at TIMESTAMP,
                UNIQUE (provider, provider_uid),
                UNIQUE (user_id, provider),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_auth_providers_user ON auth_providers(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_auth_providers_lookup ON auth_providers(provider, provider_uid)')

        # Custom formula indicators
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS custom_formulas (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                formula TEXT NOT NULL,
                style TEXT NOT NULL DEFAULT 'line',
                color TEXT NOT NULL DEFAULT '#2196F3',
                pane TEXT NOT NULL DEFAULT 'new',
                line_width INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_custom_formulas_user ON custom_formulas(user_id)')

        # Backtest strategies
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategies (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                entry_formula TEXT NOT NULL,
                exit_formula TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'long',
                capital REAL NOT NULL DEFAULT 10000,
                size_percent REAL NOT NULL DEFAULT 100,
                commission_percent REAL NOT NULL DEFAULT 0.1,
                params TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_strategies_user ON strategies(user_id)')
        # pre-existing SQLite files were created without `params`.
        try:
            cursor.execute('ALTER TABLE strategies ADD COLUMN params TEXT')
        except Exception:
            pass  # already present — SQLite has no ADD COLUMN IF NOT EXISTS

        # Context Recipes (AI Context Composer)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS context_recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                is_default BOOLEAN DEFAULT 0,
                blocks TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_recipes_user ON context_recipes(user_id)')

        # Tier Config (Admin — dynamic tier limit overrides)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tier_config (
                tier TEXT PRIMARY KEY,
                limits_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by INTEGER REFERENCES users(id)
            )
        ''')

        # Per-User Tier Overrides
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_tier_overrides (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                override_json TEXT NOT NULL,
                reason TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by INTEGER REFERENCES users(id)
            )
        ''')

        # Admin action audit trail
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                actor_user_id INTEGER,
                subject_user_id INTEGER,
                action TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                source TEXT NOT NULL,
                note TEXT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS ix_audit_log_subject_ts ON audit_log(subject_user_id, ts)')
        cursor.execute('CREATE INDEX IF NOT EXISTS ix_audit_log_action_ts ON audit_log(action, ts)')

        # Quarantine table for Paddle events without confirmed customer mapping
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_paddle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paddle_customer_id TEXT NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                event_json TEXT NOT NULL,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS ix_pending_paddle_customer ON pending_paddle_events(paddle_customer_id)')

        # Webhook inbox / DLQ
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS paddle_webhook_inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                last_error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS ix_paddle_inbox_unprocessed ON paddle_webhook_inbox(processed_at, retry_count)')

        # User alerts of every kind (carrac/alerts/store.py)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_alerts (
                id BIGINT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                alert_type TEXT NOT NULL DEFAULT 'price',
                symbol TEXT,
                active BOOLEAN NOT NULL DEFAULT 1,
                triggered BOOLEAN NOT NULL DEFAULT 0,
                data TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_alerts_user ON user_alerts(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_alerts_type_active ON user_alerts(alert_type, active)')

        # Web-push subscriptions, one per user (carrac/alerts/push_store.py)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                user_id INTEGER PRIMARY KEY,
                subscription TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        ''')

        conn.commit()
        logger.info(f"User database initialized at {self.db_path}")

    # ==================== User Management ====================

    def create_user(self, email: str, password: str, display_name: Optional[str] = None,
                    invited_by: Optional[int] = None, tos_accepted: bool = False,
                    tos_ip: Optional[str] = None, tos_user_agent: Optional[str] = None,
                    age_attested: bool = False) -> Optional[int]:
        """Create a new user. Returns user ID or None if failed."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                # Check if this is the admin email (bool(ADMIN_EMAIL) prevents
                # fail-open when ADMIN_EMAIL is unset — empty == empty would be True)
                is_admin = bool(ADMIN_EMAIL) and email.lower() == ADMIN_EMAIL.lower()

                password_hash = _ph.hash(password)
                tos_ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() if tos_accepted else None
                # age_attested uses the same timestamp/IP pair pattern as
                # tos. ip/ua come from the same request record on both.
                age_ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() if age_attested else None

                cursor.execute(adapt_query('''
                    INSERT INTO users (email, password_hash, display_name, is_admin, invited_by,
                                       tos_accepted_at, tos_accepted_ip, tos_accepted_ua,
                                       age_attested_at, age_attested_ip)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''') + returning_id(), (email.lower(), password_hash, display_name or email.split('@')[0],
                                        is_admin, invited_by, tos_ts, tos_ip, tos_user_agent,
                                        age_ts, tos_ip))

                user_id = get_last_id(cursor)

                # Initialize user settings
                cursor.execute(adapt_query('''
                    INSERT INTO user_settings (user_id, watchlist_data, preferences)
                    VALUES (?, '{}', '{}')
                '''), (user_id,))
        except handle_integrity_error():
            logger.warning(f"User already exists: {email}")
            return None
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            return None
        logger.info(f"Created user: {email} (admin={is_admin})")
        return user_id

    def authenticate(self, email: str, password: str) -> Optional[Dict]:
        """
        Authenticate user with timing-safe comparison.

        Returns user dict or None.

        Security:
        - Always performs password hash check to prevent timing attacks
        - Adds random delay to mask timing differences
        """
        with self._tx() as conn:
            cursor = conn.cursor()
            cursor.execute(adapt_query('''
                SELECT id, email, password_hash, display_name, is_admin, is_active, tier,
                       subscription_expires_at
                FROM users WHERE email = ?
            '''), (email.lower(),))
            row = cursor.fetchone()

        # Timing-safe: Always perform password check even if user doesn't exist
        # This prevents attackers from determining if a user exists via timing
        if not row:
            # Perform a dummy hash check to maintain consistent timing
            try:
                _ph.verify('$argon2id$v=19$m=65536,t=3,p=4$dummy$dummyhash', password)
            except Exception:
                pass
            add_timing_noise()
            return None

        if not row['is_active']:
            logger.warning(f"Inactive user attempted login: {email}")
            add_timing_noise()
            return None

        # Dual-hash verification: support both legacy pbkdf2 and argon2id
        stored_hash = row['password_hash']
        password_valid = False
        needs_rehash = False

        if stored_hash.startswith(('pbkdf2:', 'scrypt:')):
            # Legacy werkzeug hash (pbkdf2 or scrypt) — verify then upgrade
            password_valid = check_password_hash(stored_hash, password)
            if password_valid:
                needs_rehash = True
        else:
            # Argon2id hash
            try:
                password_valid = _ph.verify(stored_hash, password)
                # Check if argon2 params need upgrading
                if password_valid and _ph.check_needs_rehash(stored_hash):
                    needs_rehash = True
            except VerifyMismatchError:
                password_valid = False
            except Exception:
                password_valid = False

        if not password_valid:
            add_timing_noise()
            return None

        # Transparently re-hash with argon2id on successful login. Its own
        # transaction, so a failure here cannot cost the user the login.
        if needs_rehash:
            try:
                new_hash = _ph.hash(password)
                with self._tx() as conn:
                    conn.execute(adapt_query('UPDATE users SET password_hash = ? WHERE id = ?'),
                                 (new_hash, row['id']))
                logger.info(f"[SECURITY] Password hash upgraded to argon2id for user {email}")
            except Exception as e:
                logger.error("[SECURITY] Failed to upgrade password hash for user %s: %s", row['id'], e)

        with self._tx() as conn:
            conn.execute(adapt_query('UPDATE users SET last_login = ? WHERE id = ?'),
                         (datetime.now(), row['id']))

        return {
            'id': row['id'],
            'email': row['email'],
            'display_name': row['display_name'],
            'is_admin': bool(row['is_admin']),
            'tier': 'admin' if bool(row['is_admin']) else (row['tier'] or 'free'),
        }

    def get_user(self, user_id: int) -> Optional[Dict]:
        """Get user by ID."""
        return self.get_user_by_id(user_id)

    def get_user_by_email(self, email: str) -> Optional[Dict]:
        """Get user by email."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    SELECT id, email, display_name, is_admin, is_active, tier,
                           subscription_expires_at, created_at, last_login
                    FROM users WHERE email = ?
                '''), (email.lower(),))
                row = cursor.fetchone()
                if not row:
                    return None
                return dict(row)
        except Exception as e:
            logger.debug(f"get_user_by_email error: {e}")
            return None

    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Get user by ID."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    SELECT id, email, display_name, is_admin, is_active, tier,
                           subscription_expires_at, paddle_customer_id, paddle_subscription_id, created_at, last_login
                    FROM users WHERE id = ?
                '''), (user_id,))
                row = cursor.fetchone()
                if not row:
                    return None
                return dict(row)
        except Exception as e:
            logger.debug(f"get_user_by_id error: {e}")
            return None

    def list_users(self) -> List[Dict]:
        """List all users (admin function)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT u.id, u.email, u.display_name, u.is_admin, u.is_active, u.tier,
                           u.created_at, u.last_login, u.last_active, inv.email as invited_by_email
                    FROM users u
                    LEFT JOIN users inv ON u.invited_by = inv.id
                    ORDER BY u.created_at DESC
                ''')
                rows = [dict(row) for row in cursor.fetchall()]
                return rows
        except Exception as e:
            logger.error(f"Error listing users: {e}")
            return []

    def update_last_active(self, user_id: int):
        """Update user's last_active timestamp. Throttled to once per 60s by caller.

        Best-effort: a missed stamp only makes the activity column stale, so
        a failure is logged rather than failing the request that triggered it.
        """
        try:
            with self._tx() as conn:
                conn.execute(adapt_query('UPDATE users SET last_active = ? WHERE id = ?'),
                             (datetime.now(), user_id))
        except Exception as e:
            logger.warning("last_active stamp failed for user %s: %s", user_id, e)

    def update_password(self, user_id: int, new_password: str) -> bool:
        """Update user password (admin function)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                password_hash = _ph.hash(new_password)
                cursor.execute(adapt_query('UPDATE users SET password_hash = ? WHERE id = ?'),
                               (password_hash, user_id))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating password: {e}")
            return False

    def set_user_active(self, user_id: int, is_active: bool) -> bool:
        """Activate/deactivate user (admin function)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                cursor.execute(adapt_query('UPDATE users SET is_active = ? WHERE id = ?'),
                               (is_active, user_id))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error setting user active status: {e}")
            return False

    def set_user_tier(self, user_id: int, tier: str, _commit: bool = True) -> bool:
        """Set user subscription tier (admin function). Clears subscription_expires_at.

        Pass `_commit=False` when called inside
        `process_paddle_event_atomic` so the caller controls the transaction
        boundary (marker INSERT must happen LAST in the same txn).
        """
        if tier not in ('free', 'member', 'premium', 'tester'):
            return False
        try:
            with self._tx(commit=_commit) as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('UPDATE users SET tier = ?, subscription_expires_at = NULL WHERE id = ?'), (tier, user_id))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error setting user tier: {e}")
            return False

    def refund_subscription(self, user_id: int, full: bool,
                            refund_amount_cents: int = 0, event_id: str = '',
                            _commit: bool = True) -> None:
        """Only downgrade tier on full refund; partial refunds log
        the event but preserve subscription_expires_at.

        Args:
            full: True → full refund → clear tier to 'free' (existing behaviour).
            refund_amount_cents: Refunded amount from Paddle payload (informational).
            event_id: Paddle event_id for the audit row.
            _commit: Deferred commit when called inside process_paddle_event_atomic.
        """
        if full:
            self.set_user_tier(user_id, 'free', _commit=_commit)
            logger.info(f"refund_subscription: full refund — downgraded user {user_id} to free")
        else:
            # Partial refund: log an audit row but preserve paid period
            if refund_amount_cents > 0:
                self.record_ai_deposit(
                    user_id, -refund_amount_cents,
                    payment_id=event_id,
                    source='paddle_partial_refund',
                    _commit=_commit,
                )
            logger.info(
                f"refund_subscription: partial refund {refund_amount_cents}c "
                f"for user {user_id} — tier preserved"
            )

    def update_tier_only(self, user_id: int, tier: str, _commit: bool = True) -> bool:
        """Update tier without touching subscription_expires_at (for plan changes).

        `_commit=False` defers commit to the atomic webhook helper.
        """
        if tier not in ('free', 'member', 'premium', 'tester'):
            return False
        try:
            with self._tx(commit=_commit) as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('UPDATE users SET tier = ? WHERE id = ?'), (tier, user_id))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating user tier: {e}")
            return False

    # ==================== Tier Config (Admin) ====================

    def get_all_tier_configs(self) -> Dict:
        """Get all tier configs from DB. Returns {tier: limits_dict}."""
        try:
            with self._tx() as conn:
                rows = conn.execute('SELECT tier, limits_json FROM tier_config').fetchall()
                result = {}
                for row in rows:
                    data = row['limits_json']
                    result[row['tier']] = json.loads(data) if isinstance(data, str) else data
                return result
        except Exception as e:
            logger.debug(f"tier_config table not available: {e}")
            return {}

    def set_tier_config(self, tier: str, limits: Dict, admin_id: int = None) -> bool:
        """Upsert tier config."""
        try:
            with self._tx() as conn:
                # Convert frozensets/sets to sorted lists for JSON serialization
                safe_limits = {k: sorted(v) if isinstance(v, (set, frozenset)) else v
                               for k, v in limits.items()}
                limits_json = json.dumps(safe_limits)
                if get_db_type() == 'postgresql':
                    conn.execute(
                        '''INSERT INTO tier_config (tier, limits_json, updated_at, updated_by)
                           VALUES (%s, %s, NOW(), %s)
                           ON CONFLICT (tier) DO UPDATE
                           SET limits_json = EXCLUDED.limits_json,
                               updated_at = NOW(),
                               updated_by = EXCLUDED.updated_by''',
                        (tier, limits_json, admin_id)
                    )
                else:
                    conn.execute(
                        '''INSERT OR REPLACE INTO tier_config (tier, limits_json, updated_at, updated_by)
                           VALUES (?, ?, datetime('now'), ?)''',
                        (tier, limits_json, admin_id)
                    )
                return True
        except Exception as e:
            logger.error(f"Error setting tier config: {e}")
            return False

    def get_user_overrides(self, user_id: int) -> Optional[Dict]:
        """Get per-user tier overrides. Returns None if no overrides."""
        try:
            with self._tx() as conn:
                row = conn.execute(
                    adapt_query('SELECT override_json, reason FROM user_tier_overrides WHERE user_id = ?'),
                    (user_id,)
                ).fetchone()
                if not row:
                    return None
                data = row['override_json']
                result = json.loads(data) if isinstance(data, str) else data
                result['_reason'] = row['reason']
                return result
        except Exception as e:
            logger.debug(f"user_tier_overrides not available: {e}")
            return None

    def set_user_overrides(self, user_id: int, overrides: Dict, admin_id: int = None, reason: str = None) -> bool:
        """Set per-user tier limit overrides."""
        try:
            with self._tx() as conn:
                overrides_json = json.dumps(overrides)
                if get_db_type() == 'postgresql':
                    conn.execute(
                        '''INSERT INTO user_tier_overrides (user_id, override_json, reason, updated_at, updated_by)
                           VALUES (%s, %s, %s, NOW(), %s)
                           ON CONFLICT (user_id) DO UPDATE
                           SET override_json = EXCLUDED.override_json,
                               reason = EXCLUDED.reason,
                               updated_at = NOW(),
                               updated_by = EXCLUDED.updated_by''',
                        (user_id, overrides_json, reason, admin_id)
                    )
                else:
                    conn.execute(
                        '''INSERT OR REPLACE INTO user_tier_overrides (user_id, override_json, reason, updated_at, updated_by)
                           VALUES (?, ?, ?, datetime('now'), ?)''',
                        (user_id, overrides_json, reason, admin_id)
                    )
        except Exception as e:
            logger.error(f"Error setting user overrides: {e}")
            return False
        self._invalidate_overrides_cache(user_id)
        return True

    @staticmethod
    def _invalidate_overrides_cache(user_id: int):
        """Drop this worker's cached overrides for the user, after the commit,
        so the next get_tier_limits() call re-reads the DB instead of serving
        a stale (up to 60s) value. Late import to avoid a module-level cycle:
        decorators.py late-imports user_db the same way, for the same reason."""
        try:
            from carrac.auth.decorators import invalidate_user_overrides_cache
            invalidate_user_overrides_cache(user_id)
        except Exception as e:
            logger.warning("Override cache not invalidated for user %s: %s", user_id, e)

    def clear_user_overrides(self, user_id: int) -> bool:
        """Remove per-user tier overrides."""
        try:
            with self._tx() as conn:
                conn.execute(adapt_query('DELETE FROM user_tier_overrides WHERE user_id = ?'), (user_id,))
        except Exception as e:
            logger.error(f"Error clearing user overrides: {e}")
            return False
        self._invalidate_overrides_cache(user_id)
        return True

    # ==================== Subscription Management ====================

    def activate_subscription(self, user_id: int, tier: str = 'premium', days: int = 30,
                              payment_id: str = None, _commit: bool = True) -> bool:
        """Activate or extend subscription for a user at the given tier.

        Pass `_commit=False` when invoked from
        `process_paddle_event_atomic` so the entire webhook handler runs as
        one transaction. The marker INSERT then commits LAST. If the caller
        commits early, a mid-handler failure would leave the subscription
        extended without the idempotency marker, allowing the Paddle retry
        to extend it AGAIN.
        """
        if tier not in ('member', 'premium'):
            logger.error(f"Invalid subscription tier: {tier}")
            return False
        try:
            with self._tx(commit=_commit) as conn:
                cursor = conn.cursor()

                # If user already has a future expiry, extend from that date (not from now)
                cursor.execute(adapt_query('SELECT subscription_expires_at FROM users WHERE id = ?'), (user_id,))
                row = cursor.fetchone()
                if row and row['subscription_expires_at']:
                    try:
                        current_expiry = datetime.fromisoformat(row['subscription_expires_at'])
                        now_utc = datetime.now(timezone.utc)
                        # Normalize existing naive expiry for comparison
                        if current_expiry.tzinfo is None:
                            current_expiry = current_expiry.replace(tzinfo=timezone.utc)
                        if current_expiry > now_utc:
                            new_expiry = current_expiry + timedelta(days=days)
                        else:
                            new_expiry = now_utc + timedelta(days=days)
                    except (ValueError, TypeError):
                        new_expiry = datetime.now(timezone.utc) + timedelta(days=days)
                else:
                    new_expiry = datetime.now(timezone.utc) + timedelta(days=days)

                cursor.execute(adapt_query('''
                    UPDATE users SET tier = ?, subscription_expires_at = ?,
                           subscription_payment_id = ? WHERE id = ?
                '''), (tier, new_expiry.isoformat(), payment_id, user_id))
                logger.info(f"Activated {tier} subscription for user {user_id} until {new_expiry}")
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error activating subscription: {e}")
            return False

    def check_subscription_expiry(self, user_id: int) -> Dict:
        """Check and auto-downgrade expired subscriptions. Returns status dict."""
        with self._tx() as conn:
            row = conn.execute(adapt_query(
                'SELECT tier, subscription_expires_at FROM users WHERE id = ?'
            ), (user_id,)).fetchone()
        if not row:
            return {'tier': 'free', 'expires_at': None, 'expired': False}

        tier = row['tier'] or 'free'
        expires_at = row['subscription_expires_at']

        if expires_at:
            try:
                # Postgres returns a datetime, SQLite an ISO string.
                expiry_dt = (expires_at if isinstance(expires_at, datetime)
                             else datetime.fromisoformat(expires_at))
                expires_at = expiry_dt.isoformat()
                # Normalise to UTC-aware if naive (legacy rows stored without tz)
                if expiry_dt.tzinfo is None:
                    expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                grace_deadline = expiry_dt + timedelta(days=SUBSCRIPTION_GRACE_DAYS)
                if expiry_dt >= now_utc:
                    # Still within paid period
                    days_remaining = (expiry_dt - now_utc).days
                    return {'tier': tier, 'expires_at': expires_at,
                            'days_remaining': days_remaining, 'expired': False,
                            'subscription_in_grace': False}
                elif grace_deadline > now_utc:
                    # Expired but within grace window: do NOT downgrade yet
                    logger.info(
                        f"User {user_id} subscription in grace period "
                        f"(expired {expiry_dt.isoformat()}, grace until {grace_deadline.isoformat()})"
                    )
                    return {'tier': tier, 'expires_at': expires_at,
                            'days_remaining': 0, 'expired': False,
                            'subscription_in_grace': True}
                else:
                    # Beyond grace — auto-downgrade
                    with self._tx() as conn:
                        conn.execute(adapt_query('''
                            UPDATE users SET tier = 'free', subscription_expires_at = NULL
                            WHERE id = ?
                        '''), (user_id,))
                    logger.info(f"Auto-downgraded user {user_id} (subscription expired beyond grace)")
                    return {'tier': 'free', 'expires_at': None, 'expired': True,
                            'subscription_in_grace': False}
            except (ValueError, TypeError):
                logger.warning(
                    "User %s has an unreadable subscription_expires_at %r; not downgrading",
                    user_id, expires_at)

        return {'tier': tier, 'expires_at': expires_at, 'expired': False}

    def get_expiring_subscriptions(self, within_days: int = 3) -> List[Dict]:
        """Get users whose subscriptions expire within N days."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cutoff = (datetime.now() + timedelta(days=within_days)).isoformat()
                cursor.execute(adapt_query('''
                    SELECT id, email, display_name, subscription_expires_at
                    FROM users
                    WHERE tier IN ('member', 'premium') AND subscription_expires_at IS NOT NULL
                      AND subscription_expires_at <= ? AND subscription_expires_at > ?
                '''), (cutoff, datetime.now().isoformat()))
                rows = [dict(row) for row in cursor.fetchall()]
                return rows
        except Exception as e:
            logger.error(f"Error fetching expiring subscriptions: {e}")
            return []

    # ==================== Paddle Integration ====================

    def set_paddle_customer_id(self, user_id: int, customer_id: str, _commit: bool = True):
        """Store Paddle customer ID on user record.

        `_commit=False` defers commit to the atomic webhook helper.
        """
        with self._tx(commit=_commit) as conn:
            conn.execute(adapt_query('UPDATE users SET paddle_customer_id = ? WHERE id = ?'),
                         (customer_id, user_id))

    def set_paddle_subscription_id(self, user_id: int, subscription_id: str, _commit: bool = True):
        """Store Paddle subscription ID on user record.

        `_commit=False` defers commit to the atomic webhook helper.
        """
        with self._tx(commit=_commit) as conn:
            conn.execute(adapt_query('UPDATE users SET paddle_subscription_id = ? WHERE id = ?'),
                         (subscription_id, user_id))

    def get_user_by_paddle_customer_id(self, customer_id: str) -> Optional[Dict]:
        """Look up user by Paddle customer ID."""
        try:
            with self._tx() as conn:
                row = conn.execute(adapt_query(
                    'SELECT id, email, tier, paddle_customer_id, paddle_subscription_id FROM users WHERE paddle_customer_id = ?'
                ), (customer_id,)).fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.debug(f"get_user_by_paddle_customer_id error: {e}")
            return None

    # ==================== Invite Management ====================

    def create_email_invite(self, email: str, created_by: int,
                            expires_days: int = 30) -> bool:
        """Add email to whitelist."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                expires_at = datetime.now() + timedelta(days=expires_days)

                cursor.execute(adapt_query('''
                    INSERT INTO invites (type, value, created_by, expires_at)
                    VALUES ('email', ?, ?, ?)
                '''), (email.lower(), created_by, expires_at))
        except Exception as e:
            logger.error(f"Error creating email invite: {e}")
            return False
        logger.info(f"Created email invite for: {email}")
        return True

    def generate_invite_code(self, created_by: int, expires_days: int = 7) -> Optional[str]:
        """Generate a random invite code."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                # Generate 8-character alphanumeric code
                code = ''.join(secrets.choice(string.ascii_uppercase + string.digits)
                               for _ in range(8))

                expires_at = datetime.now() + timedelta(days=expires_days)

                cursor.execute(adapt_query('''
                    INSERT INTO invites (type, value, created_by, expires_at)
                    VALUES ('code', ?, ?, ?)
                '''), (code, created_by, expires_at))
        except Exception as e:
            logger.error(f"Error generating invite code: {e}")
            return None
        logger.info(f"Generated invite code: {code}")
        return code

    def validate_invite(self, email: str, code: Optional[str] = None) -> Optional[Dict]:
        """
        Check if registration is allowed.
        Returns invite dict if valid, None if not allowed.

        All users (including admin) must have a valid invite (email whitelist
        or invite code). The admin email no longer bypasses the invite check.
        """
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                now = datetime.now()

                # Check email whitelist
                cursor.execute(adapt_query('''
                    SELECT id, type, value, created_by FROM invites
                    WHERE type = 'email' AND value = ? AND used_by IS NULL
                    AND (expires_at IS NULL OR expires_at > ?)
                '''), (email.lower(), now))

                row = cursor.fetchone()
                if row:
                    return dict(row)

                # Check invite code
                if code:
                    cursor.execute(adapt_query('''
                        SELECT id, type, value, created_by FROM invites
                        WHERE type = 'code' AND value = ? AND used_by IS NULL
                        AND (expires_at IS NULL OR expires_at > ?)
                    '''), (code.upper(), now))

                    row = cursor.fetchone()
                    if row:
                        return dict(row)

                return None
        except Exception as e:
            logger.debug(f"validate_invite error: {e}")
            return None

    def mark_invite_used(self, invite_id: int, user_id: int) -> bool:
        """Mark invite as used after successful registration."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                cursor.execute(adapt_query('UPDATE invites SET used_by = ? WHERE id = ? AND used_by IS NULL'),
                               (user_id, invite_id))
                if cursor.rowcount == 0:
                    # Another concurrent registration consumed this invite first.
                    logger.warning(f"mark_invite_used: invite {invite_id} already used (rowcount=0); lost race")
                    return False
                return True
        except Exception as e:
            logger.error(f"Error marking invite used: {e}")
            return False

    # ==================== Beta Waitlist ====================

    def add_to_waitlist(self, email: str, source: Optional[str] = None,
                        ip: Optional[str] = None) -> Dict:
        """Add an email to the beta waitlist, deduped on email.

        Returns {'status': 'added'} for a new row or {'status': 'exists', ...}
        if the email is already present (any state). The UNIQUE(email) constraint
        is the race backstop — a concurrent duplicate INSERT is caught + treated
        as 'exists'.
        """
        email = (email or '').lower().strip()
        if not email or '@' not in email:
            return {'status': 'invalid'}
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('SELECT id, status FROM waitlist WHERE email = ?'), (email,))
                row = cursor.fetchone()
                if row:
                    return {'status': 'exists', 'waitlist_status': dict(row).get('status')}
                cursor.execute(
                    adapt_query('INSERT INTO waitlist (email, source, ip) VALUES (?, ?, ?)'),
                    (email, source, ip),
                )
                return {'status': 'added'}
        except Exception as e:
            if isinstance(e, handle_integrity_error()):
                # A UNIQUE(email) race — already on the list.
                logger.info(f"add_to_waitlist({email}): duplicate: {e}")
            else:
                logger.error("add_to_waitlist(%s): signup not stored: %s", email, e)
            return {'status': 'exists'}

    def pick_next_waitlist(self, n: int) -> List[Dict]:
        """Oldest-first 'waiting' rows for the next invite wave (id, email)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    SELECT id, email FROM waitlist
                    WHERE status = 'waiting' ORDER BY created_at ASC LIMIT ?
                '''), (int(n),))
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"pick_next_waitlist error: {e}")
            return []

    def mark_waitlisted_invited(self, email: str, invite_code: str,
                                cohort: Optional[str] = None) -> bool:
        """Mark a waitlist row as invited + record the issued code (+ optional cohort)."""
        email = (email or '').lower().strip()
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    UPDATE waitlist
                    SET status = 'invited', invite_code = ?,
                        cohort = COALESCE(?, cohort), invited_at = ?
                    WHERE email = ? AND status = 'waiting'
                '''), (invite_code, cohort, datetime.now(), email))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"mark_waitlisted_invited({email}) error: {e}")
            return False

    def mark_waitlisted_registered(self, email: str) -> bool:
        """Mark a waitlist row as registered (idempotent; no-op if not on the list)."""
        email = (email or '').lower().strip()
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    UPDATE waitlist SET status = 'registered', registered_at = ?
                    WHERE email = ? AND status != 'registered'
                '''), (datetime.now(), email))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("mark_waitlisted_registered(%s) failed: %s", email, e)
            return False

    def count_waitlist(self, status: Optional[str] = None) -> int:
        """Count waitlist rows, optionally filtered by status."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                if status:
                    cursor.execute(adapt_query('SELECT COUNT(*) AS c FROM waitlist WHERE status = ?'), (status,))
                else:
                    cursor.execute('SELECT COUNT(*) AS c FROM waitlist')
                row = cursor.fetchone()
                return int(dict(row)['c']) if row else 0
        except Exception as e:
            logger.error(f"count_waitlist error: {e}")
            return 0

    @staticmethod
    def beta_waitlist_path() -> str:
        """The JSONL mirror of the waitlist, written by `_log_beta_waitlist`.

        Defined here, once, because there are now two consumers —
        the writer in carrac/auth/routes.py and `delete_from_waitlist` below.
        Writing the path twice is how the two stores would drift apart, and a
        forgotten second copy of personal data is the whole defect this fixes.
        """
        return os.path.join(os.environ.get('DATA_DIR', 'data'),
                            'private', 'beta_waitlist.jsonl')

    def delete_from_waitlist(self, email: str) -> Dict[str, int]:
        """Erase a waitlist signup from BOTH stores it lives in.

        `waitlist` holds an email and an IP and has no `user_id`, so
        `anonymize_user` could not reach it even in principle, and no delete
        route existed anywhere — a waitlist signup had no way to be erased and
        nothing to be erased BY. The same address is also mirrored to
        `data/private/beta_waitlist.jsonl`, which nothing pruned, exported or
        erased either.

        Both are cleared together on purpose: clearing one and leaving the other
        is indistinguishable from having done nothing.

        Returns {'rows': n, 'file_entries': n} so a caller can log what actually
        went, rather than assuming.
        """
        email = (email or '').strip().lower()
        result = {'rows': 0, 'file_entries': 0}
        if not email:
            return result

        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('DELETE FROM waitlist WHERE LOWER(email) = ?'),
                               (email,))
                result['rows'] = cursor.rowcount or 0
        except Exception as e:
            logger.error(f"waitlist row erasure failed for a subject: {e}")

        # The JSONL mirror. Rewrite without the subject's lines rather than
        # truncating — other people's signups live in the same file.
        path = self.beta_waitlist_path()
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                kept = []
                for line in lines:
                    try:
                        entry = json.loads(line)
                    except Exception:
                        kept.append(line)  # unparseable: not ours to drop
                        continue
                    if str(entry.get('email', '')).strip().lower() == email:
                        result['file_entries'] += 1
                    else:
                        kept.append(line)
                if result['file_entries']:
                    tmp = f'{path}.tmp'
                    with open(tmp, 'w', encoding='utf-8') as f:
                        f.writelines(kept)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, path)
        except Exception as e:
            logger.error(f"waitlist file erasure failed for a subject: {e}")

        return result

    def get_waitlist(self, status: Optional[str] = None, limit: int = 200) -> List[Dict]:
        """List waitlist rows (admin), newest first."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                if status:
                    cursor.execute(adapt_query('''
                        SELECT * FROM waitlist WHERE status = ?
                        ORDER BY created_at DESC LIMIT ?
                    '''), (status, int(limit)))
                else:
                    cursor.execute(adapt_query('SELECT * FROM waitlist ORDER BY created_at DESC LIMIT ?'), (int(limit),))
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"get_waitlist error: {e}")
            return []

    def list_invites(self, include_used: bool = False) -> List[Dict]:
        """List all invites (admin function)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                if include_used:
                    cursor.execute('''
                        SELECT i.*, u.email as created_by_email, u2.email as used_by_email,
                               u2.created_at as used_at
                        FROM invites i
                        JOIN users u ON i.created_by = u.id
                        LEFT JOIN users u2 ON i.used_by = u2.id
                        ORDER BY i.created_at DESC
                    ''')
                else:
                    # Include all invites (used and unused) to show usage status
                    cursor.execute(adapt_query('''
                        SELECT i.*, u.email as created_by_email, u2.email as used_by_email,
                               u2.created_at as used_at
                        FROM invites i
                        JOIN users u ON i.created_by = u.id
                        LEFT JOIN users u2 ON i.used_by = u2.id
                        WHERE (i.expires_at IS NULL OR i.expires_at > ? OR i.used_by IS NOT NULL)
                        ORDER BY i.created_at DESC
                    '''), (datetime.now(),))
                rows = [dict(row) for row in cursor.fetchall()]
                return rows
        except Exception as e:
            logger.error(f"Error listing invites: {e}")
            return []

    def delete_invite(self, invite_id: int) -> bool:
        """Delete an invite (admin function)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                cursor.execute(adapt_query('DELETE FROM invites WHERE id = ? AND used_by IS NULL'),
                               (invite_id,))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error deleting invite: {e}")
            return False

    # ==================== User Settings ====================

    def get_watchlist(self, user_id: int) -> Dict:
        """Get user's watchlist data."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('SELECT watchlist_data FROM user_settings WHERE user_id = ?'),
                               (user_id,))
                row = cursor.fetchone()
                if row and row['watchlist_data']:
                    try:
                        return json.loads(row['watchlist_data'])
                    except json.JSONDecodeError:
                        return {}
                return {}
        except Exception as e:
            logger.debug(f"get_watchlist error: {e}")
            return {}

    def save_watchlist(self, user_id: int, watchlist_data: Dict) -> bool:
        """Save user's watchlist data."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                data_json = json.dumps(watchlist_data)

                if get_db_type() == 'postgresql':
                    cursor.execute('''
                        INSERT INTO user_settings (user_id, watchlist_data, updated_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET
                            watchlist_data = EXCLUDED.watchlist_data,
                            updated_at = EXCLUDED.updated_at
                    ''', (user_id, data_json, datetime.now()))
                else:
                    cursor.execute('''
                        INSERT INTO user_settings (user_id, watchlist_data, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            watchlist_data = excluded.watchlist_data,
                            updated_at = excluded.updated_at
                    ''', (user_id, data_json, datetime.now()))

                return True
        except Exception as e:
            logger.error(f"Error saving watchlist: {e}")
            return False

    # first-run watchlist default. Seed BTC/ETH/SOL once when a
    # net-new user first reads their watchlist so the app isn't empty on the
    # very first paint. Guarded by a one-way `watchlist_seeded` preference
    # flag so intentional clears by the user stay cleared.
    DEFAULT_WATCHLIST_SEED = ['BTC', 'ETH', 'SOL']

    def get_flat_watchlist(self, user_id: int) -> list:
        """Get user's watchlist as a flat ordered symbol list."""
        data = self.get_watchlist(user_id)
        symbols = self._extract_flat_symbols(data)
        if not symbols and user_id:
            symbols = self._seed_default_watchlist_if_needed(user_id)
        return symbols

    @staticmethod
    def _extract_flat_symbols(data) -> list:
        # Grouped format: {groups: [{id, name, symbols: [...]}]}
        if isinstance(data, dict) and 'groups' in data:
            symbols = []
            for group in data['groups']:
                symbols.extend(group.get('symbols', []))
            return symbols
        if isinstance(data, list):
            return list(data)
        if isinstance(data, dict) and 'symbols' in data:
            return list(data['symbols'])
        return []

    def _seed_default_watchlist_if_needed(self, user_id: int) -> list:
        try:
            prefs = self.get_preferences(user_id) or {}
            if prefs.get('watchlist_seeded'):
                return []
            seeded = list(self.DEFAULT_WATCHLIST_SEED)
            if self.save_flat_watchlist(user_id, seeded):
                prefs['watchlist_seeded'] = True
                try:
                    self.save_preferences(user_id, prefs)
                except PreferencesWriteError as exc:
                    # The watchlist itself is stored; without the flag it is
                    # only offered again once the user empties it.
                    logger.error("watchlist_seeded flag not saved for user %s: %s", user_id, exc)
                logger.info(f"Seeded default watchlist for user {user_id}: {seeded}")
                return seeded
        except Exception as e:
            logger.warning(f"Watchlist seed failed for user {user_id}: {e}")
        return []

    def _get_groups(self, user_id: int) -> list:
        """Normalized grouped view of watchlist_data (no seeding).

        Always a list of {'id','name','symbols'} dicts; legacy flat shapes
        are wrapped into a single default group.
        """
        data = self.get_watchlist(user_id)
        if isinstance(data, dict) and isinstance(data.get('groups'), list):
            groups = []
            seen_ids = set()
            for i, g in enumerate(data['groups']):
                if isinstance(g, dict):
                    gid = str(g.get('id') or '')
                    if not gid:
                        gid = 'default' if i == 0 else 'group_%d' % i
                    if gid in seen_ids:
                        gid = 'group_%d' % i
                        while gid in seen_ids:
                            gid += '_'
                    seen_ids.add(gid)
                    groups.append({
                        'id': gid,
                        'name': str(g.get('name') or 'Watchlist'),
                        'symbols': [str(s).upper() for s in g.get('symbols', [])
                                    if isinstance(s, str)],
                    })
            return groups
        flat = self._extract_flat_symbols(data)
        if flat:
            return [{'id': 'default', 'name': 'Watchlist',
                     'symbols': [str(s).upper() for s in flat]}]
        return []

    def _save_groups(self, user_id: int, groups: list) -> bool:
        """Persist a grouped watchlist + mark seeded (shared write path)."""
        ok = self.save_watchlist(user_id, {'groups': groups})
        if ok and user_id:
            try:
                prefs = self.get_preferences(user_id) or {}
                if not prefs.get('watchlist_seeded'):
                    prefs['watchlist_seeded'] = True
                    self.save_preferences(user_id, prefs)
            except Exception as exc:
                logger.error("watchlist_seeded flag not saved for user %s: %s", user_id, exc)
        return ok

    def get_watchlist_groups(self, user_id: int) -> list:
        """Public grouped read; seeds the default watchlist like the flat getter."""
        groups = self._get_groups(user_id)
        if not groups and user_id:
            seeded = self._seed_default_watchlist_if_needed(user_id)
            if seeded:
                return [{'id': 'default', 'name': 'Watchlist', 'symbols': seeded}]
        return groups

    def save_watchlist_groups(self, user_id: int, groups: list) -> bool:
        """Full-state grouped save (validation happens at the route layer)."""
        return self._save_groups(user_id, groups)

    def save_flat_watchlist(self, user_id: int, symbols: list) -> bool:
        """Save a flat ordered symbol list as the user's watchlist.

        Group-preserving semantics: known symbols keep their group
        and take the induced within-group order; unknown symbols append to
        the default (first) group; symbols absent from `symbols` are removed
        from every group. Single-group users get the legacy full-replace.
        """
        incoming = []
        for s in symbols:
            su = str(s).upper()
            if su not in incoming:
                incoming.append(su)
        groups = self._get_groups(user_id)
        if len(groups) <= 1:
            gid = groups[0]['id'] if groups else 'default'
            gname = groups[0]['name'] if groups else 'Watchlist'
            new_groups = [{'id': gid, 'name': gname, 'symbols': incoming}]
        else:
            membership = {}
            for g in groups:
                for s in g['symbols']:
                    membership.setdefault(s, g['id'])
            rebucketed = {g['id']: [] for g in groups}
            default_id = groups[0]['id']
            for s in incoming:
                rebucketed[membership.get(s, default_id)].append(s)
            new_groups = [{'id': g['id'], 'name': g['name'],
                           'symbols': rebucketed[g['id']]} for g in groups]
        return self._save_groups(user_id, new_groups)

    def add_to_watchlist(self, user_id: int, symbol: str, group_id: str = None) -> bool:
        """Add a symbol (no duplicates across groups). Default: first group."""
        symbol_upper = symbol.upper()
        groups = self._get_groups(user_id)
        if not groups:
            # preserve legacy seed-on-first-touch behavior
            self.get_flat_watchlist(user_id)
            groups = self._get_groups(user_id)
        for g in groups:
            if symbol_upper in g['symbols']:
                return True  # already present
        if not groups:
            groups = [{'id': 'default', 'name': 'Watchlist', 'symbols': []}]
        target = groups[0]
        if group_id:
            for g in groups:
                if g['id'] == group_id:
                    target = g
                    break
        target['symbols'].append(symbol_upper)
        return self._save_groups(user_id, groups)

    def remove_from_watchlist(self, user_id: int, symbol: str) -> bool:
        """Remove a symbol from every group it appears in."""
        symbol_upper = symbol.upper()
        groups = self._get_groups(user_id)
        found = False
        for g in groups:
            if symbol_upper in g['symbols']:
                g['symbols'] = [s for s in g['symbols'] if s != symbol_upper]
                found = True
        if not found:
            return True  # already absent
        return self._save_groups(user_id, groups)

    def get_all_watchlist_symbols(self) -> set:
        """Get union of all users' watchlist symbols (single query).

        Used by the LTF hot tier to precompute indicators for assets
        that any user has in their watchlist.
        """
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT watchlist_data FROM user_settings WHERE watchlist_data IS NOT NULL')
                all_symbols = set()
                for row in cursor.fetchall():
                    try:
                        data = json.loads(row['watchlist_data'])
                        if isinstance(data, dict) and 'groups' in data:
                            for group in data['groups']:
                                all_symbols.update(s.upper() for s in group.get('symbols', []))
                        elif isinstance(data, list):
                            all_symbols.update(s.upper() for s in data)
                        elif isinstance(data, dict) and 'symbols' in data:
                            all_symbols.update(s.upper() for s in data['symbols'])
                    except (json.JSONDecodeError, TypeError):
                        pass
                return all_symbols
        except Exception as e:
            logger.error(f"Error fetching all watchlist symbols: {e}")
            return set()

    def profile_completeness(self, user_id: int) -> int:
        """Compute profile completeness as 0-100 percentage (5 sections x 20% each)."""
        prefs = self.get_preferences(user_id)
        score = 0

        # 1. Watchlist (20%)
        wl = prefs.get('watchlist', [])
        if not wl:
            wl = self.get_flat_watchlist(user_id)
        if wl:
            score += 20

        # 2. Trading style core (20%): styles, risk, holding_period
        tp = prefs.get('trading_profile', {})
        if isinstance(tp, str):
            try:
                tp = json.loads(tp)
            except (json.JSONDecodeError, TypeError):
                tp = {}
        if tp.get('styles') or tp.get('risk') or tp.get('holding_period'):
            score += 20

        # 3. Entry/exit rules (20%)
        if tp.get('entry_conditions') or tp.get('exit_strategy'):
            score += 20

        # 4. Psychology (20%): emotional_patterns or pre_trading_routine
        if tp.get('emotional_patterns') or tp.get('pre_trading_routine'):
            score += 20

        # 5. Analyst sources (20%): x_handles or yt_channels
        if prefs.get('followed_x_handles') or prefs.get('followed_yt_channels'):
            score += 20

        return score

    def get_preferences(self, user_id: int) -> Dict:
        """Get user preferences."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('SELECT preferences FROM user_settings WHERE user_id = ?'),
                               (user_id,))
                row = cursor.fetchone()
                if row and row['preferences']:
                    try:
                        return json.loads(row['preferences'])
                    except json.JSONDecodeError:
                        return {}
                return {}
        except Exception as e:
            logger.debug(f"get_preferences error: {e}")
            return {}

    def save_preferences(self, user_id: int, preferences: Dict) -> None:
        """Store a user's preferences, replacing what was there.

        Raises ``PreferencesWriteError`` when the write fails, so no caller
        can tell the user a setting was saved when it was not.
        """
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                pref_json = json.dumps(preferences)

                if get_db_type() == 'postgresql':
                    cursor.execute('''
                        INSERT INTO user_settings (user_id, preferences, updated_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET
                            preferences = EXCLUDED.preferences,
                            updated_at = EXCLUDED.updated_at
                    ''', (user_id, pref_json, datetime.now()))
                else:
                    cursor.execute('''
                        INSERT INTO user_settings (user_id, preferences, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            preferences = excluded.preferences,
                            updated_at = excluded.updated_at
                    ''', (user_id, pref_json, datetime.now()))
        except Exception as e:
            raise PreferencesWriteError(
                f'preferences for user {user_id} not saved: {e}') from e

    # ==================== Custom Formulas ====================

    def get_custom_formulas(self, user_id: int) -> list:
        """Get all custom formulas for a user."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'SELECT id, name, formula, style, color, pane, line_width, created_at, updated_at '
                    'FROM custom_formulas WHERE user_id = ? ORDER BY created_at'
                ), (user_id,))
                rows = [dict(r) for r in cursor.fetchall()]
                return rows
        except Exception as e:
            logger.error(f"Error fetching custom formulas: {e}")
            return []

    def save_custom_formula(self, user_id: int, formula_data: dict) -> bool:
        """Create or update a custom formula. Uses client-generated ID."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                now = datetime.now()
                fid = formula_data['id']
                if get_db_type() == 'postgresql':
                    cursor.execute('''
                        INSERT INTO custom_formulas (id, user_id, name, formula, style, color, pane, line_width, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            name = EXCLUDED.name, formula = EXCLUDED.formula, style = EXCLUDED.style,
                            color = EXCLUDED.color, pane = EXCLUDED.pane, line_width = EXCLUDED.line_width,
                            updated_at = EXCLUDED.updated_at
                    ''', (fid, user_id, formula_data['name'], formula_data['formula'],
                          formula_data.get('style', 'line'), formula_data.get('color', '#2196F3'),
                          formula_data.get('pane', 'new'), formula_data.get('lineWidth', 1), now, now))
                else:
                    cursor.execute('''
                        INSERT INTO custom_formulas (id, user_id, name, formula, style, color, pane, line_width, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            name = excluded.name, formula = excluded.formula, style = excluded.style,
                            color = excluded.color, pane = excluded.pane, line_width = excluded.line_width,
                            updated_at = excluded.updated_at
                    ''', (fid, user_id, formula_data['name'], formula_data['formula'],
                          formula_data.get('style', 'line'), formula_data.get('color', '#2196F3'),
                          formula_data.get('pane', 'new'), formula_data.get('lineWidth', 1), now, now))
                return True
        except Exception as e:
            logger.error(f"Error saving custom formula: {e}")
            return False

    def delete_custom_formula(self, user_id: int, formula_id: str) -> bool:
        """Delete a custom formula (ownership check via user_id)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'DELETE FROM custom_formulas WHERE id = ? AND user_id = ?'
                ), (formula_id, user_id))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error deleting custom formula: {e}")
            return False

    def count_custom_formulas(self, user_id: int) -> int:
        """Count custom formulas for tier-limit enforcement."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'SELECT COUNT(*) as cnt FROM custom_formulas WHERE user_id = ?'
                ), (user_id,))
                row = cursor.fetchone()
                return row['cnt'] if row else 0
        except Exception as e:
            logger.debug(f"count_custom_formulas error: {e}")
            return 0

    # ==================== Strategies ====================

    def get_strategies(self, user_id: int) -> list:
        """Get all backtest strategies for a user."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'SELECT id, name, entry_formula, exit_formula, direction, capital, '
                    'size_percent, commission_percent, params, created_at, updated_at '
                    'FROM strategies WHERE user_id = ? ORDER BY updated_at DESC'
                ), (user_id,))
                rows = [dict(r) for r in cursor.fetchall()]
                # `params` holds the full setup (timeframe, date range,
                # regime, instrument, and the realism controls). Decoded here so
                # every caller sees a dict; rows saved before this column existed
                # have None, which readers treat as "scalars only" — exactly the
                # behaviour they had before.
                for r in rows:
                    raw = r.get('params')
                    if isinstance(raw, str):
                        try:
                            r['params'] = json.loads(raw)
                        except (ValueError, TypeError):
                            r['params'] = None
                return rows
        except Exception as e:
            logger.error(f"Error fetching strategies: {e}")
            return []

    def save_strategy(self, user_id: int, data: dict) -> bool:
        """Create or update a strategy."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                now = datetime.now()
                sid = data.get('id') or str(uuid.uuid4())[:8]
                # The rest of the panel's setup. Stored as one JSON
                # column rather than ten more scalars because this parameter set
                # is still moving; None when a caller supplies nothing, so old
                # rows and old callers are unaffected.
                _params = data.get('params')
                params_json = json.dumps(_params) if _params else None
                if get_db_type() == 'postgresql':
                    cursor.execute('''
                        INSERT INTO strategies (id, user_id, name, entry_formula, exit_formula,
                            direction, capital, size_percent, commission_percent, params,
                            created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            name = EXCLUDED.name, entry_formula = EXCLUDED.entry_formula,
                            exit_formula = EXCLUDED.exit_formula, direction = EXCLUDED.direction,
                            capital = EXCLUDED.capital, size_percent = EXCLUDED.size_percent,
                            commission_percent = EXCLUDED.commission_percent,
                            params = EXCLUDED.params, updated_at = EXCLUDED.updated_at
                    ''', (sid, user_id, data['name'], data['entry_formula'], data['exit_formula'],
                          data.get('direction', 'long'), data.get('capital', 10000),
                          data.get('size_percent', 100), data.get('commission_percent', 0.1),
                          params_json, now, now))
                else:
                    cursor.execute('''
                        INSERT INTO strategies (id, user_id, name, entry_formula, exit_formula,
                            direction, capital, size_percent, commission_percent, params,
                            created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            name = excluded.name, entry_formula = excluded.entry_formula,
                            exit_formula = excluded.exit_formula, direction = excluded.direction,
                            capital = excluded.capital, size_percent = excluded.size_percent,
                            commission_percent = excluded.commission_percent,
                            params = excluded.params, updated_at = excluded.updated_at
                    ''', (sid, user_id, data['name'], data['entry_formula'], data['exit_formula'],
                          data.get('direction', 'long'), data.get('capital', 10000),
                          data.get('size_percent', 100), data.get('commission_percent', 0.1),
                          params_json, now, now))
                return True
        except Exception as e:
            logger.error(f"Error saving strategy: {e}")
            return False

    def delete_strategy(self, user_id: int, strategy_id: str) -> bool:
        """Delete a strategy (ownership check via user_id)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'DELETE FROM strategies WHERE id = ? AND user_id = ?'
                ), (strategy_id, user_id))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error deleting strategy: {e}")
            return False

    def count_strategies(self, user_id: int) -> int:
        """Count strategies for tier-limit enforcement."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'SELECT COUNT(*) as cnt FROM strategies WHERE user_id = ?'
                ), (user_id,))
                row = cursor.fetchone()
                return row['cnt'] if row else 0
        except Exception as e:
            logger.debug(f"count_strategies error: {e}")
            return 0

    # ==================== AI Credits ====================

    def _ensure_ai_credits(self, user_id: int, _commit: bool = True):
        """Ensure ai_credits row exists for user (idempotent, concurrency-safe).

        `_commit=False` defers commit to the atomic webhook helper.
        The INSERT uses `ON CONFLICT(user_id) DO NOTHING` so a concurrent writer
        that created the row between our SELECT and INSERT cannot make us raise.
        This matters on the Paddle webhook path: with `_commit=False` a raise
        here would propagate out of `work_fn` and surface as a transient 500
        (Paddle then retries) instead of letting the loser fall through to the
        marker INSERT in `process_paddle_event_atomic` and no-op cleanly there.
        DO NOTHING needs no EXCLUDED clause, so one statement serves both
        SQLite and Postgres (only the placeholder differs, handled by adapt_query).

        Any other failure raises. Swallowing it would let the caller's credit
        UPDATE match no row and still report success.
        """
        with self._tx(commit=_commit) as conn:
            row = conn.execute(
                adapt_query('SELECT 1 FROM ai_credits WHERE user_id = ?'), (user_id,)
            ).fetchone()
            if not row:
                conn.execute(
                    adapt_query('INSERT INTO ai_credits (user_id, balance_cents) '
                                'VALUES (?, 0) ON CONFLICT(user_id) DO NOTHING'),
                    (user_id,)
                )

    def get_ai_balance(self, user_id: int) -> int:
        """Get user's credit balance in cents."""
        try:
            with self._tx() as conn:
                row = conn.execute(
                    adapt_query('SELECT balance_cents FROM ai_credits WHERE user_id = ?'), (user_id,)
                ).fetchone()
                return row['balance_cents'] if row else 0
        except Exception as e:
            logger.debug(f"get_ai_balance error: {e}")
            return 0

    def deduct_ai_credits(self, user_id: int, amount_cents: int) -> bool:
        """Atomically deduct credits. Returns False if insufficient balance.

        Uses single UPDATE with WHERE balance >= amount to prevent race conditions (C-1).
        """
        if amount_cents <= 0:
            return True
        with self._tx() as conn:
            cursor = conn.execute(
                adapt_query('''UPDATE ai_credits
                   SET balance_cents = balance_cents - ?,
                       total_spent_cents = total_spent_cents + ?,
                       updated_at = ?
                   WHERE user_id = ? AND balance_cents >= ?'''),
                (amount_cents, amount_cents, datetime.now(), user_id, amount_cents)
            )
            return cursor.rowcount > 0

    def refund_ai_credits(self, user_id: int, amount_cents: int) -> bool:
        """Refund credits (after overpayment or error)."""
        if amount_cents <= 0:
            return True
        with self._tx() as conn:
            cursor = conn.execute(
                adapt_query('''UPDATE ai_credits
                   SET balance_cents = balance_cents + ?,
                       total_spent_cents = CASE WHEN total_spent_cents >= ? THEN total_spent_cents - ? ELSE 0 END,
                       updated_at = ?
                   WHERE user_id = ?'''),
                (amount_cents, amount_cents, amount_cents, datetime.now(), user_id)
            )
            return cursor.rowcount > 0

    def add_ai_credits(self, user_id: int, amount_cents: int, _commit: bool = True) -> bool:
        """Add credits from a deposit.

        `_commit=False` defers commit to the atomic webhook helper.
        """
        if amount_cents <= 0:
            return False
        self._ensure_ai_credits(user_id, _commit=_commit)
        with self._tx(commit=_commit) as conn:
            conn.execute(
                adapt_query('''UPDATE ai_credits
                   SET balance_cents = balance_cents + ?,
                       total_deposited_cents = total_deposited_cents + ?,
                       updated_at = ?
                   WHERE user_id = ?'''),
                (amount_cents, amount_cents, datetime.now(), user_id)
            )
        # Note: tier is subscription-based, not credit-based.
        # Adding credits does NOT auto-upgrade tier. Tier is set by admin
        # or by payment provider webhook on subscription purchase.
        return True

    def clawback_ai_credits(self, user_id: int, amount_cents: int, _commit: bool = True) -> bool:
        """Reverse a deposit (e.g. Paddle refund). Clamps balance to 0.

        `_commit=False` defers commit to the atomic webhook helper.
        """
        if amount_cents <= 0:
            return False
        self._ensure_ai_credits(user_id, _commit=_commit)
        with self._tx(commit=_commit) as conn:
            conn.execute(
                adapt_query('''UPDATE ai_credits
                   SET balance_cents = CASE WHEN balance_cents >= ? THEN balance_cents - ? ELSE 0 END,
                       total_deposited_cents = CASE WHEN total_deposited_cents >= ? THEN total_deposited_cents - ? ELSE 0 END,
                       updated_at = ?
                   WHERE user_id = ?'''),
                (amount_cents, amount_cents, amount_cents, amount_cents, datetime.now(), user_id)
            )
        return True

    def log_ai_usage(self, user_id: int, feature: str, asset: Optional[str],
                     input_tokens: int, output_tokens: int,
                     cost_cents: int, charged_cents: int, model: str,
                     prompt_sha256: Optional[str] = None,
                     response_sha256: Optional[str] = None,
                     conversation_id: Optional[str] = None):
        """Log an AI query for analytics and billing audit.

        Optional prompt_sha256 / response_sha256 / conversation_id
        let support trace an AI complaint to the exact exchange without
        storing the raw bytes in the DB.
        """
        with self._tx() as conn:
            conn.execute(
                adapt_query('''INSERT INTO ai_usage_log
                   (user_id, feature, asset, input_tokens, output_tokens,
                    cost_cents, charged_cents, model,
                    prompt_sha256, response_sha256, conversation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''),
                (user_id, feature, asset, input_tokens, output_tokens,
                 cost_cents, charged_cents, model,
                 prompt_sha256, response_sha256, conversation_id)
            )

    def record_ai_deposit(self, user_id: int, amount_cents: int,
                          payment_id: str = None, source: str = 'admin',
                          _commit: bool = True) -> int:
        """Record a credit deposit and return deposit ID.

        Args:
            source: Where the deposit came from ('admin', 'stripe', 'paddle', etc.)
            _commit: `False` defers commit to the atomic webhook helper.
        Note: DB column is still named nowpayments_payment_id for backwards compat.
        """
        with self._tx(commit=_commit) as conn:
            cursor = conn.execute(
                adapt_query('''INSERT INTO ai_deposits
                   (user_id, amount_cents, crypto_amount, nowpayments_payment_id)
                   VALUES (?, ?, ?, ?)''') + returning_id(),
                (user_id, amount_cents, source, payment_id)
            )
            deposit_id = get_last_id(cursor)
        return deposit_id

    def process_paddle_event_atomic(self, event_id: str, user_id: int, work_fn) -> bool:
        """Atomically gate-and-run a Paddle webhook handler.

        Pattern (single transaction on the thread-local connection):
            1. SELECT marker → if present, return False (idempotent no-op).
            2. Run `work_fn()`. Every user_db call it makes joins this
               transaction (see `_tx`), reads included, so nothing commits
               until the marker is written.
            3. INSERT the marker.
            4. Commit the whole batch atomically.

        On failure:
            - If `work_fn` raises → rollback (drops marker + all partial work)
              and re-raise. Paddle retry sees no marker, replays cleanly.
            - If marker INSERT raises (concurrent racer beat us) → rollback
              and return False. Loser's work is dropped; winner's stands.

        Returns True if work ran and committed, False if already-processed or
        lost the race. Raises on transient DB errors so the webhook returns
        500 and Paddle retries.
        """
        if not event_id:
            # No event_id — can't enforce idempotency. Caller must reject upstream.
            raise ValueError("event_id required for atomic Paddle event processing")
        with self._tx() as conn:
            # Read-side idempotency check on the same connection.
            row = conn.execute(
                adapt_query('SELECT 1 FROM processed_payments WHERE payment_id = ?'),
                (event_id,),
            ).fetchone()
            if row:
                return False

            self._local.atomic = True
            try:
                work_fn()
            finally:
                self._local.atomic = False

            # Marker INSERT LAST — fails (UniqueViolation/IntegrityError) only if
            # a concurrent worker INSERTed since our SELECT.
            try:
                conn.execute(
                    adapt_query('INSERT INTO processed_payments (payment_id, user_id) VALUES (?, ?)'),
                    (event_id, user_id),
                )
            except handle_integrity_error():
                conn.rollback()  # Drop our work — winner already wrote theirs
                logger.info(f"Paddle event {event_id}: lost concurrent race, no-op")
                return False

            return True

    def has_morning_report_charge_today(self, user_id: int) -> bool:
        """Check if user was already charged for morning report today."""
        try:
            with self._tx() as conn:
                today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                if get_db_type() == 'postgresql':
                    row = conn.execute(
                        '''SELECT 1 FROM ai_usage_log
                           WHERE user_id = %s AND feature = 'morning_report'
                           AND created_at::date = %s::date''',
                        (user_id, today)
                    ).fetchone()
                else:
                    row = conn.execute(
                        '''SELECT 1 FROM ai_usage_log
                           WHERE user_id = ? AND feature = 'morning_report'
                           AND date(created_at) = ?''',
                        (user_id, today)
                    ).fetchone()
                return row is not None
        except Exception as e:
            logger.debug(f"has_morning_report_charge_today error: {e}")
            return False

    def get_monthly_ai_query_count(self, user_id: int) -> int:
        """Count AI queries this calendar month (for monthly cap enforcement)."""
        try:
            with self._tx() as conn:
                if get_db_type() == 'postgresql':
                    row = conn.execute(
                        '''SELECT COUNT(*) as cnt FROM ai_usage_log
                           WHERE user_id = %s
                           AND created_at >= date_trunc('month', CURRENT_TIMESTAMP)''',
                        (user_id,)
                    ).fetchone()
                else:
                    row = conn.execute(
                        '''SELECT COUNT(*) as cnt FROM ai_usage_log
                           WHERE user_id = ?
                           AND created_at >= date('now', 'start of month')''',
                        (user_id,)
                    ).fetchone()
                return row['cnt'] if row else 0
        except Exception as e:
            logger.debug(f"get_monthly_ai_query_count error: {e}")
            return 0

    def get_pattern_match_count_today(self, user_id: int) -> int:
        """Count pattern match queries today (for daily cap enforcement)."""
        try:
            with self._tx() as conn:
                today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                if get_db_type() == 'postgresql':
                    row = conn.execute(
                        '''SELECT COUNT(*) as cnt FROM ai_usage_log
                           WHERE user_id = %s AND feature = 'pattern_match'
                           AND created_at::date = %s::date''',
                        (user_id, today)
                    ).fetchone()
                else:
                    row = conn.execute(
                        '''SELECT COUNT(*) as cnt FROM ai_usage_log
                           WHERE user_id = ? AND feature = 'pattern_match'
                           AND date(created_at) = ?''',
                        (user_id, today)
                    ).fetchone()
                return row['cnt'] if row else 0
        except Exception as e:
            logger.debug(f"get_pattern_match_count_today error: {e}")
            return 0

    def get_ai_usage_stats(self, user_id: int) -> Dict:
        """Get aggregate usage stats for a user."""
        try:
            with self._tx() as conn:
                row = conn.execute(
                    adapt_query('''SELECT
                         COUNT(*) as total_queries,
                         COALESCE(SUM(charged_cents), 0) as total_charged,
                         COALESCE(SUM(input_tokens), 0) as total_input,
                         COALESCE(SUM(output_tokens), 0) as total_output
                       FROM ai_usage_log WHERE user_id = ?'''),
                    (user_id,)
                ).fetchone()
                credits = conn.execute(
                    adapt_query('SELECT balance_cents, total_deposited_cents, total_spent_cents FROM ai_credits WHERE user_id = ?'),
                    (user_id,)
                ).fetchone()
                return {
                    'total_queries': row['total_queries'] if row else 0,
                    'total_charged_cents': row['total_charged'] if row else 0,
                    'balance_cents': credits['balance_cents'] if credits else 0,
                    'total_deposited_cents': credits['total_deposited_cents'] if credits else 0,
                    'total_spent_cents': credits['total_spent_cents'] if credits else 0,
                }
        except Exception as e:
            logger.error(f"get_ai_usage_stats error: {e}")
            return {'total_queries': 0, 'total_charged_cents': 0, 'balance_cents': 0,
                    'total_deposited_cents': 0, 'total_spent_cents': 0}

    # ==================== User Economics ====================

    # Tax multiplier: Anthropic API credits cost more due to tax (e.g. $20 → $25.40 = 1.27x)
    _TAX_MULTIPLIER = float(os.environ.get('AI_COST_TAX_MULTIPLIER', '1.27'))

    def _gross(self, cents: int) -> int:
        """Apply tax multiplier to get gross (out-of-pocket) cost."""
        return round(cents * self._TAX_MULTIPLIER)

    def get_user_economics(self, user_id: int) -> Dict:
        """Get per-user cost vs revenue economics."""
        try:
            with self._tx() as conn:
                cutoff = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')

                row = conn.execute(adapt_query('''
                    SELECT
                        COALESCE(SUM(cost_cents), 0) as cost_lifetime,
                        COALESCE(SUM(charged_cents), 0) as charged_lifetime,
                        COUNT(*) as queries_lifetime,
                        COALESCE(SUM(CASE WHEN created_at >= ? THEN cost_cents ELSE 0 END), 0) as cost_30d,
                        COALESCE(SUM(CASE WHEN created_at >= ? THEN charged_cents ELSE 0 END), 0) as charged_30d,
                        SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) as queries_30d,
                        COALESCE(SUM(CASE WHEN created_at >= ? AND feature = 'morning_personal' THEN cost_cents ELSE 0 END), 0) as report_cost_30d,
                        COALESCE(SUM(CASE WHEN created_at >= ? AND feature NOT LIKE 'morning%%' AND feature NOT LIKE 'engine%%' THEN cost_cents ELSE 0 END), 0) as copilot_cost_30d
                    FROM ai_usage_log WHERE user_id = ?
                '''), (cutoff, cutoff, cutoff, cutoff, cutoff, user_id)).fetchone()

                dep = conn.execute(adapt_query('''
                    SELECT COALESCE(SUM(amount_cents), 0) as total
                    FROM ai_deposits WHERE user_id = ? AND status = 'confirmed'
                '''), (user_id,)).fetchone()

                cost_lt = row['cost_lifetime'] if row else 0
                cost_30 = row['cost_30d'] if row else 0
                revenue = dep['total'] if dep else 0
                gross_lt = self._gross(cost_lt)
                gross_30 = self._gross(cost_30)
                margin = revenue - gross_lt

                return {
                    'cost_cents_30d': cost_30,
                    'cost_cents_lifetime': cost_lt,
                    'gross_cost_cents_30d': gross_30,
                    'gross_cost_cents_lifetime': gross_lt,
                    'charged_cents_30d': row['charged_30d'] if row else 0,
                    'charged_cents_lifetime': row['charged_lifetime'] if row else 0,
                    'queries_30d': row['queries_30d'] if row else 0,
                    'queries_lifetime': row['queries_lifetime'] if row else 0,
                    'topup_revenue_cents': revenue,
                    'margin_cents_lifetime': margin,
                    'margin_pct': round((margin / revenue) * 100, 1) if revenue > 0 else None,
                    'report_cost_cents_30d': self._gross(row['report_cost_30d'] if row else 0),
                    'copilot_cost_cents_30d': self._gross(row['copilot_cost_30d'] if row else 0),
                    'tax_multiplier': self._TAX_MULTIPLIER,
                }
        except Exception as e:
            logger.error(f"get_user_economics error: {e}")
            return {'cost_cents_30d': 0, 'cost_cents_lifetime': 0, 'gross_cost_cents_30d': 0,
                    'gross_cost_cents_lifetime': 0, 'charged_cents_30d': 0, 'charged_cents_lifetime': 0,
                    'queries_30d': 0, 'queries_lifetime': 0, 'topup_revenue_cents': 0,
                    'margin_cents_lifetime': 0, 'margin_pct': None, 'report_cost_cents_30d': 0,
                    'copilot_cost_cents_30d': 0, 'tax_multiplier': self._TAX_MULTIPLIER}

    def get_platform_economics(self, days: int = 30) -> Dict:
        """Get platform-wide cost vs revenue economics."""
        try:
            with self._tx() as conn:
                cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

                row = conn.execute(adapt_query('''
                    SELECT
                        COALESCE(SUM(cost_cents), 0) as total_cost,
                        COALESCE(SUM(charged_cents), 0) as total_charged,
                        COALESCE(SUM(CASE WHEN user_id = 0 THEN cost_cents ELSE 0 END), 0) as platform_cost,
                        COALESCE(SUM(CASE WHEN user_id > 0 THEN cost_cents ELSE 0 END), 0) as user_cost,
                        COALESCE(SUM(CASE WHEN feature LIKE 'morning%%' OR feature LIKE 'engine%%' THEN cost_cents ELSE 0 END), 0) as report_cost,
                        COALESCE(SUM(CASE WHEN feature NOT LIKE 'morning%%' AND feature NOT LIKE 'engine%%' THEN cost_cents ELSE 0 END), 0) as copilot_cost,
                        COUNT(DISTINCT CASE WHEN user_id > 0 THEN user_id END) as active_users
                    FROM ai_usage_log WHERE created_at >= ?
                '''), (cutoff,)).fetchone()

                dep = conn.execute(adapt_query('''
                    SELECT COALESCE(SUM(amount_cents), 0) as total
                    FROM ai_deposits WHERE status = 'confirmed' AND created_at >= ?
                '''), (cutoff,)).fetchone()

                total_cost = row['total_cost'] if row else 0
                platform_cost = row['platform_cost'] if row else 0
                user_cost = row['user_cost'] if row else 0
                revenue = dep['total'] if dep else 0
                gross_total = self._gross(total_cost)

                return {
                    'total_cost_cents': total_cost,
                    'total_gross_cost_cents': gross_total,
                    'platform_cost_cents': platform_cost,
                    'platform_gross_cost_cents': self._gross(platform_cost),
                    'user_cost_cents': user_cost,
                    'user_gross_cost_cents': self._gross(user_cost),
                    'total_charged_cents': row['total_charged'] if row else 0,
                    'total_topup_revenue_cents': revenue,
                    'active_users': row['active_users'] if row else 0,
                    'report_cost_cents': row['report_cost'] if row else 0,
                    'copilot_cost_cents': row['copilot_cost'] if row else 0,
                    'margin_cents': revenue - gross_total,
                    'tax_multiplier': self._TAX_MULTIPLIER,
                }
        except Exception as e:
            logger.error(f"get_platform_economics error: {e}")
            return {'total_cost_cents': 0, 'total_gross_cost_cents': 0, 'platform_cost_cents': 0,
                    'platform_gross_cost_cents': 0, 'user_cost_cents': 0, 'user_gross_cost_cents': 0,
                    'total_charged_cents': 0, 'total_topup_revenue_cents': 0, 'active_users': 0,
                    'report_cost_cents': 0, 'copilot_cost_cents': 0, 'margin_cents': 0,
                    'tax_multiplier': self._TAX_MULTIPLIER}

    def get_top_cost_users(self, days: int = 30, limit: int = 15) -> List[Dict]:
        """Get users sorted by highest AI cost in period."""
        try:
            with self._tx() as conn:
                cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

                rows = conn.execute(adapt_query('''
                    SELECT u.id as user_id, u.email, u.tier,
                           COALESCE(SUM(l.cost_cents), 0) as total_cost_cents,
                           COALESCE(SUM(l.charged_cents), 0) as total_charged_cents,
                           COALESCE(c.total_deposited_cents, 0) as total_deposited_cents
                    FROM users u
                    JOIN ai_usage_log l ON l.user_id = u.id
                    LEFT JOIN ai_credits c ON c.user_id = u.id
                    WHERE l.created_at >= ? AND u.id > 0
                    GROUP BY u.id, u.email, u.tier, c.total_deposited_cents
                    ORDER BY total_cost_cents DESC
                    LIMIT ?
                '''), (cutoff, limit)).fetchall()

                result = []
                for r in rows:
                    gross = self._gross(r['total_cost_cents'])
                    result.append({
                        'user_id': r['user_id'],
                        'email': r['email'],
                        'tier': r['tier'],
                        'total_cost_cents': r['total_cost_cents'],
                        'total_gross_cost_cents': gross,
                        'total_charged_cents': r['total_charged_cents'],
                        'total_deposited_cents': r['total_deposited_cents'],
                        'margin_cents': r['total_deposited_cents'] - gross,
                    })
                return result
        except Exception as e:
            logger.error(f"get_top_cost_users error: {e}")
            return []

    def get_tier_economics(self, days: int = 30) -> List[Dict]:
        """Get per-tier aggregate economics."""
        try:
            with self._tx() as conn:
                cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

                rows = conn.execute(adapt_query('''
                    SELECT u.tier,
                           COUNT(DISTINCT u.id) as user_count,
                           COALESCE(SUM(l.cost_cents), 0) as total_cost_cents,
                           COALESCE(SUM(l.charged_cents), 0) as total_charged_cents
                    FROM users u
                    LEFT JOIN ai_usage_log l ON l.user_id = u.id AND l.created_at >= ?
                    WHERE u.id > 0
                    GROUP BY u.tier
                '''), (cutoff,)).fetchall()

                result = []
                for r in rows:
                    count = r['user_count'] or 1
                    cost = r['total_cost_cents']
                    result.append({
                        'tier': r['tier'],
                        'user_count': r['user_count'],
                        'total_cost_cents': cost,
                        'total_gross_cost_cents': self._gross(cost),
                        'total_charged_cents': r['total_charged_cents'],
                        'avg_gross_cost_per_user': self._gross(cost) // count,
                    })
                return result
        except Exception as e:
            logger.error(f"get_tier_economics error: {e}")
            return []

    # ==================== AI Conversations ====================

    def create_conversation(self, user_id: int, title: str = 'New Chat',
                            primary_asset: str = None, timeframe: str = None,
                            page_context: str = None) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                adapt_query('''INSERT INTO ai_conversations
                    (user_id, title, primary_asset, timeframe, page_context)
                    VALUES (?, ?, ?, ?, ?)''') + returning_id(),
                (user_id, title[:100], primary_asset, timeframe, page_context)
            )
            conv_id = get_last_id(cursor)
        # Retention pruning is not done on the write path: tying it to write
        # volume means it never fires on a low-traffic deployment (conversations
        # outlived the stated 90-day window). It runs on the worker's daily
        # retention schedule (worker.py::_run_daily_retention) instead, whether
        # or not anyone is writing.
        return conv_id

    def get_conversations(self, user_id: int, limit: int = 20) -> list:
        try:
            with self._tx() as conn:
                rows = conn.execute(
                    adapt_query('''SELECT id, title, primary_asset, timeframe, page_context,
                       message_count, created_at, updated_at FROM ai_conversations
                       WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?'''),
                    (user_id, limit)
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"get_conversations error: {e}")
            return []

    def add_message(self, conversation_id: int, role: str, content: str,
                    feature: str = None, token_count: int = None,
                    user_id: int = None) -> int:
        with self._tx() as conn:
            # Verify ownership if user_id provided
            if user_id is not None:
                owner = conn.execute(
                    adapt_query('SELECT user_id FROM ai_conversations WHERE id = ?'),
                    (conversation_id,)
                ).fetchone()
                if not owner or owner['user_id'] != user_id:
                    return -1
            cursor = conn.execute(
                adapt_query('''INSERT INTO ai_messages (conversation_id, role, content, feature, token_count)
                   VALUES (?, ?, ?, ?, ?)''') + returning_id(),
                (conversation_id, role, content[:10000], feature, token_count)
            )
            msg_id = get_last_id(cursor)
            conn.execute(
                adapt_query('''UPDATE ai_conversations SET updated_at = CURRENT_TIMESTAMP,
                   message_count = COALESCE(message_count, 0) + 1 WHERE id = ?'''),
                (conversation_id,)
            )
        return msg_id

    def get_messages(self, conversation_id: int, user_id: int, limit: int = 50) -> list:
        try:
            with self._tx() as conn:
                # Verify ownership
                owner = conn.execute(
                    adapt_query('SELECT user_id FROM ai_conversations WHERE id = ?'),
                    (conversation_id,)
                ).fetchone()
                if not owner or owner['user_id'] != user_id:
                    return []
                rows = conn.execute(
                    adapt_query('''SELECT id, role, content, feature, created_at FROM ai_messages
                       WHERE conversation_id = ? ORDER BY created_at ASC LIMIT ?'''),
                    (conversation_id, limit)
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"get_messages error: {e}")
            return []

    def delete_conversation(self, conversation_id: int, user_id: int) -> bool:
        with self._tx() as conn:
            owner = conn.execute(
                adapt_query('SELECT user_id FROM ai_conversations WHERE id = ?'),
                (conversation_id,)
            ).fetchone()
            if not owner or owner['user_id'] != user_id:
                return False
            conn.execute(adapt_query('DELETE FROM ai_messages WHERE conversation_id = ?'), (conversation_id,))
            conn.execute(adapt_query('DELETE FROM ai_conversations WHERE id = ?'), (conversation_id,))
        return True

    def auto_title_conversation(self, conversation_id: int, user_id: int):
        """Set conversation title from first user message (truncated)."""
        with self._tx() as conn:
            owner = conn.execute(
                adapt_query('SELECT user_id FROM ai_conversations WHERE id = ?'),
                (conversation_id,)
            ).fetchone()
            if not owner or owner['user_id'] != user_id:
                return
            first = conn.execute(
                adapt_query('''SELECT content FROM ai_messages
                   WHERE conversation_id = ? AND role = 'user'
                   ORDER BY created_at ASC LIMIT 1'''),
                (conversation_id,)
            ).fetchone()
            if first:
                title = first['content'][:40].replace('\n', ' ')
                if len(first['content']) > 40:
                    title += '...'
                conn.execute(
                    adapt_query('UPDATE ai_conversations SET title = ? WHERE id = ?'),
                    (title, conversation_id)
                )

    def get_conversation_meta(self, conversation_id: int, user_id: int):
        """Return conversation metadata (asset, timeframe, page_context, message_count, summary)."""
        try:
            with self._tx() as conn:
                row = conn.execute(
                    adapt_query('''SELECT id, user_id, title, primary_asset, timeframe, page_context,
                       summary, message_count, created_at, updated_at
                       FROM ai_conversations WHERE id = ? AND user_id = ?'''),
                    (conversation_id, user_id)
                ).fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"get_conversation_meta error: {e}")
            return None

    def update_conversation_summary(self, conversation_id: int, summary: str) -> bool:
        """Store a compressed conversation summary."""
        with self._tx() as conn:
            conn.execute(
                adapt_query('UPDATE ai_conversations SET summary = ? WHERE id = ?'),
                (summary[:5000], conversation_id)
            )
        return True

    def prune_old_conversations(self, days: int = 90) -> int:
        """Delete conversations older than N days. Returns count deleted."""
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        with self._tx() as conn:
            conn.execute(
                adapt_query('''DELETE FROM ai_messages WHERE conversation_id IN
                   (SELECT id FROM ai_conversations WHERE updated_at < ?)'''),
                (cutoff,)
            )
            cursor = conn.execute(
                adapt_query('DELETE FROM ai_conversations WHERE updated_at < ?'),
                (cutoff,)
            )
            count = cursor.rowcount
        if count > 0:
            logger.info(f"Pruned {count} conversations older than {days} days")
        return count


    # ==================== Widget Tokens ====================

    def create_widget_token(self, user_id: int, token_hash: str, token_suffix: str,
                            name: str = 'Default') -> Optional[int]:
        """Create a widget token. Returns token ID or None."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                # Enforce max tokens per user
                cursor.execute(
                    adapt_query('SELECT COUNT(*) as cnt FROM widget_tokens WHERE user_id = ? AND revoked = FALSE'),
                    (user_id,)
                )
                if cursor.fetchone()['cnt'] >= 5:
                    return None

                cursor.execute(adapt_query('''
                    INSERT INTO widget_tokens (user_id, token_hash, token_suffix, name)
                    VALUES (?, ?, ?, ?)
                ''') + returning_id(), (user_id, token_hash, token_suffix, name[:50]))
                token_id = get_last_id(cursor)
                return token_id
        except handle_integrity_error():
            return None
        except Exception as e:
            logger.error(f"Error creating widget token: {e}")
            return None

    def verify_widget_token(self, token_hash: str) -> Optional[Dict]:
        """Verify a widget token by its SHA-256 hash. Returns user info or None."""
        try:
            with self._tx() as conn:
                row = conn.execute(adapt_query('''
                    SELECT wt.id, wt.user_id, wt.name, wt.revoked,
                           u.email, u.is_active, u.tier
                    FROM widget_tokens wt
                    JOIN users u ON wt.user_id = u.id
                    WHERE wt.token_hash = ?
                '''), (token_hash,)).fetchone()
        except Exception as e:
            logger.error(f"Error verifying widget token: {e}")
            return None
        if not row or row['revoked'] or not row['is_active']:
            return None

        # Best-effort: a missed last_used_at stamp must not fail the widget request.
        try:
            with self._tx() as conn:
                conn.execute(
                    adapt_query('UPDATE widget_tokens SET last_used_at = ? WHERE id = ?'),
                    (datetime.now(), row['id'])
                )
        except Exception as e:
            logger.warning("widget_tokens.last_used_at stamp failed for token %s: %s", row['id'], e)
        return dict(row)

    def list_widget_tokens(self, user_id: int) -> List[Dict]:
        """List all widget tokens for a user (suffix only, not full token)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    SELECT id, token_suffix, name, created_at, last_used_at, revoked
                    FROM widget_tokens WHERE user_id = ?
                    ORDER BY created_at DESC
                '''), (user_id,))
                rows = [dict(row) for row in cursor.fetchall()]
                return rows
        except Exception as e:
            logger.error(f"Error listing widget tokens: {e}")
            return []

    def revoke_widget_token(self, token_id: int, user_id: int) -> bool:
        """Revoke a widget token. Ownership check via user_id."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    adapt_query('UPDATE widget_tokens SET revoked = TRUE WHERE id = ? AND user_id = ?'),
                    (token_id, user_id)
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error revoking widget token: {e}")
            return False


    # ==================== API Keys ====================

    def create_api_key(self, user_id: int, token_hash: str, token_suffix: str,
                       name: str = 'Default', scope: str = 'full',
                       expires_at=None, is_internal: bool = False) -> Optional[int]:
        """Create an API key. Returns key ID or None (limit reached / duplicate).

        is_internal: metering-exclusion flag for automated clients. Defaults False for
        every normal self-service key; only ever set True via a one-off DB action on
        a specific key (never exposed through the self-service /api/keys route).
        """
        try:
            with self._tx() as conn:
                cursor = conn.cursor()

                cursor.execute(
                    adapt_query('SELECT COUNT(*) as cnt FROM api_keys WHERE user_id = ? AND NOT revoked'),
                    (user_id,)
                )
                if cursor.fetchone()['cnt'] >= 5:
                    return None

                cursor.execute(adapt_query('''
                    INSERT INTO api_keys (user_id, token_hash, token_suffix, name, scope, expires_at, is_internal)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''') + returning_id(), (user_id, token_hash, token_suffix, name[:50], scope, expires_at, is_internal))
                key_id = get_last_id(cursor)
                return key_id
        except handle_integrity_error():
            return None
        except Exception as e:
            logger.error(f"Error creating API key: {e}")
            return None

    def verify_api_key_token(self, token_hash: str) -> Optional[Dict]:
        """Verify a per-user API key by hash. Returns user info with tier or None.

        On PostgreSQL this borrows a pool connection for the one query and
        returns it straight away, instead of the thread-local connection:
        classify_request runs in run_in_threadpool, and a thread-local
        connection per thread-pool thread would hold a pool slot each and
        exhaust the pool under bursty API-key traffic. SQLite (dev/test) uses
        the thread-local connection, which points at this database's file.
        """
        if get_db_type() == 'postgresql':
            from carrac.db_pool import get_conn as transaction
        else:
            transaction = self._tx
        try:
            with transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    SELECT ak.id, ak.user_id, ak.name, ak.scope, ak.expires_at, ak.revoked,
                           ak.is_internal,
                           u.email, u.is_active, u.tier, u.is_admin
                    FROM api_keys ak
                    JOIN users u ON ak.user_id = u.id
                    WHERE ak.token_hash = ?
                '''), (token_hash,))
                row = cursor.fetchone()
                if not row or row['revoked'] or not row['is_active']:
                    return None

                # Check expiry
                expires = row['expires_at']
                if expires:
                    if isinstance(expires, str):
                        try:
                            expires = datetime.fromisoformat(expires)
                        except Exception:
                            pass
                    if isinstance(expires, datetime):
                        now = datetime.now(timezone.utc) if expires.tzinfo else datetime.now()
                        if now > expires:
                            return None

                # Stamp last_used_at, throttled to once per hour so the hot
                # path does not write on every request.
                try:
                    cursor.execute(*_last_used_stamp_sql(row['id']))
                except Exception as e:
                    # Non-fatal for auth, but it must reach the error tracker:
                    # a silent pass would leave the column NULL with no signal.
                    logger.error(f"api_keys.last_used_at stamp failed for key {row['id']}: {e}")

                result = dict(row)
                # Compute effective tier
                result['tier'] = 'admin' if row['is_admin'] else (row['tier'] or 'free')
                result['is_internal'] = bool(row['is_internal'])
                return result
        except Exception as e:
            logger.error(f"Error verifying API key: {e}")
            return None

    def list_api_keys(self, user_id: int) -> List[Dict]:
        """List all API keys for a user (suffix only, never full key)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    SELECT id, token_suffix, name, scope, expires_at,
                           created_at, last_used_at, revoked
                    FROM api_keys WHERE user_id = ?
                    ORDER BY created_at DESC
                '''), (user_id,))
                rows = [dict(row) for row in cursor.fetchall()]
                return rows
        except Exception as e:
            logger.error(f"Error listing API keys: {e}")
            return []

    def revoke_api_key(self, key_id: int, user_id: int) -> bool:
        """Revoke an API key. Ownership check via user_id."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    adapt_query('UPDATE api_keys SET revoked = true WHERE id = ? AND user_id = ?'),
                    (key_id, user_id)
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error revoking API key: {e}")
            return False

    # ==================== Password Reset Tokens ====================

    def create_password_reset_token(self, user_id: int, token_hash: str, ip_address: str = None) -> bool:
        """Create a password reset token (invalidates previous ones for this user)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                # Invalidate all previous tokens for this user
                cursor.execute(adapt_query(
                    'UPDATE password_reset_tokens SET used = TRUE WHERE user_id = ? AND used = FALSE'
                ), (user_id,))
                # Insert new token
                expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
                cursor.execute(adapt_query('''
                    INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, ip_address)
                    VALUES (?, ?, ?, ?)
                '''), (user_id, token_hash, expires_at, ip_address))
                return True
        except Exception as e:
            logger.error(f"Error creating password reset token: {e}")
            return False

    def validate_password_reset_token(self, token_hash: str) -> Optional[Dict]:
        """Validate a password reset token. Returns user info or None."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    SELECT prt.id, prt.user_id, prt.token_hash, prt.expires_at, prt.used,
                           u.email, u.is_active
                    FROM password_reset_tokens prt
                    JOIN users u ON prt.user_id = u.id
                    WHERE prt.token_hash = ? AND prt.used = FALSE
                '''), (token_hash,))
                row = cursor.fetchone()
                if not row:
                    return None
                row = dict(row)
                # Check expiry
                expires_at = row['expires_at']
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                if hasattr(expires_at, 'tzinfo') and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires_at:
                    return None
                if not row['is_active']:
                    return None
                return row
        except Exception as e:
            logger.error(f"Error validating password reset token: {e}")
            return None

    def use_password_reset_token(self, token_hash: str) -> bool:
        """Mark a password reset token as used.

        The ``AND used = FALSE`` guard makes this idempotent: a second call for
        an already-consumed token updates zero rows and returns False, so the
        return value is a truthful "I consumed a live token" signal (defence-in-depth —
        the single-use gate itself is validate_*'s ``used = FALSE`` filter).
        """
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'UPDATE password_reset_tokens SET used = TRUE WHERE token_hash = ? AND used = FALSE'
                ), (token_hash,))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error using password reset token: {e}")
            return False

    def invalidate_password_reset_tokens(self, user_id: int) -> bool:
        """Invalidate all password reset tokens for a user."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'UPDATE password_reset_tokens SET used = TRUE WHERE user_id = ? AND used = FALSE'
                ), (user_id,))
                return True
        except Exception as e:
            logger.error(f"Error invalidating password reset tokens: {e}")
            return False

    # ==================== Email Verification ====================

    def create_email_verification_token(self, user_id: int, token_hash: str) -> bool:
        """Create an email verification token.

        Earlier unexpired tokens stay valid: marking them used first would
        make a "resend" click kill the link already in the inbox, and with the
        resend limiter at 2/hour a user who opens the first mail would get
        "invalid or expired" with no way back. Every link mailed inside the
        24 h window works until one is used — set_email_verified() then
        retires all of them.
        """
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
                cursor.execute(adapt_query('''
                    INSERT INTO email_verifications (user_id, token_hash, expires_at)
                    VALUES (?, ?, ?)
                '''), (user_id, token_hash, expires_at))
                return True
        except Exception as e:
            logger.error(f"Error creating email verification token: {e}")
            return False

    def validate_email_verification_token(self, token_hash: str) -> Optional[Dict]:
        """Validate an email verification token. Returns user info or None."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    SELECT ev.id, ev.user_id, ev.token_hash, ev.expires_at, ev.used,
                           u.email, u.is_active
                    FROM email_verifications ev
                    JOIN users u ON ev.user_id = u.id
                    WHERE ev.token_hash = ? AND ev.used = FALSE
                '''), (token_hash,))
                row = cursor.fetchone()
                if not row:
                    return None
                row = dict(row)
                expires_at = row['expires_at']
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                if hasattr(expires_at, 'tzinfo') and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expires_at:
                    return None
                return row
        except Exception as e:
            logger.error(f"Error validating email verification token: {e}")
            return None

    def set_email_verified(self, user_id: int) -> bool:
        """Mark a user's email as verified."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'UPDATE users SET email_verified = TRUE WHERE id = ?'
                ), (user_id,))
                updated = cursor.rowcount > 0
                # Mark all verification tokens as used
                cursor.execute(adapt_query(
                    'UPDATE email_verifications SET used = TRUE WHERE user_id = ?'
                ), (user_id,))
                return updated
        except Exception as e:
            logger.error(f"Error setting email verified: {e}")
            return False

    def is_email_verified(self, user_id: int) -> bool:
        """Check if a user's email is verified."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'SELECT email_verified FROM users WHERE id = ?'
                ), (user_id,))
                row = cursor.fetchone()
                return bool(row['email_verified']) if row else False
        except Exception:
            return False

    # ==================== Account Deletion & Export ====================

    def verify_user_password(self, user_id: int, password: str) -> bool:
        """Verify a user's password by user ID."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query(
                    'SELECT password_hash FROM users WHERE id = ?'
                ), (user_id,))
                row = cursor.fetchone()
                if not row:
                    return False
                stored_hash = row['password_hash']
                if stored_hash.startswith(('pbkdf2:', 'scrypt:')):
                    return check_password_hash(stored_hash, password)
                try:
                    return _ph.verify(stored_hash, password)
                except VerifyMismatchError:
                    return False
        except Exception:
            return False

    def export_user_data(self, user_id: int) -> Dict:
        """Export all user data for GDPR compliance. Returns dict of all user-owned data."""
        with self._tx() as conn:
            cursor = conn.cursor()
            data = {'exported_at': datetime.now(timezone.utc).isoformat()}

            is_pg = get_db_type() == 'postgresql'
            export_errors: list = []

            def _export_failed(name, exc):
                """Handle a failed section read.

                Two jobs, and the first one is why the export was far more broken on
                Postgres than first assumed. Postgres aborts the WHOLE
                transaction on any failed statement, so a bare `except: data[x] = []`
                leaves the connection poisoned and EVERY LATER READ returns nothing —
                silently, and looking exactly like "this user had no data". Observed
                in testing: one early failure emptied all twelve sections after it.
                Rolling back clears the aborted state so the rest of the export
                still runs. Safe because this function only reads.

                Second job: make it visible. An empty section is indistinguishable
                from an empty account, which is precisely how this class of bug
                survives. ERROR level so Sentry turns it into an error-tracker event, plus
                a record that goes into the user's own file.
                """
                if is_pg:
                    conn.rollback()
                export_errors.append(name)
                logger.error(
                    "GDPR export: %s failed for user %s — the download is "
                    "INCOMPLETE: %s", name, user_id, exc)

            # User profile (no password_hash)
            cursor.execute(adapt_query('''
                SELECT id, email, display_name, is_admin, is_active, tier,
                       created_at, last_login, last_active, email_verified,
                       tos_accepted_at, tos_accepted_ip
                FROM users WHERE id = ?
            '''), (user_id,))
            row = cursor.fetchone()
            if row:
                data['profile'] = {k: str(v) if v is not None else None for k, v in dict(row).items()}

            # User settings
            cursor.execute(adapt_query(
                'SELECT watchlist_data, preferences, updated_at FROM user_settings WHERE user_id = ?'
            ), (user_id,))
            row = cursor.fetchone()
            if row:
                row = dict(row)
                data['settings'] = {
                    'watchlist': json.loads(row.get('watchlist_data') or '{}'),
                    'preferences': json.loads(row.get('preferences') or '{}'),
                }

            # Drawings
            try:
                # This asked for `drawing_data`, a column that does not
                # exist — the geometry lives in price_level/price_level_upper/
                # metadata. So EVERY export has been missing every chart drawing,
                # and on Postgres that failure aborted the transaction and emptied
                # every section after it too. SELECT * for the same reason as the
                # content tables below: drawings are pure user content with no
                # secret columns, and naming columns is what broke this.
                cursor.execute(adapt_query(
                    'SELECT * FROM drawings WHERE user_id = ?'
                ), (user_id,))
                data['drawings'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['drawings'] = []
                _export_failed('drawings', exc)

            # Scenarios
            try:
                cursor.execute(adapt_query(
                    'SELECT id, asset, timeframe, scenario_type, label, notes, is_active, created_at, updated_at FROM scenarios WHERE user_id = ?'
                ), (user_id,))
                data['scenarios'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['scenarios'] = []
                _export_failed('scenarios', exc)

            # Trade journal
            try:
                cursor.execute(adapt_query(
                    'SELECT * FROM trade_journal WHERE user_id = ?'
                ), (user_id,))
                data['trade_journal'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['trade_journal'] = []
                _export_failed('trade_journal', exc)

            # Journal sessions
            try:
                cursor.execute(adapt_query(
                    'SELECT * FROM journal_sessions WHERE user_id = ?'
                ), (user_id,))
                data['journal_sessions'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['journal_sessions'] = []
                _export_failed('journal_sessions', exc)

            # Journal playbooks
            try:
                cursor.execute(adapt_query(
                    'SELECT * FROM journal_playbooks WHERE user_id = ?'
                ), (user_id,))
                data['journal_playbooks'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['journal_playbooks'] = []
                _export_failed('journal_playbooks', exc)

            # Journal views
            try:
                cursor.execute(adapt_query(
                    'SELECT * FROM journal_views WHERE user_id = ?'
                ), (user_id,))
                data['journal_views'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['journal_views'] = []
                _export_failed('journal_views', exc)

            # Journal goals (was querying the dead legacy name
            # 'monthly_goals' — the live table is journal_goals, see
            # carrac/content_db.py)
            try:
                cursor.execute(adapt_query(
                    'SELECT * FROM journal_goals WHERE user_id = ?'
                ), (user_id,))
                data['journal_goals'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['journal_goals'] = []
                _export_failed('journal_goals', exc)

            # Checklist templates
            try:
                cursor.execute(adapt_query(
                    'SELECT * FROM checklist_templates WHERE user_id = ?'
                ), (user_id,))
                data['checklist_templates'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['checklist_templates'] = []
                _export_failed('checklist_templates', exc)

            # Setup checklists
            try:
                cursor.execute(adapt_query(
                    'SELECT * FROM setup_checklists WHERE user_id = ?'
                ), (user_id,))
                data['setup_checklists'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['setup_checklists'] = []
                _export_failed('setup_checklists', exc)

            # AI conversations + messages
            try:
                cursor.execute(adapt_query(
                    'SELECT * FROM ai_conversations WHERE user_id = ?'
                ), (user_id,))
                convos = [dict(r) for r in cursor.fetchall()]
                for c in convos:
                    cursor.execute(adapt_query(
                        'SELECT role, content, feature, created_at FROM ai_messages WHERE conversation_id = ? ORDER BY created_at'
                    ), (c['id'],))
                    c['messages'] = [dict(r) for r in cursor.fetchall()]
                data['ai_conversations'] = convos
            except Exception as exc:
                data['ai_conversations'] = []
                _export_failed('ai_conversations', exc)

            # AI usage log
            try:
                cursor.execute(adapt_query(
                    'SELECT feature, input_tokens, output_tokens, cost_cents, model, created_at FROM ai_usage_log WHERE user_id = ? ORDER BY created_at'
                ), (user_id,))
                data['ai_usage'] = [dict(r) for r in cursor.fetchall()]
            except Exception:
                # Try alternate table name (PostgreSQL uses ai_usage). A failed
                # statement aborts the whole PostgreSQL transaction, so clear
                # that state before the retry.
                if is_pg:
                    conn.rollback()
                try:
                    cursor.execute(adapt_query(
                        'SELECT feature, input_tokens, output_tokens, cost_cents, model, created_at FROM ai_usage WHERE user_id = ? ORDER BY created_at'
                    ), (user_id,))
                    data['ai_usage'] = [dict(r) for r in cursor.fetchall()]
                except Exception as exc:
                    data['ai_usage'] = []
                    _export_failed('ai_usage', exc)

            # AI credits
            try:
                cursor.execute(adapt_query(
                    'SELECT balance_cents, total_deposited_cents, total_spent_cents FROM ai_credits WHERE user_id = ?'
                ), (user_id,))
                row = cursor.fetchone()
                data['ai_credits'] = dict(row) if row else {}
            except Exception as exc:
                data['ai_credits'] = {}
                _export_failed('ai_credits', exc)

            # Widget tokens (names only, not hashes)
            try:
                cursor.execute(adapt_query(
                    'SELECT name, token_suffix, created_at, last_used_at FROM widget_tokens WHERE user_id = ? AND revoked = FALSE'
                ), (user_id,))
                data['widget_tokens'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['widget_tokens'] = []
                _export_failed('widget_tokens', exc)

            # Feedback
            try:
                cursor.execute(adapt_query(
                    'SELECT type, area, description, expected, actual, how_should_work, status, created_at FROM feedback WHERE user_id = ?'
                ), (user_id,))
                data['feedback'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['feedback'] = []
                _export_failed('feedback', exc)

            # Custom formulas
            try:
                cursor.execute(adapt_query(
                    'SELECT id, name, formula, style, color, pane, line_width, created_at, updated_at FROM custom_formulas WHERE user_id = ?'
                ), (user_id,))
                data['custom_formulas'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['custom_formulas'] = []
                _export_failed('custom_formulas', exc)

            # ── The remaining user-owned tables ────────────────────
            #
            # Export read 18 of the 38 user-keyed tables, so a user exercising
            # their Art. 15 right of access received a file with no plays, no
            # strategies and no backtests — the things they would most want if
            # they were leaving. Same drift as the erasure gap: both
            # were correct when written, then features landed and nobody revisited.
            #
            # SELECT * is deliberate here. For user-authored content Art. 15 wants
            # completeness, and a column added later is then included automatically
            # instead of being silently omitted — which is the exact failure this
            # export exists to prevent. The one table where that would be dangerous is
            # handled separately below.
            #
            # tests/test_export_coverage.py fails on any user-keyed table that is
            # neither exported nor explicitly export-exempt, so the next table
            # added cannot go missing quietly.
            export_content_tables = [
                'plays', 'play_shares', 'backtest_runs', 'strategies',
                'context_recipes', 'score_snapshots', 'monthly_goals',
                'journal_screenshots', 'personal_report_sections',
                'satisfaction_ratings', 'exchange_import_suggestions',
                'user_alerts', 'push_subscriptions',
            ]

            for table in export_content_tables:
                try:
                    cursor.execute(adapt_query(
                        f'SELECT * FROM {table} WHERE user_id = ?'), (user_id,))
                    data[table] = [dict(r) for r in cursor.fetchall()]
                except Exception as exc:
                    data[table] = []
                    _export_failed(table, exc)

            # exchange_connections is the one table that is both exportable and
            # dangerous. The user is entitled to know which exchanges they linked
            # and how the sync is going; nobody is entitled to credentials_encrypted.
            # Columns are named explicitly — a SELECT * here would hand out the
            # encrypted API keys and wallet material that an earlier release spent
            # removing from account deletion.
            try:
                cursor.execute(adapt_query(
                    'SELECT id, exchange, label, masked_identifier, status, '
                    'consecutive_errors, last_error, last_sync_at, '
                    'last_sync_trades_imported, created_at, updated_at '
                    'FROM exchange_connections WHERE user_id = ?'), (user_id,))
                data['exchange_connections'] = [dict(r) for r in cursor.fetchall()]
            except Exception as exc:
                data['exchange_connections'] = []
                _export_failed('exchange_connections', exc)

            if export_errors:
                data['_incomplete'] = {
                    'tables_that_failed': sorted(export_errors),
                    'note': ('Some sections could not be read when this export ran. '
                             'They are empty above, which does not mean you had no '
                             'data in them. Please contact ' + support_email() + ' and '
                             'quote this list.'),
                }

        # Serialize any datetime objects to strings
        return json.loads(json.dumps(data, default=str))

    def anonymize_user(self, user_id: int) -> bool:
        """Anonymize a user account (GDPR deletion). Keeps id for referential integrity."""
        # Read the address BEFORE the UPDATE scrambles it. Someone
        # who joined the waitlist and later signed up has the same email sitting
        # in `waitlist` and in the JSONL mirror, neither of which is keyed to a
        # user id — so erasing the account leaves both behind unless it is done
        # here, by address, while the address is still known.
        original_email = ''
        email_preread_failure = None
        try:
            _u = self.get_user_by_id(user_id) or {}
            original_email = (_u.get('email') or '').strip().lower()
        except Exception as e:
            # Recorded and joined to
            # erasure_failures so the caller 500s instead of confirming a
            # deletion that left the address behind. A pre-read that succeeds
            # and finds no address is still fine — nothing to scrub.
            email_preread_failure = (
                f'waitlist-preread: {type(e).__name__}: {e}')

        try:
            # One transaction for the row scramble and every DELETE. It commits
            # even when some tables failed: a partial erasure is better than
            # none, and rolling back would RESTORE data the user asked us to
            # delete. The failures are reported below.
            with self._tx() as conn:
                cursor = conn.cursor()

                # Anonymize the user row
                cursor.execute(adapt_query('''
                    UPDATE users SET
                        email = ?,
                        display_name = 'Deleted User',
                        password_hash = '',
                        is_active = FALSE,
                        deleted_at = ?,
                        email_verified = FALSE,
                        trader_profile = NULL,
                        tos_accepted_ip = NULL,
                        tos_accepted_ua = NULL,
                        age_attested_ip = NULL,
                        age_attested_at = NULL
                    WHERE id = ?
                '''), (f'deleted-{user_id}@deleted.carrac.cc', datetime.now(timezone.utc), user_id))

                # Delete user data from all tables. auth_providers is included so
                # that, once social login is connected, a deleted user cannot sign
                # back in through a linked provider account.
                #
                # This list must cover every user_id-bearing table.
                # anonymize_user UPDATEs (never DELETEs) the users row, so
                # ON DELETE CASCADE never fires — a table missing from this list
                # keeps the "erased" user's personal data forever. journal_goals
                # is the real table name (see carrac/content_db.py).
                tables_to_clear = [
                    'user_settings', 'drawings', 'scenarios',
                    'trade_journal', 'journal_sessions', 'journal_screenshots',
                    'journal_playbooks', 'journal_views', 'journal_goals',
                    'checklist_templates', 'setup_checklists',
                    'ai_conversations', 'ai_credits', 'ai_deposits',
                    'widget_tokens', 'api_keys', 'feedback', 'password_reset_tokens',
                    'email_verifications', 'onboarding',
                    'auth_providers',
                    'plays', 'play_shares', 'backtest_runs',
                    'satisfaction_ratings', 'user_tier_overrides', 'custom_formulas',
                    'strategies', 'context_recipes', 'personal_report_sections',
                    'score_snapshots',
                    # exchange_connections stores ENCRYPTED API
                    # credentials and a Hyperliquid wallet address. Anonymising
                    # the user row does not neutralise a stored key, so these are
                    # deleted, never merely detached. monthly_goals is the sibling
                    # of journal_goals (already covered).
                    # tests/test_erasure_coverage.py fails on any user-keyed
                    # table that has no erasure decision, so a newly added table
                    # cannot slip through.
                    'exchange_connections', 'exchange_import_suggestions',
                    'monthly_goals', 'user_alerts', 'push_subscriptions',
                ]
                # A DELETE that swallowed its exception would let a partial
                # erasure return True and log "all personal data deleted". Collect
                # real failures and report them: an erasure that silently
                # half-happened is a swallowed exception on a path with legal
                # weight. A genuinely ABSENT table is not a failure — several
                # tables in this list are created by other modules against their own
                # SQLite file in dev/test.
                erasure_failures: List[str] = []

                def _record_failure(table_name: str, exc: Exception) -> None:
                    msg = str(exc).lower()
                    if 'no such table' in msg or 'does not exist' in msg:
                        return  # nothing there to erase
                    if 'undefinedtable' in type(exc).__name__.lower():
                        return
                    erasure_failures.append(f'{table_name}: {type(exc).__name__}: {exc}')

                is_postgres = get_db_type() == 'postgresql'
                for table in tables_to_clear:
                    if is_postgres:
                        # On Postgres, a single failed statement (e.g. DELETE
                        # against a table that doesn't exist in some environment)
                        # aborts the WHOLE transaction — every later statement
                        # would silently no-op until a rollback. A bare
                        # try/except here isn't enough: the exception is
                        # swallowed, but the connection stays poisoned. Isolate
                        # each table in its own SAVEPOINT so one bad/missing
                        # table can't take down the rest of the erasure.
                        try:
                            cursor.execute('SAVEPOINT anon_tbl')
                            cursor.execute(adapt_query(f'DELETE FROM {table} WHERE user_id = ?'), (user_id,))
                            cursor.execute('RELEASE SAVEPOINT anon_tbl')
                        except Exception as e:
                            _record_failure(table, e)
                            try:
                                cursor.execute('ROLLBACK TO SAVEPOINT anon_tbl')
                                cursor.execute('RELEASE SAVEPOINT anon_tbl')
                            except Exception:
                                pass
                    else:
                        try:
                            cursor.execute(adapt_query(f'DELETE FROM {table} WHERE user_id = ?'), (user_id,))
                        except Exception as e:
                            _record_failure(table, e)  # absent table is tolerated inside

                # Delete AI messages (cascade via conversation)
                try:
                    cursor.execute(adapt_query('''
                        DELETE FROM ai_messages WHERE conversation_id IN (
                            SELECT id FROM ai_conversations WHERE user_id = ?
                        )
                    '''), (user_id,))
                    cursor.execute(adapt_query('DELETE FROM ai_conversations WHERE user_id = ?'), (user_id,))
                except Exception as e:
                    _record_failure('ai_messages/ai_conversations', e)

                # Delete AI usage log entries
                try:
                    cursor.execute(adapt_query('DELETE FROM ai_usage_log WHERE user_id = ?'), (user_id,))
                except Exception as e:
                    _record_failure('ai_usage_log', e)
                try:
                    cursor.execute(adapt_query('DELETE FROM ai_usage WHERE user_id = ?'), (user_id,))
                except Exception as e:
                    _record_failure('ai_usage', e)

            # The waitlist stores are keyed by ADDRESS, not user_id,
            # so they need their own pass. Runs after the commit and on its own
            # connection; a failure there is recorded like any other.
            if email_preread_failure:
                erasure_failures.append(email_preread_failure)
            if original_email:
                try:
                    _wl = self.delete_from_waitlist(original_email)
                    if _wl['rows'] or _wl['file_entries']:
                        logger.info(
                            "[SECURITY] User %s: also erased %d waitlist row(s) "
                            "and %d file entr(ies)",
                            user_id, _wl['rows'], _wl['file_entries'],
                        )
                except Exception as e:
                    erasure_failures.append(f'waitlist: {type(e).__name__}: {e}')

            if erasure_failures:
                # Loud, and returned. The caller
                # (carrac/auth/routes.py) turns False into a 500 the user sees,
                # so they are not told their data is gone when some of it is not.
                logger.error(
                    "[SECURITY] User %s anonymized INCOMPLETELY — %d table(s) "
                    "failed and their rows SURVIVE: %s",
                    user_id, len(erasure_failures), '; '.join(erasure_failures),
                )
                try:
                    import sentry_sdk
                    sentry_sdk.capture_message(
                        f'GDPR erasure incomplete for user {user_id}: '
                        f'{"; ".join(erasure_failures)}',
                        level='error',
                    )
                except Exception:
                    pass
                return False

            logger.warning(f"[SECURITY] User {user_id} anonymized — all personal data deleted")
            return True
        except Exception as e:
            logger.error(f"Error anonymizing user {user_id}: {e}")
            return False

    # ==================== Satisfaction Survey ====================

    def create_satisfaction_rating(self, user_id: int, rating: int, comment: str = '') -> Optional[int]:
        """Store a satisfaction survey response (1-5 rating + optional comment)."""
        if rating < 1 or rating > 5:
            return None
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    INSERT INTO satisfaction_ratings (user_id, rating, comment)
                    VALUES (?, ?, ?)
                ''') + returning_id(), (user_id, rating, comment or None))
                rid = get_last_id(cursor)
                return rid
        except Exception as e:
            logger.error(f"Error creating satisfaction rating: {e}")
            return None

    def should_show_satisfaction_survey(self, user_id: int) -> bool:
        """Check if the monthly satisfaction survey should be shown.

        Conditions: account >= 14 days old, last prompt >= 30 days ago.
        """
        try:
            with self._tx() as conn:
                row = conn.execute(adapt_query(
                    'SELECT created_at FROM users WHERE id = ?'), (user_id,)).fetchone()
            if not row:
                return False

            # Check account age (>= 14 days)
            created_at = row['created_at']
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - created_at).days < 14:
                return False

            # Check last prompt timestamp from preferences
            prefs = self.get_preferences(user_id)
            last_prompt = prefs.get('last_satisfaction_prompt')
            if last_prompt:
                last_dt = datetime.fromisoformat(last_prompt.replace('Z', '+00:00'))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_dt).days < 30:
                    return False

            return True
        except Exception as e:
            logger.error(f"Error checking satisfaction survey eligibility: {e}")
            return False

    def set_satisfaction_prompted(self, user_id: int):
        """Record that the satisfaction survey was shown to the user."""
        prefs = self.get_preferences(user_id)
        prefs['last_satisfaction_prompt'] = datetime.now(timezone.utc).isoformat()
        self.save_preferences(user_id, prefs)

    def get_satisfaction_stats(self, user_id: int) -> Dict:
        """Get satisfaction stats for a user (for PostHog identify)."""
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    SELECT COUNT(*) as cnt, AVG(rating) as avg_rating
                    FROM satisfaction_ratings WHERE user_id = ?
                '''), (user_id,))
                row = cursor.fetchone()
                return {
                    'count': row['cnt'] or 0,
                    'avg_rating': round(float(row['avg_rating']), 2) if row['avg_rating'] else None,
                }
        except Exception:
            return {'count': 0, 'avg_rating': None}

    # ==================== Feedback ====================

    def create_feedback(self, user_id: int, user_email: str, fb_type: str,
                        area: str, description: str, expected: str = '',
                        actual: str = '', how_should_work: str = '',
                        extra: str = '') -> Optional[int]:
        """Store a bug report or feature request."""
        if fb_type not in ('bug', 'feature'):
            return None
        try:
            with self._tx() as conn:
                cursor = conn.cursor()
                cursor.execute(adapt_query('''
                    INSERT INTO feedback (user_id, user_email, type, area, description,
                                          expected, actual, how_should_work, extra)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''') + returning_id(), (user_id, user_email, fb_type, area, description,
                      expected, actual, how_should_work, extra))
                fb_id = get_last_id(cursor)
                return fb_id
        except Exception as e:
            logger.error(f"Error creating feedback: {e}")
            return None

    def list_feedback(self, status: str = None) -> List[Dict]:
        """List feedback entries, optionally filtered by status."""
        try:
            with self._tx() as conn:
                if status:
                    cursor = conn.execute(
                        adapt_query('SELECT * FROM feedback WHERE status = ? ORDER BY created_at DESC'), (status,))
                else:
                    cursor = conn.execute('SELECT * FROM feedback ORDER BY created_at DESC')
                rows = [dict(row) for row in cursor.fetchall()]
                return rows
        except Exception as e:
            logger.error(f"Error listing feedback: {e}")
            return []

    def count_new_feedback(self) -> int:
        """Count feedback entries with status 'new'."""
        try:
            with self._tx() as conn:
                row = conn.execute("SELECT COUNT(*) as cnt FROM feedback WHERE status = 'new'").fetchone()
                return row['cnt'] if row else 0
        except Exception as e:
            logger.debug(f"count_new_feedback error: {e}")
            return 0

    def update_feedback_status(self, feedback_id: int, status: str) -> bool:
        """Update feedback status (new/reviewed/done/dismissed)."""
        try:
            with self._tx() as conn:
                conn.execute(adapt_query('UPDATE feedback SET status = ? WHERE id = ?'), (status, feedback_id))
                return True
        except Exception as e:
            logger.error(f"Error updating feedback status: {e}")
            return False

    # ==================== per-user flags ====================

    def get_user_flags(self, user_id: int) -> Dict:
        """Read users.flags JSON. Returns {} on missing column / row."""
        if not user_id:
            return {}
        try:
            with self._tx() as conn:
                cur = conn.cursor()
                cur.execute(adapt_query("SELECT flags FROM users WHERE id = ?"), (user_id,))
                row = cur.fetchone()
                if not row:
                    return {}
                raw = row['flags']
                if raw is None:
                    return {}
                if isinstance(raw, dict):
                    return raw  # postgres JSONB returns dict directly
                try:
                    return json.loads(raw) or {}
                except (json.JSONDecodeError, TypeError):
                    return {}
        except Exception as e:
            logger.warning(f"get_user_flags failed for {user_id}: {e}")
            return {}

    def claim_first_event(self, user_id: int, flag: str) -> bool:
        """Atomically claim a `first_*` flag. Returns True iff this caller is
        the first to set it — used to fire one-shot funnel events without
        re-firing on subsequent visits.
        """
        if not user_id or not flag:
            return False
        flags = self.get_user_flags(user_id) or {}
        if flags.get(flag):
            return False
        flags[flag] = True
        try:
            with self._tx() as conn:
                cur = conn.cursor()
                payload = json.dumps(flags)
                if get_db_type() == 'postgresql':
                    cur.execute(
                        "UPDATE users SET flags = %s::jsonb WHERE id = %s AND (flags IS NULL OR (flags ->> %s) IS NULL)",
                        (payload, user_id, flag),
                    )
                else:
                    cur.execute(
                        "UPDATE users SET flags = ? WHERE id = ?",
                        (payload, user_id),
                    )
                return cur.rowcount > 0
        except Exception as e:
            logger.warning(f"claim_first_event({user_id}, {flag}) failed: {e}")
            return False


# Singleton instance
user_db = UserDatabase()
