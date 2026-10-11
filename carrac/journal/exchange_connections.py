"""Exchange connection storage.

Read-only Binance / Hyperliquid connections for journal auto-sync. One row
per (user_id, exchange) — connecting again replaces the stored credentials
and resets the sync cursor (a deliberate "start over" on reconnect).

Security:
  - Credentials are Fernet-encrypted at rest via
    ``carrac.secret_box`` — the SAME generic Fernet helper the
    codebase already uses for Telegram bot tokens and webhook signing
    secrets (see carrac/alerts/webhook_delivery.py's docstring for the
    precedent). No second crypto mechanism is introduced.
  - Credentials are write-only from the API's perspective: ``to_public_dict``
    never includes ``credentials_encrypted`` or the raw secret, only a
    display-safe ``masked_identifier`` (last 4 chars of a Binance API key,
    or the full Hyperliquid wallet address — a public value, not a secret).
  - Binance keys are verified read-only (see carrac/journal/binance_auth_client.py)
    by the caller BEFORE ``create_connection`` is invoked; this module does
    not itself call any exchange API.
  - Disconnect is a hard purge (DELETE), not a soft-disable.

Schema: mirrors carrac/plays/shares.py's pattern — PostgreSQL is provisioned
by the alembic migration (20260718_exchange_connections.py); SQLite
(dev/test) gets its tables created lazily here via ``ensure_exchange_tables``,
gated on ``get_db_type() == 'sqlite'``.
"""

import json
import logging
import re
from datetime import datetime, timezone

from carrac.db_pool import adapt_query, get_conn, get_db_type, handle_integrity_error
from carrac.secret_box import decrypt_token, encrypt_token

logger = logging.getLogger('carrac.journal.exchange_connections')

ALLOWED_EXCHANGES = frozenset({'binance', 'hyperliquid'})

# After this many consecutive sync failures a connection is marked 'error'
# and skipped by the periodic sync loop until the user re-saves credentials.
MAX_CONSECUTIVE_ERRORS = 5

# Hyperliquid fills are queryable by wallet address alone (public info API) —
# no secret is ever stored for HL. Standard 20-byte hex EVM address.
HL_ADDRESS_RE = re.compile(r'^0x[0-9a-fA-F]{40}$')


def is_valid_hl_address(address: str) -> bool:
    return bool(HL_ADDRESS_RE.match((address or '').strip()))


# ── schema (SQLite dev/test only — production provisioned by alembic) ──

_CREATE_CONNECTIONS_SQL = '''
    CREATE TABLE IF NOT EXISTS exchange_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        exchange VARCHAR(20) NOT NULL,
        label VARCHAR(50),
        credentials_encrypted TEXT NOT NULL,
        masked_identifier VARCHAR(80),
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        consecutive_errors INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        last_sync_at TEXT,
        last_sync_trades_imported INTEGER NOT NULL DEFAULT 0,
        cursor_data TEXT NOT NULL DEFAULT '{}',
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(user_id, exchange)
    )
'''
_CREATE_FILLS_SQL = '''
    CREATE TABLE IF NOT EXISTS exchange_sync_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id INTEGER NOT NULL,
        exchange_trade_id VARCHAR(64) NOT NULL,
        symbol VARCHAR(20) NOT NULL,
        fill_time TEXT NOT NULL,
        created_at TEXT,
        UNIQUE(connection_id, exchange_trade_id)
    )
'''
_CREATE_SUGGESTIONS_SQL = '''
    CREATE TABLE IF NOT EXISTS exchange_import_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        exchange VARCHAR(20) NOT NULL,
        symbol VARCHAR(20) NOT NULL,
        direction VARCHAR(10) NOT NULL,
        entry_price REAL,
        exit_price REAL,
        size REAL,
        fees REAL,
        opened_at TEXT,
        closed_at TEXT,
        matched_trade_id INTEGER,
        journal_trade_id INTEGER,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        created_at TEXT,
        resolved_at TEXT
    )
'''


def ensure_exchange_tables(conn) -> None:
    """Create the three exchange-sync tables if missing (SQLite dev/test only)."""
    if get_db_type() != 'sqlite':
        return
    conn.execute(_CREATE_CONNECTIONS_SQL)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_exchange_connections_user ON exchange_connections(user_id)')
    conn.execute(_CREATE_FILLS_SQL)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_exchange_sync_fills_conn ON exchange_sync_fills(connection_id)')
    conn.execute(_CREATE_SUGGESTIONS_SQL)
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_exchange_import_suggestions_user '
        'ON exchange_import_suggestions(user_id, status)'
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_unique_violation(exc) -> bool:
    try:
        return isinstance(exc, handle_integrity_error())
    except Exception:
        return False


def _row_to_dict(row):
    return dict(row) if row is not None else None


def _mask_identifier(exchange: str, credentials: dict) -> str:
    if exchange == 'binance':
        key = (credentials.get('api_key') or '').strip()
        if len(key) <= 4:
            return '****'
        return ('*' * (len(key) - 4)) + key[-4:]
    if exchange == 'hyperliquid':
        # A wallet address is public, not a secret — show it in full.
        return (credentials.get('address') or '').strip()
    return ''


# ── CRUD ──

def create_connection(user_id: int, exchange: str, credentials: dict, label: str = None) -> dict:
    """Create or replace the user's connection for ``exchange``.

    Encrypts ``credentials`` (a small dict — {'api_key','api_secret'} for
    Binance, {'address'} for Hyperliquid) as a single Fernet-encrypted JSON
    blob. Reconnecting resets status/errors/cursor to a clean slate.
    """
    exchange = (exchange or '').strip().lower()
    if exchange not in ALLOWED_EXCHANGES:
        raise ValueError(f'Unsupported exchange: {exchange!r}')

    # Exchange API secrets never fall back to plaintext.
    encrypted = encrypt_token(json.dumps(credentials), required=True,
                              purpose='exchange credentials')
    masked = _mask_identifier(exchange, credentials)
    now = _now_iso()

    with get_conn() as conn:
        ensure_exchange_tables(conn)
        conn.execute(
            adapt_query('''
                INSERT INTO exchange_connections
                    (user_id, exchange, label, credentials_encrypted, masked_identifier,
                     status, consecutive_errors, last_error, last_sync_at,
                     last_sync_trades_imported, cursor_data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', 0, NULL, NULL, 0, '{}', ?, ?)
                ON CONFLICT (user_id, exchange) DO UPDATE SET
                    label = excluded.label,
                    credentials_encrypted = excluded.credentials_encrypted,
                    masked_identifier = excluded.masked_identifier,
                    status = 'active',
                    consecutive_errors = 0,
                    last_error = NULL,
                    cursor_data = '{}',
                    updated_at = excluded.updated_at
            '''),
            (user_id, exchange, label, encrypted, masked, now, now),
        )
        row = conn.execute(
            adapt_query('SELECT * FROM exchange_connections WHERE user_id = ? AND exchange = ?'),
            (user_id, exchange),
        ).fetchone()
        return _row_to_dict(row)


def list_connections(user_id: int) -> list:
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        rows = conn.execute(
            adapt_query('SELECT * FROM exchange_connections WHERE user_id = ? ORDER BY created_at DESC'),
            (user_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_connection(connection_id: int, user_id: int = None) -> dict:
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        if user_id is not None:
            row = conn.execute(
                adapt_query('SELECT * FROM exchange_connections WHERE id = ? AND user_id = ?'),
                (connection_id, user_id),
            ).fetchone()
        else:
            row = conn.execute(
                adapt_query('SELECT * FROM exchange_connections WHERE id = ?'), (connection_id,)
            ).fetchone()
        return _row_to_dict(row)


def delete_connection(connection_id: int, user_id: int) -> bool:
    """Hard purge — connection + its raw-fill ledger + any pending suggestions.

    Ownership-checked. Postgres cascades via FK ON DELETE CASCADE; the
    explicit child deletes here make the same call correct under SQLite
    dev/test too (no FK pragma enabled there).
    """
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        owned = conn.execute(
            adapt_query('SELECT id FROM exchange_connections WHERE id = ? AND user_id = ?'),
            (connection_id, user_id),
        ).fetchone()
        if not owned:
            return False
        conn.execute(adapt_query('DELETE FROM exchange_sync_fills WHERE connection_id = ?'), (connection_id,))
        conn.execute(
            adapt_query('DELETE FROM exchange_import_suggestions WHERE connection_id = ?'), (connection_id,)
        )
        conn.execute(adapt_query('DELETE FROM exchange_connections WHERE id = ?'), (connection_id,))
        return True


def to_public_dict(connection: dict) -> dict:
    """Strip secrets/internal state before this ever reaches an API response."""
    if not connection:
        return connection
    d = dict(connection)
    d.pop('credentials_encrypted', None)
    d.pop('cursor_data', None)
    return d


def get_decrypted_credentials(connection: dict) -> dict:
    """Internal use only (the sync engine) — never expose this to a route response."""
    raw = decrypt_token(connection.get('credentials_encrypted') or '')
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.error(
            'exchange_connections: failed to parse decrypted credentials for connection %s',
            connection.get('id'),
        )
        return {}


# ── sync bookkeeping (used by carrac.journal.exchange_sync) ──

def update_cursor(connection_id: int, cursor_data: dict) -> None:
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        conn.execute(
            adapt_query('UPDATE exchange_connections SET cursor_data = ?, updated_at = ? WHERE id = ?'),
            (json.dumps(cursor_data), _now_iso(), connection_id),
        )


def mark_sync_success(connection_id: int, trades_imported: int) -> None:
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        conn.execute(
            adapt_query('''
                UPDATE exchange_connections
                SET status = 'active', consecutive_errors = 0, last_error = NULL,
                    last_sync_at = ?, last_sync_trades_imported = ?, updated_at = ?
                WHERE id = ?
            '''),
            (_now_iso(), trades_imported, _now_iso(), connection_id),
        )


def mark_sync_error(connection_id: int, error_message: str):
    """Increment the consecutive-error counter; auto-disable past the cap.

    Returns (new_consecutive_count, new_status).
    """
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        row = conn.execute(
            adapt_query('SELECT consecutive_errors FROM exchange_connections WHERE id = ?'),
            (connection_id,),
        ).fetchone()
        current = (row['consecutive_errors'] if row else 0) or 0
        new_count = current + 1
        status = 'error' if new_count >= MAX_CONSECUTIVE_ERRORS else 'active'
        conn.execute(
            adapt_query('''
                UPDATE exchange_connections
                SET consecutive_errors = ?, last_error = ?, status = ?, updated_at = ?
                WHERE id = ?
            '''),
            (new_count, str(error_message)[:2000], status, _now_iso(), connection_id),
        )
        return new_count, status


def list_syncable_connections() -> list:
    """All connections eligible for the periodic sync loop (status='active').

    'error' connections (past MAX_CONSECUTIVE_ERRORS) are excluded until the
    user re-saves credentials via create_connection (which resets status).
    """
    with get_conn() as conn:
        ensure_exchange_tables(conn)
        rows = conn.execute(adapt_query("SELECT * FROM exchange_connections WHERE status = 'active'")).fetchall()
        return [_row_to_dict(r) for r in rows]
