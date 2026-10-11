"""Journal auto-sync from exchanges — tests.

Covers: FIFO position builder (partial fills, flips, fees), cursor/fill
idempotency, dedupe-vs-manual-entry suggestions, Binance permission
rejection, credential encryption round-trip, the closed-default tier knob,
and per-connection error isolation in the sync loop.

No live exchange API calls anywhere — every network boundary
(BinanceAuthClient._request / HyperliquidFillsClient._request, or the
higher-level get_spot_fills/get_fills) is monkeypatched/mocked.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import carrac.db_pool as _db_pool


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch):
    """A Fernet key so encrypt_token doesn't fall back to plaintext for
    every test in this file (encryption behaviour itself is asserted in
    TestEncryption below)."""
    from cryptography.fernet import Fernet
    import carrac.secret_box as tc
    monkeypatch.setenv('TELEGRAM_TOKEN_ENCRYPTION_KEY', Fernet.generate_key().decode())
    tc._fernet_instance = None
    tc._warned_missing_key = False
    yield
    tc._fernet_instance = None
    tc._warned_missing_key = False


@pytest.fixture
def exchange_env(tmp_path, monkeypatch):
    """Isolated SQLite DATA_DIR for carrac.db_pool.get_conn() (exchange_* tables)."""
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('DB_TYPE', 'sqlite')
    _db_pool._db_type = None
    sl = getattr(_db_pool, '_sqlite_local', None)
    if sl is not None and getattr(sl, 'conn', None) is not None:
        try:
            sl.conn.close()
        except Exception:
            pass
        sl.conn = None
    yield tmp_path


@pytest.fixture
def db(tmp_path, exchange_env):
    """The global ContentDatabase singleton, rebound to an isolated path.

    ``resolve_suggestion`` (and other sync-engine call sites that run from
    route/worker context) call ``get_content_db()`` directly with no
    injectable override, so tests must share THIS instance rather than
    constructing a private ``ContentDatabase`` the rest of the code can't see.
    """
    import carrac.content_db as _cdb
    _cdb._db_instance = None
    instance = _cdb.get_content_db(str(tmp_path / 'test_drawings.db'))
    yield instance
    _cdb._db_instance = None


# ============================================================
# FIFO builder — pure logic, no DB/network involved
# ============================================================

class TestFifoConsume:
    def test_partial_fills_aggregate_weighted_entry(self):
        from carrac.journal.exchange_sync import fifo_consume

        fills = [
            {'id': '1', 'symbol': 'BTCUSDT', 'side': 'buy', 'price': 100, 'qty': 1.0,
             'fee': 0.1, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T00:00:00')},
            {'id': '2', 'symbol': 'BTCUSDT', 'side': 'buy', 'price': 110, 'qty': 0.5,
             'fee': 0.05, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T01:00:00')},
            {'id': '3', 'symbol': 'BTCUSDT', 'side': 'sell', 'price': 120, 'qty': 1.5,
             'fee': 0.2, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T02:00:00')},
        ]
        closed, open_state = fifo_consume(fills)

        assert open_state is None
        assert len(closed) == 1
        pos = closed[0]
        assert pos['side'] == 'long'
        assert pos['qty'] == pytest.approx(1.5)
        assert pos['entry_avg'] == pytest.approx((100 * 1.0 + 110 * 0.5) / 1.5)
        assert pos['exit_avg'] == pytest.approx(120.0)
        assert pos['fees'] == pytest.approx(0.1 + 0.05 + 0.2)

    def test_partial_close_across_multiple_fills(self):
        """Two separate closing fills before the lot fully flattens must
        average into ONE closed position, not two."""
        from carrac.journal.exchange_sync import fifo_consume

        fills = [
            {'id': '1', 'symbol': 'ETHUSDT', 'side': 'buy', 'price': 2000, 'qty': 2.0,
             'fee': 0, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T00:00:00')},
            {'id': '2', 'symbol': 'ETHUSDT', 'side': 'sell', 'price': 2100, 'qty': 1.0,
             'fee': 0.1, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T01:00:00')},
            {'id': '3', 'symbol': 'ETHUSDT', 'side': 'sell', 'price': 2200, 'qty': 1.0,
             'fee': 0.1, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T02:00:00')},
        ]
        closed, open_state = fifo_consume(fills)

        assert open_state is None
        assert len(closed) == 1
        pos = closed[0]
        assert pos['qty'] == pytest.approx(2.0)
        assert pos['exit_avg'] == pytest.approx((2100 * 1.0 + 2200 * 1.0) / 2.0)
        assert pos['fees'] == pytest.approx(0.2)

    def test_flip_long_to_short_in_one_fill(self):
        from carrac.journal.exchange_sync import fifo_consume

        fills = [
            {'id': 'a', 'symbol': 'ETHUSDT', 'side': 'buy', 'price': 100, 'qty': 1.0,
             'fee': 0, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T00:00:00')},
            {'id': 'b', 'symbol': 'ETHUSDT', 'side': 'sell', 'price': 110, 'qty': 1.5,
             'fee': 0.15, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T01:00:00')},
        ]
        closed, open_state = fifo_consume(fills)

        assert len(closed) == 1
        assert closed[0]['side'] == 'long'
        assert closed[0]['qty'] == pytest.approx(1.0)
        assert closed[0]['exit_avg'] == pytest.approx(110.0)
        # Fee on the flipping fill splits proportionally: 1/1.5 closes, 0.5/1.5 opens.
        assert closed[0]['fees'] == pytest.approx(0.15 * (1.0 / 1.5))

        assert open_state is not None
        assert open_state['side'] == 'short'
        assert open_state['qty'] == pytest.approx(0.5)
        assert open_state['avg_price'] == pytest.approx(110.0)
        assert open_state['fees'] == pytest.approx(0.15 * (0.5 / 1.5))

    def test_flip_short_to_long(self):
        from carrac.journal.exchange_sync import fifo_consume

        fills = [
            {'id': 'a', 'symbol': 'SOLUSDT', 'side': 'sell', 'price': 50, 'qty': 2.0,
             'fee': 0, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T00:00:00')},
            {'id': 'b', 'symbol': 'SOLUSDT', 'side': 'buy', 'price': 40, 'qty': 3.0,
             'fee': 0.3, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T01:00:00')},
        ]
        closed, open_state = fifo_consume(fills)

        assert len(closed) == 1
        assert closed[0]['side'] == 'short'
        assert closed[0]['entry_avg'] == pytest.approx(50.0)
        assert closed[0]['exit_avg'] == pytest.approx(40.0)
        assert open_state['side'] == 'long'
        assert open_state['qty'] == pytest.approx(1.0)

    def test_resume_across_sync_cycles(self):
        """A lot opened in a PREVIOUS sync closes correctly when fed only the
        NEW fill plus the persisted resume state."""
        from carrac.journal.exchange_sync import fifo_consume

        resume = {'side': 'long', 'qty': 1.0, 'avg_price': 100.0, 'fees': 0.0,
                  'opened_at_ms': _ms('2026-01-01T00:00:00')}
        new_fills = [
            {'id': 'c', 'symbol': 'SOLUSDT', 'side': 'sell', 'price': 130, 'qty': 1.0,
             'fee': 0.05, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-02T00:00:00')},
        ]
        closed, open_state = fifo_consume(new_fills, resume=resume)

        assert open_state is None
        assert len(closed) == 1
        assert closed[0]['entry_avg'] == pytest.approx(100.0)
        assert closed[0]['exit_avg'] == pytest.approx(130.0)
        assert closed[0]['opened_at_ms'] == resume['opened_at_ms']

    def test_still_open_after_partial_reduce(self):
        from carrac.journal.exchange_sync import fifo_consume

        fills = [
            {'id': '1', 'symbol': 'BTCUSDT', 'side': 'buy', 'price': 100, 'qty': 2.0,
             'fee': 0, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T00:00:00')},
            {'id': '2', 'symbol': 'BTCUSDT', 'side': 'sell', 'price': 110, 'qty': 0.5,
             'fee': 0.05, 'fee_asset': 'USDT', 'ts_ms': _ms('2026-01-01T01:00:00')},
        ]
        closed, open_state = fifo_consume(fills)
        assert closed == []
        assert open_state['side'] == 'long'
        assert open_state['qty'] == pytest.approx(1.5)
        # Entry avg unaffected by a partial close (only exit_avg buffers, no
        # realization emitted until fully flat).
        assert open_state['avg_price'] == pytest.approx(100.0)


# ============================================================
# Encryption round-trip
# ============================================================

class TestEncryption:
    def test_credentials_encrypted_at_rest_and_decrypt_correctly(self, exchange_env):
        from carrac.journal import exchange_connections as ec

        conn = ec.create_connection(
            1, 'binance', {'api_key': 'ABCDEFGHIJKL1234', 'api_secret': 'topsecret'}, label='main',
        )
        assert conn['credentials_encrypted'].startswith('fernet:')
        assert 'topsecret' not in conn['credentials_encrypted']

        creds = ec.get_decrypted_credentials(conn)
        assert creds == {'api_key': 'ABCDEFGHIJKL1234', 'api_secret': 'topsecret'}

    def test_public_dict_never_leaks_secrets(self, exchange_env):
        from carrac.journal import exchange_connections as ec

        conn = ec.create_connection(2, 'hyperliquid', {'address': '0x' + 'a' * 40})
        pub = ec.to_public_dict(conn)
        assert 'credentials_encrypted' not in pub
        assert 'cursor_data' not in pub
        # masked_identifier for HL is the (public, non-secret) address itself
        assert pub['masked_identifier'] == '0x' + 'a' * 40

    def test_binance_masked_identifier_hides_most_of_the_key(self, exchange_env):
        from carrac.journal import exchange_connections as ec

        conn = ec.create_connection(3, 'binance', {'api_key': 'ABCDEFGHIJKL1234', 'api_secret': 'x'})
        assert conn['masked_identifier'].endswith('1234')
        assert 'ABCDEFGH' not in conn['masked_identifier']


# ============================================================
# Connection CRUD
# ============================================================

class TestExchangeConnections:
    def test_reconnect_replaces_and_resets_cursor(self, exchange_env):
        from carrac.journal import exchange_connections as ec

        c1 = ec.create_connection(1, 'binance', {'api_key': 'AAAA', 'api_secret': 's1'})
        ec.update_cursor(c1['id'], {'BTCUSDT': {'open_trade_id': 5}})
        ec.mark_sync_error(c1['id'], 'boom')

        c2 = ec.create_connection(1, 'binance', {'api_key': 'BBBB', 'api_secret': 's2'})
        assert c2['id'] == c1['id']  # same row, replaced
        assert c2['status'] == 'active'
        assert c2['consecutive_errors'] == 0
        assert json.loads(c2['cursor_data']) == {}

    def test_rejects_unsupported_exchange(self, exchange_env):
        from carrac.journal import exchange_connections as ec
        with pytest.raises(ValueError):
            ec.create_connection(1, 'kraken', {'api_key': 'x', 'api_secret': 'y'})

    def test_hard_delete_purges_child_rows(self, exchange_env):
        from carrac.journal import exchange_connections as ec
        from carrac.journal import exchange_sync as es

        conn = ec.create_connection(9, 'hyperliquid', {'address': '0x' + 'c' * 40})
        es.create_suggestion(
            connection_id=conn['id'], user_id=9, exchange='hyperliquid', symbol='ETH',
            direction='long', entry_price=100, exit_price=110, size=1, fees=0.1,
            opened_at=datetime.now(timezone.utc), closed_at=datetime.now(timezone.utc),
            matched_trade_id=None,
        )
        assert len(es.list_suggestions(9, status=None)) == 1

        ok = ec.delete_connection(conn['id'], 9)
        assert ok is True
        assert ec.get_connection(conn['id']) is None
        assert es.list_suggestions(9, status=None) == []

    def test_delete_rejects_non_owner(self, exchange_env):
        from carrac.journal import exchange_connections as ec
        conn = ec.create_connection(1, 'binance', {'api_key': 'AAAA', 'api_secret': 's1'})
        assert ec.delete_connection(conn['id'], user_id=999) is False
        assert ec.get_connection(conn['id']) is not None

    def test_error_connection_excluded_from_syncable_list(self, exchange_env):
        from carrac.journal import exchange_connections as ec
        conn = ec.create_connection(1, 'binance', {'api_key': 'AAAA', 'api_secret': 's1'})
        for _ in range(ec.MAX_CONSECUTIVE_ERRORS):
            ec.mark_sync_error(conn['id'], 'boom')
        assert ec.get_connection(conn['id'])['status'] == 'error'
        assert ec.list_syncable_connections() == []

    def test_hl_address_validation(self):
        from carrac.journal.exchange_connections import is_valid_hl_address
        assert is_valid_hl_address('0x' + 'a' * 40) is True
        assert is_valid_hl_address('not-an-address') is False
        assert is_valid_hl_address('') is False


# ============================================================
# Sync engine: idempotency, materialization, dedupe
# ============================================================

class TestSyncConnection:
    def test_binance_open_then_close_reuses_same_trade_row(self, db, exchange_env):
        from carrac.journal import exchange_connections as ec
        from carrac.journal import exchange_sync as es

        user_id = 42
        conn = ec.create_connection(user_id, 'binance', {'api_key': 'AAAA1234', 'api_secret': 'shh'})

        open_fill = [{
            'id': '1001', 'symbol': 'BTCUSDT', 'price': '100.0', 'qty': '1.0',
            'commission': '0.1', 'commissionAsset': 'USDT',
            'time': _ms('2026-01-01T00:00:00'), 'isBuyer': True,
        }]

        with patch('user_db.user_db.get_flat_watchlist', return_value=['BTC']), \
             patch('carrac.journal.binance_auth_client.BinanceAuthClient.get_spot_fills',
                   return_value=open_fill):
            result1 = es.sync_connection(conn, db=db)

        assert result1 == {'imported': 1, 'suggested': 0}
        open_trades = db.get_open_trades(user_id)
        assert len(open_trades) == 1
        assert open_trades[0]['asset'] == 'BTC'
        assert open_trades[0]['direction'] == 'long'
        assert json.loads(open_trades[0]['tags']) == ['src:binance']
        trade_id = open_trades[0]['id']

        conn = ec.get_connection(conn['id'])
        close_fills = open_fill + [{
            'id': '1002', 'symbol': 'BTCUSDT', 'price': '120.0', 'qty': '1.0',
            'commission': '0.12', 'commissionAsset': 'USDT',
            'time': _ms('2026-01-01T02:00:00'), 'isBuyer': False,
        }]
        with patch('user_db.user_db.get_flat_watchlist', return_value=['BTC']), \
             patch('carrac.journal.binance_auth_client.BinanceAuthClient.get_spot_fills',
                   return_value=close_fills):
            result2 = es.sync_connection(conn, db=db)

        assert result2 == {'imported': 1, 'suggested': 0}
        assert db.get_open_trades(user_id) == []
        closed = db.get_trade_history(user_id, days=3650)
        assert len(closed) == 1
        assert closed[0]['id'] == trade_id  # SAME row, not a new one
        assert closed[0]['exit_price'] == pytest.approx(120.0)

    def test_cursor_idempotency_same_fills_twice_no_dupes(self, db, exchange_env):
        """Re-running sync with fills already recorded must not create a
        second trade or double-count anything."""
        from carrac.journal import exchange_connections as ec
        from carrac.journal import exchange_sync as es

        user_id = 43
        conn = ec.create_connection(user_id, 'binance', {'api_key': 'AAAA1234', 'api_secret': 'shh'})
        fills = [{
            'id': '2001', 'symbol': 'ETHUSDT', 'price': '2000.0', 'qty': '1.0',
            'commission': '0.1', 'commissionAsset': 'USDT',
            'time': _ms('2026-02-01T00:00:00'), 'isBuyer': True,
        }]

        with patch('user_db.user_db.get_flat_watchlist', return_value=['ETH']), \
             patch('carrac.journal.binance_auth_client.BinanceAuthClient.get_spot_fills',
                   return_value=fills):
            es.sync_connection(conn, db=db)
            # Same fill fetched again (overlapping refetch window) — no cursor
            # change from the caller's perspective, exercising the raw-fill
            # dedupe ledger directly rather than the cursor's start_time cut.
            conn = ec.get_connection(conn['id'])
            result_again = es.sync_connection(conn, db=db)

        assert result_again == {'imported': 0, 'suggested': 0}
        assert len(db.get_open_trades(user_id)) == 1  # still just one row

    def test_dedupe_vs_manual_entry_creates_suggestion_not_a_trade(self, db, exchange_env):
        from carrac.journal import exchange_connections as ec
        from carrac.journal import exchange_sync as es

        user_id = 77
        conn = ec.create_connection(user_id, 'hyperliquid', {'address': '0x' + 'b' * 40})

        manual_tid = db.log_trade_open(
            user_id=user_id, asset='ETH', timeframe='1D', direction='long', entry_price=2000,
        )
        raw_conn = db._get_conn()
        raw_conn.execute(
            'UPDATE trade_journal SET opened_at = ?, size = ? WHERE id = ?',
            (datetime.fromtimestamp(_ms('2026-01-05T00:05:00') / 1000, tz=timezone.utc).isoformat(),
             1.0, manual_tid),
        )
        raw_conn.commit()

        hl_fills = [
            {'coin': 'ETH', 'px': '2000', 'sz': '1.0', 'side': 'B',
             'time': _ms('2026-01-05T00:00:00'), 'tid': 5001, 'fee': '0.5', 'feeToken': 'USDC'},
            {'coin': 'ETH', 'px': '2100', 'sz': '1.0', 'side': 'A',
             'time': _ms('2026-01-05T03:00:00'), 'tid': 5002, 'fee': '0.5', 'feeToken': 'USDC'},
        ]
        with patch('carrac.journal.hyperliquid_fills_client.HyperliquidFillsClient.get_fills',
                   return_value=hl_fills):
            result = es.sync_connection(conn, db=db)

        assert result == {'imported': 0, 'suggested': 1}
        # No new journal trade beyond the original manual one.
        assert len(db.get_trade_history(user_id, days=3650)) == 0
        assert len(db.get_open_trades(user_id)) == 1  # just the manual entry

        suggestions = es.list_suggestions(user_id)
        assert len(suggestions) == 1
        assert suggestions[0]['matched_trade_id'] == manual_tid
        assert suggestions[0]['status'] == 'pending'

    def test_resolve_suggestion_import_creates_tagged_trade(self, db, exchange_env):
        from carrac.journal import exchange_connections as ec
        from carrac.journal import exchange_sync as es

        user_id = 78
        conn = ec.create_connection(user_id, 'hyperliquid', {'address': '0x' + 'd' * 40})
        es.create_suggestion(
            connection_id=conn['id'], user_id=user_id, exchange='hyperliquid', symbol='ETH',
            direction='long', entry_price=2000, exit_price=2100, size=1.0, fees=1.0,
            opened_at=datetime.now(timezone.utc), closed_at=datetime.now(timezone.utc),
            matched_trade_id=None,
        )
        sugg = es.list_suggestions(user_id)[0]

        resolved = es.resolve_suggestion(sugg['id'], user_id, 'import')
        assert resolved['status'] == 'imported'
        assert resolved['journal_trade_id'] is not None

        closed = db.get_trade_history(user_id, days=3650)
        assert len(closed) == 1
        assert json.loads(closed[0]['tags']) == ['src:hyperliquid']
        assert closed[0]['exit_price'] == pytest.approx(2100.0)

    def test_resolve_suggestion_skip_creates_no_trade(self, db, exchange_env):
        from carrac.journal import exchange_connections as ec
        from carrac.journal import exchange_sync as es

        user_id = 79
        conn = ec.create_connection(user_id, 'hyperliquid', {'address': '0x' + 'e' * 40})
        es.create_suggestion(
            connection_id=conn['id'], user_id=user_id, exchange='hyperliquid', symbol='ETH',
            direction='long', entry_price=2000, exit_price=2100, size=1.0, fees=1.0,
            opened_at=datetime.now(timezone.utc), closed_at=datetime.now(timezone.utc),
            matched_trade_id=None,
        )
        sugg = es.list_suggestions(user_id)[0]

        resolved = es.resolve_suggestion(sugg['id'], user_id, 'skip')
        assert resolved['status'] == 'skipped'
        assert db.get_trade_history(user_id, days=3650) == []


# ============================================================
# Binance permission rejection
# ============================================================

class TestBinancePermissionCheck:
    def test_rejects_key_with_withdrawal_permission(self):
        from carrac.journal.binance_auth_client import BinanceAuthClient, BinancePermissionError

        client = BinanceAuthClient('key', 'secret')
        with patch.object(BinanceAuthClient, '_request', return_value={
            'enableReading': True, 'enableWithdrawals': True, 'enableSpotAndMarginTrading': False,
            'enableMargin': False, 'enableFutures': False, 'permitsUniversalTransfer': False,
        }):
            with pytest.raises(BinancePermissionError, match='enableWithdrawals'):
                client.check_read_only()

    def test_rejects_key_with_trading_permission(self):
        from carrac.journal.binance_auth_client import BinanceAuthClient, BinancePermissionError

        client = BinanceAuthClient('key', 'secret')
        with patch.object(BinanceAuthClient, '_request', return_value={
            'enableReading': True, 'enableWithdrawals': False, 'enableSpotAndMarginTrading': True,
            'enableMargin': False, 'enableFutures': False, 'permitsUniversalTransfer': False,
        }):
            with pytest.raises(BinancePermissionError, match='enableSpotAndMarginTrading'):
                client.check_read_only()

    def test_accepts_read_only_key(self):
        from carrac.journal.binance_auth_client import BinanceAuthClient

        client = BinanceAuthClient('key', 'secret')
        with patch.object(BinanceAuthClient, '_request', return_value={
            'enableReading': True, 'enableWithdrawals': False, 'enableSpotAndMarginTrading': False,
            'enableMargin': False, 'enableFutures': False, 'permitsUniversalTransfer': False,
        }):
            data = client.check_read_only()
            assert data['enableReading'] is True


# ============================================================
# Tier knob (closed default) + worker-loop error isolation
# ============================================================

class TestKnobAndErrorIsolation:
    def test_knob_matrix_free_locked_paid_open(self):
        """Post-integration matrix: journal_autosync False for free, True
        for every paid tier (defined in TIER_LIMITS)."""
        from carrac.auth.decorators import get_tier_limits_for_user
        assert get_tier_limits_for_user(1, 'free').get('journal_autosync', False) is False
        for tier in ('member', 'tester', 'premium', 'admin'):
            assert get_tier_limits_for_user(1, tier).get('journal_autosync', False) is True

    def test_knob_removed_skips_every_connection(self, exchange_env):
        """The .get(key, False) closed default must survive the knob being
        deleted from TIER_LIMITS entirely — sync skips everything."""
        from carrac.journal import exchange_connections as ec
        from carrac.journal import exchange_sync as es
        from carrac.auth import decorators as dec

        ec.create_connection(1, 'binance', {'api_key': 'AAAA', 'api_secret': 's1'})
        ec.create_connection(2, 'hyperliquid', {'address': '0x' + 'f' * 40})

        stripped = {t: {k: v for k, v in lim.items() if k != 'journal_autosync'}
                    for t, lim in dec.TIER_LIMITS.items()}
        with patch.object(dec, 'TIER_LIMITS', stripped), \
             patch('user_db.user_db.get_user_by_id', return_value={'tier': 'admin'}):
            result = es.sync_all_connections()

        assert result['synced'] == 0
        assert result['skipped_tier'] == 2
        assert result['errored'] == 0

    def test_one_bad_connection_never_breaks_the_loop(self, db, exchange_env, monkeypatch):
        """Per-connection error isolation: a raising sync_connection for one
        connection must not stop the others, and increments its own
        consecutive-error counter."""
        from carrac.journal import exchange_connections as ec
        from carrac.journal import exchange_sync as es

        good = ec.create_connection(1, 'hyperliquid', {'address': '0x' + '1' * 40})
        bad = ec.create_connection(2, 'hyperliquid', {'address': '0x' + '2' * 40})

        # Force the knob open for this test (normally False — see the
        # closed-default test above) so we can exercise the isolation logic.
        monkeypatch.setattr(
            'carrac.auth.decorators.get_tier_limits_for_user',
            lambda uid, tier: {'journal_autosync': True},
        )

        calls = {'n': 0}

        def _fake_sync(conn_row, **kwargs):
            calls['n'] += 1
            if conn_row['id'] == bad['id']:
                raise RuntimeError('simulated exchange outage')
            return {'imported': 0, 'suggested': 0}

        with patch('user_db.user_db.get_user_by_id', return_value={'tier': 'member'}), \
             patch('carrac.journal.exchange_sync.sync_connection', side_effect=_fake_sync):
            result = es.sync_all_connections()

        assert calls['n'] == 2  # BOTH connections were attempted
        assert result['synced'] == 1
        assert result['errored'] == 1
        bad_row = ec.get_connection(bad['id'])
        assert bad_row['consecutive_errors'] == 1
        assert bad_row['status'] == 'active'  # not yet disabled (needs 5)

        good_row = ec.get_connection(good['id'])
        assert good_row['consecutive_errors'] == 0

    def test_connection_disabled_after_five_consecutive_failures(self, exchange_env, monkeypatch):
        from carrac.journal import exchange_connections as ec
        from carrac.journal import exchange_sync as es

        conn = ec.create_connection(1, 'hyperliquid', {'address': '0x' + '3' * 40})
        monkeypatch.setattr(
            'carrac.auth.decorators.get_tier_limits_for_user',
            lambda uid, tier: {'journal_autosync': True},
        )

        with patch('user_db.user_db.get_user_by_id', return_value={'tier': 'member'}), \
             patch('carrac.journal.exchange_sync.sync_connection',
                   side_effect=RuntimeError('boom')):
            for _ in range(ec.MAX_CONSECUTIVE_ERRORS):
                es.sync_all_connections()

        row = ec.get_connection(conn['id'])
        assert row['status'] == 'error'
        assert row['consecutive_errors'] == ec.MAX_CONSECUTIVE_ERRORS
        # Disabled connections drop out of future passes entirely.
        assert ec.list_syncable_connections() == []


# ── exchange credentials must never be stored in the clear ──

class TestEncryptionRequired:
    def _unset_key(self, monkeypatch):
        import carrac.secret_box as tc
        monkeypatch.delenv('TELEGRAM_TOKEN_ENCRYPTION_KEY', raising=False)
        monkeypatch.setattr(tc, '_fernet_instance', None)
        monkeypatch.setattr(tc, '_warned_missing_key', False)

    def test_create_connection_refuses_without_key(self, exchange_env, monkeypatch, caplog):
        import logging
        from carrac.secret_box import EncryptionUnavailable
        from carrac.journal import exchange_connections as ec
        self._unset_key(monkeypatch)
        with caplog.at_level(logging.ERROR):
            with pytest.raises(EncryptionUnavailable):
                ec.create_connection(7, 'hyperliquid', {'address': '0x' + 'c' * 40})
        assert ec.list_connections(7) == [], 'a row was written in the clear'
        assert any(r.levelno == logging.ERROR and 'exchange' in r.getMessage().lower()
                   for r in caplog.records)

    def test_telegram_token_path_still_fails_open(self, monkeypatch):
        """The Telegram bot-token policy is unchanged: plaintext fallback."""
        from carrac.secret_box import encrypt_token
        self._unset_key(monkeypatch)
        assert encrypt_token('123:abc') == '123:abc'
