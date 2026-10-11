"""scanner_history capture tests — shape, idempotency, regime default, gate.

Runs on SQLite (conftest forces DB_TYPE=sqlite). The PG migration doesn't run here,
so the fixture creates the table directly via get_conn (mirroring the migration).
"""
import json
import pytest

import carrac.db_pool as _cdb
from carrac.scanner.scorer import history as H

_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS scanner_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    captured_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    regime TEXT NOT NULL,
    total INTEGER NOT NULL,
    verdict_v1 TEXT NOT NULL,
    criteria TEXT NOT NULL,
    vetoes TEXT,
    price REAL,
    bar_settled INTEGER NOT NULL DEFAULT 0,
    setup_type TEXT,
    setup_confidence REAL,
    UNIQUE (symbol, captured_date, direction)
)
"""


@pytest.fixture
def sh(tmp_path, monkeypatch):
    """Fresh SQLite scanner_history table + gate open (capture always fires)."""
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    _cdb._db_type = None
    _cdb._sqlite_local.conn = None  # force a fresh thread-local conn at tmp DATA_DIR
    with _cdb.get_conn() as conn:
        conn.execute('DROP TABLE IF EXISTS scanner_history')
        conn.execute(_SQLITE_DDL)
    monkeypatch.setattr(H, 'CAPTURE_WINDOW_START_HOUR_UTC', 0)  # 0 → never gated out
    yield
    _cdb._sqlite_local.conn = None


def _history():
    """All scanner_history rows, newest first."""
    with _cdb.get_conn() as conn:
        rows = conn.execute(
            'SELECT * FROM scanner_history ORDER BY captured_at DESC').fetchall()
    return [dict(r) for r in rows]


def _scanner_data():
    return {"results": [
        {"symbol": "BTC", "price": 77000.0,
         "long":  {"total": 8, "verdict": "GOOD",
                   "criteria": {"trend": {"score": 2, "evidence": "x"}}, "vetoes": []},
         "short": {"total": 3, "verdict": "WEAK",
                   "criteria": {"trend": {"score": 0}}, "vetoes": ["all_tf_opposed"]}},
        {"symbol": "ETH", "price": 2100.0,
         "long":  {"total": 6, "verdict": "NEUTRAL", "criteria": {"rsi": {"score": 1}}, "vetoes": []},
         "short": {"total": 5, "verdict": "WEAK", "criteria": {}, "vetoes": []}},
    ]}


def test_row_shape_and_mapping(sh):
    n = H.upsert_daily_snapshot(_scanner_data(), regime="Risk-Off")
    assert n == 4  # 2 assets × 2 directions
    rows = {(r["symbol"], r["direction"]): r for r in _history()}
    btc_long = rows[("BTC", "long")]
    assert btc_long["total"] == 8
    assert btc_long["verdict_v1"] == "GOOD"
    assert btc_long["regime"] == "Risk-Off"
    assert btc_long["bar_settled"] in (1, True)
    assert json.loads(btc_long["criteria"])["trend"]["score"] == 2
    assert btc_long["vetoes"] is None            # empty vetoes → NULL
    assert json.loads(rows[("BTC", "short")]["vetoes"]) == ["all_tf_opposed"]


def test_idempotent_same_day(sh):
    H.upsert_daily_snapshot(_scanner_data(), regime="Neutral")
    H.upsert_daily_snapshot(_scanner_data(), regime="Neutral")  # second call = no-op
    assert len(_history()) == 4


def test_regime_defaults_to_unknown(sh):
    H.upsert_daily_snapshot(_scanner_data(), regime=None)
    assert all(r["regime"] == "Unknown" for r in _history())


def test_gate_blocks_outside_window(sh, monkeypatch):
    monkeypatch.setattr(H, 'CAPTURE_WINDOW_START_HOUR_UTC', 25)  # hour < 25 always → gated out
    n = H.upsert_daily_snapshot(_scanner_data(), regime="Neutral")
    assert n == 0
    assert _history() == []


def test_skips_unscored_assets(sh):
    data = {"results": [
        {"symbol": "DOGE", "price": 0.1, "long": {}, "short": {}},          # unscored → skip
        {"symbol": "SOL", "price": 85.0,
         "long": {"total": 7, "verdict": "GOOD", "criteria": {}, "vetoes": []},
         "short": {"total": 4, "verdict": "WEAK", "criteria": {}, "vetoes": []}},
    ]}
    n = H.upsert_daily_snapshot(data, regime="Neutral")
    assert n == 2  # only SOL long+short
    assert {r["symbol"] for r in _history()} == {"SOL"}


# --- regime fallback to regime_history when the in-memory cache is cold ---

_REGIME_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS regime_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    regime TEXT NOT NULL,
    score REAL,
    components TEXT
)
"""


def _seed_regime_history(*rows):
    """rows: (timestamp_iso, regime) tuples; latest timestamp = current regime."""
    with _cdb.get_conn() as conn:
        conn.execute('DROP TABLE IF EXISTS regime_history')
        conn.execute(_REGIME_HISTORY_DDL)
        for ts, reg in rows:
            conn.execute(_cdb.adapt_query(
                'INSERT INTO regime_history (timestamp, regime) VALUES (?, ?)'), (ts, reg))


def test_regime_falls_back_to_history_when_unknown(sh):
    # cold cache → caller passes 'Unknown'; the latest regime_history row wins
    _seed_regime_history(
        ("2026-05-27T22:09:24", "Risk-On"),
        ("2026-05-27T23:09:24", "Neutral"),   # latest by ISO timestamp
    )
    H.upsert_daily_snapshot(_scanner_data(), regime="Unknown")
    assert all(r["regime"] == "Neutral" for r in _history())


def test_regime_falls_back_to_history_when_none(sh):
    _seed_regime_history(("2026-05-27T23:09:24", "Risk-Off"))
    H.upsert_daily_snapshot(_scanner_data(), regime=None)
    assert all(r["regime"] == "Risk-Off" for r in _history())


def test_warm_cache_regime_wins_over_history(sh):
    # a real (non-Unknown) passed regime must NOT be overridden by history
    _seed_regime_history(("2026-05-27T23:09:24", "Neutral"))
    H.upsert_daily_snapshot(_scanner_data(), regime="Risk-On")
    assert all(r["regime"] == "Risk-On" for r in _history())


def test_regime_unknown_when_history_empty(sh):
    # regime_history present but empty → still defaults to 'Unknown' (no crash)
    _seed_regime_history()
    H.upsert_daily_snapshot(_scanner_data(), regime=None)
    assert all(r["regime"] == "Unknown" for r in _history())


# ── SQLite has no migration path, so the writer must create the table ──

@pytest.fixture
def fresh_sqlite_no_table(tmp_path, monkeypatch):
    """A fresh SQLite DB with NO scanner_history table and nothing else
    creating it — the shape of every dev/OSS install, where alembic is skipped."""
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    _cdb._db_type = None
    _cdb._sqlite_local.conn = None
    with _cdb.get_conn() as conn:
        conn.execute('DROP TABLE IF EXISTS scanner_history')
    monkeypatch.setattr(H, 'CAPTURE_WINDOW_START_HOUR_UTC', 0)
    yield
    _cdb._sqlite_local.conn = None


def test_sqlite_writer_creates_its_own_table(fresh_sqlite_no_table):
    n = H.upsert_daily_snapshot(_scanner_data(), regime="Neutral")
    assert n == 4, 'writer returned 0 — the insert died on a missing table'
    assert len(_history()) == 4


