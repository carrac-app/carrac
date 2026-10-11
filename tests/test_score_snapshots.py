"""
Tests for carrac.content_db.insert_score_snapshot /
get_score_snapshots.

These tests build a row using the EXACT key set that
carrac/scanner/snapshot.py::_do_capture assembles, insert it, and verify
every field round-trips non-null.
"""

import json

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh ContentDatabase backed by SQLite at a temp path."""
    import carrac.db_pool as _cdb
    _cdb._db_type = None
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    from carrac.content_db import ContentDatabase
    return ContentDatabase(db_path=str(tmp_path / 'test_score_snapshots.db'))


def _full_snapshot_row(journal_id=1, user_id=42):
    """A row populating every field carrac/scanner/snapshot.py::_do_capture builds."""
    return {
        'journal_id': journal_id,
        'user_id': user_id,
        'snapshot_type': 'entry',
        'asset': 'BTC',
        'direction': 'long',

        'score_trend': 2,
        'score_momentum': 2,
        'score_rsi': 1,
        'score_structure': 2,
        'score_level': 1,
        'score_macro': 1,
        'score_total': 9,
        'verdict': 'strong',
        'vetoes': json.dumps(['funding_extreme']),

        'price': 65000.5,

        'consensio_1d_signal': 1,
        'consensio_1d_positions': 3,
        'consensio_3d_signal': 1,
        'consensio_3d_positions': 2,
        'consensio_1w_signal': -1,
        'consensio_1w_positions': 1,
        'consensio_total_positions': 6,

        'ema_ribbon_trend': 'bullish',
        'ema_ribbon_gap_pct': 1.23,
        'macd_histogram': 0.45,
        'macd_momentum': 'rising',

        'weekly_rsi': 61.2,
        'rsi_div_daily': 'bullish',
        'rsi_div_weekly': 'none',

        'structure_trend': 'up',
        'last_high_type': 'HH',
        'last_low_type': 'HL',

        'cpr_pivot': 64000.0,
        'cpr_tc': 64500.0,
        'cpr_bc': 63500.0,
        'bb_upper': 66000.0,
        'bb_lower': 62000.0,
        'bb_middle': 64000.0,

        'adx_value': 28.5,
        'volume_ratio': 1.4,

        'regime': 'risk_on',
        'regime_score': 3,
        'fear_greed': 55,
        'breadth_pct': 62.5,

        'funding_rate': 0.0012,

        'entry_price': 65000.0,
        'stop_price': 63000.0,
        'tp1_price': 68000.0,

        'setup_type': 'breakout',
        'setup_readiness': 'ready',
        'setup_confidence': 0.82,
    }


def test_insert_score_snapshot_round_trips_every_field(db):
    """Every field the real caller builds must survive insert + read-back non-null.

    This must fail today on setup_type / setup_readiness / setup_confidence,
    which are silently dropped by insert_score_snapshot's column list.
    """
    row = _full_snapshot_row(journal_id=1)
    db.insert_score_snapshot(row)

    snapshots = db.get_score_snapshots(1)
    assert len(snapshots) == 1
    stored = snapshots[0]

    missing = [k for k, v in row.items() if v is not None and stored.get(k) is None]
    assert not missing, f"fields silently dropped on round-trip: {missing}"

    for key, expected in row.items():
        assert stored[key] == expected, f"{key}: expected {expected!r}, got {stored[key]!r}"


def test_insert_score_snapshot_cols_are_real_table_columns(db, monkeypatch):
    """Structural guard: the INSERT column list must be a subset of the actual
    score_snapshots table columns.

    Cheap guard against insert_score_snapshot's hardcoded column list
    drifting from the real table, which would make every INSERT fail.
    """
    import carrac.content_db as content_db_mod

    conn = db._get_conn()
    actual_cols = {row[1] for row in conn.execute("PRAGMA table_info(score_snapshots)").fetchall()}

    captured = {}
    real_adapt_query = content_db_mod.adapt_query

    def spy_adapt_query(sql):
        if 'INSERT INTO score_snapshots' in sql:
            inside = sql.split('(', 1)[1].split(')', 1)[0]
            captured['cols'] = [c.strip() for c in inside.split(',')]
        return real_adapt_query(sql)

    monkeypatch.setattr(content_db_mod, 'adapt_query', spy_adapt_query)

    db.insert_score_snapshot(_full_snapshot_row(journal_id=2))

    assert captured.get('cols'), "INSERT INTO score_snapshots statement was not captured"
    missing_from_table = set(captured['cols']) - actual_cols
    assert not missing_from_table, (
        f"insert_score_snapshot references columns not in score_snapshots: {missing_from_table}"
    )
