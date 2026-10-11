"""
Content store: what users author and the history the reports read back.

``user_db`` holds accounts, API keys, billing and AI usage; this module holds:

- Chart drawings: ``drawings``
- Trade journal: ``trade_journal``, ``journal_sessions``,
  ``journal_screenshots``, ``journal_playbooks``, ``journal_goals``,
  ``journal_views``, ``setup_checklists``, ``score_snapshots``
- Reports: ``reports``, ``personal_report_sections``
- Market history: ``sentiment_history``, ``regime_history``

Supports both PostgreSQL and SQLite backends via carrac.db_pool helpers. On
SQLite the file is ``$DATA_DIR/drawings.db``; the name predates every table
but the first, and existing installs keep their data under it.
"""

import os
import sqlite3
import threading
import logging
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any

from carrac.db_pool import get_reports_conn, get_db_type, adapt_query, returning_id, get_last_id

logger = logging.getLogger('carrac.content_db')


def report_age_days(created_at) -> Optional[float]:
    """Age of a report row in days, or None if the timestamp is unusable.

    ``created_at`` arrives as a tz-aware ``datetime`` from
    Postgres and as an ISO-8601 string from the SQLite test backend, so both
    are accepted here rather than at each call site. Naive values are read as
    UTC — every writer in this codebase stamps UTC.
    """
    if created_at is None or created_at == '':
        return None
    dt = created_at
    if not isinstance(dt, datetime):
        try:
            dt = datetime.fromisoformat(str(created_at).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0

# Module-level column allowlists for the dynamically-built INSERT/UPDATE sites
# below. Hoisted out of the methods so scripts/check_write_columns.py
# can import this module and validate them against the real Postgres schema
# without needing to interpret function bodies. Behaviour is identical to
# building these collections inline in each method.
SCORE_SNAPSHOT_COLUMNS = [
    'journal_id', 'user_id', 'snapshot_type', 'asset', 'direction',
    'score_trend', 'score_momentum', 'score_rsi', 'score_structure',
    'score_level', 'score_macro', 'score_total', 'verdict', 'vetoes',
    'price',
    'consensio_1d_signal', 'consensio_1d_positions',
    'consensio_3d_signal', 'consensio_3d_positions',
    'consensio_1w_signal', 'consensio_1w_positions',
    'consensio_total_positions',
    'ema_ribbon_trend', 'ema_ribbon_gap_pct',
    'macd_histogram', 'macd_momentum',
    'weekly_rsi', 'rsi_div_daily', 'rsi_div_weekly',
    'structure_trend', 'last_high_type', 'last_low_type',
    'cpr_pivot', 'cpr_tc', 'cpr_bc',
    'bb_upper', 'bb_lower', 'bb_middle',
    'adx_value', 'volume_ratio',
    'regime', 'regime_score', 'fear_greed', 'breadth_pct',
    'funding_rate', 'entry_price', 'stop_price', 'tp1_price',
    'setup_type', 'setup_readiness', 'setup_confidence',
]

DRAWING_UPDATE_FIELDS = {
    'price_level', 'price_level_upper', 'label', 'color',
    'line_style', 'line_width', 'metadata'
}

TRADE_UPDATE_FIELDS = {
    'entry_price', 'target_price', 'stop_price', 'direction',
    'setup_type', 'notes', 'timeframe', 'scenario_id',
    'size', 'fees', 'pnl_currency', 'account_tag',
    'confidence', 'mood', 'mistake_tags', 'checklist_results',
    'checklist_template_id', 'ai_review', 'trade_grade',
    'session_id', 'tags', 'mfe', 'mae', 'mfe_price',
    'mae_price', 'exit_efficiency', 'exit_price', 'exit_reason',
    'mfe_computed_at',
}
# rr_planned is written by trade_update too, but as a derived value (not a
# caller-supplied field) — added to the update dict after the allowlist
# filter, so it belongs in the checked column set as well.
TRADE_UPDATE_COLUMNS = TRADE_UPDATE_FIELDS | {'rr_planned'}

SESSION_UPDATE_FIELDS = {
    'session_label', 'pre_notes', 'post_notes', 'mood_pre',
    'mood_post', 'rules_followed', 'ai_summary',
}

PLAYBOOK_UPDATE_FIELDS = {
    'name', 'setup_type', 'description', 'ideal_conditions',
    'entry_rules', 'exit_rules', 'example_trade_ids', 'screenshot_ids',
    'stats_cache', 'stats_updated_at',
}


class ContentDatabase:
    """Drawings, trade journal, reports and market history (PostgreSQL + SQLite)."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.path.join(os.environ.get('DATA_DIR', 'data'), 'drawings.db')
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
        """Get thread-local database connection for the active backend."""
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
                # Autocommit avoids the idle-in-transaction lock leak
                # (an ALTER TABLE drawings hangs behind SELECT-only readers
                # that never commit). Atomic multi-statement writes use the
                # _pg_atomic() context manager to toggle autocommit off and back.
                raw.autocommit = True
                raw.cursor_factory = psycopg2.extras.RealDictCursor
                self._local.pg_conn = raw
                self._local.pg_wrapped = PgConnWrapper(raw)
            # Defensive cleanup: if the connection somehow ended up mid-transaction
            # (e.g. _pg_atomic raised before restoring autocommit), normalise it.
            import psycopg2.extensions as _pg_ext
            ts = self._local.pg_conn.info.transaction_status
            if ts == _pg_ext.TRANSACTION_STATUS_UNKNOWN:
                self._release_pg_conn()
                return self._get_conn()
            if ts in (_pg_ext.TRANSACTION_STATUS_INTRANS,
                      _pg_ext.TRANSACTION_STATUS_INERROR):
                try:
                    self._local.pg_conn.rollback()
                    self._local.pg_conn.autocommit = True
                except Exception:
                    self._release_pg_conn()
                    return self._get_conn()
            return self._local.pg_wrapped
        else:
            if not hasattr(self._local, 'conn') or self._local.conn is None:
                self._local.conn = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False,
                    timeout=30.0
                )
                self._local.conn.row_factory = sqlite3.Row
                self._local.conn.execute('PRAGMA journal_mode=WAL')
                self._local.conn.execute('PRAGMA synchronous=NORMAL')
                self._local.conn.execute('PRAGMA busy_timeout=30000')
            return self._local.conn

    @contextmanager
    def _pg_atomic(self):
        """Run a block atomically under PostgreSQL.

        Under autocommit=True every statement commits immediately, so
        multi-statement sequences that must be all-or-nothing (DELETE+INSERT,
        UPDATE+DELETE chains) wrap their work in this context manager.
        On SQLite the calls are no-ops because _get_conn() keeps the SQLite
        connection in its default (deferred-commit) mode.
        """
        if get_db_type() != 'postgresql':
            yield
            return
        raw = self._local.pg_conn
        raw.autocommit = False
        try:
            yield
            raw.commit()
        except Exception:
            try:
                raw.rollback()
            except Exception:
                pass
            raise
        finally:
            raw.autocommit = True

    def _init_database(self):
        """Initialize database schema (SQLite only; PostgreSQL uses init_postgres.sql)."""
        if get_db_type() != 'sqlite':
            logger.info("PostgreSQL backend — skipping SQLite schema init")
            return

        conn = self._get_conn()
        cursor = conn.cursor()

        # Drawings table - stores user annotations on charts
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS drawings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset VARCHAR(20) NOT NULL,
                timeframe VARCHAR(10) NOT NULL,
                drawing_type VARCHAR(30) NOT NULL,
                price_level REAL,
                price_level_upper REAL,
                label TEXT,
                color VARCHAR(20) DEFAULT '#ff0000',
                line_style VARCHAR(20) DEFAULT 'solid',
                line_width INTEGER DEFAULT 2,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Index for fast lookups by asset/timeframe
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_drawings_asset_tf
            ON drawings(asset, timeframe)
        ''')

        # Reports table - stores generated status reports
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_type VARCHAR(50) DEFAULT 'full',
                content TEXT NOT NULL,
                assets_included TEXT,
                generation_time_ms INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Migration: add scenario_id column for drawing groups
        try:
            cursor.execute('ALTER TABLE drawings ADD COLUMN scenario_id INTEGER')
            conn.commit()
            logger.info("Added scenario_id column to drawings table")
        except sqlite3.OperationalError:
            pass  # Column already exists

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_drawings_scenario
            ON drawings(scenario_id)
        ''')

        # Migration: add summary column to reports
        try:
            cursor.execute('ALTER TABLE reports ADD COLUMN summary TEXT')
            conn.commit()
            logger.info("Added summary column to reports table")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Migration: add metadata column to reports (JSON blob for internals)
        try:
            cursor.execute('ALTER TABLE reports ADD COLUMN metadata TEXT')
            conn.commit()
            logger.info("Added metadata column to reports table")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Migration: add audio_script column to reports (dedicated TTS script storage)
        try:
            cursor.execute('ALTER TABLE reports ADD COLUMN audio_script TEXT')
            conn.commit()
            logger.info("Added audio_script column to reports table")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Migration: add source_briefing column to reports.
        # Shared/market reports only — see save_report()'s docstring for why
        # personal_report_sections deliberately has no equivalent column.
        try:
            cursor.execute('ALTER TABLE reports ADD COLUMN source_briefing TEXT')
            conn.commit()
            logger.info("Added source_briefing column to reports table")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Migration: add generation_date column for idempotency key
        try:
            cursor.execute('ALTER TABLE reports ADD COLUMN generation_date TEXT')
            conn.commit()
            # Back-fill existing rows from created_at
            cursor.execute(
                "UPDATE reports SET generation_date = date(created_at) WHERE generation_date IS NULL"
            )
            conn.commit()
            logger.info("Added generation_date column to reports table")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # SQLite unique index for idempotency: one row per (type, date)
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_type_day
            ON reports(report_type, generation_date)
            WHERE generation_date IS NOT NULL
        ''')

        # Migration: add user_id column for multi-user drawing isolation
        try:
            cursor.execute('ALTER TABLE drawings ADD COLUMN user_id INTEGER DEFAULT 0')
            conn.commit()
            logger.info("Added user_id column to drawings table")
            # Migrate existing drawings to admin user
            admin_id = self._get_admin_user_id()
            if admin_id:
                cursor.execute('UPDATE drawings SET user_id = ? WHERE user_id = 0', (admin_id,))
                conn.commit()
                logger.info(f"Migrated {cursor.rowcount} existing drawings to admin user_id={admin_id}")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Migration: add version column for optimistic-concurrency sync.
        # Bumps on every update/delete; clients pass the last
        # known value as If-Match / in the body so stale overwrites become
        # HTTP 409 instead of silent last-write-wins.
        try:
            cursor.execute('ALTER TABLE drawings ADD COLUMN version INTEGER DEFAULT 1')
            conn.commit()
            logger.info("Added version column to drawings table")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # === Historical Context Tables ===

        # Sentiment history (created here for consistency; hourly snapshots written by the background worker)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sentiment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                fear_greed REAL,
                funding_rate REAL,
                open_interest_usd REAL,
                oi_change_pct REAL,
                mvrv_zscore REAL,
                alt_season REAL,
                btc_long_pct REAL
            )
        ''')

        # Migration: add btc_price to sentiment_history
        try:
            cursor.execute('ALTER TABLE sentiment_history ADD COLUMN btc_price REAL')
            conn.commit()
            logger.info("Added btc_price column to sentiment_history table")
        except sqlite3.OperationalError:
            pass

        # Trade journal — auto-captured from position drawings
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                asset VARCHAR(20) NOT NULL,
                timeframe VARCHAR(10),
                direction VARCHAR(10),
                entry_price REAL,
                target_price REAL,
                stop_price REAL,
                exit_price REAL,
                exit_reason VARCHAR(20),
                rr_planned REAL,
                rr_actual REAL,
                setup_type VARCHAR(30),
                indicators_at_entry TEXT,
                drawing_id INTEGER,
                scenario_id INTEGER,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                notes TEXT,
                size REAL,
                fees REAL DEFAULT 0,
                pnl_currency REAL,
                account_tag VARCHAR(30),
                confidence INTEGER,
                mood INTEGER,
                mistake_tags TEXT,
                checklist_results TEXT,
                checklist_template_id INTEGER,
                ai_review TEXT,
                trade_grade TEXT,
                session_id INTEGER,
                tags TEXT,
                mfe REAL,
                mae REAL,
                mfe_price REAL,
                mae_price REAL,
                exit_efficiency REAL,
                mfe_computed_at TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_journal_user ON trade_journal(user_id, opened_at)
        ''')

        # Migrate existing trade_journal — add new columns if missing
        self._migrate_trade_journal(cursor)

        # Journal sessions — daily/session wrapper for trades
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS journal_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date DATE NOT NULL,
                session_label VARCHAR(30),
                pre_notes TEXT,
                post_notes TEXT,
                mood_pre INTEGER,
                mood_post INTEGER,
                rules_followed TEXT,
                ai_summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, date, session_label)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_sessions_user_date ON journal_sessions(user_id, date)
        ''')

        # Journal screenshots — multiple per trade
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS journal_screenshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                journal_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                screenshot_type VARCHAR(10) NOT NULL,
                file_path TEXT NOT NULL,
                captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ai_analysis TEXT,
                FOREIGN KEY (journal_id) REFERENCES trade_journal(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_screenshots_journal ON journal_screenshots(journal_id)
        ''')

        # Setup checklists — per setup type templates
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS setup_checklists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                setup_type VARCHAR(30) NOT NULL,
                name VARCHAR(50),
                checklist_items TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, setup_type)
            )
        ''')

        # Saved filter views
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS journal_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name VARCHAR(50) NOT NULL,
                filters TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Playbooks — strategy definitions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS journal_playbooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name VARCHAR(50) NOT NULL,
                setup_type VARCHAR(30),
                description TEXT,
                ideal_conditions TEXT,
                entry_rules TEXT,
                exit_rules TEXT,
                example_trade_ids TEXT,
                screenshot_ids TEXT,
                stats_cache TEXT,
                stats_updated_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Monthly goals
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS journal_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                month VARCHAR(7) NOT NULL,
                goals TEXT NOT NULL,
                progress_cache TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, month)
            )
        ''')

        # Regime history — hourly snapshots of macro regime classification
        cursor.execute('''CREATE TABLE IF NOT EXISTS regime_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            regime TEXT NOT NULL,
            score INTEGER NOT NULL,
            components TEXT
        )''')

        # Personal report sections — per-user personalized content linked to shared reports
        cursor.execute('''CREATE TABLE IF NOT EXISTS personal_report_sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            cost_cents INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(report_id, user_id)
        )''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_personal_report_user
            ON personal_report_sections(user_id, report_id)
        ''')

        # Migration: add audio_script column to personal_report_sections
        try:
            cursor.execute('ALTER TABLE personal_report_sections ADD COLUMN audio_script TEXT')
            conn.commit()
            logger.info("Added audio_script column to personal_report_sections table")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Score snapshots — the scanner state captured at trade open and close
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS score_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                journal_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                snapshot_type VARCHAR(10) NOT NULL,
                asset VARCHAR(20) NOT NULL,
                direction VARCHAR(10) NOT NULL,
                captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                score_trend INTEGER,
                score_momentum INTEGER,
                score_rsi INTEGER,
                score_structure INTEGER,
                score_level INTEGER,
                score_macro INTEGER,
                score_total INTEGER,
                verdict VARCHAR(10),
                vetoes TEXT,

                price REAL,

                consensio_1d_signal INTEGER,
                consensio_1d_positions INTEGER,
                consensio_3d_signal INTEGER,
                consensio_3d_positions INTEGER,
                consensio_1w_signal INTEGER,
                consensio_1w_positions INTEGER,
                consensio_total_positions INTEGER,

                ema_ribbon_trend VARCHAR(10),
                ema_ribbon_gap_pct REAL,
                macd_histogram REAL,
                macd_momentum VARCHAR(30),

                weekly_rsi REAL,
                rsi_div_daily VARCHAR(20),
                rsi_div_weekly VARCHAR(20),

                structure_trend VARCHAR(10),
                last_high_type VARCHAR(5),
                last_low_type VARCHAR(5),

                cpr_pivot REAL,
                cpr_tc REAL,
                cpr_bc REAL,
                bb_upper REAL,
                bb_lower REAL,
                bb_middle REAL,

                adx_value REAL,
                volume_ratio REAL,

                regime VARCHAR(15),
                regime_score INTEGER,
                fear_greed INTEGER,
                breadth_pct REAL,

                funding_rate REAL,
                entry_price REAL,
                stop_price REAL,
                tp1_price REAL,

                setup_type VARCHAR(20),
                setup_readiness VARCHAR(10),
                setup_confidence REAL,

                FOREIGN KEY (journal_id) REFERENCES trade_journal(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_journal ON score_snapshots(journal_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_user ON score_snapshots(user_id, captured_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_asset ON score_snapshots(asset, direction)')

        # Migration: add setup columns to score_snapshots
        _ALLOWED_SNAPSHOT_COLS = {'setup_type', 'setup_readiness', 'setup_confidence'}
        for col, dtype in [('setup_type', 'VARCHAR(20)'), ('setup_readiness', 'VARCHAR(10)'), ('setup_confidence', 'REAL')]:
            if col not in _ALLOWED_SNAPSHOT_COLS:
                raise ValueError(f"Unexpected migration column: {col}")
            try:
                cursor.execute(f'ALTER TABLE score_snapshots ADD COLUMN {col} {dtype}')
            except sqlite3.OperationalError:
                pass

        conn.commit()
        logger.info(f"Drawing database initialized at {self.db_path}")

    def _migrate_trade_journal(self, cursor):
        """Add new columns to trade_journal for existing databases (SQLite only)."""
        # Get existing columns
        cursor.execute("PRAGMA table_info(trade_journal)")
        existing = {row[1] for row in cursor.fetchall()}

        new_columns = [
            ('size', 'REAL'),
            ('fees', 'REAL DEFAULT 0'),
            ('pnl_currency', 'REAL'),
            ('account_tag', 'VARCHAR(30)'),
            ('confidence', 'INTEGER'),
            ('mood', 'INTEGER'),
            ('mistake_tags', 'TEXT'),
            ('checklist_results', 'TEXT'),
            ('checklist_template_id', 'INTEGER'),
            ('ai_review', 'TEXT'),
            ('trade_grade', 'TEXT'),
            ('session_id', 'INTEGER'),
            ('tags', 'TEXT'),
            ('mfe', 'REAL'),
            ('mae', 'REAL'),
            ('mfe_price', 'REAL'),
            ('mae_price', 'REAL'),
            ('exit_efficiency', 'REAL'),
            ('mfe_computed_at', 'TIMESTAMP'),
        ]
        _ALLOWED_JOURNAL_COLS = {col for col, _ in new_columns}

        for col_name, col_type in new_columns:
            if col_name not in existing:
                if col_name not in _ALLOWED_JOURNAL_COLS:
                    raise ValueError(f"Unexpected migration column: {col_name}")
                try:
                    cursor.execute(f'ALTER TABLE trade_journal ADD COLUMN {col_name} {col_type}')
                    logger.info(f"Migrated trade_journal: added column {col_name}")
                except Exception as e:
                    logger.debug(f"Column {col_name} migration skipped: {e}")

    def _get_admin_user_id(self) -> Optional[int]:
        """Get admin user_id from users table."""
        try:
            if get_db_type() == 'postgresql':
                conn = self._get_conn()
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE email = %s LIMIT 1",
                               (os.getenv('ADMIN_EMAIL', ''),))
                row = cursor.fetchone()
                return row['id'] if row else None
            else:
                users_db_path = self.db_path.parent / "users.db"
                if not users_db_path.exists():
                    return None
                conn = sqlite3.connect(str(users_db_path))
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM users WHERE email = ? LIMIT 1", (os.getenv('ADMIN_EMAIL', ''),))
                row = cursor.fetchone()
                conn.close()
                return row['id'] if row else None
        except Exception as e:
            logger.warning(f"Could not get admin user_id: {e}")
            return None

    # ==================== Drawing CRUD Operations ====================

    def save_drawing(
        self,
        asset: str,
        timeframe: str,
        drawing_type: str,
        price_level: Optional[float] = None,
        price_level_upper: Optional[float] = None,
        label: Optional[str] = None,
        color: str = '#ff0000',
        line_style: str = 'solid',
        line_width: int = 2,
        metadata: Optional[Dict] = None,
        scenario_id: Optional[int] = None,
        user_id: int = 0
    ) -> int:
        """
        Save a new drawing to the database.

        Args:
            asset: Asset symbol (e.g., 'BTCUSDT')
            timeframe: Timeframe (e.g., '1D', '4H', '1W')
            drawing_type: Type of drawing ('horizontal_line', 'zone', 'annotation', 'trendline')
            price_level: Primary price level (required for lines/zones)
            price_level_upper: Upper price level (for zones)
            label: User's annotation text
            color: Hex color code
            line_style: 'solid', 'dashed', 'dotted'
            line_width: Line thickness in pixels
            metadata: Additional JSON data
            scenario_id: Parent scenario drawing ID (for grouped drawings)

        Returns:
            The ID of the newly created drawing
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        metadata_json = json.dumps(metadata) if metadata else None

        cursor.execute(adapt_query('''
            INSERT INTO drawings
            (asset, timeframe, drawing_type, price_level, price_level_upper,
             label, color, line_style, line_width, metadata, scenario_id, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''' + returning_id()), (
            asset.upper(), timeframe.upper(), drawing_type, price_level,
            price_level_upper, label, color, line_style, line_width,
            metadata_json, scenario_id, user_id
        ))

        drawing_id = get_last_id(cursor)

        # MCP/REST creators don't send metadata.local_id, but every single-delete
        # UI path deletes BY local_id (DELETE /api/drawings/by-local-id) — a row
        # without one is silently undeletable from the chart and resurrects on
        # every load. Backfill with the same 'srv_<id>' convention the client
        # synthesizes for such rows, so client and server ids always agree.
        meta = dict(metadata) if metadata else {}
        if drawing_id is not None and not meta.get('local_id'):
            meta['local_id'] = f'srv_{drawing_id}'
            cursor.execute(
                adapt_query('UPDATE drawings SET metadata = ? WHERE id = ?'),
                (json.dumps(meta), drawing_id)
            )

        conn.commit()
        logger.info(f"Saved drawing {drawing_id}: {drawing_type} on {asset} {timeframe}")
        return drawing_id

    def drawing_list(
        self,
        asset: Optional[str] = None,
        timeframe: Optional[str] = None,
        drawing_type: Optional[str] = None,
        user_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get drawings, optionally filtered by asset, timeframe, drawing_type, and user_id.
        When user_id is provided, returns that user's drawings plus system drawings (user_id=0).

        Defense-in-depth: callers MUST pass a real user_id (>0) or
        the explicit cross-user sentinel ``-1``. Passing ``None`` or ``0`` from
        a request handler would leak every user's drawings; we refuse it
        here so a caller bug can't silently reopen that leak.
        Pass ``user_id=-1`` from internal/system flows that legitimately need
        cross-user reads.
        """
        # Sentinel: -1 means "ignore user filter" (system / admin reporting).
        if user_id == -1:
            user_id = None
        else:
            # Reject the falsy values that leak everyone's
            # rows: None, 0, '', etc. Accept ints>0 OR truthy strings (some
            # session fixtures + legacy callers pass string ids like 'admin'
            # or 'test-user').
            assert user_id is not None and user_id != 0 and user_id != '', (
                "content_db.drawing_list requires a real user_id; use "
                "user_id=-1 sentinel for legitimate cross-user reads"
            )

        conn = self._get_conn()
        cursor = conn.cursor()

        query = 'SELECT * FROM drawings WHERE scenario_id IS NULL'
        params = []

        if user_id is not None:
            query += ' AND (user_id = ? OR user_id = 0)'
            params.append(user_id)
        if asset:
            query += ' AND asset = ?'
            params.append(asset.upper())
        if timeframe:
            query += ' AND timeframe = ?'
            params.append(timeframe.upper())
        if drawing_type:
            query += ' AND drawing_type = ?'
            params.append(drawing_type)

        query += ' ORDER BY created_at DESC'

        cursor.execute(adapt_query(query), params)
        return self._rows_to_drawings(cursor.fetchall())

    def get_drawings_with_all(
        self,
        asset: Optional[str] = None,
        timeframe: str = 'ALL',
        drawing_type: Optional[str] = None,
        user_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get drawings for a specific timeframe OR 'ALL' (cross-TF drawings).
        When user_id is provided, returns that user's drawings plus system drawings (user_id=0).
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        query = 'SELECT * FROM drawings WHERE scenario_id IS NULL AND (timeframe = ? OR timeframe = ?)'
        params = [timeframe.upper(), 'ALL']

        if user_id is not None:
            query += ' AND (user_id = ? OR user_id = 0)'
            params.append(user_id)
        if asset:
            query += ' AND asset = ?'
            params.append(asset.upper())
        if drawing_type:
            query += ' AND drawing_type = ?'
            params.append(drawing_type)

        query += ' ORDER BY created_at DESC'

        cursor.execute(adapt_query(query), params)
        return self._rows_to_drawings(cursor.fetchall())

    def _rows_to_drawings(self, rows) -> List[Dict[str, Any]]:
        """Convert database rows to drawing dictionaries with parsed metadata."""
        drawings = []
        for row in rows:
            drawing = dict(row)
            if drawing.get('metadata'):
                try:
                    drawing['metadata'] = json.loads(drawing['metadata'])
                except json.JSONDecodeError:
                    drawing['metadata'] = None
            drawings.append(drawing)
        return drawings

    def get_drawing_by_id(self, drawing_id: int, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Get a single drawing by ID.

        Pass ``user_id`` to scope the lookup to that owner (the
        row is returned only when it belongs to that user) — callers that
        need to verify ownership (e.g. play drawing-link checks) MUST pass
        it. Omitting it (the default) preserves the historical no-filter
        behavior for existing internal callers that already handle
        ownership themselves or intentionally read cross-user rows
        (scenario parents, admin/report flows, etc.).
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        if user_id is not None:
            cursor.execute(
                adapt_query('SELECT * FROM drawings WHERE id = ? AND user_id = ?'),
                (drawing_id, user_id))
        else:
            cursor.execute(adapt_query('SELECT * FROM drawings WHERE id = ?'), (drawing_id,))
        row = cursor.fetchone()

        if row:
            drawing = dict(row)
            if drawing.get('metadata'):
                try:
                    drawing['metadata'] = json.loads(drawing['metadata'])
                except json.JSONDecodeError:
                    drawing['metadata'] = None
            return drawing
        return None

    # Sentinel return values for drawing_update / drawing_delete so callers
    # can distinguish "not found / unauthorised" from "version conflict".
    UPDATE_OK = 'ok'
    UPDATE_NOT_FOUND = 'not_found'
    UPDATE_CONFLICT = 'conflict'

    def drawing_update(self, drawing_id: int, user_id: Optional[int] = None,
                       expected_version: Optional[int] = None, **kwargs):
        """
        Update a drawing's properties with optimistic-concurrency support.

        Args:
            drawing_id:       ID of the drawing to update.
            user_id:          When provided (non-zero), a SQL-layer ownership filter is
                              added so a forgotten API decorator can't expose someone
                              else's drawing. Pass 0 or None to skip the filter.
            expected_version: If provided, row is updated only when the stored
                              `version` column equals this value; otherwise the
                              update is refused with UPDATE_CONFLICT.
            **kwargs:         Fields to update (price_level, label, color, etc.)

        Returns:
            (status, new_version) where status is one of UPDATE_OK,
            UPDATE_NOT_FOUND, UPDATE_CONFLICT. new_version is the post-update
            version on UPDATE_OK, the current version on UPDATE_CONFLICT,
            or None on UPDATE_NOT_FOUND.

        Legacy shape: historically this method returned a plain bool. Callers
        that don't care about concurrency can treat the (status, _) tuple as
        truthy-on-OK via `status == UPDATE_OK`.

        Defense-in-depth: callers SHOULD pass a real user_id so
        the SQL ownership filter fires. Pass ``user_id=-1`` for legitimate
        admin/system updates that bypass ownership; ``None`` or ``0`` will
        assert (matches drawing_list semantics). System-drawing rows
        themselves still live at ``user_id=0`` in the table.
        """
        # Sentinel: -1 → bypass ownership filter (admin / system).
        if user_id == -1:
            user_id = None
        else:
            assert user_id is not None and user_id != 0 and user_id != '', (
                "content_db.drawing_update requires a real user_id; "
                "use user_id=-1 for legitimate admin/system updates"
            )

        allowed_fields = DRAWING_UPDATE_FIELDS

        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not updates:
            return (self.UPDATE_NOT_FOUND, None)

        if 'metadata' in updates and updates['metadata'] is not None:
            updates['metadata'] = json.dumps(updates['metadata'])

        conn = self._get_conn()
        cursor = conn.cursor()

        # Look up the current row (version + ownership) in a single SELECT.
        lookup_sql = 'SELECT version, user_id FROM drawings WHERE id = ?'
        cursor.execute(adapt_query(lookup_sql), (drawing_id,))
        row = cursor.fetchone()
        if row is None:
            return (self.UPDATE_NOT_FOUND, None)

        current_version = row['version'] if 'version' in row.keys() else 1
        row_user_id = row['user_id'] if 'user_id' in row.keys() else 0

        if user_id and row_user_id and row_user_id != user_id:
            # Treat owner mismatch as "not found" so we don't leak existence.
            return (self.UPDATE_NOT_FOUND, None)

        if expected_version is not None and expected_version != current_version:
            return (self.UPDATE_CONFLICT, current_version)

        set_clause = ', '.join(f'{k} = ?' for k in updates.keys())
        set_clause += ', updated_at = CURRENT_TIMESTAMP, version = version + 1'
        values = list(updates.values())

        sql = f'UPDATE drawings SET {set_clause} WHERE id = ?'
        values.append(drawing_id)
        if user_id:
            sql += ' AND user_id = ?'
            values.append(user_id)
        if expected_version is not None:
            sql += ' AND version = ?'
            values.append(expected_version)

        cursor.execute(adapt_query(sql), values)
        conn.commit()
        if cursor.rowcount <= 0:
            # Lost a race between the SELECT and the UPDATE — treat as conflict.
            return (self.UPDATE_CONFLICT, current_version)

        logger.info(f"Updated drawing {drawing_id}: {list(updates.keys())}")
        return (self.UPDATE_OK, current_version + 1)

    def drawing_delete(self, drawing_id: int, user_id: Optional[int] = None,
                       expected_version: Optional[int] = None):
        """Delete a drawing by ID with optional optimistic-concurrency check.

        When user_id is provided (non-zero), a SQL-layer ownership filter is
        added as defence-in-depth against a missing API-layer decorator.
        When expected_version is provided and the stored version differs,
        the delete is refused with UPDATE_CONFLICT.

        Returns: UPDATE_OK / UPDATE_NOT_FOUND / UPDATE_CONFLICT (string).
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(
            adapt_query('SELECT version, user_id FROM drawings WHERE id = ?'),
            (drawing_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return self.UPDATE_NOT_FOUND
        current_version = row['version'] if 'version' in row.keys() else 1
        row_user_id = row['user_id'] if 'user_id' in row.keys() else 0
        if user_id and row_user_id and row_user_id != user_id:
            return self.UPDATE_NOT_FOUND
        if expected_version is not None and expected_version != current_version:
            return self.UPDATE_CONFLICT

        if expected_version is not None:
            # Atomic OCC: DELETE only succeeds if version still matches what we read.
            if user_id:
                cursor.execute(
                    adapt_query('DELETE FROM drawings WHERE id = ? AND user_id = ? AND version = ?'),
                    (drawing_id, user_id, expected_version),
                )
            else:
                cursor.execute(
                    adapt_query('DELETE FROM drawings WHERE id = ? AND version = ?'),
                    (drawing_id, expected_version),
                )
        elif user_id:
            cursor.execute(
                adapt_query('DELETE FROM drawings WHERE id = ? AND user_id = ?'),
                (drawing_id, user_id),
            )
        else:
            cursor.execute(
                adapt_query('DELETE FROM drawings WHERE id = ?'),
                (drawing_id,),
            )
        conn.commit()

        if cursor.rowcount > 0:
            logger.info(f"Deleted drawing {drawing_id}")
            return self.UPDATE_OK
        # If expected_version was supplied and rowcount==0, a concurrent write
        # updated the version between our SELECT and this DELETE → conflict.
        if expected_version is not None:
            return self.UPDATE_CONFLICT
        # Row disappeared between SELECT and DELETE (concurrent delete by someone else).
        return self.UPDATE_NOT_FOUND

    def delete_drawings_bulk(self, drawing_ids: List[int], user_id: int) -> int:
        """Delete multiple drawings by ID. Returns count of rows actually deleted.

        Silently skips IDs the user doesn't own (matches existing route behavior).
        Cascades children when a scenario drawing is deleted.
        """
        if not drawing_ids:
            return 0

        conn = self._get_conn()

        # Filter to owned IDs only
        placeholders = ','.join(['?'] * len(drawing_ids))
        rows = conn.execute(
            adapt_query(
                f'SELECT id, drawing_type FROM drawings WHERE id IN ({placeholders}) AND user_id = ?'
            ),
            list(drawing_ids) + [user_id],
        ).fetchall()
        owned = [r['id'] for r in rows]
        if not owned:
            return 0

        ph = ','.join(['?'] * len(owned))
        conn.execute(
            adapt_query(f'DELETE FROM drawings WHERE id IN ({ph}) AND user_id = ?'),
            owned + [user_id],
        )

        # Cascade children for any scenario parents — defer commit so the
        # parent + child rows land in a single transaction (F4).
        scenario_ids = [r['id'] for r in rows if r['drawing_type'] == 'scenario']
        for sid in scenario_ids:
            self.delete_scenario_children(sid, commit=False)

        conn.commit()
        logger.info(f"Bulk-deleted {len(owned)} drawings for user {user_id}")
        return len(owned)

    def delete_drawing_by_local_id(self, asset: str, local_id: str, user_id: Optional[int] = None) -> bool:
        """Delete a drawing by its local_id stored in metadata JSON."""
        conn = self._get_conn()
        cursor = conn.cursor()

        if get_db_type() == 'postgresql':
            # PostgreSQL: use JSON operator
            if user_id is not None and user_id != 0:
                cursor.execute(
                    "DELETE FROM drawings WHERE asset = %s AND metadata::json->>'local_id' = %s AND user_id = %s",
                    (asset.upper(), local_id, user_id)
                )
            else:
                cursor.execute(
                    "DELETE FROM drawings WHERE asset = %s AND metadata::json->>'local_id' = %s",
                    (asset.upper(), local_id)
                )
        else:
            # SQLite: use json_extract
            if user_id is not None and user_id != 0:
                cursor.execute(
                    "DELETE FROM drawings WHERE asset = ? AND json_extract(metadata, '$.local_id') = ? AND user_id = ?",
                    (asset.upper(), local_id, user_id)
                )
            else:
                cursor.execute(
                    "DELETE FROM drawings WHERE asset = ? AND json_extract(metadata, '$.local_id') = ?",
                    (asset.upper(), local_id)
                )
        conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info(f"Deleted drawing with local_id={local_id} for {asset}")
        return deleted

    def find_drawing_by_local_id(self, asset: str, local_id: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Find a drawing by its local_id stored in metadata JSON."""
        conn = self._get_conn()
        cursor = conn.cursor()

        if get_db_type() == 'postgresql':
            if user_id is not None and user_id != 0:
                cursor.execute(
                    "SELECT * FROM drawings WHERE asset = %s AND metadata::json->>'local_id' = %s AND user_id = %s LIMIT 1",
                    (asset.upper(), local_id, user_id)
                )
            else:
                cursor.execute(
                    "SELECT * FROM drawings WHERE asset = %s AND metadata::json->>'local_id' = %s LIMIT 1",
                    (asset.upper(), local_id)
                )
        else:
            if user_id is not None and user_id != 0:
                cursor.execute(
                    "SELECT * FROM drawings WHERE asset = ? AND json_extract(metadata, '$.local_id') = ? AND user_id = ? LIMIT 1",
                    (asset.upper(), local_id, user_id)
                )
            else:
                cursor.execute(
                    "SELECT * FROM drawings WHERE asset = ? AND json_extract(metadata, '$.local_id') = ? LIMIT 1",
                    (asset.upper(), local_id)
                )

        row = cursor.fetchone()
        if row:
            drawing = dict(row)
            if drawing.get('metadata'):
                try:
                    drawing['metadata'] = json.loads(drawing['metadata'])
                except (json.JSONDecodeError, TypeError):
                    pass
            return drawing
        return None

    def delete_drawings_for_asset_timeframe(self, asset: str, timeframe: str, user_id: Optional[int] = None) -> int:
        """Delete all drawings for a given asset and timeframe. When user_id is provided, only delete that user's drawings."""
        conn = self._get_conn()
        cursor = conn.cursor()
        if user_id is not None:
            cursor.execute(
                adapt_query('DELETE FROM drawings WHERE asset = ? AND timeframe = ? AND user_id = ?'),
                (asset.upper(), timeframe.upper(), user_id)
            )
        else:
            cursor.execute(
                adapt_query('DELETE FROM drawings WHERE asset = ? AND timeframe = ?'),
                (asset.upper(), timeframe.upper())
            )
        conn.commit()
        count = cursor.rowcount
        if count:
            logger.info(f"Deleted {count} drawings for {asset} {timeframe}" + (f" user_id={user_id}" if user_id else ""))
        return count

    def get_all_drawings_grouped(self, user_id: Optional[int] = None) -> Dict[str, Dict[str, List[Dict]]]:
        """
        Get all drawings grouped by asset and timeframe.

        Args:
            user_id: When provided, returns that user's drawings plus system (user_id=0).

        Returns:
            Nested dict: {asset: {timeframe: [drawings]}}
        """
        all_drawings = self.drawing_list(user_id=user_id)
        grouped = {}

        for drawing in all_drawings:
            asset = drawing['asset']
            tf = drawing['timeframe']

            if asset not in grouped:
                grouped[asset] = {}
            if tf not in grouped[asset]:
                grouped[asset][tf] = []

            grouped[asset][tf].append(drawing)

        return grouped

    # ==================== Scenario Operations ====================

    def drawing_get_scenario_children(self, scenario_id: int,
                              user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all child drawings belonging to a scenario.

        Pass ``user_id`` (the scenario owner) to scope the read to that owner —
        defense-in-depth so a foreign child row (should one ever exist) is never
        surfaced inside another user's scenario view.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        if user_id is not None:
            cursor.execute(
                adapt_query('SELECT * FROM drawings WHERE scenario_id = ? AND user_id = ? '
                            'ORDER BY created_at ASC'),
                (scenario_id, user_id)
            )
        else:
            cursor.execute(
                adapt_query('SELECT * FROM drawings WHERE scenario_id = ? ORDER BY created_at ASC'),
                (scenario_id,)
            )
        return self._rows_to_drawings(cursor.fetchall())

    def delete_scenario_children(self, scenario_id: int, commit: bool = True,
                                 user_id: Optional[int] = None) -> int:
        """Delete all child drawings for a scenario. Returns count deleted.

        Pass commit=False when called from inside another transaction so the
        cascade lands atomically with the parent operation (F4). Pass
        ``user_id`` (the scenario owner) to scope the cascade to that owner —
        defense-in-depth against deleting a foreign row keyed to the scenario.
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        if user_id is not None:
            cursor.execute(
                adapt_query('DELETE FROM drawings WHERE scenario_id = ? AND user_id = ?'),
                (scenario_id, user_id))
        else:
            cursor.execute(adapt_query('DELETE FROM drawings WHERE scenario_id = ?'), (scenario_id,))
        if commit:
            conn.commit()
        count = cursor.rowcount
        if count:
            logger.info(f"Deleted {count} children for scenario {scenario_id}")
        return count

    def get_active_scenario_count(self, user_id: int) -> int:
        """Count non-archived top-level scenarios for a user."""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            adapt_query("SELECT id, metadata FROM drawings WHERE user_id = ? AND drawing_type = 'scenario' AND scenario_id IS NULL"),
            (user_id,)
        )
        count = 0
        for row in cursor.fetchall():
            meta_str = row['metadata']
            if meta_str:
                try:
                    meta = json.loads(meta_str)
                    if meta.get('archived'):
                        continue
                except (json.JSONDecodeError, TypeError):
                    pass
            count += 1
        return count

    def replace_scenario_children(
        self,
        scenario_id: int,
        children: List[Dict[str, Any]]
    ) -> List[int]:
        """Replace all children for a scenario (delete old + insert new) in one transaction."""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(adapt_query('SELECT asset, timeframe, user_id FROM drawings WHERE id = ?'), (scenario_id,))
        parent = cursor.fetchone()
        if not parent:
            return []

        asset = parent['asset']
        timeframe = parent['timeframe']
        parent_user_id = parent['user_id'] or 0

        created_ids: List[int] = []
        with self._pg_atomic():
            cursor.execute(adapt_query('DELETE FROM drawings WHERE scenario_id = ?'), (scenario_id,))
            for child in children:
                metadata_json = json.dumps(child.get('metadata')) if child.get('metadata') else None
                cursor.execute(adapt_query('''
                    INSERT INTO drawings
                    (asset, timeframe, drawing_type, price_level, price_level_upper,
                     label, color, line_style, line_width, metadata, scenario_id, user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''' + returning_id()), (
                    asset, timeframe,
                    child.get('drawing_type', 'annotation'),
                    child.get('price_level'),
                    child.get('price_level_upper'),
                    child.get('label'),
                    child.get('color', '#ff0000'),
                    child.get('line_style', 'solid'),
                    child.get('line_width', 2),
                    metadata_json,
                    scenario_id,
                    parent_user_id
                ))
                created_ids.append(get_last_id(cursor))
            conn.commit()

        logger.info(f"Replaced scenario {scenario_id} children: {len(created_ids)} drawings")
        return created_ids

    # ==================== Report Operations ====================

    def save_report(
        self,
        content: str,
        assets_included: List[str],
        generation_time_ms: int,
        report_type: str = 'full',
        metadata: Optional[str] = None,
        audio_script: Optional[str] = None,
        source_briefing: Optional[str] = None
    ) -> int:
        """Save a generated status report.

        Idempotent per (report_type, UTC calendar day).
        A second call for the same type + day updates the existing row instead
        of inserting a duplicate so that cron retries / manual reruns don't
        create multiple rows that confuse /latest.

        source_briefing: the assembled briefing (facts/calendar/
        quant/chart/sentiment context) that the report was generated from —
        kept so a bad published claim can be traced back to a prompt bug vs.
        a bad input value, and so the Haiku fact verifier's false-positive
        rate can be measured offline. SHARED/MARKET REPORTS ONLY — this
        parameter must never be fed a per-user personalized briefing (trader
        profile, positions, journal/plays text). Personal sections go through
        save_personal_section() into personal_report_sections, which has no
        equivalent column, by deliberate choice: that content is materially
        more sensitive and is already treated as untrusted (see
        carrac/grounding/escape.py). Pruned after ~90 days by
        prune_old_source_briefings(), not kept indefinitely.
        """
        from datetime import datetime as _dt, timezone as _tz
        today = _dt.now(_tz.utc).strftime('%Y-%m-%d')

        # The one write chokepoint for every report type, so the
        # one place worth checking. A newly generated report should never carry
        # Deribit prose; if it does, the generation gate has regressed and the
        # render-time scrub would hide that forever. Never raises, never blocks
        # the save.
        try:
            from carrac.compliance.deribit_gate import alert_if_barred_prose
            alert_if_barred_prose(content, context=report_type)
        except Exception:  # noqa: BLE001
            # The check THROWING is not the same as the check
            # passing. Its own comment says a silent failure here would hide a
            # gate failure forever — so it is an ERROR (the error tracker), while the
            # save still goes ahead.
            logger.error(
                f"barred-prose check CRASHED for {report_type} — the compliance "
                "gate did not run on this report", exc_info=True,
            )

        if get_db_type() == 'postgresql':
            # Dedicated reports pool — see db_pool.get_reports_conn(). Carved
            # out of the main pool so report writes cannot hit a PoolError
            # under main-pool exhaustion.
            with get_reports_conn() as conn:
                cursor = conn.cursor()
                # PostgreSQL: upsert on the partial unique index idx_reports_type_day,
                # which is defined WHERE generation_date IS NOT NULL. The ON CONFLICT
                # target must repeat that predicate so Postgres can match the index.
                cursor.execute(adapt_query('''
                    INSERT INTO reports
                        (report_type, content, assets_included, generation_time_ms, metadata, audio_script, generation_date, source_briefing)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (report_type, generation_date) WHERE generation_date IS NOT NULL
                    DO UPDATE SET
                        content = EXCLUDED.content,
                        assets_included = EXCLUDED.assets_included,
                        generation_time_ms = EXCLUDED.generation_time_ms,
                        metadata = EXCLUDED.metadata,
                        audio_script = EXCLUDED.audio_script,
                        source_briefing = EXCLUDED.source_briefing,
                        created_at = NOW()
                ''' + returning_id()),
                    (report_type, content, json.dumps(assets_included),
                     generation_time_ms, metadata, audio_script, today, source_briefing))
                return get_last_id(cursor)

        conn = self._get_conn()
        cursor = conn.cursor()
        # SQLite: INSERT OR REPLACE respects the UNIQUE index idx_reports_type_day.
        # It deletes the conflicting row then inserts, which bumps the id but
        # /latest queries by created_at DESC so it returns correctly.
        cursor.execute('''
            INSERT OR REPLACE INTO reports
                (report_type, content, assets_included, generation_time_ms, metadata, audio_script, generation_date, source_briefing)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (report_type, content, json.dumps(assets_included),
              generation_time_ms, metadata, audio_script, today, source_briefing))
        conn.commit()
        return get_last_id(cursor)

    def update_report_audio_script(self, report_id: int, audio_script: str) -> bool:
        """Update the audio_script column for a report."""
        try:
            if get_db_type() == 'postgresql':
                with get_reports_conn() as conn:
                    conn.execute(adapt_query(
                        'UPDATE reports SET audio_script = ? WHERE id = ?'
                    ), (audio_script, report_id))
                return True
            conn = self._get_conn()
            conn.execute(adapt_query(
                'UPDATE reports SET audio_script = ? WHERE id = ?'
            ), (audio_script, report_id))
            conn.commit()
            return True
        except Exception as e:
            logger.error("Failed to update audio_script for report %s: %s", report_id, e)
            return False

    def prune_old_source_briefings(self, retention_days: int = 90) -> int:
        """Null out source_briefing for reports older than
        retention_days. `content` and every other column are untouched —
        this only clears the debug-only briefing text once it has aged past
        its usefulness window (bad-claim diagnosis, verifier false-positive
        measurement). ~40KB/report x 4/day is under 60MB/year unbounded, so
        90 days is a generous but real bound, not urgency.

        Called from worker.py's daily intent-log-purge scheduler
        — reuses that existing daily-cadence housekeeping wake rather than
        adding a new scheduled job. Never raises; returns 0 on failure so a
        bad prune run cannot affect report generation or the worker loop.
        """
        try:
            if get_db_type() == 'postgresql':
                # Dedicated reports pool, same as save_report()/update_report_audio_script().
                with get_reports_conn() as conn:
                    cur = conn.execute(
                        "UPDATE reports SET source_briefing = NULL "
                        "WHERE source_briefing IS NOT NULL "
                        "AND created_at < NOW() - (%s || ' days')::interval",
                        (str(int(retention_days)),),
                    )
                    pruned = cur.rowcount if cur.rowcount is not None else 0
            else:
                # SQLite: get_reports_conn() falls through to the SHARED
                # carrac.db connection, not this ContentDatabase's own
                # db_path — use self._get_conn() directly, same split as
                # update_report_audio_script().
                conn = self._get_conn()
                cur = conn.execute(
                    adapt_query(
                        "UPDATE reports SET source_briefing = NULL "
                        "WHERE source_briefing IS NOT NULL "
                        "AND created_at < datetime('now', ?)"
                    ),
                    (f'-{int(retention_days)} days',),
                )
                conn.commit()
                pruned = cur.rowcount if cur.rowcount is not None else 0
            logger.info(
                "source_briefing prune: cleared %s row(s) older than %sd",
                pruned, retention_days,
            )
            return pruned
        except Exception:
            logger.exception("source_briefing prune failed (will retry next scheduler wake)")
            return 0

    def get_latest_report(self, report_type: str = None,
                          max_age_days: float = None) -> Optional[Dict[str, Any]]:
        """Get the most recent report, optionally filtered by type.

        ``max_age_days`` bounds how old the newest row may be.
        Without it this method happily returns a row of ANY age, so a frozen
        ``report_type='morning'`` (ENABLE_MORNING_REPORT off) would keep being
        served as "the latest report". A caller that means "the current
        report" must say so; a caller that genuinely wants "whatever exists,
        however old" (the archive/history readers) omits the bound.

        The age comparison is done in Python, deliberately, rather than as a
        SQL interval: ``created_at`` is a real timestamptz on Postgres and an
        ISO string on the SQLite test backend, and date arithmetic is where
        those two dialects diverge (see tests/schema/carrac_pg_schema.sql).
        One code path, both backends.

        Returns None when nothing of that type exists OR when the newest row
        is older than the bound. Callers must treat None as "no current
        report" — never as "fall back to something older".
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        if report_type:
            cursor.execute(adapt_query('''
                SELECT * FROM reports WHERE report_type = ? ORDER BY created_at DESC LIMIT 1
            '''), (report_type,))
        else:
            cursor.execute('''
                SELECT * FROM reports ORDER BY created_at DESC LIMIT 1
            ''')
        row = cursor.fetchone()

        if row:
            report = dict(row)
            if max_age_days is not None:
                age = report_age_days(report.get('created_at'))
                if age is None:
                    # Unparseable/absent timestamp. A bounded caller asked for
                    # a freshness guarantee we cannot give, so refuse rather
                    # than serve an unknown-age row as current.
                    logger.warning(
                        "get_latest_report(%s, max_age_days=%s): unparseable created_at %r "
                        "— treating as no current report",
                        report_type, max_age_days, report.get('created_at'),
                    )
                    return None
                if age > max_age_days:
                    logger.warning(
                        "get_latest_report(%s): newest row is %.1f days old, "
                        "exceeds max_age_days=%s — returning None",
                        report_type, age, max_age_days,
                    )
                    return None
            if report.get('assets_included'):
                report['assets_included'] = json.loads(report['assets_included'])
            return report
        return None

    def get_report_by_id(self, report_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific report by ID."""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(adapt_query('SELECT * FROM reports WHERE id = ?'), (report_id,))
        row = cursor.fetchone()

        if row:
            report = dict(row)
            if report.get('assets_included'):
                report['assets_included'] = json.loads(report['assets_included'])
            return report
        return None

    def get_all_reports(self, report_type: str = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Get all reports, optionally filtered by type, newest first."""
        conn = self._get_conn()
        cursor = conn.cursor()

        if report_type:
            cursor.execute(adapt_query('''
                SELECT id, report_type, assets_included, generation_time_ms, created_at
                FROM reports WHERE report_type = ? ORDER BY created_at DESC LIMIT ?
            '''), (report_type, limit))
        else:
            cursor.execute(adapt_query('''
                SELECT id, report_type, assets_included, generation_time_ms, created_at
                FROM reports ORDER BY created_at DESC LIMIT ?
            '''), (limit,))

        rows = cursor.fetchall()
        reports = []
        for row in rows:
            report = dict(row)
            if report.get('assets_included'):
                report['assets_included'] = json.loads(report['assets_included'])
            reports.append(report)
        return reports

    def get_report_by_date(self, report_type: str, date: str) -> Optional[Dict[str, Any]]:
        """Return the report for a given calendar date (UTC).

        ``date`` accepts ISO YYYY-MM-DD strings.  The most recent report whose
        ``created_at`` falls on that date is returned, matching the convention
        used by ``report_get_section`` and ``report_get_diff`` in the MCP
        layer (callers pass "yesterday"/"today" shorthands which the MCP tool
        resolves before calling this method).
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(adapt_query('''
            SELECT * FROM reports
            WHERE report_type = ?
              AND DATE(created_at) = ?
            ORDER BY created_at DESC LIMIT 1
        '''), (report_type, date))
        row = cursor.fetchone()
        if row:
            report = dict(row)
            if report.get('assets_included'):
                report['assets_included'] = json.loads(report['assets_included'])
            return report
        return None

    # ==================== Personal Report Sections ====================

    def save_personal_section(self, report_id: int, user_id: int, content: str,
                              cost_cents: int = 0, input_tokens: int = 0,
                              output_tokens: int = 0,
                              audio_script: Optional[str] = None) -> int:
        """Save a per-user personal report section."""
        if get_db_type() == 'postgresql':
            with get_reports_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO personal_report_sections
                        (report_id, user_id, content, cost_cents, input_tokens, output_tokens, audio_script)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(report_id, user_id) DO UPDATE SET
                        content = EXCLUDED.content,
                        cost_cents = EXCLUDED.cost_cents,
                        input_tokens = EXCLUDED.input_tokens,
                        output_tokens = EXCLUDED.output_tokens,
                        audio_script = EXCLUDED.audio_script,
                        created_at = CURRENT_TIMESTAMP
                    RETURNING id
                ''', (report_id, user_id, content, cost_cents, input_tokens, output_tokens, audio_script))
                return get_last_id(cursor)

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO personal_report_sections
                (report_id, user_id, content, cost_cents, input_tokens, output_tokens, audio_script)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_id, user_id) DO UPDATE SET
                content = excluded.content,
                cost_cents = excluded.cost_cents,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                audio_script = excluded.audio_script,
                created_at = CURRENT_TIMESTAMP
        ''', (report_id, user_id, content, cost_cents, input_tokens, output_tokens, audio_script))
        conn.commit()
        return cursor.lastrowid

    def get_personal_section(self, report_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        """Get a user's personal section for a specific report."""
        conn = self._get_conn()
        row = conn.execute(adapt_query('''
            SELECT * FROM personal_report_sections
            WHERE report_id = ? AND user_id = ?
        '''), (report_id, user_id)).fetchone()
        return dict(row) if row else None

    # ==================== Historical Context Operations ====================

    def get_sentiment_daily_summary(self, days: int = 14) -> List[Dict]:
        """Get one sentiment row per day (latest snapshot of each day) for last N days."""
        conn = self._get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        if get_db_type() == 'postgresql':
            rows = conn.execute(
                '''SELECT date(timestamp::timestamp) as date,
                          MAX(fear_greed) as fear_greed,
                          AVG(funding_rate) as funding_rate,
                          MAX(open_interest_usd) as open_interest_usd,
                          AVG(mvrv_zscore) as mvrv_zscore,
                          MAX(alt_season) as alt_season,
                          MAX(btc_long_pct) as btc_long_pct,
                          MAX(btc_price) as btc_price
                   FROM sentiment_history
                   WHERE timestamp > %s
                   GROUP BY date(timestamp::timestamp)
                   ORDER BY date(timestamp::timestamp) ASC''',
                (cutoff,)
            ).fetchall()
        else:
            rows = conn.execute(
                '''SELECT date(timestamp) as date,
                          MAX(fear_greed) as fear_greed,
                          AVG(funding_rate) as funding_rate,
                          MAX(open_interest_usd) as open_interest_usd,
                          AVG(mvrv_zscore) as mvrv_zscore,
                          MAX(alt_season) as alt_season,
                          MAX(btc_long_pct) as btc_long_pct,
                          MAX(btc_price) as btc_price
                   FROM sentiment_history
                   WHERE timestamp > ?
                   GROUP BY date(timestamp)
                   ORDER BY date(timestamp) ASC''',
                (cutoff,)
            ).fetchall()
        return [dict(r) for r in rows]

    def build_history_context(self, days: int = 14) -> Dict:
        """Build compact historical context for the morning bot prompt.

        Only the sentiment history feeds it; it is populated
        independently by carrac/precompute.py on the worker schedule.
        """
        sentiment = self.get_sentiment_daily_summary(days)

        # Calculate streaks
        streaks = {}
        if sentiment:
            # F&G below 15 streak
            fg_streak = 0
            for row in reversed(sentiment):
                if row.get('fear_greed') is not None and row['fear_greed'] < 15:
                    fg_streak += 1
                else:
                    break
            streaks['fg_below_15_days'] = fg_streak

            # Funding negative streak
            funding_neg = 0
            for row in reversed(sentiment):
                if row.get('funding_rate') is not None and row['funding_rate'] < 0:
                    funding_neg += 1
                else:
                    break
            streaks['funding_negative_days'] = funding_neg

            # MVRV below -1 streak
            mvrv_streak = 0
            for row in reversed(sentiment):
                if row.get('mvrv_zscore') is not None and row['mvrv_zscore'] < -1:
                    mvrv_streak += 1
                else:
                    break
            streaks['mvrv_below_neg1_days'] = mvrv_streak

        # Build compact text summary
        lines = []
        if sentiment:
            fg_vals = [str(int(r['fear_greed'])) for r in sentiment[-7:] if r.get('fear_greed') is not None]
            if fg_vals:
                lines.append(f"F&G 7d: [{', '.join(fg_vals)}]" +
                            (f" — {streaks.get('fg_below_15_days', 0)} days below 15" if streaks.get('fg_below_15_days', 0) > 1 else ""))

            oi_vals = [f"{r['open_interest_usd']/1e9:.1f}B" for r in sentiment[-7:]
                      if r.get('open_interest_usd') is not None]
            if oi_vals:
                lines.append(f"OI 7d: [{', '.join(oi_vals)}]")

            mvrv_vals = [f"{r['mvrv_zscore']:.2f}" for r in sentiment[-7:]
                        if r.get('mvrv_zscore') is not None]
            if mvrv_vals:
                lines.append(f"MVRV 7d: [{', '.join(mvrv_vals)}]" +
                            (f" — {streaks.get('mvrv_below_neg1_days', 0)} days below -1" if streaks.get('mvrv_below_neg1_days', 0) > 1 else ""))

            if streaks.get('funding_negative_days', 0) > 1:
                lines.append(f"Funding: negative {streaks['funding_negative_days']} consecutive days")

        compact = '\n'.join(lines) if lines else 'No historical data yet (first run).'

        return {
            'sentiment_trend': sentiment[-14:],
            'sentiment_streaks': streaks,
            'compact_summary': compact
        }

    # ==================== Trade Journal ====================

    def log_trade_open(self, user_id: int, asset: str, timeframe: str,
                       direction: str, entry_price: float,
                       target_price: float = None, stop_price: float = None,
                       setup_type: str = None, indicators_at_entry: dict = None,
                       drawing_id: int = None, scenario_id: int = None,
                       tags: str = None) -> int:
        """Log a new trade entry. Returns journal entry id.

        ``tags`` should be a JSON-serialised list of strings (e.g.
        ``'["src:agent"]'``).  Pass the raw serialised value — callers are
        responsible for serialising before calling this method.
        """
        conn = self._get_conn()
        rr_planned = None
        if entry_price and stop_price and target_price and stop_price != entry_price:
            risk = abs(entry_price - stop_price)
            reward = abs(target_price - entry_price)
            rr_planned = round(reward / risk, 2) if risk > 0 else None

        cursor = conn.execute(
            adapt_query('''INSERT INTO trade_journal
               (user_id, asset, timeframe, direction, entry_price, target_price,
                stop_price, rr_planned, setup_type, indicators_at_entry,
                drawing_id, scenario_id, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''' + returning_id()),
            (user_id, asset.upper(), timeframe, direction, entry_price,
             target_price, stop_price, rr_planned, setup_type,
             json.dumps(indicators_at_entry) if indicators_at_entry else None,
             drawing_id, scenario_id, tags)
        )
        conn.commit()
        return get_last_id(cursor)

    def log_trade_close(self, journal_id: int, exit_price: float,
                        exit_reason: str = 'manual', notes: str = None) -> bool:
        """Close a trade journal entry with exit details."""
        conn = self._get_conn()
        # Get entry to compute actual R:R
        row = conn.execute(adapt_query('SELECT entry_price, stop_price, direction FROM trade_journal WHERE id = ?'),
                          (journal_id,)).fetchone()
        if not row:
            return False

        rr_actual = None
        entry = row['entry_price']
        stop = row['stop_price']
        direction = row['direction'] or 'long'
        if entry and stop and stop != entry:
            risk = abs(entry - stop)
            if direction == 'short':
                pnl = entry - exit_price  # short: profit when price drops
            else:
                pnl = exit_price - entry  # long: profit when price rises
            rr_actual = round(pnl / risk, 2) if risk > 0 else None

        conn.execute(
            adapt_query('''UPDATE trade_journal
               SET exit_price = ?, exit_reason = ?, rr_actual = ?,
                   closed_at = CURRENT_TIMESTAMP, notes = COALESCE(?, notes)
               WHERE id = ?'''),
            (exit_price, exit_reason, rr_actual, notes, journal_id)
        )
        conn.commit()
        return True

    def count_trades_this_month(self, user_id: int) -> int:
        """Count trades opened by ``user_id`` in the current UTC calendar month.

        Enforces the ``journal_trades_monthly`` tier cap. Counts
        by ``opened_at`` (trade creation), not ``closed_at`` — mirrors
        ``get_goal_progress``'s db-agnostic month-filter pattern.
        """
        from datetime import datetime, timezone
        conn = self._get_conn()
        # Range predicate (not to_char/strftime) so idx_journal_user
        # (user_id, opened_at) can prune instead of scanning the user's
        # full history.
        month_start = datetime.now(timezone.utc).strftime('%Y-%m-01 00:00:00')
        if get_db_type() == 'postgresql':
            row = conn.execute(
                '''SELECT COUNT(*) as cnt FROM trade_journal
                   WHERE user_id = %s AND opened_at >= %s''',
                (user_id, month_start)
            ).fetchone()
        else:
            row = conn.execute(
                '''SELECT COUNT(*) as cnt FROM trade_journal
                   WHERE user_id = ? AND opened_at >= ?''',
                (user_id, month_start)
            ).fetchone()
        return row['cnt'] if row else 0

    # ==================== Score Snapshots ====================

    def insert_score_snapshot(self, data: dict):
        """Insert a score snapshot row. data is a flat dict matching column names."""
        cols = SCORE_SNAPSHOT_COLUMNS
        values = [data.get(c) for c in cols]
        placeholders = ', '.join(['?'] * len(cols))
        col_names = ', '.join(cols)
        conn = self._get_conn()
        conn.execute(
            adapt_query(f'INSERT INTO score_snapshots ({col_names}) VALUES ({placeholders})'),
            values
        )
        conn.commit()

    def get_score_snapshots(self, journal_id: int) -> list:
        """Get all snapshots for a trade (entry + exit)."""
        conn = self._get_conn()
        rows = conn.execute(
            adapt_query('SELECT * FROM score_snapshots WHERE journal_id = ? ORDER BY captured_at'),
            (journal_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_snapshots_with_outcomes(self, user_id: int, min_date: str = None) -> list:
        """Join entry snapshots with trade outcomes for Bayesian analysis."""
        conn = self._get_conn()
        query = '''
            SELECT s.*, t.rr_actual, t.exit_reason, t.mfe, t.mae,
                   t.target_price as tp1_hit_price,
                   CASE WHEN t.rr_actual IS NOT NULL AND t.rr_actual > 0 THEN 1 ELSE 0 END as win
            FROM score_snapshots s
            JOIN trade_journal t ON s.journal_id = t.id
            WHERE s.user_id = ? AND s.snapshot_type = 'entry'
              AND t.closed_at IS NOT NULL
        '''
        params = [user_id]
        if min_date:
            query += ' AND s.captured_at >= ?'
            params.append(min_date)
        query += ' ORDER BY s.captured_at DESC'
        rows = conn.execute(adapt_query(query), params).fetchall()
        return [dict(r) for r in rows]

    def get_open_trades(self, user_id: int, asset: str = None) -> List[Dict]:
        """Get user's open (unclosed) journal entries."""
        conn = self._get_conn()
        query = 'SELECT * FROM trade_journal WHERE user_id = ? AND closed_at IS NULL'
        params = [user_id]
        if asset:
            query += ' AND asset = ?'
            params.append(asset.upper())
        query += ' ORDER BY opened_at DESC'
        rows = conn.execute(adapt_query(query), params).fetchall()
        return [dict(r) for r in rows]

    def get_trade_history(self, user_id: int, days: int = 30, limit: int = 50,
                          asset: str = None) -> List[Dict]:
        """Get user's closed trades for the last N days, optionally filtered by asset."""
        conn = self._get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        query = '''SELECT * FROM trade_journal
               WHERE user_id = ? AND closed_at IS NOT NULL AND opened_at > ?'''
        params: list = [user_id, cutoff]
        if asset:
            query += ' AND asset = ?'
            params.append(asset.upper())
        query += ' ORDER BY closed_at DESC LIMIT ?'
        params.append(limit)
        rows = conn.execute(adapt_query(query), params).fetchall()
        return [dict(r) for r in rows]

    def get_closed_trades_since(self, user_id: int, since, limit: int = 5000) -> List[Dict]:
        """Closed trades that could have contributed to book value on or after ``since``.

        Deliberately filters on ``closed_at``, NOT ``opened_at`` (unlike
        ``get_trade_history`` above). A position opened long before ``since``
        and closed inside the window was open — and contributing to book
        value — for part of it; bounding on ``opened_at`` instead silently
        drops exactly the longest-held positions, and there is no finite
        ``opened_at`` cutoff that is safe (a position open at ``since`` could
        have been opened arbitrarily long ago). Portfolio curve reconstruction
        (``carrac/portfolio/history.py::reconstruct``) needs every trade that
        MIGHT overlap the window; its own ``_open_on`` check does the exact
        per-day overlap filtering once it has the rows.

        Added as a NEW method rather than changing ``get_trade_history`` so
        that method's existing callers (recent-activity views, which
        genuinely want "opened recently") keep their current behaviour.

        ``since`` may be a ``date``/``datetime`` or an ISO string. ``limit``
        bounds the query for very active accounts; callers that need to know
        whether the cap was hit should check ``len(result) >= limit``.
        """
        conn = self._get_conn()
        since_str = since.isoformat() if hasattr(since, 'isoformat') else str(since)
        rows = conn.execute(
            adapt_query('''SELECT * FROM trade_journal
               WHERE user_id = ? AND closed_at IS NOT NULL AND closed_at >= ?
               ORDER BY closed_at DESC LIMIT ?'''),
            (user_id, since_str, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_journal_stats(self, user_id: int, days: int = 7) -> Dict:
        """Get aggregated trade stats for the last N days."""
        trades = self.get_trade_history(user_id, days=days, limit=200)
        if not trades:
            return {'trades': 0, 'wins': 0, 'losses': 0, 'win_rate': 0, 'avg_rr': 0}

        wins = [t for t in trades if t.get('rr_actual') is not None and t['rr_actual'] > 0]
        losses = [t for t in trades if t.get('rr_actual') is not None and t['rr_actual'] <= 0]
        rr_values = [t['rr_actual'] for t in trades if t.get('rr_actual') is not None]

        # Setup type breakdown
        setup_counts = {}
        for t in trades:
            st = t.get('setup_type') or 'unknown'
            if st not in setup_counts:
                setup_counts[st] = {'total': 0, 'wins': 0}
            setup_counts[st]['total'] += 1
            if t.get('rr_actual') is not None and t['rr_actual'] > 0:
                setup_counts[st]['wins'] += 1

        total = len(wins) + len(losses)

        return {
            'trades': len(trades),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': round(len(wins) / total * 100, 1) if total > 0 else 0,
            'avg_rr': round(sum(rr_values) / len(rr_values), 2) if rr_values else 0,
            'best_rr': round(max(rr_values), 2) if rr_values else 0,
            'worst_rr': round(min(rr_values), 2) if rr_values else 0,
            'setups': setup_counts,
            'period_days': days,
        }

    def find_journal_by_drawing(self, drawing_id: Any) -> Optional[Dict]:
        """Find a journal entry linked to a specific drawing.

        Compared as TEXT.  Prod Postgres declares
        ``trade_journal.drawing_id`` as ``text`` while ``drawings.id`` is
        ``integer``, and Postgres has no implicit ``text = integer``
        comparison — so passing the int straight through raised
        ``UndefinedFunction`` and 500'd the journal page's import-from-drawings
        button.  SQLite hides the mismatch behind column affinity, which is why
        the whole SQLite suite stays green; the test lives in
        ``tests/pg/test_drawing_id_text_column.py``.

        Same coercion the spot-promotion path uses
        (``scripts/backfill_spot_journal.py``, ``selectPromotedTradeId``).
        """
        if drawing_id is None:
            return None
        conn = self._get_conn()
        row = conn.execute(
            adapt_query('SELECT * FROM trade_journal WHERE drawing_id = ? LIMIT 1'),
            (str(drawing_id),)
        ).fetchone()
        return dict(row) if row else None

    def find_candidate_manual_trades(self, user_id: int, asset: str,
                                     start_iso: str, end_iso: str) -> List[Dict]:
        """Trades opened by a HUMAN (no ``src:`` tag) within a time window.

        Used by the exchange-sync dedupe guard: before
        auto-importing a fill-derived trade, the sync engine checks whether
        the user already hand-journaled something that looks like the same
        trade, so it isn't silently duplicated. Excludes already-imported
        rows (identified by a ``src:<exchange>`` tag) so re-syncing never
        "matches" its own earlier imports.
        """
        conn = self._get_conn()
        rows = conn.execute(
            adapt_query('''SELECT * FROM trade_journal
               WHERE user_id = ? AND asset = ? AND opened_at BETWEEN ? AND ?'''),
            (user_id, asset.upper(), start_iso, end_iso)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            tags = d.get('tags')
            is_imported = False
            if tags:
                try:
                    tag_list = json.loads(tags) if isinstance(tags, str) else tags
                    is_imported = any(str(t).startswith('src:') for t in (tag_list or []))
                except (TypeError, ValueError):
                    pass
            if not is_imported:
                out.append(d)
        return out

    def trade_update(self, journal_id: int, **kwargs) -> bool:
        """Update any fields on a journal entry. Recalculates R:R if prices change.

        Pass a value of None to clear nullable text fields (notes, tags, etc.).
        Numeric/price fields ignore None to prevent accidental clears.
        """
        conn = self._get_conn()
        row = conn.execute(adapt_query('SELECT * FROM trade_journal WHERE id = ?'), (journal_id,)).fetchone()
        if not row:
            return False

        allowed = TRADE_UPDATE_FIELDS
        # Fields that can be explicitly set to NULL
        nullable = {'notes', 'setup_type', 'account_tag', 'tags', 'mistake_tags',
                    'checklist_results', 'ai_review', 'trade_grade'}
        updates = {}
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if v is None and k not in nullable:
                continue
            updates[k] = v
        if not updates:
            return False

        # Recalculate planned R:R if any price changed
        entry = updates.get('entry_price', row['entry_price'])
        stop = updates.get('stop_price', row['stop_price'])
        target = updates.get('target_price', row['target_price'])
        if entry and stop and target and stop != entry:
            risk = abs(entry - stop)
            reward = abs(target - entry)
            updates['rr_planned'] = round(reward / risk, 2) if risk > 0 else None

        set_clause = ', '.join(f'{k} = ?' for k in updates)
        values = list(updates.values()) + [journal_id]
        conn.execute(adapt_query(f'UPDATE trade_journal SET {set_clause} WHERE id = ?'), values)
        conn.commit()
        return True

    def reopen_trade(self, journal_id: int) -> bool:
        """Reopen a closed trade by clearing exit data."""
        conn = self._get_conn()
        cursor = conn.execute(
            adapt_query('''UPDATE trade_journal
               SET exit_price = NULL, exit_reason = NULL, rr_actual = NULL, closed_at = NULL
               WHERE id = ?'''),
            (journal_id,)
        )
        conn.commit()
        return cursor.rowcount > 0

    def trade_delete(self, journal_id: int, user_id: int) -> bool:
        """Delete a single trade and its screenshot files. Ownership-checked."""
        deleted = self.delete_trades_bulk([journal_id], user_id)
        return deleted == 1

    def delete_trades_bulk(self, journal_ids: List[int], user_id: int) -> int:
        """Delete multiple trades + screenshots. Returns count of rows actually deleted.

        Silently skips IDs the user doesn't own (matches existing route behavior).

        Atomicity contract:
        - The two DELETEs (journal_screenshots + trade_journal) are wrapped in
          _pg_atomic() so they land in a single transaction on PostgreSQL.
        - Filesystem os.remove() calls happen AFTER the transaction commits to
          avoid a partial failure leaving orphaned files with no DB rows, or
          leaving DB rows with missing files.  Collect paths inside the txn,
          delete files only on success.
        """
        if not journal_ids:
            return 0

        conn = self._get_conn()

        # Filter to owned IDs only (read outside the write transaction — safe
        # because we own these rows by user_id check; a concurrent delete by
        # the same user would just make the inner DELETEs a no-op).
        placeholders = ','.join(['?'] * len(journal_ids))
        rows = conn.execute(
            adapt_query(f'SELECT id FROM trade_journal WHERE id IN ({placeholders}) AND user_id = ?'),
            list(journal_ids) + [user_id],
        ).fetchall()
        owned = [r['id'] for r in rows]
        if not owned:
            return 0

        ph = ','.join(['?'] * len(owned))

        # Collect screenshot file paths inside a read before the write txn so
        # we know what to delete from disk after commit.
        screenshot_rows = conn.execute(
            adapt_query(f'SELECT file_path FROM journal_screenshots WHERE journal_id IN ({ph}) AND user_id = ?'),
            owned + [user_id],
        ).fetchall()

        # Atomic DB writes: both DELETEs succeed or both roll back.
        with self._pg_atomic():
            conn.execute(
                adapt_query(f'DELETE FROM journal_screenshots WHERE journal_id IN ({ph}) AND user_id = ?'),
                owned + [user_id],
            )
            conn.execute(
                adapt_query(f'DELETE FROM trade_journal WHERE id IN ({ph}) AND user_id = ?'),
                owned + [user_id],
            )
            conn.commit()

        # Only remove files after the transaction has committed so a DB failure
        # never leaves rows deleted while files survive (or vice versa).
        data_dir = os.environ.get('DATA_DIR', 'data')
        base = os.path.realpath(data_dir)
        for s in screenshot_rows:
            rel = s['file_path']
            if not rel:
                continue
            full = os.path.realpath(os.path.join(data_dir, rel))
            # Path-traversal guard: must stay under DATA_DIR
            if full != base and not full.startswith(base + os.sep):
                continue
            if os.path.isfile(full):
                try:
                    os.remove(full)
                except OSError:
                    pass

        return len(owned)

    def get_journal_entry(self, journal_id: int) -> Optional[Dict]:
        """Get a single journal entry by ID."""
        conn = self._get_conn()
        row = conn.execute(adapt_query('SELECT * FROM trade_journal WHERE id = ?'), (journal_id,)).fetchone()
        return dict(row) if row else None

    def get_journal_analytics_range(self, user_id: int, days_ago_start: int, days_ago_end: int) -> Dict:
        """Get basic analytics for an offset time window (for period comparison).

        E.g. days_ago_start=60, days_ago_end=30 -> trades closed 30-60 days ago.
        """
        conn = self._get_conn()
        start = (datetime.now() - timedelta(days=days_ago_start)).isoformat()
        end = (datetime.now() - timedelta(days=days_ago_end)).isoformat()
        rows = conn.execute(
            adapt_query('''SELECT rr_actual FROM trade_journal
               WHERE user_id = ? AND closed_at IS NOT NULL AND rr_actual IS NOT NULL
               AND opened_at > ? AND opened_at <= ?
               ORDER BY closed_at DESC LIMIT 500'''),
            (user_id, start, end)
        ).fetchall()
        if not rows:
            return {'trades': 0}
        rr_values = [r['rr_actual'] for r in rows]
        wins = sum(1 for r in rr_values if r >= 0)
        total = len(rr_values)
        wr = round(wins / total * 100, 1) if total else 0
        avg_w = round(sum(r for r in rr_values if r >= 0) / wins, 2) if wins else 0
        losses = total - wins
        avg_l = round(abs(sum(r for r in rr_values if r < 0)) / losses, 2) if losses else 0
        exp = round((wr / 100) * avg_w - ((100 - wr) / 100) * avg_l, 3) if total else 0
        return {
            'trades': total,
            'win_rate': wr,
            'expectancy': exp,
        }

    def get_journal_analytics(self, user_id: int, days: int = 90) -> Dict:
        """Comprehensive trade analytics for the journal analytics panel."""
        trades = self.get_trade_history(user_id, days=days, limit=500)
        if not trades:
            return {'trades': 0, 'period_days': days}

        # Separate wins / losses (only trades with rr_actual)
        scored = [t for t in trades if t.get('rr_actual') is not None]
        wins = [t for t in scored if t['rr_actual'] > 0]
        losses = [t for t in scored if t['rr_actual'] <= 0]
        total_scored = len(wins) + len(losses)

        win_rate = round(len(wins) / total_scored * 100, 1) if total_scored else 0
        loss_rate = 100 - win_rate if total_scored else 0
        rr_values = [t['rr_actual'] for t in scored]

        avg_win_rr = round(sum(t['rr_actual'] for t in wins) / len(wins), 2) if wins else 0
        avg_loss_rr = round(abs(sum(t['rr_actual'] for t in losses) / len(losses)), 2) if losses else 0

        # Expectancy: (WR * avg_win) - (LR * avg_loss)
        wr_dec = win_rate / 100
        lr_dec = loss_rate / 100
        expectancy = round(wr_dec * avg_win_rr - lr_dec * avg_loss_rr, 3) if total_scored else 0

        # Profit factor: gross_win / gross_loss
        gross_win = sum(t['rr_actual'] for t in wins) if wins else 0
        gross_loss = abs(sum(t['rr_actual'] for t in losses)) if losses else 0
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

        # Avg hold time
        avg_hold_hours = None
        hold_hours = []
        for t in scored:
            if t.get('opened_at') and t.get('closed_at'):
                try:
                    opened_str = str(t['opened_at']).replace('Z', '+00:00').split('+')[0]
                    closed_str = str(t['closed_at']).replace('Z', '+00:00').split('+')[0]
                    opened = datetime.fromisoformat(opened_str)
                    closed = datetime.fromisoformat(closed_str)
                    h = (closed - opened).total_seconds() / 3600
                    if h >= 0:
                        hold_hours.append(h)
                except Exception:
                    pass
        if hold_hours:
            avg_hold_hours = round(sum(hold_hours) / len(hold_hours), 1)

        # Streaks (breakeven = non-loss)
        longest_win = longest_loss = current_count = 0
        current_type = None
        for t in reversed(scored):  # chronological order
            is_win = t['rr_actual'] >= 0
            if current_type is None:
                current_type = 'win' if is_win else 'loss'
                current_count = 1
            elif (is_win and current_type == 'win') or (not is_win and current_type == 'loss'):
                current_count += 1
            else:
                current_type = 'win' if is_win else 'loss'
                current_count = 1
            if current_type == 'win':
                longest_win = max(longest_win, current_count)
            else:
                longest_loss = max(longest_loss, current_count)

        # Current streak (from most recent trade)
        cur_streak_type = None
        cur_streak_count = 0
        for t in scored:  # newest first
            is_win = t['rr_actual'] >= 0
            stype = 'win' if is_win else 'loss'
            if cur_streak_type is None:
                cur_streak_type = stype
                cur_streak_count = 1
            elif stype == cur_streak_type:
                cur_streak_count += 1
            else:
                break

        # Breakdowns
        def _group_stats(subset):
            if not subset:
                return {'trades': 0, 'wins': 0, 'losses': 0, 'win_rate': 0,
                        'avg_rr': 0, 'net_rr': 0, 'expectancy': 0}
            s = [t for t in subset if t.get('rr_actual') is not None]
            w = [t for t in s if t['rr_actual'] >= 0]
            l = [t for t in s if t['rr_actual'] < 0]
            total = len(w) + len(l)
            rrs = [t['rr_actual'] for t in s]
            wr = round(len(w) / total * 100, 1) if total else 0
            avg_w = round(sum(t['rr_actual'] for t in w) / len(w), 2) if w else 0
            avg_l = round(abs(sum(t['rr_actual'] for t in l) / len(l)), 2) if l else 0
            exp = round((wr / 100) * avg_w - ((100 - wr) / 100) * avg_l, 3) if total else 0
            return {
                'trades': len(subset),
                'wins': len(w),
                'losses': len(l),
                'win_rate': wr,
                'avg_rr': round(sum(rrs) / len(rrs), 2) if rrs else 0,
                'net_rr': round(sum(rrs), 2) if rrs else 0,
                'expectancy': exp,
            }

        # By setup
        by_setup = {}
        for t in trades:
            key = t.get('setup_type') or 'unknown'
            by_setup.setdefault(key, []).append(t)
        by_setup = {k: _group_stats(v) for k, v in by_setup.items()}

        # By timeframe
        by_timeframe = {}
        for t in trades:
            key = t.get('timeframe') or 'unknown'
            by_timeframe.setdefault(key, []).append(t)
        by_timeframe = {k: _group_stats(v) for k, v in by_timeframe.items()}

        # By asset
        by_asset = {}
        for t in trades:
            key = t.get('asset') or 'unknown'
            by_asset.setdefault(key, []).append(t)
        by_asset = {k: _group_stats(v) for k, v in by_asset.items()}

        # By direction
        by_direction = {}
        for t in trades:
            key = t.get('direction') or 'unknown'
            by_direction.setdefault(key, []).append(t)
        by_direction = {k: _group_stats(v) for k, v in by_direction.items()}

        # By weekday
        by_weekday = {}
        day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        for t in trades:
            try:
                dt = datetime.fromisoformat(str(t['opened_at']).replace('Z', '+00:00').split('+')[0])
                key = day_names[dt.weekday()]
            except Exception:
                key = 'unknown'
            by_weekday.setdefault(key, []).append(t)
        by_weekday = {k: _group_stats(v) for k, v in by_weekday.items()}

        # Monthly equity curve
        monthly = {}
        for t in scored:
            try:
                dt = datetime.fromisoformat(str(t['closed_at']).replace('Z', '+00:00').split('+')[0])
                key = dt.strftime('%Y-%m')
            except Exception:
                continue
            if key not in monthly:
                monthly[key] = {'month': key, 'trades': 0, 'net_rr': 0}
            monthly[key]['trades'] += 1
            monthly[key]['net_rr'] = round(monthly[key]['net_rr'] + t['rr_actual'], 2)
        monthly_list = sorted(monthly.values(), key=lambda x: x['month'])

        # By time-of-day (4-hour buckets)
        hour_labels = ['00-04', '04-08', '08-12', '12-16', '16-20', '20-24']
        by_hour = {}
        for t in trades:
            try:
                dt = datetime.fromisoformat(str(t['opened_at']).replace('Z', '+00:00').split('+')[0])
                bucket = hour_labels[dt.hour // 4]
            except Exception:
                bucket = 'unknown'
            by_hour.setdefault(bucket, []).append(t)
        by_hour = {k: _group_stats(v) for k, v in by_hour.items()}

        # By duration bucket
        by_duration = {}
        for t in trades:
            h = None
            if t.get('opened_at') and t.get('closed_at'):
                try:
                    o = datetime.fromisoformat(str(t['opened_at']).replace('Z', '+00:00').split('+')[0])
                    c = datetime.fromisoformat(str(t['closed_at']).replace('Z', '+00:00').split('+')[0])
                    h = (c - o).total_seconds() / 3600
                except Exception:
                    pass
            if h is None:
                key = 'unknown'
            elif h < 1:
                key = 'scalp'
            elif h < 8:
                key = 'intraday'
            elif h < 168:
                key = 'swing'
            else:
                key = 'position'
            by_duration.setdefault(key, []).append(t)
        by_duration = {k: _group_stats(v) for k, v in by_duration.items()}

        # R-multiple histogram (0.5R buckets from -3R to +5R)
        r_histogram = []
        bucket_edges = [i * 0.5 for i in range(-6, 11)]  # -3.0 to +5.0
        for i in range(len(bucket_edges) - 1):
            lo, hi = bucket_edges[i], bucket_edges[i + 1]
            label = f'{lo:+.1f}'
            count = sum(1 for rr in rr_values if lo <= rr < hi)
            if i == len(bucket_edges) - 2:  # last bucket is inclusive
                count = sum(1 for rr in rr_values if lo <= rr <= hi)
            r_histogram.append({'bucket': label, 'lo': lo, 'hi': hi, 'count': count})
        # Overflow buckets
        below = sum(1 for rr in rr_values if rr < -3.0)
        above = sum(1 for rr in rr_values if rr > 5.0)
        if below:
            r_histogram.insert(0, {'bucket': '<-3.0', 'lo': None, 'hi': -3.0, 'count': below})
        if above:
            r_histogram.append({'bucket': '>+5.0', 'lo': 5.0, 'hi': None, 'count': above})

        # MFE/MAE scatter data (trades that have both)
        mfe_mae = []
        exit_effs = []
        for t in scored:
            if t.get('mfe') is not None or t.get('mae') is not None:
                mfe_mae.append({
                    'trade_id': t['id'],
                    'asset': t.get('asset'),
                    'mfe': t.get('mfe'),
                    'mae': t.get('mae'),
                    'rr_actual': t['rr_actual'],
                    'is_win': t['rr_actual'] >= 0,
                    'exit_efficiency': t.get('exit_efficiency'),
                })
            if t.get('exit_efficiency') is not None:
                exit_effs.append(t['exit_efficiency'])
        avg_exit_efficiency = round(sum(exit_effs) / len(exit_effs), 2) if exit_effs else None

        # Rule adherence: trades with full/partial/no checklist
        by_adherence = {'full': [], 'partial': [], 'none': []}
        for t in trades:
            cr = t.get('checklist_results')
            if not cr:
                by_adherence['none'].append(t)
                continue
            try:
                results = json.loads(cr) if isinstance(cr, str) else cr
                if isinstance(results, dict) and results:
                    vals = list(results.values())
                    if all(vals):
                        by_adherence['full'].append(t)
                    else:
                        by_adherence['partial'].append(t)
                else:
                    by_adherence['none'].append(t)
            except (json.JSONDecodeError, TypeError):
                by_adherence['none'].append(t)
        rule_adherence = {k: _group_stats(v) for k, v in by_adherence.items()}

        # Streak history (for chart)
        streak_history = []
        s_type = None
        s_count = 0
        for t in reversed(scored):  # chronological
            is_win = t['rr_actual'] >= 0
            stype = 'win' if is_win else 'loss'
            if s_type is None:
                s_type = stype
                s_count = 1
            elif stype == s_type:
                s_count += 1
            else:
                streak_history.append({'type': s_type, 'count': s_count})
                s_type = stype
                s_count = 1
        if s_type:
            streak_history.append({'type': s_type, 'count': s_count})

        # Max drawdown (R)
        peak_r = 0
        max_dd = 0
        cum_r = 0
        for t in reversed(scored):
            cum_r += t['rr_actual']
            peak_r = max(peak_r, cum_r)
            dd = cum_r - peak_r
            max_dd = min(max_dd, dd)

        return {
            'period_days': days,
            'trades': len(trades),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': win_rate,
            'avg_rr': round(sum(rr_values) / len(rr_values), 2) if rr_values else 0,
            'best_rr': round(max(rr_values), 2) if rr_values else 0,
            'worst_rr': round(min(rr_values), 2) if rr_values else 0,
            'avg_win_rr': avg_win_rr,
            'avg_loss_rr': avg_loss_rr,
            'expectancy': expectancy,
            'profit_factor': profit_factor,
            'avg_hold_hours': avg_hold_hours,
            'max_drawdown': round(max_dd, 2),
            'longest_win_streak': longest_win,
            'longest_loss_streak': longest_loss,
            'current_streak': {'type': cur_streak_type or 'none', 'count': cur_streak_count},
            'streak_history': streak_history,
            'by_setup': by_setup,
            'by_timeframe': by_timeframe,
            'by_asset': by_asset,
            'by_direction': by_direction,
            'by_weekday': by_weekday,
            'by_hour': by_hour,
            'by_duration': by_duration,
            'r_histogram': r_histogram,
            'mfe_mae': mfe_mae,
            'avg_exit_efficiency': avg_exit_efficiency,
            'rule_adherence': rule_adherence,
            'monthly': monthly_list,
        }

    # ==================== Journal Screenshots ====================

    def get_trade_screenshots(self, journal_id: int) -> List[Dict]:
        """Get all screenshots for a trade."""
        conn = self._get_conn()
        rows = conn.execute(
            adapt_query('SELECT * FROM journal_screenshots WHERE journal_id = ? ORDER BY captured_at'),
            (journal_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ==================== Setup Checklists ====================

    def get_user_checklists(self, user_id: int) -> List[Dict]:
        """Get all checklist templates for a user."""
        conn = self._get_conn()
        rows = conn.execute(
            adapt_query('SELECT * FROM setup_checklists WHERE user_id = ? ORDER BY setup_type'),
            (user_id,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d['checklist_items'] = json.loads(d['checklist_items'])
            except (json.JSONDecodeError, TypeError):
                pass
            result.append(d)
        return result

    def get_checklist_for_setup(self, user_id: int, setup_type: str) -> Optional[Dict]:
        """Get checklist template for a specific setup type."""
        conn = self._get_conn()
        row = conn.execute(
            adapt_query('SELECT * FROM setup_checklists WHERE user_id = ? AND setup_type = ?'),
            (user_id, setup_type)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d['checklist_items'] = json.loads(d['checklist_items'])
        except (json.JSONDecodeError, TypeError):
            pass
        return d

    def upsert_checklist(self, user_id: int, setup_type: str,
                         name: str, items: list) -> int:
        """Create or update a checklist template. Returns checklist id."""
        conn = self._get_conn()
        items_json = json.dumps(items)
        # Try update first
        existing = conn.execute(
            adapt_query('SELECT id FROM setup_checklists WHERE user_id = ? AND setup_type = ?'),
            (user_id, setup_type)
        ).fetchone()
        if existing:
            conn.execute(
                adapt_query('UPDATE setup_checklists SET name = ?, checklist_items = ? WHERE id = ?'),
                (name, items_json, existing['id'])
            )
            conn.commit()
            return existing['id']
        else:
            cursor = conn.execute(
                adapt_query('''INSERT INTO setup_checklists (user_id, setup_type, name, checklist_items)
                   VALUES (?, ?, ?, ?)''' + returning_id()),
                (user_id, setup_type, name, items_json)
            )
            conn.commit()
            return get_last_id(cursor)

    # ==================== Sessions ====================

    def create_session(self, user_id: int, date: str, session_label: str = None,
                       pre_notes: str = None, mood_pre: int = None) -> int:
        """Create a trading session. Returns session id.

        Uses INSERT … ON CONFLICT DO NOTHING to eliminate the SELECT-then-INSERT
        TOCTOU race.  Two concurrent requests for the same
        (user_id, date, session_label) can no longer both hit the SELECT without
        a row and then both attempt INSERT — the second INSERT is silently
        swallowed by the database and we fall through to the SELECT that
        retrieves the winner's row.  This avoids the UniqueViolation-as-500
        that a plain check-then-insert pattern can raise under load.
        """
        conn = self._get_conn()
        label = session_label or 'default'

        if get_db_type() == 'postgresql':
            # PostgreSQL: atomic upsert — insert if not exists, return id either way.
            cursor = conn.execute(
                adapt_query('''
                    INSERT INTO journal_sessions (user_id, date, session_label, pre_notes, mood_pre)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (user_id, date, session_label) DO NOTHING
                ''' + returning_id()),
                (user_id, date, label, pre_notes, mood_pre),
            )
            row_id = get_last_id(cursor)
            if row_id:
                return row_id
            # Conflict path: the row already existed — fetch it.
            existing = conn.execute(
                adapt_query('SELECT id FROM journal_sessions WHERE user_id = ? AND date = ? AND session_label = ?'),
                (user_id, date, label),
            ).fetchone()
            return existing['id']
        else:
            # SQLite: INSERT OR IGNORE + SELECT fallback (same semantic, no RETURNING).
            conn.execute(
                adapt_query('''INSERT OR IGNORE INTO journal_sessions
                    (user_id, date, session_label, pre_notes, mood_pre)
                    VALUES (?, ?, ?, ?, ?)'''),
                (user_id, date, label, pre_notes, mood_pre),
            )
            conn.commit()
            existing = conn.execute(
                adapt_query('SELECT id FROM journal_sessions WHERE user_id = ? AND date = ? AND session_label = ?'),
                (user_id, date, label),
            ).fetchone()
            return existing['id']

    def update_session(self, session_id: int, **kwargs) -> bool:
        """Update session fields."""
        conn = self._get_conn()
        allowed = SESSION_UPDATE_FIELDS
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        set_clause = ', '.join(f'{k} = ?' for k in updates)
        values = list(updates.values()) + [session_id]
        conn.execute(adapt_query(f'UPDATE journal_sessions SET {set_clause} WHERE id = ?'), values)
        conn.commit()
        return True

    def get_sessions(self, user_id: int, year: int = None, month: int = None) -> List[Dict]:
        """Get sessions for a user, optionally filtered by year/month."""
        conn = self._get_conn()
        if year and month:
            prefix = f'{year:04d}-{month:02d}'
            rows = conn.execute(
                adapt_query('''SELECT * FROM journal_sessions
                   WHERE user_id = ? AND date LIKE ?
                   ORDER BY date DESC, session_label'''),
                (user_id, prefix + '%')
            ).fetchall()
        else:
            rows = conn.execute(
                adapt_query('SELECT * FROM journal_sessions WHERE user_id = ? ORDER BY date DESC LIMIT 60'),
                (user_id,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get('rules_followed'):
                try:
                    d['rules_followed'] = json.loads(d['rules_followed'])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(d)
        return result

    def get_session(self, session_id: int) -> Optional[Dict]:
        """Get a single session by id."""
        conn = self._get_conn()
        row = conn.execute(adapt_query('SELECT * FROM journal_sessions WHERE id = ?'), (session_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get('rules_followed'):
            try:
                d['rules_followed'] = json.loads(d['rules_followed'])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def get_session_trades(self, session_id: int) -> List[Dict]:
        """Get all trades linked to a session."""
        conn = self._get_conn()
        rows = conn.execute(
            adapt_query('SELECT * FROM trade_journal WHERE session_id = ? ORDER BY opened_at'),
            (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id: int, user_id: int) -> bool:
        """Delete a session and unlink its trades."""
        conn = self._get_conn()
        with self._pg_atomic():
            conn.execute(
                adapt_query('UPDATE trade_journal SET session_id = NULL WHERE session_id = ? AND user_id = ?'),
                (session_id, user_id)
            )
            cursor = conn.execute(
                adapt_query('DELETE FROM journal_sessions WHERE id = ? AND user_id = ?'),
                (session_id, user_id)
            )
            conn.commit()
        return cursor.rowcount > 0

    def get_or_create_session_for_date(self, user_id: int, date: str) -> int:
        """Get existing session or create a stub for auto-linking. Returns session id.

        Delegates entirely to create_session() which is now atomic
        (INSERT … ON CONFLICT DO NOTHING) so the duplicate SELECT here is
        unnecessary and has been removed.
        """
        return self.create_session(user_id, date)

    # ==================== Calendar / Equity Curve ====================

    def get_calendar_data(self, user_id: int, year: int, month: int) -> Dict:
        """Get daily trade aggregates for a calendar month."""
        conn = self._get_conn()
        start = f'{year:04d}-{month:02d}-01'
        if month == 12:
            end = f'{year + 1:04d}-01-01'
        else:
            end = f'{year:04d}-{month + 1:02d}-01'

        rows = conn.execute(
            adapt_query('''SELECT * FROM trade_journal
               WHERE user_id = ? AND closed_at >= ? AND closed_at < ?
               ORDER BY closed_at'''),
            (user_id, start, end)
        ).fetchall()

        # Also get sessions for the month
        sessions = conn.execute(
            adapt_query('''SELECT * FROM journal_sessions
               WHERE user_id = ? AND date >= ? AND date < ?
               ORDER BY date'''),
            (user_id, start, end)
        ).fetchall()

        session_map = {}
        for s in sessions:
            d = dict(s)
            session_map.setdefault(str(d['date']), []).append(d)

        # Aggregate by day
        days = {}
        for r in rows:
            t = dict(r)
            try:
                closed = str(t['closed_at']).split('T')[0].split(' ')[0]
            except (AttributeError, IndexError):
                continue
            if closed not in days:
                days[closed] = {'date': closed, 'trades': 0, 'wins': 0, 'losses': 0,
                                'net_r': 0, 'best_r': None, 'worst_r': None, 'moods': []}
            day = days[closed]
            day['trades'] += 1
            rr = t.get('rr_actual')
            if rr is not None:
                if rr > 0:
                    day['wins'] += 1
                else:
                    day['losses'] += 1
                day['net_r'] = round(day['net_r'] + rr, 2)
                if day['best_r'] is None or rr > day['best_r']:
                    day['best_r'] = round(rr, 2)
                if day['worst_r'] is None or rr < day['worst_r']:
                    day['worst_r'] = round(rr, 2)
            mood = t.get('mood')
            if mood:
                day['moods'].append(mood)

        # Calculate avg mood per day and attach session info
        for date_str, day in days.items():
            moods = day.pop('moods')
            day['avg_mood'] = round(sum(moods) / len(moods), 1) if moods else None
            day['has_session'] = date_str in session_map

        # Month summary
        all_rr = []
        for day in days.values():
            if day['net_r'] != 0:
                all_rr.append(day['net_r'])

        return {
            'year': year,
            'month': month,
            'days': days,
            'sessions': {k: [dict(s) for s in v] for k, v in session_map.items()},
            'summary': {
                'total_trades': sum(d['trades'] for d in days.values()),
                'total_net_r': round(sum(d['net_r'] for d in days.values()), 2),
                'trading_days': len(days),
                'best_day_r': max(all_rr) if all_rr else 0,
                'worst_day_r': min(all_rr) if all_rr else 0,
            }
        }

    def get_equity_curve(self, user_id: int, days: int = 90) -> List[Dict]:
        """Get cumulative R series for equity curve. One point per closed trade."""
        conn = self._get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            adapt_query('''SELECT id, asset, closed_at, rr_actual, pnl_currency
               FROM trade_journal
               WHERE user_id = ? AND closed_at IS NOT NULL AND rr_actual IS NOT NULL
                     AND opened_at > ?
               ORDER BY closed_at ASC
               LIMIT 2000'''),
            (user_id, cutoff)
        ).fetchall()

        curve = []
        cumulative_r = 0
        peak_r = 0
        for r in rows:
            t = dict(r)
            cumulative_r = round(cumulative_r + t['rr_actual'], 2)
            peak_r = max(peak_r, cumulative_r)
            drawdown = round(cumulative_r - peak_r, 2)
            curve.append({
                'trade_id': t['id'],
                'asset': t['asset'],
                'closed_at': t['closed_at'],
                'r': t['rr_actual'],
                'cumulative_r': cumulative_r,
                'drawdown': drawdown,
                'pnl': t.get('pnl_currency'),
            })

        return curve

    # -- Playbooks --

    def get_playbooks(self, user_id: int) -> List[Dict]:
        conn = self._get_conn()
        rows = conn.execute(
            adapt_query('SELECT * FROM journal_playbooks WHERE user_id = ? ORDER BY created_at DESC'),
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_playbook(self, playbook_id: int) -> Optional[Dict]:
        conn = self._get_conn()
        row = conn.execute(adapt_query('SELECT * FROM journal_playbooks WHERE id = ?'), (playbook_id,)).fetchone()
        return dict(row) if row else None

    def create_playbook(self, user_id: int, name: str, setup_type: str = None,
                        description: str = None, ideal_conditions: str = None,
                        entry_rules: str = None, exit_rules: str = None) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            adapt_query('''INSERT INTO journal_playbooks (user_id, name, setup_type, description,
               ideal_conditions, entry_rules, exit_rules) VALUES (?,?,?,?,?,?,?)''' + returning_id()),
            (user_id, name, setup_type, description, ideal_conditions, entry_rules, exit_rules)
        )
        conn.commit()
        return get_last_id(cur)

    def update_playbook(self, playbook_id: int, **kwargs) -> bool:
        if not kwargs:
            return False
        conn = self._get_conn()
        allowed = PLAYBOOK_UPDATE_FIELDS
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k in allowed:
                sets.append(f'{k} = ?')
                vals.append(v)
        if not sets:
            return False
        vals.append(playbook_id)
        conn.execute(adapt_query(f"UPDATE journal_playbooks SET {', '.join(sets)} WHERE id = ?"), vals)
        conn.commit()
        return True

    def delete_playbook(self, playbook_id: int, user_id: int) -> bool:
        conn = self._get_conn()
        conn.execute(adapt_query('DELETE FROM journal_playbooks WHERE id = ? AND user_id = ?'),
                     (playbook_id, user_id))
        conn.commit()
        return True

    def get_playbook_stats(self, user_id: int, setup_type: str) -> Dict:
        """Get live stats for trades matching a playbook's setup_type."""
        conn = self._get_conn()
        rows = conn.execute(
            adapt_query('''SELECT rr_actual FROM trade_journal
               WHERE user_id = ? AND setup_type = ? AND rr_actual IS NOT NULL'''),
            (user_id, setup_type)
        ).fetchall()
        if not rows:
            return {'trades': 0}
        rr_values = [r['rr_actual'] for r in rows]
        wins = sum(1 for r in rr_values if r > 0)
        return {
            'trades': len(rr_values),
            'wins': wins,
            'losses': len(rr_values) - wins,
            'win_rate': round(wins / len(rr_values) * 100, 1),
            'avg_r': round(sum(rr_values) / len(rr_values), 2),
            'net_r': round(sum(rr_values), 2),
            'best_r': round(max(rr_values), 2),
            'worst_r': round(min(rr_values), 2),
        }

    # -- Goals --

    def get_goals(self, user_id: int, month: str) -> Optional[Dict]:
        conn = self._get_conn()
        row = conn.execute(
            adapt_query('SELECT * FROM journal_goals WHERE user_id = ? AND month = ?'),
            (user_id, month)
        ).fetchone()
        return dict(row) if row else None

    def get_all_goals(self, user_id: int) -> List[Dict]:
        """Get all goals for a user across all months (for export)."""
        conn = self._get_conn()
        rows = conn.execute(
            adapt_query('SELECT * FROM journal_goals WHERE user_id = ? ORDER BY month DESC'),
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_goals(self, user_id: int, month: str, goals: str) -> int:
        conn = self._get_conn()
        existing = conn.execute(
            adapt_query('SELECT id FROM journal_goals WHERE user_id = ? AND month = ?'),
            (user_id, month)
        ).fetchone()
        if existing:
            conn.execute(
                adapt_query('UPDATE journal_goals SET goals = ? WHERE id = ?'),
                (goals, existing['id'])
            )
            conn.commit()
            return existing['id']
        cur = conn.execute(
            adapt_query('INSERT INTO journal_goals (user_id, month, goals) VALUES (?,?,?)' + returning_id()),
            (user_id, month, goals)
        )
        conn.commit()
        return get_last_id(cur)

    def get_goal_progress(self, user_id: int, month: str) -> Dict:
        """Calculate goal progress from trade data for the given month."""
        conn = self._get_conn()

        if get_db_type() == 'postgresql':
            rows = conn.execute(
                '''SELECT * FROM trade_journal
                   WHERE user_id = %s AND closed_at IS NOT NULL
                   AND to_char(closed_at, 'YYYY-MM') = %s''',
                (user_id, month)
            ).fetchall()
        else:
            rows = conn.execute(
                '''SELECT * FROM trade_journal
                   WHERE user_id = ? AND closed_at IS NOT NULL
                   AND strftime('%Y-%m', closed_at) = ?''',
                (user_id, month)
            ).fetchall()

        if not rows:
            return {'trades': 0, 'trading_days': 0}

        trades = [dict(r) for r in rows]
        # Group by day
        by_day = {}
        for t in trades:
            day = str(t['closed_at'])[:10] if t.get('closed_at') else ''
            if day not in by_day:
                by_day[day] = []
            by_day[day].append(t)

        rr_values = [t['rr_actual'] for t in trades if t.get('rr_actual') is not None]
        daily_r = {}
        for day, day_trades in by_day.items():
            daily_r[day] = sum(t['rr_actual'] for t in day_trades if t.get('rr_actual') is not None)

        max_daily_loss = min(daily_r.values()) if daily_r else 0
        max_trades_day = max(len(v) for v in by_day.values()) if by_day else 0
        consecutive_losses = 0
        max_consecutive = 0
        for rr in rr_values:
            if rr <= 0:
                consecutive_losses += 1
                max_consecutive = max(max_consecutive, consecutive_losses)
            else:
                consecutive_losses = 0

        return {
            'trades': len(trades),
            'trading_days': len(by_day),
            'max_daily_loss_r': round(max_daily_loss, 2),
            'max_trades_per_day': max_trades_day,
            'max_consecutive_losses': max_consecutive,
            'avg_rr': round(sum(rr_values) / len(rr_values), 2) if rr_values else 0,
            'net_r': round(sum(rr_values), 2) if rr_values else 0,
        }

    # -- Export --

    def export_trades_csv(self, user_id: int, days: int = 365) -> List[Dict]:
        """Export all trades as list of dicts (for CSV generation)."""
        conn = self._get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            adapt_query('''SELECT * FROM trade_journal WHERE user_id = ?
               AND opened_at >= ?
               ORDER BY opened_at DESC'''),
            (user_id, cutoff)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        """Close the database connection."""
        if get_db_type() == 'postgresql':
            if hasattr(self._local, 'pg_conn') and self._local.pg_conn:
                try:
                    from carrac.db_pool import _init_pg_pool
                    pool = _init_pg_pool()
                    pool.putconn(self._local.pg_conn)
                except Exception:
                    try:
                        self._local.pg_conn.close()
                    except Exception:
                        pass
                self._local.pg_conn = None
        else:
            if hasattr(self._local, 'conn') and self._local.conn:
                self._local.conn.close()
                self._local.conn = None


# Global instance for easy import
_db_instance: Optional[ContentDatabase] = None


def get_content_db(db_path: Optional[str] = None) -> ContentDatabase:
    """Get or create the global ContentDatabase instance."""
    global _db_instance
    if _db_instance is None:
        path = db_path or os.path.join(os.environ.get('DATA_DIR', 'data'), 'drawings.db')
        _db_instance = ContentDatabase(path)
    return _db_instance
