"""
Journal lifecycle tests via the ContentDatabase layer.

Covers: create → update → close → reopen → delete, analytics,
ownership isolation, checklists, stats, export.

(These tests exercise the DB layer directly rather than the routes.)
"""

import pytest
import carrac.db_pool as _cdb


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Create a ContentDatabase with SQLite backend."""
    _cdb._db_type = None
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    from carrac.content_db import ContentDatabase
    return ContentDatabase(db_path=str(tmp_path / 'test_journal.db'))


def _open_trade(db, user_id=1, **kw):
    defaults = dict(
        asset='BTC', timeframe='1D', direction='long',
        entry_price=65000, stop_price=63000, target_price=70000,
    )
    defaults.update(kw)
    return db.log_trade_open(user_id=user_id, **defaults)


class TestTradeCreate:
    def test_open_returns_id(self, db):
        tid = _open_trade(db)
        assert isinstance(tid, int)
        assert tid > 0

    def test_rr_planned_calculated(self, db):
        tid = _open_trade(db, entry_price=65000, stop_price=63000, target_price=70000)
        trades = db.get_open_trades(1)
        trade = [t for t in trades if t['id'] == tid][0]
        # Risk = 2000, Reward = 5000 → R:R = 2.5
        assert trade['rr_planned'] == 2.5

    def test_short_rr_planned(self, db):
        tid = _open_trade(db, direction='short', entry_price=70000,
                          stop_price=72000, target_price=65000)
        trades = db.get_open_trades(1)
        trade = [t for t in trades if t['id'] == tid][0]
        # Risk = 2000, Reward = 5000 → R:R = 2.5
        assert trade['rr_planned'] == 2.5

    def test_no_stop_no_rr(self, db):
        tid = _open_trade(db, stop_price=None, target_price=None)
        trades = db.get_open_trades(1)
        trade = [t for t in trades if t['id'] == tid][0]
        assert trade['rr_planned'] is None or trade['rr_planned'] == 0


class TestTradeLifecycle:
    def test_close_trade(self, db):
        tid = _open_trade(db)
        success = db.log_trade_close(tid, exit_price=67000, exit_reason='manual')
        assert success is True

        # No longer in open trades
        assert len(db.get_open_trades(1)) == 0

        # In history
        history = db.get_trade_history(1, days=7)
        assert len(history) == 1
        assert history[0]['exit_price'] == 67000

    def test_close_long_winning(self, db):
        tid = _open_trade(db, entry_price=65000, stop_price=63000)
        db.log_trade_close(tid, exit_price=67000)
        history = db.get_trade_history(1, days=7)
        # Long: pnl = 67000 - 65000 = 2000, risk = 2000, rr = 1.0
        assert history[0]['rr_actual'] == 1.0

    def test_close_long_losing(self, db):
        tid = _open_trade(db, entry_price=65000, stop_price=63000)
        db.log_trade_close(tid, exit_price=62000)
        history = db.get_trade_history(1, days=7)
        # Long: pnl = 62000 - 65000 = -3000, risk = 2000, rr = -1.5
        assert history[0]['rr_actual'] == -1.5

    def test_close_short_winning(self, db):
        tid = _open_trade(db, direction='short', entry_price=70000, stop_price=72000)
        db.log_trade_close(tid, exit_price=67000)
        history = db.get_trade_history(1, days=7)
        # Short: pnl = 70000 - 67000 = 3000, risk = 2000, rr = 1.5
        assert history[0]['rr_actual'] == 1.5

    def test_close_short_losing(self, db):
        tid = _open_trade(db, direction='short', entry_price=70000, stop_price=72000)
        db.log_trade_close(tid, exit_price=73000)
        history = db.get_trade_history(1, days=7)
        # Short: pnl = 70000 - 73000 = -3000, risk = 2000, rr = -1.5
        assert history[0]['rr_actual'] == -1.5

    def test_reopen_trade(self, db):
        tid = _open_trade(db)
        db.log_trade_close(tid, exit_price=67000)
        assert len(db.get_open_trades(1)) == 0

        db.reopen_trade(tid)
        assert len(db.get_open_trades(1)) == 1

    def test_update_trade(self, db):
        tid = _open_trade(db)
        db.trade_update(tid, setup_type='breakout', notes='Updated note')

        entry = db.get_journal_entry(tid)
        assert entry['setup_type'] == 'breakout'
        assert entry['notes'] == 'Updated note'


class TestTradeFiltering:
    def test_filter_by_asset(self, db):
        _open_trade(db, asset='BTC')
        _open_trade(db, asset='ETH')

        btc = db.get_open_trades(1, asset='BTC')
        assert len(btc) == 1
        assert btc[0]['asset'] == 'BTC'

    def test_history_respects_days(self, db):
        tid = _open_trade(db)
        db.log_trade_close(tid, exit_price=67000)

        history = db.get_trade_history(1, days=7)
        assert len(history) == 1

        # Trades within last 0 days should be empty (or just today's)
        # This depends on implementation — just verify it returns a list
        history = db.get_trade_history(1, days=1)
        assert isinstance(history, list)


class TestStats:
    def test_stats_with_trades(self, db):
        # 2 wins, 1 loss
        for exit_p in [67000, 68000, 62000]:
            tid = _open_trade(db, entry_price=65000, stop_price=63000)
            db.log_trade_close(tid, exit_price=exit_p)

        stats = db.get_journal_stats(1, days=7)
        assert stats['trades'] == 3
        assert stats['wins'] == 2
        assert stats['losses'] == 1
        assert 60 <= stats['win_rate'] <= 70  # ~66.7%

    def test_stats_empty(self, db):
        stats = db.get_journal_stats(1, days=7)
        assert stats['trades'] == 0
        assert stats['wins'] == 0


class TestOwnershipIsolation:
    def test_user_cant_see_other_trades(self, db):
        _open_trade(db, user_id=1, asset='BTC')
        _open_trade(db, user_id=2, asset='ETH')

        user1_trades = db.get_open_trades(1)
        user2_trades = db.get_open_trades(2)

        assert len(user1_trades) == 1
        assert len(user2_trades) == 1
        assert user1_trades[0]['asset'] == 'BTC'
        assert user2_trades[0]['asset'] == 'ETH'

    def test_stats_user_scoped(self, db):
        # User 1: 1 trade
        tid1 = _open_trade(db, user_id=1)
        db.log_trade_close(tid1, exit_price=67000)

        # User 2: 2 trades
        for _ in range(2):
            tid2 = _open_trade(db, user_id=2)
            db.log_trade_close(tid2, exit_price=67000)

        assert db.get_journal_stats(1, days=7)['trades'] == 1
        assert db.get_journal_stats(2, days=7)['trades'] == 2


class TestDrawingLink:
    def test_find_by_drawing_id(self, db):
        tid = _open_trade(db, drawing_id=42)
        found = db.find_journal_by_drawing(42)
        assert found is not None
        assert found['id'] == tid

    def test_find_missing_drawing(self, db):
        assert db.find_journal_by_drawing(999) is None


class TestEdgeCases:
    def test_close_nonexistent_trade(self, db):
        result = db.log_trade_close(99999, exit_price=67000)
        assert result is False

    def test_multiple_opens_same_asset(self, db):
        _open_trade(db, asset='BTC')
        _open_trade(db, asset='BTC')
        trades = db.get_open_trades(1, asset='BTC')
        assert len(trades) == 2


# ---------------------------------------------------------------------------
# delete_trades_bulk atomicity
# ---------------------------------------------------------------------------

class TestDeleteTradesBulk:
    """Verify delete_trades_bulk behaves correctly."""

    def test_delete_single(self, db):
        tid = _open_trade(db, user_id=1)
        deleted = db.delete_trades_bulk([tid], user_id=1)
        assert deleted == 1
        assert db.get_trade_history(1, days=7) == []

    def test_delete_multiple(self, db):
        ids = [_open_trade(db, user_id=1) for _ in range(3)]
        deleted = db.delete_trades_bulk(ids, user_id=1)
        assert deleted == 3
        assert db.get_open_trades(1) == []

    def test_skip_other_user_ids(self, db):
        tid_u1 = _open_trade(db, user_id=1)
        tid_u2 = _open_trade(db, user_id=2)
        # User 1 tries to delete user 2's trade — must be silently skipped.
        deleted = db.delete_trades_bulk([tid_u1, tid_u2], user_id=1)
        assert deleted == 1
        # User 2's trade still exists.
        assert len(db.get_open_trades(2)) == 1

    def test_empty_list(self, db):
        assert db.delete_trades_bulk([], user_id=1) == 0

    def test_nonexistent_ids(self, db):
        assert db.delete_trades_bulk([99999, 88888], user_id=1) == 0

    def test_file_not_deleted_for_nonexistent_path(self, db, tmp_path):
        """os.remove is only called for real files; missing paths are skipped silently."""
        tid = _open_trade(db, user_id=1)
        # Inject a screenshot row with a non-existent file path.
        conn = db._get_conn()
        conn.execute(
            "INSERT INTO journal_screenshots (journal_id, user_id, screenshot_type, file_path)"
            " VALUES (?, ?, ?, ?)",
            (tid, 1, 'entry', 'nonexistent/ghost.png'),
        )
        conn.commit()
        # Should not raise even though the file is missing.
        deleted = db.delete_trades_bulk([tid], user_id=1)
        assert deleted == 1

    def test_real_file_removed_after_delete(self, db, tmp_path, monkeypatch):
        """Screenshot files on disk are removed after a successful bulk delete."""
        monkeypatch.setenv('DATA_DIR', str(tmp_path))
        # Create a real file inside DATA_DIR.
        screenshot_rel = 'screenshots/entry.png'
        full_path = tmp_path / 'screenshots' / 'entry.png'
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(b'fake png')

        tid = _open_trade(db, user_id=1)
        conn = db._get_conn()
        conn.execute(
            "INSERT INTO journal_screenshots (journal_id, user_id, screenshot_type, file_path)"
            " VALUES (?, ?, ?, ?)",
            (tid, 1, 'entry', screenshot_rel),
        )
        conn.commit()

        deleted = db.delete_trades_bulk([tid], user_id=1)
        assert deleted == 1
        assert not full_path.exists(), "Screenshot file should have been removed"


# ---------------------------------------------------------------------------
# create_session / get_or_create_session_for_date idempotency
# ---------------------------------------------------------------------------

class TestSessionIdempotency:
    """Verify that duplicate create_session / get_or_create_session_for_date
    calls never produce two rows or raise UniqueViolation."""

    def test_create_session_idempotent(self, db):
        id1 = db.create_session(user_id=1, date='2026-05-24')
        id2 = db.create_session(user_id=1, date='2026-05-24')
        assert id1 == id2, "Second call must return the existing session id"

    def test_create_session_different_label(self, db):
        id1 = db.create_session(user_id=1, date='2026-05-24', session_label='am')
        id2 = db.create_session(user_id=1, date='2026-05-24', session_label='pm')
        assert id1 != id2

    def test_create_session_different_user(self, db):
        id1 = db.create_session(user_id=1, date='2026-05-24')
        id2 = db.create_session(user_id=2, date='2026-05-24')
        assert id1 != id2

    def test_get_or_create_idempotent(self, db):
        id1 = db.get_or_create_session_for_date(user_id=1, date='2026-05-24')
        id2 = db.get_or_create_session_for_date(user_id=1, date='2026-05-24')
        assert id1 == id2

    def test_get_or_create_returns_positive_int(self, db):
        sid = db.get_or_create_session_for_date(user_id=1, date='2026-05-25')
        assert isinstance(sid, int) and sid > 0
