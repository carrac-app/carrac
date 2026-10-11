"""Scanner history — universe-wide daily snapshot for score evaluation.

Capture-first: persist one `scanner_history` row per symbol×direction per UTC day so
that scanner verdicts can later be joined to forward outcomes (N-bar returns) and the
hit-rate per criterion×verdict×regime measured. NO reweighting happens here — this is
pure data capture.

Settled-bar gating: the scanner scores the live (possibly in-progress) HTF candle, and
nothing in the data layer exposes candle-close state. So we gate capture to a late-UTC
window (>= CAPTURE_WINDOW_START_HOUR_UTC), by which point the 1D bar is ~95% elapsed,
and record bar_settled=True truthfully. The precompute loop runs every ~60s; the first
cycle inside the window inserts, the rest hit ON CONFLICT DO NOTHING.
"""
import json
import logging
from datetime import datetime, timezone

from carrac.db_pool import get_conn, adapt_query, get_db_type

logger = logging.getLogger('carrac.scanner.history')

# UTC hour at/after which the daily snapshot is captured (1D bar ~settled by then).
CAPTURE_WINDOW_START_HOUR_UTC = 23


# alembic is skipped on SQLite (dev/OSS installs), so nothing else
# ever created this table there and every capture died on "no such table"
# — an ERROR log per cycle inside the window and no history, forever. Mirrors
# the Postgres migration (20260527 + 20260714); Postgres stays alembic-owned.
_SQLITE_DDL = (
    'CREATE TABLE IF NOT EXISTS scanner_history ('
    ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
    ' captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,'
    ' captured_date TEXT NOT NULL,'
    ' symbol TEXT NOT NULL,'
    ' direction TEXT NOT NULL,'
    ' regime TEXT NOT NULL,'
    ' total INTEGER NOT NULL,'
    ' verdict_v1 TEXT NOT NULL,'
    ' criteria TEXT NOT NULL,'
    ' vetoes TEXT,'
    ' price REAL,'
    ' bar_settled INTEGER NOT NULL DEFAULT 0,'
    ' setup_type TEXT,'
    ' setup_confidence REAL,'
    ' UNIQUE (symbol, captured_date, direction))'
)


def _ensure_sqlite_table(conn):
    """SQLite only: create scanner_history if missing. No-op on Postgres."""
    if get_db_type() != 'sqlite':
        return
    conn.execute(_SQLITE_DDL)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_scanner_history_captured_at '
                 'ON scanner_history(captured_at DESC)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_scanner_history_symbol_date '
                 'ON scanner_history(symbol, captured_date DESC)')


def _latest_regime():
    """Most recent regime label from the regime_history table, or None.

    The in-memory regime cache (5-min TTL) is reliably cold in the late-UTC
    capture window, so the caller often passes 'Unknown'. regime_history is the
    persistent record the precompute loop writes each cycle — authoritative and
    always fresh-enough. `timestamp` is an ISO-8601 text column, so DESC sorts
    chronologically. Never raises (a lookup failure must not break capture).
    """
    try:
        with get_conn() as conn:
            row = conn.execute(
                'SELECT regime FROM regime_history ORDER BY timestamp DESC LIMIT 1'
            ).fetchone()
        if row is not None:
            return dict(row).get('regime') or None
    except Exception:
        logger.debug("scanner_history: regime_history fallback lookup failed", exc_info=True)
    return None


def upsert_daily_snapshot(scanner_data, regime=None):
    """Persist one scanner_history row per symbol×direction for today's UTC date.

    Best-effort + idempotent: time-gated to the late-UTC capture window, and
    ON CONFLICT DO NOTHING so the first cycle in the window wins for the day.
    Never raises (a DB failure must never break the scanner cache rebuild).
    Returns the number of rows attempted (0 if gated out / on error).
    """
    try:
        now = datetime.now(timezone.utc)
        if now.hour < CAPTURE_WINDOW_START_HOUR_UTC:
            return 0  # too early in the UTC day — 1D bar not settled enough
        # The caller derives regime from the in-memory regime cache (5-min TTL),
        # which is reliably cold in this late-UTC capture window (lowest traffic)
        # → it passes 'Unknown'. Fall back to the persistent regime_history table
        # (written by the precompute loop each cycle) before giving up.
        if not regime or regime == 'Unknown':
            regime = _latest_regime() or regime or 'Unknown'
        captured_date = now.date().isoformat()
        results = (scanner_data or {}).get('results', []) or []
        sql = adapt_query(
            'INSERT INTO scanner_history '
            '(captured_date, symbol, direction, regime, total, verdict_v1, '
            ' criteria, vetoes, price, bar_settled, setup_type, setup_confidence) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT (symbol, captured_date, direction) DO NOTHING'
        )
        attempted = 0
        with get_conn() as conn:
            _ensure_sqlite_table(conn)
            for a in results:
                symbol = a.get('symbol')
                if not symbol:
                    continue
                for direction in ('long', 'short'):
                    d = a.get(direction) or {}
                    if d.get('total') is None or d.get('verdict') is None:
                        continue  # skip assets the scorer couldn't score
                    vetoes = d.get('vetoes') or []
                    setup = d.get('setup') or {}
                    conn.execute(sql, (
                        captured_date, symbol, direction, regime,
                        int(d['total']), d['verdict'],
                        json.dumps(d.get('criteria') or {}),
                        json.dumps(vetoes) if vetoes else None,
                        a.get('price'),
                        True,  # captured inside the late-UTC window → bar ~settled
                        setup.get('type'),
                        setup.get('confidence'),
                    ))
                    attempted += 1
        logger.info("scanner_history: %d rows for %s (regime=%s)", attempted, captured_date, regime)
        return attempted
    except Exception:
        logger.exception("scanner_history upsert failed (non-fatal)")
        return 0


