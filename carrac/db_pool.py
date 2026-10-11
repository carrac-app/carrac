"""
Unified database pool module.

Consolidates the main Carrac pool (10 connections) and the quant pool
(5 connections) with shared config, lifecycle, and logging. Pools remain
isolated — they share only DSN and module housekeeping, not connection
slots, so quant workloads can't starve the web pool.

Main pool capped at 10/process (was 20). Postgres max_connections
is 200, web + worker + quant together budget to ~75, which leaves headroom
for migrations and interactive `psql` without OOMing the postgres container.
"""

import logging
import os
import threading
from contextlib import contextmanager

try:
    import psycopg2
    _DBError = psycopg2.Error
except ImportError:  # pragma: no cover — psycopg2 always present in production
    _DBError = Exception  # type: ignore[misc,assignment]

logger = logging.getLogger('carrac.db_pool')

_db_type: "str | None" = None

_main_pool = None
_main_pool_lock = threading.Lock()

_quant_pool = None
_quant_pool_lock = threading.Lock()

_reports_pool = None
_reports_pool_lock = threading.Lock()


def get_db_type() -> str:
    """Return 'postgresql' or 'sqlite'."""
    global _db_type
    if _db_type is None:
        _db_type = os.getenv('DB_TYPE', 'postgresql' if os.getenv('DATABASE_URL') else 'sqlite')
    return _db_type


def _make_pool(minconn: int, maxconn: int, *, keepalives: bool = False):
    import psycopg2.pool
    url = os.getenv('DATABASE_URL')
    if not url:
        raise RuntimeError(
            'DATABASE_URL is not set, but PostgreSQL is selected. Set '
            'DATABASE_URL to a PostgreSQL connection URL (scheme, '
            'credentials, host, port and database name), or unset DB_TYPE '
            'to use the local SQLite file.'
        )
    kwargs = dict(minconn=minconn, maxconn=maxconn, dsn=url)
    if keepalives:
        kwargs.update(dict(
            keepalives=1,
            keepalives_idle=60,
            keepalives_interval=10,
            keepalives_count=5,
        ))
    return psycopg2.pool.ThreadedConnectionPool(**kwargs)


def _init_pg_pool():
    """Initialize the main PostgreSQL pool (2-25 connections). Thread-safe, idempotent."""
    global _main_pool
    with _main_pool_lock:
        if _main_pool is not None:
            return _main_pool
        import psycopg2.extras  # noqa: F401 — imported for side effect
        # A (2, 10) pool is too tight for gthread workers (4 × 8 threads = 32
        # concurrent request slots per process). One /api/composite request alone
        # spawns ~6 parallel sub-fetches (market/scanner/plays/alerts/positions/
        # drawings); two concurrent composites saturate a 10-slot pool. 25 covers
        # bursts; 4 workers × 25 = 100 < postgres max_connections (200).
        _main_pool = _make_pool(2, 25)
        logger.info("Main PostgreSQL pool initialized (2-25 connections)")
        return _main_pool


def init_quant_pool(force: bool = False):
    """Initialize the quant PostgreSQL pool (1-5 connections) with keepalives."""
    global _quant_pool
    with _quant_pool_lock:
        if _quant_pool is not None and not force:
            return _quant_pool
        if _quant_pool is not None:
            try:
                _quant_pool.closeall()
            except Exception:
                pass
        _quant_pool = _make_pool(1, 5, keepalives=True)
        logger.info("Quant PostgreSQL pool initialized (1-5 connections)")
        return _quant_pool


def init_reports_pool(force: bool = False):
    """Initialize the reports PostgreSQL pool (1-3 connections) with keepalives.

    Dedicated to the morning-engine write surface (save_report, summary,
    metadata, audio_script, personal sections). Kept separate from the main
    pool because dispatch can saturate the main pool, and the report-save
    POST would then 500 on PoolError and drop the entire morning report
    despite a successful generation.
    """
    global _reports_pool
    with _reports_pool_lock:
        if _reports_pool is not None and not force:
            return _reports_pool
        if _reports_pool is not None:
            try:
                _reports_pool.closeall()
            except Exception:
                pass
        _reports_pool = _make_pool(1, 3, keepalives=True)
        logger.info("Reports PostgreSQL pool initialized (1-3 connections)")
        return _reports_pool


class PgConnWrapper:
    """Thin wrapper around psycopg2 connection that adds .execute() support.

    SQLite connections have conn.execute() which returns a cursor;
    psycopg2 connections don't. This wrapper adds that method so
    legacy code using ``conn.execute()`` works with either backend.
    """
    __slots__ = ('_conn',)

    def __init__(self, conn):
        self._conn = conn

    def execute(self, query, params=None):
        cur = self._conn.cursor()
        cur.execute(query, params)
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)

    @property
    def info(self):
        return self._conn.info


_sqlite_local = threading.local()


def _get_sqlite_conn():
    """Thread-local SQLite connection (local dev only)."""
    if not hasattr(_sqlite_local, 'conn') or _sqlite_local.conn is None:
        import sqlite3
        db_dir = os.environ.get('DATA_DIR', 'data')
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, 'carrac.db')
        _sqlite_local.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        _sqlite_local.conn.row_factory = sqlite3.Row
        _sqlite_local.conn.execute('PRAGMA journal_mode=WAL')
        _sqlite_local.conn.execute('PRAGMA synchronous=NORMAL')
        _sqlite_local.conn.execute('PRAGMA busy_timeout=30000')
    return _sqlite_local.conn


@contextmanager
def get_conn():
    """Yield a connection from the main pool (PostgreSQL or SQLite)."""
    if get_db_type() == 'postgresql':
        pool = _init_pg_pool()
        conn = pool.getconn()
        try:
            import psycopg2.extras
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            yield PgConnWrapper(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)
    else:
        conn = _get_sqlite_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _test_conn(conn) -> bool:
    try:
        conn.cursor().execute("SELECT 1")
        conn.rollback()
        return True
    except (_DBError, OSError):
        return False


@contextmanager
def get_quant_conn():
    """Yield a connection from the quant pool with stale-connection recovery."""
    pool = init_quant_pool()
    conn = None
    try:
        conn = pool.getconn()
        if not _test_conn(conn):
            logger.warning("Quant DB stale connection detected, reinitializing pool")
            try:
                pool.putconn(conn, close=True)
            except (_DBError, OSError):
                pass
            pool = init_quant_pool(force=True)
            conn = pool.getconn()
        import psycopg2.extras
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        yield conn
        conn.commit()
    except Exception:
        if conn:
            try:
                conn.rollback()
            except (_DBError, OSError):
                pass
        raise
    finally:
        if conn:
            try:
                pool.putconn(conn)
            except (_DBError, OSError):
                pass


@contextmanager
def get_reports_conn():
    """Yield a connection from the dedicated reports pool.

    One-shot: connection is committed and returned to the pool on exit.
    Use for engine-driven report writes (save_report, summary, metadata,
    audio_script, personal sections) so they cannot be starved by the
    main web pool.

    On SQLite the call falls through to the shared file connection.
    """
    if get_db_type() != 'postgresql':
        with get_conn() as conn:
            yield conn
        return
    pool = init_reports_pool()
    conn = None
    try:
        conn = pool.getconn()
        if not _test_conn(conn):
            logger.warning("Reports DB stale connection detected, reinitializing pool")
            try:
                pool.putconn(conn, close=True)
            except (_DBError, OSError):
                pass
            pool = init_reports_pool(force=True)
            conn = pool.getconn()
        import psycopg2.extras
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        yield PgConnWrapper(conn)
        conn.commit()
    except Exception:
        if conn:
            try:
                conn.rollback()
            except (_DBError, OSError):
                pass
        raise
    finally:
        if conn:
            try:
                pool.putconn(conn)
            except (_DBError, OSError):
                pass


def adapt_query(sql: str) -> str:
    """Convert SQLite-style ``?`` placeholders to PostgreSQL ``%s``."""
    if get_db_type() == 'sqlite':
        return sql
    return sql.replace('?', '%s')


def returning_id() -> str:
    """Return ``' RETURNING id'`` on PostgreSQL, empty string on SQLite."""
    if get_db_type() == 'postgresql':
        return ' RETURNING id'
    return ''


def get_last_id(cursor, conn=None):
    """Return the last inserted row ID (PostgreSQL and SQLite compatible)."""
    if get_db_type() == 'postgresql':
        row = cursor.fetchone()
        if row:
            return row['id'] if isinstance(row, dict) else row[0]
        return None
    return cursor.lastrowid


def handle_integrity_error():
    """Return the appropriate IntegrityError class for the current DB backend."""
    if get_db_type() == 'postgresql':
        import psycopg2.errors
        return psycopg2.errors.UniqueViolation
    import sqlite3
    return sqlite3.IntegrityError
